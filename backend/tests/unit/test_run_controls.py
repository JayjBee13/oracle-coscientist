"""The parts of launching and controlling a run that need no database and no process.

Plan C4's action table and the launcher's config normalisation are decided here, and both
are contracts other layers read: the frontend renders its control bar from the same table,
and every run's stored config has to be the frozen C5 shape whatever the caller sent.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.db.engine_models import LIFECYCLES
from app.engine.runners import ROLES
from app.engine.spawn import terminate_pid_tree
from app.engine.store import Unchanged
from app.schemas.launch import RunControlRequest
from app.services.runs import controls
from app.services.runs.controls import (
    ACTIONS,
    CONTINUE_LIFECYCLES,
    LEGAL_ACTIONS,
    PID_TRUST_SECONDS,
    _assert_affordable,
    _pid_is_current,
    _posix_supervisor_matches,
    allowed_actions,
    quiesce_supervisors,
)
from app.services.runs.launcher import (
    BACKEND_ROOT,
    LaunchRefused,
    _assert_no_secrets,
    base_prompt_hash,
    resolve_config,
    supervisor_env,
)

# --- C4's action table --------------------------------------------------------------------

# Written out literally rather than derived from LEGAL_ACTIONS: a test that re-derives the
# table it is checking passes for a table with any content at all.
EXPECTED = {
    "queued": ("force_stop",),
    "running": ("pause", "stop", "finish", "force_stop"),
    "pausing": ("stop", "force_stop"),
    "paused": ("resume", "stop", "finish", "force_stop"),
    "stopping": ("force_stop",),
    "stopped": ("continue",),
    "finishing": ("force_stop",),
    "completed": ("continue",),
    "failed": (),
    "lost": (),
}


@pytest.mark.parametrize("lifecycle", LIFECYCLES)
def test_the_action_table_matches_the_plan(lifecycle: str):
    assert allowed_actions(lifecycle) == EXPECTED[lifecycle]


def test_every_lifecycle_has_a_verdict():
    """A lifecycle nobody thought about would silently get no controls at all."""
    assert set(EXPECTED) == set(LIFECYCLES)


def test_a_terminal_run_can_only_ever_be_continued():
    """Nothing about a finished run can be paused, stopped or finished — it is already over.

    The one thing left to ask of it is more rounds, and only of the two endings that reached
    a clean boundary and wrote a report.
    """
    for lifecycle in ("completed", "stopped"):
        assert allowed_actions(lifecycle) == ("continue",)
    for lifecycle in ("failed", "lost"):
        assert allowed_actions(lifecycle) == ()


def test_continue_is_offered_on_the_endings_that_finished_and_no_others():
    """A `failed` run stopped for a reason nobody has diagnosed and has no report to build
    on; a `lost` one died without saying anything at all. Extending either would make the
    Continue button mean "resurrect" some of the time and "go deeper" the rest, so both are
    refused and the operator is left with the diagnosis they actually need."""
    continuable = {
        lifecycle for lifecycle in LIFECYCLES if "continue" in allowed_actions(lifecycle)
    }
    assert continuable == {"completed", "stopped"}
    assert continuable == set(CONTINUE_LIFECYCLES)


def test_continue_and_resume_are_never_offered_together():
    """They are different questions — "carry on a run that never finished" against "give a
    finished one more rounds" — and a control bar that offered both at once would be asking
    the scientist to tell them apart from the label alone."""
    for lifecycle in LIFECYCLES:
        offered = set(allowed_actions(lifecycle))
        assert not {"resume", "continue"} <= offered


def test_force_stop_reaches_every_state_a_supervisor_could_be_in():
    """The kill switch has to work in the states where the cooperative path might not."""
    reachable = {
        lifecycle for lifecycle in LIFECYCLES if "force_stop" in allowed_actions(lifecycle)
    }
    assert reachable == {"queued", "running", "pausing", "paused", "stopping", "finishing"}


def test_the_action_list_and_the_table_agree():
    assert set(ACTIONS) == set(LEGAL_ACTIONS)


@pytest.mark.parametrize("lifecycle", LIFECYCLES)
def test_an_imported_run_offers_nothing_whatever_lifecycle_it_wears(lifecycle: str):
    """Imported runs are all `completed`, which used to be a lifecycle with no controls and
    since `continue` is not. The table has to refuse on the run's source or the control bar
    would light up a button that can only ever error against a historical record."""
    assert allowed_actions(lifecycle, source="imported") == ()


# --- config resolution --------------------------------------------------------------------


def test_the_resolved_config_freezes_a_model_table():
    """A run must not change models under itself when a default moves."""
    config = resolve_config({"rounds": 2, "budget_calls": 30, "budget_usd": 2.0})

    table = {row["role"]: row for row in config["model_table"]}
    assert set(table) == set(ROLES)
    assert all(row["model"] and row["effort"] for row in table.values())


def test_a_tier_moves_effort_and_never_the_model_a_step_runs_on():
    """The contract in one assertion: the same nine models at every tier, different efforts.
    A cheap tier that swapped a heavy step onto the light model would be a downgrade nobody
    could see in the UI."""
    tables = {
        tier: {row["role"]: row for row in resolve_config({"model_tier": tier})["model_table"]}
        for tier in ("max", "high", "med")
    }
    models = [{role: row["model"] for role, row in t.items()} for t in tables.values()]
    efforts = [{role: row["effort"] for role, row in t.items()} for t in tables.values()]

    assert models[0] == models[1] == models[2]
    assert efforts[0] != efforts[1] != efforts[2]
    assert models[0]["generation"] == "fable"
    assert models[0]["proximity"] == "claude-opus-5"


def test_launching_on_openai_puts_the_whole_table_on_codex_models():
    config = resolve_config({"provider": "openai", "model_tier": "max"})

    table = {row["role"]: row["model"] for row in config["model_table"]}
    assert table["generation"] == "gpt-5.6-sol"
    assert table["proximity"] == "gpt-5.6-luna"
    assert config["provider"] == "openai"


def test_a_stored_tier_name_still_resolves_into_a_launchable_config():
    """Stored runs say `balanced`/`quality`/`standard`/`maximum`. Cloning one re-resolves the
    table from the tier name, so an unmapped name would make them un-runnable-again — while
    their own stored table stays exactly as it was."""
    assert resolve_config({"model_tier": "balanced"})["model_tier"] == "high"
    assert resolve_config({"model_tier": "quality"})["model_table"] == (
        resolve_config({"model_tier": "max"})["model_table"]
    )
    assert resolve_config({"model_tier": "standard"})["model_table"] == (
        resolve_config({"model_tier": "high"})["model_table"]
    )


def test_per_role_overrides_are_merged_into_the_frozen_table():
    config = resolve_config(
        {"model_overrides": {"ranking": {"model": "claude-opus-5", "effort": "xhigh"}}}
    )

    table = {row["role"]: row for row in config["model_table"]}
    assert table["ranking"] == {
        "role": "ranking", "model": "claude-opus-5", "effort": "xhigh"
    }
    assert table["generation"]["model"] == "fable"
    # Kept in the config too, so "run again" inherits the choice rather than reverting.
    assert config["model_overrides"] == {
        "ranking": {"model": "claude-opus-5", "effort": "xhigh"}
    }


def test_a_step_may_be_overridden_onto_the_other_providers_model():
    """Mixing is the point of the override field, not an accident of it."""
    config = resolve_config(
        {"provider": "anthropic", "model_overrides": {"proximity": {"model": "gpt-5.6-luna"}}}
    )

    table = {row["role"]: row["model"] for row in config["model_table"]}
    assert table["proximity"] == "gpt-5.6-luna"
    assert table["generation"] == "fable"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ranking": {"model": "claude-sonnet-5"}},
        {"ranking": {"model": "claude-fable-5"}},
        {"proximity": {"model": "claude-haiku-4-5"}},
        {"nonesuch": {"model": "fable"}},
        {"ranking": {"effort": "colossal"}},
    ],
)
def test_an_override_the_policy_refuses_refuses_the_launch(overrides: dict):
    with pytest.raises(LaunchRefused):
        resolve_config({"model_overrides": overrides})


def test_unknown_keys_are_dropped_and_missing_ones_defaulted():
    config = resolve_config({"rounds": 3, "nonsense": True, "model_table": [{"role": "spoof"}]})

    assert "nonsense" not in config
    assert config["rounds"] == 3
    assert config["generation_batch"] == 8, "a default the caller did not send"
    assert [row["role"] for row in config["model_table"]] == list(ROLES), (
        "a caller-supplied model table is never trusted"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"rounds": 0},
        {"rounds": 999},
        {"generation_batch": 0},
        {"budget_calls": 0},
        {"model_tier": "cheap"},
        {"runner": "nonesuch"},
        {"provider": "google"},
        {"grounding_depth": "exhaustive"},
    ],
)
def test_a_config_outside_its_bounds_is_refused(override: dict):
    with pytest.raises(LaunchRefused):
        resolve_config({"budget_calls": 30, "budget_usd": 2.0, **override})


def test_the_call_ceiling_is_mandatory_and_the_others_are_not():
    """Calls are the governor. Dollars and minutes are opt-in: role calls go through the
    CLI on a subscription, so a dollar ceiling gates on telemetry rather than on money."""
    with pytest.raises(LaunchRefused, match="budget_calls"):
        resolve_config({"budget_calls": 0})

    config = resolve_config({"budget_calls": 30})
    assert config["budget_usd"] is None
    assert config["wall_clock_minutes"] is None


@pytest.mark.parametrize("ceiling", ["budget_usd", "wall_clock_minutes"])
def test_an_optional_ceiling_that_is_set_must_be_a_real_one(ceiling: str):
    """Zero would read back as "no ceiling" — the opposite of what asking for one means."""
    with pytest.raises(LaunchRefused, match=ceiling):
        resolve_config({"budget_calls": 30, ceiling: 0})
    with pytest.raises(LaunchRefused, match=ceiling):
        resolve_config({"budget_calls": 30, ceiling: -1})


@pytest.mark.parametrize("ceiling", ["budget_usd", "wall_clock_minutes"])
def test_an_optional_ceiling_survives_into_the_frozen_config(ceiling: str):
    assert resolve_config({"budget_calls": 30, ceiling: 45})[ceiling] == 45


# --- the continue request -------------------------------------------------------------------
#
# The extension arrives on the same body as every other control, so the model is what stops
# the two fields drifting onto an action that would silently ignore them.


def test_continue_must_say_how_many_more_rounds():
    """"Continue" with no number is not a request anybody can act on."""
    with pytest.raises(ValidationError, match="add_rounds"):
        RunControlRequest(action="continue")


def test_continue_carries_its_increment_and_an_optional_raised_ceiling():
    request = RunControlRequest(action="continue", add_rounds=2, budget_calls=400)

    assert request.action == "continue"
    assert (request.add_rounds, request.budget_calls) == (2, 400)
    assert RunControlRequest(action="continue", add_rounds=1).budget_calls is None


def test_a_cost_ceiling_can_be_raised_removed_or_left_alone():
    """Three states on one optional field, and JSON can only tell them apart one way.

    `null` is a request in its own right here — take the ceiling off — so it cannot double
    as "I did not mention this". The body keeps the difference in `model_fields_set` and
    `cost_ceiling` is where it stops being a Pydantic detail.
    """
    absent = RunControlRequest(action="continue", add_rounds=1)
    cleared = RunControlRequest.model_validate(
        {"action": "continue", "add_rounds": 1, "budget_usd": None}
    )
    raised = RunControlRequest.model_validate(
        {"action": "continue", "add_rounds": 1, "budget_usd": 12.5}
    )

    assert isinstance(absent.cost_ceiling, Unchanged)
    assert cleared.cost_ceiling is None
    assert raised.cost_ceiling == 12.5


@pytest.mark.parametrize("action", ["pause", "resume", "stop", "finish", "force_stop"])
def test_no_other_action_may_carry_an_extension(action: str):
    """A `finish` with a raised budget is a request that will not do what it says. Ignoring
    the field would answer 200 to a caller who thinks they just raised a ceiling."""
    with pytest.raises(ValidationError, match="continue"):
        RunControlRequest(action=action, budget_calls=400)
    with pytest.raises(ValidationError, match="continue"):
        RunControlRequest(action=action, add_rounds=3)
    with pytest.raises(ValidationError, match="continue"):
        RunControlRequest(action=action, budget_usd=12.0)
    # And an explicit null too: on this field it asks for something, so refusing it only
    # when it holds a number would let "remove the ceiling" through on a `stop`.
    with pytest.raises(ValidationError, match="continue"):
        RunControlRequest.model_validate({"action": action, "budget_usd": None})


@pytest.mark.parametrize(
    "body",
    [{"add_rounds": 0}, {"add_rounds": 99}, {"budget_calls": 0}, {"budget_usd": 0}],
)
def test_an_extension_outside_its_bounds_is_refused_at_the_edge(body: dict):
    with pytest.raises(ValidationError):
        RunControlRequest(action="continue", **{"add_rounds": 2, **body})


def test_a_control_body_still_refuses_anything_it_does_not_know():
    with pytest.raises(ValidationError):
        RunControlRequest(action="continue", add_rounds=1, model_tier="maximum")


# --- what a continue can afford -------------------------------------------------------------
#
# Decided before anything is written, so a request the run could not act on costs a refusal
# rather than a process launch. Two ceilings, judged differently: the call budget because it
# is what actually governs a run, and the dollar ceiling because runs launched before it
# became optional still carry one and must not be stranded by it.


def _ended(**overrides) -> dict:
    """A run that finished on a $5.00 cost ceiling it has just passed — the owner's run."""
    return {
        "calls_used": 30,
        "budget_calls": 67,
        "spend_usd": 5.056,
        "budget_usd": 5.0,
        **overrides,
    }


def test_a_continue_that_leaves_the_run_over_its_cost_ceiling_is_refused():
    """It would come straight back out of the loop, so the refusal names the field that
    fixes it — and never tells the scientist to start a new run, which is the one answer
    that throws the science away."""
    with pytest.raises(LaunchRefused) as raised:
        _assert_affordable(_ended(), 200)

    message = str(raised.value)
    assert "raise or remove the cost ceiling" in message.lower()
    assert "budget_usd" in message
    assert "new run" not in message.lower()


def test_a_cost_ceiling_raised_past_the_spend_is_affordable():
    _assert_affordable(_ended(), 200, 10.0)


def test_a_cost_ceiling_raised_but_not_past_the_spend_is_still_refused():
    """Higher than the old ceiling and still below what the run has spent: allowed by the
    store, which only refuses a *lowering*, and useless to the run."""
    with pytest.raises(LaunchRefused, match="Raise or remove"):
        _assert_affordable(_ended(), 200, 5.02)


def test_removing_the_cost_ceiling_makes_a_stranded_run_affordable():
    """The rescue. Nothing about the run changed — only the ceiling it is judged against."""
    _assert_affordable(_ended(), 200, None)


def test_a_run_with_no_cost_ceiling_is_never_refused_on_one():
    """The system default since dollars stopped governing runs: there is nothing to be over."""
    _assert_affordable(_ended(budget_usd=0, spend_usd=94.2), 200)


def test_the_call_ceiling_is_still_judged_on_its_own_terms():
    """Two calls held back for the report plus one for the next step. Unaffected by any of
    the above — the dollar ceiling was never the ceiling that governs."""
    with pytest.raises(LaunchRefused, match="at least 33"):
        _assert_affordable(_ended(budget_usd=0), 31, None)


# --- prompt identity ----------------------------------------------------------------------


def test_the_prompt_hash_survives_reformatting():
    """`/compare` uses this to tell an A/B of one prompt from two unrelated runs."""
    assert base_prompt_hash("Why do lakes bloom?") == base_prompt_hash("  Why do\n lakes  bloom? ")
    assert base_prompt_hash("Why do lakes bloom?") != base_prompt_hash("Why do rivers bloom?")


# --- secrets ------------------------------------------------------------------------------


def test_a_dsn_in_the_supervisor_argv_is_refused():
    """A command line is public. The June rule: connection strings go in the environment."""
    with pytest.raises(LaunchRefused, match="://"):
        _assert_no_secrets(["python.exe", "-m", "app.engine.supervisor", "--dsn", "postgres://x"])


def test_the_supervisor_environment_carries_the_settings_the_child_resolves(tmp_path):
    from app.core.config import Settings

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://user:pw@host:2000/db",
        COSCIENTIST_RUNS_ROOT=str(tmp_path / "runs"),
    )

    env = supervisor_env(settings)

    assert env["DATABASE_URL"] == settings.database_url
    assert env["COSCIENTIST_RUNS_ROOT"] == str(settings.runs_root)
    assert env["PYTHONUTF8"] == "1", "Windows consoles are cp1252 until told otherwise"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(BACKEND_ROOT), (
        "the child's cwd is the run workdir, so it can only import the app off PYTHONPATH"
    )


# --- verify before kill -------------------------------------------------------------------

# The plan's rule is that a pid is killed only once it has been shown to be the supervisor
# it is recorded as: the right image *and* a heartbeat recent enough to vouch for it. The
# image alone cannot do it — this application runs under python.exe itself, so a recycled
# pid pointing at the API server would pass an image check and `taskkill /T` would take the
# backend down with it.


def _run_row(**overrides) -> dict:
    return {
        "id": "0f9d6a1e-0000-4000-8000-000000000001",
        "supervisor_pid": 4242,
        "heartbeat_at": datetime.now(UTC).isoformat(),
        "created_at": datetime.now(UTC).isoformat(),
        **overrides,
    }


def test_a_beating_run_vouches_for_its_pid():
    assert _pid_is_current(_run_row()) is True


def test_a_long_silent_run_no_longer_vouches_for_its_pid():
    """Sixty missed beats. Whatever holds that pid now, it is not writing to this row."""
    stale = datetime.now(UTC) - timedelta(seconds=PID_TRUST_SECONDS + 60)
    assert _pid_is_current(_run_row(heartbeat_at=stale.isoformat())) is False


def test_a_run_that_never_beat_at_all_vouches_for_nothing():
    assert _pid_is_current(_run_row(heartbeat_at=None, created_at=None)) is False


def test_a_supervisor_slow_enough_to_worry_about_is_still_killable():
    """A wedged supervisor is exactly what force-stop is for; the window has to allow it."""
    quiet = datetime.now(UTC) - timedelta(seconds=PID_TRUST_SECONDS / 2)
    assert _pid_is_current(_run_row(heartbeat_at=quiet.isoformat())) is True


def test_linux_supervisor_identity_uses_module_run_id_and_non_zombie_state(tmp_path):
    run_id = "0f9d6a1e-0000-4000-8000-000000000001"
    process = tmp_path / "4242"
    process.mkdir()
    (process / "stat").write_text("4242 (python) S 1 2 3\n", encoding="utf-8")
    (process / "cmdline").write_bytes(
        b"python\0-m\0app.engine.supervisor\0--run-id\0" + run_id.encode() + b"\0"
    )

    assert _posix_supervisor_matches(4242, run_id=None, proc_root=tmp_path) is True
    assert (
        _posix_supervisor_matches(
            4242,
            run_id=UUID(run_id),
            proc_root=tmp_path,
        )
        is True
    )
    assert (
        _posix_supervisor_matches(
            4242,
            run_id=UUID("0f9d6a1e-0000-4000-8000-000000000002"),
            proc_root=tmp_path,
        )
        is False
    )

    (process / "stat").write_text("4242 (python) Z 1 2 3\n", encoding="utf-8")
    assert _posix_supervisor_matches(4242, run_id=None, proc_root=tmp_path) is False


def test_the_kill_gate_refuses_to_kill_the_process_calling_it():
    """`taskkill /T` on our own pid takes the whole test session, or the backend, with it."""
    assert terminate_pid_tree(os.getpid(), expected_images=("python.exe",)) == "refused"


class _ShutdownStore:
    def __init__(self, run: dict) -> None:
        self.run = run
        self.events: list[tuple[str, dict]] = []

    def active_runs(self, *, lifecycles):
        return [dict(self.run)] if self.run["lifecycle"] in lifecycles else []

    def get_run(self, _run_id):
        return dict(self.run)

    def set_control(self, _run_id, action):
        self.run["control_requested"] = action
        return dict(self.run)

    def park_for_shutdown(self, _run_id, *, expected_pid):
        previous = self.run["lifecycle"]
        if previous not in controls.LANE_LIFECYCLES:
            return {"changed": False, "previous": previous, "run": dict(self.run)}
        if self.run["supervisor_pid"] != expected_pid:
            return {"changed": False, "previous": previous, "run": dict(self.run)}
        self.run.update(
            lifecycle="paused",
            control_requested=None,
            supervisor_pid=None,
            heartbeat_at=None,
        )
        return {"changed": True, "previous": previous, "run": dict(self.run)}

    def emit(self, _run_id, event_type, payload, *, round=None):
        self.events.append((event_type, payload))
        return {"seq": len(self.events), "round": round}


def test_managed_shutdown_kills_then_parks_a_supervisor(monkeypatch):
    store = _ShutdownStore(
        _run_row(lifecycle="running", source="app", control_requested=None)
    )
    killed: list[int] = []
    monkeypatch.setattr(controls, "_supervisor_alive", lambda _pid, **_kwargs: True)
    monkeypatch.setattr(
        controls,
        "terminate_pid_tree",
        lambda pid, **_kwargs: killed.append(pid) or "killed",
    )

    outcome = quiesce_supervisors(store, grace_seconds=0)

    assert killed == [4242]
    assert outcome == {"parked": [store.run["id"]], "refused": []}
    assert store.run["lifecycle"] == "paused"
    assert store.run["supervisor_pid"] is None
    assert store.events[-1][1]["reason"] == "service_shutdown"


def test_managed_shutdown_does_not_overwrite_a_concurrent_completion(monkeypatch):
    store = _ShutdownStore(
        _run_row(lifecycle="finishing", source="app", control_requested="finish")
    )
    monkeypatch.setattr(controls, "_supervisor_alive", lambda _pid, **_kwargs: True)

    def finish_while_terminating(_pid, **_kwargs):
        store.run["lifecycle"] = "completed"
        return "vanished"

    monkeypatch.setattr(controls, "terminate_pid_tree", finish_while_terminating)

    outcome = quiesce_supervisors(store, grace_seconds=0)

    assert outcome == {"parked": [], "refused": []}
    assert store.run["lifecycle"] == "completed"
    assert store.events == []


def test_managed_shutdown_refuses_an_untrusted_pid(monkeypatch):
    stale = datetime.now(UTC) - timedelta(seconds=PID_TRUST_SECONDS + 1)
    store = _ShutdownStore(
        _run_row(
            lifecycle="running",
            source="app",
            control_requested=None,
            heartbeat_at=stale.isoformat(),
        )
    )
    monkeypatch.setattr(controls, "_supervisor_alive", lambda _pid, **_kwargs: True)

    outcome = quiesce_supervisors(store, grace_seconds=0)

    assert outcome == {"parked": [], "refused": [store.run["id"]]}
    assert store.run["lifecycle"] == "running"

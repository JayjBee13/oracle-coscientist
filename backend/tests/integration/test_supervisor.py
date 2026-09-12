"""The supervisor as the operating system sees it: a real detached process per run.

Nothing here is mocked. Every test that says "launch" starts an actual `python -m
app.engine.supervisor` in an actual child process, against the session's throwaway schema,
and then asks the database what happened — because the failures this layer exists to
prevent are all failures of processes and pids, and a fake process has neither.

The model is still fake: every run here is a demo run (`runner: demo`, `FakeRunner`), so no
test in this file can reach the Claude CLI or spend anything. `COSCIENTIST_DEMO_LATENCY`
gives the scripted model a heartbeat's worth of thinking time, which is what makes it
possible to press pause on a run that would otherwise be over before the request lands.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.engine.spawn import recorded_spawns, terminate_pid_tree
from app.engine.store import RunStore
from app.engine.supervisor import CRASH_ENV, DEMO_LATENCY_ENV
from app.main import create_app
from app.services.runs import controls, launcher
from tests.support.lanes import release_lanes

GOAL = "Why do some lakes bloom under falling nutrient loads?"

TERMINAL = frozenset({"completed", "stopped", "failed", "lost"})

# One round, two hypotheses, one match: enough for every step of C3's loop to execute and
# for the projection to have something to write, and small enough that a dozen of these
# run inside a normal test session.
FAST: dict[str, Any] = {
    "rounds": 1,
    "generation_batch": 2,
    "matches_per_round": 1,
    "evolve_top_k": 1,
    "budget_calls": 60,
    "budget_usd": 20.0,
    "graft": {"enabled": False},
    "runner": "demo",
}

DEAD_PID = 2147483647
"""Larger than any pid Windows hands out, and the biggest int4 the column holds."""


# --------------------------------------------------------------------------- the bench


class Bench:
    """A store, settings pointed at a temporary runs root, and cleanup that always fires."""

    def __init__(self, store: RunStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.created: list[UUID] = []

    def launch(self, **overrides: Any) -> launcher.LaunchResult:
        config = {**FAST, **overrides.pop("config", {})}
        result = launcher.launch_run(
            self.store,
            question=overrides.pop("question", GOAL),
            harness=overrides.pop("harness", "demo"),
            config=config,
            settings=self.settings,
            **overrides,
        )
        self.created.append(result.run_id)
        return result

    def adopt(self, run_id: UUID) -> UUID:
        self.created.append(run_id)
        return run_id

    def occupy_lane(self, harness: str = "claude") -> UUID:
        """A run that holds a harness lane without a process behind it.

        Its heartbeat is fresh, which is what spares it from the reconciler; it has no pid,
        so nothing in this file can be tricked into killing the test runner.
        """
        run_id = self.store.create_run(
            question=GOAL,
            prompt=GOAL,
            harness=harness,
            config={**FAST, "runner": "claude", "model_table": []},
            lifecycle="running",
        )
        self.store.heartbeat(run_id)
        return self.adopt(run_id)

    def run(self, run_id: UUID) -> dict[str, Any]:
        row = self.store.get_run(run_id)
        assert row is not None, f"run {run_id} disappeared"
        return row

    def lifecycle(self, run_id: UUID) -> str:
        return str(self.run(run_id)["lifecycle"])

    def workdir(self, run_id: UUID) -> Path:
        return self.settings.workdir_for(str(self.run(run_id)["engine_run_id"]))

    def events(self, run_id: UUID, *types: str) -> list[dict[str, Any]]:
        factory = self.store._session_factory  # noqa: SLF001 — tests read the whole log
        with factory() as session:
            rows = session.execute(
                text(
                    "select seq, round, type, payload from run_events "
                    "where run_id = :run_id order by seq"
                ),
                {"run_id": str(run_id)},
            ).mappings()
            return [dict(row) for row in rows if not types or row["type"] in types]

    def age_heartbeat(self, run_id: UUID, *, seconds: int, pid: int | None) -> None:
        """Backdate a run's heartbeat, the way a supervisor that stopped writing would."""
        factory = self.store._session_factory  # noqa: SLF001
        with factory() as session:
            session.execute(
                text(
                    "update runs set heartbeat_at = now() - make_interval(secs => :age), "
                    "supervisor_pid = :pid where id = :run_id"
                ),
                {"age": seconds, "pid": pid, "run_id": str(run_id)},
            )
            session.commit()

    def cleanup(self) -> None:
        for run_id in reversed(self.created):
            row = self.store.get_run(run_id)
            if row is None:
                continue
            pid = row.get("supervisor_pid")
            # Never the test runner itself: some fixtures record our own pid on purpose.
            if pid and int(pid) != os.getpid():
                terminate_pid_tree(int(pid), expected_images=tuple(controls.SUPERVISOR_IMAGES))
            if row["lifecycle"] not in TERMINAL:
                self.store.set_lifecycle(run_id, "stopped")
            self.store.soft_delete(run_id)


@pytest.fixture
def bench(tmp_path: Path, isolated_schema: str) -> Iterator[Bench]:
    settings = Settings(
        APP_ENV="test",
        IMPORT_ON_STARTUP=False,
        COSCIENTIST_RUNS_ROOT=str(tmp_path / "engines" / "runs"),
    )
    store = RunStore(settings=settings)
    # Every test here launches, so every test needs its lane. Freeing abandoned ones at
    # setup as well as teardown is what makes this file pass in any order pytest picks: a
    # module that ran earlier and left a run queued must not decide whether this one runs.
    # A run whose supervisor is still alive is spared — that is a process shutting down,
    # not an abandoned row.
    release_lanes(store, skip_live_processes=True)
    harness = Bench(store, settings)
    try:
        yield harness
    finally:
        harness.cleanup()


@pytest.fixture
def slow_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the scripted model enough latency that a control can land mid-run."""
    monkeypatch.setenv(DEMO_LATENCY_ENV, "0.4")


# ---------------------------------------------------------------------------- waiting


def wait_for(bench: Bench, run_id: UUID, wanted: frozenset[str] | str, *, timeout=180.0) -> str:
    """Poll until the run reaches one of `wanted`, or fail with the supervisor's own log.

    A terminal lifecycle is not the supervisor's last act: it writes the overview, moves the
    lifecycle, and only *then* emits `run_finished` and projects the artifacts. A test that
    stopped polling the moment the lifecycle flipped would race the writes it came to check,
    and would do it intermittently, which is worse than failing. So once the run is terminal
    we wait for the process to go too — after which everything it was going to write is
    written.
    """
    targets = frozenset({wanted}) if isinstance(wanted, str) else wanted
    deadline = time.monotonic() + timeout
    lifecycle = ""
    while time.monotonic() < deadline:
        lifecycle = bench.lifecycle(run_id)
        if lifecycle in targets:
            if lifecycle in TERMINAL:
                wait_for_the_supervisor_to_exit(bench, run_id)
            return lifecycle
        time.sleep(0.2)
    raise AssertionError(
        f"run {run_id} was {lifecycle!r} after {timeout}s, waiting for {sorted(targets)}.\n"
        f"--- supervisor.log ---\n{_log_tail(bench.workdir(run_id))}"
    )


def wait_for_the_supervisor_to_exit(bench: Bench, run_id: UUID, *, timeout=60.0) -> None:
    """Wait out the recorded pid. A run with no process behind it returns at once."""
    pid = bench.run(run_id).get("supervisor_pid")
    # Some fixtures record this process's pid on purpose, to stand in for a live supervisor;
    # waiting for the test session to exit would hang until the timeout.
    if not pid or int(pid) == os.getpid():
        return
    wait_until(
        lambda: not controls._supervisor_alive(int(pid)),  # noqa: SLF001
        timeout=timeout,
        what=f"supervisor pid {pid} exiting",
    )


def wait_until(predicate, *, timeout=60.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _stamp(value: str | None) -> datetime:
    assert value, "expected a timestamp"
    return datetime.fromisoformat(value)


def _log_tail(workdir: Path, limit: int = 5000) -> str:
    path = workdir / launcher.SUPERVISOR_LOG
    if not path.is_file():
        return "(no supervisor.log — the process never started)"
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


# ------------------------------------------------------------------------- happy path


def test_a_demo_run_completes_end_to_end_in_a_real_child_process(bench: Bench):
    launched = bench.launch()

    assert launched.pid > 0
    at_launch = bench.run(launched.run_id)
    assert at_launch["supervisor_pid"] == launched.pid, "force-stop needs the pid immediately"
    # The launcher leaves the run queued; moving it to running is the supervisor's first
    # act, and on a fast machine it may already have done so by the time we look.
    assert at_launch["lifecycle"] in ("queued", "running")

    assert wait_for(bench, launched.run_id, "completed") == "completed"

    snapshot = bench.store.snapshot(launched.run_id)
    summary = snapshot["run"]
    assert summary["round"] == 1
    assert summary["counts"]["active"] + summary["counts"]["rejected"] >= 2
    assert summary["has_overview"], "every ending writes the report"
    assert summary["calls_used"] > 0, "the ledger recorded what the run spent"

    finished = bench.run(launched.run_id)
    assert _stamp(finished["heartbeat_at"]) > _stamp(at_launch["heartbeat_at"]), (
        "the heartbeat kept beating"
    )
    assert finished["control_requested"] is None


def test_the_run_writes_its_events_and_its_artifacts(bench: Bench):
    launched = bench.launch()
    wait_for(bench, launched.run_id, "completed")

    types = [event["type"] for event in bench.events(launched.run_id)]
    assert types[0] == "lifecycle_changed", "the first thing a supervisor says is that it is up"
    assert "round_started" in types
    assert "hypothesis_added" in types
    assert "review_recorded" in types
    assert "round_completed" in types
    assert types[-1] == "run_finished"

    workdir = bench.workdir(launched.run_id)
    assert (workdir / "state.json").is_file()
    assert (workdir / "research_overview.md").is_file()
    assert (workdir / "ideas_ranked.csv").is_file()
    assert list((workdir / "hypotheses").glob("h*.md")), "one markdown file per hypothesis"
    assert (workdir / launcher.SUPERVISOR_LOG).is_file(), "stdout went to the run's own log"


def test_the_supervisor_is_started_with_no_secret_on_its_command_line(bench: Bench):
    """A process table is public. The DSN reaches the child through the environment."""
    launched = bench.launch()

    spawns = [
        entry for entry in recorded_spawns() if "app.engine.supervisor" in " ".join(entry["argv"])
    ]
    assert spawns, "the supervisor spawn was not recorded for the safety doctor"
    argv = spawns[-1]["argv"]
    assert not any("://" in item for item in argv)
    assert argv[-2:] == ["--run-id", str(launched.run_id)]
    assert Path(argv[0]).resolve() == Path(sys.executable).resolve(), (
        "sys.executable, not a shim"
    )


# ------------------------------------------------------------------------- the controls


def test_pause_parks_the_run_and_resume_picks_it_up_without_paying_twice(
    bench: Bench, slow_demo: None
):
    launched = bench.launch(config={"rounds": 3, "generation_batch": 3})
    wait_for(bench, launched.run_id, "running", timeout=60)

    assert controls.apply_control(
        bench.store, launched.run_id, "pause", settings=bench.settings
    ) == {"accepted": True, "lifecycle": "running"}

    assert wait_for(bench, launched.run_id, "paused", timeout=90) == "paused"
    paused = bench.run(launched.run_id)
    assert paused["control_requested"] is None, "the flag is consumed by acting on it"
    wait_until(
        lambda: not controls._supervisor_alive(int(paused["supervisor_pid"])),  # noqa: SLF001
        what="the paused supervisor exiting",
    )

    controls.apply_control(bench.store, launched.run_id, "resume", settings=bench.settings)
    resumed = bench.run(launched.run_id)
    assert resumed["supervisor_pid"] != paused["supervisor_pid"], "resume is a new process"
    assert wait_for(bench, launched.run_id, "completed") == "completed"
    reviews = bench.store.list_reviews(launched.run_id)
    assert len(reviews) == len({review["hid"] for review in reviews}), (
        "a resumed run re-reviewed a hypothesis it had already paid to review"
    )


def test_managed_service_shutdown_parks_and_resumes_an_inflight_run(
    bench: Bench, slow_demo: None
):
    launched = bench.launch()
    assert wait_for(bench, launched.run_id, "running", timeout=30) == "running"
    original_pid = int(bench.run(launched.run_id)["supervisor_pid"])

    outcome = controls.quiesce_supervisors(bench.store, grace_seconds=0)

    assert outcome["refused"] == []
    assert outcome["parked"] in ([], [str(launched.run_id)])
    parked = bench.run(launched.run_id)
    assert parked["lifecycle"] == "paused"
    wait_until(
        lambda: not controls._supervisor_alive(original_pid),  # noqa: SLF001
        what="the managed-shutdown supervisor exiting",
    )

    accepted = controls.apply_control(
        bench.store, launched.run_id, "resume", settings=bench.settings
    )
    assert accepted == {"accepted": True, "lifecycle": "paused"}
    assert wait_for(bench, launched.run_id, TERMINAL, timeout=90) == "completed"


def test_finish_completes_the_current_round_and_stops_there(bench: Bench, slow_demo: None):
    launched = bench.launch(config={"rounds": 4})
    wait_for(bench, launched.run_id, "running", timeout=60)

    controls.apply_control(bench.store, launched.run_id, "finish", settings=bench.settings)

    assert wait_for(bench, launched.run_id, "completed") == "completed"
    run = bench.run(launched.run_id)
    assert run["round"] < 4, "finish means stop early, not run to the target"
    assert bench.store.snapshot(launched.run_id)["run"]["has_overview"]


def test_stop_still_writes_the_overview(bench: Bench, slow_demo: None):
    """The Confirm step promises this in so many words, so it is enforced, not trusted."""
    launched = bench.launch(config={"rounds": 4})
    wait_for(bench, launched.run_id, "running", timeout=60)

    controls.apply_control(bench.store, launched.run_id, "stop", settings=bench.settings)

    assert wait_for(bench, launched.run_id, "stopped") == "stopped"
    assert bench.store.snapshot(launched.run_id)["run"]["has_overview"]
    finished = bench.events(launched.run_id, "run_finished")
    assert finished and finished[-1]["payload"]["lifecycle"] == "stopped"
    assert (bench.workdir(launched.run_id) / "research_overview.md").is_file()


def test_force_stop_kills_the_process_tree(bench: Bench, monkeypatch: pytest.MonkeyPatch):
    """The button for when the cooperative path is exactly what is not working."""
    monkeypatch.setenv(DEMO_LATENCY_ENV, "3")
    launched = bench.launch(config={"rounds": 20, "generation_batch": 9})
    wait_for(bench, launched.run_id, "running", timeout=60)
    pid = int(bench.run(launched.run_id)["supervisor_pid"])

    outcome = controls.apply_control(
        bench.store, launched.run_id, "force_stop", settings=bench.settings
    )

    assert outcome == {"accepted": True, "lifecycle": "stopped"}
    assert not controls._supervisor_alive(pid), "the pid survived taskkill /T /F"  # noqa: SLF001
    assert bench.lifecycle(launched.run_id) == "stopped"
    assert bench.events(launched.run_id, "lifecycle_changed")[-1]["payload"]["lifecycle"] == (
        "stopped"
    )


def test_force_stop_will_not_kill_a_live_pid_the_run_stopped_vouching_for(bench: Bench):
    """A recycled pid pointing at this application would take the backend down with `/T`.

    The run below records a pid that is alive and is a Python process — this test session —
    but has not been beaten for long enough that the record proves nothing. The kill must be
    refused, and the run left live rather than reported as stopped it never was.
    """
    run_id = bench.occupy_lane("demo")
    bench.age_heartbeat(run_id, seconds=int(controls.PID_TRUST_SECONDS) + 300, pid=os.getpid())

    outcome = controls.apply_control(bench.store, run_id, "force_stop", settings=bench.settings)

    assert outcome == {"accepted": True, "lifecycle": "running"}
    assert bench.lifecycle(run_id) == "running", "a refused kill must not report a stop"
    assert bench.run(run_id)["control_requested"] == "force_stop", (
        "the cooperative flag still stands: a supervisor that is alive will honour it"
    )


def test_force_stop_still_ends_a_run_whose_process_is_simply_gone(bench: Bench):
    """Nothing to kill is not a reason to refuse — the run still needs an ending."""
    run_id = bench.occupy_lane("demo")
    bench.age_heartbeat(run_id, seconds=int(controls.PID_TRUST_SECONDS) + 300, pid=DEAD_PID)

    outcome = controls.apply_control(bench.store, run_id, "force_stop", settings=bench.settings)

    assert outcome == {"accepted": True, "lifecycle": "stopped"}
    assert bench.lifecycle(run_id) == "stopped"


# ------------------------------------------------------------------------------ continue
#
# The one control that starts a run which had already ended. Everything else in this file
# is about stopping; these are about a scientist who ran Quick, liked it, and wants more —
# in the same run, on the same idea pool, rather than in a clone that starts from nothing.


def test_a_finished_run_continues_into_more_science_in_a_new_process(bench: Bench):
    """The whole feature, end to end and through the operating system.

    A real demo run finishes and writes its report. It is then given two more rounds, a
    second detached supervisor picks the *same* run id up, and what comes out the far side
    has to be a bigger run than went in — more hypotheses, more rounds, and a report that
    was rewritten rather than left describing round one.
    """
    launched = bench.launch(config={"rounds": 1})
    assert wait_for(bench, launched.run_id, "completed") == "completed"

    ended = bench.store.snapshot(launched.run_id)
    hypotheses = len(ended["leaderboard"])
    first_report = bench.run(launched.run_id)["engine_state"]["overview_md"]
    first_pid = bench.run(launched.run_id)["supervisor_pid"]
    on_disk = (bench.workdir(launched.run_id) / "research_overview.md").read_text(encoding="utf-8")
    assert hypotheses and first_report

    accepted = controls.apply_control(
        bench.store, launched.run_id, "continue", add_rounds=2, settings=bench.settings
    )

    assert accepted == {"accepted": True, "lifecycle": "queued"}
    assert bench.run(launched.run_id)["supervisor_pid"] != first_pid, "a new process drives it"
    assert wait_for(bench, launched.run_id, "completed") == "completed"

    continued = bench.store.snapshot(launched.run_id)
    run = bench.run(launched.run_id)
    assert continued["run"]["rounds_target"] == 3
    assert run["round"] == 3, "it ran the rounds it was given"
    assert run["engine_state"]["last_completed_round"] == 3
    assert len(continued["leaderboard"]) > hypotheses, "the extra rounds produced no ideas"
    assert {row["hid"] for row in ended["leaderboard"]} <= {
        row["hid"] for row in continued["leaderboard"]
    }, "the pool it was continued from was lost"
    assert continued["research"] is not None
    assert len(continued["research"]["decisions"]) == 3, "each checkpoint records its decision"
    assert continued["research"]["synthesis_round"] == 3
    assert run["engine_state"]["overview_md"] not in (None, "", first_report), (
        "the run ended on the report it wrote before it was extended"
    )
    assert (bench.workdir(launched.run_id) / "research_overview.md").read_text(
        encoding="utf-8"
    ) != on_disk, "the projected report on disk is still the old one"


def test_continuing_records_what_it_changed_and_by_how_much(bench: Bench):
    """Continuing is the one sanctioned exception to `config` being immutable after launch,
    so the Activity tab has to be able to say it happened without anyone inferring it."""
    launched = bench.launch(config={"rounds": 1, "budget_calls": 60})
    wait_for(bench, launched.run_id, "completed")

    controls.apply_control(
        bench.store,
        launched.run_id,
        "continue",
        add_rounds=2,
        budget_calls=120,
        settings=bench.settings,
    )
    wait_for(bench, launched.run_id, "completed")

    extended = bench.events(launched.run_id, "run_extended")
    assert len(extended) == 1
    assert extended[0]["payload"] == {
        "added_rounds": 2,
        "rounds_target": 3,
        "previous_rounds_target": 1,
        "budget_calls": 120,
        "previous_budget_calls": 60,
        "budget_usd": 20.0,
        "previous_budget_usd": 20.0,
        "previous_lifecycle": "completed",
    }
    config = bench.run(launched.run_id)["config"]
    assert (config["rounds"], config["budget_calls"]) == (3, 120)
    assert [row["role"] for row in config["model_table"]], "the model table survived intact"


def test_a_removed_cost_ceiling_is_audited_as_no_ceiling_and_not_as_zero(bench: Bench):
    """The record of the only thing that can undo a stopped-by-dollars run.

    Both sides are written, and the "after" side is `None` rather than the 0 the column
    holds — a reader a year from now has to be able to tell "this run's ceiling was taken
    off" from "this run was capped at nothing", and the database spells those the same way.
    """
    launched = bench.launch(config={"rounds": 1, "budget_usd": 12.5})
    wait_for(bench, launched.run_id, "completed")

    controls.apply_control(
        bench.store,
        launched.run_id,
        "continue",
        add_rounds=1,
        budget_usd=None,
        settings=bench.settings,
    )
    wait_for(bench, launched.run_id, "completed")

    payload = bench.events(launched.run_id, "run_extended")[0]["payload"]
    assert payload["previous_budget_usd"] == 12.5
    assert payload["budget_usd"] is None, "a removed ceiling recorded as $0.00"
    assert bench.run(launched.run_id)["config"]["budget_usd"] is None
    assert bench.run(launched.run_id)["budget_usd"] == 0, "and the gate enforces nothing"


def test_a_stopped_run_can_be_continued_and_counts_the_rounds_it_actually_did(
    bench: Bench, slow_demo: None
):
    """"Three more rounds" means three more rounds of work, not three more off the target
    a run never reached. A run stopped at round 1 of 4 and given one more ends at two."""
    launched = bench.launch(config={"rounds": 4, "generation_batch": 2})
    wait_for(bench, launched.run_id, "running", timeout=60)
    controls.apply_control(bench.store, launched.run_id, "stop", settings=bench.settings)
    assert wait_for(bench, launched.run_id, "stopped") == "stopped"

    done = int(bench.run(launched.run_id)["engine_state"].get("last_completed_round", 0))
    controls.apply_control(
        bench.store, launched.run_id, "continue", add_rounds=1, settings=bench.settings
    )

    assert wait_for(bench, launched.run_id, "completed") == "completed"
    run = bench.run(launched.run_id)
    assert run["rounds_target"] == done + 1
    assert run["engine_state"]["last_completed_round"] == done + 1


def test_continue_takes_the_harness_lane_and_redacts_an_unscoped_conflict(bench: Bench):
    """A service caller without requester context learns only that capacity is occupied."""
    launched = bench.launch(config={"rounds": 1})
    wait_for(bench, launched.run_id, "completed")
    holder = bench.occupy_lane("demo")

    with pytest.raises(launcher.LaneBusy) as raised:
        controls.apply_control(
            bench.store, launched.run_id, "continue", add_rounds=1, settings=bench.settings
        )

    assert raised.value.conflicting_run_id is None
    assert str(holder) not in str(raised.value)
    assert str(raised.value) == "Shared Demo capacity is currently in use. Try again later."
    assert bench.lifecycle(launched.run_id) == "completed", "the refused run stayed finished"
    assert bench.run(launched.run_id)["rounds_target"] == 1, "nothing was bumped"


def test_a_stale_lane_does_not_block_a_continue(bench: Bench):
    """Reconcile first, exactly like a launch: a crashed supervisor must not wedge the lane
    a finished run is trying to take back."""
    launched = bench.launch(config={"rounds": 1})
    wait_for(bench, launched.run_id, "completed")
    holder = bench.occupy_lane("demo")
    bench.age_heartbeat(holder, seconds=300, pid=DEAD_PID)

    controls.apply_control(
        bench.store, launched.run_id, "continue", add_rounds=1, settings=bench.settings
    )

    assert bench.lifecycle(holder) == "lost"
    assert wait_for(bench, launched.run_id, "completed") == "completed"


@pytest.mark.parametrize("lifecycle", ["running", "paused", "failed", "lost"])
def test_continue_is_refused_from_every_lifecycle_that_is_not_an_ending(
    bench: Bench, lifecycle: str
):
    """`running` and `paused` runs have not finished — pause, stop and finish are their
    controls. `failed` and `lost` ones stopped for a reason nobody has diagnosed and wrote
    no report, so extending them would be salvage dressed up as science."""
    run_id = bench.occupy_lane("demo")
    bench.store.set_lifecycle(run_id, lifecycle)

    with pytest.raises(controls.IllegalTransition) as raised:
        controls.apply_control(
            bench.store, run_id, "continue", add_rounds=2, settings=bench.settings
        )

    assert raised.value.lifecycle == lifecycle
    assert "continue" not in raised.value.allowed
    assert bench.run(run_id)["rounds_target"] == FAST["rounds"], "a refusal changed the run"


def test_an_imported_run_offers_no_controls_at_all_including_continue(bench: Bench):
    """The archived runs are records of research other engines did. They carry no resolved
    model table, so a supervisor would fail on its first step — and stamp `failed` over the
    only copy of a historical result. Every imported run is `completed`, so the refusal has
    to come from the run's *source* and not its lifecycle, or the control bar would offer
    a Continue button that can only ever error."""
    run_id = bench.adopt(
        bench.store.create_run(
            question=GOAL,
            prompt=GOAL,
            harness="demo",
            source="imported",
            source_version="v2",
            config={"rounds": 2},
            lifecycle="completed",
        )
    )

    with pytest.raises(controls.IllegalTransition) as raised:
        controls.apply_control(
            bench.store, run_id, "continue", add_rounds=2, settings=bench.settings
        )

    assert raised.value.allowed == (), "an imported run offers nothing"
    assert "imported run" in str(raised.value), "the refusal has to say why, not just no"
    assert controls.allowed_actions("completed", source="imported") == ()
    assert bench.lifecycle(run_id) == "completed"
    assert bench.run(run_id)["rounds_target"] == 2


def test_a_continue_with_no_room_left_for_the_report_is_refused(bench: Bench):
    """Two calls are reserved for the overview. A run continued without headroom for a step
    plus that reserve would come straight back out of the loop, so the refusal names the
    number the request would need instead of spending a process launch to find out."""
    launched = bench.launch(config={"rounds": 1})
    wait_for(bench, launched.run_id, "completed")
    used = bench.run(launched.run_id)["calls_used"]

    with pytest.raises(launcher.LaunchRefused, match=f"at least {used + 3}"):
        controls.apply_control(
            bench.store,
            launched.run_id,
            "continue",
            add_rounds=1,
            budget_calls=used + 2,
            settings=bench.settings,
        )

    assert bench.lifecycle(launched.run_id) == "completed"


def test_an_illegal_action_is_refused_with_the_legal_ones(bench: Bench):
    launched = bench.launch()
    wait_for(bench, launched.run_id, "completed")

    with pytest.raises(controls.IllegalTransition) as raised:
        controls.apply_control(bench.store, launched.run_id, "pause", settings=bench.settings)

    assert raised.value.lifecycle == "completed"
    assert raised.value.allowed == ("continue",), (
        "the refusal names what is available here, and on a finished run that is more rounds"
    )


# ---------------------------------------------------------------------------- failure


def test_a_supervisor_that_crashes_on_start_up_records_why(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
):
    """A run that dies silently is one whose operator learns about it from a blank screen."""
    monkeypatch.setenv(CRASH_ENV, "injected failure")

    launched = bench.launch()

    assert wait_for(bench, launched.run_id, "failed", timeout=90) == "failed"
    error = bench.run(launched.run_id)["error"]
    assert error is not None
    assert "injected failure" in error["message"]
    assert error["where"] == "supervisor"
    assert bench.events(launched.run_id, "run_failed"), "the Activity tab needs to see this"


def test_deleting_a_run_stops_its_supervisor(bench: Bench, monkeypatch: pytest.MonkeyPatch):
    """The heartbeat is a dead-man's switch: no row, no reason to keep spending."""
    monkeypatch.setenv(DEMO_LATENCY_ENV, "2")
    launched = bench.launch(config={"rounds": 20, "generation_batch": 9})
    wait_for(bench, launched.run_id, "running", timeout=60)
    pid = int(bench.run(launched.run_id)["supervisor_pid"])

    bench.store.soft_delete(launched.run_id)

    wait_until(
        lambda: not controls._supervisor_alive(pid),  # noqa: SLF001
        timeout=120,
        what="the supervisor noticing its run was deleted",
    )


# -------------------------------------------------------------------------- reconciler


def test_a_run_whose_supervisor_died_is_marked_lost(bench: Bench):
    run_id = bench.occupy_lane("demo")
    bench.age_heartbeat(run_id, seconds=300, pid=DEAD_PID)

    lost = controls.reconcile_runs(bench.store)

    assert [entry["run_id"] for entry in lost] == [str(run_id)]
    run = bench.run(run_id)
    assert run["lifecycle"] == "lost"
    assert run["error"]["type"] == "supervisor_lost"
    assert bench.events(run_id, "lifecycle_changed")[-1]["payload"]["lifecycle"] == "lost"


def test_the_reconciler_spares_a_stale_heartbeat_with_a_live_process(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
):
    """A supervisor blocked on a seven-minute grounded call is slow, not dead."""
    monkeypatch.setenv(DEMO_LATENCY_ENV, "2")
    launched = bench.launch(config={"rounds": 20, "generation_batch": 9})
    wait_for(bench, launched.run_id, "running", timeout=60)
    pid = int(bench.run(launched.run_id)["supervisor_pid"])
    bench.age_heartbeat(launched.run_id, seconds=300, pid=pid)

    assert controls.reconcile_runs(bench.store) == []
    assert bench.lifecycle(launched.run_id) == "running"


def test_the_reconciler_spares_a_fresh_heartbeat(bench: Bench):
    run_id = bench.occupy_lane("demo")

    assert controls.reconcile_runs(bench.store) == []
    assert bench.lifecycle(run_id) == "running"


def test_a_paused_run_is_never_reconciled(bench: Bench):
    """Paused runs have no supervisor by design; marking them lost would be a bug."""
    run_id = bench.occupy_lane("demo")
    bench.store.set_lifecycle(run_id, "paused")
    bench.age_heartbeat(run_id, seconds=3600, pid=DEAD_PID)

    assert controls.reconcile_runs(bench.store) == []
    assert bench.lifecycle(run_id) == "paused"


# --------------------------------------------------------------------------- the lane


def test_a_second_unscoped_run_on_a_busy_lane_is_refused_without_holder_details(bench: Bench):
    holder = bench.occupy_lane("claude")

    with pytest.raises(launcher.LaneBusy) as raised:
        bench.launch(harness="claude", config={"runner": "claude"})

    assert raised.value.conflicting_run_id is None
    assert str(holder) not in str(raised.value)
    assert raised.value.harness == "claude"


def test_a_demo_run_and_a_real_run_share_the_machine(bench: Bench):
    """Practising the interface while a real run is in flight is a thing people do."""
    bench.occupy_lane("claude")

    launched = bench.launch()

    assert wait_for(bench, launched.run_id, "completed") == "completed"


def test_a_stale_lane_is_freed_before_the_lane_check(bench: Bench):
    """A crashed supervisor must not wedge its lane until the next backend restart."""
    holder = bench.occupy_lane("demo")
    bench.age_heartbeat(holder, seconds=300, pid=DEAD_PID)

    launched = bench.launch()

    assert bench.lifecycle(holder) == "lost"
    assert wait_for(bench, launched.run_id, "completed") == "completed"


# ------------------------------------------------------------------------- kill switch


def test_halt_all_stops_every_live_run(bench: Bench, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(DEMO_LATENCY_ENV, "2")
    launched = bench.launch(config={"rounds": 20, "generation_batch": 9})
    idle = bench.occupy_lane("claude")
    wait_for(bench, launched.run_id, "running", timeout=60)
    pid = int(bench.run(launched.run_id)["supervisor_pid"])

    report = controls.halt_all(bench.store, settings=bench.settings)

    assert {str(launched.run_id), str(idle)} <= set(report["stopped"])
    assert bench.lifecycle(launched.run_id) == "stopped"
    assert bench.lifecycle(idle) == "stopped"
    assert not controls._supervisor_alive(pid)  # noqa: SLF001


# --------------------------------------------------------------------------- http api


@pytest.fixture
def client(bench: Bench) -> TestClient:
    return TestClient(create_app(bench.settings))


def test_post_runs_launches_and_answers_with_the_run_detail(bench: Bench, client: TestClient):
    response = client.post(
        "/api/runs",
        json={"question": GOAL, "harness": "demo", "config": FAST},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) >= {"run", "config", "model_table", "leaderboard", "budget"}
    assert body["run"]["harness"] == "demo"
    assert body["run"]["lifecycle"] in ("queued", "running")
    assert body["run"]["title"] == GOAL, "a title is derived from the question when absent"
    assert [row["role"] for row in body["model_table"]], "the resolved table is visible"

    run_id = bench.adopt(UUID(body["run"]["id"]))
    assert wait_for(bench, run_id, "completed") == "completed"


def test_post_runs_answers_409_with_the_run_holding_the_lane(bench: Bench, client: TestClient):
    holder = bench.occupy_lane("claude")

    response = client.post(
        "/api/runs",
        json={"question": GOAL, "harness": "claude", "config": {**FAST, "runner": "claude"}},
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "lane_busy"
    assert body["details"]["conflicting_run_id"] == str(holder)


def test_post_runs_refuses_a_run_with_no_spend_ceiling(client: TestClient):
    """Cost per call varies twentyfold. A run without both ceilings never gets created."""
    response = client.post(
        "/api/runs",
        json={"question": GOAL, "harness": "demo", "config": {**FAST, "budget_usd": 0}},
    )

    assert response.status_code == 422, response.text


def test_post_runs_refuses_a_run_with_nothing_to_research(client: TestClient):
    response = client.post("/api/runs", json={"harness": "demo", "config": FAST})

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_run_request"


def test_controls_over_http(bench: Bench, client: TestClient, slow_demo: None):
    launched = bench.launch(config={"rounds": 4})
    wait_for(bench, launched.run_id, "running", timeout=60)

    accepted = client.post(f"/api/runs/{launched.run_id}/controls", json={"action": "stop"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"accepted": True, "lifecycle": "running"}

    assert wait_for(bench, launched.run_id, "stopped") == "stopped"

    refused = client.post(f"/api/runs/{launched.run_id}/controls", json={"action": "pause"})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "illegal_transition"
    assert body["details"] == {
        "action": "pause",
        "lifecycle": "stopped",
        "allowed": ["continue"],
    }


def test_continue_over_http(bench: Bench, client: TestClient):
    """The frontend's whole view of this feature: one control call with an increment."""
    launched = bench.launch(config={"rounds": 1})
    wait_for(bench, launched.run_id, "completed")
    before = len(bench.store.snapshot(launched.run_id)["leaderboard"])

    accepted = client.post(
        f"/api/runs/{launched.run_id}/controls",
        json={"action": "continue", "add_rounds": 2, "budget_calls": 120},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"accepted": True, "lifecycle": "queued"}
    assert wait_for(bench, launched.run_id, "completed") == "completed"

    detail = client.get(f"/api/runs/{launched.run_id}/detail").json()
    assert detail["run"]["rounds_target"] == 3
    assert detail["run"]["budget_calls"] == 120
    assert len(detail["leaderboard"]) > before
    assert detail["run"]["has_overview"]
    assert client.get(f"/api/runs/{launched.run_id}/overview").text.strip(), (
        "the report a continued run ends on is the one the Report tab serves"
    )


def test_a_run_stopped_by_its_cost_ceiling_is_rescued_over_http(
    bench: Bench, client: TestClient
):
    """The owner's run, through the door they would actually use.

    A run capped in dollars stops short of its round target with most of its call budget
    unused, because a dollar ceiling gates on API-equivalent telemetry rather than on
    anything anybody is billed for. Continuing used to refuse this outright and tell the
    scientist to start a new run — throwing away the pool, the ratings and the guidance the
    stopped run had already paid for. It now takes `budget_usd: null` and carries on.
    """
    launched = bench.launch(config={"rounds": 3, "budget_calls": 500,
                                    "budget_usd": 0.01, "workflow": "tournament"})
    assert wait_for(bench, launched.run_id, "completed") == "completed"

    stopped = bench.run(launched.run_id)
    reasons = [e["payload"]["reason"] for e in bench.events(launched.run_id, "budget_warning")]
    assert "usd" in reasons, "this run was not stopped by its cost ceiling"
    assert float(stopped["spend_usd"]) >= float(stopped["budget_usd"])
    assert stopped["calls_used"] < 500, "the ceiling that governs was nowhere near reached"
    completed = int(stopped["engine_state"].get("last_completed_round", 0))
    assert 0 < completed < 3, "the ceiling was meant to stop it mid-run, not before it started"

    refused = client.post(
        f"/api/runs/{launched.run_id}/controls",
        json={"action": "continue", "add_rounds": 1},
    )
    assert refused.status_code == 400, refused.text
    assert "Raise or remove the cost ceiling" in refused.json()["message"]
    assert "new run" not in refused.json()["message"].lower(), (
        "the refusal must name the fix, not the thing that throws the science away"
    )

    accepted = client.post(
        f"/api/runs/{launched.run_id}/controls",
        json={"action": "continue", "add_rounds": 1, "budget_usd": None},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"accepted": True, "lifecycle": "queued"}
    assert wait_for(bench, launched.run_id, "completed") == "completed"

    after = bench.run(launched.run_id)
    assert after["engine_state"]["last_completed_round"] == completed + 1
    assert float(after["spend_usd"]) > float(stopped["spend_usd"]), "no work was done"
    assert client.get(f"/api/runs/{launched.run_id}/detail").json()["budget"]["budget_usd"] == 0


def test_continue_over_http_is_refused_on_a_run_that_has_not_finished(
    bench: Bench, client: TestClient, slow_demo: None
):
    launched = bench.launch(config={"rounds": 4})
    wait_for(bench, launched.run_id, "running", timeout=60)

    refused = client.post(
        f"/api/runs/{launched.run_id}/controls",
        json={"action": "continue", "add_rounds": 2},
    )

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "illegal_transition"
    assert body["details"]["action"] == "continue"
    assert body["details"]["allowed"] == ["pause", "stop", "finish", "force_stop"]


def test_continue_over_http_answers_409_with_the_run_holding_the_lane(
    bench: Bench, client: TestClient
):
    launched = bench.launch(config={"rounds": 1})
    wait_for(bench, launched.run_id, "completed")
    holder = bench.occupy_lane("demo")

    refused = client.post(
        f"/api/runs/{launched.run_id}/controls",
        json={"action": "continue", "add_rounds": 1},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "lane_busy"
    assert refused.json()["details"]["conflicting_run_id"] == str(holder)


def test_continue_over_http_needs_to_be_told_how_many_rounds(
    bench: Bench, client: TestClient
):
    """A 422 naming the field, rather than a run quietly extended by a number nobody chose."""
    launched = bench.launch(config={"rounds": 1})
    wait_for(bench, launched.run_id, "completed")

    refused = client.post(
        f"/api/runs/{launched.run_id}/controls", json={"action": "continue"}
    )

    assert refused.status_code == 422, refused.text
    assert "add_rounds" in refused.text
    assert bench.run(launched.run_id)["rounds_target"] == 1


def test_halt_all_over_http(bench: Bench, client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(DEMO_LATENCY_ENV, "2")
    launched = bench.launch(config={"rounds": 20, "generation_batch": 9})
    wait_for(bench, launched.run_id, "running", timeout=60)

    response = client.post("/api/admin/halt-all")

    assert response.status_code == 200, response.text
    assert str(launched.run_id) in response.json()["stopped"]
    assert bench.lifecycle(launched.run_id) == "stopped"


def test_a_run_launched_from_another_inherits_its_prompt_and_config(
    bench: Bench, client: TestClient
):
    first = bench.launch(config={"rounds": 1, "generation_batch": 3})
    wait_for(bench, first.run_id, "completed")

    response = client.post(
        "/api/runs",
        json={"from_run": str(first.run_id), "harness": "demo", "config": FAST},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    clone = bench.adopt(UUID(body["run"]["id"]))
    assert body["run"]["question"] == GOAL
    original = bench.run(first.run_id)
    assert bench.run(clone)["base_prompt_hash"] == original["base_prompt_hash"], (
        "/compare tells an A/B of one prompt from two unrelated runs by this hash"
    )
    wait_for(bench, clone, "completed")

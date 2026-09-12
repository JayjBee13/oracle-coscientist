"""Full engine runs against a real database and a fake model.

These are the tests that decide whether the loop works: two rounds end to end, then each
way a run can be interrupted — paused, killed, stopped, or run out of money — with the
same two questions asked every time. Did the run end honestly (a report, a terminal
lifecycle, an event saying so)? And did it pay exactly once for each unit of work?
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from app.engine.models import ALLOWED_MODELS
from app.engine.orchestrator import Orchestrator, run_engine
from app.engine.runners import Failure, FakeRunner
from app.engine.store import RunStore

from .conftest import GOAL, HookedRunner, events_of, ledger_of, make_run


async def drive(store: RunStore, run_id: UUID, runner) -> str:
    return await run_engine(run_id, store, runner, rate_limit_cooldown=0)


def fake(run_id: UUID, **kwargs) -> FakeRunner:
    return FakeRunner(seed=str(run_id), **kwargs)


def types_of(store: RunStore, run_id: UUID) -> list[str]:
    return [event["type"] for event in events_of(store, run_id)]


# --- the happy path -----------------------------------------------------------------------


@pytest.fixture
async def completed_run(store):
    run_id = make_run(store)
    runner = fake(run_id)
    lifecycle = await drive(store, run_id, runner)
    return run_id, runner, lifecycle


async def test_a_two_round_run_completes_and_produces_science(store, completed_run):
    run_id, _, lifecycle = completed_run

    snapshot = store.snapshot(run_id)
    summary = snapshot["run"]

    assert lifecycle == "completed"
    assert summary["lifecycle"] == "completed"
    assert summary["round"] == 2
    assert len(snapshot["leaderboard"]) >= 8, "two rounds of a batch of 6 plus evolution"
    assert summary["counts"]["matches"] >= 1, "the tournament actually ran"
    assert summary["has_overview"], "every ending writes the report"
    assert len(snapshot["feedback_history"]) == 2, "one meta-review per round"
    assert snapshot["rounds"][0]["status"] == "completed"


async def test_hypotheses_carry_bodies_titles_and_lineage(store, completed_run):
    run_id, _, _ = completed_run

    rows = store.list_hypotheses(run_id)
    evolved = [row for row in rows if row["operator"]]

    assert all(row["title"] and row["body_md"].startswith("# ") for row in rows)
    assert all("**Claim:**" in row["body_md"] for row in rows)
    assert evolved, "evolution produced variants"
    assert all(row["parent_ids"] for row in evolved), "variants know their parents"
    assert all(
        parent in {other["hid"] for other in rows}
        for row in evolved
        for parent in row["parent_ids"]
    ), "lineage points at hypotheses that exist"


async def test_every_completed_match_records_elo_on_both_sides(store, completed_run):
    run_id, _, _ = completed_run

    completed = [m for m in store.list_matches(run_id) if m["status"] == "completed"]

    assert completed
    for match in completed:
        assert match["winner"] in (1, 2)
        assert match["debate_md"]
        assert match["judge_model"]
        assert match["k"] in (16, 32)
        for key in ("elo_a_before", "elo_a_after", "elo_b_before", "elo_b_after"):
            assert match[key] is not None, f"{key} missing — the Elo curve is unreconstructable"
        # Elo is moved between the two sides, never created.
        moved_a = match["elo_a_after"] - match["elo_a_before"]
        moved_b = match["elo_b_after"] - match["elo_b_before"]
        assert moved_a == pytest.approx(-moved_b)
        assert moved_a != 0


async def test_the_ledger_reconciles_exactly_with_the_units_of_work(store, completed_run):
    run_id, runner, _ = completed_run

    ledger = ledger_of(store, run_id)
    snapshot = store.snapshot(run_id)
    by_role: dict[str, int] = {}
    for row in ledger:
        by_role[row["role"]] = by_role.get(row["role"], 0) + 1

    reviews = store.list_reviews(run_id)
    settled = [m for m in store.list_matches(run_id) if m["status"] != "planned"]

    assert len(ledger) == len(runner.calls), "one ledger row per model call"
    assert len(ledger) == snapshot["run"]["calls_used"]
    assert by_role["reflection"] == len(reviews)
    assert by_role["ranking"] == len(settled)
    assert by_role["overview"] == 1
    assert by_role["meta_review"] == 2
    assert all(row["cost_usd"] > 0 and row["cache_read"] > 0 for row in ledger)
    assert snapshot["run"]["spend_usd"] > 0
    assert snapshot["run"]["calls_used"] <= snapshot["budget"]["budget_calls"]


async def test_no_hypothesis_is_reviewed_twice_and_no_match_is_judged_twice(store, completed_run):
    run_id, _, _ = completed_run

    reviewed = [review["hid"] for review in store.list_reviews(run_id)]
    matches = store.list_matches(run_id)

    assert len(reviewed) == len(set(reviewed))
    assert not [m for m in matches if m["status"] == "planned"], "the plan was fully executed"


async def test_the_event_stream_tells_the_story_in_order(store, completed_run):
    run_id, runner, _ = completed_run

    events = events_of(store, run_id)
    types = [event["type"] for event in events]
    seqs = [event["seq"] for event in events]

    assert seqs == sorted(seqs), "single-writer: seq order is commit order"
    assert types[0] == "lifecycle_changed"
    assert types[1] == "round_started"
    assert types[-1] == "run_finished"
    assert types.count("round_started") == 2
    assert types.count("round_completed") == 2
    assert types.count("call_started") == types.count("call_finished") == len(runner.calls)
    assert types.count("hypothesis_added") == len(store.list_hypotheses(run_id))
    assert "cluster_applied" in types
    assert "feedback_recorded" in types
    assert "contract_violation" not in types
    assert "graft_abstained" not in types, "the graft is off; silence is correct"


async def test_call_events_name_the_role_the_model_and_the_round(store, completed_run):
    run_id, _, _ = completed_run

    started = events_of(store, run_id, "call_started")
    finished = events_of(store, run_id, "call_finished")

    assert {event["payload"]["role"] for event in started} >= {
        "generation",
        "reflection",
        "proximity",
        "ranking",
        "evolution",
        "meta_review",
        "overview",
    }
    # On the allowlist, not merely "starts with claude-": a run's table may name either
    # provider's model per step, and the event has to name the one that actually ran.
    assert all(event["payload"]["model"] in ALLOWED_MODELS for event in started)
    assert all(event["payload"]["ok"] for event in finished)
    assert all(event["payload"]["duration_ms"] > 0 for event in finished)


async def test_call_finished_carries_what_the_call_reported_about_itself(store):
    """A successful call's denials and searches survive it, or nobody can ever check them.

    Both facts used to die with the transcript: a permission denial was only visible when it
    failed the call outright, and whether a grounded role searched at all was unknowable.
    The real smoke asserts on exactly these two, and so can anyone reading the Activity tab.
    """
    run_id = make_run(store, rounds=1, generation_batch=3, matches_per_round=1)
    inner = fake(run_id)

    class Telemetric:
        """A runner that reports about itself the way the CLI one does."""

        name = "telemetric"
        calls = inner.calls

        async def run_role(self, role: str, prompt: str, cfg) -> object:
            result = await inner.run_role(role, prompt, cfg)
            return replace(
                result,
                telemetry={
                    "permission_denials": [],
                    "web_searches": 3 if role == "generation" else 0,
                    "num_turns": 1,
                    "stderr": "noise the event has no business carrying",
                },
            )

        async def probe(self) -> dict:
            return await inner.probe()

        async def aclose(self) -> None:
            await inner.aclose()

    await drive(store, run_id, Telemetric())

    finished = events_of(store, run_id, "call_finished")
    telemetry = [event["payload"]["telemetry"] for event in finished]
    generation = [
        event["payload"]["telemetry"]
        for event in finished
        if event["payload"]["role"] == "generation"
    ]

    assert telemetry, "every call reported something"
    assert all(entry["permission_denials"] == [] for entry in telemetry)
    assert any(entry["web_searches"] > 0 for entry in generation)
    assert all("stderr" not in entry for entry in telemetry), "the payload is an allowlist"


async def test_round_one_generation_is_sharded_across_three_directives(store):
    run_id = make_run(store, generation_batch=9, rounds=1)
    runner = fake(run_id)

    await drive(store, run_id, runner)

    shards = [call for call in runner.calls if call["role"] == "generation"]
    directives = {
        line
        for call in shards
        for line in call["prompt"].splitlines()
        if line.startswith("DIRECTIVE:")
    }

    assert len(shards) == 3, "ceil(9 / 3) parallel calls"
    assert len(directives) == 3, "each shard explores a different angle"
    assert all(call["effort"] == "high" for call in shards), "round 1 generation is high effort"
    assert len([row for row in store.list_hypotheses(run_id) if row["created_round"] == 1]) >= 9


async def test_later_rounds_see_the_pool_and_the_guidance(store, completed_run):
    run_id, runner, _ = completed_run

    round_two = [c for c in runner.calls if c["role"] == "generation" and c["round"] == 2][0]

    assert "- " in round_two["prompt"].split("EXISTING")[1], "existing titles are listed"
    assert "RECURRING ISSUES:" in round_two["prompt"], "last round's guidance is injected"
    assert round_two["effort"] == "high", (
        "the round-1 escalation is a no-op against a table that already runs high — every "
        "round gets the effort round 1 used to be singled out for"
    )


async def test_the_round_one_escalation_still_bites_when_generation_is_overridden_down(store):
    """The escalation is a no-op at the default table, which is why it needs a test that
    it still exists: a scientist who drops generation to medium to save wall clock should
    still get the round every later round is seeded from at high effort."""
    run_id = make_run(
        store, rounds=2, model_overrides={"generation": {"effort": "medium"}}
    )
    runner = fake(run_id)

    await drive(store, run_id, runner)

    efforts = {
        call["round"]: call["effort"] for call in runner.calls if call["role"] == "generation"
    }
    assert efforts[1] == "high", "round 1 seeds everything after it"
    assert efforts[2] == "medium", "later rounds run at the effort that was asked for"


async def test_an_escalation_never_pulls_an_overridden_role_back_down(store):
    """`effort_at_least`, end to end. A role deliberately set above the escalation target
    keeps its level: escalating the most important call in the run must not weaken it."""
    run_id = make_run(store, rounds=1, model_overrides={"generation": {"effort": "max"}})
    runner = fake(run_id)

    await drive(store, run_id, runner)

    generation = [call for call in runner.calls if call["role"] == "generation"]
    assert generation and all(call["effort"] == "max" for call in generation)


async def test_the_meta_review_prompt_carries_every_review_of_the_round(store, completed_run):
    run_id, runner, _ = completed_run

    for number in (1, 2):
        prompt = [c for c in runner.calls if c["role"] == "meta_review" and c["round"] == number][
            0
        ]["prompt"]
        reviewed = [
            event["payload"]["hid"]
            for event in events_of(store, run_id, "review_recorded")
            if event["round"] == number
        ]

        assert reviewed
        for hid in reviewed:
            assert hid in prompt, f"review of {hid} never reached the meta-review"
        assert "REVIEWS FROM THIS ROUND (all of them):" in prompt
        assert "STANDINGS" in prompt


async def test_the_final_round_judges_at_high_effort(store, completed_run):
    run_id, runner, _ = completed_run

    rankings = [call for call in runner.calls if call["role"] == "ranking"]

    assert all(call["effort"] == "high" for call in rankings if call["round"] == 2)


async def test_the_run_prompt_and_goal_reach_every_role(store, completed_run):
    _, runner, _ = completed_run

    assert all(GOAL in call["prompt"] for call in runner.calls)
    assert all(call["prompt"].startswith(f"ROLE: {call['role']}.") for call in runner.calls)


async def test_context_documents_reach_the_generative_roles(store):
    run_id = make_run(store, rounds=1)
    store.add_context_docs(run_id, [{"name": "brief.md", "text": "The lake is dimictic."}])
    runner = fake(run_id)

    await drive(store, run_id, runner)

    for role in ("generation", "reflection", "evolution"):
        calls = [call for call in runner.calls if call["role"] == role]
        assert calls and all("The lake is dimictic." in call["prompt"] for call in calls)
    ranking = [call for call in runner.calls if call["role"] == "ranking"]
    assert all("The lake is dimictic." not in call["prompt"] for call in ranking)


# --- interruptions ---------------------------------------------------------------------------


async def test_pause_at_a_round_boundary_then_resume_finishes_the_run(store):
    run_id = make_run(store)

    def pause_after_the_first_meta_review(role, prompt, cfg):
        if role == "meta_review" and cfg.round == 1:
            store.set_control(run_id, "pause")
        return None

    paused_runner = HookedRunner(fake(run_id), before=pause_after_the_first_meta_review)
    assert await drive(store, run_id, paused_runner) == "paused"

    run = store.get_run(run_id)
    matches_when_paused = [m["id"] for m in store.list_matches(run_id) if m["status"] != "planned"]

    assert run["lifecycle"] == "paused"
    assert run["control_requested"] is None, "the request was consumed"
    assert run["engine_state"]["last_completed_round"] == 1
    assert not run["engine_state"].get("overview_md"), "a paused run is not over"

    store.set_control(run_id, "resume")
    resumed = HookedRunner(fake(run_id))
    assert await drive(store, run_id, resumed) == "completed"

    run = store.get_run(run_id)
    settled = [m["id"] for m in store.list_matches(run_id) if m["status"] != "planned"]

    assert run["engine_state"]["last_completed_round"] == 2
    assert set(matches_when_paused) <= set(settled)
    assert all(call["round"] in (2, None) for call in resumed.calls), "round 1 was not re-run"
    assert store.snapshot(run_id)["run"]["has_overview"]
    assert types_of(store, run_id).count("run_finished") == 1


async def test_a_kill_mid_reflection_resumes_without_paying_twice(store):
    """The supervisor dies with reflections in flight; a fresh one picks up the remainder."""
    run_id = make_run(store, rounds=1, generation_batch=6)
    reached = asyncio.Event()
    blocked = asyncio.Event()
    seen = 0

    async def block_the_third_reflection():
        await blocked.wait()

    def hook(role, prompt, cfg):
        nonlocal seen
        if role != "reflection":
            return None
        seen += 1
        if seen == 3:
            reached.set()
            return block_the_third_reflection()
        return None

    killed_runner = HookedRunner(fake(run_id), before=hook)
    task = asyncio.create_task(drive(store, run_id, killed_runner))
    await asyncio.wait_for(reached.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    partial_reviews = {review["hid"] for review in store.list_reviews(run_id)}
    ledger_before = ledger_of(store, run_id)
    assert partial_reviews, "the reflections that finished were recorded"
    assert len(partial_reviews) < len(store.list_hypotheses(run_id)), "and some were not"

    # A brand-new orchestrator on the same run, exactly as the supervisor would restart.
    resumed = HookedRunner(fake(run_id))
    assert await Orchestrator(store, resumed, run_id, rate_limit_cooldown=0).run() == "completed"

    reviews = store.list_reviews(run_id)
    hids = [review["hid"] for review in reviews]
    ledger = ledger_of(store, run_id)
    reflection_rows = [row for row in ledger if row["role"] == "reflection"]

    assert len(hids) == len(set(hids)), "no hypothesis was reviewed twice"
    assert partial_reviews <= set(hids)
    assert len(reflection_rows) == len(reviews), "ledger rows equal distinct units"
    assert len(ledger) > len(ledger_before)
    assert store.snapshot(run_id)["run"]["has_overview"]


async def test_stop_mid_round_still_writes_the_report(store):
    run_id = make_run(store)

    def stop_at_the_first_match(role, prompt, cfg):
        if role == "ranking":
            store.set_control(run_id, "stop")
        return None

    runner = HookedRunner(fake(run_id), before=stop_at_the_first_match)

    assert await drive(store, run_id, runner) == "stopped"

    snapshot = store.snapshot(run_id)
    types = types_of(store, run_id)
    finished = events_of(store, run_id, "run_finished")[0]

    assert snapshot["run"]["lifecycle"] == "stopped"
    assert snapshot["run"]["has_overview"], "stopping still writes a report from what exists"
    assert finished["payload"]["lifecycle"] == "stopped"
    assert types.count("round_completed") == 0, "the round it was stopped in did not complete"
    assert store.get_run(run_id)["engine_state"]["overview_md"].startswith("# Research Overview")


async def test_finish_completes_the_current_round_and_then_stops(store):
    run_id = make_run(store, rounds=5)

    def finish_during_the_first_round(role, prompt, cfg):
        if role == "proximity":
            store.set_control(run_id, "finish")
        return None

    runner = HookedRunner(fake(run_id), before=finish_during_the_first_round)

    assert await drive(store, run_id, runner) == "completed"

    run = store.get_run(run_id)
    assert run["engine_state"]["last_completed_round"] == 1, "the round it was in finished"
    assert types_of(store, run_id).count("round_started") == 1
    assert store.snapshot(run_id)["run"]["has_overview"]


async def test_budget_exhaustion_mid_round_ends_gracefully_with_a_report(store):
    run_id = make_run(store, rounds=3, budget_calls=12)
    runner = fake(run_id)

    assert await drive(store, run_id, runner) == "completed"

    snapshot = store.snapshot(run_id)
    types = types_of(store, run_id)
    warning = events_of(store, run_id, "budget_warning")

    assert warning and warning[0]["payload"]["reason"] == "calls"
    assert snapshot["run"]["calls_used"] <= 12, "the ceiling held"
    assert snapshot["run"]["has_overview"], "the two reserved calls bought the report"
    assert types[-1] == "run_finished"
    assert len(ledger_of(store, run_id)) == snapshot["run"]["calls_used"]


async def test_the_dollar_ceiling_stops_a_run_that_is_still_inside_its_call_budget(store):
    run_id = make_run(store, rounds=3, budget_calls=500, budget_usd=0.05)
    runner = fake(run_id)

    assert await drive(store, run_id, runner) == "completed"

    snapshot = store.snapshot(run_id)
    warning = events_of(store, run_id, "budget_warning")

    assert warning and warning[0]["payload"]["reason"] == "usd"
    assert snapshot["run"]["calls_used"] < 500
    assert types_of(store, run_id)[-1] == "run_finished"


async def test_a_deleted_run_halts_the_supervisor(store):
    run_id = make_run(store, rounds=1)

    def delete_the_run(role, prompt, cfg):
        if role == "reflection":
            store.soft_delete(run_id)
        return None

    runner = HookedRunner(fake(run_id), before=delete_the_run)

    from app.engine.orchestrator import RunHalted

    with pytest.raises(RunHalted):
        await drive(store, run_id, runner)


# --- failure handling -------------------------------------------------------------------------


async def test_a_contract_violation_is_repaired_once_then_the_unit_is_skipped(store):
    run_id = make_run(store, rounds=1, matches_per_round=2)
    runner = fake(run_id, failures={("ranking", 1): Failure(malformed=True)})

    assert await drive(store, run_id, runner) == "completed"

    violations = events_of(store, run_id, "contract_violation")
    attempts = [call for call in runner.calls if call["role"] == "ranking"]
    skipped = [m for m in store.list_matches(run_id) if m["status"] == "skipped"]
    ledger = [row for row in ledger_of(store, run_id) if row["role"] == "ranking"]

    assert violations, "the failure is visible in the Activity tab"
    assert "missing required field 'winner'" in violations[0]["payload"]["error"]
    assert violations[0]["payload"]["role"] == "ranking"
    assert len(attempts) == 2 * len(skipped) + len(
        [m for m in store.list_matches(run_id) if m["status"] == "completed"]
    ), "one repair re-ask per violated unit, then it is dropped"
    assert skipped, "an unjudgeable match is closed, not left planned forever"
    assert all(match["winner"] is None for match in skipped)
    assert len(ledger) == len(attempts), "the repair call is on the books too"
    assert store.snapshot(run_id)["run"]["has_overview"]


async def test_a_repairable_violation_succeeds_on_the_second_ask(store):
    run_id = make_run(store, rounds=1, matches_per_round=2)
    runner = fake(run_id, failures={("ranking", 1): Failure(malformed=True, times=1)})

    await drive(store, run_id, runner)

    assert not [m for m in store.list_matches(run_id) if m["status"] == "skipped"]
    assert events_of(store, run_id, "contract_violation") == []
    repaired = [
        call
        for call in runner.calls
        if call["role"] == "ranking" and "DID NOT MATCH THE REQUIRED SCHEMA" in call["prompt"]
    ]
    assert len(repaired) == 1


async def test_a_timing_out_role_is_retried_once_and_then_reported(store):
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("proximity", 1): "timeout after 180s"})

    assert await drive(store, run_id, runner) == "completed"

    attempts = [call for call in runner.calls if call["role"] == "proximity"]
    violations = events_of(store, run_id, "contract_violation")
    finished = [
        event
        for event in events_of(store, run_id, "call_finished")
        if event["payload"]["role"] == "proximity"
    ]

    assert len(attempts) == 2, "one retry, then give up"
    assert all(event["payload"]["ok"] is False for event in finished)
    assert violations[0]["payload"]["error"] == "timeout after 180s"
    assert all(row["cluster"] is None for row in store.list_hypotheses(run_id))
    assert store.snapshot(run_id)["run"]["has_overview"], "one dead step does not kill the run"


async def test_rate_limiting_is_surfaced_and_does_not_break_the_round(store):
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("reflection", 1): Failure(rate_limited=True, times=1)})

    assert await drive(store, run_id, runner) == "completed"

    limited = events_of(store, run_id, "rate_limited")
    assert limited
    # The Activity tab has to be able to say "paused for Ns" rather than showing a
    # rate-limit line followed by silence. Backpressure is the one state where the run is
    # deliberately idle, and it must not be indistinguishable from a hang.
    assert limited[0]["payload"]["cooldown_s"] == 0
    assert store.list_reviews(run_id)


async def test_backpressure_actually_stops_spawning_and_then_resumes(store):
    """The existing rate-limit test runs with a zero cooldown, so it proves the event is
    emitted but not that anything waits. This one gives the cooldown real duration and
    checks that no call is spawned inside the window and that the run finishes afterwards.
    """
    # A batch of 3 is exactly one generation shard, so precisely one call is in flight when
    # the limit lands. Calls already running are deliberately never cancelled — they are
    # paid for and may succeed — so a wider wave would leave it ambiguous whether a call
    # that overlapped the window was spawned during it or merely still finishing.
    run_id = make_run(store, rounds=1, generation_batch=3, matches_per_round=2)
    cooldown = 0.4
    started: list[tuple[str, float]] = []
    limited_at: list[float] = []
    runner = fake(run_id, failures={("generation", 1): Failure(rate_limited=True, times=1)})
    inner = runner.run_role

    async def timed(role: str, prompt: str, cfg):
        loop = asyncio.get_running_loop()
        started.append((role, loop.time()))
        result = await inner(role, prompt, cfg)
        if result.rate_limited:
            # The orchestrator learns of the limit when the call returns, so this is the
            # instant backpressure is engaged from.
            limited_at.append(loop.time())
        return result

    runner.run_role = timed  # type: ignore[method-assign]

    lifecycle = await run_engine(run_id, store, runner, rate_limit_cooldown=cooldown)

    assert lifecycle == "completed", "backpressure delays a run; it does not fail one"
    limited = events_of(store, run_id, "rate_limited")
    assert limited and limited[0]["payload"]["cooldown_s"] == cooldown

    engaged = limited_at[0]
    during = [role for role, when in started if engaged < when < engaged + cooldown * 0.8]
    assert not during, f"{during} were spawned while the limit was still biting"
    assert [role for role, when in started if when > engaged], "the run carried on once it cleared"
    assert store.snapshot(run_id)["run"]["has_overview"]


async def test_a_persistent_limit_reports_every_time_and_says_it_is_already_waiting(store):
    """A plan-window limit reports on every call in the wave. Each one is surfaced — an
    operator needs to see sustained pressure, not just its first symptom — but a report
    arriving inside an open cooldown says so, rather than reading as a fresh limit.

    That the window itself does not stack is pinned deterministically in
    `tests/unit/engine/test_orchestrator_governors.py`; asserting it from elapsed wall
    time here would be measuring the whole run to catch one sleep.
    """
    run_id = make_run(store, rounds=1, generation_batch=6)
    runner = fake(run_id, failures={("generation", 1): Failure(rate_limited=True)})

    assert await run_engine(run_id, store, runner, rate_limit_cooldown=0.05) == "completed"

    limited = events_of(store, run_id, "rate_limited")
    assert len(limited) > 1, "every limited call reports, so the operator sees the pressure"
    assert limited[0]["payload"]["already_waiting"] is False
    assert any(event["payload"]["already_waiting"] for event in limited)


# --- the wall-clock ceiling ---------------------------------------------------------------


async def test_a_run_that_passes_its_wall_clock_finishes_with_its_report(store):
    """The guard against a pathological loop, now that dollars are not one. It is not a
    kill: the run ends the way `finish` does, having written the overview it earned."""
    run_id = make_run(store, rounds=6, wall_clock_minutes=1e-9)
    runner = fake(run_id)

    assert await drive(store, run_id, runner) == "completed"

    snapshot = store.snapshot(store_run_id := run_id)["run"]
    assert snapshot["has_overview"], "a ceiling must never cost the run its report"
    assert "run_finished" in types_of(store, store_run_id)
    warning = events_of(store, run_id, "budget_warning")
    assert warning and warning[0]["payload"]["reason"] == "wall_clock"
    assert warning[0]["payload"]["wall_clock_minutes"] == 1e-9
    assert len(warning) == 1, "announced once, not once per boundary"


async def test_the_wall_clock_stops_the_run_early_rather_than_running_every_round(store):
    run_id = make_run(store, rounds=6, wall_clock_minutes=1e-9)

    await drive(store, run_id, fake(run_id))

    completed = events_of(store, run_id, "round_completed")
    assert len(completed) < 6, "a ceiling that lets every round run is not a ceiling"


async def test_a_run_without_a_wall_clock_is_never_stopped_by_one(store):
    run_id = make_run(store, rounds=1, wall_clock_minutes=None)

    assert await drive(store, run_id, fake(run_id)) == "completed"

    reasons = [
        event["payload"]["reason"] for event in events_of(store, run_id, "budget_warning")
    ]
    assert "wall_clock" not in reasons


async def test_a_run_without_a_dollar_ceiling_is_never_stopped_by_one(store):
    """The default now. A run that reports a fortune in API-equivalent telemetry still
    finishes, because none of it was billed."""
    run_id = make_run(store, rounds=1, budget_usd=None)
    runner = fake(run_id, cost_per_1k_tokens=1000.0)

    assert await drive(store, run_id, runner) == "completed"

    assert store.snapshot(run_id)["run"]["spend_usd"] > 100, "the telemetry is still recorded"
    reasons = [
        event["payload"]["reason"] for event in events_of(store, run_id, "budget_warning")
    ]
    assert "usd" not in reasons


async def test_a_degraded_role_is_recorded_for_the_activity_tab(store):
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(degraded=True, times=1)})

    await drive(store, run_id, runner)

    degraded = events_of(store, run_id, "role_degraded")
    assert degraded and degraded[0]["payload"]["role"] == "evolution"
    assert store.snapshot(run_id)["degraded_count"] == 1


# --- interventions and the graft ----------------------------------------------------------------


async def test_a_scientist_note_reaches_the_next_round_and_the_event_stream(store):
    run_id = make_run(store)
    note = "Ignore phosphorus entirely; look at light attenuation."

    def add_the_note_during_round_one(role, prompt, cfg):
        if role == "proximity" and cfg.round == 1:
            store.add_note(run_id, note)
        return None

    runner = HookedRunner(fake(run_id), before=add_the_note_during_round_one)
    await drive(store, run_id, runner)

    added = events_of(store, run_id, "note_added")
    round_two = [c for c in runner.calls if c["round"] == 2]

    assert added and added[0]["payload"]["text"] == note
    assert added[0]["round"] == 2, "notes are drained at the next round boundary"
    for role in ("generation", "evolution", "meta_review"):
        calls = [call for call in round_two if call["role"] == role]
        assert calls and all(note in call["prompt"] for call in calls)
        assert all("SCIENTIST GUIDANCE (highest priority" in call["prompt"] for call in calls)
    assert store.get_run(run_id)["pending_interventions"] == []


async def test_an_archived_hypothesis_leaves_the_tournament_and_the_stream_says_so(store):
    run_id = make_run(store)
    archived: dict[str, str] = {}

    def archive_a_leader(role, prompt, cfg):
        if role == "evolution" and cfg.round == 1 and not archived:
            leader = store.list_hypotheses(run_id, statuses=["active"])[0]
            archived["hid"] = leader["hid"]
            store.archive_hypothesis(run_id, leader["hid"])
        return None

    runner = HookedRunner(fake(run_id), before=archive_a_leader)
    await drive(store, run_id, runner)

    events = events_of(store, run_id, "hypothesis_archived")
    row = [h for h in store.list_hypotheses(run_id) if h["hid"] == archived["hid"]][0]
    round_two_matches = [m for m in store.list_matches(run_id) if m["round"] == 2]

    assert events and events[0]["payload"]["hid"] == archived["hid"]
    assert row["status"] == "archived"
    assert all(archived["hid"] not in (m["hid_a"], m["hid_b"]) for m in round_two_matches)


async def test_a_collapsed_pool_fires_the_graft_and_seeds_the_next_generation(store):
    """One cluster for everything is a collapse; the Cartographer's seed must reach round 2."""
    run_id = make_run(
        store,
        rounds=2,
        graft={"enabled": True, "quorum_k": 1, "window": 2, "cooldown": 1},
    )

    def one_cluster(prompt: str) -> dict:
        import re

        return {"clusters": {hid: "the-rut" for hid in set(re.findall(r"\bh\d{3}\b", prompt))}}

    runner = HookedRunner(fake(run_id), override={"proximity": one_cluster})
    await drive(store, run_id, runner)

    fired = events_of(store, run_id, "graft_fired")
    generation_two = [c for c in runner.calls if c["role"] == "generation" and c["round"] == 2]
    graft_events = store.snapshot(run_id)["graft_events"]
    run = store.get_run(run_id)

    assert fired, "hhi of 1.0 is a collapse"
    assert fired[0]["payload"]["n_clusters"] == 1
    assert fired[0]["payload"]["hhi"] == pytest.approx(1.0)
    assert [call for call in runner.calls if call["role"] == "cartographer"]
    assert generation_two and all("DIVERGENCE SEED" in call["prompt"] for call in generation_two)
    assert all(
        "glacial hydrology" in call["prompt"] or "SOURCE DOMAIN:" in call["prompt"]
        for call in generation_two
    )
    assert run["graft_state"]["pending_seed"] is None, "the seed is consumed once"
    assert run["graft_state"]["fired_count"] >= 1
    assert graft_events[0]["source_domain"]
    seeded = [row for row in store.list_hypotheses(run_id) if row["seed_id"]]
    assert seeded, "the hypotheses the seed produced are traceable to it"


async def test_the_graft_abstains_loudly_only_when_a_scientist_would_care(store):
    run_id = make_run(store, rounds=1, graft={"enabled": True, "quorum_k": 3, "window": 2})
    runner = fake(run_id)

    await drive(store, run_id, runner)

    abstentions = events_of(store, run_id, "graft_abstained")
    recorded = store.snapshot(run_id)["graft_events"]

    assert recorded and recorded[0]["fired"] is False
    assert recorded[0]["abstained_reason"] == "quorum_not_met"
    assert abstentions == [], "a healthy no-op is not news"


def graft_rows(store: RunStore, run_id: UUID) -> list[dict]:
    """The graft_events rows including `signals`, which the API summary leaves out."""
    from sqlalchemy import text

    factory = store._session_factory  # noqa: SLF001 - the DTO drops the column under test
    with factory() as session:
        rows = session.execute(
            text(
                "select round, fired, abstained_reason, n_clusters, hhi, signals "
                "from graft_events where run_id = :run_id order by round, id"
            ),
            {"run_id": str(run_id)},
        ).mappings()
        return [dict(row) for row in rows]


async def test_a_disabled_graft_still_measures_the_pool_every_round(store, completed_run):
    """Task 4's defect: `if not graft.enabled` was the first line of the collapse check, so
    a disabled organ meant no measurement at all — no `graft_events` row and no
    `collapse_history` entry for any run the app has ever done. The organ is still off; the
    instrument is not."""
    run_id, runner, _ = completed_run
    rows = graft_rows(store, run_id)
    run = store.get_run(run_id)

    assert [row["round"] for row in rows] == [1, 2], "one measurement per round"
    assert all(row["fired"] is False for row in rows)
    assert all(row["abstained_reason"] == "graft_disabled" for row in rows)
    assert all(row["n_clusters"] and row["n_clusters"] > 0 for row in rows), "clusters counted"
    assert all(row["hhi"] is not None for row in rows)
    assert not [call for call in runner.calls if call["role"] == "cartographer"], (
        "measuring is free; only the Cartographer call is gated on `enabled`"
    )
    history = run["engine_state"]["collapse_history"]
    assert [entry["round"] for entry in history] == [1, 2]
    assert "graft_abstained" not in types_of(store, run_id), "a disabled organ is not news"


async def test_the_family_reading_is_recorded_for_the_ideas_each_round_created(
    store, completed_run
):
    """Task 6, measurement only: top-family share over the round's *new* ideas — the flow,
    not the stock — recorded on the graft event and wired to nothing."""
    run_id, _, _ = completed_run
    rows = graft_rows(store, run_id)
    everything = store.list_hypotheses(run_id)
    batch = 6  # `make_run`'s generation_batch

    assert len(everything) > 2 * batch, "there are evolved variants for the flow to exclude"

    for row in rows:
        flow = row["signals"]["family_flow"]
        born = [
            item
            for item in everything
            # No `operator` means generation produced it. The round's evolved variants
            # share its `created_round` but are born two steps after the check reads it.
            if item["created_round"] == row["round"] and not item["operator"]
        ]
        labelled = [item for item in born if item["status"] != "rejected"]
        # The check sits between proximity and the tournament, so the flow is that round's
        # generated batch — not the cumulative pool, and not the variants born two steps
        # later. Cumulative is the reading that failed: family HHI over the whole pool
        # peaks at 0.137 on the one run with documented collapse.
        assert flow["n_total"] == batch == len(born), "the round's new ideas, not the pool"
        # Proximity only ever sees the active pool, so an idea reflection rejected is
        # counted and never labelled. The gap between the two numbers is the whole reason
        # both are recorded: a share is only as trustworthy as its denominator.
        assert 0 < flow["n_labelled"] == len(labelled) <= flow["n_total"]
        assert 0.0 < flow["share"] <= 1.0
        assert flow["label"] in flow["counts"]
        assert sum(flow["counts"].values()) == flow["n_labelled"]
        assert len(flow["counts"]) <= 8, "families are coarse buckets, not mechanisms"

    assert all(row["fired"] is False for row in rows), "the signal fires nothing yet"


async def test_a_proximity_agent_that_omits_families_costs_the_run_nothing(store):
    """The family label is telemetry on trial, so it is not `required` in the schema: a
    model that never emits one must not cost a repair call, let alone the round's
    clustering."""
    run_id = make_run(store, rounds=1)

    def no_families(prompt: str) -> dict:
        import re

        return {"clusters": {hid: f"c-{hid}" for hid in set(re.findall(r"\bh\d{3}\b", prompt))}}

    runner = HookedRunner(fake(run_id), override={"proximity": no_families})
    lifecycle = await drive(store, run_id, runner)
    rows = graft_rows(store, run_id)

    assert lifecycle == "completed"
    assert not [event for event in events_of(store, run_id, "contract_violation")]
    assert rows and rows[0]["signals"]["family_flow"]["n_labelled"] == 0
    assert rows[0]["signals"]["family_flow"]["label"] is None
    assert rows[0]["n_clusters"] > 0, "clustering is unaffected"


async def test_a_pool_that_narrowed_tells_evolution_to_diverge(store):
    """Task 5's second half, and the reason it cannot read the surviving pool: proximity
    archives the non-champions of a shared label, so the survivors carry one member per
    cluster whatever the run is doing. Concentration is read over everything the run
    produced, which is where the narrowing actually shows."""
    run_id = make_run(store, rounds=1, evolve_top_k=3)

    def one_cluster(prompt: str) -> dict:
        import re

        hids = sorted(set(re.findall(r"\bh\d{3}\b", prompt)))
        return {
            "clusters": dict.fromkeys(hids, "the-rut"),
            "families": dict.fromkeys(hids, "b2b tooling"),
        }

    runner = HookedRunner(fake(run_id), override={"proximity": one_cluster})
    await drive(store, run_id, runner)

    produced = store.list_hypotheses(run_id)
    evolution = [call for call in runner.calls if call["role"] == "evolution"]
    active = [row for row in produced if row["status"] == "active"]

    assert evolution, "the round evolved"
    assert "DIVERGENCE REQUIREMENT" in evolution[0]["prompt"]
    assert len({row["cluster"] for row in active if row["cluster"]}) == 1, (
        "the surviving pool is one cluster wide and looks maximally diverse to Herfindahl"
    )
    rows = graft_rows(store, run_id)
    assert rows[0]["signals"]["family_flow"]["share"] == pytest.approx(1.0), (
        "and the family reading of the round's new ideas says so plainly"
    )


async def test_a_wide_pool_is_not_told_to_diverge(store, completed_run):
    """The counterweight: the instruction must be a response to a measurement, not boilerplate
    every run carries."""
    _, runner, _ = completed_run
    evolution = [call for call in runner.calls if call["role"] == "evolution"]

    assert evolution
    assert all("DIVERGENCE REQUIREMENT" not in call["prompt"] for call in evolution)


# --- idempotence -------------------------------------------------------------------------------


async def test_re_launching_a_finished_run_changes_nothing(store, completed_run):
    run_id, _, _ = completed_run
    before = store.snapshot(run_id)

    second = fake(run_id)
    assert await drive(store, run_id, second) == "completed"

    assert second.calls == []
    assert store.snapshot(run_id)["run"]["calls_used"] == before["run"]["calls_used"]
    assert len(events_of(store, run_id)) == len(events_of(store, run_id))


async def test_a_run_with_no_model_table_is_refused_before_it_spends(store):
    run_id = store.create_run(
        question=GOAL,
        prompt=GOAL,
        config={"rounds": 1, "budget_calls": 20, "budget_usd": 5.0},
        harness="demo",
    )
    runner = fake(run_id)

    with pytest.raises(ValueError, match="model_table"):
        await drive(store, run_id, runner)

    assert runner.calls == []
    assert store.get_run(run_id)["lifecycle"] == "queued"

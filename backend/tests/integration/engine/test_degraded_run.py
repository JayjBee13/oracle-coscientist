"""A run that loses work has to say so, and must not spend the loss twice.

Every test here is anchored to run c4566ed2 — the owner's real run, which finished as an
unqualified `Completed` after three whole steps had died, produced no report, and had
`degraded_count: 0` on its detail endpoint. The two questions are the two the owner asked
for: can a grounded role finish, and does a degraded run admit it.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.engine.orchestrator import Orchestrator, run_engine
from app.engine.runners import (
    TIMEOUT_BATCH_GROUNDED,
    TIMEOUT_TOOLLESS,
    Failure,
    FakeRunner,
)
from app.engine.store import BudgetExhausted, RunStore

from .conftest import events_of, make_run

TIMED_OUT = "timeout after 431s (limit 420s, killed)"


async def drive(store: RunStore, run_id: UUID, runner) -> str:
    return await run_engine(run_id, store, runner, rate_limit_cooldown=0)


def fake(run_id: UUID, **kwargs) -> FakeRunner:
    return FakeRunner(seed=str(run_id), **kwargs)


def calls_of(runner: FakeRunner, role: str) -> list[dict]:
    return [call for call in runner.calls if call["role"] == role]


def payloads(store: RunStore, run_id: UUID, type: str) -> list[dict]:
    return [event["payload"] for event in events_of(store, run_id, type)]


# --- the retry policy ----------------------------------------------------------------------


async def test_a_timed_out_call_is_retried_on_a_longer_deadline(store):
    """Retrying a deadline breach against the same deadline cannot change P(success). In
    c4566ed2 that arithmetic cost 6 dead attempts x 420s — 42 minutes of a 52-minute run —
    and the budget calls that would have paid for the report."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(error=TIMED_OUT, times=1)})

    await drive(store, run_id, runner)

    attempts = calls_of(runner, "evolution")
    assert len(attempts) == 2, "the timeout still earns one retry"
    assert attempts[1]["timeout_s"] == pytest.approx(attempts[0]["timeout_s"] * 2)


async def test_a_transport_failure_is_retried_on_the_same_deadline(store):
    """The deadline was not what failed, so lengthening it would be cargo cult."""
    run_id = make_run(store, rounds=1)
    runner = fake(
        run_id,
        failures={("evolution", 1): Failure(error="connection reset by peer", times=1)},
    )

    await drive(store, run_id, runner)

    attempts = calls_of(runner, "evolution")
    assert len(attempts) == 2
    assert attempts[1]["timeout_s"] == attempts[0]["timeout_s"]


async def test_a_schema_violation_is_re_asked_rather_than_given_more_time(store):
    """The one failure a re-ask on identical terms genuinely fixes."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(malformed=True, times=1)})

    await drive(store, run_id, runner)

    attempts = calls_of(runner, "evolution")
    assert len(attempts) == 2
    assert attempts[1]["timeout_s"] == attempts[0]["timeout_s"]
    assert "DID NOT MATCH THE REQUIRED SCHEMA" in attempts[1]["prompt"]


async def test_a_batch_grounded_role_is_given_the_ceiling_its_calls_actually_need(store):
    """420s sat below the median of the configuration it was bounding: 8 of 10 opus-5/high
    generation and evolution calls in c4566ed2 were killed at it, and the two survivors
    finished at 92% and 97% of it."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id)

    await drive(store, run_id, runner)

    generation = calls_of(runner, "generation")[0]
    ranking = calls_of(runner, "ranking")
    assert generation["timeout_s"] == TIMEOUT_BATCH_GROUNDED
    assert all(call["timeout_s"] == TIMEOUT_TOOLLESS for call in ranking), (
        "the tool-less ceiling was never the problem and is left alone"
    )


# --- a lost step is a fact about the round -------------------------------------------------


async def test_a_generation_wave_that_produced_nothing_says_so(store):
    """`added` was read in exactly one place — a number in the round_completed payload — and
    never compared with what was asked for. In c4566ed2's round 2 both shards failed both
    attempts and the engine went straight on to spend a proximity call and four ranking
    calls on a pool that had not changed."""
    run_id = make_run(store, rounds=1, generation_batch=6)
    runner = fake(run_id, failures={("generation", 1): Failure(error=TIMED_OUT)})

    await drive(store, run_id, runner)

    shortfalls = [row for row in payloads(store, run_id, "step_failed")
                  if row["role"] == "generation"]
    assert shortfalls, "a wave in which every shard died looked like a wave never scheduled"
    assert shortfalls[0]["requested"] == 6
    assert shortfalls[0]["produced"] == 0
    assert shortfalls[0]["units"]


async def test_a_partial_generation_wave_reports_the_shortfall_too(store):
    run_id = make_run(store, rounds=1, generation_batch=6)
    runner = fake(run_id, failures={("generation", 1): Failure(error=TIMED_OUT, times=2)})

    await drive(store, run_id, runner)

    shortfalls = [row for row in payloads(store, run_id, "step_failed")
                  if row["role"] == "generation"]
    assert shortfalls, "one dead shard out of two is still a short batch"
    assert 0 < shortfalls[0]["produced"] < shortfalls[0]["requested"]


async def test_a_failed_evolution_step_is_left_for_a_resume_rather_than_written_off(store):
    """Generation only records a shard that succeeded; evolution used to mark the step done
    on failure, so one timeout removed evolution from that round permanently. c4566ed2's
    engine_state says `{"evolution": true}` for a round whose evolution call timed out
    twice — which is why the run has zero descent edges despite evolve_top_k = 3."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(error=TIMED_OUT, times=2)})

    await drive(store, run_id, runner)

    steps = store.get_run(run_id)["engine_state"]["steps"]["1"]
    assert steps.get("evolution") is not True, "a failed step was recorded as done"
    assert steps["evolution_attempts"] == 1
    assert any(row["role"] == "evolution" for row in payloads(store, run_id, "step_failed"))


async def test_a_step_that_keeps_failing_is_eventually_written_off(store):
    """Leaving a failed step unmarked must not let a deterministic failure loop forever."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", None): Failure(error=TIMED_OUT)})

    await drive(store, run_id, runner)
    # Relaunch the same run from the same round, as a resume would.
    store.set_lifecycle(run_id, "queued")
    store.set_engine_state(run_id, {"last_completed_round": 0})
    retry = fake(run_id, failures={("evolution", None): Failure(error=TIMED_OUT)})
    await drive(store, run_id, retry)

    steps = store.get_run(run_id)["engine_state"]["steps"]["1"]
    assert steps["evolution_attempts"] == 2
    assert steps["evolution"] is True


# --- the run says what it lost -------------------------------------------------------------


async def test_run_finished_records_why_the_run_ended_and_what_it_lost(store):
    """Budget exhaustion, an expired wall clock, a finish request and an honest completion
    all wrote byte-identical payloads, so the run's own permanent record could not tell a
    success from an abort."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(error=TIMED_OUT)})

    await drive(store, run_id, runner)

    finished = payloads(store, run_id, "run_finished")[0]
    assert finished["lifecycle"] == "completed"
    assert finished["reason"] == "rounds_done"
    assert finished["lost_steps"] >= 1


async def test_the_losses_reach_the_run_summary_the_header_and_the_list_are_built_from(store):
    """`degraded_count` counts model substitutions and has never fired in 29 app runs. A run
    that lost three steps rendered identically to one that did everything asked."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(error=TIMED_OUT)})

    await drive(store, run_id, runner)

    snapshot = store.snapshot(run_id)
    assert snapshot["degraded_count"] == 0
    assert snapshot["run"]["lost_steps"] >= 1
    assert snapshot["run"]["failed_calls"] >= 2, "both attempts are counted"
    assert snapshot["run"]["ended_reason"] == "rounds_done"
    assert snapshot["problems"], "and the forensics are reachable without the event window"


async def test_the_report_is_told_what_the_run_lost(store):
    """The overview agent's whole input was goal, mode, a summary line built purely from
    successes, standings, top hypotheses and the guidance trajectory. A run whose generation
    died twice was narrated as an unqualified result because no field could express it."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(error=TIMED_OUT)})

    await drive(store, run_id, runner)

    overview = [call for call in runner.calls if call["role"] == "overview"][0]
    assert "RUN HEALTH" in overview["prompt"]
    assert "evolution" in overview["prompt"].split("RUN HEALTH")[1].split("STANDINGS")[0]


async def test_the_meta_review_is_told_when_its_round_lost_a_step(store):
    """The only component that can steer the next round away from a failure mode could not
    see one."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(error=TIMED_OUT)})

    await drive(store, run_id, runner)

    meta = [call for call in runner.calls if call["role"] == "meta_review"][0]
    assert "THIS ROUND'S HEALTH" in meta["prompt"]


async def test_a_run_that_cannot_afford_its_report_says_why_rather_than_leaving_a_blank(store):
    """Two of 29 app runs ended `completed` with no deliverable at all, and the Report tab
    said "No report was written" with no reason and no next step."""
    run_id = make_run(store, rounds=1, budget_usd=0.0001)
    runner = fake(run_id)

    lifecycle = await drive(store, run_id, runner)

    run = store.get_run(run_id)
    assert lifecycle == "completed"
    assert not run["engine_state"].get("overview_md")
    assert run["engine_state"]["overview_skipped_reason"] == "budget_usd"
    skipped = payloads(store, run_id, "report_skipped")
    assert skipped and "ceiling" in skipped[0]["detail"]
    assert store.snapshot(run_id)["run"]["overview_skipped_reason"] == "budget_usd"


async def test_a_budget_refusal_from_the_spawn_gate_stops_the_run_rather_than_blaming_a_role(
    store,
):
    """`spawn_role_process` re-checks the ceiling and raises. That used to be caught by the
    worker's blanket handler, turned into a generic role error, and *retried* — a retry
    guaranteed to raise again — while `_flag_budget` was never reached, so the run's own
    budget flag stayed false and the failure was reported as a contract violation."""
    run_id = make_run(store, rounds=1)
    inner = fake(run_id)

    class RefusingRunner:
        name = "refusing"

        def __init__(self) -> None:
            self.attempts: list[tuple[str, str | None]] = []

        async def run_role(self, role, prompt, cfg):
            self.attempts.append((role, cfg.unit))
            raise BudgetExhausted(
                run_id, "calls", calls_used=9, budget_calls=9, spend_usd=0.0, budget_usd=0.0
            )

        async def probe(self):
            return await inner.probe()

        async def aclose(self):
            return None

    runner = RefusingRunner()
    lifecycle = await drive(store, run_id, runner)

    assert lifecycle == "completed"
    assert len(runner.attempts) == len(set(runner.attempts)), (
        f"a budget refusal was retried, and the retry can only raise again: {runner.attempts}"
    )
    assert payloads(store, run_id, "budget_warning"), "the run must know it was the budget"
    assert store.snapshot(run_id)["run"]["ended_reason"] == "budget_calls"


# --- what the report is allowed to call a result -------------------------------------------


async def test_never_ranked_variants_are_not_presented_as_top_results(store):
    """Every run ends with `evolve_top_k` hypotheses created after the final tournament:
    never reviewed, never matched, parked at the default 1200. 23 runs in the corpus have at
    least one occupying a top-five slot in their report, and five have three."""
    run_id = make_run(store, rounds=2)
    runner = fake(run_id)

    await drive(store, run_id, runner)

    overview = [call for call in runner.calls if call["role"] == "overview"][0]["prompt"]
    assert "UNVERIFIED NEW VARIANTS" in overview
    top_block = overview.split("TOP HYPOTHESES")[1].split("UNVERIFIED NEW VARIANTS")[0]
    unplayed = [
        row["title"]
        for row in store.list_hypotheses(run_id, statuses=["active"])
        if row["matches"] == 0
    ]
    assert unplayed, "the fixture must actually contain some — otherwise this proves nothing"
    for title in unplayed:
        assert title not in top_block, f"{title!r} never played a match and was ranked"


async def test_clustering_keeps_the_member_the_tournament_has_actually_judged(store):
    """`_cluster` runs before `_tournament`, so Elo is not a measurement when the champion is
    picked. A newcomer entering at 1200 outranked a veteran that had lost a judged match at
    1184 — run 526ee9aa archived the tested idea and kept the one later rated worse."""
    known = {
        "h001": {"hid": "h001", "elo": 1184.0, "matches": 1},
        "h015": {"hid": "h015", "elo": 1200.0, "matches": 0},
    }

    duplicates = Orchestrator._duplicates({"h001": "a", "h015": "a"}, known)

    assert duplicates == {"h015": "h001"}, "the judged member must survive"


def test_an_unjudged_cluster_still_falls_back_to_the_highest_rating_then_the_hid():
    known = {
        "h003": {"hid": "h003", "elo": 1200.0, "matches": 0},
        "h004": {"hid": "h004", "elo": 1200.0, "matches": 0},
    }

    assert Orchestrator._duplicates({"h003": "a", "h004": "a"}, known) == {"h004": "h003"}

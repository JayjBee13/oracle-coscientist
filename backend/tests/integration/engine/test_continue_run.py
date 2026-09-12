"""Continuing a finished run: more rounds on the same run, keeping everything it learned.

A scientist runs Quick, likes what comes back, and wants to go deeper. The thing that must
survive that is not the question — cloning already copies the question — it is the *science*:
the idea pool with its Elo and match history, the clusters, and the meta-review guidance
that was steering the next round. That guidance loop is the whole reason this engine
compounds across rounds, so every test here asks the same question in a different way: is
round N+1 after a continue indistinguishable from round N+1 of a run that never stopped?

The other half is the exception being narrow. `config` is immutable after launch because a
run that changed models under itself would have half its ratings from a different judge.
Continuing bends that rule for exactly two keys, and the tests below pin the other keys down
rather than trusting a docstring.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest

from app.engine.core import INITIAL_ELO, OVERVIEW_RESERVED_CALLS
from app.engine.runners import FakeRunner
from app.engine.store import LifecycleConflict, RunStore
from app.services.runs.controls import CONTINUE_LIFECYCLES

from .conftest import HookedRunner, events_of, make_run

# One round of real work, small enough to run a dozen of these in a session: a batch of
# three, a two-match tournament so ratings actually move, and one evolved variant.
QUICK: dict[str, Any] = {
    "rounds": 1,
    "generation_batch": 3,
    "matches_per_round": 2,
    "evolve_top_k": 1,
    "budget_calls": 200,
}


async def drive(store: RunStore, run_id: UUID, runner) -> str:
    from app.engine.orchestrator import run_engine

    return await run_engine(run_id, store, runner, rate_limit_cooldown=0)


def fake(run_id: UUID, **kwargs) -> FakeRunner:
    return FakeRunner(seed=str(run_id), **kwargs)


HID = re.compile(r"h\d{3}")


def _passes(_prompt: str) -> dict[str, Any]:
    """A reflection that never rejects."""
    return {
        "verdict": "pass",
        "novelty": {"level": "moderate", "note": "A framing the standard account misses."},
        "correctness": "No fundamental flaw found.",
        "testability": "The proposed test discriminates against the obvious alternative.",
        "key_risk": "The effect may be an artefact of the measurement.",
        "note": "Worth ranking.",
    }


def _singletons(prompt: str) -> dict[str, Any]:
    """A proximity call that calls nothing a duplicate of anything."""
    return {"clusters": {hid: f"cluster-{hid}" for hid in sorted(set(HID.findall(prompt)))}}


def candid(run_id: UUID) -> HookedRunner:
    """The fake with its two random thinning steps pinned open.

    The scripted model rejects roughly one hypothesis in seven and clusters the rest on a
    digest of the prompt, so the size and shape of the pool differ from run to run — and the
    seed is the run's own uuid, which is fresh every time. That is exactly right for the
    tests that ask whether the loop survives a thin pool, and exactly wrong for one asking
    who was paired with whom.
    """
    return HookedRunner(fake(run_id), override={"reflection": _passes, "proximity": _singletons})


def extend(store: RunStore, run_id: UUID, **kwargs) -> dict[str, Any]:
    """The store mutation the `continue` control applies, without spawning a process."""
    return store.extend_run(
        run_id, expect_lifecycles=tuple(sorted(CONTINUE_LIFECYCLES)), **kwargs
    )


def untouchable(config: Mapping[str, Any]) -> dict[str, Any]:
    """Everything in a config that continuing must not be able to reach.

    Three keys are excluded and no more: the round target and the two ceilings. Everything
    else — the resolved model table above all — has to come back byte-identical.
    """
    return {
        key: value
        for key, value in config.items()
        if key not in ("rounds", "budget_calls", "budget_usd")
    }


def ratings(store: RunStore, run_id: UUID) -> dict[str, dict[str, Any]]:
    return {
        row["hid"]: {
            "elo": row["elo"],
            "matches": row["matches"],
            "wins": row["wins"],
            "cluster": row["cluster"],
            "status": row["status"],
            "created_round": row["created_round"],
        }
        for row in store.list_hypotheses(run_id)
    }


@pytest.fixture
async def finished_run(store):
    """A completed one-round run — the state a scientist is looking at when they continue."""
    run_id = make_run(store, **QUICK)
    runner = fake(run_id)
    lifecycle = await drive(store, run_id, runner)
    assert lifecycle == "completed"
    return run_id, runner


# --- what is preserved --------------------------------------------------------------------


async def test_the_extension_itself_moves_nothing_but_the_target(store, finished_run):
    """Before a single new call: the pool, the ratings and the history are untouched."""
    run_id, _ = finished_run
    before = store.get_run(run_id)
    pool = ratings(store, run_id)

    extend(store, run_id, rounds_target=3)

    after = store.get_run(run_id)
    assert ratings(store, run_id) == pool, "the standings were not disturbed"
    assert after["feedback_history"] == before["feedback_history"]
    assert after["graft_state"] == before["graft_state"]
    assert after["rounds_target"] == 3
    assert after["lifecycle"] == "queued", "it left the terminal state to be driven again"
    assert after["engine_state"]["last_completed_round"] == 1, "round 1 is still done"


async def test_the_mutation_cannot_reach_the_models_the_run_started_with(store, finished_run):
    """The reason `config` is immutable is that a run must not change judges mid-tournament.

    Continuing is the one exception to that rule, so what it may touch is worth pinning:
    two keys, and every other key byte-identical — the resolved model table above all.
    """
    run_id, _ = finished_run
    before = dict(store.get_run(run_id)["config"])

    extend(store, run_id, rounds_target=4, budget_calls=500)

    after = dict(store.get_run(run_id)["config"])

    assert after["rounds"] == 4
    assert after["budget_calls"] == 500
    assert untouchable(after) == untouchable(before)
    assert after["model_table"] == before["model_table"]
    assert store.get_run(run_id)["budget_calls"] == 500, "the enforced ceiling moved too"


async def test_an_extension_that_names_no_budget_leaves_the_ceiling_alone(store, finished_run):
    run_id, _ = finished_run
    before = store.get_run(run_id)

    extend(store, run_id, rounds_target=2)

    after = store.get_run(run_id)
    assert after["budget_calls"] == before["budget_calls"]
    assert after["config"]["budget_calls"] == before["config"]["budget_calls"]


async def test_a_budget_may_be_raised_but_never_lowered(store, finished_run):
    """Lowering it below what is already spent would end the continued run on its first
    check, having spent a process launch to change nothing."""
    run_id, _ = finished_run

    with pytest.raises(ValueError, match="raised, never lowered"):
        extend(store, run_id, rounds_target=2, budget_calls=1)


async def test_a_cost_ceiling_may_be_raised(store, finished_run):
    """The same shape as raising the call budget, and it moves both places it is written:
    the config the next supervisor reads, and the column `check_budget` enforces."""
    run_id, _ = finished_run
    assert store.get_run(run_id)["budget_usd"] == 100.0

    extend(store, run_id, rounds_target=2, budget_usd=250.0)

    after = store.get_run(run_id)
    assert after["budget_usd"] == 250.0
    assert after["config"]["budget_usd"] == 250.0


async def test_a_cost_ceiling_may_be_removed_entirely(store, finished_run):
    """Removing it is not lowering it to zero, and the difference is the whole feature.

    A dollar ceiling gates on `total_cost_usd`, which for calls made through the Claude CLI
    on a subscription is API-equivalent telemetry rather than money. Runs launched before
    that ceiling became optional still carry one; taking it off is bringing them up to the
    system default, so it is always permitted — including, as here, from a ceiling the run
    is nowhere near.
    """
    run_id, _ = finished_run

    extend(store, run_id, rounds_target=2, budget_usd=None)

    after = store.get_run(run_id)
    assert after["config"]["budget_usd"] is None, "the next supervisor reads no ceiling"
    assert after["budget_usd"] == 0, "and `check_budget` gates on nothing"


async def test_a_cost_ceiling_may_not_be_lowered(store, finished_run):
    """Lowering one can only spend a process launch to change nothing — the continued run
    would come straight back out of the loop on the ceiling it was just given."""
    run_id, _ = finished_run

    with pytest.raises(ValueError, match="raised or removed, never lowered"):
        extend(store, run_id, rounds_target=2, budget_usd=1.0)

    assert store.get_run(run_id)["budget_usd"] == 100.0, "the refusal changed nothing"


async def test_a_cost_ceiling_of_zero_is_refused_rather_than_read_as_no_ceiling(
    store, finished_run
):
    """Zero is stored as "no ceiling", so accepting it as a *request* would silently give a
    caller who asked for the tightest possible ceiling the loosest one there is."""
    run_id, _ = finished_run

    with pytest.raises(ValueError, match="greater than 0"):
        extend(store, run_id, rounds_target=2, budget_usd=0)


async def test_removing_the_cost_ceiling_reaches_nothing_else_in_the_config(
    store, finished_run
):
    """The narrowness of the exception, restated for the key that was added to it."""
    run_id, _ = finished_run
    before = dict(store.get_run(run_id)["config"])

    extend(store, run_id, rounds_target=3, budget_usd=None)

    after = dict(store.get_run(run_id)["config"])
    assert after["budget_usd"] is None
    assert after["budget_calls"] == before["budget_calls"], "the call ceiling was not named"
    assert untouchable(after) == untouchable(before)
    assert after["model_table"] == before["model_table"]


async def test_an_extension_that_names_no_cost_ceiling_leaves_it_alone(store, finished_run):
    run_id, _ = finished_run
    before = store.get_run(run_id)

    extend(store, run_id, rounds_target=2)

    after = store.get_run(run_id)
    assert after["budget_usd"] == before["budget_usd"]
    assert after["config"]["budget_usd"] == before["config"]["budget_usd"]


async def test_a_target_that_adds_no_round_is_refused(store, finished_run):
    run_id, _ = finished_run

    with pytest.raises(ValueError, match="adds nothing"):
        extend(store, run_id, rounds_target=1)


async def test_a_run_that_is_no_longer_terminal_refuses_the_extension(store, finished_run):
    """Two continues racing: the first takes the run out of `completed`, and the second must
    lose against the row lock rather than bump the target twice."""
    run_id, _ = finished_run
    extend(store, run_id, rounds_target=2)

    with pytest.raises(LifecycleConflict) as raised:
        extend(store, run_id, rounds_target=5)

    assert raised.value.lifecycle == "queued"
    assert store.get_run(run_id)["rounds_target"] == 2, "the loser changed nothing"


# --- what the continued rounds see --------------------------------------------------------


async def test_a_continued_run_keeps_every_hypothesis_and_adds_to_it(store, finished_run):
    run_id, _ = finished_run
    before = ratings(store, run_id)

    extend(store, run_id, rounds_target=3)
    assert await drive(store, run_id, fake(run_id)) == "completed"

    after = ratings(store, run_id)
    assert set(before) <= set(after), "a hypothesis from round 1 disappeared"
    assert len(after) > len(before), "the extra rounds produced nothing"
    assert {hid for hid, row in after.items() if row["created_round"] > 1}, (
        "no hypothesis was created in the rounds that were added"
    )
    assert store.get_run(run_id)["engine_state"]["last_completed_round"] == 3
    assert len(store.snapshot(run_id)["feedback_history"]) == 3, "one meta-review per round"


async def test_the_round_after_a_continue_opens_on_the_stored_guidance(store, finished_run):
    """The meta-review is what makes rounds compound. A continued run that generated
    without it would be a fresh run wearing the old one's id."""
    run_id, _ = finished_run
    guidance = store.get_run(run_id)["feedback_history"][-1]["guidance"]
    assert guidance.startswith("RECURRING ISSUES:")

    extend(store, run_id, rounds_target=2)
    runner = fake(run_id)
    await drive(store, run_id, runner)

    generation = [c for c in runner.calls if c["role"] == "generation" and c["round"] == 2]
    assert generation, "round 2 never generated"
    assert all(guidance in call["prompt"] for call in generation), (
        "round 2 was not handed round 1's guidance"
    )
    titles = [row["title"] for row in store.list_hypotheses(run_id) if row["created_round"] == 1]
    assert all(any(title in call["prompt"] for title in titles) for call in generation), (
        "round 2 was not shown the pool it is meant to avoid duplicating"
    )


async def test_the_tournament_carries_the_standings_rather_than_restarting_them(store):
    """Newcomers are pitted against the incumbents on the ratings they finished on.

    If continuing had reset the tournament, every `elo_before` below would read 1200 and the
    carried values would appear nowhere.

    The scripted model is pinned in two places so this is a statement about the pairing and
    not about the fake's dice: nothing is rejected, and no two hypotheses are ever called the
    same idea. Both of those would otherwise thin the pool at random, and a pool of a random
    size makes "who was paired with whom" unanswerable.
    """
    run_id = make_run(store, **{**QUICK, "matches_per_round": 8})
    runner = candid(run_id)
    assert await drive(store, run_id, runner) == "completed"

    pool = store.list_hypotheses(run_id)
    carried = {row["hid"]: row["elo"] for row in pool}
    assert all(row["created_round"] == 1 for row in pool)
    assert any(elo != INITIAL_ELO for elo in carried.values()), "round 1 moved no ratings"

    extend(store, run_id, rounds_target=2)
    await drive(store, run_id, candid(run_id))

    played = [m for m in store.list_matches(run_id, round=2) if m["status"] == "completed"]
    assert played, "round 2 judged nothing"

    entered: dict[str, list[float]] = {}
    for match in played:
        entered.setdefault(match["hid_a"], []).append(match["elo_a_before"])
        entered.setdefault(match["hid_b"], []).append(match["elo_b_before"])

    veterans = [hid for hid in entered if carried.get(hid, INITIAL_ELO) != INITIAL_ELO]
    assert veterans, "no hypothesis that had actually earned a rating played in round 2"
    for hid in veterans:
        assert carried[hid] in entered[hid], (
            f"{hid} entered round 2 at {entered[hid]} instead of the {carried[hid]} it earned"
        )

    assert [hid for hid in entered if hid not in carried], "round 2 fielded no newcomers"
    assert [
        match
        for match in played
        if (match["hid_a"] in carried) != (match["hid_b"] in carried)
    ], "the new ideas never met an incumbent, so their ratings mean nothing next to them"


async def test_matches_already_judged_are_never_replayed(store, finished_run):
    """Round 1's tournament is paid for. A continue must not buy it again."""
    run_id, _ = finished_run
    settled = {m["id"]: m["winner"] for m in store.list_matches(run_id, round=1)}

    extend(store, run_id, rounds_target=3)
    runner = fake(run_id)
    await drive(store, run_id, runner)

    assert {m["id"]: m["winner"] for m in store.list_matches(run_id, round=1)} == settled
    assert all(call["round"] in (2, 3, None) for call in runner.calls), (
        "the continued run re-executed a round it had already completed"
    )


# --- the report ----------------------------------------------------------------------------


async def test_a_continued_run_rewrites_its_overview_from_the_fuller_picture(
    store, finished_run
):
    """Not appended to and not left stale: a second document, composed from the whole run.

    The overview prompt is never shown the previous overview, so what comes back is a
    rewrite by construction — what this pins is that it is written *at all*, that it saw the
    bigger pool, and that the guard which stops a resumed `finishing` run paying twice does
    not also stop this.
    """
    run_id, _ = finished_run
    first = store.get_run(run_id)["engine_state"]["overview_md"]
    assert first

    extend(store, run_id, rounds_target=3)
    assert store.get_run(run_id)["engine_state"]["overview_stale"] is True
    assert store.snapshot(run_id)["run"]["has_overview"], (
        "the previous report stands until a better one replaces it"
    )

    runner = fake(run_id)
    await drive(store, run_id, runner)

    state = store.get_run(run_id)["engine_state"]
    assert state["overview_md"] != first, "the run finished on the report it wrote at round 1"
    assert state["overview_stale"] is False
    assert store.snapshot(run_id)["run"]["has_overview"]

    prompts = [call["prompt"] for call in runner.calls if call["role"] == "overview"]
    assert len(prompts) == 1, "one overview per ending, not one per round"
    assert "RUN SUMMARY: 3 round(s) completed" in prompts[0]
    for hid in (row["hid"] for row in store.list_hypotheses(run_id, statuses=["active"])):
        assert hid in prompts[0], f"{hid} was left out of the report that summarises it"


async def test_a_run_driven_again_without_an_extension_still_changes_nothing(
    store, finished_run
):
    """The idempotence that makes a re-launch harmless has to survive the stale flag."""
    run_id, _ = finished_run
    before = store.get_run(run_id)

    runner = fake(run_id)
    assert await drive(store, run_id, runner) == "completed"

    after = store.get_run(run_id)
    assert runner.calls == []
    assert after["calls_used"] == before["calls_used"]
    assert after["engine_state"]["overview_md"] == before["engine_state"]["overview_md"]


# --- the budget ----------------------------------------------------------------------------


async def test_a_run_stopped_by_its_cost_ceiling_is_rescued_by_removing_it(store):
    """The run this feature was built for, in miniature.

    A real run asked for three rounds, stopped in round two on `budget_warning reason=usd`
    with most of its call budget unused, and wrote its report — a good ending to a run that
    should never have been stopped, because the ceiling it hit gates on API-equivalent
    telemetry and the calls were made on a subscription. Before this, the only way onward
    was a new run, which throws away every hypothesis, rating, review and round of guidance
    the stopped one produced. This is that situation exactly: a completed run whose
    `spend_usd` is past its `budget_usd`, continued by taking the ceiling off.

    What is asserted about the continued round is that it *ran* — its steps executed, its
    rows landed, its guidance was written — and not that the pool grew. Those are different
    claims, and only the first is the rescue's. A dollar ceiling stops the run at whichever
    step it is on, so the round a continue picks up is a *partial* round, and only two steps
    in a round create hypotheses: generation and evolution. About one seed in twelve stops
    the run past both of them, leaving a round whose remaining work is reviews, a
    re-clustering and the meta-review. Continuing that round buys exactly those and not one
    new idea — the engine declining to pay a second time for work it already has — so
    `len(pool)` grew is a statement about where the ceiling happened to land rather than
    about the feature. `test_a_continued_run_keeps_every_hypothesis_and_adds_to_it` is where
    the pool has whole rounds to grow in and is asserted to.
    """
    run_id = make_run(store, rounds=3, budget_calls=500, budget_usd=0.05)
    assert await drive(store, run_id, fake(run_id)) == "completed"

    stopped = store.get_run(run_id)
    # Absent rather than 0 when a ceiling inside round 1 means no round ever completed.
    completed = int(stopped["engine_state"].get("last_completed_round", 0))
    reasons = [e["payload"]["reason"] for e in events_of(store, run_id, "budget_warning")]
    assert "usd" in reasons, "this test is no longer reproducing the run it was written for"
    assert completed < 3, "the dollar ceiling was meant to stop it short of its target"
    assert stopped["spend_usd"] >= stopped["budget_usd"]
    assert stopped["calls_used"] < 500, "the ceiling that actually governs was never reached"
    pool = ratings(store, run_id)
    reviews = len(store.list_reviews(run_id))
    matches = len(store.list_matches(run_id))
    assert pool, "there is no science here worth rescuing; the test proves nothing"

    extend(store, run_id, rounds_target=completed + 1, budget_usd=None)
    continued = fake(run_id)
    assert await drive(store, run_id, continued) == "completed"

    after = store.get_run(run_id)
    assert after["engine_state"]["last_completed_round"] == completed + 1, "no round ran"
    assert set(ratings(store, run_id)) >= set(pool), "the rescued pool was lost"
    assert all(call["round"] in (completed + 1, None) for call in continued.calls), (
        "the rescue re-executed a round the stopped run had already paid for"
    )
    produced = {
        "hypotheses": len(ratings(store, run_id)) - len(pool),
        "reviews": len(store.list_reviews(run_id)) - reviews,
        "matches": len(store.list_matches(run_id)) - matches,
    }
    assert sum(produced.values()) > 0, f"the continued round produced nothing: {produced}"
    assert after["feedback_history"][-1]["round"] == completed + 1, (
        "the round ran without writing the guidance the next one would be generated against"
    )
    assert after["spend_usd"] > stopped["spend_usd"], "it never spent past where it stopped"
    assert after["engine_state"]["overview_md"], (
        "a ceiling this run had already passed also cost it the report; removing the "
        "ceiling has to buy that back"
    )
    assert "usd" not in [
        e["payload"]["reason"]
        for e in events_of(store, run_id, "budget_warning")[len(reasons) :]
    ], "the ceiling was removed and stopped the run anyway"


async def test_the_overview_reserve_still_holds_after_the_bump(store):
    """A continued run that hits its ceiling mid-round still pays for its report.

    Two calls are reserved for the overview at every step, and a continue is exactly where
    that could quietly stop being true: the ceiling is raised against a run that has already
    spent most of it, so the reserve is being measured from a much higher starting point
    than at launch. This run is continued with barely any headroom, made to run out inside
    the new round, and still has to buy the overview and stay inside its budget.

    What it asserts is that the call *happened*, not that the markdown changed. Those are
    different claims, and only the first one is about the reserve: the scripted model is a
    function of its prompt, so a round that could only afford part of itself legitimately
    composes the same document twice. That is the engine being honest — nothing new to say —
    and `test_a_continued_run_rewrites_its_overview_from_the_fuller_picture` is where the
    text has room to change and is asserted to.
    """
    run_id = make_run(
        store,
        rounds=1,
        generation_batch=9,
        matches_per_round=0,
        evolve_top_k=0,
        budget_calls=20,
    )
    assert await drive(store, run_id, fake(run_id)) == "completed"

    run = store.get_run(run_id)
    used = int(run["calls_used"])
    ceiling = max(int(run["budget_calls"]), used + OVERVIEW_RESERVED_CALLS + 1)
    assert run["engine_state"]["overview_md"], "the first run never wrote a report"

    extend(store, run_id, rounds_target=2, budget_calls=ceiling)
    continued = fake(run_id)
    assert await drive(store, run_id, continued) == "completed"

    run = store.get_run(run_id)
    assert [e for e in events_of(store, run_id, "budget_warning")], (
        "the continued round was meant to reach the ceiling; this test is no longer testing it"
    )
    assert run["calls_used"] <= ceiling, "the run spent past its raised ceiling"
    assert [call for call in continued.calls if call["role"] == "overview"], (
        "the reserve was spent on the round instead of the report"
    )
    assert run["engine_state"]["overview_md"]
    assert run["engine_state"]["overview_stale"] is False

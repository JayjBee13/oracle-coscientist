"""Resume is per unit, never per round — for every step, not just the ones that had it.

The orchestrator's own module docstring promises that "work is resumable per unit, never
per round… every step derives its remaining work from the database". `_generate` keeps a
shard list, `_reflect` derives its work from the missing reviews, and the tournament
persists its plan. Two steps did not: `_cluster` short-circuited on a boolean, and
`_tournament` treated any existing plan as complete.

Run c4566ed2's round 2 is what that costs. It ran twice — the first pass lost both
generation shards, clustered the six hypotheses that existed and wrote `proximity: true`;
the second generated h007–h012, reviewed all six, and then walked past both steps because
the round was marked. Six of twelve active rows entered round 3 with `cluster = NULL`, the
round's collapse snapshot was computed over a half-unlabelled pool (`hhi 0.042`, which is
6·(1/12)²), `round_completed` recorded `matches_completed: 0`, and nothing anywhere said so.

These tests reconstruct that state directly — the marks an abandoned pass leaves, then more
hypotheses, then re-entry — because that is the shape the defect has and driving a full run
to it would test the kill path rather than the resume.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.engine.orchestrator import Orchestrator
from app.engine.runners import FakeRunner
from app.engine.store import RunStore

from .conftest import HookedRunner, make_run

HALTED_ROUND = 1


def seed_pool(store: RunStore, run_id: UUID, count: int, *, start: int = 1) -> list[str]:
    return [
        store.add_hypothesis(
            run_id,
            title=f"Hypothesis {index}",
            body_md=f"# Hypothesis {index}\n\n**Claim:** Claim {index}.\n",
            created_round=HALTED_ROUND,
        )["hid"]
        for index in range(start, start + count)
    ]


def prepared(store: RunStore, run_id: UUID, runner: Any) -> Orchestrator:
    engine = Orchestrator(store, runner, run_id, rate_limit_cooldown=0)
    engine._reload()  # noqa: SLF001 - the supervisor's own first two steps
    engine._prepare()  # noqa: SLF001
    return engine


def singleton_clusters(prompt: str) -> dict[str, Any]:
    import re

    hids = sorted(set(re.findall(r"h\d{3}", prompt)))
    return {"clusters": {hid: f"cluster-{hid}" for hid in hids}}


@pytest.mark.asyncio
async def test_a_resumed_round_clusters_the_hypotheses_its_second_pass_added(store):
    run_id = make_run(store, rounds=1, generation_batch=0, evolve_top_k=0)
    first_pass = seed_pool(store, run_id, 2)
    store.set_engine_state(
        run_id,
        {"steps": {"1": {"proximity": True, "proximity_hids": first_pass, "graft": True}}},
    )
    second_pass = seed_pool(store, run_id, 4, start=3)

    runner = HookedRunner(FakeRunner(seed=str(run_id)), override={"proximity": singleton_clusters})
    engine = prepared(store, run_id, runner)
    await engine._cluster(HALTED_ROUND)  # noqa: SLF001

    labels = {row["hid"]: row["cluster"] for row in store.list_hypotheses(run_id)}
    assert all(labels[hid] for hid in second_pass), (
        "the hypotheses the second pass added were never clustered by the round that made them"
    )
    assert all(labels[hid] for hid in first_pass)


@pytest.mark.asyncio
async def test_a_round_whose_clustering_already_covered_the_pool_does_not_pay_again(store):
    run_id = make_run(store, rounds=1, generation_batch=0, evolve_top_k=0)
    covered = seed_pool(store, run_id, 3)
    store.set_engine_state(
        run_id, {"steps": {"1": {"proximity": True, "proximity_hids": covered}}}
    )

    runner = HookedRunner(FakeRunner(seed=str(run_id)), override={"proximity": singleton_clusters})
    engine = prepared(store, run_id, runner)
    await engine._cluster(HALTED_ROUND)  # noqa: SLF001

    assert [call for call in runner.calls if call["role"] == "proximity"] == []


@pytest.mark.asyncio
async def test_the_collapse_reading_is_taken_over_what_proximity_said_not_what_dedup_left(store):
    """`collapse_signals` measured the pool *after* `apply_clusters` archived every
    non-champion, which guarantees one member per surviving label — so `herfindahl` is
    `n·(1/n)² = 1/n` and the `hhi ≥ 0.5` vote needs `n ≤ 2`, while `n_clusters == n_active`
    makes the zero-slack plateau vote need a pool that never grows. On a healthy proximity
    call the detector could not reach its quorum of two by arithmetic. Run c4566ed2's round
    3 recorded `n_active 19, n_clusters 19, hhi 0.053` — and 0.053 is 1/19 exactly."""
    run_id = make_run(
        store,
        rounds=1,
        generation_batch=0,
        evolve_top_k=0,
        graft={"enabled": True, "window": 1, "hhi_threshold": 0.5, "quorum_k": 3},
    )
    hids = seed_pool(store, run_id, 4)
    one_idea = {"clusters": dict.fromkeys(hids, "a single idea")}

    runner = HookedRunner(FakeRunner(seed=str(run_id)), override={"proximity": lambda _: one_idea})
    engine = prepared(store, run_id, runner)
    await engine._cluster(HALTED_ROUND)  # noqa: SLF001
    await engine._collapse_check(HALTED_ROUND)  # noqa: SLF001

    active = store.list_hypotheses(run_id, statuses=["active"])
    assert len(active) == 1, "dedup leaves one champion, which is what used to be measured"
    snapshot = store.get_run(run_id)["engine_state"]["collapse_history"][-1]
    assert snapshot["n_active"] == 4
    assert snapshot["n_clusters"] == 1
    assert snapshot["hhi"] == 1.0, "four ideas under one label is a concentrated pool"
    assert snapshot["coverage"] == 1.0


@pytest.mark.asyncio
async def test_a_resumed_round_ranks_the_hypotheses_its_second_pass_added(store):
    run_id = make_run(store, rounds=1, generation_batch=0, evolve_top_k=0, matches_per_round=2)
    first_pass = seed_pool(store, run_id, 2)
    plan = store.plan_matches(run_id, HALTED_ROUND, [(first_pass[0], first_pass[1])])
    store.set_engine_state(run_id, {"steps": {"1": {"tournament_pool": first_pass}}})
    store.record_match(UUID(plan[0]["id"]), winner=1)
    second_pass = seed_pool(store, run_id, 2, start=3)

    engine = prepared(store, run_id, FakeRunner(seed=str(run_id)))
    completed = await engine._tournament(HALTED_ROUND)  # noqa: SLF001

    matches = store.list_matches(run_id, round=HALTED_ROUND)
    played = {hid for row in matches for hid in (row["hid_a"], row["hid_b"])}
    assert completed >= 1
    assert set(second_pass) <= played, "the round judged nothing for the rows it had added"
    assert len([row for row in matches if row["status"] == "completed"]) == 2
    assert matches[0]["id"] == plan[0]["id"], "the match already judged was not replanned"


@pytest.mark.asyncio
async def test_a_complete_plan_over_a_complete_pool_is_replayed_untouched(store):
    run_id = make_run(store, rounds=1, generation_batch=0, evolve_top_k=0, matches_per_round=2)
    hids = seed_pool(store, run_id, 2)
    plan = store.plan_matches(run_id, HALTED_ROUND, [(hids[0], hids[1])])
    store.set_engine_state(run_id, {"steps": {"1": {"tournament_pool": hids}}})

    engine = prepared(store, run_id, FakeRunner(seed=str(run_id)))
    await engine._tournament(HALTED_ROUND)  # noqa: SLF001

    assert [row["id"] for row in store.list_matches(run_id, round=HALTED_ROUND)] == [
        plan[0]["id"]
    ]


@pytest.mark.asyncio
async def test_re_entering_a_round_that_added_nothing_does_not_grow_its_tournament(store):
    """`matches_per_round` is a ceiling the scientist set. A round whose pool is bigger than
    its match budget always leaves hypotheses unplayed, and a resume inside the same pass —
    a budget stop, a pause between matches — must not read that as a plan to repair."""
    run_id = make_run(store, rounds=1, generation_batch=0, evolve_top_k=0, matches_per_round=1)
    hids = seed_pool(store, run_id, 6)
    store.plan_matches(run_id, HALTED_ROUND, [(hids[0], hids[1])])
    store.set_engine_state(run_id, {"steps": {"1": {"tournament_pool": hids}}})

    engine = prepared(store, run_id, FakeRunner(seed=str(run_id)))
    await engine._tournament(HALTED_ROUND)  # noqa: SLF001

    assert len(store.list_matches(run_id, round=HALTED_ROUND)) == 1

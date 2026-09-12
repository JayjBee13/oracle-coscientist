"""Information the loop collected and then dropped on the floor.

Each test here pins one seam the data-chain audit found broken against the owner's real run
c4566ed2: something the engine knew, spent money to learn, or was told by the scientist, and
which then reached nobody.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.engine.orchestrator import Orchestrator, run_engine
from app.engine.runners import Failure, FakeRunner
from app.engine.store import RunStore

from .conftest import events_of, make_run


async def drive(store: RunStore, run_id: UUID, runner) -> str:
    return await run_engine(run_id, store, runner, rate_limit_cooldown=0)


def fake(run_id: UUID, **kwargs) -> FakeRunner:
    return FakeRunner(seed=str(run_id), **kwargs)


def payloads(store: RunStore, run_id: UUID, type: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events_of(store, run_id, type)]


def prepared(store: RunStore, run_id: UUID) -> Orchestrator:
    engine = Orchestrator(store, FakeRunner(seed=str(run_id)), run_id, rate_limit_cooldown=0)
    engine._reload()  # noqa: SLF001
    engine._prepare()  # noqa: SLF001
    return engine


# --- the schema-repair re-ask -----------------------------------------------------------


async def test_a_schema_repair_re_ask_leaves_a_trace(store):
    """`_record_call` has already emitted `call_finished` with `ok: true` and written an
    `ok` ledger row by the time `validate_role_output` rejects the payload, so a repaired
    call was a successful call that produced no result and said nothing about it. Run
    c4566ed2 has 24 `ok` reflection ledger rows and 21 reviews; the three orphans cost
    $0.95 and 4.4 minutes, and the 20-line supervisor.log mentions none of them."""
    run_id = make_run(store, rounds=1)
    runner = fake(run_id, failures={("evolution", 1): Failure(malformed=True, times=1)})

    await drive(store, run_id, runner)

    repairs = payloads(store, run_id, "schema_repaired")
    assert len(repairs) == 1
    assert repairs[0]["role"] == "evolution"
    assert repairs[0]["attempt"] == 1
    assert repairs[0]["problem"], "the violation itself is what makes the event useful"


# --- what the run lost, counted once ----------------------------------------------------


async def test_the_report_and_the_run_header_count_the_same_losses(store):
    """`_health_note` and the `run_finished` payload read this process's in-memory list;
    the run list and header read the event log. c4566ed2 reported `lost_steps: 0` and an
    empty RUN HEALTH section in its 28,119-character report while the store's reading of
    the same database said 11 failed calls and 3 lost steps."""
    run_id = make_run(store, rounds=1)
    runner = fake(
        run_id, failures={("evolution", 1): Failure(error="timeout after 420s", times=9)}
    )

    await drive(store, run_id, runner)

    finished = payloads(store, run_id, "run_finished")[0]
    health = store.snapshot(run_id)["run"]
    assert finished["lost_steps"] == health["lost_steps"] == 1
    assert finished["failed_calls"] == health["failed_calls"]


async def test_a_lineage_warning_is_not_reported_as_a_lost_step(store):
    """The evolution lineage violation loses no step — the offspring is still added — and
    counting it as one both overstated `lost_steps` and understated `retried_calls`."""
    run_id = make_run(store, rounds=1, evolve_top_k=1)

    def invents_a_parent(_prompt: str) -> dict[str, Any]:
        return {
            "hypotheses": [
                {
                    "title": "A variant with an invented parent",
                    "claim": "The claim.",
                    "mechanism": "m",
                    "novelty": "n",
                    "test": "t",
                    "assumptions": "a",
                    "derived_from": ["h999"],
                    "operator": "grounding",
                }
            ]
        }

    from .conftest import HookedRunner

    runner = HookedRunner(fake(run_id), override={"evolution": invents_a_parent})
    await drive(store, run_id, runner)

    violations = payloads(store, run_id, "contract_violation")
    assert any(item.get("unit") == "lineage" for item in violations)
    assert store.snapshot(run_id)["lost_steps"] == 0
    assert any(
        row["title"] == "A variant with an invented parent"
        for row in store.list_hypotheses(run_id)
    )


# --- the scientist's own inputs ----------------------------------------------------------


def test_a_note_the_scientist_typed_survives_past_the_round_that_claimed_it(store):
    """Notes were stored under the round that claimed them and rebuilt from that round's
    key alone, so a standing instruction rendered as "outranks everything else in this
    prompt" was obeyed once and silently forgotten."""
    run_id = make_run(store, rounds=3)
    store.add_note(run_id, "Never propose anything that needs a new licence.")
    engine = prepared(store, run_id)

    round_one = engine._drain_interventions(1)  # noqa: SLF001
    engine._reload()  # noqa: SLF001
    round_two = engine._drain_interventions(2)  # noqa: SLF001

    assert round_one == ("Never propose anything that needs a new licence.",)
    assert round_two == round_one, "the human's constraint expired after one round"


def test_the_run_says_what_became_of_each_context_document(store):
    """`build_context_block` computed `included`/`truncated`/`omitted` from the first run
    onward and nothing read them: the only trace of a megabyte of documents reduced to
    20,000 characters was one `log.info` line inside the run's workdir."""
    run_id = make_run(store, rounds=1)
    store.add_context_docs(
        run_id,
        [
            {"name": "protocol.md", "content": "P" * 200_000},
            {"name": "priors.md", "content": "R" * 500},
        ],
    )

    engine = prepared(store, run_id)

    delivery = store.get_run(run_id)["engine_state"]["context_delivery"]
    assert delivery == {"protocol.md": "truncated", "priors.md": "full"}
    cut = payloads(store, run_id, "context_truncated")
    assert cut and cut[0]["truncated"] == ["protocol.md"]
    assert cut[0]["omitted"] == []
    assert len(engine._context.text) <= cut[0]["cap"]  # noqa: SLF001
    docs = {doc["name"]: doc for doc in store.snapshot(run_id)["context_docs"]}
    assert docs["protocol.md"]["delivered"] == "truncated"
    assert docs["priors.md"]["delivered"] == "full"


def test_documents_that_fit_raise_no_alarm(store):
    run_id = make_run(store, rounds=1)
    store.add_context_docs(run_id, [{"name": "brief.md", "content": "Short and complete."}])

    prepared(store, run_id)

    assert payloads(store, run_id, "context_truncated") == []
    docs = store.snapshot(run_id)["context_docs"]
    assert docs[0]["delivered"] == "full"


# --- lineage that survives the first generation ------------------------------------------


async def test_an_offspring_of_seeded_parents_inherits_the_seed(store):
    """`_evolve` never passed `seed_id`, so cartographer attribution stopped at the first
    generation and any measurement of "what did the graft actually produce" undercounted
    every descendant."""
    run_id = make_run(store, rounds=1, generation_batch=0, evolve_top_k=1)
    for index in (1, 2):
        store.add_hypothesis(
            run_id,
            title=f"Seeded {index}",
            body_md=f"# Seeded {index}\n\n**Claim:** From the seed.\n",
            created_round=1,
            seed_id="seed-r1",
        )

    engine = prepared(store, run_id)
    added = await engine._evolve(1, _round_context(engine))  # noqa: SLF001

    assert added >= 1
    offspring = [row for row in store.list_hypotheses(run_id) if row["parent_ids"]]
    assert offspring, "the fake evolution call produced no lineage to check"
    assert all(row["seed_id"] == "seed-r1" for row in offspring)


def _round_context(engine: Orchestrator):
    from app.engine.prompts import RoundContext

    return RoundContext(goal=engine._goal, round=1, context=engine._context)  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_seed_consumed_by_a_finished_generation_step_is_not_consumed_twice(store):
    """`pending_seed` was cleared at the bottom of `_generate`, which is unreachable on a
    re-entry into a round whose generation already finished — so one cartographer seed was
    consumed by two generation waves and `seed_id` stopped naming one wave."""
    run_id = make_run(store, rounds=2, generation_batch=3)
    store.set_graft_state(
        run_id,
        {
            "pending_seed": {
                "seed_id": "seed-r1",
                "round": 1,
                "source_domain": "glacial hydrology",
                "skeleton": "- a reservoir",
                "seed_framing": "treat the pool as a reservoir",
            }
        },
    )
    store.set_engine_state(run_id, {"steps": {"1": {"generation_shards": [0]}}})

    engine = prepared(store, run_id)
    added = await engine._generate(1, _seeded_context(engine))  # noqa: SLF001

    assert added == 0, "the round's only shard was already done"
    assert store.get_run(run_id)["graft_state"]["pending_seed"] is None


def _seeded_context(engine: Orchestrator):
    from app.engine.prompts import RoundContext, Seed

    return RoundContext(
        goal=engine._goal,  # noqa: SLF001
        round=1,
        context=engine._context,  # noqa: SLF001
        seed=Seed.from_mapping(
            (engine._run.get("graft_state") or {}).get("pending_seed")  # noqa: SLF001
        ),
    )

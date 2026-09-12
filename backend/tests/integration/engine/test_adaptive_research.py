"""The adaptive process must improve work, survive interruption and state its limits."""

from __future__ import annotations

import pytest

from app.engine.orchestrator import run_engine
from app.engine.research import research_view
from app.engine.research_contracts import (
    assess,
    calculate,
    portfolio,
    validate_framing,
    validate_verification,
)
from app.engine.runners import Failure, FakeRunner
from app.engine.schemas import validate_role_output

from .conftest import HookedRunner, make_run


async def test_complete_adaptive_research_preserves_breadth_and_challenges_answer(store):
    run_id = make_run(store, workflow="adaptive", rounds=3)
    runner = FakeRunner(seed=str(run_id))
    assert await run_engine(run_id, store, runner) == "completed"
    state = store.get_run(run_id)["engine_state"]
    research = state["research"]
    assert [d["action"] for d in research["decisions"].values()] == [
        "explore",
        "develop",
        "explore",
    ], {k: v.get("error") for k, v in research["units"].items()}
    assert len(research["frame"]["approaches"]) == 3
    assert research["synthesis"]["round"] == research["challenge"]["round"] == 3
    assert all(a["readiness"] == "provisional" for a in research["assessments"].values())
    assert all(a["calculation_results"][0]["passed"] for a in research["assessments"].values())
    assert "Independent challenge" in state["overview_md"]
    assert "Unresolved blocking issues" in state["overview_md"]
    assert len(store.list_reviews(run_id)) == len(store.list_hypotheses(run_id))
    assert any(row["parent_ids"] for row in store.list_hypotheses(run_id))
    assert not store.list_matches(run_id), "Elo is not the adaptive workflow's selection objective"
    generation = [call for call in runner.calls if call["role"] == "generation"]
    assert len(generation) == 6
    assert all("previous_challenge" not in call["prompt"] for call in generation)
    assert "Assumption inversion" not in generation[0]["prompt"]
    assert "Structural analogy and holistic design" not in generation[0]["prompt"]


async def test_pause_resume_does_not_repeat_completed_research_calls(store):
    run_id = make_run(store, workflow="adaptive", rounds=2)
    inner = FakeRunner(seed=str(run_id))

    def pause(role, prompt, cfg):
        if role == "verification":
            store.set_control(run_id, "pause")

    assert await run_engine(run_id, store, HookedRunner(inner, before=pause)) == "paused"
    completed = {call["unit"] for call in inner.calls}
    second = FakeRunner(seed=str(run_id))
    assert await run_engine(run_id, store, second) == "completed"
    assert not completed.intersection(call["unit"] for call in second.calls)
    assert len({row["hid"] for row in store.list_hypotheses(run_id)}) == len(
        store.list_hypotheses(run_id)
    )


async def test_failed_challenge_is_not_silently_reported_as_a_validated_solution(store):
    run_id = make_run(store, workflow="adaptive", rounds=1)
    runner = FakeRunner(failures={("challenge", None): Failure(error="unavailable")})
    await run_engine(run_id, store, runner)
    state = store.get_run(run_id)["engine_state"]
    assert state["research"].get("challenge") is None
    assert "has not received a successful independent challenge" in state["overview_md"]
    assert store.snapshot(run_id)["run"]["lost_steps"] >= 1


async def test_tiny_budget_still_delivers_an_honest_report(store):
    run_id = make_run(store, workflow="adaptive", budget_calls=3)
    runner = FakeRunner()
    await run_engine(run_id, store, runner)
    assert len(runner.calls) <= 3
    assert (
        "No complete solution was produced" in store.get_run(run_id)["engine_state"]["overview_md"]
    )


async def test_parallel_repairs_cannot_spend_the_report_reserve(store):
    run_id = make_run(store, workflow="adaptive", rounds=1, budget_calls=8)
    runner = FakeRunner(failures={("generation", None): Failure(malformed=True)})
    await run_engine(run_id, store, runner)
    research_calls = [c for c in runner.calls if c["role"] in ("framing", "generation")]
    assert len(research_calls) <= 6
    assert store.get_run(run_id)["engine_state"]["overview_md"]


@pytest.mark.parametrize(
    "expression", ["__import__('os')", "2 ** 9999999", "(1).__class__", "1/0", "[1]*9999"]
)
def test_calculator_refuses_code_or_unbounded_operations(expression):
    with pytest.raises((ValueError, ZeroDivisionError)):
        calculate(expression)


def test_source_claims_without_a_search_and_failed_arithmetic_stay_unresolved():
    data = FakeRunner()._verification("", 0)
    data["claims"][0].update(
        status="supported",
        sources=[
            {"url": "https://example.org/study", "finding": "A claim", "relation": "supports"}
        ],
    )
    data["open_questions"] = []
    assert assess(data, searched=False)["readiness"] == "provisional"
    data["calculations"][0]["expected"] = 99
    assert assess(data, searched=True)["readiness"] == "provisional"


def test_bad_dependency_graph_is_rejected():
    data = FakeRunner()._framing("", 0)
    data["subproblems"][0]["depends_on"] = ["b2"]
    assert validate_framing(data)


def test_portfolio_preserves_a_different_approach_despite_low_elo():
    rows = [{"hid": f"h{i}", "status": "active", "elo": 1800 if i < 3 else 900} for i in range(4)]
    chosen = portfolio(rows, {"h0": "a", "h1": "a", "h2": "a", "h3": "b"}, {}, 2)
    assert {row["hid"] for row in chosen} == {"h0", "h3"}


async def test_restart_after_insert_materializes_cached_results_without_duplicates(
    store, monkeypatch
):
    run_id = make_run(store, workflow="adaptive", rounds=1)
    first = FakeRunner(seed=str(run_id))
    original = store.add_hypothesis
    crashed = False

    def interrupted_insert(*args, **kwargs):
        nonlocal crashed
        row = original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash after candidate insert")
        return row

    monkeypatch.setattr(store, "add_hypothesis", interrupted_insert)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await run_engine(run_id, store, first)
    store.set_lifecycle(run_id, "paused")
    second = FakeRunner(seed=str(run_id))
    await run_engine(run_id, store, second)
    assert len(store.list_hypotheses(run_id)) == 6
    assert not [c for c in second.calls if c["role"] in ("generation", "framing")]


def test_stale_or_withdrawn_work_cannot_be_recommendation_ready():
    state = {
        "frame_key": "f1",
        "guidance_digest": "g1",
        "synthesis": {
            "round": 1,
            "frame_key": "f1",
            "guidance_digest": "g1",
            "used_hids": ["h1"],
            "unresolved": [],
            "dependencies": [],
        },
        "challenge": {"round": 1, "blocking_issues": []},
        "assessments": {"h1": {"readiness": "source_supported"}},
    }
    rows = [{"hid": "h1", "title": "A candidate", "status": "active"}]
    assert research_view(state, rows)["recommendation_ready"]
    rows[0]["status"] = "archived"
    assert not research_view(state, rows)["recommendation_ready"]
    rows[0]["status"] = "active"
    state["guidance_digest"] = "g2"
    assert not research_view(state, rows)["recommendation_ready"]


def test_mixed_evidence_and_malformed_urls_never_become_confirmed():
    data = FakeRunner()._verification("", 0)
    data["open_questions"] = []
    data["claims"][0].update(
        status="supported",
        sources=[
            {"url": "https://example.org/a", "finding": "support", "relation": "supports"},
            {
                "url": "https://example.org/b",
                "finding": "counterexample",
                "relation": "contradicts",
            },
            {"url": "http://[bad", "finding": "malformed", "relation": "supports"},
        ],
    )
    result = assess(data, searched=True)
    assert result["readiness"] == "provisional"
    assert len(result["claims"][0]["sources"]) == 2
    data["calculations"][0]["expected"] = float("inf")
    assert validate_verification(data)


def test_counterevidence_remains_visible_when_candidate_leaves_portfolio():
    data = FakeRunner()._verification("", 0)
    data["claims"][0].update(
        status="contradicted",
        sources=[
            {
                "url": "https://example.org/test",
                "finding": "Counterexample",
                "relation": "contradicts",
            }
        ],
    )
    assessment = assess(data, searched=True)
    assert assessment["readiness"] == "contradicted"
    view = research_view(
        {"assessments": {"h1": assessment}},
        [{"hid": "h1", "title": "Rejected premise", "status": "active"}],
    )
    assert not view["portfolio"]
    assert view["contradictions"][0]["claims"][0]["sources"][0]["finding"] == "Counterexample"


async def test_adaptive_calls_honor_configured_effort_and_grounding(store):
    run_id = make_run(
        store,
        workflow="adaptive",
        rounds=1,
        grounding_depth="shallow",
        model_overrides={"generation": {"effort": "medium"}},
    )
    seen = []

    def observe(role, prompt, cfg):
        if role == "generation":
            seen.append(cfg)

    await run_engine(run_id, store, HookedRunner(FakeRunner(), before=observe))
    assert len(seen) == 3
    assert all(cfg.effort == "medium" and "at most 1 search" in cfg.system_prompt for cfg in seen)


async def test_decisive_challenge_target_gets_checked_before_untargeted_backlog(store):
    run_id = make_run(
        store, workflow="adaptive", rounds=2, generation_batch=12, matches_per_round=3
    )
    target = []

    def challenge(prompt):
        research = store.get_run(run_id)["engine_state"]["research"]
        selected = research_view(research, store.list_hypotheses(run_id))["portfolio"]
        if not target:
            target.append(selected[-1]["hid"])
        return {
            "assessment": "A decisive premise needs checking.",
            "blocking_issues": ["Decisive premise is unresolved."],
            "next_action": "verify",
            "target_hids": target,
            "tests": ["Check the targeted premise."],
        }

    await run_engine(run_id, store, HookedRunner(FakeRunner(), override={"challenge": challenge}))
    research = store.get_run(run_id)["engine_state"]["research"]
    assert research["decisions"]["2"]["action"] == "verify"
    assert target[0] in research["verification_plans"]["2"]


async def test_topic_source_strategy_reaches_every_grounded_role_and_survives_report(store):
    run_id = make_run(store, workflow="adaptive", rounds=2)
    observed = {}

    def observe(role, prompt, cfg):
        if cfg.tools:
            observed[role] = cfg.system_prompt

    await run_engine(run_id, store, HookedRunner(FakeRunner(), before=observe))
    assert set(observed) == {"generation", "reflection", "evolution", "verification", "challenge"}
    for prompt in observed.values():
        assert "70% published scholarly research / 30% other authoritative web sources" in prompt
        assert "peer-reviewed journal articles" in prompt
        assert "not actual source counts" in prompt
    snapshot = store.snapshot(run_id)
    assert snapshot["research"]["source_strategy"]["published_research_percent"] == 70
    assert "not measured source counts" in store.get_run(run_id)["engine_state"]["overview_md"]


@pytest.mark.parametrize("share", [-1, 101, 20.5, True])
def test_source_strategy_rejects_invalid_proportions(share):
    frame = FakeRunner()._framing("", 0)
    frame["source_strategy"]["published_research_percent"] = share
    assert validate_role_output("framing", frame)


def test_older_framing_without_source_strategy_stays_readable():
    assert research_view({"frame": {"objective": "Earlier run"}}, [])["source_strategy"] is None

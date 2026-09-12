"""`ProviderRouter`: which CLI runs a call, decided from the row rather than from the run.

The orchestrator's call site is unchanged by any of this — it still builds a `RoleConfig` and
hands it to one `AgentRunner`. What these tests pin is that the runner it holds sends each
call to the lane its *model* belongs to, and that a lane nobody uses is never built.
"""

from __future__ import annotations

import pytest

from app.engine.models import ModelPolicyError
from app.engine.routing import ProviderRouter
from app.engine.runners import (
    AgentRunner,
    FakeRunner,
    RoleResult,
    resolve_model_table,
    role_config,
)


class Lane(FakeRunner):
    """A `FakeRunner` that knows which lane it is, so a test can see where a call went."""

    def __init__(self, name: str) -> None:
        super().__init__(seed=name)
        self.name = name
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def cfg(model: str, role: str = "ranking"):
    return role_config(role, model=model, effort="low", system_prompt="…")


def router(built: list[str] | None = None) -> tuple[ProviderRouter, dict[str, Lane]]:
    lanes: dict[str, Lane] = {}
    log = built if built is not None else []

    def make(name: str):
        def factory() -> Lane:
            log.append(name)
            lanes[name] = Lane(name)
            return lanes[name]

        return factory

    return ProviderRouter({"anthropic": make("claude"), "openai": make("codex")}), lanes


def test_the_router_is_an_agent_runner_the_orchestrator_can_hold():
    routed, _ = router()

    assert isinstance(routed, AgentRunner)


async def test_a_call_goes_to_the_lane_its_model_belongs_to():
    routed, lanes = router()

    await routed.run_role("ranking", "h001 h002", cfg("fable"))
    await routed.run_role("proximity", "h001 | a | b", cfg("gpt-5.6-luna", "proximity"))

    assert [call["model"] for call in lanes["claude"].calls] == ["fable"]
    assert [call["model"] for call in lanes["codex"].calls] == ["gpt-5.6-luna"]


async def test_astra_override_routes_to_codex_with_its_requested_effort():
    table = resolve_model_table("openai", "high", overrides={
        "generation": {"model": "gpt-6-astra", "effort": "xhigh"},
    })
    generation = next(row for row in table if row["role"] == "generation")
    assert generation["model"] == "gpt-6-astra" and generation["effort"] == "xhigh"
    routed, lanes = router()
    call = role_config("generation", model=generation["model"], effort=generation["effort"],
                       system_prompt="Research the goal.")
    await routed.run_role("generation", "N: 1", call)
    assert list(lanes) == ["codex"]
    assert lanes["codex"].calls[0]["model"] == "gpt-6-astra"
    assert lanes["codex"].calls[0]["effort"] == "xhigh"


async def test_one_run_may_mix_providers_step_by_step():
    """The provider button is a quick-set, not a lock: Fable judging a tournament whose
    clustering runs on Luna is a table the engine has to be able to execute, not merely to
    store."""
    routed, lanes = router()

    for model, role in (
        ("fable", "generation"),
        ("gpt-5.6-luna", "proximity"),
        ("fable", "ranking"),
        ("gpt-5.6-sol", "meta_review"),
    ):
        result = await routed.run_role(role, "h001 | a | b\nh002 | c | d", cfg(model, role))
        assert isinstance(result, RoleResult)

    assert len(lanes["claude"].calls) == 2
    assert len(lanes["codex"].calls) == 2


async def test_a_low_tier_table_under_anthropic_executes_entirely_on_the_codex_lane():
    """The `low` tier pins every step to Luna, so a run stored with `provider="anthropic"`
    drives Codex for every call. Resolving that table is one claim; executing it is the one
    that matters, and it is this router that makes the two the same — the run's provider never
    reaches the dispatch, only each row's model does.

    Which is also why Low requires the codex CLI: the claude lane is never even opened."""
    routed, lanes = router()
    table = resolve_model_table("anthropic", "low")

    for row in table:
        result = await routed.run_role(
            row["role"],
            "h001 | a | b\nh002 | c | d",
            role_config(
                row["role"], model=row["model"], effort=row["effort"], system_prompt="…"
            ),
        )
        assert isinstance(result, RoleResult)

    assert len(lanes["codex"].calls) == len(table)
    assert "claude" not in lanes, "a Low run never opens the Anthropic lane"


async def test_a_lane_nobody_uses_is_never_constructed():
    """Building a `CodexCliRunner` resolves `codex.exe` off the filesystem. An eager router
    would fail an all-Anthropic run at startup on a box with no Codex installed, with an
    error about a CLI it was never going to use."""
    built: list[str] = []
    routed, _ = router(built)

    await routed.run_role("ranking", "h001 h002", cfg("fable"))

    assert built == ["claude"]


async def test_a_lane_is_built_once_and_reused():
    built: list[str] = []
    routed, _ = router(built)

    await routed.run_role("ranking", "h001 h002", cfg("fable"))
    await routed.run_role("ranking", "h003 h004", cfg("fable"))

    assert built == ["claude"]


async def test_closing_closes_every_lane_that_was_opened_and_no_others():
    routed, lanes = router()
    await routed.run_role("ranking", "h001 h002", cfg("fable"))

    await routed.aclose()

    assert lanes["claude"].closed == 1
    assert "codex" not in lanes


async def test_a_model_whose_provider_has_no_runner_is_refused_by_name():
    """Better than a `NoneType has no run_role` three frames down, and it is a configuration
    this build genuinely cannot execute rather than a model-side failure."""
    routed = ProviderRouter({"anthropic": lambda: Lane("claude")})

    with pytest.raises(ModelPolicyError, match="no runner is configured"):
        await routed.run_role("ranking", "h001 h002", cfg("gpt-5.6-sol"))


def test_the_routers_name_is_the_default_lane_the_run_holds():
    """The run's `harness` column is single-valued and is the default provider's lane —
    solo-operator semantics, and a lane lock rather than a description of the table."""
    assert ProviderRouter({}, default_provider="anthropic").name == "claude"
    assert ProviderRouter({}, default_provider="openai").name == "codex"


async def test_a_demo_run_never_reaches_the_router_at_all(tmp_path):
    """The supervisor answers `runner: demo` with the fake before any CLI layer is imported,
    which is what lets the demo work on a machine with neither CLI installed."""
    from app.engine.supervisor import build_runner

    runner = build_runner(
        run_id=None,  # type: ignore[arg-type]
        run={"config": {"runner": "demo"}},
        workdir=tmp_path,
        store=None,  # type: ignore[arg-type]
    )

    assert isinstance(runner, FakeRunner)

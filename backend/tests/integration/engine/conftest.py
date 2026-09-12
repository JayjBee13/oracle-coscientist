"""Fixtures for full-loop engine runs against the session's throwaway schema."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.engine.runners import FakeRunner, RoleConfig, RoleResult, resolve_model_table
from app.engine.store import RunStore
from tests.support.lanes import release_lanes

ENGINE_TABLES = (
    "run_events",
    "budget_ledger",
    "graft_events",
    "matches",
    "reviews",
    "hypotheses",
    "run_context_docs",
    "runs",
)

GOAL = "Why do some lakes bloom under falling nutrient loads?"


@pytest.fixture(scope="session")
def session_factory(isolated_schema):
    return get_session_factory(get_settings())


@pytest.fixture
def store(session_factory) -> Iterator[RunStore]:
    with session_factory() as session:
        schema = session.execute(text("select current_schema()")).scalar_one()
        assert schema.startswith("test_"), f"refusing to truncate live schema {schema!r}"
        session.execute(text(f"TRUNCATE {', '.join(ENGINE_TABLES)} RESTART IDENTITY CASCADE"))
        session.commit()
    run_store = RunStore(session_factory)
    try:
        yield run_store
    finally:
        # Several tests here end on purpose with a run still queued or paused — that is the
        # assertion. Truncating at setup keeps the *next* engine test clean, but the run
        # left by the last one outlives this module and would hold the demo lane against
        # every launch the supervisor suite makes. The lane constraint is right; leaving it
        # held is the bug.
        release_lanes(run_store)


def make_run(store: RunStore, **config_overrides: Any) -> UUID:
    """A launch-shaped run: model table already resolved into the immutable config."""
    config = {
        "rounds": 2,
        "generation_batch": 6,
        "matches_per_round": 4,
        "evolve_top_k": 2,
        "budget_calls": 200,
        "budget_usd": 100.0,
        "grounding_depth": "standard",
        "graft": {"enabled": False},
        "model_tier": "high",
        "runner": "demo",
    }
    config.update(config_overrides)
    config["model_table"] = resolve_model_table(
        str(config.get("provider") or "anthropic"),
        str(config["model_tier"]),
        overrides=config.get("model_overrides"),
    )
    return store.create_run(
        question=GOAL,
        prompt=GOAL,
        config=config,
        harness="demo",
        root_path="engines/runs/run-test",
    )


class HookedRunner:
    """A `FakeRunner` with a hook before each call and optional payload overrides.

    The hook is how a test plays the part of the outside world — the scientist pressing
    pause mid-round, or the supervisor being killed — at an exact point in the loop.
    """

    name = "hooked"

    def __init__(
        self,
        inner: FakeRunner,
        *,
        before: Callable[[str, str, RoleConfig], Any] | None = None,
        override: dict[str, Callable[[str], dict[str, Any]]] | None = None,
    ) -> None:
        self.inner = inner
        self.before = before
        self.override = override or {}

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.inner.calls

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        if self.before is not None:
            outcome = self.before(role, prompt, cfg)
            if outcome is not None:
                await outcome
        result = await self.inner.run_role(role, prompt, cfg)
        if result.ok and role in self.override:
            return replace(result, data=self.override[role](prompt))
        return result

    async def probe(self) -> dict[str, Any]:
        return await self.inner.probe()

    async def aclose(self) -> None:
        await self.inner.aclose()


def events_of(store: RunStore, run_id: UUID, *types: str) -> list[dict[str, Any]]:
    """Every event of the given types, in seq order (not just the recent window)."""
    factory = store._session_factory  # noqa: SLF001 - tests read what the API paginates
    with factory() as session:
        rows = session.execute(
            text(
                "select seq, round, type, payload from run_events "
                "where run_id = :run_id order by seq"
            ),
            {"run_id": str(run_id)},
        ).mappings()
        return [dict(row) for row in rows if not types or row["type"] in types]


def ledger_of(store: RunStore, run_id: UUID) -> list[dict[str, Any]]:
    factory = store._session_factory  # noqa: SLF001
    with factory() as session:
        rows = session.execute(
            text(
                "select role, model, round, tokens_in, tokens_out, cache_read, cost_usd, status "
                "from budget_ledger where run_id = :run_id order by ts, id"
            ),
            {"run_id": str(run_id)},
        ).mappings()
        return [dict(row) for row in rows]

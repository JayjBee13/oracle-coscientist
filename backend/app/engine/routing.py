"""Per-call dispatch to the runner that can execute a row's model.

A run's model table may name models from both providers — the top bar's provider button is a
quick-set, not a lock, and mixing is deliberate: a scientist may want Fable judging a
tournament whose clustering runs on Luna. So "which CLI runs this call" is a property of the
row, not of the run, and it is decided here rather than by the orchestrator, which has no
business knowing that CLIs exist.

`ProviderRouter` is an `AgentRunner` that owns other `AgentRunner`s. The orchestrator's call
site is unchanged; what changes is that `run_role` first asks `models.provider_of(cfg.model)`
which lane the row belongs to.

**Runners are built lazily, one per lane, on the first call that needs one.** Constructing a
`CodexCliRunner` resolves `codex.exe` from the filesystem, so an eager router would make an
all-Anthropic run fail at startup on a box with no Codex installed — and it would fail with
a resolution error rather than anything about the run. A lane that is never used is never
constructed and never resolved.

**The run's `harness` column stays single-valued**, and it is the *default* provider's lane.
That is the plan's decision and it is a lane-lock decision, not a description: the harness
column is what stops two runs contending for one CLI, and a mixed run genuinely holds its
default lane. Solo-operator semantics; revisit if a second operator ever appears.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from app.engine.models import (
    DEFAULT_PROVIDER,
    PROVIDER_HARNESS,
    ModelPolicyError,
    normalise_provider,
    provider_of,
)
from app.engine.runners import AgentRunner, RoleConfig, RoleResult

__all__ = ["ProviderRouter"]

log = logging.getLogger(__name__)


class ProviderRouter:
    """`AgentRunner` that picks a per-provider runner from the model each call names."""

    def __init__(
        self,
        factories: Mapping[str, Callable[[], AgentRunner]],
        *,
        default_provider: str = DEFAULT_PROVIDER,
    ) -> None:
        self._factories = {
            normalise_provider(provider): factory for provider, factory in factories.items()
        }
        self._default = normalise_provider(default_provider)
        self._runners: dict[str, AgentRunner] = {}
        self.name = PROVIDER_HARNESS[self._default]

    # --- protocol ---------------------------------------------------------------------

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        return await self._runner(provider_of(cfg.model)).run_role(role, prompt, cfg)

    async def probe(self) -> dict[str, Any]:
        """The default lane's health. `GET /health` probes each CLI separately."""
        return await self._runner(self._default).probe()

    async def aclose(self) -> None:
        """Close every lane that was actually opened. One failure must not skip the rest."""
        for provider, runner in list(self._runners.items()):
            try:
                await runner.aclose()
            except Exception:  # noqa: BLE001 — closing one lane cannot mask another
                log.exception("closing the %s runner failed", provider)
        self._runners.clear()

    # --- lanes ------------------------------------------------------------------------

    def _runner(self, provider: str) -> AgentRunner:
        lane = normalise_provider(provider)
        existing = self._runners.get(lane)
        if existing is not None:
            return existing
        factory = self._factories.get(lane)
        if factory is None:
            # A model whose provider has no runner is a configuration this build cannot
            # execute. Refusing by name beats a `NoneType has no run_role` three frames down.
            raise ModelPolicyError(
                f"no runner is configured for provider {lane!r}; this run has "
                f"{', '.join(sorted(self._factories)) or 'none'}"
            )
        runner = factory()
        self._runners[lane] = runner
        log.info("opened the %s lane (%s)", lane, getattr(runner, "name", type(runner).__name__))
        return runner

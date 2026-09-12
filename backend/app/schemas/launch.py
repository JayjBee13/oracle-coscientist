"""Request bodies for launching and controlling a run (plan C5).

Kept apart from `schemas/run.py`, which describes the pre-overhaul API and goes away with
the legacy tables. Validation here is deliberately thin — bounds a person could plausibly
type wrong, and nothing else — because the launcher normalises the config through the
engine's own `RunConfig` and is the single place that decides what a run's settings mean.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.engine.models import ALLOWED_MODELS, EFFORTS
from app.engine.runners import LEGACY_MODEL_TIERS, ROLES
from app.engine.store import UNCHANGED, Unchanged
from app.schemas.dto import (
    ControlAction,
    GroundingDepth,
    Lifecycle,
    ModelTier,
    Provider,
    RunnerKind,
)

__all__ = [
    "AcceptedResponse",
    "ContextDocIn",
    "CreateRunRequest",
    "GraftConfigIn",
    "ModelOverrideIn",
    "NoteRequest",
    "PatchRunRequest",
    "RunConfigIn",
    "RunControlRequest",
    "RunControlResponse",
]

class GraftConfigIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    quorum_k: Annotated[int, Field(ge=1, le=3)] = 3
    window: Annotated[int, Field(ge=1, le=10)] = 3
    cooldown: Annotated[int, Field(ge=0, le=10)] = 2


class ModelOverrideIn(BaseModel):
    """One role's model and/or effort, replacing what the tier would have given it.

    Both fields are optional and independent: naming only an effort keeps the tier's model
    and vice versa. The literal sets are the allowlist itself rather than a copy of it, so
    a model this engine refuses is a 422 at the edge and never reaches the launcher — and
    `claude-fable-5`, which the CLI silently downgrades to Opus 5, is refused here by not
    being in the set (the launcher's error explains why if it arrives another way).

    A row may name **either provider's** model regardless of the run's `provider`: mixing
    providers within one run is the point of the field, not an accident of it.

    `effort` is checked here only against the engine's whole vocabulary. Whether a given
    level exists on the chosen *model* is the model's own business — every model publishes
    its own ladder in `/api/capabilities`, and a level outside it is clamped, visibly, in
    the resolved table. **A client offering an effort picker must read the ladder off the
    selected model, not off its provider.**
    """

    model_config = ConfigDict(extra="forbid")

    model: Literal[ALLOWED_MODELS] | None = None  # type: ignore[valid-type]
    effort: Literal[EFFORTS] | None = None  # type: ignore[valid-type]


class RunConfigIn(BaseModel):
    """Plan C5's config block. `model_table` is not accepted: the launcher resolves it."""

    model_config = ConfigDict(extra="ignore")

    rounds: Annotated[int, Field(ge=1, le=25)] = 5
    workflow: Literal["adaptive", "tournament"] = "adaptive"
    generation_batch: Annotated[int, Field(ge=1, le=40)] = 8
    matches_per_round: Annotated[int, Field(ge=0, le=100)] = 6
    evolve_top_k: Annotated[int, Field(ge=0, le=25)] = 3
    budget_calls: Annotated[int, Field(gt=0, le=10_000)] = 150
    """The governor. Always set, and the ceiling that actually stops a run."""

    budget_usd: Annotated[float | None, Field(gt=0, le=1_000)] = None
    """Optional dollar ceiling, off by default.

    Role calls go through the Claude CLI on a subscription, so nothing is billed per token
    and `total_cost_usd` is API-equivalent telemetry rather than money. Enforcing a dollar
    figure against that only ever produced one outcome in practice: a run stopped one step
    short of the report it had already done the work for. Still honoured when set, for a
    metered API key."""

    wall_clock_minutes: Annotated[float | None, Field(gt=0, le=1_440)] = None
    """Optional elapsed-time ceiling, off by default.

    The guard against a pathological loop now that dollars are not one. Passing it ends the
    run the way the `finish` control action does — the overview is written first."""

    grounding_depth: GroundingDepth = GroundingDepth.STANDARD
    graft: GraftConfigIn = Field(default_factory=GraftConfigIn)

    provider: Provider | None = None
    """Which provider's models fill the rows this request does not override.

    `null` (the default) means "whatever the system default says", which is what the top-bar
    editor writes and what the wizard inherits. Stated explicitly, it wins for this run
    alone and changes nothing about the default."""

    model_tier: ModelTier | None = None
    """Thinking effort preset. `null` inherits the system default, like `provider`."""

    model_overrides: dict[Literal[ROLES], ModelOverrideIn] | None = None  # type: ignore[valid-type]
    """Per-role choices merged over the tier's table at launch. An unknown role name is a
    422 rather than a silently ignored key: a typo'd role is a request that did not do what
    the caller asked, which is the failure the model policy exists to prevent.

    `null` inherits the system default's overrides; `{}` is a request for *no* overrides and
    is honoured as one."""

    runner: RunnerKind = RunnerKind.CLAUDE

    @field_validator("model_tier", mode="before")
    @classmethod
    def _accept_legacy_tier(cls, value: Any) -> Any:
        """Keep `balanced`/`quality` working on the wire.

        Stored configs are read through `RunConfig.from_mapping`, which maps them — but a
        clone posts the inherited config back through this model first, and a 422 there
        would make the four pre-floor runs un-cloneable.
        """
        if isinstance(value, str):
            return LEGACY_MODEL_TIERS.get(value, value)
        return value


class ContextDocIn(BaseModel):
    """A document inlined into the prompts. Bytes only — no path ever reaches the CLI."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(max_length=200_000)]


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str | None = None
    prompt: str | None = None
    title: Annotated[str | None, Field(max_length=200)] = None
    harness: Literal["claude", "codex", "demo"] = "claude"
    """The lane this run occupies. `demo` is honoured as asked; for a real run the lane
    follows the config's provider (anthropic → claude, openai → codex), because the lane
    exists to stop two runs contending for one CLI and the provider is what decides which
    CLI a run mostly drives."""
    from_run: str | None = None
    """Uuid of a run to copy question, prompt, config and context documents from.

    A copy of the *setup* into a brand new run, which starts with no hypotheses, no ratings
    and no meta-review guidance — the way to ask the same question again independently. To
    carry on with the run itself, keeping its whole idea pool, send
    `POST /runs/{id}/controls {action: "continue", add_rounds: n}` instead."""

    context_docs: Annotated[list[ContextDocIn], Field(max_length=5)] = []
    config: RunConfigIn = Field(default_factory=RunConfigIn)

    def config_document(self) -> dict[str, Any]:
        """The config **as the caller stated it** — the fields they actually sent, no more.

        `mode="json"` because this lands in a JSONB column: the enum members become
        the plain strings the database and every reader of `config` expect.

        `exclude_unset` is what makes `from_run` mean "run that again". Every field on
        `RunConfigIn` has a default and `config` itself has a `default_factory`, so a dump
        of all of them cannot be told apart from a request that stated all of them — and
        `launcher._inherit`, which merges the caller's keys over the source run's stored
        config, would then write this model's defaults (`rounds: 5`) and its "say nothing"
        `None`s over everything the source run chose. A clone would land on the *current*
        system default instead of reproducing a setup, which is the one thing it exists to
        do. Dumping only what was sent keeps the two apart: an omitted field inherits, a
        stated one overrides, key by key.

        Nothing downstream wants a fully-populated document. `launcher.resolve_config` is
        the single place that decides what a run's settings mean: it fills every missing
        field from `RunConfig`'s own defaults (and the three model fields from the stored
        system default), so `{}` normalises to exactly the same stored config a dump of the
        defaults did.
        """
        return self.config.model_dump(mode="json", exclude_unset=True)

    def docs(self) -> list[dict[str, str]]:
        return [doc.model_dump() for doc in self.context_docs]


class RunControlRequest(BaseModel):
    """`POST /runs/{id}/controls`. One action, plus the three fields `continue` needs."""

    model_config = ConfigDict(extra="forbid")

    action: ControlAction

    add_rounds: Annotated[int | None, Field(ge=1, le=25)] = None
    """`continue` only: how many more rounds to run, counted from the ones already done.

    An increment rather than a new target, because that is the question a scientist is
    actually answering — "three more rounds", not "recompute what the total should now be"
    for a run that may have stopped short of the target it was launched with."""

    budget_calls: Annotated[int | None, Field(gt=0, le=10_000)] = None
    """`continue` only: raise the run's call ceiling to this. Absent leaves it alone.

    Absolute rather than an increment, because it is the same number the launch wizard set
    and reads the same way on both screens. It may only ever go up."""

    budget_usd: Annotated[float | None, Field(gt=0, le=1_000)] = None
    """`continue` only, and the one field here with three states rather than two.

    Absent leaves the run's cost ceiling as it is; a number raises it; an explicit `null`
    removes it, which is the state every run launched since the ceiling became optional is
    already in. Removal is a first-class answer rather than a lowering to zero, because
    these calls go through the Claude CLI on a subscription: the figure is API-equivalent
    telemetry, not money, and a run that stopped on an old default deserves the ceiling
    gone rather than nudged. Absent and `null` are told apart by `model_fields_set`, which
    is why `cost_ceiling` below exists and this attribute is not read directly."""

    @property
    def cost_ceiling(self) -> float | None | Unchanged:
        """What the request asks of the run's dollar ceiling, in the store's own vocabulary.

        `None` on this field is a value — "no ceiling" — so the default cannot also be
        `None`. JSON has no way to say "I did not mention this" other than not mentioning
        it, and Pydantic records exactly that in `model_fields_set`; translating it here
        keeps the three states in the layer that can still see the difference.
        """
        if "budget_usd" not in self.model_fields_set:
            return UNCHANGED
        return self.budget_usd

    @model_validator(mode="after")
    def _extension_belongs_to_continue(self) -> RunControlRequest:
        """All three extension fields are meaningless on any other action, so they are
        refused.

        A `finish` carrying a raised `budget_calls` is a request that did not do what it
        said; a 422 naming the field is a better answer than a 200 that quietly ignored it.
        The dollar ceiling is checked on whether it was *mentioned* rather than on whether
        it is `None`, because on this field `null` is a request in its own right.
        """
        if self.action is ControlAction.CONTINUE:
            if self.add_rounds is None:
                raise ValueError(
                    "continue needs add_rounds: how many more rounds to run"
                )
        elif (
            self.add_rounds is not None
            or self.budget_calls is not None
            or "budget_usd" in self.model_fields_set
        ):
            raise ValueError(
                f"{self.action.value} takes no add_rounds, budget_calls or budget_usd; "
                "only continue extends a run"
            )
        return self


class RunControlResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    lifecycle: Lifecycle


class PatchRunRequest(BaseModel):
    """`PATCH /runs/{id}`: the only two fields a scientist may change after launch.

    Everything else — question, prompt, config — is immutable once a run exists;
    renaming or archiving it is bookkeeping, not a change to what the run did. A field left
    out of the body is left alone, not cleared.
    """

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    archived: bool | None = None


class NoteRequest(BaseModel):
    """`POST /runs/{id}/note`. Blank text is refused here rather than queued and ignored."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=4000)]

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class AcceptedResponse(BaseModel):
    """`202` body for the two fire-and-forget writes: queued, not yet applied."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool = True

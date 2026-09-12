"""How the engine calls a model, and a fake that behaves like one.

The orchestrator never knows what a role call *is*. It builds a `RoleConfig`, hands it and
a prompt to an `AgentRunner`, and gets a `RoleResult` back. Two implementations exist:
`ClaudeCliRunner` (Wave 2.1 — spawns `claude -p` per call) and `FakeRunner` below, which
every test and every demo run uses. Because the protocol is narrow, no test in this
repository needs a real model, and no code path is exercised only by the fake.

`RoleConfig` is also the contract for plan C1's argv builder — `model`, `effort`, `tools`,
`system_prompt` and `json_schema` map one-to-one onto CLI flags, and `tools` is used for
both `--tools` and `--allowedTools` (availability is not permission; a grounded call
without the second flag is denied after burning the prompt).

This module additionally owns the role table: which model, effort, tools and timeout each
role gets. The launcher resolves it once into the run's immutable config so a run's models
cannot change under it mid-flight, and the orchestrator escalates effort within the rules
C1 fixes (round-1 generation, and ranking when the match matters). Which models may appear
in that table at all is `app/engine/models.py`'s business, not this module's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from app.engine.models import (
    EFFORTS,
    MODEL_CLASSES,
    PROVIDER_HARNESS,
    PROVIDERS,
    ModelPolicyError,
    model_for,
    normalise_provider,
    provider_of,
    resolve_effort,
    supported_efforts,
    validate_effort,
    validate_model,
)
from app.engine.schemas import SCHEMA_BY_ROLE, schema_for

__all__ = [
    "BASELINE_MODEL_TABLE",
    "DEFAULT_MODEL_TIER",
    "EFFORTS",
    "GROUNDED_ROLES",
    "LEGACY_MODEL_TIERS",
    "MODEL_ESCALATION_NOTES",
    "MODEL_TIERS",
    "PINNED_LOW_ROLES",
    "ROLES",
    "ROLE_CLASSES",
    "ROLE_NOTES",
    "TIER_EFFORTS",
    "TIER_LABELS",
    "TIER_MODELS",
    "TIER_NOTES",
    "TIMEOUT_BATCH_GROUNDED",
    "TIMEOUT_GROUNDED",
    "TIMEOUT_TOOLLESS",
    "TIMEOUT_WARN_FRACTION",
    "TOOLS_WEB",
    "AgentRunner",
    "FakeRunner",
    "Failure",
    "RoleConfig",
    "RoleResult",
    "Usage",
    "normalise_tier",
    "resolve_model_table",
    "role_catalog",
    "role_config",
    "tier_catalog",
    "tier_effort",
    "tier_model",
    "timeout_for",
    "tools_for",
]

ROLES: tuple[str, ...] = (
    "generation",
    "reflection",
    "proximity",
    "ranking",
    "evolution",
    "meta_review",
    "cartographer",
    "overview",
    "workshop",
    "framing",
    "verification",
    "synthesis",
    "challenge",
)

TOOLS_WEB = "WebSearch"
"""The only tool any role ever gets. Never WebFetch: it reaches loopback and the LAN."""

GROUNDED_ROLES: frozenset[str] = frozenset(
    {
        "generation",
        "reflection",
        "evolution",
        "workshop",
        "verification",
        "challenge",
    }
)

BATCH_ROLES: frozenset[str] = frozenset({"generation", "evolution", "workshop"})
"""Grounded roles asked for *several* hypotheses in one call.

Reflection is grounded too, but it critiques one hypothesis and stops; these three search,
then compose two to eight complete hypotheses. Measured on the same model that is the
default here, the two classes differ by an order of magnitude, so they cannot share a
ceiling — which is exactly what run c4566ed2 proved by killing every call of this class.
"""

TIMEOUT_TOOLLESS = 180.0
"""Tool-less roles. Max observed across the whole corpus is 95.3s (overview) — 53% of
this — and no tool-less call has ever exceeded it for a reason other than the reader bug
fixed in `claude_runner._consume`."""

TIMEOUT_GROUNDED = 420.0
"""Reflection: one hypothesis, one critique, web search on. opus-5/high measures p50 75.7s,
p90 99.7s, max 107.0s — 25% of this ceiling. Left where it was because the evidence says it
was never the problem."""

TIMEOUT_BATCH_GROUNDED = 1200.0
"""Generation, evolution and workshop at the Opus floor, high effort, standard grounding.

Set from a censored sample, and deliberately generous. Run c4566ed2 (opus-5 / high /
standard) made 10 calls of this class: 2 completed, at 388.4s and 407.2s — 92% and 97% of
the old 420s ceiling — and 8 were killed at the ceiling. With 80% of the sample censored
the true median is provably above 420s and has no observable upper bound, so this number
is a lower bound on what is needed rather than a measurement of what is enough.

Being generous is close to free now that a stalled reader can no longer consume the whole
ceiling (`claude_runner._consume` races the stdout reader against the child's exit) and a
finished answer is salvaged from the transcript rather than discarded. What a ceiling costs
is only paid when a call is genuinely stuck. Re-measure once calls stop being censored.
"""

TIMEOUT_WARN_FRACTION = 0.8
"""A call past this fraction of its ceiling is reported as `near_timeout` on `call_finished`.

Both of c4566ed2's surviving generation calls were over 0.9× and nothing said so; the run
read as healthy right up until the same calls started dying."""

# Effort buys thinking with latency, so it moves the ceiling. Grounding depth moves it for
# the same reason: `deep` instructs the role to verify every major claim independently, and
# each verification is a search plus the reading of its results.
_EFFORT_TIMEOUT_FACTOR: dict[str, float] = {
    "low": 0.5,
    "medium": 0.75,
    "high": 1.0,
    "xhigh": 1.35,
    "max": 1.75,
}
_GROUNDING_TIMEOUT_FACTOR: dict[str, float] = {
    "shallow": 0.6,
    "standard": 1.0,
    "deep": 1.3,
}

# The model/tier contract, in three tables and one rule.
#
# Model ids are pinned — there is deliberately no fallback model, because a silent downgrade
# mid-tournament poisons Elo with results from a different judge.
#
# Effort was once the cost dial. It is not one any more: these are headless CLI calls on a
# subscription, so effort buys thinking with *latency* rather than with money. That is why
# the two dials are separated the way they are — how hard a step is fixes its model class
# (`ROLE_CLASSES`), and the tier chooses how long the run is willing to think
# (`TIER_EFFORTS`). One tier, `low`, additionally names the model itself (`TIER_MODELS`):
# the speed preset, where the point is the model rather than the effort. It is the single
# exception, it is data, and it is published — see `MODEL_TIERS`.
ROLE_CLASSES: dict[str, str] = {
    "framing": "heavy",
    "verification": "heavy",
    "synthesis": "heavy",
    "challenge": "heavy",
    "generation": "heavy",
    "reflection": "heavy",
    "ranking": "heavy",
    "meta_review": "heavy",
    "overview": "heavy",
    "proximity": "light",
    "evolution": "light",
    "cartographer": "light",
    "workshop": "light",
}
"""Which class of model each role runs on. **Constant across every tier and provider.**

This is the half of the contract a tier cannot touch. A step is heavy because its judgement
compounds into everything after it — the hypotheses a run can consider at all, the gate on
what reaches the tournament, the Elo table, the guidance the next round is generated
against, and the document a person actually reads. A step is light because it is bounded
work whose output the next step re-derives anyway: clustering titles, mutating a survivor,
grafting a structure, shaping a question a human then edits.

A tier moves effort, and — for `low` alone — pins the model as well (`TIER_MODELS`). What
this table still fixes is the half that matters: a step's *class* never moves, so no tier
can hand a heavy step the light model of the provider it is already on. `low` does not do
that either. It pins **every** class to one model, `gpt-5.6-luna`, which is a whole tier
running on the other provider's fast model — a decision, not a substitution, and one the
owner made deliberately on 2026-08-17 to have a speed preset.

The rule `engine/models.py` exists to enforce is unchanged, because what it refuses is the
downgrade *nobody can see*: `TIER_MODELS` is data, `tier_catalog()` publishes each tier's
pin and the harness it needs, and the resolved table carries the model on every row. A Low
run says on screen that it is running Luna."""

# The classes must cover the roles exactly. Asserted rather than tested because a role added
# without a class would otherwise resolve to a KeyError at launch, on a run someone is
# waiting for, rather than at import.
assert set(ROLE_CLASSES) == set(ROLES), "every role needs a model class, and only a role"
assert set(ROLE_CLASSES.values()) <= set(MODEL_CLASSES)

MODEL_TIERS: tuple[str, ...] = ("max", "high", "med", "low")
"""The four presets, strongest first.

Three of them are a pair of efforts and nothing else. `low` is the exception, added on the
owner's instruction on 2026-08-17: it is the *speed* preset, and it pins every role to
`gpt-5.6-luna` at high effort rather than only lowering the effort of the models the
provider would otherwise pick.

That relaxes what this line used to promise — "a tier is a pair of efforts, not a pair of
models" — and the reason behind the promise is kept rather than dropped. The thing being
refused was never a tier naming a model; it was a tier naming one *invisibly*. So the pin is
data (`TIER_MODELS`), it is published per tier by `tier_catalog()` alongside the harness it
requires, and it appears on every row of the resolved table the UI already renders."""

TIER_LABELS: dict[str, str] = {"max": "Max", "high": "High", "med": "Med", "low": "Low"}

DEFAULT_MODEL_TIER = "max"

TIER_EFFORTS: dict[str, dict[str, str]] = {
    "max": {"heavy": "xhigh", "light": "high"},
    "high": {"heavy": "high", "light": "medium"},
    "med": {"heavy": "low", "light": "low"},
    # Low is not "med with a cheaper model": it is a fast model asked to think properly.
    # Luna answers quickly enough that high effort costs a Low run less wall clock than
    # `med` costs on a frontier model, which is the whole reason the tier exists.
    "low": {"heavy": "high", "light": "high"},
}
"""Tier → effort, per model class. What every tier changes; see `TIER_MODELS` for the one
tier that also changes which model a class runs on."""

TIER_MODELS: dict[str, dict[str, str]] = {
    "low": {"heavy": "gpt-5.6-luna", "light": "gpt-5.6-luna"},
}
"""Tier → model class → the model that tier pins, for the tiers that pin one at all.

Sparse on purpose: a tier absent from here leaves the model to the provider, which is what
`max`, `high` and `med` all do, and a class absent from a tier's entry is left the same way.

`low` pins both classes to Luna — OpenAI's fast model — so a Low run is fast whatever the
run's `provider` says. That is cross-provider by design and not a special case in the
engine: `engine/routing.ProviderRouter` picks the runner per call from the row's own model,
so a Low table stored under `provider="anthropic"` executes entirely on the Codex CLI. It
is also why Low **requires the codex CLI installed and authenticated**, which
`tier_catalog()` states and `/api/capabilities` serves so the UI can warn beforehand rather
than let a run discover it mid-flight."""

PINNED_LOW_ROLES: frozenset[str] = frozenset({"proximity"})
"""Roles held at `low` in every tier, because effort buys them nothing.

Clustering reads a list of titles and labels them. It is mechanical, it is on the critical
path of every round, and minutes spent thinking about it are minutes the run is not
generating. Pinning it is not a saving — these are subscription calls — it is a refusal to
spend the one resource that is actually scarce on the one step that cannot use it.

This holds in the `low` tier too, where every other role runs at high effort. Thinking buys
clustering nothing there either, and a tier whose entire purpose is speed is the last place
to spend minutes on the one step that cannot use them."""

TIER_NOTES: dict[str, str] = {
    "max": (
        "Everything thinks at the top of its ladder: the judgement-heavy steps at extra "
        "high effort, the mechanical ones at high. The strongest run, and the slowest."
    ),
    "high": (
        "The default shape of a run: heavy steps at high effort, light steps at medium. "
        "Strong where judgement compounds and quick everywhere else."
    ),
    "med": (
        "Every step at low effort, on exactly the models the stronger tiers use. A run "
        "that thinks less — never a run that thinks with something weaker."
    ),
    "low": (
        "The speed preset. Every step runs Luna, OpenAI's fast model, at high effort "
        "whatever the run's provider says — so this is the one tier that changes which "
        "model a step runs on, and it needs the codex CLI installed and signed in."
    ),
}
"""Per tier, what choosing it does, in the words the control that selects it should use.

Data rather than a comment for the same reason `ROLE_NOTES` is: the UI has to say this next
to the segment a person clicks, and a UI restating engine reasoning in its own words gets it
wrong the first time the reasoning changes. `low` naming its model here is load-bearing — it
is half of how the model pin stays visible."""

# Every tier needs an effort pair, a label and a note; a tier is not allowed to exist in any
# one of those tables alone. Asserted rather than tested because a tier added to `MODEL_TIERS`
# without an effort pair resolves to a KeyError at launch, on a run someone is waiting for.
assert set(TIER_EFFORTS) == set(MODEL_TIERS), "every tier needs an effort pair, and only a tier"
assert set(TIER_LABELS) == set(MODEL_TIERS), "every tier needs a label the UI can print"
assert set(TIER_NOTES) == set(MODEL_TIERS), "every tier needs a note the UI can print"
# The model pin is the sparse one: most tiers pin nothing, and that is the normal case.
assert set(TIER_MODELS) <= set(MODEL_TIERS)
assert all(set(pinned) <= set(MODEL_CLASSES) for pinned in TIER_MODELS.values())

# What the tiers were called before this table existed. Stored run configs still say these,
# and they must keep resolving rather than 500 on read — a run's own frozen `model_table` is
# what it actually used and is never rewritten, but a "run again" clone and a `continue`
# re-resolve from the tier name and would otherwise refuse to launch.
#
# The mapping preserves *rank*, not the exact efforts: `maximum`/`quality` were the strongest
# tier on offer at the time, so they become `max`; `standard`/`balanced` were the default,
# and `high` is the default-shaped tier now. Nothing about the runs that used those names
# changes — only what a clone of one resolves to today.
LEGACY_MODEL_TIERS: dict[str, str] = {
    "balanced": "high",
    "standard": "high",
    "quality": "max",
    "maximum": "max",
}

# The two escalations the orchestrator applies on top of the table. They are decisions
# made per call from run state (round number, standings), so no table can express them —
# they live here beside the table they modify, and `/api/capabilities` serves them verbatim
# so the UI never has to restate them in its own words and get them wrong.
MODEL_ESCALATION_NOTES: tuple[str, ...] = (
    "A tier sets thinking effort. Which model a step runs on is fixed by how hard the step "
    "is — the same in every tier except Low, which pins every step to Luna for speed.",
    "Clustering is pinned to low effort in every tier: it labels a list of titles, and "
    "thinking longer about that buys nothing.",
    "Generation runs at high effort in round 1, then at the effort listed here.",
    "The tournament runs at high effort when both ideas are in the top 5, "
    "and for every match in the final round.",
    "Both escalations are floors, not overrides: a role already at high or above is "
    "left where it is.",
)


# Why each role is set the way it is, in the words the wizard's per-role picker should use.
# Data rather than a comment because the UI needs to say this next to each row, and a UI
# that restates engine reasoning in its own words gets it wrong the first time the reasoning
# changes.
ROLE_NOTES: dict[str, str] = {
    "framing": "Preserves the goal while creating alternative framings and a dependency map.",
    "verification": "Independently checks decisive claims; records uncertainty and evidence.",
    "synthesis": "Integrates a diverse portfolio into a complete answer with explicit gaps.",
    "challenge": "Tests the complete answer against the objective and reopens decisive weaknesses.",
    "generation": (
        "Proposes the hypotheses. Round 1 seeds every later round, so it escalates to high "
        "effort there; a stronger model here widens what the run is able to consider at all."
    ),
    "reflection": (
        "The only gate on whether an idea reaches the tournament, and a wrong reject is "
        "unrecoverable — nothing downstream reconsiders it. Runs at high effort."
    ),
    "proximity": (
        "Clusters near-duplicate ideas. Mechanical work: it reads a list of titles and "
        "labels them, so effort and model buy the least here of any role."
    ),
    "ranking": (
        "Judges the tournament. Its verdicts compound into every Elo rating, and it "
        "escalates to high effort when both sides are top-5 or the round is the last."
    ),
    "evolution": "Mutates the surviving ideas. Bounded by what generation and ranking gave it.",
    "meta_review": (
        "Reads the round's reviews and writes the guidance the next round is generated "
        "against. One call per round, felt in every call of the next one."
    ),
    "cartographer": (
        "Fires only when the pool collapses, to graft in a structure from a distant domain. "
        "Rare enough that its cost never shows up in a budget."
    ),
    "overview": (
        "Writes the report — the artefact a person actually reads. Runs at high effort, and "
        "its two calls are reserved up front so a stopped run still produces one."
    ),
    "workshop": (
        "Turns a rough question into two research goals before a run exists. Its output is "
        "read and edited by the scientist, so a mistake here is cheap to catch."
    ),
}


def role_catalog(
    provider: str = "anthropic",
    tier: str = DEFAULT_MODEL_TIER,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The resolved table plus the reason each row is what it is, ready to serve.

    What `/api/capabilities`, the top-bar editor and the wizard's per-role picker read: one
    row per role carrying the model and effort it would actually run at, the note explaining
    why, and whether the call gets web search. Pairs with `models.model_catalog()`, which
    says what may be chosen instead — including, per model, the effort ladder that model
    actually has.
    """
    return [
        {
            **row,
            "note": ROLE_NOTES.get(row["role"], ""),
            "grounded": row["role"] in GROUNDED_ROLES,
            "model_class": ROLE_CLASSES[row["role"]],
        }
        for row in resolve_model_table(provider, tier, overrides=overrides)
    ]


def normalise_tier(tier: str | None) -> str:
    """Map a stored tier name onto a current one. Raises for anything not a tier at all."""
    name = (tier or DEFAULT_MODEL_TIER).strip()
    name = LEGACY_MODEL_TIERS.get(name, name)
    if name not in MODEL_TIERS:
        raise ModelPolicyError(
            f"unknown model tier {tier!r}; expected one of {', '.join(MODEL_TIERS)}"
        )
    return name


def tier_effort(tier: str, role: str) -> str:
    """The effort a tier gives a role, before any override or escalation.

    One function so the table, the capabilities payload and the tests cannot disagree about
    where the pinned-low rule lives.
    """
    resolved = normalise_tier(tier)
    if role in PINNED_LOW_ROLES:
        return "low"
    return TIER_EFFORTS[resolved][ROLE_CLASSES[role]]


def tier_model(tier: str, role: str) -> str | None:
    """The model a tier pins for this role's class, or `None` when it pins nothing.

    `None` is the ordinary answer and means "leave the model to the provider" — which is
    what `max`, `high` and `med` say for every role. Only `low` answers with a model, and it
    answers with the same one for both classes.

    A sibling of `tier_effort` so the two halves of a tier are read the same way, and so
    `resolve_model_table`, the import-time check and `tier_catalog` cannot disagree about
    where the pin lives.
    """
    return TIER_MODELS.get(normalise_tier(tier), {}).get(ROLE_CLASSES[role])


def tier_catalog() -> list[dict[str, Any]]:
    """One row per tier: its label, what choosing it does, and what it needs installed.

    Served by `/api/capabilities` so the control that selects a tier can name the tier's
    consequences without restating them. Two fields exist purely to keep the `low` tier's
    model pin visible, which is the condition on which that pin was allowed at all:

    * `pinned_models` — model class → the model this tier forces, empty for a tier that
      forces none. The reason a client can say "this tier runs Luna" rather than having to
      diff two resolved tables to notice.
    * `requires_harness` — the CLIs a run in this tier will drive *because of the pin*,
      regardless of its provider. `low` requires `codex`, so the UI can warn on an install
      that has no Codex instead of letting the first call fail.
    """
    rows: list[dict[str, Any]] = []
    for tier in MODEL_TIERS:
        pinned = dict(TIER_MODELS.get(tier, {}))
        rows.append(
            {
                "id": tier,
                "label": TIER_LABELS[tier],
                "note": TIER_NOTES[tier],
                "pinned_models": pinned,
                "requires_harness": sorted(
                    {PROVIDER_HARNESS[provider_of(model)] for model in pinned.values()}
                ),
            }
        )
    return rows


def resolve_model_table(
    provider: str = "anthropic",
    tier: str = DEFAULT_MODEL_TIER,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """The per-role model/effort table for a provider and tier, resolved at run creation.

    Stored in `runs.config['model_table']` so the Confirm step can print it, the Settings
    tab can show it, and a resumed run uses the models it started with.

    `provider` is a quick-set, not a lock: it decides the model for every row that has no
    override, and an override may name a model from the *other* provider freely — steps mix
    providers within one run by design. Which runner executes a row is decided per call from
    the row's model, not from this argument.

    Which model a row gets is decided in one precedence, highest first: **the role's own
    override, then the model the tier pins for that role's class, then the provider's model
    for that class.** Only `low` pins anything (`TIER_MODELS`), so for every other tier the
    middle step is absent and the provider decides — which is what it always did.

    `overrides` is the scientist's per-role choice, merged over the tier's row and validated
    against the allowlist here rather than at call time — a model the CLI would silently
    substitute must be refused before a run exists, not discovered from telemetry once it has
    been paid for. The effort is validated against **the chosen model's own ladder**, so an
    override that names a level a model does not have is clamped here, visibly, in the table
    the whole UI then shows — rather than 400-ing halfway through a paid run.
    """
    lane = normalise_provider(provider)
    resolved = normalise_tier(tier)
    chosen = _clean_overrides(overrides)

    table: list[dict[str, str]] = []
    for role in ROLES:
        override = chosen.get(role, {})
        model = validate_model(
            str(
                override.get("model")
                or tier_model(resolved, role)
                or model_for(lane, ROLE_CLASSES[role])
            )
        )
        effort = str(override.get("effort") or tier_effort(resolved, role))
        table.append({"role": role, "model": model, "effort": resolve_effort(model, effort).effort})
    return table


def _assert_every_tier_is_runnable() -> None:
    """No tier may ask a model for a rung its own ladder does not have.

    Checked at import, against every provider and every tier, because the alternative is
    finding out mid-run: the effort a tier assigns is sent verbatim to the CLI, and a level
    outside the model's ladder is a 400 on a call that has already been paid for. A model
    added with a shorter ladder than the `max` tier needs therefore fails here, loudly, at
    startup — not quietly as a clamped call three rounds in.

    The model checked is the one the tier **actually resolves to**, pin included, not the
    provider's model for the class. Those were the same thing until `low` started pinning
    Luna; checking the provider's model instead would have left a tier asking a pinned model
    for a rung its own ladder lacks to be discovered by a paid call.
    """
    for provider in PROVIDERS:
        for tier in MODEL_TIERS:
            for role in ROLES:
                model = tier_model(tier, role) or model_for(provider, ROLE_CLASSES[role])
                wanted = tier_effort(tier, role)
                ladder = supported_efforts(model)
                if wanted not in ladder:
                    raise ModelPolicyError(
                        f"tier {tier!r} asks {model!r} for {wanted!r} effort, which is not "
                        f"on its ladder ({', '.join(ladder)}); either the tier or the "
                        "catalog is wrong, and a run must not discover this mid-flight"
                    )


def _clean_overrides(
    overrides: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    """Refuse an override for a role that does not exist, rather than dropping it silently.

    A typo in a role name is a request that did not do what the caller meant; answering it
    with the default table would be the same failure this module exists to prevent, one
    layer up.
    """
    if not overrides:
        return {}
    unknown = sorted(set(overrides) - set(ROLES))
    if unknown:
        raise ModelPolicyError(
            f"unknown role(s) in model_overrides: {', '.join(unknown)}; "
            f"expected any of {', '.join(ROLES)}"
        )
    return {role: dict(value or {}) for role, value in overrides.items()}


_assert_every_tier_is_runnable()


BASELINE_MODEL_TABLE: tuple[dict[str, str], ...] = tuple(resolve_model_table())
"""The default table — Anthropic at the `max` tier — as a constant, for readers who want
one without resolving it. Derived rather than typed out: a second copy of this table is how
the engine and the UI come to disagree about what a run would do."""


def tools_for(role: str) -> str:
    """`--tools` / `--allowedTools` value for a role: web search, or nothing at all."""
    return TOOLS_WEB if role in GROUNDED_ROLES else ""


def timeout_for(role: str, *, effort: str = "high", grounding_depth: str = "standard") -> float:
    """The wall-clock ceiling for one call of this role, on these terms.

    A function of more than the role, because the same role at `low` effort with `shallow`
    grounding and at `max` effort with `deep` grounding are not the same call — and the
    single number this used to return was set from the cheaper of the two while the engine
    ran the more expensive one.

    Unknown efforts and depths resolve to 1.0 rather than raising: this is a ceiling, and a
    vocabulary that grows must not be able to fail a call.
    """
    if role not in GROUNDED_ROLES:
        return TIMEOUT_TOOLLESS
    base = TIMEOUT_BATCH_GROUNDED if role in BATCH_ROLES else TIMEOUT_GROUNDED
    factor = _EFFORT_TIMEOUT_FACTOR.get(effort, 1.0) * _GROUNDING_TIMEOUT_FACTOR.get(
        grounding_depth, 1.0
    )
    return round(base * factor, 1)


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """Everything a runner needs for one call. Maps onto C1's argv, flag for flag."""

    role: str
    model: str
    effort: str
    tools: str
    system_prompt: str
    json_schema: dict[str, Any] | None = None
    timeout_s: float = TIMEOUT_TOOLLESS
    round: int | None = None
    unit: str | None = None
    """What this call is about (`h003`, a match id) — for logs and failure reports only."""

    @property
    def grounded(self) -> bool:
        return bool(self.tools)

    def with_effort(self, effort: str) -> RoleConfig:
        return replace(self, effort=effort)

    def with_timeout(self, timeout_s: float) -> RoleConfig:
        """The same call on a longer clock. How a timeout is retried on different terms.

        Re-sending an identical request against an identical deadline cannot change the
        odds; it only spends the wall clock and the call budget twice. This is the one
        knob the retry is allowed to turn.
        """
        return replace(self, timeout_s=float(timeout_s))


def role_config(
    role: str,
    *,
    model: str,
    effort: str,
    system_prompt: str,
    round: int | None = None,
    unit: str | None = None,
    json_schema: dict[str, Any] | None = None,
    grounding_depth: str = "standard",
) -> RoleConfig:
    """Build a `RoleConfig`, filling tools, timeout and schema from the role itself."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {ROLES}")
    # The effort is checked here but the model deliberately is not: this is also the path a
    # *resumed* run takes, replaying the table it froze at launch, and a run must not become
    # unresumable because the allowlist moved under it. The allowlist is enforced where the
    # table is built (`resolve_model_table`) and again on what actually answered
    # (`claude_runner`'s substitution guard).
    validate_effort(effort)
    return RoleConfig(
        role=role,
        model=model,
        effort=effort,
        tools=tools_for(role),
        system_prompt=system_prompt,
        json_schema=json_schema if json_schema is not None else schema_for(role),
        timeout_s=timeout_for(role, effort=effort, grounding_depth=grounding_depth),
        round=round,
        unit=unit,
    )


@dataclass(frozen=True, slots=True)
class Usage:
    """What one call cost. All four token classes: input alone under-reports ~700×."""

    tokens_in: int = 0
    tokens_out: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out + self.cache_creation + self.cache_read


@dataclass(frozen=True, slots=True)
class RoleResult:
    """One call's outcome. `data` is the parsed structured output, never a JSON string."""

    role: str
    model: str
    data: dict[str, Any] | None = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    raw_tail: str = ""
    """Tail of the raw transcript, kept for the Activity tab when a call fails."""

    telemetry: dict[str, Any] = field(default_factory=dict)
    """What the call reported about itself: permission denials, searches, turns, exit.

    Separate from `raw_tail` because these facts matter on a *successful* call too — a
    grounded role that searched nothing, or a denial on a tool-less role, is invisible in a
    transcript nobody reads. The orchestrator puts a fixed subset on `call_finished`, which
    is what makes "no call was denied" and "generation actually searched" checkable after
    the fact rather than a claim."""

    degraded: bool = False
    """The call completed on different terms than asked (model or effort fell back)."""

    rate_limited: bool = False
    """The provider signalled backpressure; the orchestrator stops spawning new calls."""

    @property
    def ok(self) -> bool:
        return self.error is None and self.data is not None


@runtime_checkable
class AgentRunner(Protocol):
    """A stateless function call per role. Implementations must be safe to call in parallel."""

    name: str

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        """Run one role call. Never raises for a model-side failure — returns `error`."""
        ...

    async def probe(self) -> dict[str, Any]:
        """Cheap health check for `GET /health`: `{installed, version, ...}`."""
        ...

    async def aclose(self) -> None:
        """Release anything the runner holds. Safe to call more than once."""
        ...


# ------------------------------------------------------------------------- fake runner


@dataclass(frozen=True, slots=True)
class Failure:
    """A scripted failure for `FakeRunner`, keyed by `(role, round)`.

    `error` fails the call the way a timeout or non-zero exit does. `malformed` instead
    returns a payload that passes JSON but fails the role's schema, which is how the
    orchestrator's one-repair-then-skip path gets exercised. `times` limits how many calls
    the script affects, so a test can fail the first attempt and let the retry succeed.
    """

    error: str | None = None
    malformed: bool = False
    rate_limited: bool = False
    degraded: bool = False
    times: int | None = None


_FAILURE_KEY = tuple[str, int | None]

# Vocabulary the fake composes titles and prose from. Nonsense is not good enough: the
# demo run is a real product surface, and a developer reading a fake overview should be
# able to tell at a glance whether the pipeline wired the right text into the right place.
_SUBJECTS = (
    "membrane potential",
    "supply-chain slack",
    "symbiotic microbiota",
    "attention sparsity",
    "sediment turnover",
    "credit-cycle timing",
    "protein misfolding",
    "urban heat retention",
    "swarm consensus",
    "catalytic surface strain",
)
_MECHANISMS = (
    "a feedback loop that saturates under load",
    "a threshold effect that inverts below a critical density",
    "a shared bottleneck two subsystems compete for",
    "a delay between signal and response that compounds",
    "an energy-budget tradeoff resolved locally, not globally",
)
_FRAMES = (
    "reframes the effect as a scheduling problem",
    "treats the anomaly as the signal rather than the noise",
    "predicts a reversal no current account allows for",
    "moves the explanation one level below where it is usually placed",
)
_CLUSTER_LABELS = ("energetics", "topology", "timing", "signalling", "materials", "incentives")
_DOMAINS = (
    "glacial hydrology",
    "medieval guild economics",
    "coral reef acoustics",
    "railway signalling",
    "immunological tolerance",
)


class FakeRunner:
    """Deterministic stand-in for the CLI: same run id and prompt, same answer.

    Determinism is by `(seed, role, prompt)`, so a resumed run that re-sends an identical
    prompt gets an identical answer, while any change to the pool changes the output — the
    property a fake needs for resume tests to mean anything.

    Every call is recorded on `calls`, which is how tests assert what the orchestrator
    actually sent (that a meta-review prompt carried every review, that a killed run did
    not re-execute a completed unit).
    """

    name = "fake"

    def __init__(
        self,
        *,
        seed: str = "",
        failures: Mapping[_FAILURE_KEY, str | Failure] | None = None,
        latency: float = 0.0,
        cost_per_1k_tokens: float = 0.0015,
    ) -> None:
        self.seed = seed
        self.latency = latency
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self.calls: list[dict[str, Any]] = []
        self._failures: dict[_FAILURE_KEY, Failure] = {
            key: Failure(error=value) if isinstance(value, str) else value
            for key, value in (failures or {}).items()
        }
        self._fired: dict[_FAILURE_KEY, int] = {}

    # --- protocol ---------------------------------------------------------------------

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        if role not in SCHEMA_BY_ROLE:
            raise ValueError(f"unknown role {role!r}")
        if self.latency:
            await asyncio.sleep(self.latency)

        self.calls.append(
            {
                "role": role,
                "round": cfg.round,
                "unit": cfg.unit,
                "model": cfg.model,
                "effort": cfg.effort,
                "tools": cfg.tools,
                "timeout_s": cfg.timeout_s,
                "prompt": prompt,
            }
        )

        digest = self._digest(role, prompt)
        usage = self._usage(prompt, digest)
        failure = self._claim_failure(role, cfg.round)

        if failure is not None and failure.error:
            return RoleResult(
                role=role,
                model=cfg.model,
                error=failure.error,
                usage=usage,
                raw_tail=f"[fake] scripted failure for {role} round {cfg.round}",
                rate_limited=failure.rate_limited,
                degraded=failure.degraded,
            )

        data = (
            {"unexpected": "shape"}
            if failure is not None and failure.malformed
            else self._payload(role, prompt, digest)
        )
        return RoleResult(
            role=role,
            model=cfg.model,
            data=data,
            usage=usage,
            raw_tail="[fake] ok",
            rate_limited=bool(failure and failure.rate_limited),
            degraded=bool(failure and failure.degraded),
        )

    async def probe(self) -> dict[str, Any]:
        return {"installed": True, "version": "fake", "name": self.name}

    async def aclose(self) -> None:
        return None

    # --- determinism ------------------------------------------------------------------

    def _digest(self, role: str, prompt: str) -> int:
        material = f"{self.seed}\0{role}\0{prompt}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")

    def _usage(self, prompt: str, digest: int) -> Usage:
        tokens_in = max(1, len(prompt) // 4)
        tokens_out = 120 + digest % 400
        cache_read = tokens_in // 2
        total = tokens_in + tokens_out + cache_read
        return Usage(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_creation=tokens_in // 4,
            cache_read=cache_read,
            cost_usd=round(total / 1000 * self.cost_per_1k_tokens, 6),
            duration_ms=200 + digest % 1800,
        )

    def _claim_failure(self, role: str, round: int | None) -> Failure | None:
        for key in ((role, round), (role, None)):
            failure = self._failures.get(key)
            if failure is None:
                continue
            fired = self._fired.get(key, 0)
            if failure.times is not None and fired >= failure.times:
                continue
            self._fired[key] = fired + 1
            return failure
        return None

    # --- payloads ---------------------------------------------------------------------

    def _payload(self, role: str, prompt: str, digest: int) -> dict[str, Any]:
        builder = getattr(self, f"_{role}")
        return builder(prompt, digest)

    def _framing(self, prompt: str, digest: int) -> dict[str, Any]:
        return {
            "objective": "Resolve the stated objective while testing its central assumptions.",
            "source_strategy": {
                "published_research_percent": 70,
                "rationale": "Demo: combine scholarly mechanisms with contextual primary data.",
                "scholarly_queries": ["systematic review causal mechanism controlled study"],
                "other_source_queries": ["official dataset measurement methodology"],
            },
            "success_criteria": ["A useful complete solution with explicit evidence limits."],
            "approaches": [
                {
                    "id": f"a{i}",
                    "framing": framing,
                    "method": method,
                    "assumptions": ["The proposed bottleneck must be measured independently."],
                }
                for i, (framing, method) in enumerate(
                    [
                        ("A causal bottleneck limits the outcome.", "First-principles derivation"),
                        ("Measurement may create the apparent bottleneck.", "Assumption inversion"),
                        (
                            "Interactions determine the whole outcome.",
                            "Structural analogy and holistic design",
                        ),
                    ],
                    1,
                )
            ],
            "subproblems": [
                {
                    "id": "b1",
                    "question": "What is the binding constraint?",
                    "depends_on": [],
                    "acceptance_test": "Independent measurement discriminates causal alternatives.",
                },
                {
                    "id": "b2",
                    "question": "How can a complete design address that constraint?",
                    "depends_on": ["b1"],
                    "acceptance_test": "A controlled comparison improves the objective.",
                },
            ],
            "holistic_route": "Retain a holistic route when interactions defeat decomposition.",
        }

    def _verification(self, prompt: str, digest: int) -> dict[str, Any]:
        return {
            "claims": [
                {
                    "id": "c1",
                    "claim": "The proposed mechanism improves the objective.",
                    "status": "unresolved",
                    "sources": [],
                    "rationale": "Demo data cannot establish empirical support.",
                }
            ],
            "calculations": [
                {
                    "claim_id": "c1",
                    "expression": "(12 - 8) / 8",
                    "expected": 0.5,
                    "tolerance": 0.000001,
                    "assumption": "Illustrative inputs only; not observed measurements.",
                }
            ],
            "dimensions": {
                name: {
                    "grade": "moderate",
                    "rationale": "Plausible in the demo; domain evidence is still required.",
                }
                for name in ("originality", "usefulness", "feasibility", "upside")
            },
            "open_questions": ["Does the mechanism survive a controlled external test?"],
            "next_test": "Measure the proposed mechanism against an independent baseline.",
        }

    def _synthesis(self, prompt: str, digest: int) -> dict[str, Any]:
        workspace = self._research_workspace(prompt)
        ids = [row["hid"] for row in workspace["candidates"]]
        return {
            "markdown": "## Proposed complete solution\n\nCombine a causal intervention with "
            "an independent measurement strategy and evaluate the whole system against "
            "the objective. Preserve the measurement-artifact explanation as a competing "
            "design. This demo illustrates integration; it does not establish a real result.\n\n"
            "First identify the binding constraint, then compare the integrated intervention "
            "with a baseline. Reject the integration if component assumptions conflict.",
            "used_hids": ids,
            "dependencies": [
                {
                    "subproblem_id": row["id"],
                    "solution_hids": ids[:1],
                    "status": "open",
                    "rationale": "A proposal exists; external validation remains.",
                }
                for row in workspace["frame"]["subproblems"]
            ],
            "incompatibilities": [
                "A causal intervention and an artifact-only explanation may conflict."
            ],
            "unresolved": ["The complete design has not been independently tested."],
        }

    def _challenge(self, prompt: str, digest: int) -> dict[str, Any]:
        workspace = self._research_workspace(prompt)
        return {
            "assessment": "The integrated proposal covers the objective, but its causal "
            "premise remains untested. A polished integration is not independent evidence.",
            "blocking_issues": ["The causal premise requires a discriminating external test."],
            "next_action": "develop",
            "target_hids": [workspace["candidates"][0]["hid"]],
            "tests": [
                "Compare causal and measurement-artifact predictions on held-out observations."
            ],
        }

    def _research_workspace(self, prompt: str) -> dict[str, Any]:
        if "RESEARCH WORKSPACE:\n" in prompt:
            return json.JSONDecoder().raw_decode(prompt.split("RESEARCH WORKSPACE:\n", 1)[1])[0]
        return {"candidates": [{"hid": "h001"}], "frame": self._framing(prompt, 0)}

    def _generation(self, prompt: str, digest: int) -> dict[str, Any]:
        count = _requested_count(prompt, default=3)
        return {"hypotheses": [self._hypothesis(digest, index) for index in range(count)]}

    def _evolution(self, prompt: str, digest: int) -> dict[str, Any]:
        parents = _hids_in(prompt) or ["h001"]
        count = _requested_count(prompt, default=min(3, len(parents)))
        hypotheses = []
        for index in range(count):
            body = self._hypothesis(digest, index)
            operator = ("grounding", "combination", "simplification", "out_of_box")[
                (digest >> (index * 3)) % 4
            ]
            parent = parents[index % len(parents)]
            derived = (
                [parent, parents[(index + 1) % len(parents)]]
                if operator == "combination" and len(parents) > 1
                else [parent]
            )
            body["title"] = f"{body['title']} ({operator.replace('_', ' ')})"[:120]
            hypotheses.append({**body, "derived_from": derived, "operator": operator})
        return {"hypotheses": hypotheses}

    def _hypothesis(self, digest: int, index: int) -> dict[str, str]:
        shift = digest >> (index * 5)
        subject = _SUBJECTS[shift % len(_SUBJECTS)]
        mechanism = _MECHANISMS[(shift >> 3) % len(_MECHANISMS)]
        frame = _FRAMES[(shift >> 5) % len(_FRAMES)]
        marker = f"{(shift >> 7) % 0x1000:03x}"
        lever = mechanism.split(" that ")[0]
        return {
            "title": f"{subject.capitalize()} governs the outcome via {lever} [{marker}]"[:120],
            "claim": f"Variation in {subject} sets the ceiling on the observed effect, not the "
            f"other way round.",
            "mechanism": f"The proposal is {mechanism}; measured at the level of {subject}, the "
            f"account {frame}.",
            "novelty": f"Existing accounts treat {subject} as a consequence. This inverts the "
            f"direction, which {frame}.",
            "test": f"Hold everything else fixed and perturb {subject} across a range that "
            f"crosses the threshold; the effect should reverse sign, not merely weaken.",
            "assumptions": f"That {subject} can be perturbed independently, and that the "
            f"threshold sits inside the reachable range.",
        }

    def _reflection(self, prompt: str, digest: int) -> dict[str, Any]:
        # Roughly one in seven rejected: enough that a fake run exercises the rejection
        # path, rare enough that a two-round demo still has a tournament.
        reject = digest % 7 == 0
        level = ("high", "moderate", "moderate", "low")[digest % 4]
        return {
            "verdict": "reject" if reject else "pass",
            "novelty": {
                "level": level,
                "note": f"Novelty reads {level}: the framing is not the standard one, "
                f"though the underlying quantity is well studied.",
            },
            "correctness": (
                "A load-bearing assumption is stated but never defended, and the claim does "
                "not survive without it."
                if reject
                else "No fundamental flaw found; the causal story is at least coherent."
            ),
            "testability": "The proposed test discriminates against the obvious alternative, "
            "given a large enough perturbation.",
            "key_risk": "The effect may be an artefact of how the quantity is measured rather "
            "than of the mechanism proposed.",
            "note": (
                "Rejected: the central assumption is doing all the work and is not supported."
                if reject
                else "Worth ranking. Specific, testable, and not a restatement of the "
                "standard account."
            ),
        }

    def _proximity(self, prompt: str, digest: int) -> dict[str, Any]:
        hids = _hids_in(prompt)
        clusters: dict[str, str] = {}
        families: dict[str, str] = {}
        for position, hid in enumerate(hids):
            # Mostly singletons with the occasional genuine pair: the shape a healthy pool
            # has, and the one that leaves the tournament something to do.
            bucket = (digest >> (position * 2)) % max(2, len(_CLUSTER_LABELS))
            family = _CLUSTER_LABELS[bucket % len(_CLUSTER_LABELS)]
            clusters[hid] = f"{family}-{position % 2}"
            # The coarse label is the fine one with its variant dropped, which is the
            # relationship the real prompt asks for: several mechanisms, one kind of thing.
            families[hid] = family
        return {"clusters": clusters, "families": families}

    def _ranking(self, prompt: str, digest: int) -> dict[str, Any]:
        hids = _hids_in(prompt)
        first, second = (hids + ["h001", "h002"])[:2]
        winner = 1 if digest % 2 == 0 else 2
        kept, lost = (first, second) if winner == 1 else (second, first)
        return {
            "debate": f"Both hypotheses survive the correctness screen. {kept} states a "
            f"mechanism that makes a falsifiable prediction at a specific scale, while "
            f"{lost} restates the effect at the level it was already described. On "
            f"testability the panel splits, but {kept} names the perturbation and the "
            f"expected sign of the reversal. Impact favours {kept} for the same reason: "
            f"if it is wrong, the experiment says so cheaply.",
            "winner": winner,
        }

    def _meta_review(self, prompt: str, digest: int) -> dict[str, Any]:
        theme = _SUBJECTS[digest % len(_SUBJECTS)]
        return {
            "recurring_issues": "- Load-bearing assumptions asserted rather than argued\n"
            "- Tests that would confirm but could not refute\n"
            f"- Repeated retreat to {theme} as the explanation of last resort",
            "what_wins": "Hypotheses that name a threshold and predict a sign change win "
            "their matches; hypotheses that predict 'more' or 'less' lose them.",
            "guidance_next_round": "1. State the perturbation and the predicted direction "
            "before the mechanism.\n2. Attack the measurement, not only the effect.\n"
            f"3. Stop routing every explanation through {theme}.",
        }

    def _overview(self, prompt: str, digest: int) -> dict[str, Any]:
        titles = _titles_in(prompt)[:5] or ["(no hypotheses survived)"]
        body = "\n".join(
            f"### {index}. {title}\n\nRanked highly for naming a threshold and a falsifiable "
            f"direction of change. Open risk: the effect may be measurement-induced. Next "
            f"experiment: perturb across the threshold and check for a sign reversal.\n"
            for index, title in enumerate(titles, start=1)
        )
        return {
            "markdown": "# Research Overview\n\n## Summary\n\nThe run explored "
            f"{len(titles)} surviving line(s) of attack, concentrated around "
            f"{_SUBJECTS[digest % len(_SUBJECTS)]}.\n\n## Top Hypotheses\n\n{body}\n"
            "## Promising Directions Not Yet Pursued\n\nThe measurement-artefact account "
            "was raised in reviews but never developed into a hypothesis of its own.\n\n"
            "## Caveats\n\nRankings are self-evaluated by the model, not ground truth. "
            "Treat these as leads to validate.\n"
        }

    def _cartographer(self, prompt: str, digest: int) -> dict[str, Any]:
        domain = _DOMAINS[digest % len(_DOMAINS)]
        return {
            "source_domain": f"{domain} — far from the current clusters' shared frame, which "
            f"treats the system as a single well-mixed pool.",
            "skeleton": "- A slow reservoir feeds a fast channel through a constriction\n"
            "- The constriction's width is set by the reservoir's own history\n"
            "- Discharge is episodic, not proportional to input",
            "seed_framing": f"Generate a hypothesis that maps the reservoir/constriction/"
            f"episodic-discharge structure of {domain} onto the goal by identifying what "
            f"plays the role of the constriction.",
        }

    def _workshop(self, prompt: str, digest: int) -> dict[str, Any]:
        subject = _SUBJECTS[digest % len(_SUBJECTS)]
        return {
            "options": [
                {
                    "prompt": f"Investigate the mechanisms by which {subject} constrains the "
                    f"outcome, prioritising accounts that predict a threshold.",
                    "strategy": "Mechanism-first",
                    "optimizes_for": "Depth on a single causal story",
                    "excludes": "Population-level and purely statistical accounts",
                    "rationale": "Narrow goals produce hypotheses that can actually be tested.",
                    "recommended_settings": {
                        "rounds": 4,
                        "budget_calls": 120,
                        "matches_per_round": 6,
                        "grounding_depth": "standard",
                    },
                },
                {
                    "prompt": f"Map the full space of explanations for the outcome, including "
                    f"ones that do not involve {subject} at all.",
                    "strategy": "Breadth-first",
                    "optimizes_for": "Coverage of competing accounts",
                    "excludes": "Deep mechanistic detail on any one account",
                    "rationale": "Broad goals surface the framing nobody in the field has tried.",
                    "recommended_settings": {
                        "rounds": 3,
                        "budget_calls": 90,
                        "matches_per_round": 8,
                        "grounding_depth": "deep",
                    },
                },
            ]
        }


# --- prompt reading (the fake's only input) ------------------------------------------------

_HID = re.compile(r"\bh\d{3}\b")
_COUNT = re.compile(r"^N:\s*(\d+)", re.MULTILINE)
_TITLE_LINE = re.compile(r"^\s*(?:\d+\.\s*)?h\d{3}\s*\|\s*([^|\n]+)", re.MULTILINE)


def _requested_count(prompt: str, *, default: int) -> int:
    match = _COUNT.search(prompt)
    return max(1, int(match.group(1))) if match else default


def _hids_in(prompt: str) -> list[str]:
    """Hypothesis ids in first-seen order — the fake's view of what it was asked about."""
    seen: dict[str, None] = {}
    for hid in _HID.findall(prompt):
        seen.setdefault(hid, None)
    return list(seen)


def _titles_in(prompt: str) -> list[str]:
    return [title.strip() for title in _TITLE_LINE.findall(prompt)]

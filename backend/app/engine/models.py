"""Which models a role may run on, and the policy that keeps every run above the floor.

2026-09-08: Fable's stable application key remains `fable`, but the CLI receives the
explicit `claude-fable-5-1` id and its response is checked against that family. Astra
(`gpt-6-astra`) is an additional selectable heavy Codex model; Sol remains the default
heavy choice. The dated investigation below explains the original alias safeguards.

The engine offers **two models per provider**, and the reason is a fact about the CLIs
rather than a taste in models: **`--model` accepts identifiers the CLI does not recognise
and silently substitutes something else for them.** Probed live on 2026-08-08 against CLI
2.1.220:

| `--model` | what actually ran |
| --- | --- |
| `claude-opus-5` | `claude-opus-5` — correct |
| `fable` | `claude-fable-5` — correct, and the **only** working identifier for Fable 5 |
| `claude-fable-5` | **`claude-opus-5`**, with no error and no warning |
| `fable-5` | hard error |

That third row is the whole reason this module exists. A run configured with
`claude-fable-5` looks right in the config, right in the Settings tab and right in the
argv log, and is judged throughout by a different model than the one the scientist chose.
It is the same class of failure as the June 2026 incident: a flag the CLI quietly did not
honour. So the allowlist is a closed set of two identifiers, `claude-fable-5` is rejected
by name with the trap spelled out in the error, and `claude_runner` compares the model the
result envelope says ran against the one we asked for on every single call.

**The Opus floor.** No role may run below `claude-opus-5`. Proximity is mechanical
clustering and generation is not, but a cheaper model on the mechanical role still
produces cluster labels that the tournament, the collapse detector and the graft all
depend on — and the saving is a few cents on a run that costs dollars. The floor is
therefore absolute rather than per-role, and `model_rank` exists so the substitution guard
can tell a downgrade *below* it (a hard failure) from a sideways move (degrade and carry
on).

Prices are Anthropic list prices per million tokens as of 2026-08-08. Every model here
prices output at exactly 5× input, so the ratio between two of them is a single scalar
that holds whatever the token mix or cache-hit rate turns out to be — which is what makes
the per-call bracket derivable from a run measured on a *different* model rather than
guessed.

**This module is the published contract for what the UI shows about models.** Nothing
above the engine should re-type a model id, a label, a price or a per-call bracket:
`model_catalog()` returns the whole list ready to serve, `EFFORTS` is the effort
vocabulary, and `engine/runners.role_catalog()` pairs those with the per-role table and
the reason each role is set the way it is. Two copies of this list is exactly how a UI
ends up offering a model the engine refuses.

**Two providers, two models each (2026-08-11).** Codex returned as a peer runner, so every
model now carries the provider whose CLI can run it and the *class* — heavy or light — that
fixes which roles it takes. The four are `fable`/`claude-opus-5` on Anthropic and
`gpt-5.6-sol`/`gpt-5.6-luna` on OpenAI, and two facts about the OpenAI pair were pinned by
live probe rather than read off the plan:

* **Sol is the heavy model, Luna is the light one.** `codex debug models` calls
  `gpt-5.6-sol` the "Latest frontier agentic coding model" at priority 1 and `gpt-5.6-luna`
  the "Fast and affordable" one at priority 3, and the operator's own Codex config defaults
  to Sol. The plan had the two the other way round; wiring it literally would have put
  generation, reflection and the tournament on the cheap model — a substitution below the
  run's intent, which is the failure this module exists to prevent.
* **Codex effort does not top out at `high`.** The API enum is
  `none/minimal/low/medium/high/xhigh/max`, and `xhigh` was proven live on `gpt-5.6-luna`
  (exit 0). So the tier tables are provider-symmetric and nothing clamps today. The clamp is
  still implemented, because a *model* — not a provider — is what has an effort ladder:
  `supported_efforts` is per model, and `resolve_effort` degrades loudly against it rather
  than sending a level the server would 400 on mid-run. `ultra` appears in Sol's CLI catalog
  and in no API enum at all; it is never emitted.

Prices exist for the Anthropic pair and not for the OpenAI pair — Codex runs on a ChatGPT
account with no per-token list price to quote. Those two therefore report `None` for their
per-million rates and carry `price_basis="class_parity"`: their planning bracket is the
bracket of the Anthropic model of the same class, and the field says so rather than
inventing a number and letting it read as measured.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ALLOWED_MODELS",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "EFFORTS",
    "HOUSEKEEPING_BY_PROVIDER",
    "HOUSEKEEPING_MODELS",
    "MODEL_CHOICES",
    "MODEL_CLASSES",
    "MODEL_TRAPS",
    "OPUS_FLOOR_RANK",
    "PROVIDERS",
    "PROVIDER_FLOOR_RANK",
    "PROVIDER_HARNESS",
    "PROVIDER_LABELS",
    "EffortResolution",
    "ModelChoice",
    "ModelPolicyError",
    "Substitution",
    "canonical_family",
    "check_substitution",
    "clamp_effort",
    "effort_at_least",
    "is_housekeeping",
    "model_catalog",
    "model_for",
    "model_rank",
    "models_for",
    "normalise_provider",
    "price_for",
    "provider_catalog",
    "provider_of",
    "reported_models",
    "reported_provider",
    "resolve_effort",
    "supported_efforts",
    "usd_per_call_bracket",
    "validate_effort",
    "validate_model",
]


class ModelPolicyError(ValueError):
    """A model or effort that this engine refuses to run. A `ValueError`, so the launcher's
    existing `LaunchRefused` path and the API's 400 envelope both already understand it."""


EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
"""The engine's effort vocabulary, weakest first. The order is the comparison.

A subset of both providers': Claude's `--effort` and Codex's `model_reasoning_effort` both
also accept levels below `low`, and Codex's catalog lists `ultra` above `max` for one model.
Neither end of that is offered — nothing in this engine has a use for a level that thinks
less than `low`, and `ultra` is a CLI-side auto-delegation mode that the API enum does not
contain at all (verified 2026-08-11: sending it is a 400)."""

PROVIDERS: tuple[str, ...] = ("anthropic", "openai")

DEFAULT_PROVIDER = "anthropic"

PROVIDER_LABELS: dict[str, str] = {"anthropic": "Anthropic", "openai": "OpenAI"}

PROVIDER_HARNESS: dict[str, str] = {"anthropic": "claude", "openai": "codex"}
"""Which CLI runs a provider's models. The run's `harness` column, and the runner to pick."""

MODEL_CLASSES: tuple[str, ...] = ("heavy", "light")
"""What a model is *for*, which is fixed, unlike the effort a tier gives it.

`heavy` is the judgement that compounds across a run — generating, reviewing, ranking,
guiding the next round, writing the report. `light` is the workhorse: clustering, mutating,
grafting, shaping a question. The role → class map lives in `engine/runners.py`; this is the
other half of it, and what makes a *quiet* model swap detectable: a heavy role answered by
the light model of its own provider is the substitution this module is built to refuse.

A tier normally moves effort alone. One — `runners.TIER_MODELS`, today only the `low` tier —
names a model for both classes at once, which is a different thing from downgrading one class
into the other: it is data, it is published per tier by `runners.tier_catalog()`, and it
appears on every row of the resolved table. Refused is the swap nobody can see."""

DEFAULT_MODEL = "claude-opus-5"


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One selectable model, with everything a chooser needs to choose between them.

    `relative_cost` is a multiple of `claude-opus-5`, which is the floor and therefore the
    natural baseline. `usd_per_call` is a planning bracket, not a price — see
    `usd_per_call_bracket` below for how it is derived and what it excludes.

    `efforts` is this model's *own* ladder, not its provider's. A ladder is a property of a
    model in both CLIs (Codex publishes one per slug in `codex debug models`), and treating
    it as a provider-wide constant is how a run ends up sending a level the server rejects
    halfway through — or, worse, silently gets a weaker one.

    `grounded` says whether the model can search the web on a grounded role. All four can
    today; the flag exists because the day one cannot, a run must be able to warn about it
    before it spends a generation call on ungrounded ideas rather than after.
    """

    id: str
    label: str
    provider: str
    model_class: str
    recommendation: str
    efforts: tuple[str, ...]
    grounded: bool
    relative_cost: float
    usd_per_call: tuple[float, float]
    price_basis: str
    """`list` — derived from the provider's published per-million prices. `class_parity` —
    no published price exists for this model, so it is bracketed at the same figure as the
    model of the same class that does have one. Served rather than hidden, because a bracket
    that came from a parity assumption must not read as a measured price."""

    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "model_class": self.model_class,
            "recommendation": self.recommendation,
            "efforts": list(self.efforts),
            "grounded": self.grounded,
            "relative_cost": self.relative_cost,
            "price_basis": self.price_basis,
            "input_usd_per_mtok": self.input_usd_per_mtok,
            "output_usd_per_mtok": self.output_usd_per_mtok,
            "usd_per_call_low": self.usd_per_call[0],
            "usd_per_call_high": self.usd_per_call[1],
        }


# Measured, not estimated. Two real runs on sonnet-5 in August 2026:
#
#   12 calls, standard grounding : $2.12 total → $0.18 a call
#    9 calls, deep grounding     : $3.10 total → $0.34 a call
#      (generation $0.91, reflections $0.46/$0.28/$0.25, ranking $0.45/$0.08,
#       evolution $0.66, proximity on haiku $0.01)
#
# So a sonnet-5 run averaged $0.15–$0.40 a call depending on grounding depth. Scaling that
# by the price ratio is exact rather than approximate because every model in the allowlist
# prices output at 5× input, so one scalar covers input, output, cache reads and cache
# writes alike. The bracket is per-*call average over a run*, which is what a budget is set
# from — a single deep grounded generation call can sit well above the top of it.
_SONNET_5_MEASURED_USD_PER_CALL: tuple[float, float] = (0.15, 0.40)
_SONNET_5_INPUT_USD_PER_MTOK = 3.00


def usd_per_call_bracket(input_usd_per_mtok: float) -> tuple[float, float]:
    """The per-call planning bracket for a model, scaled from the measured sonnet-5 runs.

    Deliberately not a token model: the engine's token mix is dominated by cached prompt
    reads whose size depends on the goal, the round and the pool, and a token model would
    put a spurious third decimal place on a number whose real uncertainty is ±2×.
    """
    ratio = input_usd_per_mtok / _SONNET_5_INPUT_USD_PER_MTOK
    low, high = _SONNET_5_MEASURED_USD_PER_CALL
    return (round(low * ratio, 3), round(high * ratio, 3))


# Codex publishes a per-slug ladder in `codex debug models`; these are those ladders
# intersected with `EFFORTS` (both models' published ladders are
# low/medium/high/xhigh/max, plus `ultra` on Sol, which is CLI-only and never emitted).
_CODEX_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

MODEL_CHOICES: tuple[ModelChoice, ...] = (
    ModelChoice(
        id="claude-opus-5",
        label="Opus 5",
        provider="anthropic",
        model_class="light",
        recommendation=(
            "The floor, and the right workhorse. Strong enough that its judgement is worth "
            "compounding across a tournament, cheap enough to run every role on — which is "
            "what makes it the model the mechanical steps should be left on."
        ),
        efforts=EFFORTS,
        grounded=True,
        relative_cost=1.0,
        usd_per_call=usd_per_call_bracket(5.00),
        price_basis="list",
        input_usd_per_mtok=5.00,
        output_usd_per_mtok=25.00,
    ),
    ModelChoice(
        id="fable",
        label="Fable 5.1",
        provider="anthropic",
        model_class="heavy",
        recommendation=(
            "Fable 5.1 for demanding reasoning, research and long-running work. "
            "Pinned to the 5.1 model; input and output list prices are $10/$50 per million."
        ),
        efforts=EFFORTS,
        grounded=True,
        relative_cost=2.0,
        usd_per_call=usd_per_call_bracket(10.00),
        price_basis="list",
        input_usd_per_mtok=10.00,
        output_usd_per_mtok=50.00,
    ),
    ModelChoice(
        id="gpt-5.6-luna",
        label="Luna",
        provider="openai",
        model_class="light",
        recommendation=(
            "OpenAI's fast, affordable model, and the Codex side's workhorse. The right "
            "place for clustering, mutation and question-shaping when a run is on Codex."
        ),
        efforts=_CODEX_EFFORTS,
        grounded=True,
        relative_cost=1.0,
        usd_per_call=usd_per_call_bracket(5.00),
        price_basis="class_parity",
    ),
    ModelChoice(
        id="gpt-5.6-sol",
        label="Sol",
        provider="openai",
        model_class="heavy",
        recommendation=(
            "OpenAI's frontier model and the Codex side's judge. Takes the roles whose "
            "verdicts compound — generation, reflection, the tournament, the report. "
            "Confirmed heavy by the CLI's own catalog, which the plan had inverted."
        ),
        efforts=_CODEX_EFFORTS,
        grounded=True,
        relative_cost=2.0,
        usd_per_call=usd_per_call_bracket(10.00),
        price_basis="class_parity",
    ),
    ModelChoice(
        id="gpt-6-astra",
        label="Astra",
        provider="openai",
        model_class="heavy",
        recommendation=(
            "GPT-6 Astra for the hardest reasoning, research and complete-solution work. "
            "An optional Codex upgrade; choosing it preserves the configured thinking effort."
        ),
        efforts=_CODEX_EFFORTS,
        grounded=True,
        relative_cost=2.0,
        usd_per_call=usd_per_call_bracket(10.00),
        price_basis="list",
        input_usd_per_mtok=10.00,
        output_usd_per_mtok=50.00,
    ),
)

ALLOWED_MODELS: tuple[str, ...] = tuple(choice.id for choice in MODEL_CHOICES)

_CHOICE_BY_ID: dict[str, ModelChoice] = {choice.id: choice for choice in MODEL_CHOICES}

# Identifiers that must be refused with an explanation rather than a bare "not allowed",
# because the reason they are wrong is not visible from the string itself.
MODEL_TRAPS: dict[str, str] = {
    "claude-fable-5": (
        "use `fable` — `claude-fable-5` silently resolves to Opus 5 (verified live "
        "2026-08-08: the CLI accepts it, reports no error, and runs claude-opus-5)"
    ),
    "fable-5": "use `fable` — the CLI rejects `fable-5` outright",
    "claude-mythos-5": "Mythos 5 is not available to this installation; use `fable`",
    "opus": "use the pinned id `claude-opus-5`, not the bare alias",
    "opus-5": "use the pinned id `claude-opus-5`",
    "sol": "use the pinned id `gpt-5.6-sol`, not the bare name",
    "luna": "use the pinned id `gpt-5.6-luna`, not the bare name",
    "gpt-5.6-sol-wm": (
        "`gpt-5.6-sol-wm` is a hidden Work-Mode routing alias with supported_in_api=false; "
        "the API refuses it. Use `gpt-5.6-sol`"
    ),
    "gpt-5.6-terra": (
        "Terra is not offered by this engine. Use `gpt-6-astra`, `gpt-5.6-sol` "
        "or `gpt-5.6-luna`"
    ),
}


def model_catalog() -> list[dict[str, Any]]:
    """The allowlist with its display metadata, ready for `/api/capabilities` to serve."""
    return [choice.as_dict() for choice in MODEL_CHOICES]


def provider_catalog() -> list[dict[str, Any]]:
    """One row per provider: its label, its CLI lane, and the models it can run.

    What the top bar's provider segment reads. Served rather than restated so a UI cannot
    offer a provider whose runner this build does not have.
    """
    return [
        {
            "id": provider,
            "label": PROVIDER_LABELS[provider],
            "harness": PROVIDER_HARNESS[provider],
            "models": [choice.id for choice in models_for(provider)],
            "efforts": sorted(
                {effort for choice in models_for(provider) for effort in choice.efforts},
                key=EFFORTS.index,
            ),
        }
        for provider in PROVIDERS
    ]


def price_for(model: str) -> ModelChoice:
    """The pricing row for an allowed model. Raises for anything off the allowlist."""
    choice = _CHOICE_BY_ID.get(model)
    if choice is None:
        raise ModelPolicyError(_refusal(model))
    return choice


def normalise_provider(provider: Any) -> str:
    """The provider id, or a refusal naming the ones this build can actually run."""
    if provider is None:
        return DEFAULT_PROVIDER
    if not isinstance(provider, str) or provider.strip().lower() not in PROVIDERS:
        raise ModelPolicyError(
            f"unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}"
        )
    return provider.strip().lower()


def provider_of(model: str) -> str:
    """Which provider's CLI runs this model. Raises for anything off the allowlist."""
    return price_for(str(model).strip()).provider


def models_for(provider: str) -> tuple[ModelChoice, ...]:
    return tuple(
        choice for choice in MODEL_CHOICES if choice.provider == normalise_provider(provider)
    )


def model_for(provider: str, model_class: str) -> str:
    """The id of a provider's heavy or light model — what a role runs on when nothing else
    says otherwise. The bottom of one precedence: a role's own override, then the model its
    tier pins (`runners.TIER_MODELS`, only `low`), then this."""
    resolved = normalise_provider(provider)
    if model_class not in MODEL_CLASSES:
        raise ModelPolicyError(
            f"unknown model class {model_class!r}; expected one of {', '.join(MODEL_CLASSES)}"
        )
    for choice in models_for(resolved):
        if choice.model_class == model_class:
            return choice.id
    raise ModelPolicyError(  # pragma: no cover — the catalog covers both classes per provider
        f"provider {resolved!r} has no {model_class} model"
    )


def validate_model(model: Any, *, provider: str | None = None) -> str:
    """The one gate on what may reach `--model` / `-m`. Returns the id, or raises.

    `provider` is an optional extra assertion, for the callers that already know which lane
    the call belongs to. It is deliberately not a filter that silently substitutes: naming a
    model of the other provider is a request that did not mean what it said, and the answer
    is a refusal naming both.
    """
    if not isinstance(model, str) or not model.strip():
        raise ModelPolicyError("a model id must be a non-empty string")
    name = model.strip()
    choice = _CHOICE_BY_ID.get(name)
    if choice is None:
        raise ModelPolicyError(_refusal(name))
    if provider is not None and choice.provider != normalise_provider(provider):
        raise ModelPolicyError(
            f"model {name!r} belongs to {choice.provider}, not {normalise_provider(provider)}; "
            f"{normalise_provider(provider)} offers "
            f"{', '.join(item.id for item in models_for(provider))}"
        )
    return name


def _refusal(model: str) -> str:
    trap = MODEL_TRAPS.get(model.strip().lower())
    allowed = ", ".join(repr(item) for item in ALLOWED_MODELS)
    if trap:
        return f"model {model!r} is refused: {trap}. Allowed models: {allowed}"
    return (
        f"model {model!r} is not allowed; no role may run below Opus 5 or its Codex "
        f"equivalent. Allowed models: {allowed}"
    )


def effort_at_least(effort: str, minimum: str) -> str:
    """The stronger of two efforts. How an escalation is applied.

    The orchestrator escalates round-1 generation and a decisive match to `high`. That has
    to raise a floor rather than assign a value: with the baseline table now at high, the
    assignment reads as a no-op — but against a role a scientist deliberately overrode to
    `xhigh` or `max`, assigning `high` would quietly *downgrade* the call that was singled
    out as the most important one in the run.
    """
    order = {name: index for index, name in enumerate(EFFORTS)}
    return effort if order[validate_effort(effort)] >= order[validate_effort(minimum)] else minimum


def validate_effort(effort: Any) -> str:
    if not isinstance(effort, str) or effort.strip() not in EFFORTS:
        raise ModelPolicyError(
            f"unknown effort {effort!r}; expected one of {', '.join(EFFORTS)}"
        )
    return effort.strip()


# ------------------------------------------------------------------- the per-model ladder


@dataclass(frozen=True, slots=True)
class EffortResolution:
    """What a model will actually think at, given what was asked for.

    `clamped` is the interesting one and it is never silent: it becomes `effort_clamped` in
    a call's telemetry and is what a UI shows instead of the level nobody could run. The
    alternative — sending a level a model's ladder does not contain — is a 400 in the middle
    of a paid run, or, on a provider that rounds instead of refusing, a call answered at an
    effort nothing in the artefacts records.
    """

    model: str
    requested: str
    effort: str

    @property
    def clamped(self) -> bool:
        return self.effort != self.requested

    @property
    def detail(self) -> str:
        return (
            f"{self.model} does not offer {self.requested} effort; ran at {self.effort} "
            f"(its ladder is {', '.join(supported_efforts(self.model))})"
        )


def supported_efforts(model: str) -> tuple[str, ...]:
    """This model's own effort ladder, weakest first. Raises off the allowlist."""
    return price_for(str(model).strip()).efforts


def clamp_effort(effort: str, supported: Sequence[str]) -> str:
    """The strongest supported effort that is no stronger than `effort`.

    Pure, and takes the ladder rather than the model, because the property worth testing is
    the arithmetic — a ladder shorter than the vocabulary is exactly the case that has no
    example in today's catalog and will have one the day a model ships with a shorter one.

    Clamping *down* rather than refusing is deliberate: a tier asking for more thinking than
    a model can do is a preference the model cannot fully honour, not a malformed request.
    Clamping up would be the other thing — spending more than was asked for.
    """
    validate_effort(effort)
    ladder = [item for item in EFFORTS if item in set(supported)]
    if not ladder:
        raise ModelPolicyError(
            f"no usable effort in {list(supported)}; expected some of {', '.join(EFFORTS)}"
        )
    ceiling = EFFORTS.index(effort)
    below = [item for item in ladder if EFFORTS.index(item) <= ceiling]
    # Nothing at or below what was asked for means the model's floor is above it. Its floor
    # is then the only thing it can run, and it is louder than refusing the call outright.
    return below[-1] if below else ladder[0]


def resolve_effort(model: str, effort: str) -> EffortResolution:
    """The effort this model will really run at, and whether that is what was asked for."""
    requested = validate_effort(effort)
    return EffortResolution(
        model=str(model).strip(),
        requested=requested,
        effort=clamp_effort(requested, supported_efforts(model)),
    )


# --------------------------------------------------------------------- the substitution guard
#
# Two providers means the guard's three Anthropic-shaped assumptions all had to be reopened:
#
# * **The floor is per provider, not one integer.** Ranks used to be a single line-up with
#   `OPUS_FLOOR_RANK` drawn across it. Sol and Luna cannot join that line-up: rank Luna at
#   the Anthropic floor and it ties by coincidence, rank it below and every legitimate Luna
#   call reads as a floor breach — a *hard failure on a correct call*, which is the one
#   error class that looks fine in review. So a family carries its provider, and the floor
#   is looked up per provider: Opus 5 within Anthropic, Luna within OpenAI.
# * **An unrecognised provider is loud.** An unknown model *within* a known provider stays a
#   degrade: models ship faster than this table is edited, and calling a newer Opus a
#   downgrade would fail runs for being current. A name that belongs to no provider we know
#   is a different claim — it says the invocation reached something this engine has no
#   account of — and that fails the call.
# * **Aliases and housekeeping are provider-specific.** `fable` → `claude-fable-5` is a
#   Claude-CLI fact; Codex slugs are exact and need no alias. Haiku housekeeping is a
#   Claude-CLI fact too, and filtering it out of an OpenAI comparison would erase exactly
#   the cross-provider leak the rule above exists to catch.
#
# WHAT CODEX REPORTS — answered from the 2026-08-11 live probe, and the answer is *nothing*.
# `codex exec --json` emits `thread.started`, `turn.started`, `item.started`/`item.completed`
# (agent_message, web_search, error) and `turn.completed` with a `usage` block. Not one of
# them names the model that answered; there is no `modelUsage` equivalent. So this guard
# covers zero Codex calls, and that is a stated gap rather than a silent one:
# `codex_runner` marks every call `model_verified: False` in telemetry and gets the same
# guarantee three other ways — the id is validated against the closed allowlist before the
# spawn, an unknown id fails loudly (exit 1, no last-message file, `turn.failed`), and any
# `item.type == "error"` mentioning "fallback metadata" is treated as a hard refusal even
# when the turn later succeeds. If a future Codex release does report a model, the runner
# feeds it through here and the provider rules above apply unchanged.

# The stable application key `fable` is pinned to Fable 5.1 in argv and response checks.
# The CLI reports the full family, possibly with a date suffix. Codex uses exact slugs;
# ids are exact slugs (`gpt-5.6-sol`), so an alias map for it would be a fiction, and a wrong
# entry here would make every Codex call read as a swap.
_ALIAS_TO_FAMILY: dict[str, str] = {"fable": "claude-fable-5-1"}

# Ranked by capability *within a provider*. Longest prefix wins, so `claude-opus-5` is not
# mistaken for `claude-opus-4-8` and `gpt-5.4-mini` is not read as `gpt-5.4`. This table is
# the authority for *rank* and nothing else: an id matching nothing here has rank `None`,
# and which provider it belongs to is settled separately by `_PROVIDER_PREFIX` below. The
# split is the point — "we cannot rank it" and "we cannot place it" are different claims,
# the first a degrade inside a known provider and only the second a hard failure.
_FAMILY_RANK: tuple[tuple[str, str, int], ...] = (
    ("claude-fable-5-1", "anthropic", 4),
    ("claude-fable-5", "anthropic", 3),
    ("claude-mythos-5", "anthropic", 3),
    ("claude-opus-5", "anthropic", 2),
    ("claude-opus-4", "anthropic", 1),
    ("claude-opus-3", "anthropic", 1),
    ("claude-sonnet", "anthropic", 1),
    ("claude-haiku", "anthropic", 0),
    ("claude-3", "anthropic", 0),
    ("claude-2", "anthropic", 0),
    # Codex's catalog as of 2026-08-11, ranked by its own priority field: sol (priority 1,
    # "latest frontier") above terra (2, "balanced") above luna (3, "fast and affordable"),
    # with luna at the floor because it is the light model this engine actually runs.
    ("gpt-6-astra", "openai", 4),
    ("gpt-5.6-sol", "openai", 3),
    ("gpt-5.6-terra", "openai", 2),
    ("gpt-5.6-luna", "openai", 2),
    ("gpt-5.5", "openai", 1),
    ("gpt-5.4-mini", "openai", 0),
    ("gpt-5.4", "openai", 1),
    ("gpt-5.3", "openai", 0),
)

# Which provider an id belongs to, decided *without* reference to rank — so that a model
# newer than the table above is "anthropic, unranked" rather than "nobody's". Each prefix is
# the vendor stem every one of that provider's ids in `_FAMILY_RANK` already shares
# (`claude-` for all nine Anthropic entries, `gpt-` for all seven OpenAI ones), so this table
# is derived from that one rather than guessed; a unit test re-derives it and fails if the
# two ever disagree.
#
# Why each prefix is safe to claim:
# * `claude-` is Anthropic's product namespace and the bare shape the Claude CLI reports in
#   `modelUsage`. The Anthropic ids it does *not* match are the cloud-gateway spellings
#   (`anthropic.claude-…`, `us.anthropic.claude-…`), left unclaimed on purpose: this engine
#   spawns the CLI, which never emits them, so a prefix for them would be coverage invented
#   for a shape nothing here produces.
# * `gpt-` is OpenAI's. The historical collisions — EleutherAI's `gpt-j`, `gpt-neox` — cannot
#   reach the lenient verdict, because a matched prefix only softens anything when the
#   *requested* model was that same provider's, i.e. when Codex itself named what answered,
#   and Codex does not run third-party weights. Named on an Anthropic call, they are still
#   cross-provider and still fatal.
#
# There is deliberately no catch-all: an id in neither namespace keeps provider `None`, which
# is the loud case this whole section exists to preserve.
_PROVIDER_PREFIX: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
)

PROVIDER_FLOOR_RANK: dict[str, int] = {"anthropic": 2, "openai": 2}
"""The weakest model each provider may answer a role call with: Opus 5, and Luna.

Per provider because the two scales are not commensurable — there is no fact of the matter
about whether Luna is "above Opus 5" — and because the thing the floor protects is a run's
internal consistency, which is a within-provider property."""

OPUS_FLOOR_RANK = PROVIDER_FLOOR_RANK["anthropic"]
"""The Anthropic floor, kept under its old name because it is what the incident is called."""

# A CLI runs its own housekeeping turns on a small model and reports them in the same map.
# They are not the role model and must not be read as one — but a map containing *only*
# housekeeping tells us nothing about what answered, which is why that case is "unverified"
# below rather than "substituted". Per provider: Codex reports no housekeeping model (it
# reports no model at all), and filtering Anthropic's haiku out of an OpenAI comparison
# would hide a Claude model answering a Codex call, which is the loudest thing this guard
# has to say.
HOUSEKEEPING_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-haiku-4-5", "claude-haiku-3"),
    "openai": (),
}

HOUSEKEEPING_MODELS: tuple[str, ...] = HOUSEKEEPING_BY_PROVIDER["anthropic"]
"""Every provider's housekeeping models at once. `is_housekeeping`'s default scope."""


def canonical_family(model: str) -> str:
    """The name a model reports itself as, given the identifier we ask for it by."""
    return _ALIAS_TO_FAMILY.get(model.strip(), model.strip())


def model_rank(reported: str) -> int | None:
    """Capability rank of a model named in a result envelope, or None if we do not know it.

    Only comparable against another rank from the *same* provider — see
    `reported_provider`."""
    return _family(reported)[1]


def reported_provider(reported: str) -> str | None:
    """Which provider a model named in a result envelope belongs to, or None if no idea.

    Independent of whether we can rank the id: a `claude-*` name this table has never heard
    of is still Anthropic's. None is the loud case, not the quiet one — it means something
    answered that this engine has no account of at all."""
    return _family(reported)[0]


def _family(reported: str) -> tuple[str | None, int | None]:
    """Provider and rank, decided independently, so `(provider, None)` is a real answer.

    An id off `_FAMILY_RANK` but inside a vendor namespace we know — the newer Opus, on the
    day it ships — is `("anthropic", None)`: placed, unranked. No rank is invented for it;
    `None` stays `None` so every rank comparison downstream skips it. Only an id in no
    namespace at all is `(None, None)`, and that is the one the guard fails a run over."""
    name = str(reported).strip().lower()
    for prefix, provider, rank in _FAMILY_RANK:
        if name.startswith(prefix):
            return provider, rank
    for prefix, provider in _PROVIDER_PREFIX:
        if name.startswith(prefix):
            return provider, None
    return None, None


def is_housekeeping(reported: str, *, provider: str | None = None) -> bool:
    """Whether this name is a CLI's own bookkeeping model rather than a role model.

    Scoped to one provider when the caller knows which lane the call was in. Unscoped it
    matches any provider's, which is the right default for a caller that only wants to know
    whether a name is *somebody's* housekeeping.
    """
    name = str(reported).strip().lower()
    prefixes = (
        HOUSEKEEPING_BY_PROVIDER.get(normalise_provider(provider), ())
        if provider is not None
        else tuple(item for values in HOUSEKEEPING_BY_PROVIDER.values() for item in values)
    )
    return any(name.startswith(prefix) for prefix in prefixes)


@dataclass(frozen=True, slots=True)
class Substitution:
    """A call answered by a model other than the one it was configured with."""

    requested: str
    ran: tuple[str, ...]
    below_floor: bool
    """True when something below the requested provider's floor answered. A hard failure,
    not a warning: an Elo rating half of which came from a weaker judge is worth less than
    it looks, and a run that keeps going hides the fact rather than reporting it."""

    cross_provider: bool = False
    """True when a model from another provider — or from none this engine knows — answered.

    Also a hard failure, and for a stronger reason than the floor: within a provider an
    unrecognised id is probably a model newer than this table, but a name belonging to no
    known provider means the invocation reached something the engine has no account of.
    Degrading on that would be inferring "probably fine" from "no idea".

    An id whose vendor namespace we recognise but whose family we do not is *not* this: it
    sets `unrecognised` instead and stays non-fatal."""

    below_request: bool = False
    """True when a *weaker model of the same provider* answered — Luna for Sol, Opus 5 for
    Fable. Not fatal on its own (the answer is still above the floor and worth keeping) but
    it is the run's intent not being honoured, so it is named separately from a sideways or
    upward swap rather than folded into `degraded`."""

    unrecognised: bool = False
    """True when something in the requested provider's own namespace answered under an id
    this table cannot rank — `claude-opus-6` on an Anthropic call.

    Carried separately from `cross_provider` because the two say opposite things about
    whether the run survives, and one `provider is None` test used to answer both — which
    is what made every unrankable Anthropic id fatal. Here the provider is known and only
    the family is not, so the honest report is "we cannot rank what ran": not "another
    provider answered", and not "a weaker model answered" either, because no rank was
    established. Non-fatal by design — models ship faster than this table is edited, and
    failing a run for being current is the outcome `check_substitution` exists to avoid."""

    @property
    def provider(self) -> str | None:
        """Which provider was asked for. Derived, so it cannot disagree with `requested`."""
        return reported_provider(canonical_family(self.requested))

    @property
    def fatal(self) -> bool:
        """Whether this swap invalidates the answer rather than merely annotating it."""
        return self.below_floor or self.cross_provider

    @property
    def detail(self) -> str:
        ran = ", ".join(self.ran)
        if self.cross_provider:
            note = " — from another provider entirely"
        elif self.below_floor:
            # Named after the floor model rather than the provider, because that is the name
            # the incident is known by and the one a reader of a failed run will search for.
            floor = "Opus 5" if self.provider == "anthropic" else "Luna"
            note = f" — below the {floor} floor"
        elif self.below_request:
            note = " — a weaker model than the run asked for"
        elif self.unrecognised:
            # Neither "another provider" nor "weaker": the provider is the one that was
            # asked for and the capability is genuinely unknown, so the sentence has to say
            # unknown rather than pick whichever of the two loud phrasings is nearer.
            note = " — an id this engine does not recognise"
        else:
            note = ""
        return f"requested {self.requested}, ran {ran}{note}"


def reported_models(model_usage: Any, *, provider: str | None = None) -> list[str]:
    """The role-model names in a result envelope's `modelUsage`, housekeeping removed.

    Placeholder keys the CLI writes for synthetic turns (`<synthetic>`) are dropped too;
    they are not models. `provider` scopes which housekeeping names are filtered.
    """
    if not isinstance(model_usage, Mapping):
        return []
    return [
        name
        for name in model_usage
        if isinstance(name, str)
        and name
        and not name.startswith("<")
        and not is_housekeeping(name, provider=provider)
    ]


def check_substitution(requested: str, model_usage: Any) -> Substitution | None:
    """Compare what ran against what was asked for. None means "no evidence of a swap".

    Three ways to get None, and they are deliberately not distinguished by the caller:
    the models match; `modelUsage` is missing or unreadable; or it names nothing but the
    CLI's own housekeeping model. In none of those cases do we have evidence that the
    wrong judge answered, and inventing a failure from an absent field would fail runs for
    a CLI release that renamed a key.

    Everything else is a `Substitution`, and the caller decides from `fatal` whether the
    answer survives. Fatal means the floor was breached or the provider changed; the rest
    is recorded, reported and kept. An id inside the requested provider that this table
    cannot rank is in that second group — the table lagging a release is not evidence that
    the wrong judge answered, and treating it as such would fail every call in a run on the
    day a new model ships.
    """
    wanted = str(requested).strip()
    expected_provider = reported_provider(canonical_family(wanted))
    names = reported_models(model_usage, provider=expected_provider)
    if not names:
        return None

    expected = canonical_family(wanted)
    # A shorter prefix is not the requested version: Fable 5 cannot satisfy Fable 5.1.
    # Every non-housekeeping entry must match, including when a CLI reports a fallback.
    if all(name == expected or name.startswith(expected + "-") for name in names):
        return None

    providers = [reported_provider(name) for name in names]
    cross_provider = any(
        provider is None or provider != expected_provider for provider in providers
    )

    ranks = [model_rank(name) for name in names]
    floor = PROVIDER_FLOOR_RANK.get(expected_provider or "", None)
    below_floor = floor is not None and any(
        rank is not None and provider == expected_provider and rank < floor
        for rank, provider in zip(ranks, providers, strict=True)
    )
    wanted_rank = model_rank(expected)
    below_request = wanted_rank is not None and any(
        rank is not None and provider == expected_provider and rank < wanted_rank
        for rank, provider in zip(ranks, providers, strict=True)
    )
    # The requested provider's own namespace, an id the rank table has never seen. Both the
    # floor and the request comparisons above skip it on `rank is not None`, which is correct
    # — there is nothing to compare — so without this flag the swap would report as an
    # unremarkable sideways move and say nothing about why it could not be ranked.
    unrecognised = expected_provider is not None and any(
        rank is None and provider == expected_provider
        for rank, provider in zip(ranks, providers, strict=True)
    )
    return Substitution(
        requested=wanted,
        ran=tuple(sorted(names)),
        below_floor=below_floor,
        cross_provider=cross_provider,
        below_request=below_request,
        unrecognised=unrecognised,
    )

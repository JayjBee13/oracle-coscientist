from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser
from app.engine.models import EFFORTS, PROVIDERS, model_catalog, provider_catalog
from app.engine.runners import (
    MODEL_ESCALATION_NOTES,
    MODEL_TIERS,
    normalise_tier,
    role_catalog,
    tier_catalog,
)
from app.services.harness.probes import cached_probe_cli, probe_cli
from app.services.settings.models import stored_model_settings

router = APIRouter(prefix="/api/capabilities", tags=["capabilities"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


class HarnessBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installed: bool
    version: str | None = None


class GroundingBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web: bool
    perplexity: bool


class RoleModelBrief(BaseModel):
    """One step of the loop: what it runs on, and why it is set that way."""

    model_config = ConfigDict(extra="forbid")

    role: str
    model: str
    effort: str
    note: str
    grounded: bool
    model_class: str
    """`heavy` or `light` — how hard this step is, which is what fixes its model. Constant
    across every tier and provider: a tier moves effort and never this."""


class ModelChoiceBrief(BaseModel):
    """A model a role may be set to, with what it costs to prefer it.

    **`efforts` is this model's own effort ladder, and an effort picker must read it off the
    selected model rather than off its provider.** The two providers' ladders happen to match
    today and are not required to: a ladder is published per model slug by the CLI that runs
    it, and offering a rung the model does not have is a 400 in the middle of a paid run.

    `input_usd_per_mtok`/`output_usd_per_mtok` are list prices per million tokens and are
    **null for models that have none** — the Codex pair runs on a ChatGPT account with no
    per-token price to quote. Their per-call bracket is still given, and `price_basis` says
    where it came from: `list` (derived from those prices) or `class_parity` (the bracket of
    the model of the same class that does have them). A bracket labelled as a parity estimate
    is honest; a price nobody quoted would not be.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    provider: str
    model_class: str
    recommendation: str
    efforts: list[str]
    grounded: bool
    relative_cost: float
    price_basis: str
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    usd_per_call_low: float
    usd_per_call_high: float


class ProviderBrief(BaseModel):
    """One provider the top bar's quick-set can choose, and the CLI lane it drives.

    `efforts` here is the *union* of this provider's models' ladders and exists only to size
    a control. It is not a per-row vocabulary: a row's real options are the `efforts` of the
    model on that row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    harness: str
    models: list[str]
    efforts: list[str]


class TierBrief(BaseModel):
    """One tier the top bar can select, and what selecting it does.

    Served rather than restated for the same reason the role table is: a control that
    described a tier in its own words would describe it wrongly the first time a tier changed.

    Two fields exist to keep the `low` tier's model pin **visible**, which is the condition
    on which a tier was allowed to name a model at all:

    * `pinned_models` — model class → the model this tier forces on it, empty for the tiers
      that force none (`max`, `high`, `med` all leave the model to the provider). A client can
      therefore say "this tier runs Luna" without diffing two resolved tables to notice.
    * `requires_harness` — the CLIs a run in this tier drives *because of the pin*, whatever
      its provider says. `low` requires `codex`, and `available` is that requirement checked
      against the probe, so the UI can warn on an install without Codex rather than let the
      first call of a run discover it.

    `available` is false only when a required CLI is missing. A tier that pins nothing is
    always available: which CLI it needs is then decided by the run's provider, which the
    `harnesses` block already reports on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    note: str
    """What choosing this tier does, in the words the control should use."""

    pinned_models: dict[str, str] = {}
    requires_harness: list[str] = []
    available: bool = True

    unavailable_reason: str | None = None
    """Why this tier cannot run here, naming the CLI that is missing. Null when it can.

    Not called `notes` — `note` above is a permanent description of the tier and this is a
    fact about this machine right now, and two fields a letter apart holding those two
    different things is a mix-up waiting to be rendered."""


class ModelDefaultBrief(BaseModel):
    """The persisted system default every new run and workshop inherits.

    The same value `GET /api/settings/models` serves, carried here so the wizard and the top
    bar can render on one round trip. `source` distinguishes a default somebody chose from
    the built-in one nobody has changed yet.

    `tiers` is the stored per-tier override buckets — saving in the editor redefines the tier
    that is selected, so there is one bucket per tier and each is sparse. `overrides` is the
    derived mirror of the selected tier's bucket, which is what the launcher and the workshop
    actually read, and `table` is that tier resolved.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    tier: str
    tiers: dict[str, dict[str, dict[str, str]]] = {}
    overrides: dict[str, dict[str, str]] = {}
    source: str
    updated_at: str | None = None
    table: list[RoleModelBrief] = []


class ModelsBrief(BaseModel):
    """Which model and thinking effort each step gets, per provider and per tier.

    Read straight out of the engine's own `role_catalog`, so what the top bar and the wizard
    show is what a run launched right now would actually use. `notes` carries the escalations
    the orchestrator applies from run state, which no table can express; `catalog`,
    `providers` and `efforts` are the vocabularies a per-role picker chooses from — served
    rather than restated, because a client that types its own model list is a client that can
    offer a model the engine will refuse.

    `tiers` is keyed by provider and then by tier, because the same tier names a different
    pair of models on each provider. `efforts` is the engine's whole vocabulary and is the
    outer bound only — **the options for one row are the `efforts` of that row's model in
    `catalog`.**

    `tier_catalog` is the tiers themselves: label, what each one does, and — for the one tier
    that pins a model rather than only an effort — which model and which CLI that needs. A
    control offering tiers reads that instead of hard-coding four names and a description each.
    """

    model_config = ConfigDict(extra="forbid")

    # Engine vocabulary, not the request enum's: `default_tier` is a key of every provider's
    # entry in `tiers`, which is the whole contract this block offers a client. A stored tier
    # the engine still accepts under an old name resolves to the current one here rather than
    # naming a tier the payload does not carry.
    default_tier: str
    default_provider: str
    tiers: dict[str, dict[str, list[RoleModelBrief]]]
    tier_catalog: list[TierBrief] = []
    providers: list[ProviderBrief] = []
    catalog: list[ModelChoiceBrief] = []
    efforts: list[str] = []
    notes: list[str] = []
    default: ModelDefaultBrief | None = None


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grounding: GroundingBrief
    harnesses: dict[str, HarnessBrief]
    models: ModelsBrief


@router.get("", response_model=CapabilitiesResponse)
def capabilities_summary(_current: CurrentUser) -> CapabilitiesResponse:
    """Plan C5's single combined view: what the wizard needs to decide, nothing it doesn't.

    Unlike `/harnesses` below, this never spawns a fresh probe on every request — every
    open tab polls it, and a minute-old install/version answer is as good as a live one
    (`cached_probe_cli`, 60s).
    """
    claude = cached_probe_cli("claude")
    codex = cached_probe_cli("codex")
    return CapabilitiesResponse(
        grounding=GroundingBrief(web=True, perplexity=False),
        harnesses={
            "claude": HarnessBrief(installed=claude.installed, version=claude.version),
            "codex": HarnessBrief(installed=codex.installed, version=codex.version),
        },
        models=_models_brief(),
    )


def _models_brief() -> ModelsBrief:
    """Every provider's role table for every tier, resolved by the engine, not described here.

    `default_tier`/`default_provider` are the *stored* system default put through the
    engine's normaliser, so what this reports is what a run created without a tier actually
    resolves to — not a constant that could drift from the setting the editor writes.
    """
    default = stored_model_settings()
    return ModelsBrief(
        default_tier=normalise_tier(default.tier),
        default_provider=default.provider,
        tiers={
            provider: {
                tier: [RoleModelBrief(**row) for row in role_catalog(provider, tier)]
                for tier in MODEL_TIERS
            }
            for provider in PROVIDERS
        },
        tier_catalog=[_tier_brief(row) for row in tier_catalog()],
        providers=[ProviderBrief(**row) for row in provider_catalog()],
        catalog=[ModelChoiceBrief(**choice) for choice in model_catalog()],
        efforts=list(EFFORTS),
        notes=list(MODEL_ESCALATION_NOTES),
        default=ModelDefaultBrief(
            **default.as_dict(),
            table=[
                RoleModelBrief(**row)
                for row in role_catalog(
                    default.provider, default.tier, overrides=default.overrides
                )
            ],
        ),
    )


def _tier_brief(row: dict[str, Any]) -> TierBrief:
    """One tier, with its pinned model's CLI checked against the probe.

    The engine's `tier_catalog()` says which CLIs a tier needs; whether they are *there* is a
    fact about this machine, so it is answered here, from the same cached probe the harness
    block uses (no extra spawn — `cached_probe_cli` is memoized per command for 60s).

    A missing CLI makes the tier unavailable rather than absent: the UI has to be able to show
    a tier it cannot currently run and say why, because "Low is missing from the control" is
    indistinguishable from a bug, while "Low needs the codex CLI" is an instruction.
    """
    missing = [
        command for command in row["requires_harness"] if not cached_probe_cli(command).installed
    ]
    return TierBrief(
        **row,
        available=not missing,
        unavailable_reason=(
            f"needs the {', '.join(missing)} CLI installed and signed in; every step of this "
            f"tier runs {', '.join(sorted(set(row['pinned_models'].values())))} through it"
            if missing
            else None
        ),
    )


class CapabilityStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    checked_at: datetime
    notes: str | None = None


class GroundingCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    built_in_web: CapabilityStatus
    perplexity: CapabilityStatus


class HarnessCapabilityStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    installed: bool
    executable: str | None
    version: str | None
    real_runs_enabled: bool
    checked_at: datetime
    notes: str | None = None


class HarnessCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claude: HarnessCapabilityStatus
    codex: HarnessCapabilityStatus


@router.get("/grounding", response_model=GroundingCapabilitiesResponse)
def grounding_capabilities(_current: CurrentUser) -> GroundingCapabilitiesResponse:
    checked_at = datetime.now(UTC)
    return GroundingCapabilitiesResponse(
        built_in_web=CapabilityStatus(
            available=True,
            checked_at=checked_at,
            notes="Built-in harness web tools are allowed by prompt instruction.",
        ),
        perplexity=CapabilityStatus(
            available=False,
            checked_at=checked_at,
            notes="Perplexity integration is not enabled in the fake-first runtime.",
        ),
    )


@router.get("/harnesses", response_model=HarnessCapabilitiesResponse)
def harness_capabilities(
    settings: SettingsDep, _current: CurrentUser
) -> HarnessCapabilitiesResponse:
    checked_at = datetime.now(UTC)
    return HarnessCapabilitiesResponse(
        claude=_harness_status("claude", checked_at, settings.real_harness_enabled),
        codex=_harness_status("codex", checked_at, settings.real_harness_enabled),
    )


def _harness_status(
    command: str,
    checked_at: datetime,
    real_runs_enabled: bool,
) -> HarnessCapabilityStatus:
    probe = probe_cli(command)
    available = probe.installed and probe.error is None and real_runs_enabled
    notes = probe.error
    if probe.installed and probe.error is None and not real_runs_enabled:
        notes = "real_harness_disabled"

    return HarnessCapabilityStatus(
        available=available,
        installed=probe.installed,
        executable=probe.executable,
        version=probe.version,
        real_runs_enabled=real_runs_enabled,
        checked_at=checked_at,
        notes=notes,
    )

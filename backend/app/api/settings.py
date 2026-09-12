"""`GET`/`PUT /api/settings/models` — the system-wide default the top bar edits.

Two routes over one stored value. What is worth knowing about them:

* **The value holds one override bucket per tier.** Saving redefines the tier that is
  selected, so `tiers` is the field a client edits and `overrides` is a derived mirror of the
  selected tier's bucket, served so the launcher, the workshop and the wizard keep reading one
  block as they always did. `PUT` accepts either: `tiers` is the shape, and a body carrying
  `overrides` is read as the selected tier's bucket, which is what it meant when there was
  only one. Both are validated by resolving **every** bucket, so a bad row in Med is refused
  while High is selected.

* **`PUT` validates by resolving.** The body goes through the same `resolve_model_table` a
  launch does, so a default that could not become a run is a 400 here — carrying the model
  policy's own message, which names the specific trap rather than saying "invalid model".
  That is why the `PUT` body types provider, tier, role key, model and effort as plain
  strings: typing them as the allowed literals reads like extra safety and is the opposite,
  because Pydantic then refuses the body first and answers with a generic 422 listing the
  four allowed ids — the one answer that cannot say `claude-fable-5` silently runs Opus 5.
  The shape is still strict (strings, and no field an override does not have); only the
  *vocabulary* is left to the layer that owns it.
* **The setting is global.** Everyone can read it; only a live gateway/local admin can
  replace it.
* **Both return the resolved table**, not just the three fields that produced it. The editor
  and the wizard both render a table; deriving it in two clients from `{provider, tier,
  overrides}` is how two screens come to show different tables for one setting.
* **The effort a step can run at is a property of its model.** Each model publishes its own
  ladder in `/api/capabilities`; a picker must read the ladder off the selected model rather
  than off its provider, because the two providers' models do not have to agree and will not
  forever.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import ApiError, bad_request
from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser
from app.engine.models import ALLOWED_MODELS, DEFAULT_PROVIDER, EFFORTS, ModelPolicyError
from app.engine.runners import DEFAULT_MODEL_TIER
from app.schemas.dto import ModelTier, Provider
from app.schemas.launch import ModelOverrideIn
from app.services.settings.models import (
    ModelSettings,
    store_model_settings,
    stored_model_settings,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])
SettingsDep = Annotated[Settings, Depends(get_settings)]

__all__ = [
    "ModelOverrideSettingIn",
    "ModelSettingsResponse",
    "ModelSettingsUpdate",
    "router",
]


class ModelSettingsRow(BaseModel):
    """One resolved step of the default table."""

    model_config = ConfigDict(extra="forbid")

    role: str
    model: Literal[ALLOWED_MODELS]  # type: ignore[valid-type]
    effort: Literal[EFFORTS]  # type: ignore[valid-type]
    note: str
    grounded: bool
    model_class: Literal["heavy", "light"]


class ModelOverrideSettingIn(BaseModel):
    """One role's model and/or effort on the **settings** body. Deliberately plain strings.

    Not `schemas.launch.ModelOverrideIn`, which types both fields as the literal allowlist.
    That is right for a launch — a run must never reach the launcher naming a model this
    engine refuses — and wrong here, because this endpoint's entire contract is that a
    default which could not become a run comes back carrying the *model policy's own*
    sentence. A literal union answers `claude-fable-5` with "Input should be
    'claude-opus-5', 'fable', 'gpt-5.6-luna' or 'gpt-5.6-sol'", which is true and useless:
    the fact worth telling anybody is that the CLI accepts that id and silently runs Opus 5,
    and it lives in `MODEL_TRAPS`, one layer below where Pydantic stops.

    So the values go through as strings and `validate_model`/`validate_effort` refuse them
    with a reason. `extra="forbid"` stays: an override carries these two fields and nothing
    else, and an invented field is a malformed request rather than a policy question.
    """

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    effort: str | None = None


class ModelSettingsUpdate(BaseModel):
    """`PUT` body. Every field optional; an absent one keeps the built-in default's value.

    `overrides` may name either provider's model on any row — mixing providers inside one
    table is the point of the field. The effort named for a row is checked against the
    engine's vocabulary in the service and against **the chosen model's own ladder** when the
    table is resolved, which is where a level the model does not have gets clamped, visibly,
    into the table this endpoint returns.

    Provider, tier and the override role keys are strings for the same reason the two fields
    of `ModelOverrideSettingIn` are: every one of them has an engine validator whose refusal
    names the value *and* the alternatives, and a wire enum in front of that validator
    replaces the message with a list. Tightening any of these back into an enum silently
    turns this endpoint's 400s into generic 422s again.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = DEFAULT_PROVIDER
    tier: str = DEFAULT_MODEL_TIER

    tiers: dict[str, dict[str, ModelOverrideSettingIn]] = Field(default_factory=dict)
    """Per-tier override buckets, keyed by tier name then by role. **The field to send.**

    Sparse: a bucket holds the rows somebody changed, and a tier that is absent or empty
    follows the built-in table for that tier. A `PUT` replaces the whole block, so a client
    sends every bucket it wants kept — the editor holds them all anyway.

    Every bucket present is validated by resolving it *at its own tier*, which is why a bad
    row in a preset nobody has switched to yet is still a 400. The tier keys are strings for
    the same reason the tier field is: `normalise_tier`'s refusal names the tiers this build
    has, and a wire enum in front of it would answer with a bare list instead.
    """

    overrides: dict[str, ModelOverrideSettingIn] = Field(default_factory=dict)
    """The pre-per-tier shape, still accepted: read as the bucket for `tier`.

    Kept because a client that has not been updated must not silently wipe the preset it is
    editing, and because it is what the stored rows written before this change say. Sending it
    *and* a `tiers` entry for the same tier that disagrees is a 400 rather than a guess.
    """


class ModelSettingsResponse(BaseModel):
    """The stored default and the table it resolves to.

    `source` is `stored` once the editor has saved anything and `built_in` before that, so a
    UI can tell "this is what the system does" from "this is what somebody chose".

    `tiers` is the stored per-tier buckets and `overrides` is a **read-only mirror** of
    `tiers[tier]` — the same object the launcher and the workshop read, served so a client that
    only cares about the tier in force does not have to index the buckets itself. It is derived
    on every response rather than stored, so the mirror cannot fall out of step with what it
    mirrors. `table` is the resolved table for the **selected** tier.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Provider
    tier: ModelTier
    tiers: dict[ModelTier, dict[str, ModelOverrideIn]]
    overrides: dict[str, ModelOverrideIn]
    source: Literal["stored", "built_in"]
    updated_at: str | None = None
    table: list[ModelSettingsRow]


def _response(settings: ModelSettings) -> ModelSettingsResponse:
    from app.engine.runners import role_catalog

    payload: dict[str, Any] = settings.as_dict()
    payload["table"] = role_catalog(
        settings.provider, settings.tier, overrides=settings.overrides
    )
    return ModelSettingsResponse(**payload)


@router.get("/models", response_model=ModelSettingsResponse)
def get_model_settings(current: CurrentUser) -> ModelSettingsResponse:
    """The default every new run and workshop inherits when it does not state its own."""
    return _response(stored_model_settings())


@router.put("/models", response_model=ModelSettingsResponse)
def put_model_settings(body: ModelSettingsUpdate, current: CurrentUser) -> ModelSettingsResponse:
    """Replace the default. Refuses anything that could not become a run.

    A whole-value replace rather than a patch: the editor always holds the complete table,
    and a partial update would make "what is the default" depend on the order two tabs saved
    in. `tiers` is part of that value — a save is every preset the editor is holding, not the
    selected one alone.

    Every refusal is `400 model_policy` carrying the engine's own sentence — the model trap,
    the allowlist, the effort rungs, the role names, the providers this build can run. The
    `except` below is the endpoint's contract, not a safety net, which is why the body's
    fields are strings: an enum in front of it would make this branch unreachable and hand
    the client a 422 that names alternatives without naming the problem.
    """
    if not current.identity.is_admin:
        raise ApiError(403, "admin_required", "Only an administrator can change model settings.")
    try:
        saved = store_model_settings(body.model_dump(mode="json", exclude_none=False))
    except ModelPolicyError as exc:
        raise bad_request("model_policy", str(exc)) from exc
    return _response(saved)

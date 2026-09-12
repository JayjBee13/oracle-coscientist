"""The system-wide model default: what a new run gets when it does not say.

One stored value, `{provider, tier, tiers}`, written by the top bar's editor and read by
every launch and every workshop that did not state its own.

**`tiers` is one bucket of per-role overrides per tier, and it is sparse.** Saving in the
editor redefines *the tier that is selected* — "if set to high and user readjusts and saves
this becomes the new 'high' setting. same for max and med" (owner, 2026-08-17) — so a run at
High and a run at Med inherit different tables from one stored value. Sparse because a bucket
holds only the rows somebody actually changed: a role nobody edited keeps following the
built-in table, and so it keeps following it after the catalog moves under it. A missing or
empty bucket therefore means *pristine*, never "empty table", and is not stored at all.

`overrides` is still served, as a read-only mirror of `tiers[tier]`: it is what the launcher,
the workshop and the wizard read, and it is derived so the two can never disagree. It is also
still *accepted* on a `PUT` and in a stored row, where it means `tiers[tier]` — that is the
read migration, and it is what keeps a default saved before this change from being silently
dropped.

Four things about the value are deliberate:

* **It is validated by resolving it, not by describing it.** `validate_model_settings` puts
  the block through `resolve_model_table`, the same function the launcher uses, and keeps the
  resolved table. So a default that cannot become a run cannot be saved — a `PUT` naming
  `claude-fable-5` is refused at the moment somebody sets it, with the trap spelled out,
  rather than at the moment a run made three weeks later is judged by the wrong model. This
  layer is therefore the *only* place the vocabulary is checked: `api/settings.py` takes the
  provider, tier, role keys, models and efforts as plain strings on purpose, because a wire
  enum in front of these functions would refuse the body first and replace every sentence
  below with a list of allowed values.
* **Every bucket is resolved, not only the active one.** A bad row in the Med preset is
  refused while High is selected, because the alternative is a save that succeeds and a
  *later* tier switch that cannot — a 400 nobody can connect to the edit that caused it,
  arriving at the moment somebody wanted a run.
* **Reading is total.** `stored_model_settings` answers with the built-in default when the
  row is missing, when the database is unreachable, and when the row holds something this
  build cannot resolve — logging, in the last two cases, rather than raising. A launch must
  not fail because a *preference* is unreadable; the built-in default is a real, runnable
  table, and a run that quietly used it is far better than a run that never started.
* **It is not a run's model table.** A run freezes its own table at create time and never
  re-reads this. Changing the default changes what the *next* run inherits and nothing about
  any run that exists — which is the only way "the models a run used" can stay a fact.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.engine_models import AppSetting
from app.db.session import get_session_factory
from app.engine.models import (
    DEFAULT_PROVIDER,
    ModelPolicyError,
    normalise_provider,
    validate_effort,
    validate_model,
)
from app.engine.runners import (
    DEFAULT_MODEL_TIER,
    MODEL_TIERS,
    ROLES,
    TIER_LABELS,
    normalise_tier,
    resolve_model_table,
)

__all__ = [
    "MODEL_SETTINGS_KEY",
    "ModelSettings",
    "stored_model_settings",
    "store_model_settings",
    "validate_model_settings",
]

log = logging.getLogger(__name__)

MODEL_SETTINGS_KEY = "models.default"
"""Dotted, and the dot is doing work: `app_settings` is keyed by name for the whole
application, so the first key names its area rather than claiming the bare word `models` for
whatever this one setting happens to hold today."""


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """The default, plus where it came from. `source` is what the UI needs to say
    "system default" versus "never set, showing the built-in one"."""

    provider: str
    tier: str
    tiers: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    """Per-tier override buckets, sparse: tier → role → the fields that role overrides.

    A tier missing from here, or present with an empty bucket, is pristine — it follows the
    built-in table for that tier, today and after the catalog moves."""

    source: str = "default"
    updated_at: datetime | None = None

    @property
    def overrides(self) -> dict[str, dict[str, str]]:
        """The active tier's bucket. **Derived, never stored** — a mirror, not a field.

        Everything that reads this default one tier at a time reads it here: the launcher
        (`resolve_config`), the workshop (`_model_row`), and the payload both API routes
        serve. Keeping it a property rather than a second stored copy is what makes "the
        editor saved High" and "a launch inherits High" the same fact instead of two facts
        that agree until one of them is written and the other is not."""
        return {role: dict(row) for role, row in self.tiers.get(self.tier, {}).items()}

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "tier": self.tier,
            "tiers": {
                tier: {role: dict(row) for role, row in bucket.items()}
                for tier, bucket in self.tiers.items()
            },
            "overrides": self.overrides,
            "source": self.source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def table(self) -> list[dict[str, str]]:
        """The resolved role table this default produces. Never stored — always derived, so
        it cannot go stale against a catalog that moved under it."""
        return resolve_model_table(self.provider, self.tier, overrides=self.overrides)


BUILT_IN = ModelSettings(
    provider=DEFAULT_PROVIDER, tier=DEFAULT_MODEL_TIER, tiers={}, source="built_in"
)
"""What the system does before anybody has chosen: Anthropic at the strongest tier.

Not a placeholder — it is the table every run used before this setting existed, so an
installation that never opens the editor behaves exactly as it did."""


def validate_model_settings(payload: Mapping[str, Any] | None) -> ModelSettings:
    """Normalise and prove a settings block, or raise `ModelPolicyError` saying why.

    The proof is that it resolves: any refusal the launcher would make — an unknown model,
    an unknown role, an effort outside the vocabulary, a tier that is not one — is made here
    instead, before it can be stored and inherited. **Every** bucket in `tiers` is resolved,
    at its own tier, not only the selected one: a bad row in a preset nobody has switched to
    yet is still a bad row, and refusing it now is the difference between one 400 on a save
    and one 400 on a launch weeks later.

    Reads and writes both come through here, which is where the legacy shape is migrated: a
    block carrying `overrides` and no bucket for its tier means `tiers = {tier: overrides}`.
    Same function for both directions on purpose — a migration written only on the read path
    is a migration a `PUT` from an un-upgraded client can undo.
    """
    data = dict(payload or {})
    unknown = sorted(set(data) - {"provider", "tier", "tiers", "overrides"})
    if unknown:
        raise ModelPolicyError(
            f"unknown key(s) in the models setting: {', '.join(unknown)}; "
            "expected provider, tier, tiers"
        )

    provider = normalise_provider(data.get("provider") or BUILT_IN.provider)
    tier = normalise_tier(data.get("tier") or BUILT_IN.tier)
    tiers = _clean_tiers(data.get("tiers"))

    # The legacy single block. Interpreted as the selected tier's bucket, which is what it
    # always meant — it was the only tier there was. Refused rather than merged when the same
    # body also states that tier in `tiers` and disagrees: two answers to "what is High" in
    # one request is a client bug, and picking either one silently discards a real choice.
    legacy = _clean(data.get("overrides"), where="overrides")
    if legacy:
        if tier in tiers and tiers[tier] != legacy:
            raise ModelPolicyError(
                f"the {tier!r} preset is given twice and the two disagree: once in `tiers` "
                "and once in the legacy `overrides` block. Send `tiers` alone."
            )
        tiers = {**tiers, tier: legacy}

    # The whole validation: if it cannot become a table, it is not a default. Once per bucket
    # *and* once for the selected tier, which may have no bucket at all.
    for name, bucket in tiers.items():
        resolve_model_table(provider, name, overrides=bucket)
    resolve_model_table(provider, tier, overrides=tiers.get(tier, {}))
    return ModelSettings(provider=provider, tier=tier, tiers=tiers)


def _clean_tiers(value: Any) -> dict[str, dict[str, dict[str, str]]]:
    """Normalise the per-tier buckets: known tier names, clean rows, nothing empty kept.

    Tier keys go through `normalise_tier`, so a bucket stored under a retired name
    (`standard`, `quality`) lands on the tier that name became rather than being dropped —
    the same courtesy stored run configs already get. Two keys that normalise onto one tier
    are refused instead: they are two answers to "what is High", and keeping whichever came
    last would discard a real choice on dictionary order.

    An empty bucket is dropped rather than stored. `{}` and "absent" have to mean the same
    thing — pristine, following the built-in table — because a stored empty object would
    otherwise read as a *decision* that this tier has no overrides, and freeze a tier's rows
    against a catalog that moved.
    """
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise ModelPolicyError("tiers must be an object keyed by tier name")

    cleaned: dict[str, dict[str, dict[str, str]]] = {}
    seen: set[str] = set()
    for name, bucket in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ModelPolicyError(
                f"tier name {name!r} is not a tier; expected one of {', '.join(MODEL_TIERS)}"
            )
        tier = normalise_tier(name)
        if tier in seen:
            raise ModelPolicyError(
                f"the {tier!r} preset is given twice in `tiers` (once as {name!r}); "
                "one bucket per tier"
            )
        seen.add(tier)
        rows = _clean(bucket, where=f"the {TIER_LABELS[tier]} preset")
        if rows:
            cleaned[tier] = rows
    return cleaned


def _clean(value: Any, *, where: str = "overrides") -> dict[str, dict[str, str]]:
    """Keep only the two fields an override may carry, and refuse a role that is not one.

    `where` names the block the bad value was written in — "the Med preset" rather than
    "overrides" — because an editor holding four presets at once is exactly the place where
    knowing *which* one was rejected is the difference between a fixable error and a puzzle.

    An unknown role is refused rather than dropped for the same reason the launcher refuses
    it: a typo'd role name is a request that did not do what its author meant, and a saved
    default that silently ignored half of it is worse than a rejected one.

    The model and the effort are put through the engine's own validators here rather than
    left for `resolve_model_table` to catch a few lines later. Same refusal either way, but
    this one names the *role* the bad value was written against, and it does not depend on
    the resolver happening to visit every role — which is the property that would quietly
    stop holding the day a role is retired from `ROLES` while an old default still names it.
    """
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise ModelPolicyError(f"{where} must be an object keyed by role")

    unknown = sorted(set(map(str, value)) - set(ROLES))
    if unknown:
        raise ModelPolicyError(
            f"unknown role(s) in {where}: {', '.join(unknown)}; "
            f"expected any of {', '.join(ROLES)}"
        )

    cleaned: dict[str, dict[str, str]] = {}
    for role, row in value.items():
        if not isinstance(row, Mapping):
            raise ModelPolicyError(f"override for {role!r} in {where} must be an object")
        extra = sorted(set(map(str, row)) - {"model", "effort"})
        if extra:
            raise ModelPolicyError(
                f"override for {role!r} in {where} has unexpected field(s): "
                f"{', '.join(extra)}"
            )
        kept: dict[str, str] = {}
        if row.get("model") not in (None, ""):
            kept["model"] = _checked(role, validate_model, row["model"], where=where)
        if row.get("effort") not in (None, ""):
            kept["effort"] = _checked(role, validate_effort, row["effort"], where=where)
        if kept:
            cleaned[str(role)] = kept
    return cleaned


def _checked(
    role: Any, validator: Callable[[Any], str], value: Any, *, where: str = "overrides"
) -> str:
    """Run one engine validator, and say which row the value that failed was written on.

    The engine's refusals name the value and the alternatives, which is everything a person
    needs except *where* — and an editor showing nine rows at once is exactly the place that
    matters. The engine's sentence is kept verbatim so the trap text still arrives whole.
    """
    try:
        return validator(value)
    except ModelPolicyError as exc:
        raise ModelPolicyError(f"override for {str(role)!r} in {where}: {exc}") from exc


# ------------------------------------------------------------------------------ storage


def _factory(
    session_factory: sessionmaker[Session] | None, settings: Settings | None
) -> sessionmaker[Session]:
    return session_factory or get_session_factory(settings)


def stored_model_settings(
    *,
    session_factory: sessionmaker[Session] | None = None,
    settings: Settings | None = None,
) -> ModelSettings:
    """The stored default, or the built-in one. Never raises.

    Called on the launch path, so an unreadable preference degrades to the built-in default
    with a log line rather than refusing to start a run. The row is validated on the way out
    as well as on the way in: a catalog that dropped a model since the default was saved
    would otherwise hand the launcher a table it will refuse anyway, one layer later and with
    a worse message.

    A row written before per-tier presets existed carries `overrides` and no `tiers`;
    `validate_model_settings` reads it as that row's own tier's bucket, so the owner's saved
    choice survives the upgrade rather than reading as "no overrides anywhere". The row itself
    is left alone until the next save — a read that rewrote what it read would put a write on
    the launch path, and this function's whole contract is that it cannot fail one.
    """
    try:
        with _factory(session_factory, settings)() as session:
            row = session.execute(
                select(AppSetting).where(AppSetting.key == MODEL_SETTINGS_KEY)
            ).scalar_one_or_none()
            if row is None:
                return BUILT_IN
            value, updated_at = dict(row.value or {}), row.updated_at
    except Exception:  # noqa: BLE001 — a preference must never be able to stop a run
        log.exception("could not read the stored model settings; using the built-in default")
        return BUILT_IN

    try:
        resolved = validate_model_settings(value)
    except ModelPolicyError:
        log.exception(
            "the stored model settings no longer resolve (%r); using the built-in default",
            value,
        )
        return BUILT_IN
    if "overrides" in value and not value.get("tiers"):
        log.debug(
            "read the model settings in the pre-per-tier shape; the stored `overrides` block "
            "is the %r preset until the next save",
            resolved.tier,
        )
    return ModelSettings(
        provider=resolved.provider,
        tier=resolved.tier,
        tiers=resolved.tiers,
        source="stored",
        updated_at=updated_at,
    )


def store_model_settings(
    payload: Mapping[str, Any] | None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    settings: Settings | None = None,
) -> ModelSettings:
    """Validate and save the default. Raises `ModelPolicyError` for anything unrunnable.

    An upsert rather than a read-modify-write: two tabs saving at once should leave the
    later one's choice, not a unique-violation traceback.

    A whole-value replace, `tiers` included — the editor holds every preset it is showing, so
    a save is "this is the default now" rather than a patch on one tier. That also means the
    legacy body shape still does exactly what it says: `{provider, tier, overrides}` from an
    un-upgraded client sets that tier's bucket and clears the others, which is the same thing
    it did when there was only one bucket to clear.

    `overrides` is not stored — it is derived from `tiers[tier]` on the way out. Storing both
    is how the two come to disagree.
    """
    resolved = validate_model_settings(payload)
    stored = {
        "provider": resolved.provider,
        "tier": resolved.tier,
        "tiers": resolved.tiers,
    }
    with _factory(session_factory, settings)() as session:
        statement = (
            insert(AppSetting)
            .values(key=MODEL_SETTINGS_KEY, value=stored)
            .on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": stored, "updated_at": datetime.now().astimezone()},
            )
            .returning(AppSetting.updated_at)
        )
        updated_at = session.execute(statement).scalar_one()
        session.commit()
    return ModelSettings(
        provider=resolved.provider,
        tier=resolved.tier,
        tiers=resolved.tiers,
        source="stored",
        updated_at=updated_at,
    )

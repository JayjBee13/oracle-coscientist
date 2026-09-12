"""Derived run-card metadata from the immutable config and append-only event log."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.engine.runners import MODEL_TIERS, normalise_tier, resolve_model_table

__all__ = ["elapsed_seconds", "model_level"]


def model_level(config: Mapping[str, Any] | None, *, source: str) -> tuple[str | None, bool]:
    """Return the nearest current preset and whether the effective table is custom.

    Runs freeze their resolved model table at launch. For a customized run, comparing that
    table with every canonical tier gives a more useful approximation than repeating the
    base tier it started from. An uncustomized old run keeps its saved tier even if today's
    model catalog has moved on, because catalog drift must not rewrite its history.
    """
    if source != "app" or not config:
        return None, False

    raw_tier = config.get("model_tier")
    if not isinstance(raw_tier, str) or not raw_tier.strip():
        return None, False
    try:
        base = normalise_tier(raw_tier)
    except (KeyError, TypeError, ValueError):
        return None, False

    overrides = config.get("model_overrides")
    has_overrides = isinstance(overrides, Mapping) and bool(overrides)
    actual = _table(config.get("model_table"))
    if not actual:
        return base, has_overrides

    provider = config.get("provider")
    if not isinstance(provider, str) or not provider:
        return base, has_overrides

    canonical: dict[str, dict[str, tuple[str, str]]] = {}
    try:
        for tier in MODEL_TIERS:
            canonical[tier] = _table(resolve_model_table(provider, tier))
    except (KeyError, TypeError, ValueError):
        return base, has_overrides

    if actual == canonical[base]:
        return base, False
    if not has_overrides:
        return base, False

    # Put the saved tier first so an exact tie stays stable instead of changing merely
    # because MODEL_TIERS is reordered. Missing or extra roles count as two differences,
    # the same weight as changing both a row's model and effort.
    candidates = (base, *(tier for tier in MODEL_TIERS if tier != base))
    nearest = min(candidates, key=lambda tier: _table_distance(actual, canonical[tier]))
    return nearest, True


def elapsed_seconds(events: Sequence[Mapping[str, Any]], *, lifecycle: str) -> int | None:
    """Wall elapsed time from the first start through the final matching finish.

    Pauses and time between continuations are included. A run that has been continued is
    active again, so an older ``run_finished`` event must not freeze its displayed timer.
    """
    started: datetime | None = None
    finished: datetime | None = None
    finished_lifecycle: str | None = None

    for event in events:
        type_ = event.get("type")
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        ts = _datetime(event.get("ts"))
        if ts is None:
            continue
        if type_ == "lifecycle_changed" and payload.get("lifecycle") == "running":
            if started is None:
                started = ts
            # Continuing a finished run invalidates that earlier endpoint. Keep the first
            # start so a later finish still reports the whole run, including the gap.
            finished = None
            finished_lifecycle = None
        elif type_ == "run_finished":
            finished = ts
            value = payload.get("lifecycle")
            finished_lifecycle = value if isinstance(value, str) else None

    if (
        started is None
        or finished is None
        or finished_lifecycle != lifecycle
        or finished < started
    ):
        return None
    return int((finished - started).total_seconds())


def _table(value: Any) -> dict[str, tuple[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return {}
    table: dict[str, tuple[str, str]] = {}
    for row in value:
        if not isinstance(row, Mapping):
            continue
        role, model, effort = row.get("role"), row.get("model"), row.get("effort")
        if all(isinstance(item, str) and item for item in (role, model, effort)):
            table[str(role)] = (str(model), str(effort))
    return table


def _table_distance(
    actual: Mapping[str, tuple[str, str]], expected: Mapping[str, tuple[str, str]]
) -> int:
    score = 0
    for role in actual.keys() | expected.keys():
        left = actual.get(role)
        right = expected.get(role)
        if left is None or right is None:
            score += 2
            continue
        score += int(left[0] != right[0]) + int(left[1] != right[1])
    return score


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None

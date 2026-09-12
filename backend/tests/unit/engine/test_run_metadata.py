from datetime import UTC, datetime, timedelta

from app.engine.run_metadata import elapsed_seconds, model_level
from app.engine.runners import resolve_model_table


def test_custom_level_uses_the_nearest_effective_preset() -> None:
    config = {
        "provider": "openai",
        "model_tier": "max",
        "model_overrides": {"generation": {"effort": "low"}},
        # A heavily edited Max setup whose effective table is exactly Med.
        "model_table": resolve_model_table("openai", "med"),
    }

    assert model_level(config, source="app") == ("med", True)


def test_effectively_canonical_table_is_not_called_custom() -> None:
    config = {
        "provider": "openai",
        "model_tier": "high",
        "model_overrides": {"generation": {"effort": "high"}},
        "model_table": resolve_model_table("openai", "high"),
    }

    assert model_level(config, source="app") == ("high", False)


def test_legacy_tier_survives_catalog_drift_and_imports_remain_unknown() -> None:
    old_table = [dict(row) for row in resolve_model_table("anthropic", "high")]
    old_table[0]["model"] = "retired-model-from-that-release"
    config = {
        "provider": "anthropic",
        "model_tier": "standard",
        "model_overrides": {},
        "model_table": old_table,
    }

    assert model_level(config, source="app") == ("high", False)
    assert model_level(config, source="imported") == (None, False)


def test_elapsed_time_uses_first_start_and_final_finish_including_pause() -> None:
    start = datetime(2026, 9, 10, 12, 39, 35, tzinfo=UTC)
    events = [
        _event("lifecycle_changed", start, lifecycle="running"),
        _event("lifecycle_changed", start + timedelta(minutes=10), lifecycle="paused"),
        _event("lifecycle_changed", start + timedelta(minutes=20), lifecycle="running"),
        _event(
            "run_finished",
            start + timedelta(hours=1, minutes=10, seconds=54),
            lifecycle="completed",
        ),
    ]

    assert elapsed_seconds(events, lifecycle="completed") == 4254


def test_continuation_invalidates_an_earlier_finish_until_it_finishes_again() -> None:
    start = datetime(2026, 9, 10, tzinfo=UTC)
    events = [
        _event("lifecycle_changed", start, lifecycle="running"),
        _event("run_finished", start + timedelta(minutes=5), lifecycle="completed"),
        _event("lifecycle_changed", start + timedelta(minutes=10), lifecycle="running"),
    ]

    assert elapsed_seconds(events, lifecycle="completed") is None


def test_missing_start_or_finish_has_no_invented_elapsed_time() -> None:
    now = datetime(2026, 9, 10, tzinfo=UTC)

    assert elapsed_seconds([], lifecycle="completed") is None
    assert elapsed_seconds(
        [_event("lifecycle_changed", now, lifecycle="running")], lifecycle="running"
    ) is None


def _event(type_: str, ts: datetime, **payload: str) -> dict[str, object]:
    return {"type": type_, "payload": payload, "ts": ts}

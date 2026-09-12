from fastapi.testclient import TestClient

from app.api import capabilities
from app.core.config import Settings
from app.engine.models import EFFORTS, PROVIDERS, model_catalog
from app.engine.runners import (
    MODEL_ESCALATION_NOTES,
    MODEL_TIERS,
    normalise_tier,
    role_catalog,
    tier_catalog,
)
from app.main import create_app
from app.services.harness import probes as harness_probes
from app.services.harness.probes import CliProbeResult
from app.services.settings.models import stored_model_settings


def test_harness_capabilities_probe_installed_but_disabled_by_default(monkeypatch):
    monkeypatch.setattr(
        capabilities,
        "probe_cli",
        lambda command: CliProbeResult(
            installed=True,
            executable=f"C:/Tools/{command}.cmd",
            version=f"{command} version",
        ),
    )
    client = TestClient(create_app(Settings(APP_ENV="test", REAL_HARNESS_ENABLED=False)))

    response = client.get("/api/capabilities/harnesses")

    assert response.status_code == 200
    payload = response.json()
    assert payload["claude"]["installed"] is True
    assert payload["claude"]["available"] is False
    assert payload["claude"]["real_runs_enabled"] is False
    assert payload["claude"]["notes"] == "real_harness_disabled"
    assert payload["codex"]["version"] == "codex version"


def test_harness_capabilities_available_when_cli_installed_and_real_gate_enabled(monkeypatch):
    monkeypatch.setattr(
        capabilities,
        "probe_cli",
        lambda command: CliProbeResult(
            installed=True,
            executable=f"C:/Tools/{command}.cmd",
            version=f"{command} version",
        ),
    )
    client = TestClient(create_app(Settings(APP_ENV="test", REAL_HARNESS_ENABLED=True)))

    response = client.get("/api/capabilities/harnesses")

    assert response.status_code == 200
    payload = response.json()
    assert payload["claude"]["available"] is True
    assert payload["codex"]["available"] is True
    assert payload["codex"]["real_runs_enabled"] is True


def test_capabilities_summary_reports_grounding_and_both_harnesses(monkeypatch):
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=True, executable=f"/bin/{command}", version=f"{command} 1.2.3"
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    response = client.get("/api/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["grounding"] == {"web": True, "perplexity": False}
    assert payload["harnesses"] == {
        "claude": {"installed": True, "version": "claude 1.2.3"},
        "codex": {"installed": True, "version": "codex 1.2.3"},
    }


def test_capabilities_summary_serves_the_engines_own_model_table(monkeypatch):
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=True, executable=f"/bin/{command}", version=None
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    models = client.get("/api/capabilities").json()["models"]

    # The tier a run gets when the launch request does not name one. Read from the *stored*
    # system default, so it is the tier the top bar promised rather than a constant that
    # could drift from it — and always one the payload carries a table for.
    default = stored_model_settings()
    assert models["default_tier"] == normalise_tier(default.tier)
    assert models["default_provider"] == default.provider
    assert set(models["tiers"]) == set(PROVIDERS)
    for provider in PROVIDERS:
        assert set(models["tiers"][provider]) == set(MODEL_TIERS)
        assert models["default_tier"] in models["tiers"][provider]
        # Row for row, what the engine will actually resolve — the point of serving it.
        for tier in MODEL_TIERS:
            assert models["tiers"][provider][tier] == role_catalog(provider, tier)
    assert models["notes"] == list(MODEL_ESCALATION_NOTES)
    # The persisted default itself, resolved, so the wizard and the top bar agree on one
    # round trip instead of each deriving a table from three fields.
    assert models["default"]["provider"] == default.provider
    assert models["default"]["table"] == role_catalog(
        default.provider, default.tier, overrides=default.overrides
    )
    # The per-tier buckets and the mirror of the one in force, so the wizard and the top bar
    # can render every preset from the same round trip that told them which one is selected.
    assert models["default"]["tiers"] == default.tiers
    assert models["default"]["overrides"] == default.overrides


def test_capabilities_summary_publishes_what_choosing_each_tier_does(monkeypatch):
    """A control offering four tiers reads them here instead of hard-coding four names and a
    sentence each. `low` is why the block exists at all: it is the one tier that changes which
    model a step runs on, and a model change nobody can see is the failure the model policy is
    built to refuse — so the pin and the CLI it needs are published."""
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=True, executable=f"/bin/{command}", version=None
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    served = client.get("/api/capabilities").json()["models"]["tier_catalog"]

    assert [row["id"] for row in served] == list(MODEL_TIERS), "strongest first"
    engine = {row["id"]: row for row in tier_catalog()}
    for row in served:
        # Served verbatim from the engine, with only availability added on top.
        assert row["label"] == engine[row["id"]]["label"]
        assert row["note"] == engine[row["id"]]["note"]
        assert row["pinned_models"] == engine[row["id"]]["pinned_models"]
        assert row["requires_harness"] == engine[row["id"]]["requires_harness"]
        assert row["available"] is True, "both CLIs are installed in this test"
        assert row["unavailable_reason"] is None


def test_a_tier_whose_cli_is_missing_is_reported_unavailable_rather_than_hidden(monkeypatch):
    """"Low is missing from the control" is indistinguishable from a bug. "Low needs the codex
    CLI" is an instruction — and it arrives before a run rather than from one that failed on
    its first call."""
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=False, executable=None, version=None, error="not_found"
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    tiers = {
        row["id"]: row for row in client.get("/api/capabilities").json()["models"]["tier_catalog"]
    }

    assert tiers["low"]["available"] is False
    assert "codex" in (tiers["low"]["unavailable_reason"] or "")
    assert "gpt-5.6-luna" in (tiers["low"]["unavailable_reason"] or ""), (
        "and what that CLI would be running"
    )
    # A tier that pins nothing needs whatever its provider needs, which the `harnesses` block
    # already reports. Marking those unavailable here would double-report one fact and make
    # every tier unavailable on a machine that is missing either CLI.
    for tier in ("max", "high", "med"):
        assert tiers[tier]["available"] is True
        assert tiers[tier]["unavailable_reason"] is None


def test_capabilities_summary_serves_the_vocabularies_a_picker_chooses_from(monkeypatch):
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=True, executable=f"/bin/{command}", version=None
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    models = client.get("/api/capabilities").json()["models"]

    # The allowlist itself, so a per-role picker cannot offer a model the engine
    # refuses — and so the price of preferring one is on screen where it is chosen.
    assert models["catalog"] == model_catalog()
    assert models["efforts"] == list(EFFORTS)
    assert [row["id"] for row in models["providers"]] == list(PROVIDERS)
    # Every model any tier resolves to is one a client is allowed to pick, at an effort that
    # model's own ladder actually has. The second half is the one that matters: a picker
    # keyed on the provider rather than on the model will eventually offer a rung the model
    # does not have, which is a 400 in the middle of a paid run.
    ladders = {choice["id"]: set(choice["efforts"]) for choice in models["catalog"]}
    for provider in PROVIDERS:
        for tier in MODEL_TIERS:
            for row in models["tiers"][provider][tier]:
                assert row["model"] in ladders
                assert row["effort"] in ladders[row["model"]], (provider, tier, row["role"])


def test_capabilities_summary_reports_an_absent_harness(monkeypatch):
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=False, executable=None, version=None, error="not_found"
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    response = client.get("/api/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["harnesses"]["claude"] == {"installed": False, "version": None}
    assert payload["harnesses"]["codex"] == {"installed": False, "version": None}


def test_capabilities_summary_caches_the_probe(monkeypatch):
    calls: list[str] = []

    def _probe(command: str, timeout_seconds: float = 3.0) -> CliProbeResult:
        calls.append(command)
        return CliProbeResult(installed=False, executable=None, version=None, error="not_found")

    monkeypatch.setattr(harness_probes, "probe_cli", _probe)
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(Settings(APP_ENV="test")))

    client.get("/api/capabilities")
    client.get("/api/capabilities")

    # One probe per command, not one per request: the second call is served from cache.
    assert calls == ["claude", "codex"]

"""`GET /health` (plan C5): status, version, a cheap db check, cached harness probes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import main as main_app
from app.core.config import Settings
from app.main import create_app
from app.services.harness import probes as harness_probes
from app.services.harness.probes import CliProbeResult


def _settings(**overrides: object) -> Settings:
    return Settings(APP_ENV="test", IMPORT_ON_STARTUP=False, **overrides)


def test_health_reports_status_version_db_ok_and_harnesses(monkeypatch):
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=True, executable=f"/bin/{command}", version=f"{command} 9.9.9"
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(_settings()))

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["db_ok"] is True
    assert payload["harnesses"] == {
        "claude": {"installed": True, "version": "claude 9.9.9"},
        "codex": {"installed": True, "version": "codex 9.9.9"},
    }
    assert isinstance(payload["active_runs"], int)


def test_health_route_has_plain_alias():
    client = TestClient(create_app(_settings()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_a_missing_harness_without_raising(monkeypatch):
    monkeypatch.setattr(
        harness_probes,
        "probe_cli",
        lambda command, timeout_seconds=3.0: CliProbeResult(
            installed=False, executable=None, version=None, error="not_found"
        ),
    )
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(_settings()))

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["harnesses"]["claude"] == {"installed": False, "version": None}
    assert payload["harnesses"]["codex"] == {"installed": False, "version": None}


def test_health_probes_are_cached_across_requests(monkeypatch):
    calls: list[str] = []

    def _probe(command: str, timeout_seconds: float = 3.0) -> CliProbeResult:
        calls.append(command)
        return CliProbeResult(installed=False, executable=None, version=None, error="not_found")

    monkeypatch.setattr(harness_probes, "probe_cli", _probe)
    harness_probes.reset_probe_cache()
    client = TestClient(create_app(_settings()))

    client.get("/health")
    client.get("/health")

    # One probe per command, not one per request: the second poll is served from cache.
    assert calls == ["claude", "codex"]


def test_optional_auth_token_allows_health_without_token():
    client = TestClient(create_app(_settings(APP_AUTH_TOKEN="secret")))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_optional_auth_token_protects_non_health_routes():
    client = TestClient(create_app(_settings(APP_AUTH_TOKEN="secret")))

    blocked = client.get("/api/capabilities/grounding")
    allowed = client.get("/api/capabilities/grounding", headers={"X-Coscientist-Token": "secret"})

    assert blocked.status_code == 401
    assert blocked.json()["code"] == "auth_required"
    assert allowed.status_code == 200


def test_managed_supervisor_shutdown_is_opt_in_and_receives_the_configured_grace(monkeypatch):
    calls: list[float] = []

    def quiesce(_store, *, grace_seconds):
        calls.append(grace_seconds)
        return {"parked": [], "refused": []}

    monkeypatch.setattr(main_app, "quiesce_supervisors", quiesce)
    settings = _settings(
        MANAGE_SUPERVISORS_ON_SHUTDOWN=True,
        SUPERVISOR_SHUTDOWN_GRACE_SECONDS=2.5,
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200

    assert calls == [2.5]

"""Phase 2/3 security invariants: identity fails closed and resources do not leak."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import Request

from app.core.config import Settings
from app.core.identity import InvalidGatewaySecret, resolve_identity
from app.db.engine_models import Run, User, Workshop
from app.db.session import get_session_factory
from app.engine.store import RunStore
from app.main import create_app
from app.services.runs.launcher import lane_busy
from tests.support.imported import fixture_settings

SECRET = "integration-gateway-secret"


def gateway_headers(username: str, *, groups: str = "") -> dict[str, str]:
    return {
        "X-Gateway-Secret": SECRET,
        "Remote-User": username,
        "Remote-Name": username.title(),
        "Remote-Email": f"{username}@example.test",
        "Remote-Groups": groups,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"IDENTITY_REQUIRE_GATEWAY": True, "IDENTITY_TRUST_HEADERS": False},
        {"IDENTITY_REQUIRE_GATEWAY": True, "GATEWAY_SECRET": None},
        {"IDENTITY_REQUIRE_GATEWAY": True, "GATEWAY_SECRET": "   "},
        {"GATEWAY_SECRET": ""},
    ],
)
def test_gateway_required_configuration_fails_before_startup(overrides):
    base = {"GATEWAY_SECRET": SECRET, **overrides}
    with pytest.raises(ValidationError):
        Settings(**base)


def test_non_ascii_gateway_header_is_an_auth_failure_not_a_server_error():
    settings = Settings(
        GATEWAY_SECRET=SECRET,
        IDENTITY_REQUIRE_GATEWAY=True,
        IMPORT_ON_STARTUP=False,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/me",
            "headers": [(b"x-gateway-secret", b"\xff")],
        }
    )

    with pytest.raises(InvalidGatewaySecret):
        resolve_identity(request, settings)


def test_legacy_local_mode_ignores_forged_identity_headers(tmp_path: Path):
    settings = fixture_settings(
        IDENTITY_REQUIRE_GATEWAY=False,
        IDENTITY_TRUST_HEADERS=False,
        LOCAL_IDENTITY_USERNAME="local-owner",
        WEB_DIST_DIR=tmp_path / "missing-dist",
    )
    response = TestClient(create_app(settings)).get(
        "/api/me", headers={"Remote-User": "mallory"}
    )

    assert response.status_code == 200
    assert response.json()["username"] == "local-owner"
    assert response.json()["source"] == "local"


@pytest.fixture
def secured(tmp_path: Path, isolated_schema):
    settings = fixture_settings(
        GATEWAY_SECRET=SECRET,
        IDENTITY_REQUIRE_GATEWAY=True,
        IDENTITY_TRUST_HEADERS=True,
        LOCAL_IDENTITY_USERNAME="local-owner",
        FRONTEND_ORIGIN="https://oracle.example.test",
        COSCIENTIST_RUNS_ROOT=tmp_path / "runs",
        WEB_DIST_DIR=tmp_path / "missing-dist",
    )
    client = TestClient(create_app(settings))
    assert client.get("/api/me", headers=gateway_headers("alice")).status_code == 200
    assert client.get("/api/me", headers=gateway_headers("bob")).status_code == 200
    with get_session_factory(settings)() as session:
        alice_id = session.execute(select(User.id).where(User.username == "alice")).scalar_one()

    artifact_root = tmp_path / "alice-run"
    artifact_root.mkdir()
    (artifact_root / "report.md").write_text("private", encoding="utf-8")
    run_id = RunStore(settings=settings).create_run(
        question="Alice's question",
        prompt="Alice's prompt",
        title="Z Alice private run",
        harness="claude",
        source="imported",
        lifecycle="completed",
        engine_run_id=f"alice-private-run-{uuid4().hex}",
        root_path=artifact_root.as_posix(),
        owner_id=alice_id,
    )
    return client, run_id, settings


def test_forged_remote_user_without_gateway_secret_fails_closed(secured):
    client, _, _ = secured
    response = client.get("/api/me", headers={"Remote-User": "mallory"})

    assert response.status_code == 401
    assert response.json()["code"] == "gateway_auth_required"
    assert response.headers["cache-control"] == "private, no-store"


def test_query_parameters_cannot_forge_gateway_identity(secured):
    client, _, _ = secured
    response = client.get(
        f"/api/me?X-Gateway-Secret={SECRET}&Remote-User=mallory"
    )

    assert response.status_code == 401
    assert response.json()["code"] == "gateway_auth_required"


def test_wrong_gateway_secret_is_401_without_local_downgrade(secured):
    client, _, _ = secured
    response = client.get(
        "/api/me",
        headers={"X-Gateway-Secret": "wrong", "Remote-User": "local-owner"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_gateway_secret"


def test_valid_gateway_secret_without_remote_user_fails_loudly(secured):
    client, _, _ = secured
    response = client.get("/api/me", headers={"X-Gateway-Secret": SECRET})

    assert response.status_code == 401
    assert response.json()["code"] == "gateway_auth_required"


def test_health_stays_public_when_gateway_is_required(secured):
    client, _, _ = secured

    assert client.get("/api/health").status_code == 200
    assert client.get("/health").status_code == 200


def test_protected_responses_are_not_shared_cacheable(secured):
    client, run_id, _ = secured

    me = client.get("/api/me", headers=gateway_headers("alice"))
    artifact = client.get(
        f"/api/artifacts/{run_id}/report.md", headers=gateway_headers("alice")
    )

    assert me.headers["cache-control"] == "private, no-store"
    assert artifact.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_unsafe_gateway_requests_reject_cross_site_origins(secured, origin):
    client, run_id, _ = secured
    response = client.patch(
        f"/api/runs/{run_id}",
        headers={**gateway_headers("alice"), "Origin": origin},
        json={"title": "Cross-site edit"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "origin_not_allowed"
    assert response.headers["cache-control"] == "private, no-store"


def test_unsafe_gateway_requests_accept_configured_or_absent_origin(secured):
    client, run_id, _ = secured
    allowed = client.patch(
        f"/api/runs/{run_id}",
        headers={
            **gateway_headers("alice"),
            "Origin": "https://oracle.example.test",
        },
        json={"title": "Allowed edit"},
    )
    non_browser = client.patch(
        f"/api/runs/{run_id}",
        headers=gateway_headers("alice"),
        json={"title": "Trusted client edit"},
    )

    assert allowed.status_code == 200, allowed.text
    assert non_browser.status_code == 200, non_browser.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/runs/{run_id}/detail"),
        ("get", "/api/runs/{run_id}/events"),
        ("post", "/api/runs/{run_id}/events/ticket"),
        ("get", "/api/artifacts/{run_id}/report.md"),
        ("get", "/api/runs/{run_id}/export.md"),
    ],
)
def test_non_admin_gets_404_for_another_users_run_surface(secured, method, path):
    client, run_id, _ = secured
    response = getattr(client, method)(
        path.format(run_id=run_id), headers=gateway_headers("bob")
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "run_not_found"


def test_admin_can_read_another_users_run(secured):
    client, run_id, _ = secured
    response = client.get(
        f"/api/runs/{run_id}/detail", headers=gateway_headers("admin", groups="admin")
    )

    assert response.status_code == 200, response.text


def test_live_groups_not_cached_user_row_control_admin_authority(secured):
    client, _, _ = secured
    headers = gateway_headers("alice", groups="admin")
    assert client.put("/api/settings/models", headers=headers, json={}).status_code == 200

    response = client.put(
        "/api/settings/models", headers=gateway_headers("alice"), json={}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


def test_gateway_admin_group_is_configurable(tmp_path: Path):
    settings = fixture_settings(
        GATEWAY_SECRET=SECRET,
        IDENTITY_REQUIRE_GATEWAY=True,
        IDENTITY_ADMIN_GROUP="oracle-operators",
        WEB_DIST_DIR=tmp_path / "missing-dist",
    )
    client = TestClient(create_app(settings))

    ordinary = client.get("/api/me", headers=gateway_headers("alice", groups="admin"))
    operator = client.get(
        "/api/me",
        headers=gateway_headers("alice", groups="scientists, oracle-operators"),
    )

    assert ordinary.json()["is_admin"] is False
    assert operator.json()["is_admin"] is True


def test_lane_conflict_redacts_another_users_run(secured):
    _, _, settings = secured
    with get_session_factory(settings)() as session:
        alice_id = session.execute(select(User.id).where(User.username == "alice")).scalar_one()
        bob_id = session.execute(select(User.id).where(User.username == "bob")).scalar_one()
    store = RunStore(settings=settings)
    run_id = store.create_run(
        question="Lane question",
        prompt="Lane prompt",
        title="Protein folding",
        harness="codex",
        source="imported",
        lifecycle="running",
        engine_run_id=f"alice-lane-{uuid4().hex}",
        owner_id=alice_id,
    )
    try:
        redacted = lane_busy(store, "codex", requester_owner_id=bob_id)
        owner_view = lane_busy(store, "codex", requester_owner_id=alice_id)
        admin_view = lane_busy(store, "codex", requester_owner_id=bob_id, reveal_conflict=True)

        assert str(redacted) == "Shared Codex capacity is currently in use. Try again later."
        assert redacted.conflicting_run_id is None
        assert owner_view.conflicting_run_id == str(run_id)
        assert "Protein folding" in str(owner_view)
        assert admin_view.conflicting_run_id == str(run_id)
    finally:
        store.set_lifecycle(run_id, "completed")


def test_lane_busy_http_response_only_identifies_a_visible_holder(
    secured, monkeypatch
):
    client, _, settings = secured
    with get_session_factory(settings)() as session:
        alice_id = session.execute(select(User.id).where(User.username == "alice")).scalar_one()
    store = RunStore(settings=settings)
    holder_id = store.create_run(
        question="Private lane holder",
        prompt="Private lane holder",
        title="Secret protein program",
        harness="codex",
        source="imported",
        lifecycle="running",
        engine_run_id=f"alice-http-lane-{uuid4().hex}",
        owner_id=alice_id,
    )
    monkeypatch.setattr("app.services.runs.controls.reconcile_runs", lambda _store: [])
    body = {
        "question": "Another run",
        "harness": "codex",
        "config": {"provider": "openai"},
    }
    try:
        hidden = client.post("/api/runs", headers=gateway_headers("bob"), json=body)
        visible = client.post("/api/runs", headers=gateway_headers("alice"), json=body)

        assert hidden.status_code == 409, hidden.text
        assert hidden.json() == {
            "code": "lane_busy",
            "message": "Shared Codex capacity is currently in use. Try again later.",
            "details": {"harness": "codex"},
        }
        assert "Secret protein program" not in hidden.text
        assert "Alice" not in hidden.text
        assert visible.status_code == 409, visible.text
        assert visible.json()["details"]["conflicting_run_id"] == str(holder_id)
    finally:
        store.set_lifecycle(holder_id, "completed")


def test_non_admin_list_contains_only_their_runs(secured):
    client, run_id, _ = secured

    alice = client.get("/api/runs", headers=gateway_headers("alice")).json()
    bob = client.get("/api/runs", headers=gateway_headers("bob")).json()

    assert str(run_id) in {item["id"] for item in alice["items"]}
    assert {item["owner_display_name"] for item in alice["items"]} == {"Alice"}
    assert bob == {"items": [], "total": 0}


def test_admin_only_sees_all_users_when_mine_is_false(secured):
    client, run_id, _ = secured
    headers = gateway_headers("carol", groups="admin")

    own = client.get("/api/runs?mine=true", headers=headers).json()
    all_users = client.get("/api/runs?mine=false", headers=headers).json()

    assert own == {"items": [], "total": 0}
    assert str(run_id) in {item["id"] for item in all_users["items"]}


def test_run_creation_uses_live_owner_and_keeps_per_run_model_config(secured, monkeypatch):
    client, _, settings = secured
    monkeypatch.setattr("app.services.runs.launcher.spawn_supervisor", lambda *a, **kw: 4242)

    response = client.post(
        "/api/runs",
        headers=gateway_headers("bob"),
        json={
            "question": "Bob's configured question",
            "harness": "demo",
            "config": {
                "runner": "demo",
                "rounds": 1,
                "provider": "openai",
                "model_tier": "med",
            },
        },
    )

    assert response.status_code == 201, response.text
    run_id = response.json()["run"]["id"]
    try:
        with get_session_factory(settings)() as session:
            run = RunStore(settings=settings).get_run(UUID(run_id))
            owner_id = session.execute(
                select(Run.owner_id).where(Run.id == UUID(run_id))
            ).scalar_one()
            owner = session.execute(select(User).where(User.id == owner_id)).scalar_one()
        assert owner.username == "bob"
        assert run["config"]["provider"] == "openai"
        assert run["config"]["model_tier"] == "med"
    finally:
        RunStore(settings=settings).set_lifecycle(UUID(run_id), "completed")


@pytest.mark.parametrize(
    ("method", "suffix", "json"),
    [
        ("patch", "", {"title": "Stolen"}),
        ("delete", "", None),
        ("post", "/note", {"text": "Injected note"}),
        ("post", "/controls", {"action": "force_stop"}),
        ("post", "/hypotheses/H0001/archive", None),
    ],
)
def test_non_admin_cannot_mutate_another_users_run(secured, method, suffix, json):
    client, run_id, _ = secured

    response = getattr(client, method)(
        f"/api/runs/{run_id}{suffix}",
        headers=gateway_headers("bob"),
        **({"json": json} if json is not None else {}),
    )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "run_not_found"


def test_compare_cannot_mix_in_another_users_run(secured):
    client, alice_run_id, settings = secured
    with get_session_factory(settings)() as session:
        bob_id = session.execute(select(User.id).where(User.username == "bob")).scalar_one()
    bob_run_id = RunStore(settings=settings).create_run(
        question="Bob's comparison",
        prompt="Bob's comparison",
        title="Bob comparison",
        harness="demo",
        source="imported",
        lifecycle="completed",
        engine_run_id=f"bob-compare-{uuid4().hex}",
        owner_id=bob_id,
    )

    response = client.get(
        f"/api/compare?baseline={bob_run_id}&challenger={alice_run_id}",
        headers=gateway_headers("bob"),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_non_admin_cannot_use_operator_endpoints(secured):
    client, _, _ = secured

    reimport = client.post("/api/admin/reimport", headers=gateway_headers("bob"))
    halt = client.post("/api/admin/halt-all", headers=gateway_headers("bob"))

    assert reimport.status_code == 403
    assert reimport.json()["code"] == "admin_required"
    assert halt.status_code == 403
    assert halt.json()["code"] == "admin_required"


def test_hypothesis_detail_is_hidden_with_its_run(secured):
    client, run_id, settings = secured
    hypothesis = RunStore(settings=settings).add_hypothesis(
        run_id, title="Private idea", body_md="Private body", created_round=1
    )

    response = client.get(
        f"/api/hypotheses/{hypothesis['id']}", headers=gateway_headers("bob")
    )

    assert response.status_code == 404
    assert response.json()["code"] == "hypothesis_not_found"


@pytest.mark.parametrize("suffix", ["", "/refine", "/choose"])
def test_workshop_routes_hide_another_users_workshop(secured, suffix):
    client, _, settings = secured
    with get_session_factory(settings)() as session:
        alice_id = session.execute(select(User.id).where(User.username == "alice")).scalar_one()
        workshop = Workshop(
            owner_id=alice_id,
            question="Private workshop",
            state="options_ready",
            harness="claude",
        )
        session.add(workshop)
        session.commit()
        workshop_id = workshop.id

    if suffix == "/refine":
        response = client.post(
            f"/api/workshops/{workshop_id}{suffix}",
            headers=gateway_headers("bob"),
            json={"base": "merge", "note": ""},
        )
    elif suffix == "/choose":
        response = client.post(
            f"/api/workshops/{workshop_id}{suffix}",
            headers=gateway_headers("bob"),
            json={"option_id": "missing", "final_prompt": "private"},
        )
    else:
        response = client.get(
            f"/api/workshops/{workshop_id}", headers=gateway_headers("bob")
        )

    assert response.status_code == 404
    assert response.json()["code"] == "workshop_not_found"

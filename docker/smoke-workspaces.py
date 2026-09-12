"""Black-box Docker smoke for strict gateway identity and private workspaces.

Uses only the scripted demo harness, a disposable PostgreSQL schema, container, and
volume. It never starts/stops Compose services and never invokes a real model CLI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LIFECYCLE_PATH = HERE / "smoke-lifecycle.py"
_spec = importlib.util.spec_from_file_location("oracle_smoke_lifecycle", LIFECYCLE_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"cannot load lifecycle smoke helpers from {LIFECYCLE_PATH}")
_lifecycle = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _lifecycle
_spec.loader.exec_module(_lifecycle)

SmokeFailure = _lifecycle.SmokeFailure
assert_port_free = _lifecycle.assert_port_free
command = _lifecycle.command
create_schema = _lifecycle.create_schema
drop_schema = _lifecycle.drop_schema
launch_body = _lifecycle.launch_body
wait_for = _lifecycle.wait_for

PUBLIC_ORIGIN = "http://localhost:18000"
SAFE_SCHEMA = re.compile(r"^smoke_[0-9a-f]{8}$")
SAFE_CONTAINER = re.compile(r"^oracle-workspace-smoke-[0-9a-f]{8}$")
SAFE_VOLUME = re.compile(r"^oracle-workspace-runs-[0-9a-f]{8}$")


def request(
    base: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    expected: int = 200,
    timeout: float = 20,
) -> tuple[dict[str, Any] | None, bytes]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    sent = dict(headers or {})
    if payload is not None:
        sent["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=payload, method=method, headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    if status != expected:
        detail = raw.decode("utf-8", errors="replace")[:800]
        raise SmokeFailure(f"{method} {path} returned {status}, expected {expected}: {detail}")
    parsed: dict[str, Any] | None = None
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                parsed = value
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return parsed, raw


def gateway_headers(secret: str, username: str, groups: str = "") -> dict[str, str]:
    return {
        "X-Gateway-Secret": secret,
        "Remote-User": username,
        "Remote-Name": username.title(),
        "Remote-Email": f"{username}@example.invalid",
        "Remote-Groups": groups,
        "Origin": PUBLIC_ORIGIN,
    }


def wait_finished(base: str, run_id: str, headers: dict[str, str]) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        detail, _ = request(base, "GET", f"/api/runs/{run_id}/detail", headers=headers)
        assert detail is not None
        lifecycle = detail["run"]["lifecycle"]
        events = {event["type"] for event in detail.get("recent_events", [])}
        if lifecycle == "completed" and "run_finished" in events:
            return detail
        if lifecycle in {"failed", "lost", "stopped"}:
            raise SmokeFailure(f"run ended unexpectedly as {lifecycle}")
        return None

    return wait_for(f"private run {run_id} completion", probe, timeout=180)


def listed_ids(base: str, headers: dict[str, str]) -> set[str]:
    body, _ = request(base, "GET", "/api/runs?include_demo=true", headers=headers)
    assert body is not None
    return {str(item["id"]) for item in body["items"]}


def error_code(body: dict[str, Any] | None) -> str | None:
    return None if body is None else body.get("code")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="oracle-coscientist:local")
    parser.add_argument("--port", type=int, default=18002)
    args = parser.parse_args()

    assert_port_free(args.port)
    database_url = schema = container = volume = ""
    gateway_secret = secrets.token_urlsafe(36)
    base = f"http://127.0.0.1:{args.port}"
    evidence: dict[str, Any] = {}

    try:
        database_url, schema = create_schema()
        suffix = schema.removeprefix("smoke_")
        container = f"oracle-workspace-smoke-{suffix}"
        volume = f"oracle-workspace-runs-{suffix}"
        if not (
            SAFE_SCHEMA.fullmatch(schema)
            and SAFE_CONTAINER.fullmatch(container)
            and SAFE_VOLUME.fullmatch(volume)
        ):
            raise SmokeFailure("derived disposable resource name failed its safety check")

        command(["docker", "volume", "create", volume])
        docker_env = dict(os.environ)
        docker_env.update(DATABASE_URL=database_url, GATEWAY_SECRET=gateway_secret)
        command(
            [
                "docker", "run", "--detach", "--name", container, "--init",
                "--read-only", "--cap-drop", "ALL", "--security-opt",
                "no-new-privileges:true", "--stop-timeout", "45", "--publish",
                f"127.0.0.1:{args.port}:8787", "--mount",
                f"type=volume,source={volume},target=/app/engines", "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1777", "--tmpfs",
                "/app/.dev:uid=10001,gid=10001", "--tmpfs",
                "/home/oracle:uid=10001,gid=10001", "--env", "DATABASE_URL",
                "--env", "GATEWAY_SECRET", "--env", "IMPORT_ON_STARTUP=false",
                "--env", "FAKE_HARNESS_ENABLED=true", "--env",
                "REAL_HARNESS_ENABLED=false", "--env", "ENGINE_RUNNER=fake", "--env",
                "COSCIENTIST_DEMO_LATENCY=2", "--env", "IDENTITY_TRUST_HEADERS=true",
                "--env", "IDENTITY_REQUIRE_GATEWAY=true", "--env",
                f"FRONTEND_ORIGIN={PUBLIC_ORIGIN}", "--env",
                "MANAGE_SUPERVISORS_ON_SHUTDOWN=true", "--env",
                "SUPERVISOR_SHUTDOWN_GRACE_SECONDS=10", args.image,
            ],
            env=docker_env,
        )

        def healthy() -> dict[str, Any] | None:
            body, _ = request(base, "GET", "/api/health")
            return body if body and body.get("db_ok") else None

        wait_for("strict workspace container health", healthy, timeout=60)
        missing, _ = request(base, "GET", "/api/me", expected=401)
        if error_code(missing) != "gateway_auth_required":
            raise SmokeFailure("missing gateway identity did not fail closed")
        spoof, _ = request(
            base, "GET", "/api/me", headers={"Remote-User": "mallory"}, expected=401
        )
        wrong, _ = request(
            base, "GET", "/api/me",
            headers={"Remote-User": "mallory", "X-Gateway-Secret": "wrong"},
            expected=401,
        )
        if error_code(spoof) != "gateway_auth_required" or error_code(wrong) != "invalid_gateway_secret":
            raise SmokeFailure("forged gateway headers were not rejected")

        alice_h = gateway_headers(gateway_secret, "alice")
        bob_h = gateway_headers(gateway_secret, "bob")
        admin_h = gateway_headers(gateway_secret, "operator", "admin")
        for name, headers in (("alice", alice_h), ("bob", bob_h)):
            identity, _ = request(base, "GET", "/api/me", headers=headers)
            if not identity or identity["username"] != name or identity["source"] != "gateway":
                raise SmokeFailure(f"gateway identity mismatch for {name}")
            if identity["is_admin"]:
                raise SmokeFailure(f"ordinary user {name} unexpectedly received admin")

        forbidden, _ = request(
            base, "PUT", "/api/settings/models", headers=alice_h, body={}, expected=403
        )
        if error_code(forbidden) != "admin_required":
            raise SmokeFailure("non-admin model-settings write was not refused")
        bad_origin = dict(alice_h, Origin="https://evil.example")
        origin_error, _ = request(
            base, "POST", "/api/runs", headers=bad_origin,
            body=launch_body("must never launch"), expected=403,
        )
        if error_code(origin_error) != "origin_not_allowed":
            raise SmokeFailure("cross-origin unsafe request was not refused")

        alice_title = "Alice private workspace smoke"
        launched, _ = request(
            base, "POST", "/api/runs", headers=alice_h, body=launch_body(alice_title), expected=201
        )
        assert launched is not None
        alice_id = str(launched["run"]["id"])

        busy, busy_raw = request(
            base, "POST", "/api/runs", headers=bob_h,
            body=launch_body("Bob blocked capacity probe"), expected=409,
        )
        if error_code(busy) != "lane_busy":
            raise SmokeFailure("shared capacity did not return lane_busy")
        busy_text = busy_raw.decode("utf-8", errors="replace")
        if alice_id in busy_text or alice_title in busy_text:
            raise SmokeFailure("busy response leaked another user's run identity or title")
        if listed_ids(base, bob_h):
            raise SmokeFailure("Bob's empty workspace exposed Alice's running run")

        foreign = [
            ("GET", f"/api/runs/{alice_id}/detail", None),
            ("GET", f"/api/runs/{alice_id}/events", None),
            ("POST", f"/api/runs/{alice_id}/events/ticket", None),
            ("GET", f"/api/artifacts/{alice_id}/report.md", None),
            ("POST", f"/api/runs/{alice_id}/controls", {"action": "pause"}),
            ("GET", f"/api/compare?baseline={alice_id}&challenger={alice_id}", None),
        ]
        for method, path, body in foreign:
            hidden, _ = request(base, method, path, headers=bob_h, body=body, expected=404)
            if error_code(hidden) != "run_not_found":
                raise SmokeFailure(f"foreign surface did not hide run: {method} {path}")

        alice_done = wait_finished(base, alice_id, alice_h)
        overview, overview_raw = request(
            base, "GET", f"/api/runs/{alice_id}/overview", headers=alice_h
        )
        _, export_raw = request(base, "GET", f"/api/runs/{alice_id}/export.md", headers=alice_h)
        if not overview_raw.strip() or not export_raw.strip():
            raise SmokeFailure("owner could not read completed report and download")

        bob_launched, _ = request(
            base, "POST", "/api/runs", headers=bob_h,
            body=launch_body("Bob private workspace smoke"), expected=201,
        )
        assert bob_launched is not None
        bob_id = str(bob_launched["run"]["id"])
        bob_done = wait_finished(base, bob_id, bob_h)
        if listed_ids(base, alice_h) != {alice_id} or listed_ids(base, bob_h) != {bob_id}:
            raise SmokeFailure("per-user run lists were not isolated")
        admin_ids = listed_ids(base, admin_h)
        if not {alice_id, bob_id}.issubset(admin_ids):
            raise SmokeFailure("explicit admin could not view both workspaces")
        for run_id in (alice_id, bob_id):
            request(base, "GET", f"/api/runs/{run_id}/detail", headers=admin_h)

        evidence = {
            "status": "PASS",
            "strict_gateway": True,
            "origin_guard": True,
            "busy_without_private_metadata": True,
            "private_surfaces_checked": len(foreign),
            "alice_calls": alice_done["run"]["calls_used"],
            "bob_calls": bob_done["run"]["calls_used"],
            "admin_cross_workspace": True,
            "report_and_download": bool(overview or overview_raw) and bool(export_raw),
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        message = str(exc)
        for sensitive in (gateway_secret, database_url):
            if sensitive:
                message = message.replace(sensitive, "<redacted>")
        print(f"Workspace smoke failed: {message}", file=sys.stderr)
        return 1
    finally:
        if container and SAFE_CONTAINER.fullmatch(container):
            command(["docker", "rm", "--force", container], timeout=30, check=False)
        if volume and SAFE_VOLUME.fullmatch(volume):
            command(["docker", "volume", "rm", volume], timeout=30, check=False)
        if schema and SAFE_SCHEMA.fullmatch(schema):
            try:
                drop_schema(schema)
            except Exception as exc:
                print(f"WARNING: failed to drop disposable schema: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

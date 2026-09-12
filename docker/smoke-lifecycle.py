"""Free Docker lifecycle smoke against a disposable PostgreSQL schema.

This launches only the scripted demo harness. It never invokes Claude or Codex, never
uses the public schema, and never starts or stops the production Compose services.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_HELPER = REPO_ROOT / "scripts" / "smoke" / "_smoke_schema.py"
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SAFE_SCHEMA = re.compile(r"^smoke_[0-9a-f]{8}$")
SAFE_CONTAINER = re.compile(r"^oracle-docker-smoke-[0-9a-f]{8}$")
SAFE_VOLUME = re.compile(r"^oracle-smoke-runs-[0-9a-f]{8}$")


class SmokeFailure(RuntimeError):
    pass


class OpenEventStream:
    """Hold an SSE response open while Docker asks uvicorn to shut down."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.connected = threading.Event()
        self.finished = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._read, name="docker-smoke-sse", daemon=True)

    def _read(self) -> None:
        try:
            with urllib.request.urlopen(self.url, timeout=30) as response:
                if response.status != 200:
                    raise SmokeFailure(f"SSE stream returned HTTP {response.status}")
                self.connected.set()
                while response.readline():
                    pass
        except Exception as exc:  # connection closure during shutdown is expected
            self.error = exc
        finally:
            self.finished.set()

    def start(self) -> None:
        self.thread.start()
        if not self.connected.wait(timeout=15):
            raise SmokeFailure(f"SSE stream did not connect: {self.error}")

    def assert_closed(self) -> None:
        if not self.finished.wait(timeout=10):
            raise SmokeFailure("SSE stream remained open after the container stopped")


def command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if check and completed.returncode != 0:
        executable = Path(args[0]).name
        raise SmokeFailure(
            f"{executable} failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()[-1200:]}"
        )
    return completed


def api(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} {path} returned {exc.code}: {detail[:1000]}") from exc
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SmokeFailure(f"{method} {path} returned a non-object JSON response")
    return value


def wait_for(
    label: str,
    probe: Callable[[], Any],
    *,
    timeout: float = 120,
    interval: float = 0.5,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except (OSError, SmokeFailure, json.JSONDecodeError) as exc:
            last = str(exc)
        time.sleep(interval)
    raise SmokeFailure(f"timed out waiting for {label}; last={last!r}")


def assert_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SmokeFailure(f"127.0.0.1:{port} is already in use") from exc


def create_schema() -> tuple[str, str]:
    if not VENV_PYTHON.is_file():
        raise SmokeFailure(f"backend virtualenv was not found at {VENV_PYTHON}")
    created = command([str(VENV_PYTHON), str(SCHEMA_HELPER), "create"], timeout=120)
    database_url = created.stdout.strip()
    options = parse_qs(urlparse(database_url).query).get("options", [])
    match = re.search(r"search_path=(smoke_[0-9a-f]{8})", " ".join(options))
    if not match or not SAFE_SCHEMA.fullmatch(match.group(1)):
        raise SmokeFailure("schema helper returned a URL without a safe smoke schema")
    return database_url, match.group(1)


def drop_schema(schema: str) -> None:
    if not SAFE_SCHEMA.fullmatch(schema):
        raise SmokeFailure(f"refusing to drop unsafe schema name {schema!r}")
    command([str(VENV_PYTHON), str(SCHEMA_HELPER), "drop", schema], timeout=60)


def launch_body(title: str) -> dict[str, Any]:
    return {
        "question": "How does a reproducible container preserve a long-running research job?",
        "title": title,
        "harness": "demo",
        "config": {
            "runner": "demo",
            "workflow": "adaptive",
            "rounds": 1,
            "generation_batch": 4,
            "matches_per_round": 1,
            "evolve_top_k": 0,
            "budget_calls": 40,
            "grounding_depth": "shallow",
            "graft": {"enabled": False},
        },
    }


def run_summary(base: str, run_id: str) -> dict[str, Any]:
    return api(base, "GET", f"/api/runs/{run_id}")


def run_detail(base: str, run_id: str) -> dict[str, Any]:
    return api(base, "GET", f"/api/runs/{run_id}/detail")


def wait_lifecycle(base: str, run_id: str, expected: set[str], timeout: float = 120) -> dict:
    def probe() -> dict[str, Any] | None:
        row = run_summary(base, run_id)
        return row if row["lifecycle"] in expected else None

    return wait_for(
        f"run {run_id} lifecycle in {sorted(expected)}",
        probe,
        timeout=timeout,
    )


def wait_finished(base: str, run_id: str) -> dict[str, Any]:
    def finished() -> dict[str, Any] | None:
        detail = run_detail(base, run_id)
        lifecycle = detail["run"]["lifecycle"]
        event_types = {event["type"] for event in detail.get("recent_events", [])}
        if lifecycle == "completed" and "run_finished" in event_types:
            return detail
        if lifecycle in {"failed", "lost", "stopped"}:
            raise SmokeFailure(f"run {run_id} ended as {lifecycle}")
        return None

    return wait_for(f"run {run_id} run_finished event", finished, timeout=180)


def assert_adaptive_deliverable(detail: dict[str, Any], run_id: str) -> None:
    research = detail.get("research") or {}
    if not str(research.get("synthesis_markdown") or "").strip():
        raise SmokeFailure(f"completed adaptive run {run_id} has no synthesis")
    if not str(research.get("challenge_assessment") or "").strip():
        raise SmokeFailure(f"completed adaptive run {run_id} has no independent challenge")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="oracle-coscientist:local")
    parser.add_argument("--port", type=int, default=18002)
    args = parser.parse_args()

    assert_port_free(args.port)
    database_url = schema = ""
    container = volume = ""
    base = f"http://127.0.0.1:{args.port}"
    evidence: dict[str, Any] = {}
    event_stream: OpenEventStream | None = None

    try:
        database_url, schema = create_schema()
        suffix = schema.removeprefix("smoke_")
        container = f"oracle-docker-smoke-{suffix}"
        volume = f"oracle-smoke-runs-{suffix}"
        if not SAFE_CONTAINER.fullmatch(container) or not SAFE_VOLUME.fullmatch(volume):
            raise SmokeFailure("derived Docker resource name failed its safety check")

        command(["docker", "volume", "create", volume])
        docker_env = dict(os.environ)
        docker_env["DATABASE_URL"] = database_url
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--init",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--stop-timeout",
                "45",
                "--publish",
                f"127.0.0.1:{args.port}:8787",
                "--mount",
                f"type=volume,source={volume},target=/app/engines",
                "--tmpfs",
                "/tmp:uid=10001,gid=10001,mode=1777",
                "--tmpfs",
                "/app/.dev:uid=10001,gid=10001",
                "--tmpfs",
                "/home/oracle:uid=10001,gid=10001",
                "--env",
                "DATABASE_URL",
                "--env",
                "IMPORT_ON_STARTUP=false",
                "--env",
                "FAKE_HARNESS_ENABLED=true",
                "--env",
                "REAL_HARNESS_ENABLED=false",
                "--env",
                "COSCIENTIST_DEMO_LATENCY=2",
                "--env",
                "MANAGE_SUPERVISORS_ON_SHUTDOWN=true",
                "--env",
                "SUPERVISOR_SHUTDOWN_GRACE_SECONDS=10",
                args.image,
            ],
            env=docker_env,
        )

        def healthy() -> dict[str, Any] | None:
            row = api(base, "GET", "/api/health")
            return row if row.get("db_ok") else None

        health = wait_for("container health with database connectivity", healthy, timeout=60)
        evidence["health"] = {
            "db_ok": health["db_ok"],
            "claude": health["harnesses"]["claude"]["version"],
            "codex": health["harnesses"]["codex"]["version"],
        }

        first = api(base, "POST", "/api/runs", launch_body("Docker smoke: pause and resume"))
        first_id = first["run"]["id"]
        wait_lifecycle(base, first_id, {"running"}, timeout=20)
        api(base, "POST", f"/api/runs/{first_id}/controls", {"action": "pause"})
        wait_lifecycle(base, first_id, {"paused"}, timeout=60)
        api(base, "POST", f"/api/runs/{first_id}/controls", {"action": "resume"})
        first_done = wait_finished(base, first_id)
        assert_adaptive_deliverable(first_done, first_id)
        evidence["pause_resume"] = {
            "run_id": first_id,
            "calls_used": first_done["run"]["calls_used"],
        }

        second = api(base, "POST", "/api/runs", launch_body("Docker smoke: restart and resume"))
        second_id = second["run"]["id"]

        def has_persisted_work() -> dict[str, Any] | None:
            detail = run_detail(base, second_id)
            run = detail["run"]
            if run["lifecycle"] != "running":
                return None
            if not detail.get("leaderboard"):
                return None
            return detail

        before_restart = wait_for("a running unit to persist", has_persisted_work, timeout=90)
        before_hids = {row["hid"] for row in before_restart["leaderboard"]}
        before_calls = before_restart["run"]["calls_used"]

        event_stream = OpenEventStream(f"{base}/api/runs/{second_id}/events?after_seq=0")
        event_stream.start()
        command(["docker", "stop", "--timeout", "45", container], timeout=60)
        event_stream.assert_closed()
        command(["docker", "start", container], timeout=30)
        wait_for("restarted container health", healthy, timeout=60)
        paused = wait_lifecycle(base, second_id, {"paused"}, timeout=30)
        if paused["id"] != second_id:
            raise SmokeFailure("restart changed the run id")
        api(base, "POST", f"/api/runs/{second_id}/controls", {"action": "resume"})
        second_done = wait_finished(base, second_id)
        assert_adaptive_deliverable(second_done, second_id)
        after_hids = {row["hid"] for row in second_done["leaderboard"]}
        if not before_hids.issubset(after_hids):
            raise SmokeFailure("restart lost previously persisted hypotheses")
        if second_done["run"]["calls_used"] < before_calls:
            raise SmokeFailure("restart moved the persisted call count backwards")
        evidence["restart_resume"] = {
            "run_id": second_id,
            "persisted_hypotheses": len(before_hids),
            "calls_before": before_calls,
            "calls_after": second_done["run"]["calls_used"],
            "open_sse_during_stop": True,
        }

        third = api(base, "POST", "/api/runs", launch_body("Docker smoke: force stop"))
        third_id = third["run"]["id"]
        wait_lifecycle(base, third_id, {"running"}, timeout=20)
        api(base, "POST", f"/api/runs/{third_id}/controls", {"action": "force_stop"}, timeout=30)
        wait_lifecycle(base, third_id, {"stopped"}, timeout=20)
        process_probe = """import os, pathlib, sys
needle = sys.argv[1].encode()
found = []
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    if path.parent.name == str(os.getpid()):
        continue
    try:
        argv = path.read_bytes().split(b'\\0')
    except OSError:
        continue
    expected = [b'-m', b'app.engine.supervisor', b'--run-id', needle]
    for index in range(max(0, len(argv) - len(expected) + 1)):
        if argv[index:index + len(expected)] == expected:
            found.append(str(path))
            break
print(len(found))
raise SystemExit(bool(found))
"""
        process_result = command(
            ["docker", "exec", container, "python", "-c", process_probe, third_id],
            timeout=20,
        )
        if process_result.stdout.strip() != "0":
            raise SmokeFailure("force-stop left a matching supervisor process alive")
        evidence["force_stop"] = {"run_id": third_id, "matching_processes": 0}

        print(json.dumps({"status": "PASS", **evidence}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Docker lifecycle smoke failed: {exc}", file=sys.stderr)
        if container and SAFE_CONTAINER.fullmatch(container):
            logs = command(["docker", "logs", "--tail", "80", container], check=False)
            if logs.stdout.strip() or logs.stderr.strip():
                print((logs.stdout + logs.stderr)[-6000:], file=sys.stderr)
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
                print(f"WARNING: failed to drop {schema}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

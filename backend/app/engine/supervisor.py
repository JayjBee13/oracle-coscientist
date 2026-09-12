"""One detached process per run: `python -m app.engine.supervisor --run-id <uuid>`.

This is the process the launcher starts and the operating system owns. It builds the three
things a run needs — a store, a runner, a projector — hands them to the orchestrator, and
then does exactly two jobs of its own: it keeps the heartbeat going, and it makes sure that
however the run ends, the database says so.

Why it is a separate process at all: a research run takes tens of minutes and spawns model
processes of its own. Running that inside the API server means a backend restart kills the
science, a stuck call blocks request handling, and there is nothing to `taskkill` when the
scientist presses stop. A detached child has its own pid, its own log file, and its own
lifetime.

Three rules this module exists to enforce:

* **Secrets arrive through the environment, never argv.** The connection string, the app
  token and everything else come from `Settings`, which reads the environment. A process
  table is public; a command line is in every crash dump. The launcher's own tests assert
  no `://` reaches the supervisor's argv.
* **The heartbeat is a dead-man's switch, not telemetry.** Every ten seconds the supervisor
  writes its liveness to the run row. A `False` back means the row is gone or soft-deleted —
  the scientist deleted the run, or the kill switch was pulled — and the supervisor stops
  immediately rather than finishing a round nobody is waiting for. Two consecutive write
  *failures* stop it too: a supervisor that cannot reach the database cannot be stopped
  through the database either, and an unstoppable process that spends money is the failure
  mode this whole design is arranged around.
* **A crash is a recorded event.** Anything that escapes the loop lands in `runs.error` as
  JSON, moves the lifecycle to `failed`, emits `run_failed`, and exits non-zero. A run that
  dies silently is a run whose operator learns about it from an empty screen.

Exit codes: `0` the run reached a terminal lifecycle, `1` it failed (and the failure is in
the database), `2` it was halted because its row went away.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.config import Settings, get_settings
from app.engine.events import EventType, EventWriter
from app.engine.models import DEFAULT_PROVIDER
from app.engine.orchestrator import RunHalted, run_engine
from app.engine.projection import make_projector
from app.engine.runners import AgentRunner, FakeRunner
from app.engine.store import RunStore

__all__ = [
    "CRASH_ENV",
    "DEMO_LATENCY_ENV",
    "EXIT_FAILED",
    "EXIT_HALTED",
    "EXIT_OK",
    "HEARTBEAT_INTERVAL",
    "MAX_HEARTBEAT_FAILURES",
    "SupervisorHalted",
    "main",
    "supervise",
]

log = logging.getLogger("supervisor")

HEARTBEAT_INTERVAL = 10.0
MAX_HEARTBEAT_FAILURES = 2

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_HALTED = 2

CRASH_ENV = "COSCIENTIST_SUPERVISOR_CRASH"
"""Set to any message to make the supervisor raise during start-up.

The failure path is the one part of this process that cannot be exercised by asking the
engine nicely, and it is the path an operator depends on when something has gone wrong.
"""

DEMO_LATENCY_ENV = "COSCIENTIST_DEMO_LATENCY"
"""Seconds of artificial delay per scripted call on a demo run. Zero by default."""


class SupervisorHalted(RuntimeError):
    """The run row went away mid-flight, or the heartbeat stopped landing."""


# --------------------------------------------------------------------------- assembly


def build_runner(
    run_id: UUID, run: dict[str, Any], workdir: Path, store: RunStore
) -> AgentRunner:
    """The demo runner, or a router over the real CLIs with the budget cap wired in.

    Both provider lanes are registered and neither is constructed: a lane is built by the
    first call whose model belongs to it (`engine/routing.ProviderRouter`). That laziness is
    what keeps an all-Anthropic run working on a box with no Codex installed — resolving
    `codex.exe` eagerly would fail such a run at startup, with an error about a CLI it was
    never going to use.
    """
    config = dict(run.get("config") or {})
    if str(config.get("runner") or "claude") == "demo":
        latency = _float_env(DEMO_LATENCY_ENV, 0.0)
        return FakeRunner(seed=str(run_id), latency=latency)

    # Imported here so a demo run never imports the CLI layer, and so a machine without
    # the CLI installed can still run the demo.
    from app.engine.claude_runner import ClaudeCliRunner
    from app.engine.codex_runner import CodexCliRunner
    from app.engine.routing import ProviderRouter

    def budget_guard() -> None:
        """Refuse a spawn past either ceiling. The cap that survives an orchestrator bug."""
        store.check_budget(run_id)

    return ProviderRouter(
        {
            "anthropic": lambda: ClaudeCliRunner(workdir=workdir, budget_guard=budget_guard),
            "openai": lambda: CodexCliRunner(workdir=workdir, budget_guard=budget_guard),
        },
        default_provider=str(config.get("provider") or DEFAULT_PROVIDER),
    )


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("ignoring %s=%r: not a number", name, raw)
        return default


# -------------------------------------------------------------------------- heartbeat


async def _heartbeat(store: RunStore, run_id: UUID, pid: int, *, interval: float) -> str:
    """Stamp liveness until the run row says stop. Returns why it stopped.

    Never returns for a healthy run — the caller cancels it when the engine finishes.
    """
    failures = 0
    while True:
        await asyncio.sleep(interval)
        try:
            alive = await asyncio.to_thread(store.heartbeat, run_id, pid=pid)
        except Exception as exc:  # noqa: BLE001 — a database we cannot reach is the point
            failures += 1
            log.warning("heartbeat %d/%d failed: %s", failures, MAX_HEARTBEAT_FAILURES, exc)
            if failures >= MAX_HEARTBEAT_FAILURES:
                return (
                    f"{failures} consecutive heartbeat writes failed; "
                    "a supervisor that cannot be stopped through the database must stop itself"
                )
            continue
        if not alive:
            return "the run row is gone or was deleted"
        failures = 0


# ------------------------------------------------------------------------------- run


async def supervise(run_id: UUID, *, settings: Settings | None = None) -> str:
    """Drive one run to a terminal lifecycle and return it. Raises on failure.

    Everything that can be decided before the first model call is decided here — the
    workdir, the runner, the projector — so a misconfigured run fails at start-up with a
    recorded reason rather than halfway through a round.
    """
    resolved = settings or get_settings()
    store = RunStore(settings=resolved)
    pid = os.getpid()

    run = store.get_run(run_id)
    if run is None:
        raise SupervisorHalted(f"no run {run_id}")
    if run.get("deleted_at"):
        raise SupervisorHalted(f"run {run_id} is deleted")

    workdir = resolved.workdir_for(str(run["engine_run_id"]))
    workdir.mkdir(parents=True, exist_ok=True)
    store.heartbeat(run_id, pid=pid)

    log.info(
        "supervising run %s (%s) pid=%s harness=%s runner=%s workdir=%s",
        run_id,
        run["engine_run_id"],
        pid,
        run["harness"],
        (run.get("config") or {}).get("runner"),
        workdir,
    )

    crash = os.environ.get(CRASH_ENV, "").strip()
    if crash:
        raise RuntimeError(f"start-up aborted by {CRASH_ENV}: {crash}")

    runner = build_runner(run_id, run, workdir, store)
    projector = make_projector(store, run_id, workdir, engine_root=resolved.engine_root)

    engine = asyncio.create_task(
        run_engine(run_id, store, runner, projector=projector), name="engine"
    )
    beat = asyncio.create_task(
        _heartbeat(store, run_id, pid, interval=HEARTBEAT_INTERVAL), name="heartbeat"
    )
    try:
        done, _ = await asyncio.wait({engine, beat}, return_when=asyncio.FIRST_COMPLETED)
        if engine in done:
            return engine.result()
        raise SupervisorHalted(beat.result())
    finally:
        for task in (engine, beat):
            task.cancel()
        await asyncio.gather(engine, beat, return_exceptions=True)
        await _close(runner)


async def _close(runner: AgentRunner) -> None:
    try:
        await runner.aclose()
    except Exception:  # noqa: BLE001 — closing a runner must not mask the run's outcome
        log.exception("closing the runner failed")


def record_failure(store: RunStore, run_id: UUID, exc: BaseException) -> None:
    """Put the crash in the database, once.

    The orchestrator records failures that happen inside its own loop; this covers the ones
    that happen on the way in — a missing model table, an unreachable workdir, a runner that
    will not construct — and is a no-op when the orchestrator already got there.
    """
    error = {"type": type(exc).__name__, "message": str(exc)[:2000], "where": "supervisor"}
    try:
        run = store.get_run(run_id)
        if run is None:
            log.error("cannot record the failure of run %s: its row is gone", run_id)
            return
        if run["lifecycle"] == "failed":
            return
        store.set_lifecycle(run_id, "failed", error=error)
        events = EventWriter(store, run_id)
        events.emit(
            EventType.LIFECYCLE_CHANGED,
            {"lifecycle": "failed", "previous": run["lifecycle"]},
        )
        events.emit(EventType.RUN_FAILED, {"error": error})
    except Exception:  # noqa: BLE001 — nothing left but the log file
        log.exception("could not record the failure of run %s", run_id)


# ------------------------------------------------------------------------------ entry


def _configure_logging() -> None:
    """Everything to stdout, which the launcher has already pointed at the run's log file."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no branch — always present on 3.12+
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )


def _install_signal_handlers(stop: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    """Treat an interrupt as a stop request rather than a traceback.

    Force-stop kills the whole tree and never gets here. This is for the operator who runs
    a supervisor by hand in a console and presses Ctrl-C.
    """

    def handle(signum: int, _frame: object) -> None:
        log.warning("signal %s received; stopping", signum)
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handle)
        except (OSError, ValueError):  # pragma: no cover — not every signal binds
            log.debug("could not install a handler for %s", name)


async def _run(run_id: UUID, settings: Settings) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    _install_signal_handlers(stop, loop)

    supervision = asyncio.create_task(supervise(run_id, settings=settings), name="supervise")
    interrupt = asyncio.create_task(stop.wait(), name="interrupt")
    try:
        done, _ = await asyncio.wait(
            {supervision, interrupt}, return_when=asyncio.FIRST_COMPLETED
        )
        if interrupt in done and supervision not in done:
            # A cooperative stop: the orchestrator honours it at the next step boundary and
            # still writes the report.
            RunStore(settings=settings).set_control(run_id, "stop")
            lifecycle = await supervision
        else:
            lifecycle = supervision.result()
    except (SupervisorHalted, RunHalted) as exc:
        log.warning("halted: %s", exc)
        return EXIT_HALTED
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.warning("cancelled")
        return EXIT_HALTED
    except Exception as exc:  # noqa: BLE001 — the last line of defence, and it must record
        log.exception("run %s failed", run_id)
        record_failure(RunStore(settings=settings), run_id, exc)
        return EXIT_FAILED
    finally:
        interrupt.cancel()

    log.info("run %s finished: %s", run_id, lifecycle)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.engine.supervisor",
        description="Drive one co-scientist run. Configuration comes from the environment.",
    )
    parser.add_argument("--run-id", required=True, help="uuid of the run to supervise")
    args = parser.parse_args(argv)

    _configure_logging()
    try:
        run_id = UUID(args.run_id)
    except ValueError:
        log.error("--run-id must be a uuid, got %r", args.run_id)
        return EXIT_FAILED

    settings = get_settings()
    return asyncio.run(_run(run_id, settings))


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    raise SystemExit(main())

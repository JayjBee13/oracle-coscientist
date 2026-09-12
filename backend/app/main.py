import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.artifacts import router as artifacts_router
from app.api.capabilities import router as capabilities_router
from app.api.compare import router as compare_router
from app.api.errors import install_error_handlers
from app.api.events import router as events_router
from app.api.hypotheses import router as hypotheses_router
from app.api.identity import router as identity_router
from app.api.runs import router as runs_router
from app.api.settings import router as settings_router
from app.api.workshops import router as workshops_router
from app.core.auth import OptionalTokenAuthMiddleware
from app.core.config import Settings, get_settings
from app.core.identity import IdentityMiddleware
from app.core.logging import install_credential_redaction
from app.db.session import get_engine
from app.engine.store import RunStore
from app.services.events.listener import RunEventListener
from app.services.harness.probes import cached_probe_cli
from app.services.runs.controls import quiesce_supervisors, reconcile_runs
from app.services.runs.importer import import_all
from app.web import install_web_ui

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 30.0
"""How often to look for runs whose supervisor died.

Periodic rather than startup-only: a supervisor can die while the backend stays up, and
until something notices, its harness lane is held by a process that no longer exists.
"""


async def _reconcile_runs_forever(settings: Settings, interval: float) -> None:
    """Sweep for orphaned runs until cancelled. Never lets one failure end the loop."""
    store = RunStore(settings=settings)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(reconcile_runs, store)
        except Exception:  # noqa: BLE001 — a sweep that failed is retried, not fatal
            logger.exception("run reconciliation failed")


class HarnessHealthStatus(BaseModel):
    installed: bool
    version: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    db_ok: bool
    harnesses: dict[str, HarnessHealthStatus]
    active_runs: int


def _db_ok(settings: Settings) -> bool:
    """A cheap `SELECT 1`. False, never a traceback, when the database is unreachable."""
    try:
        with get_engine(settings).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — health must never 500 because the DB is down
        return False


def _active_run_count(settings: Settings, *, db_ok: bool) -> int:
    """Runs in a live lifecycle. Skipped (0) when the database is already known down."""
    if not db_ok:
        return 0
    try:
        return len(RunStore(settings=settings).active_runs())
    except Exception:  # noqa: BLE001 — a transient failure here must not fail /health
        return 0


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI) -> AsyncIterator[None]:
        if resolved_settings.import_on_startup:
            # Idempotent, so booting twice changes nothing. A failure here must not stop
            # the app: the archive being unreadable is a reason to show the runs already
            # imported, not to refuse to start.
            try:
                report = import_all(resolved_settings)
                logger.info("imported %s archived runs", report.imported)
            except Exception:
                logger.exception("archived run import failed; continuing without it")

        # Runs whose supervisor died while the backend was down are marked lost here, which
        # is what frees their harness lane. The periodic sweep below catches the ones that
        # die while it is up.
        try:
            lost = await asyncio.to_thread(reconcile_runs, RunStore(settings=resolved_settings))
            if lost:
                logger.warning("marked %d run(s) lost on startup", len(lost))
        except Exception:  # noqa: BLE001 — a database that is not up yet must not block boot
            logger.exception("startup run reconciliation failed")

        # One LISTEN connection for the whole process, however many streams are open. A
        # database that is not up yet is not a reason to refuse to boot: the listener
        # keeps retrying, and streams still replay from the database meanwhile.
        listener = RunEventListener(resolved_settings)
        fastapi_app.state.run_event_listener = listener
        if not await listener.start():
            logger.warning("run event listener is not connected yet; it will keep retrying")

        reconciler = asyncio.create_task(
            _reconcile_runs_forever(resolved_settings, RECONCILE_INTERVAL_SECONDS),
            name="run-reconciler",
        )
        try:
            yield
        finally:
            reconciler.cancel()
            with suppress(asyncio.CancelledError):
                await reconciler
            if resolved_settings.manage_supervisors_on_shutdown:
                try:
                    outcome = await asyncio.to_thread(
                        quiesce_supervisors,
                        RunStore(settings=resolved_settings),
                        grace_seconds=resolved_settings.supervisor_shutdown_grace_seconds,
                    )
                    if outcome["refused"]:
                        logger.error(
                            "managed shutdown could not verify %d supervisor pid(s)",
                            len(outcome["refused"]),
                        )
                except Exception:  # noqa: BLE001 - shutdown must continue after logging
                    logger.exception("managed supervisor shutdown failed")
            await listener.stop()

    app = FastAPI(
        title="Oracle GUI API",
        version="0.1.0",
        description="Local-first API for Oracle run orchestration and artifact discovery.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Starlette inserts middleware at the front. Add identity first so token auth is the
    # outer layer and rejects unauthenticated requests before any identity is trusted.
    app.add_middleware(IdentityMiddleware, settings=resolved_settings)
    app.add_middleware(OptionalTokenAuthMiddleware, token=resolved_settings.app_auth_token)

    install_credential_redaction()
    install_error_handlers(app)
    app.include_router(runs_router)
    app.include_router(events_router)
    app.include_router(hypotheses_router)
    app.include_router(identity_router)
    app.include_router(artifacts_router)
    app.include_router(capabilities_router)
    app.include_router(settings_router)
    app.include_router(compare_router)
    app.include_router(admin_router)
    app.include_router(workshops_router)
    app.dependency_overrides[get_settings] = lambda: resolved_settings

    @app.get("/health", response_model=HealthResponse)
    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Plan C5's health shape. Harness probes are cached (60s); this never spawns a
        model call — `--version` only."""
        db_ok = _db_ok(resolved_settings)
        claude = cached_probe_cli("claude")
        codex = cached_probe_cli("codex")
        return HealthResponse(
            status="ok",
            version=app.version,
            db_ok=db_ok,
            harnesses={
                "claude": HarnessHealthStatus(installed=claude.installed, version=claude.version),
                "codex": HarnessHealthStatus(installed=codex.installed, version=codex.version),
            },
            active_runs=_active_run_count(resolved_settings, db_ok=db_ok),
        )

    # Last, and it has to be last: the fallback inside matches every path, and FastAPI
    # matches routes in the order they were registered.
    install_web_ui(app, resolved_settings.web_dist_dir)

    return app


app = create_app()

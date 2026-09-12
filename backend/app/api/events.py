"""The live event stream, and the ticket that lets a browser open it.

`GET /api/runs/{id}/events` is a Server-Sent Events stream: every event the run has
written since the seq the client names, then every new one as it lands, then a comment
every fifteen seconds so a quiet run still proves the connection is alive.

Two things are worth knowing before changing anything here.

**No request-scoped database session.** A `Depends(get_db)` session is held until the
response finishes sending, and this response finishes when the scientist closes the tab —
which could be an hour. So the existence check opens its own session and gives it straight
back, and the stream itself reads through a session per query in a worker thread.

**Auth happens in the middleware, not here.** `EventSource` cannot send headers, so this
one route also accepts `?ticket=` (single-use, sixty seconds, issued below) or `?token=`
for curl. `OptionalTokenAuthMiddleware` is what redeems them, because it is the only place
that sees a request before routing and can refuse it for every path uniformly.
"""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.errors import not_found
from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser, Identity, RequestIdentity, upsert_user
from app.db.session import get_db, get_session_factory
from app.services.events.listener import RunEventListener
from app.services.events.stream import run_event_stream
from app.services.events.tickets import TICKETS
from app.services.runs import reads

router = APIRouter(prefix="/api/runs", tags=["events"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]

# `no-cache` and `X-Accel-Buffering: no` are plan C5's: without the second, nginx buffers
# the stream into silence and every event arrives at once, minutes late.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

_listener_lock = asyncio.Lock()


class EventTicket(BaseModel):
    ticket: str
    expires_in: int


async def get_listener(app, settings: Settings) -> RunEventListener:
    """The process's listener, started by the app's lifespan.

    The lazy branch is for anything that skipped lifespan (a `TestClient` used without its
    context manager). It never runs under uvicorn.
    """
    listener = getattr(app.state, "run_event_listener", None)
    if listener is not None:
        return listener
    async with _listener_lock:
        listener = getattr(app.state, "run_event_listener", None)
        if listener is None:
            listener = RunEventListener(settings)
            app.state.run_event_listener = listener
            await listener.start()
    return listener


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    settings: SettingsDep,
    identity: RequestIdentity,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Every event of this run from `after_seq` on, as it happens."""
    resolved_id = await asyncio.to_thread(_resolve_run_id, settings, run_id, identity)
    listener = await get_listener(request.app, settings)
    return StreamingResponse(
        run_event_stream(
            run_id=resolved_id,
            listener=listener,
            session_factory=get_session_factory(settings),
            after_seq=_resume_from(request, after_seq),
            heartbeat_seconds=settings.sse_heartbeat_seconds,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{run_id}/events/ticket", response_model=EventTicket)
def issue_event_ticket(run_id: str, db: DbSession, current: CurrentUser) -> EventTicket:
    """A credential good for one stream of this run, for the next minute.

    The ticket is bound to the id exactly as spelled in this URL, since the middleware
    that redeems it compares path segments and never resolves a run.
    """
    run = reads.find_run(db, run_id, current.identity)
    if run is None:
        raise not_found("run_not_found", "No run with that id.", run_id=run_id)
    return EventTicket(
        ticket=TICKETS.issue(run_id),
        expires_in=int(TICKETS.ttl_seconds),
    )


def _resolve_run_id(settings: Settings, reference: str, identity: Identity) -> UUID:
    """Resolve the run and hand the connection straight back — see the module docstring."""
    with get_session_factory(settings)() as session:
        upsert_user(session, identity)
        run = reads.find_run(session, reference, identity)
        if run is None:
            raise not_found("run_not_found", "No run with that id.", run_id=reference)
        return run.id


def _resume_from(request: Request, after_seq: int) -> int:
    """Where to replay from. `Last-Event-ID` wins: it is what the client actually got.

    A browser sets that header itself on reconnect, so it reflects delivery rather than
    whatever seq the page was holding when it built the URL. An unparseable value is
    ignored rather than refused — resuming from the start beats refusing to stream.
    """
    header = request.headers.get("last-event-id")
    if header is not None:
        try:
            return max(int(header.strip()), 0)
        except ValueError:
            pass
    return max(after_seq, 0)

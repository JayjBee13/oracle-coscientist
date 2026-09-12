"""The body of `GET /runs/{id}/events`: replay, then live, with nothing lost between.

The order below is the whole point of the file and it is load-bearing:

1. **Subscribe.** The process-wide listener is already LISTENing; registering this
   stream's queue is what makes the next step safe.
2. **Replay.** Select everything after the seq the client says it has, and send it.
3. **Go live.** Drain the queue, ignoring any doorbell for a seq the replay already
   covered.

Do those in any other order and there is a window — between the replay query committing
and the subscription existing — in which an event is written, notified to nobody, and
never seen again by a client that believes it is caught up. Subscribing first turns that
window into a duplicate instead of a loss, and step 3's seq check turns the duplicate into
nothing at all.

Reads run in a worker thread. The rest of the app talks to Postgres through synchronous
SQLAlchemy, and a live stream is exactly where a blocking call in the event loop hurts
most: this endpoint's predecessor did a full artifact rescan per tick per watcher, and
roughly a quarter of the loop went to each open browser tab.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import anyio
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect

from app.db.engine_models import RunEvent
from app.services.events.listener import RESYNC, RunEventListener

HEARTBEAT_FRAME = ": heartbeat\n\n"
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_BATCH_SIZE = 500

# Everything a client going away can look like from inside the generator. None of them is
# an error worth a log line, let alone a traceback: the scientist closed a tab.
CLIENT_GONE = (
    ConnectionResetError,
    BrokenPipeError,
    ClientDisconnect,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
)


def event_dict(row: RunEvent) -> dict[str, Any]:
    """One event in the shape plan C5 froze for `Event`."""
    return {
        "seq": row.seq,
        "run_id": str(row.run_id),
        "round": row.round,
        "type": row.type,
        "payload": dict(row.payload),
        "ts": row.ts.isoformat() if row.ts is not None else None,
    }


def format_event(event: dict[str, Any]) -> str:
    """An SSE frame. `id:` is the seq, which is what a reconnect resumes from."""
    data = json.dumps(event, separators=(",", ":"))
    return f"id: {event['seq']}\nevent: run_event\ndata: {data}\n\n"


def fetch_events_after(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    after_seq: int,
    limit: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Events past `after_seq`, oldest first. Runs in a thread; opens its own session."""
    with session_factory() as session:
        rows = (
            session.execute(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                .order_by(RunEvent.seq)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [event_dict(row) for row in rows]


async def _catch_up(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    last_seq: int,
    batch_size: int,
) -> AsyncIterator[tuple[str, int]]:
    """Yield `(frame, seq)` for everything after `last_seq`, a page at a time."""
    while True:
        events = await asyncio.to_thread(
            fetch_events_after, session_factory, run_id, last_seq, batch_size
        )
        for event in events:
            last_seq = event["seq"]
            yield format_event(event), last_seq
        if len(events) < batch_size:
            return


async def run_event_stream(
    *,
    run_id: UUID,
    listener: RunEventListener,
    session_factory: sessionmaker[Session],
    after_seq: int = 0,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> AsyncIterator[str]:
    """Frames for one watcher, from `after_seq` until the client goes away.

    The `async with` is the ordering: the subscription exists before the replay inside it
    can read a row, so there is no instant at which this stream is caught up but not
    listening. It also releases the subscription however the block ends — including the
    cancellation that a closed browser tab arrives as.
    """
    last_seq = max(int(after_seq), 0)
    try:
        async with listener.subscribe(str(run_id)) as subscription:
            async for frame, seq in _catch_up(session_factory, run_id, last_seq, batch_size):
                last_seq = seq
                yield frame

            while True:
                try:
                    doorbell = await asyncio.wait_for(subscription.queue.get(), heartbeat_seconds)
                except TimeoutError:
                    # Not decoration: a comment every 15s is how a client on a quiet run
                    # finds out its connection died, and how this generator finds out too.
                    yield HEARTBEAT_FRAME
                    continue

                if doorbell != RESYNC and doorbell <= last_seq:
                    continue  # the replay already sent this one

                async for frame, seq in _catch_up(session_factory, run_id, last_seq, batch_size):
                    last_seq = seq
                    yield frame
    except CLIENT_GONE:
        return
    # asyncio.CancelledError is deliberately not caught. Letting it through is what
    # "silently" means here: it unwinds the response's task group without a log line or an
    # error body, and the context manager above still releases the subscription.

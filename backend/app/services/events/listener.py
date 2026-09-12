"""One database connection listening for run events, fanned out inside the process.

Postgres delivers NOTIFY per connection, so the obvious implementation of
`GET /runs/{id}/events` — a listening connection per watcher — puts the connection count
under the control of however many browser tabs happen to be open, on a server that also
hosts the owner's other databases. There is therefore exactly one listener per process. It
reads the `run_events` channel and drops each notification into an `asyncio.Queue` per
subscriber; opening a hundred streams moves the connection count by zero.

The notification payload is `{schema, run_id, seq}` — a doorbell, not the event. A stream
answers it by selecting everything past the last seq it delivered, which keeps payloads
small (event bodies never have to fit NOTIFY's own limit) and makes a lost notification
harmless: the next one catches the stream up.

**Why a thread and not `AsyncConnection.notifies()`.** The plan called for psycopg's async
API. It cannot be used in this process on Windows: psycopg refuses to run async on a
`ProactorEventLoop`, which is exactly what uvicorn creates here, and the loop cannot simply
be switched to a selector one because `asyncio.create_subprocess_exec` — how the workshop
service reaches the Claude CLI — is Proactor-only on Windows. Each requirement rules out
the other's loop, so the listener owns a thread with a synchronous connection and hands
every notification to the event loop through `call_soon_threadsafe`. The contract the plan
actually cared about is unchanged: one connection, asyncio queues, no asyncpg.

That handover also keeps the subscriber registry single-threaded — `_dispatch` and every
`subscribe`/`unsubscribe` run on the event loop, so none of it needs a lock.

Two details that look like paranoia and are not:

* **Schema filtering.** The channel is database-wide, and the test suite runs against
  throwaway `test_<hex>` schemas in the same database as live data. Without the filter a
  test's events would be delivered to a real watcher, and vice versa.
* **Bounded queues.** A subscriber that stops reading — a wedged client, a generator no
  one is draining — must not grow a queue without limit. When one fills, its oldest
  doorbell is dropped and counted as lag. Because every wake-up re-reads by seq, the
  dropped notification costs nothing except the record that it happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import psycopg
from psycopg import sql

from app.core.config import Settings
from app.db.session import libpq_dsn

logger = logging.getLogger(__name__)

CHANNEL = "run_events"

# Queued instead of a seq: "you may have missed something, re-read from where you are".
# Sent to every subscriber after a reconnect, since events written while the listener was
# away produced notifications nobody was there to hear.
RESYNC = -1

DEFAULT_QUEUE_SIZE = 256
DEFAULT_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0)

# How long the listening thread blocks before looking at the stop flag. Notifications
# arrive as they happen regardless; this only bounds how long shutdown waits.
POLL_SECONDS = 0.5

# How long a new subscriber waits for the connection before streaming anyway. Only a
# startup or reconnect race ever spends it; the app's lifespan has already waited once.
READY_TIMEOUT_SECONDS = 2.0

# A connection that stayed up this long counts as healthy: the next failure starts its
# backoff from the beginning rather than from wherever the last outage left off.
_STABLE_SECONDS = 30.0


class Subscription:
    """One stream's doorbell queue. Created by `RunEventListener.subscribe`."""

    def __init__(self, run_id: str, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        self.run_id = run_id
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        self.lagged = 0

    def offer(self, seq: int) -> bool:
        """Queue a doorbell, dropping the oldest if this subscriber is behind.

        Returns True when something was dropped, so the listener can count it.
        """
        try:
            self.queue.put_nowait(seq)
            return False
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.lagged += 1
            with suppress(asyncio.QueueFull):
                self.queue.put_nowait(seq)
            return True


class RunEventListener:
    """The process's single `LISTEN run_events` connection and its fanout."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        channel: str = CHANNEL,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        reconnect_delays: tuple[float, ...] = DEFAULT_RECONNECT_DELAYS,
    ) -> None:
        self._dsn = libpq_dsn(settings)
        self._channel = channel
        self._queue_size = queue_size
        self._reconnect_delays = reconnect_delays
        self._subscribers: dict[str, set[Subscription]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._ready = asyncio.Event()
        self._schema: str | None = None
        self._connected = False
        self._reconnects = 0
        self._dropped = 0

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self, *, wait_seconds: float = 5.0) -> bool:
        """Start listening. Returns whether the connection was up within `wait_seconds`.

        A False here is not fatal and must not stop the app from booting: the thread keeps
        retrying, and a stream opened meanwhile still replays from the database — it just
        will not see live events until the connection comes back.
        """
        if self._thread is None or not self._thread.is_alive():
            self._loop = asyncio.get_running_loop()
            self._ready.clear()
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._listen_forever, name="run-events-listener", daemon=True
            )
            self._thread.start()
        return await self.wait_ready(wait_seconds)

    async def stop(self) -> None:
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, POLL_SECONDS * 10)
            if thread.is_alive():
                logger.warning("run event listener thread did not stop in time")
        self._connected = False
        self._ready.clear()
        self._subscribers.clear()
        self._loop = None

    async def wait_ready(self, timeout: float) -> bool:
        """Whether the connection is up, waiting up to `timeout` for it to come up.

        A listener nobody started returns immediately: there is nothing on its way, and
        waiting out the timeout for an answer that cannot change would be a pure delay.
        """
        if self._connected:
            return True
        if self._thread is None or not self._thread.is_alive():
            return False
        with suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout)
        return self._connected

    # --- subscriptions (event loop only) -----------------------------------------------

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[Subscription]:
        """A registered queue for this run, for exactly as long as the block runs.

        A context manager rather than a pair of calls, because the ordering it enforces is
        the entire correctness argument of the live stream. Registration happens before
        this yields, so nothing inside the block — above all the replay query — can run
        against an unsubscribed listener. As two plain calls that ordering is a rule the
        caller has to remember, and the price of forgetting is an event that is written,
        notified to nobody, and never seen again by a client that believes it is caught up.

        Registration also happens before the readiness wait rather than after it: waiting
        first would reopen the very gap this closes.
        """
        subscription = self._register(run_id)
        try:
            # Bounded, and non-fatal when it expires. A listener still coming up must not
            # hold a stream open with nothing in it, and it does not have to: the queue is
            # already registered, and the reconnect's RESYNC will bring this subscriber
            # back in step. So a degraded listener costs live latency, never an event.
            if not await self.wait_ready(READY_TIMEOUT_SECONDS):
                logger.warning("streaming run %s while the listener is disconnected", run_id)
            yield subscription
        finally:
            self._unregister(subscription)

    def _register(self, run_id: str) -> Subscription:
        subscription = Subscription(str(run_id), self._queue_size)
        self._subscribers.setdefault(subscription.run_id, set()).add(subscription)
        return subscription

    def _unregister(self, subscription: Subscription) -> None:
        peers = self._subscribers.get(subscription.run_id)
        if peers is None:
            return
        peers.discard(subscription)
        if not peers:
            del self._subscribers[subscription.run_id]

    def health(self) -> dict[str, object]:
        return {
            "connected": self._connected,
            "subscribers": sum(len(peers) for peers in self._subscribers.values()),
            "runs": len(self._subscribers),
            "schema": self._schema,
            "reconnects": self._reconnects,
            "dropped": self._dropped,
        }

    def _dispatch(self, payload: str) -> None:
        """Hand one notification to whoever is watching that run. Runs on the loop."""
        try:
            message = json.loads(payload)
            schema = message["schema"]
            run_id = str(message["run_id"])
            seq = int(message["seq"])
        except (TypeError, ValueError, KeyError):
            logger.warning("ignoring malformed run_events notification: %.200r", payload)
            return
        if schema != self._schema:
            return
        for subscription in tuple(self._subscribers.get(run_id, ())):
            if subscription.offer(seq):
                self._dropped += 1

    def _resync_all(self) -> None:
        for peers in self._subscribers.values():
            for subscription in peers:
                subscription.offer(RESYNC)

    # --- the listening thread ----------------------------------------------------------

    def _listen_forever(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                self._listen_once()
            except Exception as exc:  # noqa: BLE001 - any failure here means "reconnect"
                if not self._stopping.is_set():
                    logger.warning("run_events listener disconnected (%s); reconnecting", exc)
            finally:
                self._connected = False
                self._on_loop(self._ready.clear)
            if self._stopping.is_set():
                return
            if time.monotonic() - started > _STABLE_SECONDS:
                attempt = 0
            delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
            attempt += 1
            self._reconnects += 1
            self._stopping.wait(delay)

    def _listen_once(self) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            row = connection.execute("select current_schema()").fetchone()
            self._schema = None if row is None else row[0]
            connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
            self._connected = True
            self._on_loop(self._ready.set)
            # Anything written while we were away notified nobody; wake every subscriber
            # so it re-reads from its last seq.
            self._on_loop(self._resync_all)
            logger.info("listening for run events on %r (schema %s)", self._channel, self._schema)
            while not self._stopping.is_set():
                for notification in connection.notifies(timeout=POLL_SECONDS):
                    self._on_loop(self._dispatch, notification.payload)
                    if self._stopping.is_set():
                        return

    def _on_loop(self, function, *args) -> None:
        """Run something on the event loop from the listening thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with suppress(RuntimeError):  # the loop closed between the check and the call
            loop.call_soon_threadsafe(function, *args)

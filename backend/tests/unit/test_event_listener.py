"""Fanout and backpressure, without a database.

The listener's connection is exercised for real in `tests/integration/test_events_sse.py`.
What is worth testing in isolation is the part that runs on the event loop once a
notification has arrived: who gets it, who does not, what happens to a subscriber that has
stopped reading, and what `subscribe` guarantees to whoever is inside its block.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import Settings
from app.services.events.listener import RESYNC, RunEventListener, Subscription

RUN = "8f14e45f-ea8f-4b3c-9b3a-2c1d0e5a7b60"
OTHER_RUN = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
SCHEMA = "test_abcd1234"


def notification(run_id: str = RUN, seq: int = 1, schema: str = SCHEMA) -> str:
    return json.dumps({"schema": schema, "run_id": run_id, "seq": seq})


@pytest.fixture
def listener() -> RunEventListener:
    made = RunEventListener(Settings())
    made._schema = SCHEMA  # noqa: SLF001 - normally learned from the connection
    return made


async def test_a_notification_reaches_the_run_it_names(listener):
    async with listener.subscribe(RUN) as subscription:
        listener._dispatch(notification(seq=7))  # noqa: SLF001

        assert subscription.queue.get_nowait() == 7


async def test_another_run_hears_nothing(listener):
    async with listener.subscribe(RUN) as watching, listener.subscribe(OTHER_RUN) as elsewhere:
        listener._dispatch(notification(run_id=OTHER_RUN, seq=3))  # noqa: SLF001

        assert watching.queue.empty()
        assert elsewhere.queue.get_nowait() == 3


async def test_every_subscriber_of_one_run_gets_it(listener):
    """Two browser tabs on the same run are two queues, not two connections."""
    async with listener.subscribe(RUN) as first, listener.subscribe(RUN) as second:
        listener._dispatch(notification(seq=5))  # noqa: SLF001

        assert first.queue.get_nowait() == 5
        assert second.queue.get_nowait() == 5


async def test_another_schema_is_ignored(listener):
    async with listener.subscribe(RUN) as subscription:
        listener._dispatch(notification(schema="public"))  # noqa: SLF001

        assert subscription.queue.empty()


async def test_a_malformed_payload_is_ignored_rather_than_fatal(listener):
    async with listener.subscribe(RUN) as subscription:
        listener._dispatch("not json at all")  # noqa: SLF001
        listener._dispatch(json.dumps({"schema": SCHEMA}))  # noqa: SLF001

        assert subscription.queue.empty()


async def test_leaving_the_block_stops_delivery_and_forgets_the_run(listener):
    async with listener.subscribe(RUN) as subscription:
        pass

    listener._dispatch(notification(seq=9))  # noqa: SLF001

    assert subscription.queue.empty()
    assert listener.health()["subscribers"] == 0
    assert listener.health()["runs"] == 0


async def test_the_subscription_is_released_even_when_the_block_raises(listener):
    """A stream ends by being cancelled far more often than by finishing."""
    with pytest.raises(asyncio.CancelledError):
        async with listener.subscribe(RUN):
            raise asyncio.CancelledError

    assert listener.health()["subscribers"] == 0


async def test_the_queue_is_registered_before_the_block_runs(listener):
    """The ordering the context manager exists to enforce.

    Whatever the block does first — for the live stream, replaying the table — the queue
    is already collecting. Registering afterwards would leave a window in which an event
    is written, notified to nobody, and then missed by a replay that has already read.
    """
    async with listener.subscribe(RUN) as subscription:
        assert listener.health()["subscribers"] == 1
        listener._dispatch(notification(seq=1))  # noqa: SLF001
        assert subscription.queue.get_nowait() == 1


async def test_a_disconnected_listener_does_not_hold_the_stream_up(listener):
    """A listener that is down costs live latency, never an event.

    The subscription is registered regardless, so the reconnect's RESYNC catches this
    subscriber up. Blocking here instead would turn a database blip into a hung request.
    """
    assert listener.health()["connected"] is False

    async with asyncio.timeout(1.0), listener.subscribe(RUN) as subscription:
        assert listener.health()["subscribers"] == 1
        listener._resync_all()  # noqa: SLF001
        assert subscription.queue.get_nowait() == RESYNC


async def test_a_full_queue_drops_its_oldest_doorbell_and_counts_the_lag():
    """A subscriber that stopped reading must not grow a queue without limit.

    Dropping the oldest is safe because a doorbell is not the event: whatever the stream
    reads next, it reads by seq, so it catches up on everything the drop skipped.
    """
    subscription = Subscription(RUN, maxsize=2)

    assert subscription.offer(1) is False
    assert subscription.offer(2) is False
    assert subscription.offer(3) is True

    assert [subscription.queue.get_nowait() for _ in range(2)] == [2, 3]
    assert subscription.lagged == 1


async def test_dropped_doorbells_are_counted_on_the_listener(listener):
    async with listener.subscribe(RUN) as subscription:
        subscription.queue._maxsize = 1  # noqa: SLF001

        listener._dispatch(notification(seq=1))  # noqa: SLF001
        listener._dispatch(notification(seq=2))  # noqa: SLF001

        assert listener.health()["dropped"] == 1


async def test_a_reconnect_wakes_every_subscriber(listener):
    """Events written while the connection was down notified nobody."""
    async with listener.subscribe(RUN) as first, listener.subscribe(OTHER_RUN) as second:
        listener._resync_all()  # noqa: SLF001

        assert first.queue.get_nowait() == RESYNC
        assert second.queue.get_nowait() == RESYNC


async def test_health_reports_what_is_open(listener):
    async with (
        listener.subscribe(RUN),
        listener.subscribe(RUN),
        listener.subscribe(OTHER_RUN),
    ):
        health = listener.health()

    assert health["subscribers"] == 3
    assert health["runs"] == 2
    assert health["connected"] is False


async def test_waiting_on_a_listener_nobody_started_answers_at_once():
    """Otherwise every subscriber pays the full timeout for an answer that cannot change."""
    listener = RunEventListener(Settings())

    async with asyncio.timeout(0.5):
        assert await listener.wait_ready(30.0) is False

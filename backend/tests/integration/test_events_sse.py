"""The live event stream, against a real LISTEN/NOTIFY connection.

These are integration tests on purpose. Everything that makes SSE hard here — the seam
between replaying history and going live, notifications from another test's schema, a
subscriber that must not see another run's events — only exists once a real Postgres
connection is delivering real notifications, and a mocked listener would assert nothing but
that the mock was written to match the code.

Runs are created directly in a terminal lifecycle so they never hold a harness lane: SSE
does not care what state a run is in, and a test that parked a lane would fail whichever
module launches a run next.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from uvicorn.loops.asyncio import asyncio_loop_factory

from app.core.config import Settings
from app.db.session import get_session_factory
from app.engine.store import RunStore
from app.main import create_app
from app.services.events import stream as stream_module
from app.services.events.tickets import TICKETS
from tests.support.imported import fixture_settings
from tests.support.sse import asgi_request, open_sse

HEARTBEAT_SECONDS = 0.25
TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def settings(isolated_schema) -> Settings:
    return fixture_settings(SSE_HEARTBEAT_SECONDS=HEARTBEAT_SECONDS)


@pytest.fixture
def store(settings: Settings) -> RunStore:
    return RunStore(settings=settings)


@pytest.fixture
async def app(settings: Settings):
    """The real application, with the real lifespan — the listener wiring is under test."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        listener = application.state.run_event_listener
        assert listener.health()["connected"], "listener never reached the database"
        yield application


def make_run(store: RunStore, harness: str = "claude") -> UUID:
    return store.create_run(
        question="Why do some lakes bloom under falling nutrient loads?",
        prompt="…",
        harness=harness,
        source="imported",
        lifecycle="completed",
        engine_run_id=f"sse-{uuid4().hex[:8]}",
    )


async def emit(store: RunStore, run_id: UUID, type: str, **payload: Any) -> int:
    """Write an event the way the orchestrator does, off the event loop."""
    event = await asyncio.to_thread(store.emit, run_id, type, payload)
    return int(event["seq"])


async def test_these_tests_run_on_the_event_loop_uvicorn_would_create():
    """Otherwise everything below proves compatibility with a loop nobody serves on.

    This is not idle: psycopg's async API refuses to run on the `ProactorEventLoop` uvicorn
    creates on Windows, which is why the listener owns a thread with a synchronous
    connection. A test suite that quietly ran on a selector loop would let that design be
    "simplified" back to `AsyncConnection.notifies()` with every test still green, and the
    breakage would first appear when the server was started for real.
    """
    assert type(asyncio.get_running_loop()) is asyncio_loop_factory()


async def test_replays_history_then_streams_live_events_in_order(app, store):
    run_id = make_run(store)
    replayed = [await emit(store, run_id, "round_started", round=index) for index in range(3)]

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        assert stream.status == 200
        seen = [(await stream.next_event()).seq for _ in replayed]
        assert seen == replayed

        live = [await emit(store, run_id, "hypothesis_added", hid=f"h00{i}") for i in range(3)]
        assert [(await stream.next_event()).seq for _ in live] == live


async def test_frame_carries_the_full_event_dto(app, store):
    run_id = make_run(store)
    seq = await emit(store, run_id, "match_completed", hid_a="h001", hid_b="h002")

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        frame = await stream.next_event()

    assert frame.event == "run_event"
    assert frame.id == str(seq)
    assert frame.raw.startswith(f"id: {seq}\nevent: run_event\ndata: ")
    assert frame.data is not None
    assert set(frame.data) == {"seq", "run_id", "round", "type", "payload", "ts"}
    assert frame.data["type"] == "match_completed"
    assert frame.data["payload"] == {"hid_a": "h001", "hid_b": "h002"}
    assert frame.data["run_id"] == str(run_id)


async def test_response_headers_keep_the_stream_unbuffered(app, store):
    run_id = make_run(store)

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "private, no-store"
        assert stream.headers["x-accel-buffering"] == "no"


async def test_event_written_between_replay_and_live_arrives_exactly_once(app, store, monkeypatch):
    """The race the ordering exists to prevent.

    An event committed after the replay query has read the table but before the stream is
    listening would, in the obvious implementation, be notified to nobody and then skipped
    by the replay that already ran — gone for good. Subscribing first turns that loss into
    a possible duplicate, and the seq check turns the duplicate into nothing.

    The insert happens inside the replay call itself, which is the only way to land in the
    window reliably: the stream is suspended on that thread call when it commits.
    """
    run_id = make_run(store)
    before = await emit(store, run_id, "round_started", round=1)

    original = stream_module.fetch_events_after
    raced: list[int] = []

    def fetch_then_race(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        rows = original(*args, **kwargs)
        if not raced:
            raced.append(store.emit(run_id, "hypothesis_added", {"hid": "h001"})["seq"])
        return rows

    monkeypatch.setattr(stream_module, "fetch_events_after", fetch_then_race)

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        first = await stream.next_event()
        second = await stream.next_event()
        extra = await stream.drain(0.6)

    assert [first.seq, second.seq] == [before, raced[0]]
    assert [frame.seq for frame in extra if not frame.is_heartbeat] == []


async def test_after_seq_resumes_without_repeating_what_the_client_has(app, store):
    run_id = make_run(store)
    seqs = [await emit(store, run_id, "round_started", round=index) for index in range(4)]

    async with open_sse(app, f"/api/runs/{run_id}/events?after_seq={seqs[1]}") as stream:
        assert [(await stream.next_event()).seq for _ in seqs[2:]] == seqs[2:]
        assert [frame.seq for frame in await stream.drain(0.6) if not frame.is_heartbeat] == []


async def test_last_event_id_header_is_honoured_and_beats_after_seq(app, store):
    run_id = make_run(store)
    seqs = [await emit(store, run_id, "round_started", round=index) for index in range(4)]

    async with open_sse(
        app,
        f"/api/runs/{run_id}/events?after_seq=0",
        headers={"Last-Event-ID": str(seqs[2])},
    ) as stream:
        assert (await stream.next_event()).seq == seqs[3]


async def test_unparseable_last_event_id_replays_from_the_beginning(app, store):
    run_id = make_run(store)
    first = await emit(store, run_id, "round_started", round=0)

    async with open_sse(
        app, f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "not-a-number"}
    ) as stream:
        assert (await stream.next_event()).seq == first


async def test_quiet_stream_sends_heartbeat_comments(app, store):
    run_id = make_run(store)

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        first = await stream.next_frame(timeout=HEARTBEAT_SECONDS * 8)
        second = await stream.next_frame(timeout=HEARTBEAT_SECONDS * 8)

    assert first.is_heartbeat and second.is_heartbeat
    assert first.raw == ": heartbeat"


async def test_heartbeats_do_not_interrupt_delivery(app, store):
    run_id = make_run(store)

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        await stream.next_frame(timeout=HEARTBEAT_SECONDS * 8)  # idle first
        seq = await emit(store, run_id, "round_completed", round=1)
        assert (await stream.next_event()).seq == seq


async def test_two_subscribers_on_different_runs_stay_isolated(app, store):
    watched = make_run(store, harness="claude")
    other = make_run(store, harness="codex")

    async with (
        open_sse(app, f"/api/runs/{watched}/events") as watched_stream,
        open_sse(app, f"/api/runs/{other}/events") as other_stream,
    ):
        seq = await emit(store, watched, "hypothesis_added", hid="h001")
        assert (await watched_stream.next_event()).seq == seq
        assert [f for f in await other_stream.drain(0.6) if not f.is_heartbeat] == []

        back = await emit(store, other, "hypothesis_added", hid="h001")
        assert (await other_stream.next_event()).seq == back
        assert [f for f in await watched_stream.drain(0.6) if not f.is_heartbeat] == []


async def test_notifications_from_another_schema_are_ignored(app, store, settings):
    """The channel is database-wide; the payload's schema is what makes it ours.

    Every test session runs in its own `test_<hex>` schema of the live database, so
    without this filter one session's events would be delivered to another's watchers —
    and to a real one.
    """
    run_id = make_run(store)

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        payload = f'{{"schema": "some_other_schema", "run_id": "{run_id}", "seq": 999999}}'
        await asyncio.to_thread(_notify, settings, payload)
        assert [f for f in await stream.drain(0.8) if not f.is_heartbeat] == []

        # …and the same run in this schema still gets through, so the filter is a filter
        # and not a wall.
        seq = await emit(store, run_id, "round_started", round=1)
        assert (await stream.next_event()).seq == seq


async def test_unknown_run_is_a_404_in_the_error_envelope(app):
    response = await asgi_request(app, "GET", f"/api/runs/{uuid4()}/events")

    assert response.status == 404
    assert response.json()["code"] == "run_not_found"


async def test_disconnect_releases_the_subscription(app, store):
    run_id = make_run(store)
    listener = app.state.run_event_listener

    async with open_sse(app, f"/api/runs/{run_id}/events") as stream:
        await stream.next_frame(timeout=HEARTBEAT_SECONDS * 8)
        assert listener.health()["subscribers"] == 1

    for _ in range(50):
        if listener.health()["subscribers"] == 0:
            break
        await asyncio.sleep(0.05)
    assert listener.health()["subscribers"] == 0


class TestGuardedByAToken:
    """With APP_AUTH_TOKEN set, the stream is reachable only by ticket or by token."""

    @pytest.fixture
    def settings(self, isolated_schema) -> Settings:
        return fixture_settings(
            SSE_HEARTBEAT_SECONDS=HEARTBEAT_SECONDS, APP_AUTH_TOKEN=TOKEN
        )

    async def ticket_for(self, app, run_id: UUID) -> str:
        response = await asgi_request(
            app,
            "POST",
            f"/api/runs/{run_id}/events/ticket",
            headers={"X-Coscientist-Token": TOKEN},
        )
        assert response.status == 200, response.body
        body = response.json()
        assert body["expires_in"] == 60
        return body["ticket"]

    async def test_stream_without_a_credential_is_refused(self, app, store):
        run_id = make_run(store)
        response = await asgi_request(app, "GET", f"/api/runs/{run_id}/events")

        assert response.status == 401

    async def test_ticket_endpoint_itself_needs_the_token(self, app, store):
        run_id = make_run(store)
        response = await asgi_request(app, "POST", f"/api/runs/{run_id}/events/ticket")

        assert response.status == 401

    async def test_a_ticket_opens_the_stream_once(self, app, store):
        run_id = make_run(store)
        ticket = await self.ticket_for(app, run_id)
        seq = await emit(store, run_id, "round_started", round=1)

        async with open_sse(app, f"/api/runs/{run_id}/events?ticket={ticket}") as stream:
            assert (await stream.next_event()).seq == seq

        spent = await asgi_request(app, "GET", f"/api/runs/{run_id}/events?ticket={ticket}")
        assert spent.status == 401

    async def test_a_ticket_is_no_good_for_another_run(self, app, store):
        run_id = make_run(store, harness="claude")
        other = make_run(store, harness="codex")
        ticket = await self.ticket_for(app, run_id)

        response = await asgi_request(app, "GET", f"/api/runs/{other}/events?ticket={ticket}")
        assert response.status == 401

    async def test_an_expired_ticket_is_refused(self, app, store, monkeypatch):
        run_id = make_run(store)
        ticket = await self.ticket_for(app, run_id)
        monkeypatch.setattr(TICKETS, "_clock", lambda: time.monotonic() + 61)

        response = await asgi_request(app, "GET", f"/api/runs/{run_id}/events?ticket={ticket}")
        assert response.status == 401

    async def test_the_long_lived_token_still_works_for_curl(self, app, store):
        run_id = make_run(store)
        seq = await emit(store, run_id, "round_started", round=1)

        async with open_sse(app, f"/api/runs/{run_id}/events?token={TOKEN}") as stream:
            assert (await stream.next_event()).seq == seq

    async def test_a_query_credential_opens_nothing_but_the_stream(self, app, store):
        """`?token=` is an SSE affordance, not a second way in to the whole API."""
        run_id = make_run(store)
        response = await asgi_request(app, "GET", f"/api/runs/{run_id}/detail?token={TOKEN}")

        assert response.status == 401


def _notify(settings: Settings, payload: str) -> None:
    with get_session_factory(settings)() as session:
        session.execute(text("select pg_notify('run_events', :payload)"), {"payload": payload})
        session.commit()

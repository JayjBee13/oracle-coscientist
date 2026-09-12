"""SSE tickets: one run, one use, one minute."""

from __future__ import annotations

from app.services.events.tickets import TICKET_TTL_SECONDS, TicketStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_ticket_opens_its_own_run():
    store = TicketStore()
    ticket = store.issue("run-a")

    assert store.consume(ticket, "run-a") is True


def test_a_ticket_is_spent_by_its_first_use():
    store = TicketStore()
    ticket = store.issue("run-a")
    store.consume(ticket, "run-a")

    assert store.consume(ticket, "run-a") is False


def test_a_ticket_is_no_good_for_another_run():
    store = TicketStore()
    ticket = store.issue("run-a")

    assert store.consume(ticket, "run-b") is False
    # …and it is spent either way: presenting one against the wrong run is a bug or an
    # attempt, and neither gets a second try.
    assert store.consume(ticket, "run-a") is False


def test_a_ticket_expires_after_a_minute():
    clock = FakeClock()
    store = TicketStore(clock=clock)
    ticket = store.issue("run-a")

    clock.now += TICKET_TTL_SECONDS + 1
    assert store.consume(ticket, "run-a") is False


def test_a_ticket_still_works_just_before_it_expires():
    clock = FakeClock()
    store = TicketStore(clock=clock)
    ticket = store.issue("run-a")

    clock.now += TICKET_TTL_SECONDS - 1
    assert store.consume(ticket, "run-a") is True


def test_an_unknown_ticket_is_refused():
    assert TicketStore().consume("made-up", "run-a") is False


def test_expired_tickets_do_not_accumulate():
    """Nobody redeems most tickets — a page that never opened its stream leaves one."""
    clock = FakeClock()
    store = TicketStore(clock=clock)
    for _ in range(5):
        store.issue("run-a")

    clock.now += TICKET_TTL_SECONDS + 1
    store.issue("run-b")

    assert len(store) == 1

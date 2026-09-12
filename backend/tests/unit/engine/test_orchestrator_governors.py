"""The two governors that stop a run, tested without a database or a model.

Both of these are timing-shaped, and timing-shaped invariants asserted from elapsed wall
clock are the flakiest tests there are. They are pinned here instead, against the
orchestrator's own state: whether a second rate limit opens a second cooldown, and whether
the wall-clock ceiling announces itself exactly once and asks for the graceful ending.

`tests/integration/engine/test_full_fake_run.py` covers the same two through a real run —
that a limited run still completes with its report, and that a run out of time stops early
and keeps its overview. This file covers the parts a full run cannot observe directly.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import pytest

from app.engine.core import RunConfig
from app.engine.orchestrator import Orchestrator, _Directive
from app.engine.runners import role_config


class StubStore:
    """Just enough `RunStore` for the governors: a live run, a ledger, and an event sink."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.alive = True
        self.control: str | None = None

    def heartbeat(self, run_id, **_: Any) -> bool:
        return self.alive

    def get_control(self, run_id) -> str | None:
        return self.control

    def budget_totals(self, run_id) -> dict[str, Any]:
        return {
            "calls_used": 7,
            "budget_calls": 150,
            "spend_usd": 1.25,
            "budget_usd": 0.0,
        }

    def emit(self, run_id, type, payload=None, **_: Any) -> dict[str, Any]:
        self.events.append((type, dict(payload or {})))
        return {"seq": len(self.events)}


def orchestrator(store: StubStore, **config: Any) -> Orchestrator:
    engine = Orchestrator(store, runner=object(), run_id=uuid4(), rate_limit_cooldown=60.0)
    engine._config = RunConfig(**config)
    return engine


def events_of(store: StubStore, type: str) -> list[dict[str, Any]]:
    return [payload for kind, payload in store.events if kind == type]


# --- rate-limit backpressure --------------------------------------------------------------


async def test_engaging_backpressure_closes_the_gate_workers_wait_on():
    engine = orchestrator(StubStore())

    assert engine._ready.is_set(), "a healthy run spawns freely"
    engine._engage_backpressure()

    assert not engine._ready.is_set(), "no worker may take a slot while the limit bites"
    assert len(engine._background) == 1


async def test_a_second_limit_inside_an_open_cooldown_does_not_open_a_second_one():
    """Stacking a cooldown per report would push a run out to an unbounded wait: a
    plan-window limit reports on every call in the wave, not once for the wave."""
    engine = orchestrator(StubStore())

    engine._engage_backpressure()
    engine._engage_backpressure()
    engine._engage_backpressure()

    assert len(engine._background) == 1, "one window, however many reports arrive inside it"


async def test_the_gate_reopens_when_the_cooldown_elapses():
    engine = orchestrator(StubStore())
    engine._rate_limit_cooldown = 0.01

    engine._engage_backpressure()
    await asyncio.sleep(0.05)

    assert engine._ready.is_set(), "the run resumes on its own; nothing has to poke it"
    assert not engine._background, "and the cooldown task cleans itself up"


async def test_a_cooldown_cancelled_before_it_ever_ran_still_reopens_the_gate():
    """The teardown case, and the one a `finally` inside the coroutine would miss: a task
    cancelled before its first step never enters its own body, so the gate has to be
    reopened from a done callback. A run torn down mid-cooldown must not leave workers
    parked on an event nothing will ever set."""
    engine = orchestrator(StubStore())

    engine._engage_backpressure()
    for task in list(engine._background):
        task.cancel()  # synchronously, before the loop has given the task a single step
    await asyncio.gather(*engine._background, return_exceptions=True)
    await asyncio.sleep(0)  # let the done callbacks run

    assert engine._ready.is_set()
    assert not engine._background


# --- the wall-clock ceiling ---------------------------------------------------------------


def test_a_run_without_a_ceiling_arms_no_clock():
    engine = orchestrator(StubStore(), wall_clock_minutes=None)

    engine._start_clock()

    assert engine._deadline is None
    assert engine._out_of_time_now() is False


def test_a_run_inside_its_ceiling_is_not_out_of_time():
    engine = orchestrator(StubStore(), wall_clock_minutes=60)

    engine._start_clock()

    assert engine._deadline is not None
    assert engine._out_of_time_now() is False


def test_passing_the_ceiling_announces_it_once_and_asks_to_finish():
    store = StubStore()
    engine = orchestrator(store, wall_clock_minutes=30)
    engine._start_clock()
    engine._deadline = 0.0  # the run has been going for longer than any monotonic clock

    assert engine._out_of_time_now() is True
    assert engine._out_of_time_now() is True, "the state sticks"

    warnings = events_of(store, "budget_warning")
    assert len(warnings) == 1, "announced once, not once per boundary"
    assert warnings[0]["reason"] == "wall_clock"
    assert warnings[0]["wall_clock_minutes"] == 30
    # The ledger numbers ride along so the Activity tab renders this like any other ceiling.
    assert warnings[0]["calls_used"] == 7
    assert warnings[0]["budget_calls"] == 150


def test_the_boundary_turns_a_passed_ceiling_into_a_finish_not_a_stop():
    """`finish` and not `stop` on purpose: the run ran out of time, it was not cancelled,
    so it ends `completed` with the report it earned rather than as a stopped run."""
    store = StubStore()
    engine = orchestrator(store, wall_clock_minutes=30)
    engine._start_clock()
    engine._deadline = 0.0

    assert engine._boundary() is _Directive.FINISH
    assert engine._finish_requested is True
    assert engine._stop_requested is False


def test_a_passed_ceiling_unwinds_the_round_it_is_in():
    """Mid-round, between steps — the same path budget exhaustion takes. A ceiling that
    lets the run keep spending until the round ends is not a ceiling."""
    engine = orchestrator(StubStore(), wall_clock_minutes=30)
    engine._start_clock()
    engine._deadline = 0.0

    assert engine._interrupted() is True


def test_a_finish_request_does_not_unwind_the_round_the_way_a_ceiling_does():
    """The distinction the two share a flag for: `finish` means "stop after this round"."""
    store = StubStore()
    store.control = "finish"
    engine = orchestrator(store, wall_clock_minutes=None)
    engine._start_clock()

    assert engine._interrupted() is False
    assert engine._finish_requested is True


@pytest.mark.parametrize("minutes", [0.0, None])
def test_a_falsy_ceiling_is_no_ceiling(minutes):
    engine = orchestrator(StubStore(), wall_clock_minutes=minutes)

    engine._start_clock()

    assert engine._deadline is None


# --- the retry policy ---------------------------------------------------------------------
#
# A timeout retried against the deadline it already broke is a second failure bought at full
# price. Run c4566ed2 spent 10 attempts on 5 timed-out units and recovered 2, both of which
# only ever fit inside the same wall — the other 6 attempts were 42 minutes of a 52-minute
# run, and the budget calls they burned are why the report was never written.


def a_cfg(timeout_s: float = 420.0):
    return role_config(
        "generation",
        model="claude-opus-5",
        effort="high",
        system_prompt="…",
    ).with_timeout(timeout_s)


def test_a_timeout_is_retried_on_a_doubled_deadline():
    engine = orchestrator(StubStore())

    retried = engine._retry_terms(a_cfg(420.0), "timeout after 431s (limit 420s, killed)")

    assert retried is not None
    assert retried.timeout_s == 840.0


def test_a_failure_that_is_not_a_timeout_is_retried_exactly_as_it_was():
    engine = orchestrator(StubStore())
    cfg = a_cfg(420.0)

    assert engine._retry_terms(cfg, "OSError: [WinError 216]") == cfg


def test_a_doubling_retry_cannot_outlive_the_wall_clock():
    """A ceiling the retry is allowed to walk through is not a ceiling."""
    engine = orchestrator(StubStore(), wall_clock_minutes=30)
    engine._start_clock()

    assert engine._retry_terms(a_cfg(420.0), "timeout after 421s").timeout_s < 1800.0

    engine._deadline = time.monotonic() + 60  # a minute left
    assert engine._retry_terms(a_cfg(420.0), "timeout after 421s") is None, (
        "there is no room to try on different terms, so there is nothing to try"
    )

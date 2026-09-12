"""The canonical event vocabulary: closed, and enforced at the call site."""

from __future__ import annotations

import pytest

from app.engine.events import REQUIRED_PAYLOAD_FIELDS, EventType, EventWriter, validate_event

# Plan C3's list, written out again here on purpose: this test fails if the enum drifts
# from the plan, which is the only way anyone would notice.
#
# One type postdates the plan and is listed separately rather than folded in, so that the
# comparison below still says exactly what the plan said and what was added to it since.
PLAN_C3_TYPES = {
    "round_started",
    "hypothesis_added",
    "review_recorded",
    "cluster_applied",
    "graft_fired",
    "graft_abstained",
    "match_completed",
    "feedback_recorded",
    "round_completed",
    "call_started",
    "call_finished",
    "rate_limited",
    "role_degraded",
    "contract_violation",
    "note_added",
    "hypothesis_archived",
    "budget_warning",
    "lifecycle_changed",
    "run_finished",
    "run_failed",
}

ADDED_SINCE_THE_PLAN = {
    # The audit record for the one sanctioned exception to `config` being immutable after
    # launch: the scientist gave an ended run more rounds. See `EventType.RUN_EXTENDED`.
    "run_extended",
    # Plan C3 had a per-unit failure event and nothing that said the *round* was poorer
    # for it, and no event at all for the run ending with no report. Both gaps were
    # invisible in the product for exactly as long as they existed.
    "step_failed",
    "report_skipped",
    # A schema-repair re-ask was the only failure class in the loop that left no trace at
    # all — `call_finished` had already gone out with `ok: true` — and documents that did
    # not fit the context cap were reported nowhere the scientist could look.
    "schema_repaired",
    "context_truncated",
    "research_updated",
}


class FakeStore:
    def __init__(self):
        self.rows = []

    def emit(self, run_id, type, payload=None, *, round=None):
        row = {"run_id": run_id, "type": type, "payload": payload, "round": round}
        self.rows.append(row)
        return {"seq": len(self.rows), **row}


def test_the_enum_is_the_plans_list_plus_what_was_deliberately_added():
    assert {member.value for member in EventType} == PLAN_C3_TYPES | ADDED_SINCE_THE_PLAN


def test_run_extended_records_the_size_of_the_exception_it_documents():
    """An extension nobody can measure from the log is not an audit record. The event has to
    carry both sides of every number it changed, and where the run was continued from.

    Both ceilings, not just the calls one: continuing can raise or remove the cost ceiling
    an older run was launched with, and "this run stopped at $5.00 and was let past it" is
    the line somebody will come looking for.
    """
    assert set(REQUIRED_PAYLOAD_FIELDS[EventType.RUN_EXTENDED]) == {
        "added_rounds",
        "rounds_target",
        "previous_rounds_target",
        "budget_calls",
        "previous_budget_calls",
        "budget_usd",
        "previous_budget_usd",
        "previous_lifecycle",
    }

    with pytest.raises(ValueError, match="previous_rounds_target"):
        validate_event("run_extended", {"added_rounds": 2, "rounds_target": 3})


def test_a_removed_cost_ceiling_records_as_no_ceiling_rather_than_as_missing():
    """`None` is a value on these two keys, not an omission.

    A run continued out of a $5.00 ceiling records `budget_usd: None`, and a reader has to
    be able to tell that from a zero — "capped at $0.00" is a different and false claim —
    and from an absent key, which would say the event never carried the number at all.
    """
    payload = {
        "added_rounds": 1,
        "rounds_target": 3,
        "previous_rounds_target": 3,
        "budget_calls": 67,
        "previous_budget_calls": 67,
        "budget_usd": None,
        "previous_budget_usd": 5.0,
        "previous_lifecycle": "completed",
    }

    assert validate_event("run_extended", payload) is EventType.RUN_EXTENDED

    with pytest.raises(ValueError, match="budget_usd"):
        validate_event("run_extended", {k: v for k, v in payload.items() if k != "budget_usd"})


def test_every_type_documents_its_payload():
    assert set(REQUIRED_PAYLOAD_FIELDS) == set(EventType)


def test_call_started_carries_role_model_and_round():
    assert REQUIRED_PAYLOAD_FIELDS[EventType.CALL_STARTED] == ("role", "model", "round")


def test_an_unknown_type_is_refused_and_the_message_lists_the_known_ones():
    with pytest.raises(ValueError, match="unknown run event type"):
        validate_event("hypothesis_invented", {})


def test_a_payload_missing_a_documented_field_is_refused():
    with pytest.raises(ValueError, match="novelty_level"):
        validate_event(EventType.REVIEW_RECORDED, {"hid": "h001", "verdict": "pass"})


def test_a_present_but_null_field_satisfies_the_contract():
    # `novelty_level` is legitimately null when a review omitted it; the requirement is
    # that the key is there, so the frontend never has to guess whether it was dropped.
    validate_event(
        EventType.REVIEW_RECORDED, {"hid": "h001", "verdict": "pass", "novelty_level": None}
    )


def test_extra_keys_are_allowed():
    validate_event(EventType.ROUND_STARTED, {"round": 2, "note": "resumed"})


def test_the_writer_stores_the_canonical_string(monkeypatch):
    store = FakeStore()
    writer = EventWriter(store, "run-1")

    writer.emit(EventType.ROUND_STARTED, {"round": 3}, round=3)

    assert store.rows == [
        {"run_id": "run-1", "type": "round_started", "payload": {"round": 3}, "round": 3}
    ]


def test_the_writer_refuses_before_it_writes():
    store = FakeStore()
    writer = EventWriter(store, "run-1")

    with pytest.raises(ValueError):
        writer.emit(EventType.MATCH_COMPLETED, {"match_id": "m1"})

    assert store.rows == []

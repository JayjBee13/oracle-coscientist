"""The canonical `run_events.type` vocabulary and the payload each type carries.

`RunStore.emit` stores whatever type string it is handed — deliberately, so the store has
no opinion about the engine's vocabulary. This module is where the vocabulary is defined
and where it is enforced: every emit in the orchestrator goes through `EventWriter`, which
rejects an unknown type or a payload missing a documented field *before* the row is
written. A typo therefore fails in the orchestrator's own tests rather than silently
producing an event the frontend filters out and nobody ever sees.

The stream these events form is the product's live view, its resume log and its post-mortem
record, so two rules hold everywhere:

* **One writer.** Events are emitted from the orchestrator's main coroutine only. `seq` is
  allocated at INSERT, and a subscriber resuming from "everything after N" is only correct
  when commit order matches seq order (see `store` module docstring).
* **Payloads are small and literal.** They carry ids and numbers the UI can render without
  a second query — never whole hypothesis bodies. `RunStore.cap_payload` truncates anything
  over 8KB and flags it, but a payload that needs truncating is usually a design mistake.

Payload shapes, by type:

| type | payload |
|---|---|
| `round_started` | `{round}` |
| `hypothesis_added` | `{hid, title, round, source, operator?, parent_ids?, seed_id?}` |
| `review_recorded` | `{hid, verdict, novelty_level}` |
| `cluster_applied` | `{round, n_clusters, labelled, duplicates}` |
| `graft_fired` | `{round, votes, n_clusters, hhi, source_domain, seed_id}` |
| `graft_abstained` | `{round, reason, votes, n_clusters, hhi}` |
| `match_completed` | `{match_id, round, hid_a, hid_b, winner, k,` |
| | `  elo_a_before, elo_a_after, elo_b_before, elo_b_after}` |
| `feedback_recorded` | `{round, guidance}` |
| `round_completed` | `{round, hypotheses_added, reviews, matches_completed}` |
| `call_started` | `{role, model, round}` |
| `call_finished` | `{role, model, round, ok, duration_ms?, error?, telemetry?}` |
| | `  telemetry: {permission_denials, web_search_requests, num_turns, …}` |
| `rate_limited` | `{role, round}` |
| `role_degraded` | `{role, model, reason}` |
| `contract_violation` | `{role, round, error, unit?}` |
| `step_failed` | `{role, round, requested, produced, units?}` |
| `schema_repaired` | `{role, round, attempt, problem, unit?}` |
| `context_truncated` | `{cap, included, truncated, omitted}` |
| `report_skipped` | `{reason, detail}` |
| `note_added` | `{text}` |
| `hypothesis_archived` | `{hid}` |
| `budget_warning` | `{reason, calls_used, budget_calls, spend_usd, budget_usd}` |
| `lifecycle_changed` | `{lifecycle, previous?}` |
| `run_extended` | `{added_rounds, rounds_target, previous_rounds_target,` |
| | `  budget_calls, previous_budget_calls,` |
| | `  budget_usd, previous_budget_usd, previous_lifecycle}` |
| | `  the two usd values are null when there is no cost ceiling` |
| `run_finished` | `{lifecycle, rounds_completed, hypotheses, calls_used, reason, lost_steps}` |
| `run_failed` | `{error}` |

Optional keys (`?`) are omitted when they do not apply; every other key is required and
`EventWriter.emit` raises if it is missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from app.engine.store import RunStore

__all__ = ["REQUIRED_PAYLOAD_FIELDS", "EventType", "EventWriter", "validate_event"]


class EventType(StrEnum):
    """Every value `run_events.type` may hold. Plan C3's list, in loop order."""

    ROUND_STARTED = "round_started"
    RESEARCH_UPDATED = "research_updated"
    HYPOTHESIS_ADDED = "hypothesis_added"
    REVIEW_RECORDED = "review_recorded"
    CLUSTER_APPLIED = "cluster_applied"
    GRAFT_FIRED = "graft_fired"
    GRAFT_ABSTAINED = "graft_abstained"
    MATCH_COMPLETED = "match_completed"
    FEEDBACK_RECORDED = "feedback_recorded"
    ROUND_COMPLETED = "round_completed"
    CALL_STARTED = "call_started"
    CALL_FINISHED = "call_finished"
    RATE_LIMITED = "rate_limited"
    ROLE_DEGRADED = "role_degraded"
    CONTRACT_VIOLATION = "contract_violation"

    STEP_FAILED = "step_failed"
    """A whole step of a round produced less than it was asked for, or nothing at all.

    `contract_violation` is per unit and says a *call* failed. This says the round is
    poorer for it, which is a different fact and the one a reader needs: run c4566ed2's
    round 2 emitted two contract violations for two failed generation shards and then went
    on to spend a proximity call and four ranking calls on a pool that had not changed, and
    nothing anywhere said "this round added no hypotheses"."""

    SCHEMA_REPAIRED = "schema_repaired"
    """A call came back clean but failed its schema, and was re-asked at full price.

    The one failure class in the loop that left no trace at all. `_record_call` has already
    emitted `call_finished` with `ok: true` and written an `ok` ledger row by the time
    `validate_role_output` rejects the payload, so the scientist saw N successful calls and
    N−k results with no way to reconcile them: run c4566ed2 has 24 `ok` reflection ledger
    rows and 21 reviews, and neither the event log nor the 20-line `supervisor.log`
    mentions any of the three re-asks that cost $0.95 and four minutes."""

    CONTEXT_TRUNCATED = "context_truncated"
    """Documents the scientist attached did not fit the per-call cap.

    Emitted once at run start. The cut was computed from the first run onward and reported
    nowhere a scientist could see it — the Settings tab showed each document's full
    character count, which reads as confirmation that it was used."""

    REPORT_SKIPPED = "report_skipped"
    """The run ended without writing its overview, and why.

    Two of 29 app runs finished `completed` with no deliverable at all, and the only trace
    was `has_overview: false` buried in the `run_finished` payload. A missing report is the
    most consequential thing that can happen to a run and it had no event of its own."""

    NOTE_ADDED = "note_added"
    HYPOTHESIS_ARCHIVED = "hypothesis_archived"
    BUDGET_WARNING = "budget_warning"
    LIFECYCLE_CHANGED = "lifecycle_changed"
    RUN_EXTENDED = "run_extended"
    """The scientist gave an ended run more rounds — see `services/runs/controls.continue`.

    The one type here that postdates plan C3's list, and the only one emitted by a request
    handler rather than by the loop. It exists because continuing is the single sanctioned
    exception to `config` being immutable after launch, and an exception that is not written
    down is not audited: a reader asking why a run has five rounds when its config was
    launched with three has to find one line that says so, by how much, and from which
    ending. `lifecycle_changed` records the `completed → queued` move in the same breath but
    carries none of that.
    """

    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


REQUIRED_PAYLOAD_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.RESEARCH_UPDATED: ("phase", "round", "detail"),
    EventType.ROUND_STARTED: ("round",),
    EventType.HYPOTHESIS_ADDED: ("hid", "title", "round", "source"),
    EventType.REVIEW_RECORDED: ("hid", "verdict", "novelty_level"),
    EventType.CLUSTER_APPLIED: ("round", "n_clusters", "labelled", "duplicates"),
    EventType.GRAFT_FIRED: ("round", "votes", "n_clusters", "hhi", "source_domain"),
    EventType.GRAFT_ABSTAINED: ("round", "reason", "votes", "n_clusters", "hhi"),
    EventType.MATCH_COMPLETED: (
        "match_id",
        "round",
        "hid_a",
        "hid_b",
        "winner",
        "elo_a_before",
        "elo_a_after",
        "elo_b_before",
        "elo_b_after",
        "k",
    ),
    EventType.FEEDBACK_RECORDED: ("round", "guidance"),
    EventType.ROUND_COMPLETED: ("round", "hypotheses_added", "reviews", "matches_completed"),
    EventType.CALL_STARTED: ("role", "model", "round"),
    EventType.CALL_FINISHED: ("role", "model", "round", "ok"),
    EventType.RATE_LIMITED: ("role", "round"),
    EventType.ROLE_DEGRADED: ("role", "model", "reason"),
    EventType.CONTRACT_VIOLATION: ("role", "round", "error"),
    EventType.STEP_FAILED: ("role", "round", "requested", "produced"),
    EventType.SCHEMA_REPAIRED: ("role", "round", "attempt", "problem"),
    EventType.CONTEXT_TRUNCATED: ("cap", "included", "truncated", "omitted"),
    EventType.REPORT_SKIPPED: ("reason", "detail"),
    EventType.NOTE_ADDED: ("text",),
    EventType.HYPOTHESIS_ARCHIVED: ("hid",),
    EventType.BUDGET_WARNING: ("reason", "calls_used", "budget_calls", "spend_usd", "budget_usd"),
    EventType.LIFECYCLE_CHANGED: ("lifecycle",),
    EventType.RUN_EXTENDED: (
        "added_rounds",
        "rounds_target",
        "previous_rounds_target",
        "budget_calls",
        "previous_budget_calls",
        # Required, and nullable: `None` is the honest reading of "no ceiling", which is
        # both the system default and what continuing a run capped in dollars usually
        # leaves behind. A key that could be absent would make those indistinguishable
        # from an older event that never carried one.
        "budget_usd",
        "previous_budget_usd",
        "previous_lifecycle",
    ),
    EventType.RUN_FINISHED: (
        "lifecycle",
        "rounds_completed",
        "hypotheses",
        "calls_used",
        # What the run ended *as* was never the same question as why it ended. Budget
        # exhaustion, an expired wall clock, a scientist's finish request and an honest
        # completion of every planned round all wrote the identical payload, so the run's
        # own permanent record could not tell a success from an abort.
        "reason",
        "lost_steps",
    ),
    EventType.RUN_FAILED: ("error",),
}


def validate_event(type: str | EventType, payload: Mapping[str, Any] | None) -> EventType:
    """Return the canonical type, or raise if the type or payload is not the contract."""
    try:
        event_type = EventType(type)
    except ValueError:
        known = ", ".join(sorted(member.value for member in EventType))
        raise ValueError(f"unknown run event type {type!r}; expected one of: {known}") from None

    missing = [
        field
        for field in REQUIRED_PAYLOAD_FIELDS[event_type]
        if field not in (payload or {})
    ]
    if missing:
        raise ValueError(
            f"{event_type.value} payload is missing {', '.join(missing)} "
            f"(required: {', '.join(REQUIRED_PAYLOAD_FIELDS[event_type])})"
        )
    return event_type


class EventWriter:
    """Validating front door to `RunStore.emit`, bound to one run.

    Hold exactly one of these per live run and emit through it from a single coroutine.
    """

    __slots__ = ("_run_id", "_store")

    def __init__(self, store: RunStore, run_id: UUID) -> None:
        self._store = store
        self._run_id = run_id

    def emit(
        self,
        type: str | EventType,
        payload: Mapping[str, Any] | None = None,
        *,
        round: int | None = None,
    ) -> dict[str, Any]:
        event_type = validate_event(type, payload)
        return self._store.emit(self._run_id, event_type.value, dict(payload or {}), round=round)

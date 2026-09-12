"""RunStore is the only writer the engine has; these tests pin its contract.

Everything here runs against the session's throwaway `test_<hex>` schema (see the root
conftest), which is migrated with Alembic — so the NOTIFY trigger and every server default
are the production ones, not a metadata.create_all approximation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.engine.store import MAX_PAYLOAD_BYTES, BudgetExhausted, RunStore

ENGINE_TABLES = (
    "run_events",
    "budget_ledger",
    "graft_events",
    "matches",
    "reviews",
    "hypotheses",
    "run_context_docs",
    "workshop_options",
    "workshops",
    "runs",
)


@pytest.fixture(scope="session")
def session_factory(isolated_schema):
    return get_session_factory(get_settings())


@pytest.fixture
def store(session_factory):
    with session_factory() as session:
        schema = session.execute(text("select current_schema()")).scalar_one()
        assert schema.startswith("test_"), f"refusing to truncate live schema {schema!r}"
        session.execute(
            text(f"TRUNCATE {', '.join(ENGINE_TABLES)} RESTART IDENTITY CASCADE")
        )
        session.commit()
    return RunStore(session_factory)


def make_run(store: RunStore, **overrides):
    kwargs = {
        "question": "Why do tardigrades survive vacuum?",
        "prompt": "Investigate cryptobiosis mechanisms.",
        "config": {"rounds": 3, "budget_calls": 40, "budget_usd": 5.0},
        "harness": "demo",
        "root_path": "engines/runs/run-test",
    }
    kwargs.update(overrides)
    return store.create_run(**kwargs)


# --- run creation ---------------------------------------------------------------------


def test_create_run_returns_id_and_derives_title_and_engine_run_id(store):
    run_id = make_run(store, question="A" * 200)

    run = store.get_run(run_id)
    assert run["id"] == str(run_id)
    assert run["lifecycle"] == "queued"
    assert run["source"] == "app"
    assert len(run["title"]) <= 60
    assert run["engine_run_id"].startswith("run-")
    assert run["budget_calls"] == 40
    assert run["budget_usd"] == pytest.approx(5.0)
    assert run["config"]["rounds"] == 3


def test_create_run_stores_context_docs(store):
    run_id = make_run(store, context_docs=[{"name": "brief.md", "text": "hello"}])

    docs = store.get_context_docs(run_id)
    assert [(doc["name"], doc["chars"], doc["content"]) for doc in docs] == [
        ("brief.md", 5, "hello")
    ]


def test_create_run_rejects_an_app_run_without_a_call_ceiling(store):
    """Calls are the governor: it is the ceiling that stops a runaway loop."""
    with pytest.raises(ValueError, match="budget_calls"):
        make_run(store, config={"rounds": 1, "budget_usd": 5.0})


def test_an_app_run_needs_no_dollar_ceiling(store):
    """These are CLI calls on a subscription, so a dollar figure is telemetry rather than
    money. A stored 0 is what "no ceiling" has always looked like."""
    run_id = make_run(store, config={"rounds": 1, "budget_calls": 10})

    run = store.get_run(run_id)
    assert run["budget_calls"] == 10
    assert float(run["budget_usd"] or 0) == 0


def test_a_run_without_a_dollar_ceiling_never_exhausts_on_dollars(store):
    run_id = make_run(store, config={"rounds": 1, "budget_calls": 10})

    totals = store.spend(run_id, role="generation", cost_usd=999.0)

    assert totals["spend_usd"] == 999.0, "the ledger still records what the call reported"
    assert store.check_budget(run_id)["budget_usd"] == 0, "and nothing refuses the next step"


def test_a_dollar_ceiling_is_still_enforced_when_one_is_set(store):
    """Kept working for whoever points this engine at a metered API key."""
    run_id = make_run(store, config={"rounds": 1, "budget_calls": 100, "budget_usd": 1.0})

    with pytest.raises(BudgetExhausted, match="usd"):
        store.spend(run_id, role="generation", cost_usd=1.5)


def test_imported_runs_need_no_budget(store):
    run_id = make_run(store, source="imported", config={}, harness="claude", lifecycle="completed")

    assert store.get_run(run_id)["budget_calls"] == 0


def test_one_active_run_per_harness_but_demo_has_its_own_lane(store):
    make_run(store, harness="claude", lifecycle="running")
    make_run(store, harness="demo", lifecycle="running")

    with pytest.raises(IntegrityError):
        make_run(store, harness="claude", lifecycle="running")


def test_a_paused_run_does_not_hold_the_lane(store):
    make_run(store, harness="claude", lifecycle="paused")

    make_run(store, harness="claude", lifecycle="running")  # must not raise


# --- hypotheses and reviews -----------------------------------------------------------


def test_add_hypothesis_assigns_sequential_hids_per_run(store):
    first = make_run(store, harness="claude")
    second = make_run(store, harness="demo")

    a = store.add_hypothesis(first, title="Alpha", body_md="body", created_round=1)
    b = store.add_hypothesis(first, title="Beta", body_md="body", created_round=1)
    c = store.add_hypothesis(second, title="Gamma", body_md="body", created_round=1)

    assert [a["hid"], b["hid"], c["hid"]] == ["h001", "h002", "h001"]
    assert a["elo"] == 1200
    assert a["status"] == "active"


def test_add_hypothesis_records_lineage(store):
    run_id = make_run(store)
    parent = store.add_hypothesis(run_id, title="Parent", body_md="b", created_round=1)

    child = store.add_hypothesis(
        run_id,
        title="Child",
        body_md="b",
        created_round=2,
        parent_ids=[parent["hid"]],
        operator="combination",
        seed_id="seed-1",
    )

    assert child["parent_ids"] == ["h001"]
    assert child["operator"] == "combination"
    assert child["seed_id"] == "seed-1"


def test_record_review_rejects_the_hypothesis_when_the_verdict_is_reject(store):
    run_id = make_run(store)
    kept = store.add_hypothesis(run_id, title="Kept", body_md="b", created_round=1)
    dropped = store.add_hypothesis(run_id, title="Dropped", body_md="b", created_round=1)

    store.record_review(kept["id"], verdict="pass", novelty_level="high", note="fine")
    store.record_review(dropped["id"], verdict="reject", key_risk="fatal")

    by_hid = {h["hid"]: h for h in store.list_hypotheses(run_id)}
    assert by_hid["h001"]["status"] == "active"
    assert by_hid["h002"]["status"] == "rejected"


def test_hypotheses_lacking_a_review_is_the_reflection_resume_unit(store):
    run_id = make_run(store)
    reviewed = store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)
    store.add_hypothesis(run_id, title="Two", body_md="b", created_round=1)
    store.record_review(reviewed["id"], verdict="pass")

    pending = store.list_hypotheses_without_review(run_id)

    assert [h["hid"] for h in pending] == ["h002"]


def test_apply_clusters_labels_and_marks_duplicates_reversibly(store):
    run_id = make_run(store)
    for title in ("One", "Two"):
        store.add_hypothesis(run_id, title=title, body_md="b", created_round=1)

    store.apply_clusters(
        run_id, {"h001": "thermal", "h002": "thermal"}, duplicates={"h002": "h001"}
    )

    by_hid = {h["hid"]: h for h in store.list_hypotheses(run_id)}
    assert by_hid["h001"]["cluster"] == "thermal"
    assert by_hid["h002"]["duplicate_of"] == "h001"
    assert by_hid["h002"]["status"] == "archived"


def test_archive_hypothesis_applies_now_and_queues_the_event_for_a_live_run(store):
    run_id = make_run(store, lifecycle="running")
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)

    store.archive_hypothesis(run_id, "h001")

    assert store.list_hypotheses(run_id)[0]["status"] == "archived"
    assert [i["kind"] for i in store.claim_interventions(run_id)] == ["hypothesis_archived"]


def test_archive_hypothesis_on_a_finished_run_queues_nothing(store):
    run_id = make_run(store, lifecycle="completed")
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)

    store.archive_hypothesis(run_id, "h001")

    assert store.claim_interventions(run_id) == []


# --- matches --------------------------------------------------------------------------


def test_plan_matches_persists_the_plan_before_any_match_runs(store):
    run_id = make_run(store)
    for title in ("One", "Two", "Three", "Four"):
        store.add_hypothesis(run_id, title=title, body_md="b", created_round=1)

    planned = store.plan_matches(run_id, 1, [("h001", "h002"), ("h003", "h004")])

    assert [(m["hid_a"], m["hid_b"], m["status"]) for m in planned] == [
        ("h001", "h002", "planned"),
        ("h003", "h004", "planned"),
    ]


def test_plan_matches_is_idempotent_so_resume_never_re_derives_pairings(store):
    run_id = make_run(store)
    first = store.plan_matches(run_id, 1, [("h001", "h002")])

    again = store.plan_matches(run_id, 1, [("h009", "h010")])

    assert [m["id"] for m in again] == [m["id"] for m in first]
    assert again[0]["hid_a"] == "h001"


def test_record_match_moves_elo_and_updates_both_hypotheses(store):
    run_id = make_run(store)
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)
    store.add_hypothesis(run_id, title="Two", body_md="b", created_round=1)
    planned = store.plan_matches(run_id, 1, [("h001", "h002")])

    result = store.record_match(
        planned[0]["id"], winner=1, debate_md="A wins", judge_model="claude-sonnet-5"
    )

    assert result["status"] == "completed"
    assert result["elo_a_before"] == 1200
    assert result["elo_a_after"] > 1200
    assert result["elo_b_after"] < 1200
    assert result["k"] == 32
    by_hid = {h["hid"]: h for h in store.list_hypotheses(run_id)}
    assert by_hid["h001"]["wins"] == 1
    assert by_hid["h001"]["matches"] == 1
    assert by_hid["h002"]["wins"] == 0
    assert by_hid["h002"]["matches"] == 1
    assert by_hid["h001"]["elo"] == result["elo_a_after"]


def test_record_match_without_a_winner_is_a_skip_and_moves_no_elo(store):
    run_id = make_run(store)
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)
    store.add_hypothesis(run_id, title="Two", body_md="b", created_round=1)
    planned = store.plan_matches(run_id, 1, [("h001", "h002")])

    result = store.record_match(planned[0]["id"], winner=None, status="skipped")

    assert result["status"] == "skipped"
    assert store.list_hypotheses(run_id)[0]["matches"] == 0


def test_planned_matches_are_the_ranking_resume_unit(store):
    run_id = make_run(store)
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)
    store.add_hypothesis(run_id, title="Two", body_md="b", created_round=1)
    planned = store.plan_matches(run_id, 1, [("h001", "h002"), ("h002", "h001")])
    store.record_match(planned[0]["id"], winner=2)

    outstanding = store.list_matches(run_id, round=1, status="planned")

    assert [m["id"] for m in outstanding] == [planned[1]["id"]]


# --- graft ----------------------------------------------------------------------------


def test_record_graft_stores_the_seed_on_the_run_when_it_fires(store):
    run_id = make_run(store)

    store.record_graft(
        run_id,
        2,
        n_clusters=3,
        hhi=0.61,
        signals={"plateau": True, "hhi": True, "birth_rate": False},
        votes=2,
        fired=True,
        source_domain="mycology",
        skeleton="network foraging",
        seed_framing="Treat the market as a fungal network.",
        seed_id="seed-1",
    )

    run = store.get_run(run_id)
    assert run["graft_state"]["fired_count"] == 1
    assert run["graft_state"]["last_fired_round"] == 2
    assert run["graft_state"]["pending_seed"]["seed_id"] == "seed-1"


def test_an_abstained_graft_records_its_reason_and_leaves_no_seed(store):
    run_id = make_run(store)

    store.record_graft(run_id, 1, n_clusters=0, fired=False, abstained_reason="no_clusters")

    run = store.get_run(run_id)
    assert run["graft_state"].get("pending_seed") is None
    assert store.snapshot(run_id)["graft_events"][0]["abstained_reason"] == "no_clusters"


def test_set_graft_state_clears_the_pending_seed_once_consumed(store):
    run_id = make_run(store)
    store.record_graft(run_id, 2, fired=True, seed_id="seed-1", seed_framing="framing")

    store.set_graft_state(run_id, {"pending_seed": None})

    assert store.get_run(run_id)["graft_state"]["pending_seed"] is None


# --- budget ---------------------------------------------------------------------------


def test_spend_records_all_four_token_classes_and_accumulates(store):
    run_id = make_run(store)

    store.spend(
        run_id,
        role="generation",
        model="claude-sonnet-5",
        round=1,
        tokens_in=100,
        tokens_out=50,
        cache_creation=900,
        cache_read=8000,
        cost_usd=0.25,
        duration_ms=3800,
    )
    store.spend(run_id, role="reflection", tokens_in=10, tokens_out=5, cost_usd=0.05)

    run = store.get_run(run_id)
    assert run["calls_used"] == 2
    assert run["tokens_in"] == 110
    assert run["tokens_out"] == 55
    assert run["spend_usd"] == pytest.approx(0.30)
    ledger = store.snapshot(run_id)["budget"]["by_role"]
    generation = next(row for row in ledger if row["role"] == "generation")
    assert generation["calls"] == 1
    assert generation["tokens"] == 150
    assert generation["usd"] == pytest.approx(0.25)


def test_spend_raises_on_the_call_ceiling_but_still_records_the_call(store):
    run_id = make_run(store, config={"rounds": 1, "budget_calls": 2, "budget_usd": 100.0})
    store.spend(run_id, role="generation", cost_usd=0.01)

    with pytest.raises(BudgetExhausted) as excinfo:
        store.spend(run_id, role="generation", cost_usd=0.01)

    assert excinfo.value.reason == "calls"
    run = store.get_run(run_id)
    assert run["calls_used"] == 2
    assert run["spend_usd"] == pytest.approx(0.02)


def test_spend_raises_on_the_usd_ceiling_independently_of_calls(store):
    run_id = make_run(store, config={"rounds": 1, "budget_calls": 500, "budget_usd": 1.0})

    with pytest.raises(BudgetExhausted) as excinfo:
        store.spend(run_id, role="generation", cost_usd=1.50)

    assert excinfo.value.reason == "usd"
    assert store.get_run(run_id)["spend_usd"] == pytest.approx(1.50)


def test_check_budget_refuses_a_step_that_would_eat_the_overview_reserve(store):
    run_id = make_run(store, config={"rounds": 1, "budget_calls": 4, "budget_usd": 100.0})
    for _ in range(2):
        store.spend(run_id, role="generation", cost_usd=0.01)

    store.check_budget(run_id, cost=1, reserve=0)
    with pytest.raises(BudgetExhausted):
        store.check_budget(run_id, cost=1, reserve=2)


# --- events ---------------------------------------------------------------------------


def test_emit_returns_a_monotonic_seq_and_the_stored_row(store):
    run_id = make_run(store)

    first = store.emit(run_id, "round_started", {"round": 1}, round=1)
    second = store.emit(run_id, "hypothesis_added", {"hid": "h001"}, round=1)

    assert second["seq"] > first["seq"]
    assert first["type"] == "round_started"
    assert first["payload"] == {"round": 1}
    assert first["run_id"] == str(run_id)


def test_emit_truncates_oversized_payloads_and_flags_them(store):
    run_id = make_run(store)

    event = store.emit(run_id, "call_finished", {"role": "ranking", "raw_tail": "x" * 40_000})

    assert event["payload"]["truncated"] is True
    assert event["payload"]["role"] == "ranking"
    assert len(json.dumps(event["payload"]).encode("utf-8")) <= MAX_PAYLOAD_BYTES
    assert len(event["payload"]["raw_tail"]) < 40_000


def test_emit_leaves_payloads_under_the_cap_untouched(store):
    run_id = make_run(store)

    event = store.emit(run_id, "call_started", {"role": "generation", "model": "claude-sonnet-5"})

    assert event["payload"] == {"role": "generation", "model": "claude-sonnet-5"}
    assert "truncated" not in event["payload"]


def test_inserting_an_event_notifies_with_schema_run_id_and_seq(store, session_factory):
    run_id = make_run(store)
    dsn = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    with psycopg.connect(dsn, autocommit=True) as listener:
        schema = listener.execute("select current_schema()").fetchone()[0]
        listener.execute("LISTEN run_events")
        event = store.emit(run_id, "round_started", {"round": 1}, round=1)

        received = None
        for notice in listener.notifies(timeout=15.0):
            payload = json.loads(notice.payload)
            if payload.get("run_id") == str(run_id):
                received = payload
                break

    assert received == {"schema": schema, "run_id": str(run_id), "seq": event["seq"]}


# --- lifecycle, controls, interventions -----------------------------------------------


def test_set_lifecycle_validates_against_the_canonical_enum(store):
    run_id = make_run(store)

    store.set_lifecycle(run_id, "running")
    assert store.get_run(run_id)["lifecycle"] == "running"

    with pytest.raises(ValueError, match="lifecycle"):
        store.set_lifecycle(run_id, "pause_requested")


def test_set_lifecycle_records_the_error_payload_on_failure(store):
    run_id = make_run(store)

    store.set_lifecycle(run_id, "failed", error={"code": "supervisor_crash", "message": "boom"})

    assert store.get_run(run_id)["error"]["code"] == "supervisor_crash"


def test_control_requests_round_trip_and_clear(store):
    run_id = make_run(store, lifecycle="running")

    store.set_control(run_id, "pause")
    assert store.get_control(run_id) == "pause"

    store.set_control(run_id, None)
    assert store.get_control(run_id) is None

    with pytest.raises(ValueError, match="control"):
        store.set_control(run_id, "explode")


def test_heartbeat_reports_false_once_the_run_is_soft_deleted(store):
    run_id = make_run(store, lifecycle="running")

    assert store.heartbeat(run_id, pid=4242) is True
    assert store.get_run(run_id)["supervisor_pid"] == 4242

    store.soft_delete(run_id)
    assert store.heartbeat(run_id) is False


def test_managed_shutdown_parks_only_the_supervisor_identity_it_observed(store):
    run_id = make_run(store, lifecycle="running")
    store.heartbeat(run_id, pid=4242)
    store.set_control(run_id, "pause")

    stale = store.park_for_shutdown(run_id, expected_pid=9999)
    assert stale["changed"] is False
    assert store.get_run(run_id)["lifecycle"] == "running"

    parked = store.park_for_shutdown(run_id, expected_pid=4242)
    run = store.get_run(run_id)
    assert parked["changed"] is True
    assert parked["previous"] == "running"
    assert run["lifecycle"] == "paused"
    assert run["control_requested"] is None
    assert run["supervisor_pid"] is None
    assert run["heartbeat_at"] is None


def test_managed_shutdown_does_not_overwrite_a_run_that_finished(store):
    run_id = make_run(store, lifecycle="running")
    store.heartbeat(run_id, pid=4242)
    store.set_lifecycle(run_id, "completed")

    outcome = store.park_for_shutdown(run_id, expected_pid=4242)

    assert outcome["changed"] is False
    assert store.get_run(run_id)["lifecycle"] == "completed"


def test_notes_are_queued_for_the_round_boundary_and_claimed_once(store):
    run_id = make_run(store, lifecycle="running")

    store.add_note(run_id, "Focus on cheap experiments.")
    store.add_note(run_id, "Ignore anything needing a synchrotron.")

    claimed = store.claim_interventions(run_id)
    assert [item["text"] for item in claimed] == [
        "Focus on cheap experiments.",
        "Ignore anything needing a synchrotron.",
    ]
    assert all(item["kind"] == "note" for item in claimed)
    assert store.claim_interventions(run_id) == []


def test_record_feedback_appends_the_round_guidance(store):
    run_id = make_run(store)

    store.record_feedback(run_id, 1, "Push on mechanism depth.")
    store.record_feedback(run_id, 2, "Stop proposing surveys.")

    assert store.get_run(run_id)["feedback_history"] == [
        {"round": 1, "guidance": "Push on mechanism depth."},
        {"round": 2, "guidance": "Stop proposing surveys."},
    ]


def test_updated_at_moves_when_a_run_changes(store):
    run_id = make_run(store)
    before = store.get_run(run_id)["updated_at"]

    store.set_lifecycle(run_id, "running")

    assert store.get_run(run_id)["updated_at"] > before


# --- snapshot -------------------------------------------------------------------------


def test_snapshot_has_the_run_detail_shape(store):
    run_id = make_run(store)

    snapshot = store.snapshot(run_id)

    assert set(snapshot) == {
        "research",
        "run",
        "config",
        "model_table",
        "leaderboard",
        "rounds",
        "recent_events",
        "budget",
        "graft_events",
        "context_docs",
        "feedback_history",
        "degraded_count",
        "failed_calls",
        "lost_steps",
        "retried_calls",
        "problems",
    }
    assert set(snapshot["run"]) == {
        "id",
        "engine_run_id",
        "source",
        "source_version",
        "title",
            "question",
            "owner_display_name",
        "harness",
        "lifecycle",
        "round",
        "rounds_target",
        "calls_used",
        "budget_calls",
        "spend_usd",
        "tokens_total",
        "model_level",
        "model_level_custom",
        "elapsed_seconds",
        "counts",
        "top",
        "graft",
        "archived",
        "has_overview",
        "failed_calls",
        "lost_steps",
        "ended_reason",
        "overview_skipped_reason",
        "created_at",
        "updated_at",
    }
    assert set(snapshot["budget"]) == {
        "calls_used",
        "budget_calls",
        "spend_usd",
        "budget_usd",
        "by_role",
    }


def test_snapshot_summarises_hypotheses_matches_and_rounds(store):
    run_id = make_run(store, config={"rounds": 2, "budget_calls": 40, "budget_usd": 5.0})
    store.set_round(run_id, 1)
    store.emit(run_id, "round_started", {"round": 1}, round=1)
    winner = store.add_hypothesis(run_id, title="Winner", body_md="b", created_round=1)
    loser = store.add_hypothesis(run_id, title="Loser", body_md="b", created_round=1)
    store.add_hypothesis(run_id, title="Rejected", body_md="b", created_round=1)
    store.record_review(winner["id"], verdict="pass", novelty_level="high")
    store.record_review(loser["id"], verdict="pass", novelty_level="moderate")
    store.record_review(_hid_id(store, run_id, "h003"), verdict="reject")
    planned = store.plan_matches(run_id, 1, [("h001", "h002")])
    store.record_match(planned[0]["id"], winner=1, judge_model="claude-sonnet-5")
    store.emit(run_id, "round_completed", {"round": 1}, round=1)

    snapshot = store.snapshot(run_id)

    assert snapshot["run"]["counts"] == {
        "active": 2,
        "rejected": 1,
        "archived": 0,
        "matches": 1,
    }
    assert [row["hid"] for row in snapshot["leaderboard"]][0] == "h001"
    assert snapshot["leaderboard"][0]["novelty_level"] == "high"
    assert [entry["hid"] for entry in snapshot["run"]["top"]] == ["h001", "h002"]
    round_one = snapshot["rounds"][0]
    assert round_one["round"] == 1
    assert round_one["hypotheses_added"] == 3
    assert round_one["reviews"] == 3
    assert round_one["matches_planned"] == 1
    assert round_one["matches_completed"] == 1
    assert round_one["status"] == "completed"
    assert round_one["started_at"] is not None
    assert round_one["completed_at"] is not None


def test_snapshot_top_falls_back_to_any_status_when_everything_was_rejected(store):
    run_id = make_run(store)
    for title in ("One", "Two"):
        created = store.add_hypothesis(run_id, title=title, body_md="b", created_round=1)
        store.record_review(created["id"], verdict="reject")

    snapshot = store.snapshot(run_id)

    assert snapshot["run"]["counts"]["active"] == 0
    assert [entry["hid"] for entry in snapshot["run"]["top"]] == ["h001", "h002"]


def test_snapshot_keeps_the_last_fifty_events_in_order(store):
    run_id = make_run(store)
    for index in range(60):
        store.emit(run_id, "call_started", {"index": index})

    events = store.snapshot(run_id)["recent_events"]

    assert len(events) == 50
    assert [event["payload"]["index"] for event in events] == list(range(10, 60))
    assert events == sorted(events, key=lambda event: event["seq"])


def test_snapshot_counts_degraded_roles_and_reports_overview_presence(store):
    run_id = make_run(store)
    store.emit(run_id, "role_degraded", {"role": "ranking"})
    store.emit(run_id, "role_degraded", {"role": "evolution"})

    assert store.snapshot(run_id)["degraded_count"] == 2
    assert store.snapshot(run_id)["run"]["has_overview"] is False

    store.set_engine_state(run_id, {"overview_md": "# Findings"})
    assert store.snapshot(run_id)["run"]["has_overview"] is True


def test_snapshot_is_json_serialisable(store):
    run_id = make_run(store, context_docs=[{"name": "brief.md", "text": "hello"}])
    store.spend(run_id, role="generation", cost_usd=Decimal("0.25"))
    store.emit(run_id, "round_started", {"round": 1}, round=1)

    json.dumps(store.snapshot(run_id))


def _hid_id(store: RunStore, run_id, hid: str):
    return next(h["id"] for h in store.list_hypotheses(run_id) if h["hid"] == hid)


# --- the standings are a ranking, not a column sort ---------------------------------------


def test_a_hypothesis_that_never_played_ranks_below_one_that_competed_and_lost(store):
    """`leaderboard` was every row ordered by Elo alone, and the UI prints rank ordinals over
    it verbatim. A hypothesis with no matches sits at the untouched default of 1200, so it
    outranked everything the tournament had actually rated below that — 19 runs in the corpus
    show exactly this, and there is no visual signal for it at all."""
    run_id = make_run(store)
    store.add_hypothesis(
        run_id, title="Fought and lost", body_md="b", created_round=1, elo=1184.0
    )
    store.add_hypothesis(run_id, title="Never played", body_md="b", created_round=2)
    _mark_played(store, run_id, "h001")

    order = [row["hid"] for row in store.snapshot(run_id)["leaderboard"]]

    assert order == ["h001", "h002"], "an unplayed 1200 was ranked above a judged 1184"


def test_removed_hypotheses_fall_to_the_bottom_of_the_standings(store):
    """Rejected and archived rows keep whatever Elo they had and used to hold rank
    positions: 29 runs show one inside the rendered top eight."""
    run_id = make_run(store)
    best = store.add_hypothesis(
        run_id, title="Rejected", body_md="b", created_round=1, elo=1300.0
    )
    store.add_hypothesis(run_id, title="Active", body_md="b", created_round=1, elo=1210.0)
    store.record_review(UUID(best["id"]), verdict="reject", correctness="flawed")
    _mark_played(store, run_id, "h001")
    _mark_played(store, run_id, "h002")

    rows = store.snapshot(run_id)["leaderboard"]

    assert [row["hid"] for row in rows] == ["h002", "h001"]
    assert rows[0]["status"] == "active"
    assert len(rows) == 2, "every row is still returned; only the order changes"


def _mark_played(store, run_id, hid):
    """One recorded match, without going through a tournament to get it."""
    with store._session_factory() as session:  # noqa: SLF001
        session.execute(
            text(
                "update hypotheses set matches = 1 "
                "where run_id = :run_id and hid = :hid"
            ),
            {"run_id": str(run_id), "hid": hid},
        )
        session.commit()


# --- duplicate_of stays true ---------------------------------------------------------------


def test_a_duplicate_pointer_is_re_aimed_when_its_target_is_itself_archived(store):
    """The pointer was written once and never moved, so a survivor archived by a later round
    left every hypothesis that named it pointing at a non-survivor — 13 such rows in run
    d282dda7 alone. The idea graph renders that pointer to the scientist as a factual claim
    about where the idea went."""
    run_id = make_run(store)
    for title in ("One", "Two", "Three"):
        store.add_hypothesis(run_id, title=title, body_md="b", created_round=1)

    store.apply_clusters(run_id, {"h001": "a", "h002": "a"}, duplicates={"h002": "h001"})
    store.apply_clusters(run_id, {"h001": "b", "h003": "b"}, duplicates={"h001": "h003"})

    by_hid = {row["hid"]: row for row in store.list_hypotheses(run_id)}
    assert by_hid["h001"]["duplicate_of"] == "h003"
    assert by_hid["h002"]["duplicate_of"] == "h003", "h002 still named an archived survivor"
    assert by_hid["h003"]["status"] == "active"


def test_a_champion_that_is_already_archived_resolves_through_to_a_live_one(store):
    run_id = make_run(store)
    for title in ("One", "Two", "Three"):
        store.add_hypothesis(run_id, title=title, body_md="b", created_round=1)
    store.apply_clusters(run_id, {"h001": "a", "h003": "a"}, duplicates={"h001": "h003"})

    store.apply_clusters(run_id, {"h002": "a", "h001": "a"}, duplicates={"h002": "h001"})

    by_hid = {row["hid"]: row for row in store.list_hypotheses(run_id)}
    assert by_hid["h002"]["duplicate_of"] == "h003"


# --- the report's reserve is a reserve in both currencies ----------------------------------


def test_the_dollar_gate_keeps_the_overview_reserve_the_way_the_call_gate_does(store):
    """`reserve` was applied to the call ceiling and ignored by the dollar gate, which was a
    bare `spend >= budget`. So the runs that hit the money ceiling were exactly the runs that
    lost their only deliverable — c4566ed2 finished `completed` with 37 calls to spare, no
    report and no error."""
    run_id = make_run(store, config={"rounds": 3, "budget_calls": 40, "budget_usd": 5.0})
    for _ in range(9):
        store.spend(run_id, role="generation", cost_usd=0.5)

    with pytest.raises(BudgetExhausted, match="usd"):
        # 9 calls at $0.50 = $4.50; two more would breach $5.00, so a normal step is refused.
        store.check_budget(run_id, cost=1, reserve=2)

    # …and the overview, which asks with no reserve, is still affordable.
    assert store.check_budget(run_id, cost=1, reserve=0)["spend_usd"] == pytest.approx(4.5)


def test_a_run_with_no_spend_yet_is_not_refused_by_a_reserve_it_cannot_price(store):
    run_id = make_run(store, config={"rounds": 3, "budget_calls": 40, "budget_usd": 5.0})

    assert store.check_budget(run_id, cost=1, reserve=2)["calls_used"] == 0


# --- what the run lost ---------------------------------------------------------------------


def test_the_snapshot_counts_lost_work_and_not_only_model_substitutions(store):
    """`degraded_count` counts `role_degraded`, which fires only on a model/effort swap and
    has never fired once across 29 app runs. A timed-out call goes down a disjoint path, so
    the only failure aggregate the API exposed was structurally always zero."""
    run_id = make_run(store)
    store.emit(run_id, "call_finished", {"role": "generation", "model": "m", "ok": False})
    store.emit(run_id, "call_finished", {"role": "generation", "model": "m", "ok": False})
    store.emit(run_id, "call_finished", {"role": "generation", "model": "m", "ok": True})
    store.emit(
        run_id,
        "contract_violation",
        {"role": "generation", "round": 2, "error": "timeout after 420s"},
    )

    snapshot = store.snapshot(run_id)

    assert snapshot["degraded_count"] == 0
    assert snapshot["failed_calls"] == 2
    assert snapshot["lost_steps"] == 1
    assert snapshot["retried_calls"] == 1, "one failure the retry rescued"
    assert snapshot["run"]["failed_calls"] == 2
    assert snapshot["run"]["lost_steps"] == 1


def test_the_failure_record_survives_a_log_longer_than_the_recent_window(store):
    """Activity only ever sees the last 50 events for a finished run. On a 401-event run that
    is 13% of the log, so whether its failures are reachable at all is luck of position."""
    run_id = make_run(store)
    store.emit(
        run_id,
        "contract_violation",
        {"role": "evolution", "round": 1, "error": "timeout after 420s"},
    )
    for index in range(80):
        store.emit(run_id, "call_started", {"role": "ranking", "model": "m", "round": index})

    snapshot = store.snapshot(run_id)

    assert len(snapshot["recent_events"]) == 50
    assert all(event["type"] != "contract_violation" for event in snapshot["recent_events"])
    assert [event["type"] for event in snapshot["problems"]] == ["contract_violation"]


def test_a_round_that_never_finished_is_incomplete_rather_than_running_forever(store):
    """The status ladder compared `round_ >= run.round` with no reference to the lifecycle,
    so the last round of any run that died mid-round read `running` permanently — the API
    still says so about c4566ed2, terminal since 2026-08-08."""
    run_id = make_run(store)
    store.set_round(run_id, 2)
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=2)

    assert store.snapshot(run_id)["rounds"][-1]["status"] == "running"

    store.set_lifecycle(run_id, "completed")

    assert store.snapshot(run_id)["rounds"][-1]["status"] == "incomplete"


# --- what the audit found in the store ----------------------------------------------------


def test_a_round_that_generated_more_can_extend_its_match_plan(store):
    """Run c4566ed2's round 2 ran twice: the first pass lost both generation shards and
    wrote a plan for the six hypotheses that existed, the second generated h007-h012 and
    reviewed all six - and judged nothing, because `list_matches` returned a complete plan
    and the step returned 0. Extending honours everything already planned."""
    run_id = make_run(store)
    for index in range(1, 5):
        store.add_hypothesis(run_id, title=f"H{index}", body_md="b", created_round=1)
    first = store.plan_matches(run_id, 1, [("h001", "h002")])
    store.record_match(first[0]["id"], winner=1)

    plan = store.plan_matches(run_id, 1, [("h001", "h002"), ("h003", "h004")], extend=True)

    assert [(row["hid_a"], row["hid_b"]) for row in plan] == [
        ("h001", "h002"),
        ("h003", "h004"),
    ]
    assert plan[0]["status"] == "completed", "nothing already judged is re-planned"
    assert plan[1]["status"] == "planned"


def test_planning_without_extend_still_refuses_to_touch_an_existing_plan(store):
    run_id = make_run(store)
    store.add_hypothesis(run_id, title="One", body_md="b", created_round=1)
    store.add_hypothesis(run_id, title="Two", body_md="b", created_round=1)
    store.plan_matches(run_id, 1, [("h001", "h002")])

    replan = store.plan_matches(run_id, 1, [("h002", "h001"), ("h001", "h002")])

    assert len(replan) == 1


def test_a_review_stored_as_a_json_literal_is_served_as_prose(store):
    """The unwrap fix was write-side only, with no migration and no reader-side guard. Run
    c4566ed2's h006 holds the literal two-character string `{}` in four of its five fields,
    and those were served verbatim to the GUI under a human label and would be recomposed
    into every ranking prompt of a continued run - h006 is still active at 1184.8."""
    run_id = make_run(store)
    row = store.add_hypothesis(run_id, title="Six", body_md="b", created_round=1)
    store.record_review(
        UUID(row["id"]),
        verdict="pass",
        correctness="{}",
        testability='{"summary": "The test is a two-arm trial."}',
        key_risk="A plainly written risk.",
    )

    served = store.list_reviews(run_id)[0]

    assert served["correctness"] == ""
    assert served["testability"] == "The test is a two-arm trial."
    assert served["key_risk"] == "A plainly written risk."
    assert served["note"] is None, "an absent field stays absent, not empty"


def test_archiving_a_champion_re_aims_the_rows_that_pointed_at_it(store):
    """`apply_clusters` resolves champions through chains and re-aims every pointer; the
    scientist's own archive control did neither, so setting aside a champion recreated
    exactly the state that fix exists to prevent - the graph telling a reader "merged into
    hNNN" about a hypothesis that is itself set aside."""
    run_id = make_run(store)
    for index in range(1, 4):
        store.add_hypothesis(run_id, title=f"H{index}", body_md="b", created_round=1)
    store.apply_clusters(run_id, {"h001": "one", "h002": "one"}, {"h002": "h001"})

    store.archive_hypothesis(run_id, "h001")

    rows = {row["hid"]: row for row in store.list_hypotheses(run_id)}
    assert rows["h001"]["status"] == "archived"
    assert rows["h002"]["duplicate_of"] is None, "a pointer at a retired row is not kept"


def test_a_lineage_warning_is_not_counted_as_a_lost_step(store):
    """`store._run_health` counted every `contract_violation`, including the evolution
    lineage warning - which loses no step at all, since the hypothesis is still added. That
    inflated `lost_steps` and, via `retried_calls = failed - lost`, deflated the retries."""
    run_id = make_run(store)
    store.emit(run_id, "call_finished", {"role": "evolution", "ok": False})
    store.emit(run_id, "contract_violation", {"role": "evolution", "error": "timeout"})
    store.emit(run_id, "contract_violation", {"role": "evolution", "error": "…", "lost": False})

    snapshot = store.snapshot(run_id)

    assert snapshot["failed_calls"] == 1
    assert snapshot["lost_steps"] == 1
    assert snapshot["retried_calls"] == 0


def test_a_re_entered_round_reports_the_live_attempts_clock(store):
    """`func.min` made a round resumed after a continue inherit the abandoned pass's start
    time: run c4566ed2's round 2 is reported as a 33-hour round, which is the gap between
    two supervisor sessions rather than any work."""
    run_id = make_run(store)
    store.emit(run_id, "round_started", {"round": 2}, round=2)
    first = store.snapshot(run_id)["rounds"]
    store.emit(run_id, "round_started", {"round": 2}, round=2)
    store.emit(run_id, "round_completed", {"round": 2}, round=2)

    ladder = {row["round"]: row for row in store.snapshot(run_id)["rounds"]}
    original_start = {row["round"]: row for row in first}[2]["started_at"]

    assert ladder[2]["started_at"] >= original_start
    assert ladder[2]["started_at"] <= ladder[2]["completed_at"]


def test_reviews_are_counted_by_the_round_the_reflection_call_ran_in(store):
    """Attribution went through `Hypothesis.created_round`, which is the round the
    *hypothesis* was born in. Evolution's offspring are born at step 6 and reviewed by the
    next round, so run c4566ed2's ladder reported {1:6, 2:9, 3:6} for calls actually made
    {1:6, 2:6, 3:9}."""
    run_id = make_run(store)
    row = store.add_hypothesis(
        run_id, title="Evolved late in round 2", body_md="b", created_round=2
    )
    store.record_review(UUID(row["id"]), verdict="pass")
    store.emit(run_id, "review_recorded", {"hid": row["hid"], "verdict": "pass"}, round=3)

    ladder = {entry["round"]: entry for entry in store.snapshot(run_id)["rounds"]}

    assert ladder[3]["reviews"] == 1
    assert ladder.get(2, {}).get("reviews", 0) == 0


def test_an_imported_run_with_no_event_log_still_reports_its_reviews(store):
    run_id = make_run(store)
    row = store.add_hypothesis(run_id, title="Imported", body_md="b", created_round=1)
    store.record_review(UUID(row["id"]), verdict="pass")

    ladder = {entry["round"]: entry for entry in store.snapshot(run_id)["rounds"]}

    assert ladder[1]["reviews"] == 1


def test_the_snapshot_says_what_became_of_each_context_document(store):
    """`chars` on its own reads as "all of this was used". Every call is capped at 20,000
    characters and the loss was recorded in one log line inside the run's workdir."""
    run_id = make_run(store)
    store.add_context_docs(run_id, [{"name": "protocol.md", "content": "x" * 40}])

    before = store.snapshot(run_id)["context_docs"][0]
    store.set_engine_state(
        run_id, {"context_delivery": {"protocol.md": "truncated"}, "context_char_cap": 20_000}
    )
    after = store.snapshot(run_id)["context_docs"][0]

    assert before["delivered"] == "pending"
    assert after["delivered"] == "truncated"
    assert after["cap_chars"] == 20_000

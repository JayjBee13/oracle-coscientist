"""The run read API, served from the database.

Assertions here name the fixture runs rather than counting rows: the whole suite shares
one throwaway schema, so other modules' runs are legitimately present and a test that
asserts a global total would fail for a reason that has nothing to do with it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.engine_models import Run, RunEvent
from app.db.session import get_session_factory
from app.engine.runners import resolve_model_table
from app.engine.store import RunStore
from tests.support.imported import (
    GRAFT_RUN,
    GUI_RUN,
    LEGACY_RUN,
    MOJIBAKE_RUN,
    REJECTED_RUN,
    fixture_settings,
    imported_client,
)

RUN_SUMMARY_FIELDS = {
    "id", "engine_run_id", "source", "source_version", "title", "question", "harness",
    "lifecycle", "round", "rounds_target", "calls_used", "budget_calls", "spend_usd",
    "tokens_total", "counts", "top", "graft", "archived", "has_overview",
    "failed_calls", "lost_steps", "ended_reason", "overview_skipped_reason",
    "created_at", "updated_at", "owner_display_name", "model_level",
    "model_level_custom", "elapsed_seconds",
}
RUN_DETAIL_FIELDS = {
    "run", "config", "model_table", "leaderboard", "rounds", "recent_events", "budget",
    "graft_events", "context_docs", "feedback_history", "degraded_count",
    "failed_calls", "lost_steps", "retried_calls", "problems",
}
HYPOTHESIS_ROW_FIELDS = {
    "id", "hid", "title", "status", "elo", "matches", "wins", "cluster", "duplicate_of",
    "parent_ids", "operator", "created_round", "source", "novelty_level",
}
MATCH_ROW_FIELDS = {
    "id", "round", "a", "b", "status", "winner", "elo_a_before", "elo_a_after",
    "elo_b_before", "elo_b_after", "judge_model", "debate_md", "ts",
}


@pytest.fixture(scope="module")
def client():
    return imported_client()


@pytest.fixture
def extra_run() -> Iterator[RunStore]:
    """A throwaway run to poke at, so the fixtures stay exactly as imported."""
    store = RunStore(settings=fixture_settings())
    yield store


def _get(client, engine_run_id: str, suffix: str = "", **params):
    return client.get(f"/api/runs/{engine_run_id}{suffix}", params=params)


def _summary(client, engine_run_id: str) -> dict:
    response = _get(client, engine_run_id)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------------------ list


def test_list_returns_run_summaries_and_a_total(client):
    payload = client.get("/api/runs", params={"page_size": 100}).json()

    assert set(payload) == {"items", "total"}
    assert payload["total"] >= 5
    listed = {item["engine_run_id"]: item for item in payload["items"]}
    assert {LEGACY_RUN, GRAFT_RUN, REJECTED_RUN, GUI_RUN} <= set(listed)
    assert set(listed[GRAFT_RUN]) == RUN_SUMMARY_FIELDS
    assert listed[GRAFT_RUN]["source"] == "imported"
    assert listed[GRAFT_RUN]["lifecycle"] == "completed"
    assert listed[GRAFT_RUN]["has_overview"] is True
    assert listed[GUI_RUN]["has_overview"] is False


def test_list_searches_titles_and_questions(client):
    response = client.get("/api/runs", params={"q": "braid", "page_size": 50})

    assert response.status_code == 200
    found = {item["engine_run_id"] for item in response.json()["items"]}
    assert LEGACY_RUN not in found or "braid" in _summary(client, LEGACY_RUN)["question"].lower()

    hair = client.get("/api/runs", params={"q": "hair styling"}).json()
    assert all(
        "hair styling" in f"{item['title']} {item['question']}".lower()
        for item in hair["items"]
    )


def test_list_paginates(client):
    first = client.get("/api/runs", params={"page": 1, "page_size": 2}).json()
    second = client.get("/api/runs", params={"page": 2, "page_size": 2}).json()

    assert len(first["items"]) == 2
    assert first["total"] == second["total"]
    assert {item["id"] for item in first["items"]}.isdisjoint(
        {item["id"] for item in second["items"]}
    )


def test_list_sorts_by_title(client):
    items = client.get("/api/runs", params={"sort": "title", "page_size": 100}).json()["items"]

    titles = [item["title"].lower() for item in items]
    assert titles == sorted(titles)


def test_list_filters_by_lifecycle_and_harness(client):
    completed = client.get(
        "/api/runs", params={"status": "completed", "page_size": 100}
    ).json()
    codex = client.get("/api/runs", params={"harness": "codex", "page_size": 100}).json()

    assert all(item["lifecycle"] == "completed" for item in completed["items"])
    assert all(item["harness"] == "codex" for item in codex["items"])


def test_demo_runs_are_hidden_until_asked_for(client, extra_run):
    extra_run.create_run(
        question="A practice run",
        prompt="A practice run",
        harness="demo",
        source="app",
        lifecycle="completed",
        engine_run_id="run-demo-listing",
        budget_calls=5,
        budget_usd=Decimal("1.00"),
    )

    default = client.get("/api/runs", params={"page_size": 100}).json()
    included = client.get(
        "/api/runs", params={"include_demo": True, "page_size": 100}
    ).json()
    by_harness = client.get("/api/runs", params={"harness": "demo"}).json()

    assert "run-demo-listing" not in {item["engine_run_id"] for item in default["items"]}
    assert "run-demo-listing" in {item["engine_run_id"] for item in included["items"]}
    assert "run-demo-listing" in {item["engine_run_id"] for item in by_harness["items"]}


def test_archived_runs_are_hidden_until_asked_for(client, extra_run):
    run_id = extra_run.create_run(
        question="An archived run",
        prompt="An archived run",
        harness="claude",
        source="app",
        lifecycle="completed",
        engine_run_id="run-archived-listing",
        budget_calls=5,
        budget_usd=Decimal("1.00"),
    )
    with get_session_factory(fixture_settings())() as session:
        session.execute(select(Run).where(Run.id == run_id)).scalar_one().archived = True
        session.commit()

    default = client.get("/api/runs", params={"page_size": 100}).json()
    shown = client.get(
        "/api/runs", params={"show_archived": True, "page_size": 100}
    ).json()

    assert "run-archived-listing" not in {i["engine_run_id"] for i in default["items"]}
    assert "run-archived-listing" in {i["engine_run_id"] for i in shown["items"]}


# --------------------------------------------------------------------------- summary


def test_a_run_resolves_by_uuid_and_by_engine_run_id(client):
    by_engine_id = _summary(client, GRAFT_RUN)
    by_uuid = _summary(client, by_engine_id["id"])

    assert by_uuid == by_engine_id


def test_summary_counts_and_top_three(client):
    summary = _summary(client, GRAFT_RUN)

    assert summary["counts"] == {"active": 4, "rejected": 0, "archived": 0, "matches": 3}
    assert [entry["hid"] for entry in summary["top"]] == ["h001", "h004", "h002"]
    assert summary["graft"] == {"enabled": True, "fired_count": 1, "pending": False}
    assert summary["round"] == 2


def test_an_all_rejected_run_still_has_a_top_three(client):
    """Otherwise the run reads as one that produced nothing, rather than one that lost."""
    summary = _summary(client, REJECTED_RUN)

    assert summary["counts"]["active"] == 0
    assert summary["counts"]["rejected"] == 3
    assert summary["counts"]["matches"] == 0
    assert len(summary["top"]) == 3
    assert {entry["status"] for entry in summary["top"]} == {"rejected"}


def test_imported_runs_report_calls_but_no_ceiling(client):
    summary = _summary(client, GRAFT_RUN)

    assert summary["calls_used"] == 31
    assert summary["budget_calls"] == 0
    assert summary["spend_usd"] == 0.0
    assert summary["model_level"] is None
    assert summary["model_level_custom"] is False
    assert summary["elapsed_seconds"] is None


def test_app_summary_and_detail_share_level_and_event_bounded_elapsed_time(client, extra_run):
    started = datetime(2026, 9, 10, 12, 39, 35, tzinfo=UTC)
    finished = datetime(2026, 9, 10, 13, 50, 29, tzinfo=UTC)
    config = {
        "provider": "openai",
        "model_tier": "low",
        "model_overrides": {},
        "model_table": resolve_model_table("openai", "low"),
    }
    run_id = extra_run.create_run(
        question="Measure this run",
        prompt="Measure this run",
        title="Measured run metadata",
        config=config,
        lifecycle="completed",
        engine_run_id="run-metadata-listing",
        budget_calls=160,
    )
    with get_session_factory(fixture_settings())() as session:
        session.add_all(
            [
                RunEvent(
                    run_id=run_id,
                    type="lifecycle_changed",
                    payload={"lifecycle": "running"},
                    ts=started,
                ),
                RunEvent(
                    run_id=run_id,
                    type="lifecycle_changed",
                    payload={"lifecycle": "paused"},
                    ts=started + timedelta(minutes=10),
                ),
                RunEvent(
                    run_id=run_id,
                    type="run_finished",
                    payload={"lifecycle": "completed"},
                    ts=finished,
                ),
            ]
        )
        # A later presentation edit must not extend the completed run's elapsed time.
        session.get(Run, run_id).updated_at = finished + timedelta(days=2)  # type: ignore[union-attr]
        session.commit()

    summary = client.get(f"/api/runs/{run_id}").json()
    detail_run = client.get(f"/api/runs/{run_id}/detail").json()["run"]

    for payload in (summary, detail_run):
        assert payload["model_level"] == "low"
        assert payload["model_level_custom"] is False
        assert payload["elapsed_seconds"] == 4254


def test_unknown_run_answers_the_error_envelope(client):
    response = client.get("/api/runs/nope-not-a-run")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"
    assert response.json()["message"]
    assert response.json()["details"]["run_id"] == "nope-not-a-run"


# ---------------------------------------------------------------------------- detail


def test_detail_returns_the_whole_workspace_in_one_round_trip(client):
    detail = _get(client, GRAFT_RUN, "/detail").json()

    assert set(detail) == RUN_DETAIL_FIELDS | {"research"}
    assert detail["research"] is None, "historical runs retain their original workflow"
    assert set(detail["run"]) == RUN_SUMMARY_FIELDS
    assert set(detail["leaderboard"][0]) == HYPOTHESIS_ROW_FIELDS
    assert "body_md" not in detail["leaderboard"][0]
    # h004 carries a rating (1208) and has played nothing, so it used to sit at rank 2
    # above h002 and h003, both of which fought two matches. The standings order judged
    # ideas first now, and h004 falls to the bottom rather than borrowing a position from
    # a number it never earned.
    assert [row["hid"] for row in detail["leaderboard"]] == ["h001", "h002", "h003", "h004"]
    assert detail["leaderboard"][-1]["matches"] == 0
    assert detail["config"]["graft"]["enabled"] is True
    assert len(detail["graft_events"]) == 3
    assert detail["feedback_history"][0]["round"] == 2


def test_detail_reports_an_imported_runs_rounds_as_completed(client):
    """Round status normally comes from events; imported runs have none, but did finish."""
    detail = _get(client, GRAFT_RUN, "/detail").json()

    assert [round_["round"] for round_ in detail["rounds"]] == [1, 2, 3]
    assert {round_["status"] for round_ in detail["rounds"]} == {"completed"}
    assert detail["rounds"][0]["matches_completed"] == 2


def test_detail_budget_is_zero_for_an_imported_run(client):
    detail = _get(client, GRAFT_RUN, "/detail").json()

    assert detail["budget"]["calls_used"] == 31
    assert detail["budget"]["budget_calls"] == 0
    assert detail["budget"]["spend_usd"] == 0.0
    assert detail["budget"]["by_role"] == []
    assert detail["degraded_count"] == 0


# ------------------------------------------------------------------------ hypotheses


def test_hypotheses_are_ranked_and_filterable(client):
    ranked = _get(client, GRAFT_RUN, "/hypotheses").json()
    rejected = _get(client, REJECTED_RUN, "/hypotheses", status="rejected").json()
    by_hid = _get(client, GRAFT_RUN, "/hypotheses", sort="hid").json()

    assert set(ranked[0]) == HYPOTHESIS_ROW_FIELDS
    assert [row["hid"] for row in ranked] == ["h001", "h004", "h002", "h003"]
    assert [row["hid"] for row in by_hid] == ["h001", "h002", "h003", "h004"]
    assert len(rejected) == 3
    assert {row["status"] for row in rejected} == {"rejected"}


def test_hypothesis_detail_carries_body_reviews_matches_and_lineage(client):
    rows = _get(client, GRAFT_RUN, "/hypotheses").json()
    fourth = next(row for row in rows if row["hid"] == "h004")

    detail = client.get(f"/api/hypotheses/{fourth['id']}").json()

    assert detail["body_md"].startswith("# ")
    assert detail["reviews"][0]["verdict"] == "pass"
    assert detail["reviews"][0]["novelty_level"] is None
    assert [parent["hid"] for parent in detail["lineage"]["parents"]] == ["h001", "h002"]
    assert detail["lineage"]["children"] == []

    first = next(row for row in rows if row["hid"] == "h001")
    first_detail = client.get(f"/api/hypotheses/{first['id']}").json()
    assert [child["hid"] for child in first_detail["lineage"]["children"]] == ["h003", "h004"]
    assert len(first_detail["match_history"]) == 2


def test_unknown_hypothesis_answers_the_error_envelope(client):
    response = client.get("/api/hypotheses/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["code"] == "hypothesis_not_found"


# --------------------------------------------------------------------------- matches


def test_matches_name_both_sides_and_admit_the_missing_elo_curve(client):
    matches = _get(client, GRAFT_RUN, "/matches").json()
    round_two = _get(client, GRAFT_RUN, "/matches", round=2).json()

    assert set(matches[0]) == MATCH_ROW_FIELDS
    assert len(matches) == 3
    assert matches[0]["a"]["title"] == "PastoralMind"
    assert matches[0]["b"]["title"] in {"TeshuvaDraft", "ZakatLedger"}
    assert matches[0]["winner"] == 1
    assert matches[0]["elo_a_before"] is None
    assert matches[0]["elo_b_after"] is None
    assert [match["round"] for match in round_two] == [2]


# -------------------------------------------------------------------------- overview


def test_overview_is_served_as_markdown(client):
    response = _get(client, MOJIBAKE_RUN, "/overview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.strip()
    assert "�" not in response.text


def test_a_run_without_an_overview_says_so(client):
    response = _get(client, GUI_RUN, "/overview")

    assert response.status_code == 404
    assert response.json()["code"] == "overview_missing"


# ------------------------------------------------------------------------ the write path

# Launching for real is `test_supervisor.py`'s subject — it spawns actual child processes.
# What belongs here is the half of the write path a read-only fixture can answer for: that
# these endpoints validate against the same run rows every read above is served from.


def test_a_launch_request_the_wizard_could_not_have_sent_is_refused_not_stubbed(client):
    """The old shape (`version`, `final_prompt`) is gone; the endpoint is not."""
    response = client.post(
        "/api/runs",
        json={
            "title": "Anything",
            "version": "v1",
            "harness": "claude",
            "final_prompt": "Find something interesting.",
        },
    )

    assert response.status_code == 422, response.text
    assert response.status_code != 501, "launching is implemented; this is a schema refusal"


def test_controlling_a_run_that_does_not_exist_says_so(client):
    response = client.post("/api/runs/any-run/controls", json={"action": "pause"})

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "run_not_found"


def test_an_imported_run_refuses_every_control_and_names_what_is_legal(client):
    """Imported runs finished years before this app existed: there is nothing to pause."""
    response = client.post(f"/api/runs/{LEGACY_RUN}/controls", json={"action": "pause"})

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "illegal_transition"
    assert body["details"] == {"action": "pause", "lifecycle": "completed", "allowed": []}


def test_an_unknown_control_is_refused_before_it_reaches_a_run(client):
    response = client.post(f"/api/runs/{LEGACY_RUN}/controls", json={"action": "detonate"})

    assert response.status_code == 422, response.text

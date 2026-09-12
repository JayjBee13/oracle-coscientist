"""The archive import: what it reads, what it repairs, and what it refuses."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import APP_ROOT
from app.db.engine_models import GraftEvent, Hypothesis, Match, Review, Run
from app.db.session import get_session_factory
from app.main import create_app
from app.services.runs.importer import (
    ImportRefused,
    import_all,
    normalize_legacy_hypothesis,
)
from tests.support.imported import (
    FIXTURE_ARCHIVE,
    FIXTURE_RUNS,
    GRAFT_RUN,
    GUI_RUN,
    LEGACY_RUN,
    MOJIBAKE_RUN,
    REAL_ARCHIVE,
    REJECTED_RUN,
    fixture_settings,
    import_fixtures,
)

MOJIBAKE_MARKERS = ("â€", "Ã©", "â€™")


@pytest.fixture(scope="module", autouse=True)
def imported() -> None:
    import_fixtures()


def _session():
    return get_session_factory(fixture_settings())()


def _run(session, engine_run_id: str) -> Run:
    return session.execute(
        select(Run).where(Run.engine_run_id == engine_run_id)
    ).scalar_one()


def test_every_subtree_is_imported_as_a_completed_imported_run():
    with _session() as session:
        runs = {
            run.engine_run_id: run
            for run in session.execute(
                select(Run).where(Run.source == "imported")
            ).scalars()
        }

    for engine_run_id in (LEGACY_RUN, MOJIBAKE_RUN, GRAFT_RUN, REJECTED_RUN, GUI_RUN):
        run = runs[engine_run_id]
        assert run.source == "imported"
        assert run.lifecycle == "completed"
        assert run.harness in {"claude", "codex"}
        assert run.root_path.endswith(engine_run_id)

    # The gui subtree ran on the v1 engine, so it carries v1 provenance — which is what
    # keeps the Cartographer UI off runs that never had it.
    assert runs[GUI_RUN].source_version == "v1"
    assert runs[LEGACY_RUN].source_version == "v1"
    assert runs[GRAFT_RUN].source_version == "v2"


def test_imported_runs_carry_no_budget_ceiling():
    with _session() as session:
        run = _run(session, GRAFT_RUN)

    assert run.calls_used == 31
    assert run.budget_calls == 0
    assert float(run.budget_usd) == 0
    assert float(run.spend_usd) == 0


def test_mojibake_is_repaired_in_the_database_and_never_on_disk():
    with _session() as session:
        run = _run(session, MOJIBAKE_RUN)
        titles = [
            row.title
            for row in session.execute(
                select(Hypothesis).where(Hypothesis.run_id == run.id)
            ).scalars()
        ]
        guidance = [entry["guidance"] for entry in run.feedback_history]

    stored = [*titles, *guidance, run.question, run.title]
    for value in stored:
        assert "�" not in value, value
        for marker in MOJIBAKE_MARKERS:
            assert marker not in value, value
    assert any("—" in value for value in stored), "expected a repaired em dash"

    # The archive is the only copy of the historical runs: the repair happens on the way
    # into the column, so the bytes on disk must still hold the damage.
    on_disk = (FIXTURE_ARCHIVE / "v1" / MOJIBAKE_RUN / "state.json").read_text(
        encoding="utf-8"
    )
    assert "\\u00e2\\u20ac" in on_disk


def test_legacy_plaintext_bodies_are_recomposed_onto_the_schema_fields():
    with _session() as session:
        run = _run(session, LEGACY_RUN)
        rows = {
            row.hid: row
            for row in session.execute(
                select(Hypothesis).where(Hypothesis.run_id == run.id)
            ).scalars()
        }

    first = rows["h001"]
    assert first.title == "BraidPilot"
    assert first.body_md.startswith("# BraidPilot")
    assert "**Claim:**" in first.body_md
    assert "**Mechanism:**" in first.body_md
    # Sections the schema has no home for are kept rather than dropped.
    assert "**Activity:**" in first.body_md
    assert "**Userbase_Score:**" in first.body_md
    # A field the run never wrote leaves no empty heading behind.
    assert "**Test:**\n" not in first.body_md
    assert "TITLE:" not in first.body_md.splitlines()[0]


def test_modern_bodies_are_stored_as_written():
    with _session() as session:
        run = _run(session, GRAFT_RUN)
        row = session.execute(
            select(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.hid == "h001")
        ).scalar_one()

    assert row.body_md.startswith("# ")
    assert "**Claim:**" in row.body_md


def test_normalize_legacy_hypothesis_ignores_a_modern_body():
    assert normalize_legacy_hypothesis("# A title\n\n**Claim:** Something.\n") is None


def test_overview_is_imported_for_every_run_that_wrote_one():
    with _session() as session:
        with_overview = [
            run.engine_run_id
            for run in session.execute(
                select(Run).where(Run.source == "imported")
            ).scalars()
            if run.engine_state.get("overview_md")
        ]

    assert set(with_overview) >= {LEGACY_RUN, MOJIBAKE_RUN, GRAFT_RUN, REJECTED_RUN}
    assert GUI_RUN not in with_overview


def test_reviews_keep_the_verdict_and_note_history_actually_recorded():
    with _session() as session:
        run = _run(session, GRAFT_RUN)
        reviews = list(
            session.execute(
                select(Review)
                .join(Hypothesis, Hypothesis.id == Review.hypothesis_id)
                .where(Hypothesis.run_id == run.id)
            ).scalars()
        )

    assert len(reviews) == 4
    assert {review.verdict for review in reviews} == {"pass"}
    assert all(review.note for review in reviews)
    # Everything else the reflection schema now carries is absent from history, and is
    # stored NULL rather than guessed.
    assert all(review.novelty_level is None for review in reviews)
    assert all(review.correctness is None for review in reviews)


def test_matches_import_as_completed_rows_without_an_elo_curve():
    with _session() as session:
        run = _run(session, GRAFT_RUN)
        matches = list(
            session.execute(select(Match).where(Match.run_id == run.id)).scalars()
        )

    assert len(matches) == 3
    assert {match.status for match in matches} == {"completed"}
    assert {match.winner for match in matches} == {1}
    assert all(match.elo_a_before is None and match.elo_a_after is None for match in matches)
    assert all(match.k is None for match in matches)
    assert all(match.debate_md for match in matches)


def test_lineage_survives_the_single_free_text_parent_field():
    with _session() as session:
        run = _run(session, GRAFT_RUN)
        rows = {
            row.hid: row
            for row in session.execute(
                select(Hypothesis).where(Hypothesis.run_id == run.id)
            ).scalars()
        }

    assert rows["h003"].parent_ids == ["h001"]
    assert rows["h004"].parent_ids == ["h001", "h002"]
    assert rows["h001"].parent_ids == []


def test_graft_state_and_collapse_history_are_imported_for_v2_runs():
    with _session() as session:
        run = _run(session, GRAFT_RUN)
        events = list(
            session.execute(
                select(GraftEvent).where(GraftEvent.run_id == run.id).order_by(GraftEvent.round)
            ).scalars()
        )
        v1_events = session.execute(
            select(GraftEvent).where(GraftEvent.run_id == _run(session, LEGACY_RUN).id)
        ).scalars()

        assert run.graft_state["fired_count"] == 1
        assert run.graft_state["last_fired_round"] == 3
        assert len(events) == 3
        assert [event.fired for event in events] == [False, False, True]
        assert events[0].hhi == 0.5
        assert events[0].signals["clusters"] == ["c-clergy-notes", "c-responsa"]
        assert list(v1_events) == []


def test_reimport_changes_nothing_and_keeps_hypothesis_ids_stable():
    with _session() as session:
        run = _run(session, GRAFT_RUN)
        before = {
            row.hid: row.id
            for row in session.execute(
                select(Hypothesis).where(Hypothesis.run_id == run.id)
            ).scalars()
        }
        run_id = run.id

    second = import_fixtures()

    with _session() as session:
        run = _run(session, GRAFT_RUN)
        after = {
            row.hid: row.id
            for row in session.execute(
                select(Hypothesis).where(Hypothesis.run_id == run.id)
            ).scalars()
        }
        matches = len(
            list(session.execute(select(Match).where(Match.run_id == run.id)).scalars())
        )
        events = len(
            list(
                session.execute(
                    select(GraftEvent).where(GraftEvent.run_id == run.id)
                ).scalars()
            )
        )
        assert run.id == run_id, "a re-import must update the run, not replace it"

    assert after == before, "hypothesis ids appear in URLs and must survive a re-import"
    assert matches == 3
    assert events == 3
    assert second.imported == 5
    assert second.created == 0
    assert second.updated == 5


def test_import_refuses_to_read_the_supervisor_workdir_root():
    with pytest.raises(ImportRefused):
        import_all(fixture_settings(archive=APP_ROOT / "engines" / "runs"))


def test_import_report_counts_what_it_did():
    report = import_fixtures()

    assert report.imported == 5
    assert report.hypotheses == 13
    assert report.matches == 6
    assert report.reviews == 12
    assert report.overviews == 4
    assert report.graft_events == 3
    # The legacy and all-rejected fixtures both came from the plaintext era.
    assert report.legacy_bodies == 6
    assert report.warnings == []


@pytest.mark.skipif(
    not (REAL_ARCHIVE / "v1").is_dir(), reason="the run archive is not present"
)
def test_the_real_archive_imports_completely():
    """The gate Task 6.3's deletion is held against: everything is in the database."""
    report = import_all(fixture_settings(archive=REAL_ARCHIVE))

    assert report.imported == 15, "12 historical runs plus the 3 the GUI created"
    assert report.hypotheses == 298
    assert report.overviews == 12
    assert report.legacy_bodies == 74
    assert report.warnings == []

    with get_session_factory(fixture_settings(archive=REAL_ARCHIVE))() as session:
        runs = list(
            session.execute(
                select(Run).where(Run.source == "imported", Run.engine_run_id.like("run-%"))
            ).scalars()
        )
        damaged = []
        for run in runs:
            overview = run.engine_state.get("overview_md") or ""
            assert overview, f"{run.engine_run_id} lost its overview"
            for value in (run.title, run.question, overview):
                if "�" in value or any(marker in value for marker in MOJIBAKE_MARKERS):
                    damaged.append(run.engine_run_id)
    assert damaged == []


@pytest.mark.skipif(
    not (REAL_ARCHIVE / "v1").is_dir(), reason="the run archive is not present"
)
def test_every_archived_hypothesis_body_lands_undamaged():
    import_all(fixture_settings(archive=REAL_ARCHIVE))

    with get_session_factory(fixture_settings(archive=REAL_ARCHIVE))() as session:
        rows = list(
            session.execute(
                select(Hypothesis.title, Hypothesis.body_md)
                .join(Run, Run.id == Hypothesis.run_id)
                .where(Run.source == "imported")
            )
        )

    assert len(rows) >= 298
    for title, body in rows:
        assert "�" not in title and "�" not in body
        for marker in MOJIBAKE_MARKERS:
            assert marker not in title, title
        assert not title.upper().startswith("TITLE:")


def test_the_app_imports_the_archive_when_it_boots():
    """A fresh install must open on the owner's runs, not on an empty list."""
    settings = fixture_settings(IMPORT_ON_STARTUP=True)

    with TestClient(create_app(settings)) as client:
        listed = client.get("/api/runs", params={"page_size": 100}).json()

    assert {item["engine_run_id"] for item in listed["items"]} >= set(FIXTURE_RUNS)


def test_reimport_endpoint_reports_what_it_did():
    with TestClient(create_app(fixture_settings())) as client:
        payload = client.post("/api/admin/reimport").json()

    assert payload["imported"] == 5
    assert payload["created"] == 0
    assert payload["hypotheses"] == 13


def test_root_path_points_into_the_archive_and_serves_the_files():
    with _session() as session:
        run = _run(session, LEGACY_RUN)

    root = json.loads(
        (FIXTURE_ARCHIVE / "v1" / LEGACY_RUN / "state.json").read_text(encoding="utf-8")
    )
    assert root["run_id"] == LEGACY_RUN
    assert run.root_path == f"archive/v1/{LEGACY_RUN}"
    assert run.engine_state["imported_from"] == run.root_path

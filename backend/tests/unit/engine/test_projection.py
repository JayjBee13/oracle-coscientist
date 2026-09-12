"""The projection has one job: produce files the legacy artifact reader still understands.

So the reader itself is the oracle here. Every structural test parses the projected run with
the real `services/artifacts/normalizer.py` and demands zero warnings — a projection that
drops a `file` path, writes a Windows separator, or forgets a hypothesis markdown file shows
up as a warning string rather than as a mystery in the UI three waves later.

The value assertions read the database back through `RunStore` rather than restating
constants, because the point is that the file agrees with the run, not that it agrees with
whatever the test author typed.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.engine.projection import (
    IDEAS_CSV_COLUMNS,
    IDEAS_CSV_FILENAME,
    OVERVIEW_FILENAME,
    SCHEMA_VERSION,
    STATE_FILENAME,
    make_projector,
    project_run,
)
from app.engine.store import RunStore
from app.services.artifacts.discovery import DiscoveredRun
from app.services.artifacts.normalizer import normalize_run

ENGINE_TABLES = (
    "run_events",
    "budget_ledger",
    "graft_events",
    "matches",
    "reviews",
    "hypotheses",
    "run_context_docs",
    "runs",
)

ENGINE_RUN_ID = "run-projection-01"

# Accented text, an em dash and emoji in every layer that gets written: the archived engines
# produced mojibake in five of twelve runs precisely because file writes had no encoding.
GOAL = "Pourquoi les tardigrades survivent-ils au vide — une question naïve 🐻"
OVERVIEW_MD = "# Aperçu\n\nRésumé des résultats 🧪\n"
BODY_H1 = "# Trehalose glass\n\n**Claim:** Vitrification préserve les protéines ✨\n"
QUOTED_TITLE = 'Trehalose "glass", vitrified ✨'


@pytest.fixture(scope="session")
def session_factory(isolated_schema):
    return get_session_factory(get_settings())


@pytest.fixture
def store(session_factory):
    with session_factory() as session:
        schema = session.execute(text("select current_schema()")).scalar_one()
        assert schema.startswith("test_"), f"refusing to truncate live schema {schema!r}"
        session.execute(text(f"TRUNCATE {', '.join(ENGINE_TABLES)} RESTART IDENTITY CASCADE"))
        session.commit()
    return RunStore(session_factory)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """The launcher's layout: `<engine root>/runs/<engine_run_id>`.

    Resolved, because Windows hands out both the 8.3 and the long form of the temp
    directory and the projection resolves what it is given.
    """
    return (tmp_path / "engines" / "runs" / ENGINE_RUN_ID).resolve()


def build_run(store: RunStore, *, graft: bool = True):
    """A small but complete run: every hypothesis status, a settled match, a fired graft."""
    run_id = store.create_run(
        question=GOAL,
        prompt="Investigate cryptobiosis mechanisms.",
        config={
            "rounds": 2,
            "generation_batch": 3,
            "matches_per_round": 4,
            "evolve_top_k": 2,
            "budget_calls": 40,
            "budget_usd": 5.0,
            "grounding_depth": "standard",
            "graft": {"enabled": graft, "quorum_k": 2, "window": 3, "cooldown": 2},
        },
        harness="demo",
        engine_run_id=ENGINE_RUN_ID,
        root_path=f"engines/runs/{ENGINE_RUN_ID}",
    )

    store.add_hypothesis(run_id, title=QUOTED_TITLE, body_md=BODY_H1, created_round=1)
    store.add_hypothesis(
        run_id, title="Dsup shields DNA", body_md="# Dsup shields DNA\n", created_round=1
    )
    store.add_hypothesis(
        run_id,
        title="Vitrified Dsup scaffold",
        body_md="# Vitrified Dsup scaffold\n",
        created_round=2,
        parent_ids=["h001", "h002"],
        operator="combination",
    )
    store.add_hypothesis(
        run_id, title="Radiation memory", body_md="# Radiation memory\n", created_round=1
    )
    store.add_hypothesis(
        run_id, title="Dsup shields DNA (again)", body_md="# Dsup again\n", created_round=2
    )

    by_hid = {row["hid"]: row for row in store.list_hypotheses(run_id)}
    store.record_review(by_hid["h001"]["id"], verdict="pass", note="Testable ✓", model="sonnet-5")
    store.record_review(by_hid["h002"]["id"], verdict="pass", note="Buyer exists")
    store.record_review(by_hid["h004"]["id"], verdict="reject", note="Fundamental flaw")

    store.apply_clusters(
        run_id,
        {"h001": "c-vitrification", "h002": "c-protein", "h003": "c-protein", "h005": "c-protein"},
        duplicates={"h005": "h002"},
    )

    planned = store.plan_matches(run_id, 1, [("h001", "h002")])
    store.record_match(
        planned[0]["id"],
        winner=1,
        debate_md="# Debate\n\nh001 carries the clearer mechanism.\nSecond line ignored.",
        judge_model="sonnet-5",
    )
    store.plan_matches(run_id, 2, [("h001", "h003")])  # planned, never settled

    store.record_graft(
        run_id,
        1,
        fired=True,
        n_clusters=2,
        hhi=0.5,
        votes=2,
        source_domain="metallurgy",
        skeleton="anneal → quench → temper",
        seed_framing="Traiter le tardigrade comme un alliage 🔬",
        seed_id="seed-1",
    )
    store.record_feedback(run_id, 1, "Push for datable why-now evidence.")
    store.set_engine_state(run_id, {"overview_md": OVERVIEW_MD})
    store.set_round(run_id, 2)
    for role in ("generation", "reflection", "ranking"):
        store.spend(run_id, role=role, model="claude-sonnet-5", tokens_in=100, tokens_out=50)
    return run_id


def discovered(workdir: Path, version: str = "v2") -> DiscoveredRun:
    return DiscoveredRun(
        version=version,
        engine_root=workdir.parents[1],
        run_dir=workdir,
        engine_run_id=workdir.name,
        state_path=workdir / STATE_FILENAME,
    )


def read_state(workdir: Path) -> dict:
    return json.loads((workdir / STATE_FILENAME).read_text(encoding="utf-8"))


# --- the legacy reader is the oracle -------------------------------------------------------


def test_normalizer_reads_a_projected_run_without_a_single_warning(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)

    normalized = normalize_run(discovered(workdir))

    assert normalized.warnings == []
    assert normalized.engine_run_id == ENGINE_RUN_ID
    assert normalized.goal == GOAL
    assert normalized.lifecycle == "completed"  # research_overview.md is on disk


def test_projected_values_match_the_database(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)
    run = store.get_run(run_id)
    rows = {row["hid"]: row for row in store.list_hypotheses(run_id)}

    normalized = normalize_run(discovered(workdir))

    assert normalized.progress.calls_used == run["calls_used"] == 3
    assert normalized.progress.iteration == run["round"] == 2
    assert normalized.settings.budget == run["budget_calls"] == 40
    assert normalized.settings.matches_per_round == 4
    assert normalized.settings.top_k == 2
    assert (normalized.counts.active, normalized.counts.rejected) == (3, 1)
    assert (normalized.counts.archived, normalized.counts.total) == (1, 5)
    assert normalized.counts.matches == 1  # only the settled match projects
    assert normalized.counts.clusters == 2

    best = normalized.top[0]
    assert best.id == "h001"
    assert best.title == QUOTED_TITLE == rows["h001"]["title"]
    assert best.elo == pytest.approx(rows["h001"]["elo"])
    assert best.elo > normalized.top[-1].elo  # winning the match moved it
    assert best.file == f"runs/{ENGINE_RUN_ID}/hypotheses/h001.md"
    assert best.matches == rows["h001"]["matches"] == 1
    assert best.wins == rows["h001"]["wins"] == 1
    assert best.status == "active"
    assert best.verdict == "pass"
    assert best.cluster == "c-vitrification"
    assert best.created_iter == 1


def test_graft_state_reaches_the_reader_when_graft_is_enabled(store, workdir):
    run_id = build_run(store, graft=True)
    project_run(store, run_id, workdir)

    normalized = normalize_run(discovered(workdir))

    assert normalized.graft.applicable is True
    assert normalized.graft.enabled is True
    assert normalized.graft.fired_count == 1
    assert normalized.graft.pending_injection is True


def test_collapse_keys_are_absent_when_graft_is_disabled(store, workdir):
    run_id = build_run(store, graft=False)
    project_run(store, run_id, workdir)

    state = read_state(workdir)

    assert "collapse" not in state
    assert "pending_injection" not in state
    assert normalize_run(discovered(workdir)).warnings == []


# --- the exact document -------------------------------------------------------------------


def test_state_json_carries_every_field_the_reader_looks_for(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)

    state = read_state(workdir)

    assert state["schema_version"] == SCHEMA_VERSION
    assert {"run_id", "goal", "config", "calls_used", "iteration", "hypotheses", "matches"} <= set(
        state
    )
    assert set(state["config"]) >= {"max_llm_calls", "matches_per_round", "evolve_top_k"}
    assert state["next_id"] == 6

    hypothesis = state["hypotheses"]["h001"]
    assert set(hypothesis) >= {
        "id",
        "title",
        "file",
        "elo",
        "matches",
        "wins",
        "status",
        "review",
        "cluster",
        "created_iter",
        "parent",
    }
    assert hypothesis["review"]["verdict"] == "pass"
    assert state["hypotheses"]["h005"]["duplicate_of"] == "h002"
    assert state["feedback"] == [{"iter": 1, "text": "Push for datable why-now evidence."}]

    (match,) = state["matches"]
    assert match == {
        "a": "h001",
        "b": "h002",
        "winner": "a",
        "iter": 1,
        # The one-line legacy note is the debate's opening claim, not its `# Debate` heading.
        "note": "h001 carries the clearer mechanism.",
    }


def test_parent_ids_collapse_into_the_legacy_comma_joined_parent(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)

    state = read_state(workdir)

    assert state["hypotheses"]["h003"]["parent"] == "h001,h002"
    assert state["hypotheses"]["h001"]["parent"] is None
    # The lossless form stays in the database, which is what every reader should use.
    evolved = next(r for r in store.list_hypotheses(run_id) if r["hid"] == "h003")
    assert evolved["parent_ids"] == ["h001", "h002"]


def test_every_path_in_state_json_is_posix(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)

    state = read_state(workdir)

    assert all("\\" not in row["file"] for row in state["hypotheses"].values())
    # A literal backslash — the Windows separator the archived runs stored — serialises as
    # a doubled one. The single backslashes escaping the quotes in a title are not that.
    assert "\\\\" not in (workdir / STATE_FILENAME).read_text(encoding="utf-8")


def test_every_hypothesis_gets_a_markdown_file(store, workdir):
    run_id = build_run(store)
    result = project_run(store, run_id, workdir)

    assert result.hypotheses == 5
    files = sorted(path.name for path in (workdir / "hypotheses").iterdir())
    assert files == ["h001.md", "h002.md", "h003.md", "h004.md", "h005.md"]


def test_overview_is_written_only_once_it_exists(store, workdir):
    run_id = store.create_run(
        question="A run with no report yet",
        prompt="…",
        config={"rounds": 1, "budget_calls": 8, "budget_usd": 1.0, "matches_per_round": 1},
        harness="demo",
        engine_run_id="run-no-overview",
    )
    store.add_hypothesis(run_id, title="Only idea", body_md="# Only idea\n", created_round=1)

    project_run(store, run_id, workdir)
    assert not (workdir / OVERVIEW_FILENAME).exists()
    assert normalize_run(discovered(workdir)).warnings == []

    store.set_engine_state(run_id, {"overview_md": OVERVIEW_MD})
    project_run(store, run_id, workdir)
    assert (workdir / OVERVIEW_FILENAME).read_text(encoding="utf-8") == OVERVIEW_MD


# --- ideas_ranked.csv ----------------------------------------------------------------------


def test_ideas_csv_ranks_active_hypotheses_and_quotes_properly(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)
    ranked_by_elo = [
        row["hid"]
        for row in sorted(
            (r for r in store.list_hypotheses(run_id) if r["status"] == "active"),
            key=lambda r: -r["elo"],
        )
    ]

    raw = (workdir / IDEAS_CSV_FILENAME).read_bytes()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))

    assert not raw.startswith(b"\xef\xbb\xbf"), "the CSV must be BOM-free UTF-8"
    assert rows[0] == list(IDEAS_CSV_COLUMNS)
    assert [row[0] for row in rows[1:]] == ranked_by_elo
    # Rejected and archived hypotheses never finished the tournament, so they are not ranked.
    assert {row[6] for row in rows[1:]} == {"active"}
    # The quotes and comma in the title survive a round trip through the csv module.
    assert rows[1][1] == QUOTED_TITLE


# --- determinism, encoding, atomicity -------------------------------------------------------


def test_reprojecting_an_unchanged_run_produces_identical_bytes(store, workdir):
    run_id = build_run(store)
    result = project_run(store, run_id, workdir)
    before = {name: (workdir / name).read_bytes() for name in result.files}

    project_run(store, run_id, workdir, reason="round_completed")

    after = {name: (workdir / name).read_bytes() for name in result.files}
    assert after == before


def test_unicode_round_trips_through_every_projected_file(store, workdir):
    run_id = build_run(store)
    project_run(store, run_id, workdir)

    state = read_state(workdir)
    assert state["goal"] == GOAL
    assert state["hypotheses"]["h001"]["title"] == QUOTED_TITLE
    assert state["pending_injection"] == "Traiter le tardigrade comme un alliage 🔬"
    assert (workdir / "hypotheses" / "h001.md").read_text(encoding="utf-8") == BODY_H1
    assert (workdir / OVERVIEW_FILENAME).read_text(encoding="utf-8") == OVERVIEW_MD
    csv_rows = list(csv.reader(io.StringIO((workdir / IDEAS_CSV_FILENAME).read_text("utf-8"))))
    assert csv_rows[1][1] == QUOTED_TITLE
    # No lossy escaping anywhere: the emoji is in the bytes, not as 🐻.
    assert "🐻".encode() in (workdir / STATE_FILENAME).read_bytes()


def test_a_crash_before_the_rename_leaves_the_previous_state_json(store, workdir, monkeypatch):
    run_id = build_run(store)
    project_run(store, run_id, workdir)
    previous = (workdir / STATE_FILENAME).read_bytes()
    store.add_hypothesis(run_id, title="Late idea", body_md="# Late idea\n", created_round=2)

    real_replace = os.replace

    def crash_on_state(src, dst):
        if Path(dst).name == STATE_FILENAME:
            raise OSError("simulated crash between write and rename")
        return real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", crash_on_state)
        with pytest.raises(OSError, match="simulated crash"):
            project_run(store, run_id, workdir)

    assert (workdir / STATE_FILENAME).read_bytes() == previous
    assert list(workdir.glob("*.tmp")) == []
    assert normalize_run(discovered(workdir)).warnings == []


def test_a_crash_mid_write_leaves_no_partial_file(store, workdir, monkeypatch):
    run_id = build_run(store)
    project_run(store, run_id, workdir)
    previous = {
        name: (workdir / name).read_bytes()
        for name in (STATE_FILENAME, IDEAS_CSV_FILENAME, OVERVIEW_FILENAME)
    }
    store.add_hypothesis(run_id, title="Late idea", body_md="# Late idea\n", created_round=2)

    def crash(_fd):
        raise OSError("simulated crash while flushing")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", crash)
        with pytest.raises(OSError, match="simulated crash"):
            project_run(store, run_id, workdir)

    for name, content in previous.items():
        assert (workdir / name).read_bytes() == content
    assert list(workdir.rglob("*.tmp")) == []
    assert normalize_run(discovered(workdir)).warnings == []


# --- orchestrator hook ----------------------------------------------------------------------


def test_make_projector_matches_the_orchestrator_hook(store, workdir):
    run_id = build_run(store)

    project = make_projector(store, run_id, workdir)
    project("round_completed")

    assert (workdir / STATE_FILENAME).exists()
    assert normalize_run(discovered(workdir)).warnings == []


def test_projecting_outside_the_engine_root_is_refused(store, workdir, tmp_path):
    run_id = build_run(store)

    with pytest.raises(ValueError, match="not inside engine root"):
        project_run(store, run_id, workdir, engine_root=tmp_path / "elsewhere")


def test_projection_result_lists_what_it_wrote(store, workdir):
    run_id = build_run(store)

    result = project_run(store, run_id, workdir)

    assert result.state_path == workdir / STATE_FILENAME
    assert result.matches == 1
    assert set(result.files) >= {
        STATE_FILENAME,
        OVERVIEW_FILENAME,
        IDEAS_CSV_FILENAME,
        "hypotheses/h001.md",
    }

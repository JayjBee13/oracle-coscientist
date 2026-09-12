import json

from app.services.artifacts.discovery import DiscoveredRun, discover_run_folders
from app.services.artifacts.normalizer import normalize_run, normalize_runs
from tests.fixtures.artifact_roots import materialize_run


def test_discovery_finds_v1_and_v2_runs_and_skips_folders_without_state(tmp_path):
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    materialize_run(v1, "v1")
    materialize_run(v2, "v2")
    (v1 / "runs" / "not-a-run").mkdir(parents=True)
    (v1 / "runs" / "stray.txt").write_text("ignored", encoding="utf-8")

    discovered = discover_run_folders(v1, v2)

    assert [(run.version, run.engine_run_id) for run in discovered] == [
        ("v1", "run-fixture-v1"),
        ("v2", "run-fixture-v2"),
    ]
    assert all(run.state_path.name == "state.json" for run in discovered)


def test_normalizes_v2_run_with_ranked_csv_and_graft_fields(tmp_path):
    run = materialize_run(tmp_path, "v2")

    dto = normalize_run(run)

    assert dto.id == "imported-v2-run-fixture-v2"
    assert dto.version == "v2"
    assert dto.lifecycle == "completed"
    assert dto.progress.calls_used == 129
    assert dto.progress.budget == 150
    assert dto.progress.percent_budget == 86
    assert dto.counts.total == 6
    assert dto.counts.active == 4
    assert dto.counts.rejected == 1
    assert dto.counts.archived == 1
    assert dto.counts.matches == 3
    assert dto.counts.clusters == 2
    # h001 and h006 are tied on Elo; the id breaks the tie.
    assert [item.id for item in dto.top] == ["h001", "h006", "h002", "h003"]
    assert dto.graft.applicable is True
    assert dto.graft.enabled is True
    assert dto.graft.fired_count == 2
    assert dto.graft.pending_injection is False
    assert any(artifact.kind == "ideas_ranked_csv" for artifact in dto.artifacts)
    assert dto.paths.state_json == "runs/run-fixture-v2/state.json"
    assert dto.warnings == []


def test_normalizes_v1_run_without_graft_or_ranked_csv(tmp_path):
    run = materialize_run(tmp_path, "v1")

    dto = normalize_run(run)

    assert dto.id == "imported-v1-run-fixture-v1"
    assert dto.version == "v1"
    assert dto.progress.calls_used == 45
    assert dto.progress.budget == 120
    assert dto.counts.total == 4
    assert dto.counts.active == 3
    assert dto.counts.matches == 2
    assert dto.top[0].id == "h002"
    assert dto.graft.applicable is False
    assert dto.graft.enabled is False
    assert not any(artifact.kind == "ideas_ranked_csv" for artifact in dto.artifacts)
    assert dto.warnings == []


def test_normalized_dtos_are_json_serializable(tmp_path):
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    materialize_run(v1, "v1")
    materialize_run(v2, "v2")
    discovered = discover_run_folders(v1, v2)

    payload = [dto.model_dump(mode="json") for dto in normalize_runs(discovered)]

    assert [item["id"] for item in payload] == [
        "imported-v1-run-fixture-v1",
        "imported-v2-run-fixture-v2",
    ]
    assert json.dumps(payload)


def test_normalizer_flags_hypothesis_file_that_is_missing(tmp_path):
    state = {
        "run_id": "run-missing-file",
        "goal": "Missing hypothesis file",
        "config": {},
        "hypotheses": {
            "h001": {
                "id": "h001",
                "title": "Gone",
                "file": "runs/run-missing-file/hypotheses/h001.md",
                "status": "active",
            }
        },
        "matches": [],
    }
    run_dir = tmp_path / "runs" / "run-missing-file"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    dto = normalize_run(
        DiscoveredRun(
            version="v1",
            engine_root=tmp_path,
            run_dir=run_dir,
            state_path=run_dir / "state.json",
            engine_run_id="run-missing-file",
        )
    )

    assert "hypothesis_file_missing:h001:runs/run-missing-file/hypotheses/h001.md" in dto.warnings


def test_normalizer_skips_hypothesis_paths_outside_root(tmp_path):
    root = tmp_path / "engine"
    run_dir = root / "runs" / "outside-path"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "outside-path",
                "goal": "Outside path fixture",
                "config": {},
                "hypotheses": {
                    "h001": {
                        "id": "h001",
                        "title": "Outside",
                        "file": str(outside),
                        "status": "active",
                    }
                },
                "matches": [],
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_run_folders(root, tmp_path / "empty-v2")
    dto = normalize_run(discovered[0])

    assert "hypothesis_path_outside_root:h001" in dto.warnings
    assert not any(artifact.path == str(outside) for artifact in dto.artifacts)


def test_normalizer_repairs_common_windows_mojibake_in_titles(tmp_path):
    run_dir = tmp_path / "runs" / "run-mojibake"
    hypothesis_dir = run_dir / "hypotheses"
    hypothesis_dir.mkdir(parents=True)
    (hypothesis_dir / "h001.md").write_text("# EchoBuddy\n", encoding="utf-8")
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "run_id": "run-mojibake",
                "goal": "Find apps for kids â€” safely.",
                "hypotheses": {
                    "h001": {
                        "id": "h001",
                        "title": "EchoBuddy â€” call-and-response",
                        "file": "runs/run-mojibake/hypotheses/h001.md",
                        "status": "active",
                        "elo": 1200,
                    }
                },
                "matches": [],
            }
        ),
        encoding="utf-8",
    )

    dto = normalize_run(
        DiscoveredRun(
            version="v2",
            engine_root=tmp_path,
            run_dir=run_dir,
            state_path=run_dir / "state.json",
            engine_run_id="run-mojibake",
        )
    )

    assert "—" in dto.goal
    assert "—" in dto.top[0].title
    assert "â" not in dto.top[0].title

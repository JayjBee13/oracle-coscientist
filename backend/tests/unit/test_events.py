import json
import os
import time

from app.services.artifacts.discovery import discover_run_folders
from app.services.artifacts.normalizer import normalize_run
from app.services.events.artifacts import detect_artifact_changes, snapshot_artifacts


def test_touch_fixture_emits_artifact_changed(tmp_path):
    root = tmp_path / "engine"
    run_dir = root / "runs" / "run-test"
    hypotheses_dir = run_dir / "hypotheses"
    hypotheses_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "run_id": "run-test",
                "goal": "Test run",
                "config": {"max_llm_calls": 10},
                "calls_used": 1,
                "iteration": 1,
                "hypotheses": {},
                "matches": [],
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_run_folders(root, tmp_path / "missing-v2")
    before = snapshot_artifacts(normalize_run(discovered[0]))

    time.sleep(0.01)
    os.utime(state_path, None)

    discovered_after = discover_run_folders(root, tmp_path / "missing-v2")
    after = snapshot_artifacts(normalize_run(discovered_after[0]))
    events = detect_artifact_changes(before, after)

    assert len(events) == 1
    assert events[0].event_type == "artifact_changed"
    assert events[0].payload["path"] == "runs/run-test/state.json"
    assert events[0].payload["change"] == "modified"

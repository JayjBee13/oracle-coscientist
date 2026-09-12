"""Checked-in engine-run fixtures.

The parser tests used to assert against the owner's real runs under `ai-coscientist/` and
`ai-coscientist_v2/` — data that is not in git, is being archived and deleted, and whose
numbers changed whenever a run was re-run. `state_v1.json` / `state_v2.json` are trimmed
copies of that shape (backslash `file` paths and all) that we own and can reason about.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.schemas.run import EngineVersion
from app.services.artifacts.discovery import DiscoveredRun

FIXTURES_DIR = Path(__file__).resolve().parent


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[4]


def v1_root() -> Path:
    """The owner's live v1 engine root. Only for tests still pending migration."""
    return workspace_root() / "ai-coscientist"


def v2_root() -> Path:
    """The owner's live v2 engine root. Only for tests still pending migration."""
    return workspace_root() / "ai-coscientist_v2"


def load_state(version: EngineVersion) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / f"state_{version}.json").read_text(encoding="utf-8"))


def materialize_run(
    root: Path,
    version: EngineVersion,
    *,
    state: dict[str, Any] | None = None,
    overview: bool = True,
    ideas_csv: bool | None = None,
) -> DiscoveredRun:
    """Write a fixture run under `root` as a real engine root and return it discovered.

    `ideas_csv` defaults to True for v2 and False for v1, matching what the two engine
    generations actually wrote.
    """
    state = state if state is not None else load_state(version)
    run_id = state["run_id"]
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    for hypothesis_id, hypothesis in state.get("hypotheses", {}).items():
        relative = str(hypothesis.get("file") or "").replace("\\", "/")
        if not relative:
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {hypothesis.get('title', hypothesis_id)}\n\nFixture body.\n", encoding="utf-8"
        )

    (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    if overview:
        (run_dir / "research_overview.md").write_text(
            "# Research overview\n\nFixture overview.\n", encoding="utf-8"
        )

    if ideas_csv is None:
        ideas_csv = version == "v2"
    if ideas_csv:
        (run_dir / "ideas_ranked.csv").write_text(
            "id,title,elo\nh001,Fixture,1300\n", encoding="utf-8"
        )

    return DiscoveredRun(
        version=version,
        engine_root=root,
        run_dir=run_dir,
        state_path=run_dir / "state.json",
        engine_run_id=run_id,
    )

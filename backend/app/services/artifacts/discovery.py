from pathlib import Path
from typing import NamedTuple

from app.schemas.run import EngineVersion


class DiscoveredRun(NamedTuple):
    version: EngineVersion
    engine_root: Path
    run_dir: Path
    engine_run_id: str
    state_path: Path


def discover_run_folders(v1_root: Path, v2_root: Path) -> list[DiscoveredRun]:
    discovered: list[DiscoveredRun] = []
    for version, root in (("v1", v1_root), ("v2", v2_root)):
        discovered.extend(discover_version_run_folders(root, version))

    return sorted(discovered, key=lambda run: (run.version, run.engine_run_id))


def discover_version_run_folders(root: Path, version: EngineVersion) -> list[DiscoveredRun]:
    runs_dir = root / "runs"
    if not runs_dir.exists() or not runs_dir.is_dir():
        return []

    discovered: list[DiscoveredRun] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue

        state_path = run_dir / "state.json"
        if not state_path.exists():
            continue

        discovered.append(
            DiscoveredRun(
                version=version,
                engine_root=root,
                run_dir=run_dir,
                engine_run_id=run_dir.name,
                state_path=state_path,
            )
        )

    return discovered

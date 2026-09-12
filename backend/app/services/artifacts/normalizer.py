import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas.run import (
    ArtifactKind,
    ArtifactSummary,
    GraftDTO,
    HypothesisSummary,
    NormalizedRunDTO,
    ProcessSummary,
    RunCountsDTO,
    RunPaths,
    RunProgressDTO,
    RunSettingsDTO,
)
from app.services.artifacts.discovery import DiscoveredRun
from app.services.runs.paths import is_within


def normalize_runs(discovered_runs: list[DiscoveredRun]) -> list[NormalizedRunDTO]:
    return [normalize_run(run) for run in discovered_runs]


def normalize_run(discovered: DiscoveredRun) -> NormalizedRunDTO:
    warnings: list[str] = []
    state = _read_json(discovered.state_path, warnings)
    engine_run_id = str(state.get("run_id") or discovered.engine_run_id)
    goal = repair_mojibake(str(state.get("goal") or ""))
    config = _as_dict(state.get("config"))
    hypotheses = _as_dict(state.get("hypotheses"))
    matches = _as_list(state.get("matches"))
    overview_path = discovered.run_dir / "research_overview.md"

    summaries = [_hypothesis_summary(hypothesis) for hypothesis in hypotheses.values()]
    summaries = [summary for summary in summaries if summary is not None]
    summaries.sort(key=lambda item: (-(item.elo or 0), item.id))

    active = [item for item in summaries if item.status == "active"]
    rejected = [item for item in summaries if item.status == "rejected"]
    archived = [item for item in summaries if item.status == "archived"]
    active_clusters = {item.cluster for item in active if item.cluster}

    budget = _as_int(config.get("max_llm_calls"))
    calls_used = _as_int(state.get("calls_used"))
    percent_budget = _percent_budget(calls_used, budget)
    artifacts = _artifact_summaries(discovered, hypotheses, warnings)
    lifecycle = "completed" if overview_path.exists() else "imported"

    return NormalizedRunDTO(
        id=f"imported-{discovered.version}-{engine_run_id}",
        engine_run_id=engine_run_id,
        harness="claude",
        version=discovered.version,
        lifecycle=lifecycle,
        title=_title_from_goal(goal, engine_run_id),
        goal=goal,
        paths=RunPaths(
            root=str(discovered.engine_root),
            run_dir=_relative_path(discovered.run_dir, discovered.engine_root),
            state_json=_relative_path(discovered.state_path, discovered.engine_root),
        ),
        settings=RunSettingsDTO(
            grounding_mode="built_in_web",
            grounding_depth="standard",
            rounds_target=_as_int(state.get("iteration")),
            budget=budget,
            matches_per_round=_as_int(config.get("matches_per_round")),
            top_k=_as_int(config.get("evolve_top_k")),
        ),
        progress=RunProgressDTO(
            iteration=_as_int(state.get("iteration")),
            calls_used=calls_used,
            budget=budget,
            phase="finished" if lifecycle == "completed" else "idle",
            percent_budget=percent_budget,
        ),
        counts=RunCountsDTO(
            active=len(active),
            rejected=len(rejected),
            archived=len(archived),
            total=len(summaries),
            matches=len(matches),
            clusters=len(active_clusters),
        ),
        top=active[:10],
        graft=_graft_summary(discovered, state, config),
        artifacts=artifacts,
        process=ProcessSummary(),
        warnings=warnings,
    )


def normalize_run_hypotheses(discovered: DiscoveredRun) -> list[HypothesisSummary]:
    warnings: list[str] = []
    state = _read_json(discovered.state_path, warnings)
    hypotheses = _as_dict(state.get("hypotheses"))
    summaries = [_hypothesis_summary(hypothesis) for hypothesis in hypotheses.values()]
    summaries = [summary for summary in summaries if summary is not None]
    summaries.sort(key=lambda item: (-(item.elo or 0), item.id))
    return summaries


def _read_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.append(f"state_json_invalid:{path.name}:{exc.msg}")
    except OSError as exc:
        warnings.append(f"state_json_unreadable:{path.name}:{exc}")
    return {}


def _hypothesis_summary(raw: Any) -> HypothesisSummary | None:
    hypothesis = _as_dict(raw)
    hypothesis_id = hypothesis.get("id")
    if not hypothesis_id:
        return None

    review = _as_dict(hypothesis.get("review"))
    return HypothesisSummary(
        id=str(hypothesis_id),
        title=repair_mojibake(str(hypothesis.get("title") or hypothesis_id)),
        file=_optional_str(hypothesis.get("file")),
        elo=_as_float(hypothesis.get("elo")),
        matches=_as_int(hypothesis.get("matches")) or 0,
        wins=_as_int(hypothesis.get("wins")) or 0,
        status=str(hypothesis.get("status") or "unknown"),
        verdict=_optional_str(review.get("verdict")),
        cluster=_optional_str(hypothesis.get("cluster")),
        created_iter=_as_int(hypothesis.get("created_iter")),
        parent=_optional_str(hypothesis.get("parent")),
    )


def _graft_summary(
    discovered: DiscoveredRun,
    state: dict[str, Any],
    config: dict[str, Any],
) -> GraftDTO:
    collapse = _as_dict(state.get("collapse"))
    graft_config = _as_dict(config.get("graft"))
    return GraftDTO(
        applicable=discovered.version == "v2",
        enabled=bool(graft_config.get("enabled")) if discovered.version == "v2" else False,
        fired_count=_as_int(collapse.get("fired_count")) or 0,
        pending_injection=bool(state.get("pending_injection")),
    )


def _artifact_summaries(
    discovered: DiscoveredRun,
    hypotheses: dict[str, Any],
    warnings: list[str],
) -> list[ArtifactSummary]:
    candidates: list[tuple[Path, ArtifactKind]] = [
        (discovered.state_path, "state_json"),
        (discovered.run_dir / "research_overview.md", "research_overview"),
        (discovered.run_dir / "ideas_ranked.csv", "ideas_ranked_csv"),
        (discovered.run_dir / "prompt_package.json", "other"),
        (discovered.run_dir / "status.json", "other"),
    ]

    for hypothesis_id, raw_hypothesis in hypotheses.items():
        hypothesis = _as_dict(raw_hypothesis)
        relative_file = hypothesis.get("file")
        if not relative_file:
            warnings.append(f"hypothesis_missing_file:{hypothesis_id}")
            continue

        path = _artifact_path(discovered.engine_root, str(relative_file))
        if not is_within(path, discovered.engine_root):
            warnings.append(f"hypothesis_path_outside_root:{hypothesis_id}")
            continue

        candidates.append((path, "hypothesis_markdown"))
        if not path.exists():
            warnings.append(f"hypothesis_file_missing:{hypothesis_id}:{relative_file}")

    artifacts = [
        _artifact_summary(path, kind, discovered.engine_root)
        for path, kind in candidates
        if path.exists()
    ]
    artifacts.sort(key=lambda artifact: (artifact.kind, artifact.path))
    return artifacts


def _artifact_summary(path: Path, kind: ArtifactKind, engine_root: Path) -> ArtifactSummary:
    stat = path.stat()
    relative = _relative_path(path, engine_root)
    return ArtifactSummary(
        id=f"{kind}:{relative.replace('\\', '/')}",
        path=relative,
        kind=kind,
        size=stat.st_size,
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _artifact_path(engine_root: Path, relative_file: str) -> Path:
    normalized = relative_file.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute():
        return path
    return engine_root / path


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _title_from_goal(goal: str, fallback: str) -> str:
    cleaned = " ".join(goal.split())
    if not cleaned:
        return fallback
    if len(cleaned) <= 96:
        return cleaned
    return f"{cleaned[:93]}..."


def _percent_budget(calls_used: int | None, budget: int | None) -> int | None:
    if calls_used is None or not budget:
        return None
    return round(calls_used / budget * 100)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def repair_mojibake(value: str) -> str:
    """Undo cp1252-through-UTF-8 damage (`â€"` → `—`) in text read from the old engines.

    Applied to text on its way into the database or a DTO, never to bytes on disk: the
    archived files are the only copy of the historical runs and stay exactly as copied.
    """
    if not any(marker in value for marker in ("â", "Ã", "Â", "€")):
        return value

    try:
        repaired = value.encode("cp1252").decode("utf-8")
    except UnicodeError:
        return value

    if _mojibake_score(repaired) < _mojibake_score(value):
        return repaired
    return value


def _mojibake_score(value: str) -> int:
    return sum(value.count(marker) for marker in ("â", "Ã", "Â", "€", "�"))


# Kept as a private alias: this module's own callers predate the public name.
_repair_mojibake = repair_mojibake

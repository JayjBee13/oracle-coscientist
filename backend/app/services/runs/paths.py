"""Path confinement for run artifacts — one implementation, used everywhere.

There used to be four separate `_is_relative_to` helpers, each slightly different, and a
mix of ad-hoc `..`/absolute checks at the API edges. Artifacts are read from paths that
originate in state.json files we did not write, so the containment check is a security
boundary and should exist exactly once.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "PathEscapesRoot",
    "archive_root_ref",
    "confine",
    "is_within",
    "live_root_ref",
    "relative_posix",
    "resolve_run_root",
    "to_posix",
]

ARCHIVE_REF_PREFIX = "archive/"
LIVE_REF_PREFIX = "runs/"
ARCHIVE_SUBTREES = ("v1", "v2", "gui")


class PathEscapesRoot(ValueError):
    """A path resolved outside the root it was supposed to be confined to."""


def to_posix(path: Path | str) -> str:
    """Forward slashes, always. Historical state.json files store Windows separators."""
    return str(path).replace("\\", "/")


def is_within(path: Path | str, root: Path | str) -> bool:
    """True when `path` is `root` or sits underneath it, after resolving both.

    Resolution matters: it is what stops `root/../sibling` and what makes `/a/run2`
    correctly fall outside `/a/run` — a plain string prefix test gets that wrong.
    """
    resolved_root = Path(to_posix(root)).resolve()
    resolved_path = Path(to_posix(path)).resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def relative_posix(path: Path | str, root: Path | str) -> str:
    """`path` relative to `root` in POSIX form, or the full POSIX path when outside."""
    resolved_path = Path(to_posix(path))
    try:
        return resolved_path.relative_to(Path(to_posix(root))).as_posix()
    except ValueError:
        return to_posix(path)


def confine(root: Path | str, relative: Path | str) -> Path:
    """Resolve `relative` under `root`, refusing anything that reaches outside.

    Absolute paths and any `..` segment are refused outright — a traversal that happens
    to land back inside the root is still a caller doing something it should not.
    """
    normalized = to_posix(relative)
    candidate = Path(normalized)
    windows_candidate = PureWindowsPath(normalized)
    if candidate.is_absolute() or windows_candidate.drive:
        raise PathEscapesRoot(f"absolute path is not allowed: {to_posix(relative)}")
    if ".." in candidate.parts:
        raise PathEscapesRoot(f"parent traversal is not allowed: {to_posix(relative)}")

    resolved_root = Path(to_posix(root)).resolve()
    resolved = (resolved_root / candidate).resolve()
    if not is_within(resolved, resolved_root):
        raise PathEscapesRoot(f"{to_posix(relative)} resolves outside {to_posix(root)}")
    return resolved


def live_root_ref(engine_run_id: str) -> str:
    """Portable database reference for an app-owned run directory."""
    _validate_run_id(engine_run_id)
    return f"{LIVE_REF_PREFIX}{engine_run_id}"


def archive_root_ref(run_dir: Path | str, archive_root: Path | str) -> str:
    """Portable database reference for a directory inside the immutable archive."""
    resolved_root = Path(archive_root).resolve()
    resolved_run = Path(run_dir).resolve()
    if not is_within(resolved_run, resolved_root):
        raise PathEscapesRoot(
            f"{to_posix(run_dir)} is outside archive root {to_posix(archive_root)}"
        )
    relative = resolved_run.relative_to(resolved_root)
    if len(relative.parts) != 2 or relative.parts[0] not in ARCHIVE_SUBTREES:
        raise PathEscapesRoot(
            f"archived run must be <v1|v2|gui>/<run-id>, got {relative.as_posix()}"
        )
    _validate_run_id(relative.parts[1])
    return f"{ARCHIVE_REF_PREFIX}{relative.as_posix()}"


def resolve_run_root(
    *,
    source: str,
    source_version: str | None,
    engine_run_id: str,
    stored_root: str,
    settings: Settings,
) -> Path:
    """Resolve a stored run root against this process's configured mounts.

    New rows store logical references (``runs/<id>`` or
    ``archive/<subtree>/<id>``). Older rows contain absolute Windows paths. Live runs are
    authoritative under ``COSCIENTIST_RUNS_ROOT`` and can therefore be relocated from a
    Windows host to a Linux container solely from their engine run id. Imported runs are
    recovered from their archived subtree and resolved under ``IMPORT_ARCHIVE_ROOT``.
    The persisted machine path is never returned directly.
    """
    _validate_run_id(engine_run_id)
    if source != "imported":
        return confine(settings.runs_root, engine_run_id)

    relative = _archive_relative(stored_root, engine_run_id)
    if relative is not None:
        return confine(settings.import_archive_root, relative)

    candidates = _archive_candidates(source_version, engine_run_id)
    existing = [
        confine(settings.import_archive_root, candidate)
        for candidate in candidates
        if confine(settings.import_archive_root, candidate).is_dir()
    ]
    if len(existing) == 1:
        return existing[0]
    if len(existing) > 1:
        raise PathEscapesRoot(
            f"archive location for {engine_run_id} is ambiguous under "
            f"{to_posix(settings.import_archive_root)}"
        )
    return confine(settings.import_archive_root, candidates[0])


def _archive_relative(stored_root: str, engine_run_id: str) -> str | None:
    normalized = to_posix(stored_root).rstrip("/")
    if not normalized:
        return None
    parts = tuple(part for part in normalized.split("/") if part)

    if normalized.startswith(ARCHIVE_REF_PREFIX):
        relative = normalized.removeprefix(ARCHIVE_REF_PREFIX)
        relative_parts = tuple(part for part in relative.split("/") if part)
    else:
        # Legacy roots are absolute host paths. Only their final archive subtree and run
        # directory have portable meaning; drive letters and host directories are ignored.
        relative_parts = parts[-2:]

    if (
        len(relative_parts) == 2
        and relative_parts[0] in ARCHIVE_SUBTREES
        and relative_parts[1] == engine_run_id
    ):
        return "/".join(relative_parts)
    return None


def _archive_candidates(source_version: str | None, engine_run_id: str) -> tuple[str, ...]:
    if source_version == "v2":
        subtrees = ("v2",)
    elif source_version == "v1":
        subtrees = ("v1", "gui")
    else:
        subtrees = ARCHIVE_SUBTREES
    return tuple(f"{subtree}/{engine_run_id}" for subtree in subtrees)


def _validate_run_id(engine_run_id: str) -> None:
    candidate = Path(to_posix(engine_run_id))
    if (
        not engine_run_id
        or candidate.is_absolute()
        or len(candidate.parts) != 1
        or candidate.parts[0] in {".", ".."}
        or ":" in engine_run_id
    ):
        raise PathEscapesRoot(f"invalid engine run id: {engine_run_id!r}")

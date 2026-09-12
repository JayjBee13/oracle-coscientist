"""Serve one file out of a run's directory.

Addressed as `/api/artifacts/{run_id}/{path}`: the run identifies the directory, the path
is relative to it. The old endpoint took an opaque `kind:path` id, resolved it by walking
every artifact root of every run, and read the whole file into a JSON string.

The path arrives from a URL and the directories being served include the archive that
holds the only copy of twelve historical runs, so `confine` is a security boundary, not a
tidiness check: absolute paths and any `..` segment are refused before the filesystem is
touched, and the result must still resolve inside the run's own root.
"""

from __future__ import annotations

import mimetypes
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.errors import ApiError, not_found
from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser
from app.db.session import get_db
from app.services.runs import reads
from app.services.runs.paths import PathEscapesRoot, confine, resolve_run_root

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])
DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# The ceiling is about what a browser tab can take, not what the disk holds. The largest
# thing the engines write is a 21KB overview.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024

_TEXT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
}


@router.get("/{run_id}/{artifact_path:path}")
def read_artifact(
    run_id: str,
    artifact_path: str,
    db: DbSession,
    settings: SettingsDep,
    current: CurrentUser,
) -> FileResponse:
    run = reads.find_run(db, run_id, current.identity)
    if run is None:
        raise not_found("run_not_found", "No run with that id.", run_id=run_id)
    if not run.root_path:
        raise not_found(
            "artifact_not_found", "This run has no artifact directory.", run_id=str(run.id)
        )

    try:
        root = resolve_run_root(
            source=run.source,
            source_version=run.source_version,
            engine_run_id=run.engine_run_id,
            stored_root=run.root_path,
            settings=settings,
        )
        path = confine(root, artifact_path)
    except PathEscapesRoot as exc:
        raise ApiError(
            403,
            "artifact_outside_root",
            "That path is outside the run's directory.",
            {"path": artifact_path},
        ) from exc

    if not path.is_file():
        raise not_found("artifact_not_found", "No such file in this run.", path=artifact_path)

    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ApiError(
            413,
            "artifact_too_large",
            "That file is too large to open here.",
            {"path": artifact_path, "size": size, "limit": MAX_ARTIFACT_BYTES},
        )

    return FileResponse(path, media_type=_media_type(path.suffix.lower()))


def _media_type(suffix: str) -> str:
    if suffix in _TEXT_TYPES:
        return f"{_TEXT_TYPES[suffix]}; charset=utf-8"
    guessed, _ = mimetypes.guess_type(f"artifact{suffix}")
    return guessed or "application/octet-stream"

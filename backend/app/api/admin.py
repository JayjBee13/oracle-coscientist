"""Operator endpoints: re-import the archive, and the kill switch."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.errors import ApiError
from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser
from app.engine.store import RunStore
from app.schemas.dto import HaltAllResponse, ImportReportResponse
from app.services.runs.controls import halt_all
from app.services.runs.importer import import_all

router = APIRouter(prefix="/api/admin", tags=["admin"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/reimport", response_model=ImportReportResponse)
def reimport(settings: SettingsDep, current: CurrentUser) -> dict[str, object]:
    """Re-read the archive into the database.

    Idempotent, so this is also the repair when a run was edited by hand or a startup
    import was interrupted: runs match on `engine_run_id` and hypotheses on `hid`, so
    nothing is duplicated and the ids already in the frontend's URLs keep working.
    """
    _require_admin(current)
    return import_all(settings).as_dict()


@router.post("/halt-all", response_model=HaltAllResponse)
def halt_everything(settings: SettingsDep, current: CurrentUser) -> dict[str, object]:
    """Stop every live run and kill every supervisor this app started.

    The panic button behind the app shell's red "Stop all". It sets the stop flag on each
    active run *and* kills its recorded pid, because the situations that call for this are
    the ones where a supervisor might not be reading its flag any more. Runs whose pid now
    belongs to something else are left for the cooperative path rather than guessed at.
    """
    _require_admin(current)
    return halt_all(RunStore(settings=settings), settings=settings)


def _require_admin(current: CurrentUser) -> None:
    if not current.identity.is_admin:
        raise ApiError(403, "admin_required", "Only an administrator can perform this action.")

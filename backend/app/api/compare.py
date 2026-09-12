"""`GET /api/compare?baseline=&challenger=` — two runs, side by side."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.errors import ApiError, not_found
from app.core.identity import CurrentUser
from app.db.session import get_db
from app.schemas.dto import CompareAnalytics
from app.services import compare
from app.services.runs import reads

router = APIRouter(prefix="/api/compare", tags=["compare"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=CompareAnalytics)
def compare_runs(
    baseline: str, challenger: str, db: DbSession, current: CurrentUser
) -> CompareAnalytics:
    base_run = _run(db, baseline, "baseline", current)
    challenger_run = _run(db, challenger, "challenger", current)
    try:
        analytics = compare.compare_runs(db, base_run, challenger_run)
    except compare.SameRunComparison as exc:
        raise ApiError(
            422,
            "same_run_comparison",
            "Pick two different runs to compare.",
            {"run_id": str(base_run.id)},
        ) from exc
    return CompareAnalytics.model_validate(analytics)


def _run(db: Session, reference: str, side: str, current: CurrentUser):
    run = reads.find_run(db, reference, current.identity)
    if run is None:
        raise not_found(
            "run_not_found", f"No run with that id on the {side} side.", run_id=reference
        )
    return run

"""One hypothesis, in full.

The product's whole point is readable science, and until this endpoint existed the
hypotheses were the one thing the GUI never showed: the bodies sat on disk, the reviews
were parsed and thrown away, and the leaderboard listed titles nobody could open.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.errors import not_found
from app.core.identity import CurrentUser
from app.db.session import get_db
from app.schemas.dto import HypothesisDetail
from app.services.runs import reads

router = APIRouter(prefix="/api/hypotheses", tags=["hypotheses"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{hypothesis_id}", response_model=HypothesisDetail)
def get_hypothesis(
    hypothesis_id: UUID, db: DbSession, current: CurrentUser
) -> HypothesisDetail:
    """Body, reviews, every match it played, and its place in the lineage."""
    detail = reads.hypothesis_detail(db, hypothesis_id, current.identity)
    if detail is None:
        raise not_found(
            "hypothesis_not_found", "No hypothesis with that id.", hypothesis_id=str(hypothesis_id)
        )
    return HypothesisDetail.model_validate(detail)

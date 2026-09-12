"""The frozen `Workshop` response shape of plan C5, and the request bodies beside it.

Kept out of `dto.py` only so the workshop lands as one self-contained module; the field
lists are the same contract, and the same Wave 4 OpenAPI export covers them.

`recommended_settings` is part of the promise, not a detail: the launch wizard prefills
from it, and the previous UI ignored it entirely — recommending 120 calls over 4 rounds
while the form beside it showed 20 over 1.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.dto import GroundingDepth, Harness, WorkshopState

__all__ = [
    "ChooseWorkshopRequest",
    "ChosenPrompt",
    "CreateWorkshopRequest",
    "RefineWorkshopRequest",
    "Workshop",
    "WorkshopContextDoc",
    "WorkshopError",
    "WorkshopOption",
    "WorkshopRecommendedSettings",
]


class WorkshopRecommendedSettings(BaseModel):
    """What this option wants the run configured as. Defaults match the Standard preset."""

    rounds: int = 3
    budget_calls: int = 60
    matches_per_round: int = 6
    grounding_depth: GroundingDepth = GroundingDepth.STANDARD


class WorkshopOption(BaseModel):
    id: str
    ordinal: int
    prompt: str
    strategy: str
    optimizes_for: str | None = None
    excludes: str | None = None
    rationale: str | None = None
    recommended_settings: WorkshopRecommendedSettings
    chosen: bool = False
    rejected: bool = False
    note: str | None = None


class WorkshopError(BaseModel):
    """Why a workshop stopped. `code` is for the app, `message` is for the scientist."""

    code: str
    message: str
    detail: str | None = None


class Workshop(BaseModel):
    id: str
    question: str
    state: WorkshopState
    harness: Harness
    options: list[WorkshopOption] = []
    error: WorkshopError | None = None
    created_at: str | None = None


class WorkshopContextDoc(BaseModel):
    name: str
    text: str


class CreateWorkshopRequest(BaseModel):
    question: str
    harness: Harness = Harness.CLAUDE
    context_docs: list[WorkshopContextDoc] = []


class RefineWorkshopRequest(BaseModel):
    base: str = Field(description="An option id, or 'merge' to combine both.")
    note: str = ""


class ChooseWorkshopRequest(BaseModel):
    option_id: str
    final_prompt: str = ""
    """The scientist's edit. Stored verbatim; empty means keep the option as written."""


class ChosenPrompt(BaseModel):
    prompt: str

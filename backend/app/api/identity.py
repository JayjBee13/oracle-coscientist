"""The request's live identity, for client-side capability presentation."""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.identity import CurrentUser

router = APIRouter(prefix="/api", tags=["identity"])


class IdentityResponse(BaseModel):
    username: str
    email: str | None
    display_name: str | None
    groups: list[str]
    is_admin: bool
    source: Literal["gateway", "local"]


@router.get("/me", response_model=IdentityResponse)
def current_identity(current: CurrentUser) -> IdentityResponse:
    return IdentityResponse(
        username=current.identity.username,
        email=current.identity.email,
        display_name=current.identity.display_name,
        groups=list(current.identity.groups),
        is_admin=current.identity.is_admin,
        source=current.identity.source,
    )

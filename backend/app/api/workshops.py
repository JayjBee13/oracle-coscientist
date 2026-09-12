"""The prompt workshop endpoints (plan C5).

Four calls, and the shape of the surface is the product decision: creating a workshop
starts a call and returns straight away, `GET` is how the wizard watches it, and neither
`GET` nor anything else on the read path writes — the old endpoint reconciled state on
read, so opening a workshop twice could change its answer.

`refine` and `choose` are the two things a scientist does with the options: ask again with
a note, or settle on one and edit it. The edit is the prompt; nothing inspects it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.errors import ApiError
from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser
from app.schemas.workshop import (
    ChooseWorkshopRequest,
    ChosenPrompt,
    CreateWorkshopRequest,
    RefineWorkshopRequest,
    Workshop,
)
from app.services.workshop.service import (
    OptionNotFound,
    WorkshopInputError,
    WorkshopNotFound,
    WorkshopService,
    WorkshopStateError,
)

router = APIRouter(prefix="/api/workshops", tags=["workshops"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_workshop_service(settings: SettingsDep) -> WorkshopService:
    """A service per request: it holds no workshop state, and the call is off-thread."""
    return WorkshopService(settings=settings)


ServiceDep = Annotated[WorkshopService, Depends(get_workshop_service)]


@contextmanager
def _translated(workshop_id: UUID | None = None) -> Iterator[None]:
    """Service exceptions as the `{code, message, details?}` envelope."""
    try:
        yield
    except WorkshopNotFound as exc:
        raise ApiError(
            404,
            "workshop_not_found",
            "No workshop with that id.",
            {"workshop_id": str(workshop_id)} if workshop_id else None,
        ) from exc
    except OptionNotFound as exc:
        raise ApiError(
            404,
            "workshop_option_not_found",
            "That option is not one of this workshop's current choices.",
        ) from exc
    except WorkshopStateError as exc:
        raise ApiError(
            409,
            "workshop_not_ready",
            str(exc),
            {"state": exc.state, "expected": exc.expected},
        ) from exc
    except WorkshopInputError as exc:
        raise ApiError(422, exc.code, exc.message) from exc


@router.post("", response_model=Workshop)
def create_workshop(
    request: CreateWorkshopRequest, service: ServiceDep, current: CurrentUser
) -> Workshop:
    """Open a workshop. Returns in state `refining`; poll `GET` for the options."""
    with _translated():
        workshop = service.create(
            request.question,
            harness=request.harness,
            context_docs=[doc.model_dump() for doc in request.context_docs],
            owner_id=current.user.id,
        )
    return Workshop.model_validate(workshop)


@router.get("/{workshop_id}", response_model=Workshop)
def get_workshop(
    workshop_id: UUID, service: ServiceDep, current: CurrentUser
) -> Workshop:
    """The workshop as it stands. Idempotent — this endpoint never writes."""
    with _translated(workshop_id):
        return Workshop.model_validate(
            service.get(
                workshop_id, owner_id=current.user.id, is_admin=current.identity.is_admin
            )
        )


@router.post("/{workshop_id}/refine", response_model=Workshop)
def refine_workshop(
    workshop_id: UUID,
    request: RefineWorkshopRequest,
    service: ServiceDep,
    current: CurrentUser,
) -> Workshop:
    """Reject the current pair and ask again, building on one option or merging both."""
    with _translated(workshop_id):
        workshop = service.refine(
            workshop_id,
            base=request.base,
            note=request.note,
            owner_id=current.user.id,
            is_admin=current.identity.is_admin,
        )
    return Workshop.model_validate(workshop)


@router.post("/{workshop_id}/choose", response_model=ChosenPrompt)
def choose_workshop_option(
    workshop_id: UUID,
    request: ChooseWorkshopRequest,
    service: ServiceDep,
    current: CurrentUser,
) -> ChosenPrompt:
    """Settle on an option. The scientist's edited prompt wins, stored exactly as typed."""
    with _translated(workshop_id):
        chosen = service.choose(
            workshop_id,
            option_id=request.option_id,
            final_prompt=request.final_prompt,
            owner_id=current.user.id,
            is_admin=current.identity.is_admin,
        )
    return ChosenPrompt.model_validate(chosen)

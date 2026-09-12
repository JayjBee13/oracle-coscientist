"""Resolve the authenticated person and persist their directory entry.

This is the only module that knows whether identity came from the gateway or the
single-user local fallback. Route code consumes ``CurrentUser`` and never interprets
trusted headers itself.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.auth import PUBLIC_PATHS, is_web_ui_path
from app.core.config import Settings
from app.db.engine_models import User
from app.db.session import get_db

PRIVATE_CACHE_CONTROL = "private, no-store"


@dataclass(frozen=True, slots=True)
class Identity:
    username: str
    email: str | None
    display_name: str | None
    groups: tuple[str, ...]
    is_admin: bool
    source: Literal["gateway", "local"]


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    identity: Identity
    user: User


def resolve_identity(request: Request, settings: Settings) -> Identity:
    """Resolve one request, failing closed whenever a gateway credential is supplied."""
    supplied = request.headers.get("X-Gateway-Secret")
    if not settings.identity_trust_headers:
        return _local_identity(settings)
    if supplied is None:
        if settings.identity_require_gateway:
            raise GatewayIdentityRequired
        return _local_identity(settings)

    expected = settings.gateway_secret
    if expected is None or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise InvalidGatewaySecret

    username = (request.headers.get("Remote-User") or "").strip()
    if not username:
        raise GatewayIdentityRequired
    groups = tuple(
        group for raw in request.headers.getlist("Remote-Groups") for group in _groups(raw)
    )
    email = (request.headers.get("Remote-Email") or "").strip() or None
    display_name = (request.headers.get("Remote-Name") or "").strip() or None
    admin_group = settings.identity_admin_group.strip()
    return Identity(
        username,
        email,
        display_name,
        groups,
        admin_group in groups,
        "gateway",
    )


def _local_identity(settings: Settings) -> Identity:
    """The backwards-compatible single-user identity, used only outside required mode."""
    username = settings.local_identity_username.strip()
    if not username:
        raise RuntimeError("LOCAL_IDENTITY_USERNAME must not be blank")
    return Identity(username, None, username, ("admin",), True, "local")


class InvalidGatewaySecret(Exception):
    pass


class GatewayIdentityRequired(Exception):
    pass


def _groups(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


class IdentityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS or is_web_ui_path(path):
            return await call_next(request)
        if _origin_is_forbidden(request, self.settings):
            expected_origin = self.settings.frontend_origin.rstrip("/")
            return _private_response(JSONResponse(
                status_code=403,
                content={
                    "code": "origin_not_allowed",
                    "message": "This request's Origin is not allowed.",
                    "details": {"expected_origin": expected_origin},
                },
            ))
        try:
            request.state.identity = resolve_identity(request, self.settings)
        except InvalidGatewaySecret:
            return _private_response(JSONResponse(
                status_code=401,
                content={
                    "code": "invalid_gateway_secret",
                    "message": "The gateway identity credential is invalid.",
                },
            ))
        except GatewayIdentityRequired:
            return _private_response(JSONResponse(
                status_code=401,
                content={
                    "code": "gateway_auth_required",
                    "message": "Sign in through the gateway to access this API.",
                },
            ))
        return _private_response(await call_next(request))


def _private_response(response: Response) -> Response:
    response.headers["Cache-Control"] = PRIVATE_CACHE_CONTROL
    return response


def _origin_is_forbidden(request: Request, settings: Settings) -> bool:
    """Reject a browser mutation when its origin is not the configured application."""
    if not settings.identity_require_gateway or request.method not in {
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    }:
        return False
    supplied = request.headers.get("Origin")
    expected = settings.frontend_origin.rstrip("/")
    return supplied is not None and supplied != expected


def get_current_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> AuthenticatedUser:
    identity: Identity = request.state.identity
    return AuthenticatedUser(identity=identity, user=upsert_user(db, identity))


def upsert_user(db: Session, identity: Identity) -> User:
    """Refresh the directory cache for a live identity and return its stable row."""
    now = datetime.now(UTC)
    statement = (
        insert(User)
        .values(
            username=identity.username,
            email=identity.email,
            display_name=identity.display_name,
            is_admin=identity.is_admin,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=[User.username],
            set_={
                "email": identity.email,
                "display_name": identity.display_name,
                "is_admin": identity.is_admin,
                "last_seen_at": now,
            },
        )
        .returning(User)
    )
    user = db.execute(statement).scalar_one()
    db.commit()
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


def get_request_identity(request: Request) -> Identity:
    return request.state.identity


RequestIdentity = Annotated[Identity, Depends(get_request_identity)]

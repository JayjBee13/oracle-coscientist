"""The `{code, message, details?}` error envelope, raised as an exception.

Plan C5 promises every error looks the same. FastAPI's own shape is `{"detail": ...}`,
which puts the machine-readable part one level down and leaves its contents up to whoever
raised it — the old code had ad-hoc dicts and a helper that classified failures by
substring-matching exception text.

`ApiError` is the whole of it: a status, a stable `code` the frontend can branch on, a
sentence a person can read, and optional structured details. `install_error_handlers`
renders it flat, and also flattens FastAPI's own `RequestValidationError` (a bad request
body) into the same shape, so every failure this API can produce looks the same on the
wire — nothing here has to reach for `HTTPException` and its nested `{"detail": ...}`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

__all__ = ["ApiError", "bad_request", "conflict", "install_error_handlers", "not_found"]


class ApiError(Exception):
    """An error the API states in the envelope rather than leaking a traceback for."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def not_found(code: str, message: str, **details: Any) -> ApiError:
    return ApiError(404, code, message, details or None)


def conflict(code: str, message: str, **details: Any) -> ApiError:
    """409: the request is well formed but the system is in a state that refuses it.

    Both users of this carry the way out in their details — the run holding the lane, or
    the actions that *are* legal here — because "conflict" on its own is not actionable.
    """
    return ApiError(409, code, message, details or None)


def bad_request(code: str, message: str, **details: Any) -> ApiError:
    return ApiError(400, code, message, details or None)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI's own shape is `{"detail": [...]}` — flatten it into the same envelope."""
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "The request did not match the expected shape.",
                "details": {"errors": jsonable_encoder(exc.errors())},
            },
        )

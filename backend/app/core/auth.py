import re

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.services.events.tickets import TICKETS

PUBLIC_PATHS = {"/health", "/api/health"}

# The API is `/api/*` and nothing else, so everything outside it is the frontend build this
# process may also be serving — `index.html`, its hashed assets, the icons beside them. Those
# stay reachable without a token on purpose: a page that 401s is a page nobody can open to
# supply the token, and a static shell holds nothing a viewer could not read by asking the
# API with the credential they already need. What it does *not* cover is the schema: the docs
# routes live outside `/api` too, and publishing the shape of every endpoint to anyone who
# finds the hostname is a different decision from publishing the login screen.
PRIVATE_NON_API_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def is_web_ui_path(path: str) -> bool:
    """True for a path the frontend build owns, rather than the API or its schema."""
    if path == "/api" or path.startswith("/api/"):
        return False
    return path not in PRIVATE_NON_API_PATHS


# The one route a credential may travel in the query string on. `EventSource` cannot send
# headers, so without this a live stream is unwatchable the moment APP_AUTH_TOKEN is set —
# which is how the previous frontend ended up with no auth support at all.
SSE_EVENTS_PATH = re.compile(r"^/api/runs/(?P<run_id>[^/]+)/events/?$")


class OptionalTokenAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str | None) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not self.token or request.method == "OPTIONS" or path in PUBLIC_PATHS:
            return await call_next(request)

        if is_web_ui_path(path):
            return await call_next(request)

        if _request_token(request) == self.token or self._query_credential_ok(request):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={
                "code": "auth_required",
                "message": "A valid token is required for this request.",
            },
            headers={"Cache-Control": "private, no-store"},
        )

    def _query_credential_ok(self, request: Request) -> bool:
        """Accept `?ticket=` or `?token=`, on the SSE route and nowhere else.

        The ticket is redeemed here rather than in the endpoint because this is the only
        place that sees every request to that path, so "single use" cannot be dodged by
        reaching the route another way.
        """
        match = SSE_EVENTS_PATH.match(request.url.path)
        if match is None:
            return False

        ticket = request.query_params.get("ticket")
        if ticket and TICKETS.consume(ticket, match.group("run_id")):
            return True
        return request.query_params.get("token") == self.token


def _request_token(request: Request) -> str | None:
    header_token = request.headers.get("X-Coscientist-Token")
    if header_token:
        return header_token

    authorization = request.headers.get("Authorization")
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return None

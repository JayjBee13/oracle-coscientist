"""Serving the built frontend from the API process.

The app is two tiers in development — Vite on 5173, uvicorn on 8787 — and one process in
deployment, because the second tier was a *dev server*, and a dev server holding up remote
access is a foot-gun with a long fuse: it rebuilds on file changes, it answers 403 to any
Host it was not told about, and nothing restarts it when it dies. Serving the build from
here means one thing to keep alive and one thing to start at boot.

Three rules this module keeps, each of which is a way the naive version breaks:

* **`/api` always belongs to the API.** The catch-all below refuses those paths outright
  rather than falling back to `index.html`, so a mistyped endpoint answers with the app's
  own 404 shape instead of 200 and a page — which is the failure that turns a typo in a
  client into an afternoon.
* **Every other path falls back to `index.html`.** The frontend routes on the client, so a
  deep link is a real URL that the server has never heard of. Anything else 404s on reload.
* **A missing build is not an error.** A backend with no `dist` beside it serves the API and
  says so once in the log. That is the ordinary state of a dev machine, and of this repo's
  tests, and neither should have to build a frontend to start.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.services.runs.paths import is_within

log = logging.getLogger(__name__)

__all__ = ["ASSET_CACHE_CONTROL", "INDEX_CACHE_CONTROL", "install_web_ui"]

# `index.html` names every other file by a hashed URL, so it is the one file whose staleness
# is permanent: a cached copy points at a build that is gone. The hashed files it names can
# be cached forever precisely because their names change when their contents do.
INDEX_CACHE_CONTROL = "no-store"
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


def install_web_ui(app: FastAPI, dist_dir: Path | None) -> bool:
    """Mount the built frontend, if there is one. True when it was mounted.

    Call **after** every router: FastAPI matches in registration order and the catch-all
    here matches everything.
    """
    if dist_dir is None:
        return False
    dist = Path(dist_dir)
    index = dist / "index.html"
    if not index.is_file():
        log.info("No frontend build at %s — serving the API only.", dist)
        return False

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", _ImmutableStatic(directory=assets), name="assets")

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            # Past every router, so this is a path no endpoint claimed. Answering with the
            # page would dress a 404 up as a working request.
            raise HTTPException(status_code=404, detail="Not Found")
        if path:
            candidate = dist / path
            # `is_within` resolves both sides, which is what stops `../` from reaching out
            # of the build directory — a plain prefix test does not.
            if is_within(candidate, dist) and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": INDEX_CACHE_CONTROL})

    log.info("Serving the frontend build at %s", dist)
    return True


class _ImmutableStatic(StaticFiles):
    """`StaticFiles` that lets the browser keep what it fetched.

    Vite hashes these filenames, so a changed file is a changed URL and a year-long cache
    can never serve the wrong bytes.
    """

    def file_response(self, *args, **kwargs) -> Response:  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return response

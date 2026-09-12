"""The frontend served by the API process, and the boundary between the two.

Deployment runs one process: uvicorn serves `/api` and the built SPA beside it. The tests
that matter here are the three ways that arrangement goes wrong — an API 404 answered with
a page, a deep link answered with a 404, and a token that locks the operator out of the
page they need in order to supply the token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

TOKEN = "test-token-value"


def build_dist(root: Path) -> Path:
    """A minimal stand-in for `npm run build`'s output."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Oracle</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return dist


def make_client(dist: Path | None, token: str | None = None) -> TestClient:
    settings = Settings(
        APP_ENV="test",
        IMPORT_ON_STARTUP=False,
        APP_AUTH_TOKEN=token,
        WEB_DIST_DIR=str(dist) if dist else str(Path("does-not-exist")),
    )
    return TestClient(create_app(settings))


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    return build_dist(tmp_path)


def test_serves_the_page_at_the_root(dist: Path) -> None:
    with make_client(dist) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Oracle" in response.text


def test_a_deep_link_gets_the_page_rather_than_a_404(dist: Path) -> None:
    """The frontend routes on the client, so `/runs/abc` is a URL this server never had."""
    with make_client(dist) as client:
        for path in ("/runs", "/runs/abc-123", "/workshop/nested/deeper"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "Oracle" in response.text, path


def test_an_unknown_api_path_still_404s_as_json(dist: Path) -> None:
    """The failure this guards: a mistyped endpoint answered 200 with a page."""
    with make_client(dist) as client:
        response = client.get("/api/nope")
    assert response.status_code == 404
    assert "application/json" in response.headers["content-type"]
    assert "<!doctype" not in response.text.lower()


def test_the_api_still_answers_with_the_build_mounted(dist: Path) -> None:
    with make_client(dist) as client:
        assert client.get("/api/health").status_code == 200


def test_hashed_assets_are_cacheable_and_the_shell_is_not(dist: Path) -> None:
    with make_client(dist) as client:
        asset = client.get("/assets/index-abc123.js")
        shell = client.get("/")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    # `index.html` names the hashed files, so a cached copy points at a build that is gone.
    assert shell.headers["cache-control"] == "no-store"


def test_a_file_beside_the_build_is_served(dist: Path) -> None:
    with make_client(dist) as client:
        response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.text == "<svg/>"


def test_traversal_gets_the_shell_rather_than_a_file_outside_the_build(
    dist: Path, tmp_path: Path
) -> None:
    (tmp_path / "secret.txt").write_text(
        "DATABASE_URL=postgresql://user:pw@host/db", encoding="utf-8"
    )
    with make_client(dist) as client:
        for path in (
            "/%2e%2e%2fsecret.txt",
            "/..%2fsecret.txt",
            "/assets/%2e%2e%2f%2e%2e%2fsecret.txt",
        ):
            response = client.get(path)
            assert "DATABASE_URL" not in response.text, path
            assert response.status_code in (200, 404), path


def test_without_a_build_the_api_runs_and_the_root_404s(tmp_path: Path) -> None:
    """The ordinary state of a dev box, and of every other test in this suite."""
    with make_client(None) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 404


class TestWithATokenSet:
    """`APP_AUTH_TOKEN` gates the API. It must not gate the page that asks for the token."""

    def test_the_page_loads_without_a_token(self, dist: Path) -> None:
        with make_client(dist, token=TOKEN) as client:
            page = client.get("/")
            asset = client.get("/assets/index-abc123.js")
            deep = client.get("/runs/abc-123")
        assert page.status_code == 200
        assert asset.status_code == 200
        assert deep.status_code == 200

    def test_the_api_still_requires_the_token(self, dist: Path) -> None:
        with make_client(dist, token=TOKEN) as client:
            assert client.get("/api/runs").status_code == 401
            assert client.get("/api/health").status_code == 200
            ok = client.get("/api/capabilities", headers={"X-Coscientist-Token": TOKEN})
        assert ok.status_code == 200

    def test_the_schema_is_not_published_with_the_shell(self, dist: Path) -> None:
        """`/openapi.json` and `/docs` sit outside `/api` too — and stay private."""
        with make_client(dist, token=TOKEN) as client:
            assert client.get("/openapi.json").status_code == 401
            assert client.get("/docs").status_code == 401

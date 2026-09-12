"""`/api/artifacts/{run_id}/{path}` — files from a run's directory, and nothing else.

The directories being served include the archive holding the only copy of the owner's
twelve historical runs, so the confinement cases here are the point of the endpoint.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.engine_models import Run
from app.db.session import get_session_factory
from tests.support.imported import (
    GRAFT_RUN,
    LEGACY_RUN,
    fixture_settings,
    imported_client,
)


@pytest.fixture(scope="module")
def client():
    return imported_client()


def test_serves_a_file_from_the_runs_own_directory(client):
    response = client.get(f"/api/artifacts/{LEGACY_RUN}/state.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert '"run_id"' in response.text


def test_serves_a_hypothesis_body_as_markdown(client):
    response = client.get(f"/api/artifacts/{GRAFT_RUN}/hypotheses/h001.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# ")


def test_serves_an_import_after_relocating_its_stored_windows_root(client):
    settings = fixture_settings()
    factory = get_session_factory(settings)
    with factory() as session:
        run = session.execute(
            select(Run).where(Run.engine_run_id == LEGACY_RUN)
        ).scalar_one()
        portable = run.root_path
        run.root_path = (
            "C:/srv/oracle/archive/"
            f"imported-runs/v1/{LEGACY_RUN}"
        )
        session.commit()

    try:
        response = client.get(f"/api/artifacts/{LEGACY_RUN}/state.json")
        assert response.status_code == 200
        assert '"run_id"' in response.text
    finally:
        with factory() as session:
            run = session.execute(
                select(Run).where(Run.engine_run_id == LEGACY_RUN)
            ).scalar_one()
            run.root_path = portable
            session.commit()


@pytest.mark.parametrize(
    "path",
    [
        # Percent-encoded, because an HTTP client resolves a literal `..` out of the URL
        # before it is ever sent — the server must refuse the decoded form it does see.
        "%2E%2E/%2E%2E/%2E%2E/MEMORY.md",
        "hypotheses/%2E%2E/%2E%2E/run-fixture-mojibake/state.json",
        "C:/Windows/win.ini",
        "/etc/passwd",
    ],
)
def test_refuses_to_leave_the_run_directory(client, path):
    response = client.get(f"/api/artifacts/{LEGACY_RUN}/{path}")

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "artifact_outside_root"


def test_missing_file_answers_the_error_envelope(client):
    response = client.get(f"/api/artifacts/{LEGACY_RUN}/hypotheses/h999.md")

    assert response.status_code == 404
    assert response.json()["code"] == "artifact_not_found"


def test_unknown_run_answers_the_error_envelope(client):
    response = client.get("/api/artifacts/not-a-run/state.json")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"

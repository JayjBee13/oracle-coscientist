"""PATCH/DELETE `runs/{id}`, `note`, `hypotheses/{hid}/archive`, `export.md`, `context`.

Mutating tests create their own throwaway run rather than touching the shared imported
fixtures (`GRAFT_RUN` and friends): the whole suite shares one schema, and `test_runs_api.py`
asserts exact hypothesis counts and statuses against those fixtures, so archiving one of
their hypotheses here would break assertions that have nothing to do with this file.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from app.engine.store import RunStore
from tests.support.imported import (
    GRAFT_RUN,
    LEGACY_RUN,
    REJECTED_RUN,
    fixture_settings,
    imported_client,
)


@pytest.fixture(scope="module")
def client():
    return imported_client()


@pytest.fixture
def store() -> RunStore:
    return RunStore(settings=fixture_settings())


def _new_run(
    store: RunStore, engine_run_id: str, *, lifecycle: str = "completed", **overrides: object
) -> str:
    defaults: dict[str, object] = {
        "question": "A practice question",
        "prompt": "A practice prompt",
        "harness": "claude",
        "source": "app",
        "lifecycle": lifecycle,
        "engine_run_id": engine_run_id,
        "budget_calls": 10,
        "budget_usd": Decimal("2.00"),
    }
    defaults.update(overrides)
    return str(store.create_run(**defaults))


# --------------------------------------------------------------------------- patch


def test_patch_renames_and_archives_a_run(client, store):
    run_id = _new_run(store, "run-patch-rename")

    renamed = client.patch(f"/api/runs/{run_id}", json={"title": "A better title"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "A better title"
    assert renamed.json()["archived"] is False

    archived = client.patch(f"/api/runs/{run_id}", json={"archived": True})
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] is True
    assert archived.json()["title"] == "A better title"  # untouched by the second patch


def test_patch_unknown_run_answers_the_error_envelope(client):
    response = client.patch("/api/runs/does-not-exist", json={"title": "x"})

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_patch_rejects_a_blank_title(client, store):
    run_id = _new_run(store, "run-patch-blank-title")

    response = client.patch(f"/api/runs/{run_id}", json={"title": ""})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# -------------------------------------------------------------------------- delete


def test_delete_soft_deletes_and_the_run_disappears(client, store):
    run_id = _new_run(store, "run-delete-me")

    listed_before = client.get("/api/runs", params={"page_size": 100}).json()["items"]
    assert "run-delete-me" in {item["engine_run_id"] for item in listed_before}

    response = client.delete(f"/api/runs/{run_id}")
    assert response.status_code == 204
    assert response.content == b""

    assert client.get(f"/api/runs/{run_id}").status_code == 404
    listed_after = client.get("/api/runs", params={"page_size": 100}).json()["items"]
    assert "run-delete-me" not in {item["engine_run_id"] for item in listed_after}


def test_delete_unknown_run_answers_the_error_envelope(client):
    response = client.delete("/api/runs/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


# ---------------------------------------------------------------------------- note


def test_note_is_accepted_and_queued_while_a_run_is_active(client, store):
    # `paused` (not `running`): a lane-holding lifecycle would collide with every other
    # `claude`-harness fixture the module's tests create, since only one may hold it.
    run_id = _new_run(store, "run-note-active", lifecycle="paused")

    response = client.post(f"/api/runs/{run_id}/note", json={"text": "focus on mechanism"})

    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": True}
    run = store.get_run(UUID(run_id))
    assert run is not None
    assert run["pending_interventions"][-1]["text"] == "focus on mechanism"


def test_note_is_refused_when_blank(client, store):
    run_id = _new_run(store, "run-note-blank", lifecycle="paused")

    response = client.post(f"/api/runs/{run_id}/note", json={"text": "   "})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_note_is_refused_on_a_finished_run(client):
    response = client.post(f"/api/runs/{LEGACY_RUN}/note", json={"text": "too late now"})

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "run_not_active"
    assert response.json()["details"]["lifecycle"] == "completed"


def test_note_on_an_unknown_run_answers_the_error_envelope(client):
    response = client.post("/api/runs/does-not-exist/note", json={"text": "hi"})

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


# ---------------------------------------------------------------- archive hypothesis


def test_archive_hypothesis_marks_it_archived(client, store):
    run_id = _new_run(store, "run-archive-hyp")
    hyp = store.add_hypothesis(
        UUID(run_id), title="Something worth archiving", body_md="# Something", created_round=1
    )

    response = client.post(f"/api/runs/{run_id}/hypotheses/{hyp['hid']}/archive")

    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": True}
    rows = client.get(f"/api/runs/{run_id}/hypotheses").json()
    assert next(row for row in rows if row["hid"] == hyp["hid"])["status"] == "archived"


def test_archive_hypothesis_unknown_hid_answers_the_error_envelope(client, store):
    run_id = _new_run(store, "run-archive-hyp-missing")

    response = client.post(f"/api/runs/{run_id}/hypotheses/hNOPE/archive")

    assert response.status_code == 404
    assert response.json()["code"] == "hypothesis_not_found"


def test_archive_hypothesis_unknown_run_answers_the_error_envelope(client):
    response = client.post("/api/runs/does-not-exist/hypotheses/h001/archive")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


# -------------------------------------------------------------------------- context


def test_context_docs_report_name_and_char_count(client, store):
    run_id = _new_run(
        store,
        "run-context-docs",
        context_docs=[{"name": "brief.md", "text": "hello world"}],
    )

    response = client.get(f"/api/runs/{run_id}/context")

    assert response.status_code == 200
    # `delivered` rides along with the count, because the count on its own reads as "all of
    # this was used" and the run had not composed a prompt yet when this was asked.
    assert response.json() == [
        {
            "name": "brief.md",
            "chars": len("hello world"),
            "delivered": "pending",
            "cap_chars": None,
        }
    ]


def test_context_docs_unknown_run_answers_the_error_envelope(client):
    response = client.get("/api/runs/does-not-exist/context")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


# -------------------------------------------------------------------------- export


def test_export_md_downloads_a_markdown_report(client):
    response = client.get(f"/api/runs/{GRAFT_RUN}/export.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert GRAFT_RUN in disposition
    body = response.text
    assert body.startswith("# ")
    assert "## Ranked hypotheses" in body
    assert "Elo" in body


def test_export_md_top_limits_active_bodies_in_report_order(client, store):
    engine_run_id = "run-export-top-five"
    run_id = _new_run(store, engine_run_id)
    active = [
        store.add_hypothesis(
            UUID(run_id),
            title=f"Ranked idea {index}",
            body_md=f"Unique full body {index}",
            created_round=1,
            elo=elo,
        )
        for index, elo in enumerate((1280, 1410, 1330, 1500, 1410, 1190, 1370, 1250), 1)
    ]
    store.add_hypothesis(
        UUID(run_id),
        title="Rejected idea",
        body_md="Rejected full body must stay out",
        created_round=1,
        status="rejected",
        elo=1600,
    )

    whole = client.get(f"/api/runs/{run_id}/export.md")
    top_five = client.get(f"/api/runs/{run_id}/export.md", params={"top": 5})

    assert whole.status_code == 200, whole.text
    assert top_five.status_code == 200, top_five.text
    assert whole.text.count("Unique full body") == 8
    assert top_five.text.count("Unique full body") == 5
    ranked = sorted(active, key=lambda row: (-row["elo"], row["hid"]))
    headings = [f"### {row['title']} ({row['hid']})" for row in ranked]
    assert [heading in top_five.text for heading in headings] == [True] * 5 + [False] * 3
    assert [top_five.text.index(heading) for heading in headings[:5]] == sorted(
        top_five.text.index(heading) for heading in headings[:5]
    )
    assert "- Rejected idea (rejected)" in top_five.text
    assert "Rejected full body must stay out" not in top_five.text
    assert whole.headers["content-disposition"].endswith(f"-{engine_run_id}.md\"")
    assert top_five.headers["content-disposition"].endswith(f"-{engine_run_id}-top5.md\"")


def test_export_md_refuses_a_non_positive_top(client):
    response = client.get(f"/api/runs/{GRAFT_RUN}/export.md", params={"top": 0})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_export_md_names_rejected_hypotheses_without_their_body(client):
    response = client.get(f"/api/runs/{REJECTED_RUN}/export.md")

    assert response.status_code == 200
    assert "_No active hypotheses" in response.text
    assert "## Rejected & archived" in response.text


def test_export_md_unknown_run_answers_the_error_envelope(client):
    response = client.get("/api/runs/does-not-exist/export.md")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"

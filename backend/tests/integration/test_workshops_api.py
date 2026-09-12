"""`/api/workshops` — the four calls of plan C5, and the DTO they are frozen to."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.workshops import get_workshop_service
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.engine.runners import FakeRunner
from app.main import create_app
from app.services.workshop.service import WorkshopService
from tests.support.dbspy import no_writes
from tests.support.imported import fixture_settings

QUESTION = "Why do some shallow lakes bloom when nutrient loading is falling?"

# Plan C5 freezes these field lists. Wave 4 generates the frontend's types from them.
WORKSHOP_FIELDS = {"id", "question", "state", "harness", "options", "error", "created_at"}
OPTION_FIELDS = {
    "id",
    "ordinal",
    "prompt",
    "strategy",
    "optimizes_for",
    "excludes",
    "rationale",
    "recommended_settings",
    "chosen",
    "rejected",
    "note",
}
SETTINGS_FIELDS = {"rounds", "budget_calls", "matches_per_round", "grounding_depth"}


def build(runner: FakeRunner | None = None, *, timeout_s: float = 10.0):
    """A client whose workshops are answered by the fake, and the service behind it."""
    settings = fixture_settings()
    session_factory = get_session_factory(get_settings())
    with session_factory() as session:
        schema = session.execute(text("select current_schema()")).scalar_one()
        assert schema.startswith("test_"), f"refusing to truncate live schema {schema!r}"
        session.execute(text("TRUNCATE workshops, workshop_options CASCADE"))
        session.commit()

    service = WorkshopService(
        lambda harness: runner or FakeRunner(seed="workshops-api"),
        session_factory,
        timeout_s=timeout_s,
    )
    app = create_app(settings)
    app.dependency_overrides[get_workshop_service] = lambda: service
    return TestClient(app), service


@pytest.fixture
def client_and_service(isolated_schema):
    return build()


@pytest.fixture
def client(client_and_service):
    return client_and_service[0]


def settle(client_and_service, workshop_id: str) -> dict:
    client, service = client_and_service
    assert service.wait(uuid_of(workshop_id), timeout=20), "the workshop call never finished"
    response = client.get(f"/api/workshops/{workshop_id}")
    assert response.status_code == 200
    return response.json()


def uuid_of(workshop_id: str) -> UUID:
    return UUID(workshop_id)


def start(client, **overrides) -> dict:
    response = client.post("/api/workshops", json={"question": QUESTION, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def test_creating_a_workshop_answers_immediately_in_the_frozen_shape(client_and_service):
    client, _ = client_and_service
    workshop = start(client)

    assert set(workshop) == WORKSHOP_FIELDS
    assert workshop["state"] == "refining"
    assert workshop["harness"] == "claude"
    assert workshop["options"] == []
    assert workshop["error"] is None
    assert workshop["created_at"]


def test_the_options_arrive_with_settings_the_wizard_can_prefill(client_and_service):
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client)["id"])

    assert workshop["state"] == "options_ready"
    assert len(workshop["options"]) == 2
    for option in workshop["options"]:
        assert set(option) == OPTION_FIELDS
        assert set(option["recommended_settings"]) == SETTINGS_FIELDS
        assert option["recommended_settings"]["rounds"] >= 1
        assert option["chosen"] is False and option["rejected"] is False
    assert workshop["options"][0]["strategy"] != workshop["options"][1]["strategy"]


def test_a_demo_workshop_is_accepted_and_says_so(client_and_service):
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client, harness="demo")["id"])

    assert workshop["harness"] == "demo"
    assert workshop["state"] == "options_ready"


def test_context_documents_are_accepted_at_creation(client_and_service):
    client, _ = client_and_service
    created = start(
        client,
        context_docs=[{"name": "field-notes.md", "text": "Secchi depth fell in June."}],
    )

    assert settle(client_and_service, created["id"])["state"] == "options_ready"


def test_refine_marks_the_previous_pair_rejected_and_keeps_it(client_and_service):
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client)["id"])
    first = workshop["options"]

    response = client.post(
        f"/api/workshops/{workshop['id']}/refine",
        json={"base": first[0]["id"], "note": "Stay in the sediment."},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "refining"

    refined = settle(client_and_service, workshop["id"])
    assert [option["ordinal"] for option in refined["options"]] == [0, 1, 2, 3]
    assert [option["rejected"] for option in refined["options"]] == [True, True, False, False]
    assert [option["note"] for option in refined["options"][:2]] == ["Stay in the sediment."] * 2
    assert [option["prompt"] for option in refined["options"][:2]] == [
        option["prompt"] for option in first
    ]


def test_merge_refines_without_naming_an_option(client_and_service):
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client)["id"])

    response = client.post(
        f"/api/workshops/{workshop['id']}/refine", json={"base": "merge", "note": ""}
    )

    assert response.status_code == 200
    assert settle(client_and_service, workshop["id"])["state"] == "options_ready"


def test_choose_returns_the_edited_prompt_and_stores_it_verbatim(client_and_service):
    """A prompt containing `[1]` or `[source]` is a normal prompt, not a template."""
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client)["id"])
    option = workshop["options"][1]
    edited = (
        "Explain summer blooms under falling external load [1].\n\n"
        "Treat internal loading as the null hypothesis [source].\n\n"
        "  Indentation and blank lines are part of the prompt."
    )

    response = client.post(
        f"/api/workshops/{workshop['id']}/choose",
        json={"option_id": option["id"], "final_prompt": edited},
    )

    assert response.status_code == 200
    assert response.json() == {"prompt": edited}

    after = settle(client_and_service, workshop["id"])
    assert after["state"] == "chosen"
    chosen = next(row for row in after["options"] if row["id"] == option["id"])
    assert chosen["prompt"] == edited
    assert chosen["chosen"] is True


def test_reading_a_workshop_twice_writes_nothing(client_and_service):
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client)["id"])
    before = _updated_at(workshop["id"])

    with no_writes() as flushed:
        first = client.get(f"/api/workshops/{workshop['id']}").json()
        second = client.get(f"/api/workshops/{workshop['id']}").json()

    assert flushed == []
    assert first == second == workshop
    assert _updated_at(workshop["id"]) == before


def test_an_unknown_workshop_is_a_named_404(client):
    response = client.get(f"/api/workshops/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "workshop_not_found"
    assert response.json()["message"]


def test_choosing_an_option_that_is_not_on_this_workshop_is_a_named_404(client_and_service):
    client, _ = client_and_service
    workshop = settle(client_and_service, start(client)["id"])

    response = client.post(
        f"/api/workshops/{workshop['id']}/choose",
        json={"option_id": str(uuid4()), "final_prompt": ""},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "workshop_option_not_found"


def test_refining_while_the_call_is_still_running_is_a_409_that_says_the_state(isolated_schema):
    client, service = build(FakeRunner(latency=2.0))
    workshop = start(client)

    response = client.post(f"/api/workshops/{workshop['id']}/refine", json={"base": "merge"})

    assert response.status_code == 409
    assert response.json()["code"] == "workshop_not_ready"
    assert response.json()["details"]["state"] == "refining"
    assert service.wait(uuid_of(workshop["id"]), timeout=20)


def test_a_question_too_short_to_work_with_is_refused_before_any_row_exists(client_and_service):
    client, _ = client_and_service

    response = client.post("/api/workshops", json={"question": "lakes"})

    assert response.status_code == 422
    assert response.json()["code"] == "question_too_short"
    assert _workshop_count() == 0


def test_a_failed_call_is_diagnosable_from_the_dto(isolated_schema):
    client, service = build(FakeRunner(failures={("workshop", None): "claude exited 1"}))
    workshop = start(client)
    assert service.wait(uuid_of(workshop["id"]), timeout=20)

    payload = client.get(f"/api/workshops/{workshop['id']}").json()

    assert payload["state"] == "failed"
    assert payload["error"]["code"] == "workshop_call_failed"
    assert "claude exited 1" in payload["error"]["message"]


def _updated_at(workshop_id: str):
    with get_session_factory(get_settings())() as session:
        return session.execute(
            text("select updated_at from workshops where id = :id"),
            {"id": uuid_of(workshop_id)},
        ).scalar_one()


def _workshop_count() -> int:
    with get_session_factory(get_settings())() as session:
        return session.execute(text("select count(*) from workshops")).scalar_one()

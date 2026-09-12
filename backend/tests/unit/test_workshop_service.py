"""The workshop service: one bounded call, a state machine, and reads that never write."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.engine.runners import Failure, FakeRunner, RoleConfig, RoleResult
from app.engine.schemas import WORKSHOP_SCHEMA
from app.services.workshop.prompts import WORKSHOP_SYSTEM_PROMPT
from app.services.workshop.service import (
    OptionNotFound,
    WorkshopInputError,
    WorkshopNotFound,
    WorkshopService,
    WorkshopStateError,
)
from tests.fixtures.workshop_c4566ed2 import QUESTION as OWNER_QUESTION
from tests.support.dbspy import no_writes

QUESTION = "Why do some shallow lakes bloom when nutrient loading is falling?"

RECOMMENDED_KEYS = {"rounds", "budget_calls", "matches_per_round", "grounding_depth"}


class Recorder:
    """A `FakeRunner` that keeps every config it was handed, and counts its own closes."""

    name = "recorder"

    def __init__(self, inner: FakeRunner, payload: dict[str, Any] | None = None) -> None:
        self.inner = inner
        self.payload = payload
        self.configs: list[RoleConfig] = []
        self.closed = 0

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.inner.calls

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        self.configs.append(cfg)
        result = await self.inner.run_role(role, prompt, cfg)
        return result if self.payload is None else replace(result, data=self.payload)

    async def probe(self) -> dict[str, Any]:
        return await self.inner.probe()

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture(scope="module")
def session_factory(isolated_schema):
    return get_session_factory(get_settings())


@pytest.fixture
def clean(session_factory):
    with session_factory() as session:
        schema = session.execute(text("select current_schema()")).scalar_one()
        assert schema.startswith("test_"), f"refusing to truncate live schema {schema!r}"
        session.execute(text("TRUNCATE workshops, workshop_options CASCADE"))
        session.commit()
    return session_factory


def make_service(
    session_factory,
    *,
    runner: FakeRunner | None = None,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
) -> tuple[WorkshopService, Recorder]:
    recorder = Recorder(runner or FakeRunner(seed="workshop-tests"), payload)
    service = WorkshopService(
        lambda harness: recorder, session_factory, timeout_s=timeout_s
    )
    return service, recorder


@pytest.fixture
def service(clean) -> WorkshopService:
    built, _ = make_service(clean)
    return built


def settled(service: WorkshopService, workshop_id: UUID) -> dict[str, Any]:
    assert service.wait(workshop_id, timeout=20), "the workshop call never finished"
    return service.get(workshop_id)


def updated_at(session_factory, workshop_id: UUID):
    with session_factory() as session:
        return session.execute(
            text("select updated_at from workshops where id = :id"), {"id": workshop_id}
        ).scalar_one()


# --------------------------------------------------------------------------- create


def test_create_returns_immediately_in_refining(service):
    workshop = service.create(QUESTION)

    assert workshop["state"] == "refining"
    assert workshop["options"] == []
    assert workshop["question"] == QUESTION
    assert workshop["harness"] == "claude"
    assert workshop["error"] is None


def test_create_produces_two_options_each_with_recommended_settings(service):
    created = service.create(QUESTION)
    workshop = settled(service, UUID(created["id"]))

    assert workshop["state"] == "options_ready"
    assert [option["ordinal"] for option in workshop["options"]] == [0, 1]
    for option in workshop["options"]:
        assert option["prompt"].strip()
        assert RECOMMENDED_KEYS <= set(option["recommended_settings"])
        assert option["recommended_settings"]["grounding_depth"] in {
            "shallow",
            "standard",
            "deep",
        }
        assert option["chosen"] is False
        assert option["rejected"] is False
        assert option["note"] is None
    strategies = {option["strategy"] for option in workshop["options"]}
    assert len(strategies) == 2, "the two options must offer different strategies"


def test_the_call_is_grounded_schema_enforced_and_released(clean):
    service, recorder = make_service(clean)
    created = service.create(QUESTION)
    settled(service, UUID(created["id"]))

    assert len(recorder.configs) == 1, "a workshop is exactly one call"
    cfg = recorder.configs[0]
    assert cfg.role == "workshop"
    assert cfg.model == "claude-opus-5"
    assert cfg.tools == "WebSearch"
    assert cfg.json_schema is WORKSHOP_SCHEMA
    assert cfg.system_prompt == WORKSHOP_SYSTEM_PROMPT
    assert cfg.timeout_s == 10.0
    assert QUESTION in recorder.calls[0]["prompt"]
    assert recorder.closed == 1


def test_context_documents_reach_the_prompt(clean):
    service, recorder = make_service(clean)
    created = service.create(
        QUESTION,
        context_docs=[{"name": "field-notes.md", "text": "Secchi depth fell in June."}],
    )
    settled(service, UUID(created["id"]))

    prompt = recorder.calls[0]["prompt"]
    assert "field-notes.md" in prompt
    assert "Secchi depth fell in June." in prompt


def test_a_wide_question_reaches_the_model_demanding_a_wide_option(clean):
    """Run `c4566ed2`, end to end through the service: the defect was in what we sent.

    The engine never saw the owner's brief — it saw the workshop's rewrite, which named six
    families of answer and got six hypotheses back, one per family. The call that produced
    that rewrite carried no instruction to stay wide; this one does, and it is the whole
    fix. Asserted on the prompt because the prompt is the only thing this side controls.
    """
    service, recorder = make_service(clean)
    created = service.create(OWNER_QUESTION)
    workshop = settled(service, UUID(created["id"]))

    sent = " ".join(recorder.calls[0]["prompt"].split()).lower()
    assert "breadth of this question: open" in sent
    assert "one of the two options must be the wide one" in sent
    assert "neither goal may name the candidate answers" in sent
    assert "carry forward only the exclusions the scientist wrote" in sent
    assert workshop["state"] == "options_ready"


def test_a_narrow_question_is_not_sent_the_wide_option_requirement(clean):
    """The lake question is a mechanism hunt; forcing half the A/B wide would waste it."""
    service, recorder = make_service(clean)
    settled(service, UUID(service.create(QUESTION)["id"]))

    sent = " ".join(recorder.calls[0]["prompt"].split()).lower()
    assert "breadth of this question: focused" in sent
    assert "one of the two options must be the wide one" not in sent
    # The rule that actually caused the collapse applies to both regimes.
    assert "never the list of candidate answers" in sent


def test_the_two_options_reach_the_wizard_differing_on_breadth(clean):
    """Both halves of the A/B survive storage and read-back, breadth distinction intact."""
    service, _ = make_service(clean)
    workshop = settled(service, UUID(service.create(OWNER_QUESTION)["id"]))

    strategies = [option["strategy"].lower() for option in workshop["options"]]
    assert len(set(strategies)) == 2
    assert any("breadth" in strategy or "wide" in strategy for strategy in strategies)


def test_a_question_too_short_to_work_with_is_refused(service):
    with pytest.raises(WorkshopInputError) as caught:
        service.create("lakes")

    assert caught.value.code == "question_too_short"


def test_an_unknown_harness_is_refused(service):
    with pytest.raises(WorkshopInputError) as caught:
        service.create(QUESTION, harness="codex")

    assert caught.value.code == "unknown_harness"


# --------------------------------------------------------------------------- refine


def test_refine_rejects_the_previous_pair_and_keeps_it_as_history(clean):
    service, _ = make_service(clean)
    workshop_id = UUID(service.create(QUESTION)["id"])
    first = settled(service, workshop_id)
    base = first["options"][0]

    service.refine(workshop_id, base=base["id"], note="Stay in the sediment, not the water column.")
    workshop = settled(service, workshop_id)

    assert workshop["state"] == "options_ready"
    assert [option["ordinal"] for option in workshop["options"]] == [0, 1, 2, 3]
    old, new = workshop["options"][:2], workshop["options"][2:]
    assert [option["rejected"] for option in old] == [True, True]
    assert [option["note"] for option in old] == [
        "Stay in the sediment, not the water column."
    ] * 2
    assert [option["rejected"] for option in new] == [False, False]
    assert [option["prompt"] for option in old] != [option["prompt"] for option in new]
    # The rejected pair survives verbatim: the wizard's "rejected directions" drawer is the
    # only record of what the scientist has already turned down.
    assert [option["prompt"] for option in old] == [option["prompt"] for option in first["options"]]


def test_the_refine_call_carries_the_note_the_base_and_the_rejected_directions(clean):
    service, recorder = make_service(clean)
    workshop_id = UUID(service.create(QUESTION)["id"])
    first = settled(service, workshop_id)
    base = first["options"][0]
    other = first["options"][1]

    service.refine(workshop_id, base=base["id"], note="Focus on internal phosphorus loading.")
    settled(service, workshop_id)

    prompt = recorder.calls[1]["prompt"]
    assert "Focus on internal phosphorus loading." in prompt
    assert "SCIENTIST NOTE" in prompt
    assert base["strategy"] in prompt
    assert "REJECTED DIRECTIONS" in prompt
    assert other["prompt"][:120] in prompt


def test_merge_asks_for_the_strengths_of_both(clean):
    service, recorder = make_service(clean)
    workshop_id = UUID(service.create(QUESTION)["id"])
    settled(service, workshop_id)

    service.refine(workshop_id, base="merge", note="")
    workshop = settled(service, workshop_id)

    assert workshop["state"] == "options_ready"
    assert "BASE: merge" in recorder.calls[1]["prompt"]


def test_refine_needs_options_to_refine(clean):
    """A call is already in flight; asking for another one is a state error, not a queue."""
    service, _ = make_service(clean, runner=FakeRunner(latency=2.0))
    workshop_id = UUID(service.create(QUESTION)["id"])

    with pytest.raises(WorkshopStateError) as caught:
        service.refine(workshop_id, base="merge")

    assert caught.value.state == "refining"
    settled(service, workshop_id)


def test_refine_refuses_an_option_from_another_workshop(service):
    first = UUID(service.create(QUESTION)["id"])
    second = UUID(service.create("How does lake ice cover change spring turnover?")["id"])
    settled(service, first)
    foreign = settled(service, second)["options"][0]["id"]

    with pytest.raises(OptionNotFound):
        service.refine(first, base=foreign)


# --------------------------------------------------------------------------- choose


def test_choose_stores_the_edited_prompt_verbatim(service):
    """Brackets are not placeholders.

    The old workshop scanned the final prompt for `[...]` and rejected it as an unfilled
    template, which made it impossible to submit a prompt that cited anything.
    """
    workshop_id = UUID(service.create(QUESTION)["id"])
    workshop = settled(service, workshop_id)
    option = workshop["options"][1]
    edited = (
        "Explain the summer blooms in shallow lakes under falling external load [1].\n\n"
        "Treat internal loading as the null hypothesis [source]: a hypothesis must say "
        "what would distinguish it from sediment release.\n\n"
        "  Keep the indentation and the blank lines exactly as written."
    )

    chosen = service.choose(workshop_id, option_id=option["id"], final_prompt=edited)

    assert chosen == {"prompt": edited}
    after = service.get(workshop_id)
    assert after["state"] == "chosen"
    stored = next(row for row in after["options"] if row["id"] == option["id"])
    assert stored["prompt"] == edited
    assert stored["chosen"] is True
    assert [row["chosen"] for row in after["options"]] == [False, True]


def test_choosing_without_editing_keeps_the_option_as_written(service):
    workshop_id = UUID(service.create(QUESTION)["id"])
    workshop = settled(service, workshop_id)
    option = workshop["options"][0]

    chosen = service.choose(workshop_id, option_id=option["id"], final_prompt="   ")

    assert chosen["prompt"] == option["prompt"]


def test_a_chosen_workshop_cannot_be_chosen_from_again(service):
    workshop_id = UUID(service.create(QUESTION)["id"])
    option = settled(service, workshop_id)["options"][0]
    service.choose(workshop_id, option_id=option["id"])

    with pytest.raises(WorkshopStateError) as caught:
        service.choose(workshop_id, option_id=option["id"])

    assert caught.value.state == "chosen"


def test_choose_refuses_a_rejected_option(service):
    workshop_id = UUID(service.create(QUESTION)["id"])
    rejected = settled(service, workshop_id)["options"][0]["id"]
    service.refine(workshop_id, base="merge")
    settled(service, workshop_id)

    with pytest.raises(OptionNotFound):
        service.choose(workshop_id, option_id=rejected)


def test_an_unknown_workshop_is_not_found(service):
    with pytest.raises(WorkshopNotFound):
        service.get(uuid4())


# --------------------------------------------------------------------------- failure


def test_a_timeout_marks_the_workshop_failed(clean):
    service, _ = make_service(
        clean, runner=FakeRunner(seed="slow", latency=1.0), timeout_s=0.05
    )
    workshop_id = UUID(service.create(QUESTION)["id"])

    workshop = settled(service, workshop_id)

    assert workshop["state"] == "failed"
    assert workshop["options"] == []
    assert workshop["error"]["code"] == "workshop_timeout"
    assert "0.05 seconds" in workshop["error"]["message"]


def test_a_failed_call_says_why_in_the_dto(clean):
    service, _ = make_service(
        clean,
        runner=FakeRunner(failures={("workshop", None): "claude exited 1: OAuth expired"}),
    )
    workshop_id = UUID(service.create(QUESTION)["id"])

    workshop = settled(service, workshop_id)

    assert workshop["state"] == "failed"
    assert workshop["error"]["code"] == "workshop_call_failed"
    assert "OAuth expired" in workshop["error"]["message"]
    assert workshop["error"]["detail"]


def test_output_that_misses_the_schema_is_a_contract_violation(clean):
    service, _ = make_service(
        clean, runner=FakeRunner(failures={("workshop", None): Failure(malformed=True)})
    )
    workshop_id = UUID(service.create(QUESTION)["id"])

    workshop = settled(service, workshop_id)

    assert workshop["state"] == "failed"
    assert workshop["error"]["code"] == "contract_violation"
    assert "options" in workshop["error"]["message"]


def test_an_option_without_recommended_settings_never_reaches_the_wizard(clean):
    """The launch step prefills from these; an option that omits them is not usable.

    Plan C2 makes `recommended_settings` required on every option for exactly this reason —
    the previous UI showed a form of defaults beside a workshop recommending something
    else entirely.
    """
    option = {
        "prompt": "Explain the blooms.",
        "strategy": "Mechanism-first",
        "optimizes_for": "depth",
        "excludes": "breadth",
        "rationale": "Narrow goals produce testable hypotheses.",
    }
    service, _ = make_service(clean, payload={"options": [option, option]})
    workshop_id = UUID(service.create(QUESTION)["id"])

    workshop = settled(service, workshop_id)

    assert workshop["state"] == "failed"
    assert workshop["error"]["code"] == "contract_violation"
    assert "recommended_settings" in workshop["error"]["message"]


def test_a_failed_workshop_can_be_read_but_not_refined(clean):
    service, _ = make_service(clean, runner=FakeRunner(failures={("workshop", None): "boom"}))
    workshop_id = UUID(service.create(QUESTION)["id"])
    settled(service, workshop_id)

    with pytest.raises(WorkshopStateError) as caught:
        service.refine(workshop_id, base="merge")

    assert caught.value.state == "failed"


# ------------------------------------------------------------------------ idempotence


def test_reading_a_workshop_never_writes(clean):
    """The old endpoint reconciled state on read; two GETs could disagree."""
    service, _ = make_service(clean)
    workshop_id = UUID(service.create(QUESTION)["id"])
    settled(service, workshop_id)
    before = updated_at(clean, workshop_id)

    with no_writes() as flushed:
        first = service.get(workshop_id)
        second = service.get(workshop_id)

    assert flushed == []
    assert first == second
    assert updated_at(clean, workshop_id) == before


def test_reading_a_workshop_still_refining_never_writes(clean):
    """Not even the in-flight case: the thread owns the outcome, the reader owns nothing."""
    service, _ = make_service(clean, runner=FakeRunner(latency=2.0))
    workshop_id = UUID(service.create(QUESTION)["id"])

    with no_writes() as flushed:
        assert service.get(workshop_id)["state"] == "refining"
        assert service.get(workshop_id)["state"] == "refining"

    assert flushed == []
    assert settled(service, workshop_id)["state"] == "options_ready"

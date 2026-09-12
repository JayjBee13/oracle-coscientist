""""Run again": what `POST /runs {from_run}` reproduces, and what it must not.

A clone copies a *setup*. The failure this file exists to prevent is the quiet one: a clone
that looks like it worked, ran to completion, and answered the same question with a different
provider, a different tier, a different per-role table and a different number of rounds —
because it silently landed on whatever the top bar happened to say the day it was launched
rather than on what the run it names actually used. Nothing on either run's screen would
explain the difference, and the two would then be compared against each other.

So every test here plants a stored system default that is **deliberately different** from the
source run. A clone that matched the source by accident — because the default happened to
agree with it — would prove nothing.

The whole launch path is exercised except the spawn: the request body goes through
`CreateRunRequest`, whose `config_document()` decides what counts as "stated", then through
`_inherit`, which merges it over the source run's stored config, then through
`resolve_config`, which freezes the model table. Starting an actual supervisor would add
minutes and test the operating system instead.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete

from app.core.config import Settings, get_settings
from app.db.engine_models import AppSetting
from app.db.session import get_session_factory
from app.engine.store import RunStore
from app.schemas.launch import CreateRunRequest
from app.services.runs.launcher import _align_harness, _inherit, resolve_config
from app.services.settings.models import store_model_settings, stored_model_settings

# The source run's setup, and the stored default it must not be confused with. Every field
# differs: provider, tier, overrides, and — through them — the whole frozen model table.
SOURCE_SETUP: dict[str, Any] = {
    "provider": "openai",
    "model_tier": "med",
    "model_overrides": {"ranking": {"model": "gpt-5.6-luna", "effort": "xhigh"}},
    "rounds": 7,
    "generation_batch": 4,
    "budget_calls": 200,
}
PLANTED_DEFAULT: dict[str, Any] = {"provider": "anthropic", "tier": "max", "overrides": {}}


@pytest.fixture(autouse=True)
def planted_default():
    """A stored system default that agrees with the source run about nothing.

    Deleted on the way in as well as on the way out: this row is global, and a test that
    inherited another file's default would be asserting against a value it did not choose.
    """
    factory = get_session_factory(get_settings())
    with factory() as session:
        session.execute(delete(AppSetting))
        session.commit()
    store_model_settings(PLANTED_DEFAULT)
    yield stored_model_settings()
    with factory() as session:
        session.execute(delete(AppSetting))
        session.commit()


@pytest.fixture
def store() -> RunStore:
    return RunStore(settings=Settings(APP_ENV="test"))


def make_source(store: RunStore, engine_run_id: str, **config: Any) -> tuple[UUID, dict]:
    """A completed run whose config was frozen the way a real launch freezes it."""
    document = resolve_config({**SOURCE_SETUP, **config})
    document["runner"] = _align_harness("claude", document)
    run_id = store.create_run(
        question="Why do some lakes bloom under falling nutrient loads?",
        prompt="Why do some lakes bloom under falling nutrient loads?",
        harness=document["runner"],
        config=document,
        engine_run_id=engine_run_id,
        lifecycle="completed",
        root_path="",
    )
    return run_id, document


def clone(store: RunStore, source_id: UUID, body: dict[str, Any] | None = None) -> dict:
    """Everything `launch_run` decides about a clone's config, short of the spawn."""
    request = CreateRunRequest.model_validate({"from_run": str(source_id), **(body or {})})
    _question, _prompt, config, _docs = _inherit(
        store, str(source_id), None, None, request.config_document(), None
    )
    document = resolve_config(config)
    document["harness"] = _align_harness(request.harness, document)
    return document


def test_a_clone_that_states_no_config_reproduces_the_source_run(store: RunStore):
    """The point of "Run again": a second independent sample of *that* run, not a new run
    wearing its question. A request that says nothing about the config must therefore change
    nothing about it — and the request body cannot help saying nothing, because the wizard's
    config block has a default for every field it holds."""
    source_id, source = make_source(store, "run-clone-bare")

    document = clone(store, source_id)

    assert document["provider"] == source["provider"] == "openai"
    assert document["model_tier"] == source["model_tier"] == "med"
    assert document["model_overrides"] == source["model_overrides"]
    assert document["rounds"] == source["rounds"] == 7
    assert document["generation_batch"] == source["generation_batch"] == 4
    assert document["budget_calls"] == source["budget_calls"] == 200

    # The table is re-resolved rather than copied, so this is the interesting assertion:
    # re-resolving from the *source's* three inputs lands on the source's table, and the
    # planted default's table is a different one entirely.
    assert document["model_table"] == source["model_table"]
    assert document["model_table"] != stored_model_settings().table()

    # And the lane follows the provider, so a clone of a Codex run does not go and contend
    # for the Claude CLI.
    assert document["harness"] == source["runner"] == "codex"


def test_a_clone_that_states_a_field_overrides_that_field_and_no_other(store: RunStore):
    """"Same run but three rounds instead of seven" is one field, not a re-entered form."""
    source_id, source = make_source(store, "run-clone-one-field")

    document = clone(store, source_id, {"config": {"rounds": 3}})

    assert document["rounds"] == 3, "the caller wins on what they stated"
    assert document["provider"] == source["provider"]
    assert document["model_tier"] == source["model_tier"]
    assert document["model_overrides"] == source["model_overrides"]
    assert document["budget_calls"] == source["budget_calls"]
    assert document["model_table"] == source["model_table"]


def test_a_clone_may_move_the_whole_table_to_the_other_provider(store: RunStore):
    """The other half of the same contract: a stated field really does override, including
    the ones a `null` abstains on."""
    source_id, source = make_source(store, "run-clone-switch")

    document = clone(store, source_id, {"config": {"provider": "anthropic"}})

    assert document["provider"] == "anthropic"
    assert document["harness"] == "claude"
    assert document["model_tier"] == source["model_tier"], "still the source's tier"
    assert document["model_table"] != source["model_table"]


def test_the_wizards_whole_draft_still_reproduces_the_source_run(store: RunStore):
    """The shape the UI actually posts, which is not the shape the API minimally accepts.

    `NewRunPage` sends its entire config object on every launch, so a clone arrives with every
    field explicitly stated. Nothing about *this* body needs the fix — a request that states
    the source's provider gets the source's provider under any version of `_inherit`. That is
    the point: it pins the far end of the frontend's half of the repair (`normaliseConfig`
    now carries `provider` out of the source run's config and into the draft), so a client
    change that stopped doing so fails here rather than launching the wrong vendor quietly."""
    source_id, source = make_source(store, "run-clone-wizard")
    draft = {key: source[key] for key in ("rounds", "generation_batch", "matches_per_round",
                                          "evolve_top_k", "budget_calls", "budget_usd",
                                          "wall_clock_minutes", "grounding_depth", "graft",
                                          "model_tier", "model_overrides", "runner")}
    draft["provider"] = source["provider"]

    document = clone(store, source_id, {"config": draft})

    assert document["provider"] == "openai"
    assert document["model_tier"] == "med"
    assert document["model_overrides"] == source["model_overrides"]
    assert document["rounds"] == 7
    assert document["model_table"] == source["model_table"]


def test_a_clone_of_a_run_written_before_providers_existed_stays_on_anthropic(store: RunStore):
    """Those configs have no `provider` key, and the answer is not "ask the top bar".

    Every one of them ran on Anthropic — the field did not exist yet — so that is what a clone
    reproduces, whatever the stored default says today. The wizard makes this the ordinary
    case rather than a curiosity: a draft cloned from such a run has nothing to put in the
    field, so it posts `provider: null`, and a `null` that erased what was inherited would
    land the clone on the current default and move it into the other CLI's lane.

    The stored default is planted as `openai` here precisely so that a clone landing on it
    would be visible: `anthropic` is also the *built-in* default, so a test run against the
    built-in one would pass for the wrong reason.
    """
    store_model_settings({"provider": "openai", "tier": "med", "overrides": {}})
    legacy = {
        key: value
        for key, value in resolve_config({"model_tier": "high", "rounds": 4}).items()
        if key not in ("provider", "model_table")
    }
    source_id = store.create_run(
        question="A question from before the field existed",
        prompt="A prompt from before the field existed",
        harness="claude",
        config=legacy,
        engine_run_id="run-clone-preprovider",
        lifecycle="completed",
        root_path="",
    )

    # The shape the wizard posts for such a run: a tier it could read off the stored config,
    # and the `null` provider its blank draft starts with.
    document = clone(store, source_id, {"config": {"provider": None, "model_tier": "high"}})

    assert stored_model_settings().provider == "openai"
    assert document["provider"] == "anthropic", "what that run used, not what a new run gets"
    assert document["model_tier"] == "high"
    assert document["rounds"] == 4
    assert document["harness"] == "claude"


def test_config_document_reports_only_the_fields_the_caller_sent():
    """The distinction the whole file rests on. JSON cannot say "I did not mention this"
    other than by not mentioning it, and Pydantic records exactly that — so the dump has to
    respect it, or `_inherit` is handed a full config it cannot tell from a stated one."""
    assert CreateRunRequest.model_validate({"from_run": "x"}).config_document() == {}
    assert CreateRunRequest.model_validate(
        {"from_run": "x", "config": {"rounds": 3}}
    ).config_document() == {"rounds": 3}
    # An explicitly-sent `null` is still *sent*, and stays in the document: on `budget_usd` it
    # is a request in its own right, and only `_inherit` knows which fields abstain.
    assert CreateRunRequest.model_validate(
        {"from_run": "x", "config": {"budget_usd": None}}
    ).config_document() == {"budget_usd": None}

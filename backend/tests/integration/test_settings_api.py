"""`GET`/`PUT /api/settings/models`, and the promise it makes to everything downstream.

The value of a persisted default is entirely in whether the rest of the system honours it,
so most of this file is not about the endpoint: it is about a run launched afterwards, a
workshop opened afterwards, and the capabilities payload the wizard reads — all agreeing with
what was saved. A settings endpoint that round-trips its own JSON and changes nothing else
would pass a narrower test and be worthless.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import Settings, get_settings
from app.db.engine_models import AppSetting
from app.db.session import get_session_factory
from app.engine.models import EFFORTS, MODEL_TRAPS
from app.engine.runners import MODEL_TIERS, ROLES
from app.main import create_app
from app.services.runs.launcher import resolve_config
from app.services.settings.models import (
    BUILT_IN,
    ModelPolicyError,
    store_model_settings,
    stored_model_settings,
    validate_model_settings,
)


@pytest.fixture(autouse=True)
def clean_settings():
    """No stored default before or after: these tests are about what saving one does."""
    factory = get_session_factory(get_settings())
    with factory() as session:
        session.execute(delete(AppSetting))
        session.commit()
    yield
    with factory() as session:
        session.execute(delete(AppSetting))
        session.commit()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(APP_ENV="test")))


def stated(bucket: dict) -> dict:
    """One bucket, with the fields nobody stated dropped.

    An override carries a model and an effort and either may be absent, so the wire shape
    (`ModelOverrideIn`) spells the absent one as `null`. That is noise in an assertion about
    what somebody actually chose, and it is the same noise in `overrides` and in `tiers` —
    dropping it here rather than per assertion keeps the two comparable.
    """
    return {
        role: {field: value for field, value in row.items() if value is not None}
        for role, row in bucket.items()
    }


# --- the endpoint --------------------------------------------------------------------------


def test_before_anything_is_saved_the_built_in_default_is_served_as_such(client: TestClient):
    """Not an error and not an empty body: the built-in default is a real, runnable table —
    the one every run used before this setting existed — and `source` is how a UI tells "this
    is what the system does" from "this is what somebody chose"."""
    payload = client.get("/api/settings/models").json()

    assert payload["source"] == "built_in"
    assert payload["provider"] == BUILT_IN.provider
    assert payload["tier"] == BUILT_IN.tier
    assert [row["role"] for row in payload["table"]] == list(ROLES)


def test_saving_a_default_round_trips_and_reports_the_table_it_resolves_to(client: TestClient):
    response = client.put(
        "/api/settings/models",
        json={"provider": "openai", "tier": "high", "overrides": {}},
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["source"] == "stored"
    assert saved["updated_at"]
    table = {row["role"]: row for row in saved["table"]}
    assert table["generation"]["model"] == "gpt-5.6-sol"
    assert table["proximity"]["model"] == "gpt-5.6-luna"
    assert client.get("/api/settings/models").json()["tier"] == "high"


def test_the_resolved_table_comes_back_rather_than_being_derived_twice(client: TestClient):
    """Both the editor and the wizard render a table. Deriving it in two clients from
    provider/tier/overrides is how two screens come to show different tables for one
    setting."""
    saved = client.put(
        "/api/settings/models",
        json={
            "provider": "anthropic",
            "tier": "med",
            "overrides": {"ranking": {"model": "gpt-5.6-sol", "effort": "xhigh"}},
        },
    ).json()

    table = {row["role"]: row for row in saved["table"]}
    assert table["ranking"]["model"] == "gpt-5.6-sol", "a row may name the other provider"
    assert table["ranking"]["effort"] == "xhigh"
    assert table["generation"]["effort"] == "low", "the rest of the med tier is unaffected"
    assert table["generation"]["note"], "the reason each row is what it is comes with it"


# --- the refusals, and the sentence each one comes back with ---------------------------------
#
# Every one of these asserts the *message*, not only the status. A 422 saying "Input should be
# 'claude-opus-5', 'fable', 'gpt-5.6-luna' or 'gpt-5.6-sol'" is not a worse-worded version of
# these refusals, it is a different answer: it names the four values that are allowed and says
# nothing about why the one that was asked for is not among them. `MODEL_TRAPS` exists because
# for `claude-fable-5` that "why" — the CLI accepts it and silently runs Opus 5 — is the entire
# content of the error, and it cannot be recovered from a list of alternatives.


def test_a_default_that_could_not_become_a_run_is_refused_when_it_is_set(client: TestClient):
    """The whole reason `PUT` validates by resolving. Refusing here means the trap is named
    at the moment somebody sets it, rather than by a run made three weeks later and judged by
    the wrong model."""
    response = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "max",
              "overrides": {"ranking": {"model": "claude-fable-5"}}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert MODEL_TRAPS["claude-fable-5"] in body["message"], (
        "the trap itself has to reach the client — a generic 'not one of the allowed "
        "values' does not tell anybody that the CLI runs Opus 5 for this id"
    )
    assert client.get("/api/settings/models").json()["source"] == "built_in"


def test_a_model_below_the_floor_is_refused_by_name_with_the_allowlist(client: TestClient):
    """No trap entry, so the generic half of the refusal has to carry it: which model was
    refused, and what may be chosen instead."""
    response = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "max",
              "overrides": {"generation": {"model": "claude-sonnet-5"}}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert "claude-sonnet-5" in body["message"], "the refusal names what was asked for"
    assert "is not allowed" in body["message"]
    assert "'fable'" in body["message"], "and what may be chosen instead"


def test_a_tier_this_build_does_not_have_is_refused(client: TestClient):
    response = client.put(
        "/api/settings/models", json={"provider": "anthropic", "tier": "cheapest"}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert "cheapest" in body["message"]
    assert ", ".join(MODEL_TIERS) in body["message"], "the tiers this build actually has"


def test_an_unknown_role_in_the_overrides_is_refused_rather_than_dropped(client: TestClient):
    """A typo'd role is a request that did not do what its author meant. A saved default that
    silently ignored half of it is worse than a rejected one — and the message has to name the
    key, because "generatio" and "generation" differ by one character on screen."""
    response = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "max",
              "overrides": {"generatio": {"effort": "low"}}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert "generatio" in body["message"]
    for role in ROLES:
        assert role in body["message"], "every valid role is listed, so the typo is visible"


def test_an_effort_the_ladder_does_not_have_is_refused_with_the_rungs_named(
    client: TestClient,
):
    """`ultra` is in Sol's CLI catalog and in no API enum — sending it is a 400 mid-run. The
    refusal has to say which rungs exist rather than only that this one does not."""
    response = client.put(
        "/api/settings/models",
        json={"provider": "openai", "tier": "max",
              "overrides": {"ranking": {"effort": "ultra"}}},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert "ultra" in body["message"]
    assert ", ".join(EFFORTS) in body["message"]


def test_a_provider_this_build_cannot_run_is_refused_by_name(client: TestClient):
    response = client.put("/api/settings/models", json={"provider": "google", "tier": "max"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert "google" in body["message"]
    assert "anthropic, openai" in body["message"], "the providers this build has a runner for"


def test_a_refused_save_leaves_the_stored_default_exactly_as_it_was(client: TestClient):
    """A refusal is not a partial write. The six bodies below fail at six different layers —
    provider, tier, tier key, role key, model, effort — and none of them may reach the row."""
    good = {"provider": "openai", "tier": "high",
            "tiers": {"high": {"evolution": {"effort": "max"}}}}
    client.put("/api/settings/models", json=good)
    before = client.get("/api/settings/models").json()

    for bad in (
        {"provider": "google", "tier": "max"},
        {"provider": "anthropic", "tier": "cheapest"},
        {"provider": "anthropic", "tier": "max", "tiers": {"cheapest": {}}},
        {"provider": "anthropic", "tier": "max", "overrides": {"generatio": {"effort": "low"}}},
        {"provider": "anthropic", "tier": "max",
         "overrides": {"ranking": {"model": "claude-fable-5"}}},
        {"provider": "anthropic", "tier": "max", "overrides": {"ranking": {"effort": "ultra"}}},
    ):
        assert client.put("/api/settings/models", json=bad).status_code == 400, bad

    after = client.get("/api/settings/models").json()
    assert after == before

    factory = get_session_factory(get_settings())
    with factory() as session:
        row = session.execute(
            select(AppSetting).where(AppSetting.key == "models.default")
        ).scalar_one()
    assert row.value == good, "the row itself, not only what the endpoint reports"
    assert "overrides" not in row.value, (
        "the mirror is derived on the way out; a second stored copy is how the two come to "
        "disagree about what the selected tier is"
    )


def test_an_unknown_field_inside_an_override_is_still_a_shape_error(client: TestClient):
    """Loosening the *values* is not loosening the shape: an override carries a model and an
    effort and nothing else, and a field the body invented is a 422 as it always was."""
    response = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "max",
              "overrides": {"ranking": {"modell": "fable"}}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_a_second_save_replaces_the_first_whole(client: TestClient):
    client.put("/api/settings/models", json={"provider": "openai", "tier": "med",
                                             "overrides": {"evolution": {"effort": "high"}}})
    saved = client.put("/api/settings/models",
                       json={"provider": "anthropic", "tier": "max"}).json()

    assert saved["provider"] == "anthropic"
    assert saved["overrides"] == {}, "a replace, not a patch"
    assert saved["tiers"] == {}, "including the buckets the first save wrote"


# --- one bucket per tier --------------------------------------------------------------------
#
# The owner's first request, 2026-08-17: "if set to high and user readjusts and saves this
# becomes the new 'high' setting. same for max and med." So the stored value holds a bucket per
# tier, and the tier in force decides which one a launch inherits.


def test_each_tier_keeps_its_own_overrides_and_the_tier_in_force_selects_one(client: TestClient):
    """The whole feature. One save carries three presets; which of them a launch inherits is
    decided by `tier` alone, so switching the tier switches the table without editing it."""
    body = {
        "provider": "anthropic",
        "tier": "high",
        "tiers": {
            "high": {"generation": {"model": "fable", "effort": "xhigh"}},
            "med": {"generation": {"effort": "medium"}},
        },
    }
    saved = client.put("/api/settings/models", json=body).json()

    assert {tier: stated(bucket) for tier, bucket in saved["tiers"].items()} == body["tiers"], (
        "every bucket comes back, not only the live one"
    )
    assert {row["role"]: row["effort"] for row in saved["table"]}["generation"] == "xhigh"

    switched = client.put(
        "/api/settings/models", json={**body, "tier": "med"}
    ).json()
    assert {row["role"]: row["effort"] for row in switched["table"]}["generation"] == "medium"
    assert switched["tiers"] == saved["tiers"], "switching the tier edits no bucket"


def test_the_served_overrides_are_the_selected_tiers_bucket(client: TestClient):
    """`overrides` is a derived mirror, kept because the launcher, the workshop and the wizard
    all read one block. Derived rather than stored, so it cannot disagree with `tiers`."""
    saved = client.put(
        "/api/settings/models",
        json={
            "provider": "anthropic",
            "tier": "med",
            "tiers": {
                "high": {"ranking": {"effort": "max"}},
                "med": {"ranking": {"effort": "low"}},
            },
        },
    ).json()

    assert stated(saved["overrides"]) == {"ranking": {"effort": "low"}}, "med, not high"
    assert saved["overrides"] == saved["tiers"]["med"]
    served = client.get("/api/settings/models").json()
    assert stated(served["overrides"]) == {"ranking": {"effort": "low"}}


def test_a_bucket_is_a_diff_so_a_role_nobody_edited_keeps_following_the_built_in_table(
    client: TestClient,
):
    """Sparse on purpose: the bucket holds the rows somebody changed. A saved default that
    froze all nine rows would keep pointing at a model the catalog had moved on from — the
    editor showed nine rows, but only one of them was a decision."""
    saved = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "high",
              "tiers": {"high": {"proximity": {"effort": "high"}}}},
    ).json()

    assert stated(saved["tiers"]["high"]) == {"proximity": {"effort": "high"}}, "one row, not nine"
    table = {row["role"]: row for row in saved["table"]}
    assert table["proximity"]["effort"] == "high", "the one edited row"
    assert table["generation"]["effort"] == "high", "and the rest is still the tier's own"
    assert table["evolution"]["effort"] == "medium"


def test_an_empty_bucket_is_pristine_rather_than_a_decision(client: TestClient):
    """`{}` and "absent" have to mean the same thing. A stored empty object would otherwise
    read as "this tier has no overrides, on purpose" and be indistinguishable from a tier
    somebody had deliberately reset — while doing nothing different."""
    saved = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "high", "tiers": {"high": {}, "med": {}}},
    ).json()

    assert saved["tiers"] == {}
    assert saved["overrides"] == {}


def test_a_bad_row_in_a_tier_nobody_selected_is_still_refused(client: TestClient):
    """Validation stays "validate by resolving", and it resolves *every* bucket. The
    alternative is a save that succeeds and a tier switch weeks later that 400s — an error
    nobody can connect to the edit that caused it, arriving when somebody wanted a run."""
    response = client.put(
        "/api/settings/models",
        json={
            "provider": "anthropic",
            "tier": "high",
            "tiers": {
                "high": {"ranking": {"effort": "high"}},
                "med": {"ranking": {"model": "claude-fable-5"}},
            },
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "model_policy"
    assert MODEL_TRAPS["claude-fable-5"] in body["message"], "the trap still arrives whole"
    assert "Med" in body["message"], "and it names which preset the bad row is in"
    assert client.get("/api/settings/models").json()["source"] == "built_in", "nothing stored"


def test_a_tier_name_this_build_does_not_have_is_refused_as_a_bucket_key(client: TestClient):
    response = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "high", "tiers": {"cheapest": {}}},
    )

    assert response.status_code == 400
    assert "cheapest" in response.json()["message"]


def test_a_legacy_body_still_saves_and_means_the_tier_it_names(client: TestClient):
    """A client that has not been updated must not silently wipe the preset it is editing.
    `overrides` is what that client sends, and it is what the row written before this change
    says, so both are read as the bucket for the tier the body names."""
    saved = client.put(
        "/api/settings/models",
        json={"provider": "anthropic", "tier": "med",
              "overrides": {"ranking": {"effort": "medium"}}},
    ).json()

    assert {tier: stated(bucket) for tier, bucket in saved["tiers"].items()} == {
        "med": {"ranking": {"effort": "medium"}}
    }
    assert stated(saved["overrides"]) == {"ranking": {"effort": "medium"}}


def test_a_body_that_answers_the_same_tier_twice_and_disagrees_is_refused(client: TestClient):
    """Two answers to "what is High" in one request is a client bug. Picking either one
    silently discards a real choice, so the request is refused and says which two."""
    response = client.put(
        "/api/settings/models",
        json={
            "provider": "anthropic",
            "tier": "high",
            "tiers": {"high": {"ranking": {"effort": "max"}}},
            "overrides": {"ranking": {"effort": "low"}},
        },
    )

    assert response.status_code == 400
    message = response.json()["message"]
    assert "given twice" in message
    assert "tiers" in message and "overrides" in message


def test_a_default_stored_before_per_tier_presets_keeps_the_owners_setting(client: TestClient):
    """The read migration, and the reason it exists: the row written by the old editor carries
    `overrides` and no `tiers`. Reading it as "no overrides anywhere" would silently throw
    away the choice somebody had made and leave the chip and the launch reporting a table
    nobody chose."""
    factory = get_session_factory(get_settings())
    with factory() as session:
        session.add(
            AppSetting(
                key="models.default",
                value={
                    "provider": "anthropic",
                    "tier": "high",
                    "overrides": {"generation": {"model": "fable", "effort": "xhigh"}},
                },
            )
        )
        session.commit()

    settings = stored_model_settings()

    assert settings.source == "stored", "not degraded to the built-in default"
    assert settings.tiers == {"high": {"generation": {"model": "fable", "effort": "xhigh"}}}
    assert settings.overrides == {"generation": {"model": "fable", "effort": "xhigh"}}
    # And it reaches a launch, which is the only thing the setting is for.
    assert resolve_config({})["model_table"][0] == {
        "role": "generation", "model": "fable", "effort": "xhigh"
    }
    # The endpoint serves the migrated shape without a save having happened.
    served = client.get("/api/settings/models").json()
    assert served["tiers"] == settings.tiers
    assert served["overrides"] == settings.overrides


def test_a_launch_inherits_the_bucket_of_the_tier_in_force(client: TestClient):
    """Per-tier persistence end to end: two presets stored at once, and the launch takes the
    one the tier names — the same fact the top-bar chip reports."""
    client.put(
        "/api/settings/models",
        json={
            "provider": "anthropic",
            "tier": "med",
            "tiers": {
                "max": {"overview": {"effort": "max"}},
                "med": {"overview": {"effort": "medium"}},
            },
        },
    )

    config = resolve_config({"rounds": 2, "budget_calls": 30})

    assert config["model_tier"] == "med"
    assert config["model_overrides"] == {"overview": {"effort": "medium"}}
    assert {row["role"]: row["effort"] for row in config["model_table"]}["overview"] == "medium"


# --- the low tier ---------------------------------------------------------------------------


def test_the_low_tier_can_be_saved_and_launched_and_is_all_luna_under_anthropic(
    client: TestClient,
):
    """Low pins every step to Luna for speed, so a default saved with `provider="anthropic"`
    still launches a table that is entirely OpenAI's fast model. Cross-provider by design —
    the runner is chosen per call from the row's model — and `resolve_config` is where a table
    that could not launch is refused, so a config coming back out of it is the launchability
    claim rather than a description of one."""
    saved = client.put(
        "/api/settings/models", json={"provider": "anthropic", "tier": "low", "tiers": {}}
    ).json()

    assert {row["model"] for row in saved["table"]} == {"gpt-5.6-luna"}

    config = resolve_config({"rounds": 2, "budget_calls": 30})

    assert config["provider"] == "anthropic", "the provider is not rewritten by the tier"
    assert config["model_tier"] == "low"
    assert {row["model"] for row in config["model_table"]} == {"gpt-5.6-luna"}
    efforts = {row["role"]: row["effort"] for row in config["model_table"]}
    assert efforts["generation"] == "high"
    assert efforts["proximity"] == "low", "clustering stays pinned low, which is the point here"


def test_the_capabilities_payload_says_the_low_tier_pins_luna_and_needs_codex(
    client: TestClient,
):
    """The condition on which the pin was allowed: it is visible before a run, not discovered
    by one. A tier that changes the model has to say so, and say which CLI that needs."""
    served = client.get("/api/capabilities").json()["models"]["tier_catalog"]
    tiers = {row["id"]: row for row in served}

    assert list(tiers) == list(MODEL_TIERS)
    assert tiers["low"]["pinned_models"] == {"heavy": "gpt-5.6-luna", "light": "gpt-5.6-luna"}
    assert tiers["low"]["requires_harness"] == ["codex"]
    assert "codex" in tiers["low"]["note"]
    assert tiers["high"]["pinned_models"] == {}
    assert tiers["high"]["available"] is True, "a tier that pins nothing needs no extra CLI"


# --- what the default is for ----------------------------------------------------------------


def test_a_launch_that_states_no_models_inherits_the_saved_default(client: TestClient):
    """The point of the whole feature. A request that says nothing about models must launch
    exactly what the top bar promised it would."""
    client.put("/api/settings/models", json={"provider": "openai", "tier": "med"})

    config = resolve_config({"rounds": 2, "budget_calls": 30})

    assert config["provider"] == "openai"
    assert config["model_tier"] == "med"
    assert {row["model"] for row in config["model_table"]} == {"gpt-5.6-sol", "gpt-5.6-luna"}


def test_a_launch_that_states_its_own_tier_still_overrides_the_default(client: TestClient):
    client.put("/api/settings/models", json={"provider": "openai", "tier": "med"})

    config = resolve_config({"model_tier": "max"})

    assert config["model_tier"] == "max", "the run wins for itself"
    assert config["provider"] == "openai", "and inherits what it did not state"


def test_the_run_takes_the_lane_of_the_provider_it_mostly_drives(client: TestClient):
    """The harness column is a lane lock, not a description. A run whose table is entirely
    Codex sitting in the Claude lane would block a Claude run for no reason."""
    from app.services.runs.launcher import _align_harness

    document = resolve_config({"provider": "openai"})
    assert _align_harness("claude", document) == "codex"

    demo = resolve_config({"provider": "openai", "runner": "demo"})
    assert _align_harness("demo", demo) == "demo", "a demo run never takes a real lane"


def test_capabilities_serves_the_saved_default_so_the_wizard_inherits_it(client: TestClient):
    client.put("/api/settings/models", json={"provider": "openai", "tier": "high"})

    models = client.get("/api/capabilities").json()["models"]

    assert models["default_provider"] == "openai"
    assert models["default_tier"] == "high"
    assert models["default"]["source"] == "stored"


def test_a_workshop_reads_the_same_default_a_run_would(client: TestClient):
    """A workshop runs before a run exists, so it has no tier of its own. Reading a hard-coded
    one here is how the top bar comes to say one thing while the first call a scientist makes
    quietly does another."""
    from app.services.workshop.service import _model_row

    client.put("/api/settings/models", json={"provider": "openai", "tier": "med"})

    assert _model_row()["model"] == "gpt-5.6-luna"
    assert _model_row()["effort"] == "low"


# --- reading is total -----------------------------------------------------------------------


def test_an_unresolvable_stored_default_degrades_to_the_built_in_one_rather_than_failing():
    """A launch must not fail because a *preference* is unreadable. The built-in default is a
    real table, and a run that quietly used it is far better than a run that never started."""
    factory = get_session_factory(get_settings())
    with factory() as session:
        session.add(
            AppSetting(key="models.default", value={"provider": "anthropic", "tier": "atlantis"})
        )
        session.commit()

    settings = stored_model_settings()

    assert settings.source == "built_in"
    assert settings.tier == BUILT_IN.tier


def test_validation_names_the_specific_trap_rather_than_saying_invalid():
    with pytest.raises(ModelPolicyError) as excinfo:
        validate_model_settings(
            {"provider": "anthropic", "tier": "max",
             "overrides": {"ranking": {"model": "claude-fable-5"}}}
        )

    assert "silently resolves to Opus 5" in str(excinfo.value)


def test_an_unknown_top_level_key_is_refused_rather_than_stored_and_ignored():
    with pytest.raises(ModelPolicyError, match="unknown key"):
        validate_model_settings({"provider": "anthropic", "tier": "max", "models": []})


def test_storing_twice_updates_in_place_rather_than_conflicting():
    """Two tabs saving at once should leave the later one's choice, not a unique violation."""
    store_model_settings({"provider": "anthropic", "tier": "max"})
    second = store_model_settings({"provider": "openai", "tier": "med"})

    assert second.provider == "openai"
    assert stored_model_settings().tier == "med"

"""The runner contract and the fake that stands in for the CLI.

FakeRunner is not a stub — it is what every test and every demo run executes, so its
determinism and its failure script are load-bearing and pinned here.
"""

from __future__ import annotations

import asyncio

import pytest

from app.engine.models import (
    ALLOWED_MODELS,
    EFFORTS,
    PROVIDERS,
    ModelPolicyError,
    model_for,
    models_for,
    supported_efforts,
)
from app.engine.prompts import GROUNDING_DEPTHS
from app.engine.runners import (
    BASELINE_MODEL_TABLE,
    BATCH_ROLES,
    GROUNDED_ROLES,
    MODEL_TIERS,
    ROLE_CLASSES,
    ROLE_NOTES,
    ROLES,
    TIER_LABELS,
    TIER_MODELS,
    TIMEOUT_BATCH_GROUNDED,
    TIMEOUT_GROUNDED,
    TIMEOUT_TOOLLESS,
    TOOLS_WEB,
    AgentRunner,
    Failure,
    FakeRunner,
    resolve_model_table,
    role_catalog,
    role_config,
    tier_catalog,
    tier_effort,
    tier_model,
    timeout_for,
    tools_for,
)
from app.engine.schemas import validate_role_output


def cfg(role="generation", *, round=1, effort="medium", unit=None):
    return role_config(
        role, model="claude-opus-5", effort=effort, system_prompt="…", round=round, unit=unit
    )


async def call(runner, role, prompt, **kwargs):
    return await runner.run_role(role, prompt, cfg(role, **kwargs))


# --- the role table ---------------------------------------------------------------------


def test_the_baseline_table_covers_every_role():
    assert {row["role"] for row in BASELINE_MODEL_TABLE} == set(ROLES)


def test_no_role_runs_on_anything_off_the_allowlist_in_any_tier_or_provider():
    """The whole point of the policy: no tier and no role can reach a model the CLI would
    silently substitute. A regression here is a quality cut nobody would see in the UI."""
    for provider in PROVIDERS:
        for tier in MODEL_TIERS:
            for row in resolve_model_table(provider, tier):
                assert row["model"] in ALLOWED_MODELS, f"{provider}/{tier}/{row['role']}"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_tier_that_pins_no_model_moves_effort_only(provider: str):
    """The owner's own framing: "even medium effort would still use fable low effort for the
    hardest tasks". A cheap tier that quietly swapped a heavy role onto the light model would
    be a downgrade invisible in the UI — so for every tier that pins nothing the model column
    is asserted identical, and only the effort column moves.

    `low` is excluded here because it pins a model *on purpose*, which the next two tests
    cover. It is excluded by asking `TIER_MODELS` rather than by naming it, so a second pinned
    tier cannot slip into this assertion and be read as a regression."""
    unpinned = [tier for tier in MODEL_TIERS if tier not in TIER_MODELS]
    assert len(unpinned) >= 2, "nothing is being compared"
    tables = {
        tier: {row["role"]: row for row in resolve_model_table(provider, tier)}
        for tier in unpinned
    }
    models = [{role: row["model"] for role, row in table.items()} for table in tables.values()]

    assert all(column == models[0] for column in models)
    efforts = {tier: {role: row["effort"] for role, row in t.items()} for tier, t in tables.items()}
    assert efforts["max"] != efforts["high"] != efforts["med"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_low_tier_pins_every_step_to_luna_whatever_the_provider_says(provider: str):
    """The one deliberate exception, relaxed by the owner on 2026-08-17 to have a speed
    preset. What makes it legitimate rather than the invisible downgrade the policy refuses is
    that it is on every row of the table the whole UI renders — which is what this asserts,
    including under `provider="anthropic"`, where a run's untouched rows would otherwise all
    be Anthropic's."""
    table = {row["role"]: row for row in resolve_model_table(provider, "low")}

    assert {row["model"] for row in table.values()} == {"gpt-5.6-luna"}
    assert table["generation"]["effort"] == "high", "fast model, still asked to think"
    assert table["proximity"]["effort"] == "low", "clustering stays pinned here too"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_heavy_steps_take_the_frontier_model_in_every_tier_that_pins_none(provider: str):
    heavy = {"generation", "reflection", "ranking", "meta_review", "overview"}
    light = {"proximity", "evolution", "cartographer", "workshop"}

    for tier in MODEL_TIERS:
        if tier in TIER_MODELS:
            continue
        table = {row["role"]: row["model"] for row in resolve_model_table(provider, tier)}
        assert {table[role] for role in heavy} == {model_for(provider, "heavy")}, tier
        assert {table[role] for role in light} == {model_for(provider, "light")}, tier


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_four_tiers_are_the_effort_pairs_the_contract_names(provider: str):
    expected = {
        "max": {"heavy": "xhigh", "light": "high"},
        "high": {"heavy": "high", "light": "medium"},
        "med": {"heavy": "low", "light": "low"},
        "low": {"heavy": "high", "light": "high"},
    }
    assert set(expected) == set(MODEL_TIERS), "a tier was added without an effort pair here"
    for tier, wanted in expected.items():
        table = {row["role"]: row["effort"] for row in resolve_model_table(provider, tier)}
        assert table["generation"] == wanted["heavy"], tier
        assert table["evolution"] == wanted["light"], tier


@pytest.mark.parametrize("provider", PROVIDERS)
def test_clustering_is_pinned_to_low_effort_in_every_tier(provider: str):
    """It reads a list of titles and labels them. Effort buys it nothing, and the resource it
    would spend — minutes on the critical path of every round — is the scarce one."""
    for tier in MODEL_TIERS:
        table = {row["role"]: row["effort"] for row in resolve_model_table(provider, tier)}
        assert table["proximity"] == "low", tier


def test_the_two_providers_tables_are_symmetric_because_codex_accepts_xhigh():
    """The plan expected an OpenAI column clamped at `high`. The probe disproved it, and a
    clamp kept out of deference to the plan would have downgraded every heavy OpenAI role at
    the max tier."""
    for tier in MODEL_TIERS:
        anthropic = [row["effort"] for row in resolve_model_table("anthropic", tier)]
        openai = [row["effort"] for row in resolve_model_table("openai", tier)]
        assert anthropic == openai, tier


def test_the_older_tier_names_still_resolve():
    """Stored run configs say these. A clone or a `continue` of one must still launch, and
    the mapping preserves rank: the strongest tier of the day stays the strongest."""
    assert resolve_model_table(tier="maximum") == resolve_model_table(tier="max")
    assert resolve_model_table(tier="quality") == resolve_model_table(tier="max")
    assert resolve_model_table(tier="standard") == resolve_model_table(tier="high")
    assert resolve_model_table(tier="balanced") == resolve_model_table(tier="high")


def test_an_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="unknown model tier"):
        resolve_model_table(tier="cheapest")


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="unknown provider"):
        resolve_model_table("google")


def test_no_tier_can_ask_a_model_for_a_rung_it_does_not_have():
    """Asserted at import, and asserted again here so the reason is written down: the effort
    a tier assigns is sent verbatim to the CLI, and a level outside the model's ladder is a
    400 on a call that has already been paid for."""
    for provider in PROVIDERS:
        for tier in MODEL_TIERS:
            for row in resolve_model_table(provider, tier):
                assert row["effort"] in supported_efforts(row["model"]), f"{tier}/{row['role']}"


def test_the_ladder_check_is_made_against_the_model_the_tier_pins():
    """The import-time check used to read `model_for(provider, class)`, which was the same
    model the tier resolved to until `low` started pinning one. Against a pinned tier that
    check asks the wrong model's ladder, so a rung Luna does not have would have been
    discovered by a paid call. Asserted through `tier_model`, which is the one place the pin
    is read, so the check and the resolver cannot drift apart."""
    for role in ROLES:
        assert tier_model("low", role) == "gpt-5.6-luna"
        assert tier_effort("low", role) in supported_efforts("gpt-5.6-luna")
        # And every unpinned tier still answers None, which is what leaves the model to the
        # provider — the case that must not become "some model" by accident.
        for tier in MODEL_TIERS:
            if tier not in TIER_MODELS:
                assert tier_model(tier, role) is None, tier


def test_every_row_of_the_low_tier_is_a_call_the_engine_can_actually_build():
    """Resolving is not launching. Each row goes through `role_config`, which is what the
    orchestrator builds per call — so an effort outside Luna's ladder or a timeout the table
    cannot produce fails here rather than on the first paid call of a Low run."""
    for row in resolve_model_table("anthropic", "low"):
        built = role_config(
            row["role"], model=row["model"], effort=row["effort"], system_prompt="…"
        )
        assert built.model == "gpt-5.6-luna"
        assert built.effort in supported_efforts(built.model)
        assert built.timeout_s > 0


def test_the_tier_catalog_publishes_the_pin_and_the_cli_it_needs():
    """The condition on which the `low` tier was allowed to name a model at all: the pin is
    published, so a UI can say "this tier runs Luna, and it needs Codex" instead of a person
    discovering both from a run that failed."""
    rows = {row["id"]: row for row in tier_catalog()}

    assert [row["id"] for row in tier_catalog()] == list(MODEL_TIERS), "strongest first"
    for tier, row in rows.items():
        assert row["label"] == TIER_LABELS[tier]
        assert row["note"].endswith("."), f"{tier}'s note is a label rather than a sentence"
    assert rows["low"]["pinned_models"] == {"heavy": "gpt-5.6-luna", "light": "gpt-5.6-luna"}
    assert rows["low"]["requires_harness"] == ["codex"], "a Low run drives Codex, not Claude"
    for tier in ("max", "high", "med"):
        assert rows[tier]["pinned_models"] == {}
        assert rows[tier]["requires_harness"] == [], "an unpinned tier needs the provider's CLI"


# --- per-role overrides -----------------------------------------------------------------


def test_a_per_role_override_beats_even_a_tier_that_pins_its_model():
    """The precedence in one line: override, then the tier's pin, then the provider's model
    for the class. A scientist who put Fable on the tournament wants Fable on the tournament,
    including in the tier whose whole point is that everything else is Luna."""
    table = {
        row["role"]: row["model"]
        for row in resolve_model_table(
            "anthropic", "low", overrides={"ranking": {"model": "fable"}}
        )
    }

    assert table["ranking"] == "fable"
    assert table["generation"] == "gpt-5.6-luna", "the rest of the pin holds"


def test_an_override_replaces_only_the_fields_it_names():
    table = {
        row["role"]: row
        for row in resolve_model_table(
            "anthropic",
            "high",
            overrides={"proximity": {"effort": "high"}, "ranking": {"model": "claude-opus-5"}},
        )
    }

    assert table["proximity"] == {"role": "proximity", "model": "claude-opus-5", "effort": "high"}
    assert table["ranking"] == {"role": "ranking", "model": "claude-opus-5", "effort": "high"}
    assert table["generation"]["model"] == "fable", "untouched rows keep the tier's"


def test_an_override_beats_the_tier():
    table = {
        row["role"]: row["model"]
        for row in resolve_model_table(
            "anthropic", "max", overrides={"reflection": {"model": "claude-opus-5"}}
        )
    }

    assert table["reflection"] == "claude-opus-5"
    assert table["generation"] == "fable", "the rest of the tier is unaffected"


def test_a_row_may_name_the_other_providers_model_because_mixing_is_the_point():
    """The provider button is a quick-set, not a lock: a scientist may want Fable judging a
    tournament whose clustering runs on Luna. Which CLI executes a row is decided per call
    from that row's model, so a mixed table is a normal table."""
    table = {
        row["role"]: row["model"]
        for row in resolve_model_table(
            "anthropic", "max", overrides={"proximity": {"model": "gpt-5.6-luna"}}
        )
    }

    assert table["proximity"] == "gpt-5.6-luna"
    assert table["generation"] == "fable"


def test_an_override_effort_outside_the_models_ladder_is_clamped_into_the_stored_table():
    """Visibly, in the table the whole UI then shows — rather than 400-ing halfway through a
    paid run, and rather than being clamped invisibly at call time where nothing renders it.
    """
    table = {
        row["role"]: row["effort"]
        for row in resolve_model_table(
            "anthropic", "max", overrides={"ranking": {"effort": "max"}}
        )
    }

    assert table["ranking"] in supported_efforts("fable")


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"rankingg": {"model": "fable"}}, "unknown role"),
        ({"ranking": {"model": "claude-sonnet-5"}}, "not allowed"),
        ({"ranking": {"model": "claude-haiku-4-5"}}, "not allowed"),
        ({"ranking": {"model": "gpt-5.6-terra"}}, "not offered by this engine"),
        ({"ranking": {"effort": "extreme"}}, "unknown effort"),
        ({"ranking": {"effort": "ultra"}}, "unknown effort"),
    ],
)
def test_an_override_outside_the_policy_is_refused(overrides, match):
    with pytest.raises(ModelPolicyError, match=match):
        resolve_model_table("anthropic", "high", overrides=overrides)


def test_the_fable_spelling_that_silently_downgrades_is_refused_by_name():
    """`--model claude-fable-5` runs Opus 5 and says nothing. The error has to name the
    trap, because "invalid model" would send someone to check their spelling — which is
    correct — rather than to the alias."""
    with pytest.raises(ModelPolicyError) as excinfo:
        resolve_model_table("anthropic", "high", overrides={
            "ranking": {"model": "claude-fable-5"}
        })

    message = str(excinfo.value)
    assert "use `fable`" in message
    assert "silently resolves to Opus 5" in message


# --- the catalog the UI reads -------------------------------------------------------------


def test_the_role_catalog_explains_every_row_it_serves():
    """The wizard's per-role picker reads this instead of restating engine reasoning in its
    own words — which is how a UI ends up describing a table it no longer matches."""
    catalog = role_catalog("anthropic", "high")

    assert [row["role"] for row in catalog] == list(ROLES)
    for row in catalog:
        assert row["note"], f"{row['role']} has no explanation to show"
        assert row["model"] in ALLOWED_MODELS
        assert row["effort"] in EFFORTS
        assert row["model_class"] in ("heavy", "light")
    assert {row["role"] for row in catalog if row["grounded"]} == GROUNDED_ROLES


def test_the_role_catalog_reflects_the_provider_the_tier_and_the_overrides():
    maximum = {row["role"]: row["model"] for row in role_catalog("anthropic", "max")}
    openai = {row["role"]: row["model"] for row in role_catalog("openai", "max")}
    overridden = {
        row["role"]: row["effort"]
        for row in role_catalog("anthropic", "high", overrides={"proximity": {"effort": "high"}})
    }

    assert maximum["reflection"] == "fable"
    assert openai["reflection"] == "gpt-5.6-sol"
    assert overridden["proximity"] == "high"


def test_the_role_class_of_every_step_is_the_same_whatever_the_provider():
    """The role → class map is the half of the contract a tier and a provider both cannot
    touch. Two providers disagreeing about which steps are hard would make the two columns
    incomparable."""
    for tier in MODEL_TIERS:
        classes = [
            {row["role"]: row["model_class"] for row in role_catalog(provider, tier)}
            for provider in PROVIDERS
        ]
        assert classes[0] == classes[1] == ROLE_CLASSES


def test_every_role_note_is_a_sentence_rather_than_a_label():
    for role, note in ROLE_NOTES.items():
        assert role in ROLES
        assert note.endswith("."), role
        assert len(note.split()) >= 8, f"{role}'s note says nothing a picker could use"


def test_only_the_grounded_roles_get_a_tool_and_it_is_only_ever_websearch():
    for role in ROLES:
        assert tools_for(role) == (TOOLS_WEB if role in GROUNDED_ROLES else "")
    assert GROUNDED_ROLES == {
        "generation", "reflection", "evolution", "workshop", "verification", "challenge",
    }


def test_role_config_fills_tools_timeout_and_schema_from_the_role():
    grounded = cfg("generation")
    judging = cfg("ranking")

    assert grounded.tools == TOOLS_WEB and grounded.grounded
    assert grounded.timeout_s > TIMEOUT_GROUNDED
    assert judging.tools == "" and not judging.grounded
    assert judging.timeout_s == TIMEOUT_TOOLLESS
    assert judging.json_schema["required"] == ["debate", "winner"]


def test_a_batch_grounded_role_at_the_baseline_outlives_the_calls_that_were_killed():
    """Run c4566ed2 killed 8 of 10 opus-5/high generation and evolution calls at 420s, and
    the two that survived finished at 388.4s and 407.2s. A ceiling those calls can reach is
    the whole point of the fix, so it is asserted rather than left to a constant."""
    for role in ("generation", "evolution"):
        ceiling = timeout_for(role, effort="high", grounding_depth="standard")
        assert ceiling >= 1200.0, role
        assert ceiling > TIMEOUT_GROUNDED * 2, role


def test_reflection_keeps_the_old_grounded_ceiling_and_toolless_roles_keep_theirs():
    """The measured roles are left alone. Reflection at opus-5/high peaks at 107s against
    420s, and no tool-less role has ever exceeded 95.3s against 180s."""
    assert timeout_for("reflection", effort="high") == TIMEOUT_GROUNDED
    for role in ROLES:
        if role not in GROUNDED_ROLES:
            assert timeout_for(role, effort="max", grounding_depth="deep") == TIMEOUT_TOOLLESS


def test_the_ceiling_moves_with_effort_and_grounding_depth():
    """`timeout_for(role)` used to bound sonnet-5/shallow and opus-5/high/deep identically,
    although their observed medians differ by 4x."""
    cheap = timeout_for("generation", effort="low", grounding_depth="shallow")
    dear = timeout_for("generation", effort="max", grounding_depth="deep")
    baseline = timeout_for("generation", effort="high", grounding_depth="standard")
    assert cheap < baseline < dear
    # An effort or depth this build has never heard of must not fail a call.
    assert timeout_for("generation", effort="galactic", grounding_depth="???") == baseline


def test_a_role_gets_the_same_ceiling_on_every_model_in_the_catalog_whatever_its_provider():
    """A live Codex workshop call was seen at 180s while carrying WebSearch, and the reading
    offered was that the ceiling table keys off something Anthropic-specific — so Codex's
    grounded calls are strangled while Claude's are not. It does not: `timeout_for` takes no
    model and no provider, and that shape is the thing worth pinning.

    A provider-conditional ceiling is the worst class of regression this module can grow,
    because it is invisible. One lane's runs read as healthy while the other loses whole
    steps at a deadline built for a different kind of call — which is exactly how run
    c4566ed2 went, on one lane, before anybody was measuring. Every role, every effort and
    every grounding depth is checked, on every model the catalog offers, so a fifth model
    added later is covered on the day it is added rather than the day it is debugged.
    """
    anthropic = [choice.id for choice in models_for("anthropic")]
    openai = [choice.id for choice in models_for("openai")]
    assert anthropic and openai, "the catalog must offer both lanes for this to guard them"

    reference = anthropic[0]
    others = [*anthropic[1:], *openai]
    for role in ROLES:
        for effort in EFFORTS:
            for depth in GROUNDING_DEPTHS:
                ceilings = {
                    model: role_config(
                        role,
                        model=model,
                        effort=effort,
                        system_prompt="…",
                        grounding_depth=depth,
                    ).timeout_s
                    for model in (reference, *others)
                }
                expected = ceilings[reference]
                for model in others:
                    assert ceilings[model] == expected, (
                        f"{role}/{effort}/{depth}: {model} gets {ceilings[model]:g}s but "
                        f"{reference} gets {expected:g}s — a ceiling that varies by model "
                        "strangles one provider's calls and nothing in the UI says so"
                    )


def test_a_grounded_role_keeps_its_large_ceiling_on_every_provider_and_not_the_toolless_one():
    """Equality across providers is not enough on its own: a regression that collapsed
    *both* lanes onto `TIMEOUT_TOOLLESS` would still leave every pair equal. So the ceilings
    are also asserted to be the right ones — the batch roles on `TIMEOUT_BATCH_GROUNDED`,
    reflection on `TIMEOUT_GROUNDED` — for each model the catalog allows, which is what
    would fail if either lane's grounded calls were quietly bounded like tool-less ones.
    """
    assert TIMEOUT_BATCH_GROUNDED > TIMEOUT_GROUNDED > TIMEOUT_TOOLLESS
    for model in ALLOWED_MODELS:
        for role in sorted(BATCH_ROLES):
            ceiling = role_config(
                role, model=model, effort="high", system_prompt="…", grounding_depth="standard"
            ).timeout_s
            assert ceiling == TIMEOUT_BATCH_GROUNDED, f"{role} on {model}"
        assert (
            role_config(
                "reflection",
                model=model,
                effort="high",
                system_prompt="…",
                grounding_depth="standard",
            ).timeout_s
            == TIMEOUT_GROUNDED
        ), model


def test_a_config_can_be_retried_on_a_longer_clock_without_changing_anything_else():
    original = cfg("generation")
    stretched = original.with_timeout(original.timeout_s * 2)
    assert stretched.timeout_s == original.timeout_s * 2
    assert stretched.with_timeout(original.timeout_s) == original


def test_role_config_refuses_an_unknown_role_or_effort():
    with pytest.raises(ValueError, match="unknown role"):
        role_config("librarian", model="m", effort="low", system_prompt="…")
    with pytest.raises(ValueError, match="unknown effort"):
        role_config("ranking", model="m", effort="enormous", system_prompt="…")


def test_effort_can_be_escalated_without_rebuilding_the_config():
    assert cfg("ranking").with_effort("high").effort == "high"


def test_the_fake_satisfies_the_protocol():
    assert isinstance(FakeRunner(), AgentRunner)


# --- determinism -------------------------------------------------------------------------


async def test_the_same_run_role_and_prompt_give_the_same_answer():
    runner = FakeRunner(seed="run-a")

    first = await call(runner, "generation", "N: 3\nGOAL: x")
    second = await call(runner, "generation", "N: 3\nGOAL: x")

    assert first.data == second.data


async def test_a_different_run_gives_a_different_answer():
    a = await call(FakeRunner(seed="run-a"), "generation", "N: 3\nGOAL: x")
    b = await call(FakeRunner(seed="run-b"), "generation", "N: 3\nGOAL: x")

    assert a.data != b.data


async def test_a_changed_prompt_changes_the_answer():
    runner = FakeRunner(seed="run-a")

    first = await call(runner, "generation", "N: 3\nGOAL: x")
    second = await call(runner, "generation", "N: 3\nGOAL: y")

    assert first.data != second.data


# --- payloads ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "prompt"),
    [
        ("generation", "N: 4\nGOAL: goal"),
        ("evolution", "N: 2\nTOP: h001 | a | b\nh002 | c | d"),
        ("reflection", "HYPOTHESIS h001:\nbody"),
        ("proximity", "h001 | a | b\nh002 | c | d\nh003 | e | f"),
        ("ranking", "HYPOTHESIS 1 (h001)\nHYPOTHESIS 2 (h002)"),
        ("meta_review", "MODE: feedback"),
        ("overview", "h001 | Leader | claim"),
        ("cartographer", "CURRENT CLUSTERS:\nh001 | a | b"),
        ("workshop", "QUESTION: why"),
    ],
)
async def test_every_role_produces_a_payload_its_own_schema_accepts(role, prompt):
    result = await call(FakeRunner(seed="run-a"), role, prompt)

    assert result.ok
    assert validate_role_output(role, result.data) is None


async def test_generation_produces_the_number_of_hypotheses_the_prompt_asked_for():
    result = await call(FakeRunner(seed="run-a"), "generation", "N: 5\nGOAL: goal")

    assert len(result.data["hypotheses"]) == 5
    assert len({item["title"] for item in result.data["hypotheses"]}) == 5


async def test_proximity_labels_exactly_the_ids_it_was_given():
    result = await call(FakeRunner(seed="run-a"), "proximity", "h001 | a | b\nh002 | c | d")

    assert set(result.data["clusters"]) == {"h001", "h002"}


async def test_ranking_names_both_sides_in_its_debate():
    result = await call(
        FakeRunner(seed="run-a"), "ranking", "HYPOTHESIS 1 (h007)\nHYPOTHESIS 2 (h009)"
    )

    assert result.data["winner"] in (1, 2)
    assert "h007" in result.data["debate"] and "h009" in result.data["debate"]


async def test_evolution_derives_from_the_parents_in_the_prompt():
    result = await call(FakeRunner(seed="run-a"), "evolution", "N: 3\nh001 | a | b\nh002 | c | d")

    for variant in result.data["hypotheses"]:
        assert set(variant["derived_from"]) <= {"h001", "h002"}
        assert variant["operator"] in ("grounding", "combination", "simplification", "out_of_box")


async def test_the_overview_reads_like_a_report():
    result = await call(FakeRunner(seed="run-a"), "overview", "h001 | The leading idea | claim")

    markdown = result.data["markdown"]
    assert markdown.startswith("# Research Overview")
    assert "The leading idea" in markdown
    assert "## Caveats" in markdown


async def test_usage_reports_all_four_token_classes_and_a_cost():
    result = await call(FakeRunner(seed="run-a"), "ranking", "a prompt of some length")

    usage = result.usage
    assert usage.tokens_in > 0 and usage.tokens_out > 0
    assert usage.cache_creation > 0 and usage.cache_read > 0
    assert usage.cost_usd > 0
    assert usage.duration_ms is not None
    assert usage.tokens_total == (
        usage.tokens_in + usage.tokens_out + usage.cache_creation + usage.cache_read
    )


async def test_an_unknown_role_is_a_programming_error_not_a_result():
    with pytest.raises(ValueError, match="unknown role"):
        await FakeRunner().run_role("librarian", "…", cfg("generation"))


# --- the failure script -------------------------------------------------------------------


async def test_a_scripted_failure_hits_only_its_role_and_round():
    runner = FakeRunner(seed="run-a", failures={("ranking", 3): "timeout"})

    failed = await call(runner, "ranking", "h001 h002", round=3)
    fine = await call(runner, "ranking", "h001 h002", round=2)

    assert failed.error == "timeout"
    assert failed.data is None and not failed.ok
    assert "scripted failure" in failed.raw_tail
    assert fine.ok


async def test_a_failure_with_no_round_applies_to_every_round():
    runner = FakeRunner(seed="run-a", failures={("proximity", None): "exit code 1"})

    assert (await call(runner, "proximity", "h001 | a | b", round=1)).error == "exit code 1"
    assert (await call(runner, "proximity", "h001 | a | b", round=9)).error == "exit code 1"


async def test_times_limits_how_many_calls_a_failure_touches():
    runner = FakeRunner(seed="run-a", failures={("reflection", 1): Failure("timeout", times=1)})

    first = await call(runner, "reflection", "h001")
    second = await call(runner, "reflection", "h001")

    assert first.error == "timeout"
    assert second.ok


async def test_a_malformed_failure_returns_json_the_schema_rejects():
    runner = FakeRunner(seed="run-a", failures={("ranking", 1): Failure(malformed=True)})

    result = await call(runner, "ranking", "h001 h002")

    assert result.error is None, "the call itself succeeded"
    assert validate_role_output("ranking", result.data) is not None


async def test_rate_limiting_and_degradation_are_reported_on_the_result():
    runner = FakeRunner(
        seed="run-a",
        failures={
            ("ranking", 1): Failure(rate_limited=True),
            ("proximity", 1): Failure(degraded=True),
        },
    )

    assert (await call(runner, "ranking", "h001 h002")).rate_limited
    assert (await call(runner, "proximity", "h001 | a | b")).degraded


# --- bookkeeping ---------------------------------------------------------------------------


async def test_every_call_is_recorded_for_tests_to_inspect():
    runner = FakeRunner(seed="run-a")

    await call(runner, "ranking", "h001 vs h002", round=2, unit="h001 vs h002")

    assert runner.calls == [
        {
            "role": "ranking",
            "round": 2,
            "unit": "h001 vs h002",
            "model": "claude-opus-5",
            "effort": "medium",
            "tools": "",
            # The deadline the call was given. Recorded because the retry policy turns
            # exactly this knob and a test cannot check it any other way.
            "timeout_s": TIMEOUT_TOOLLESS,
            "prompt": "h001 vs h002",
        }
    ]


async def test_latency_is_configurable_and_zero_by_default():
    runner = FakeRunner(seed="run-a", latency=0.05)

    started = asyncio.get_running_loop().time()
    await call(runner, "proximity", "h001 | a | b")

    assert asyncio.get_running_loop().time() - started >= 0.05


async def test_probe_and_close_are_safe():
    runner = FakeRunner()

    assert (await runner.probe())["installed"] is True
    await runner.aclose()
    await runner.aclose()

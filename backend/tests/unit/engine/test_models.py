"""The model allowlist, the Opus floor, and the guard on what actually answered.

Every assertion here is downstream of one live probe (2026-08-08, CLI 2.1.220): `--model
claude-fable-5` runs `claude-opus-5` and reports no error. That is why the allowlist is a
closed set, why `fable` is the only accepted spelling of Fable 5, and why every call
compares the model named in the result envelope against the one it asked for.
"""

from __future__ import annotations

import pytest

from app.engine.models import (
    _FAMILY_RANK,
    _PROVIDER_PREFIX,
    ALLOWED_MODELS,
    EFFORTS,
    MODEL_CHOICES,
    OPUS_FLOOR_RANK,
    ModelPolicyError,
    canonical_family,
    check_substitution,
    clamp_effort,
    effort_at_least,
    model_catalog,
    model_for,
    model_rank,
    normalise_provider,
    price_for,
    provider_catalog,
    reported_provider,
    resolve_effort,
    supported_efforts,
    usd_per_call_bracket,
    validate_effort,
    validate_model,
)

# --- the allowlist ------------------------------------------------------------------------


def test_the_allowlist_adds_astra_without_changing_default_role_assignments():
    assert ALLOWED_MODELS == (
        "claude-opus-5", "fable", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra"
    )
    for provider, heavy, light in (
        ("anthropic", "fable", "claude-opus-5"),
        ("openai", "gpt-5.6-sol", "gpt-5.6-luna"),
    ):
        assert model_for(provider, "heavy") == heavy
        assert model_for(provider, "light") == light


def test_sol_is_the_heavy_openai_model_and_luna_the_light_one():
    """The plan had these inverted. `codex debug models` calls sol the "latest frontier"
    model at priority 1 and luna "fast and affordable" at priority 3, and the operator's own
    Codex config defaults to sol — so mapping the judgement-heavy roles onto luna would have
    put generation, reflection and the tournament on the cheap model. That is the silent
    downgrade this whole module exists to refuse, so it is pinned by name."""
    by_id = {choice.id: choice for choice in MODEL_CHOICES}

    assert by_id["gpt-5.6-sol"].model_class == "heavy"
    assert by_id["gpt-5.6-luna"].model_class == "light"
    assert model_rank("gpt-5.6-sol") > model_rank("gpt-5.6-luna")


def test_a_provider_that_this_build_cannot_run_is_refused_by_name():
    assert normalise_provider(None) == "anthropic"
    for value in ("google", "", "Anthropic Inc"):
        with pytest.raises(ModelPolicyError, match="unknown provider"):
            normalise_provider(value)


def test_naming_the_wrong_providers_model_is_refused_rather_than_quietly_swapped():
    """`validate_model(model, provider=…)` is an assertion for callers that already know the
    lane. A filter that substituted the right-provider model instead would be the exact
    failure mode the allowlist exists to prevent, dressed as a convenience."""
    assert validate_model("gpt-5.6-sol", provider="openai") == "gpt-5.6-sol"
    with pytest.raises(ModelPolicyError, match="belongs to openai"):
        validate_model("gpt-5.6-sol", provider="anthropic")


@pytest.mark.parametrize("model", ALLOWED_MODELS)
def test_an_allowed_model_passes_through_unchanged(model: str):
    assert validate_model(model) == model
    assert validate_model(f"  {model} ") == model, "whitespace is trimmed, not refused"


def test_the_fable_5_spelling_is_refused_with_the_trap_named():
    """The one that matters. `claude-fable-5` is a plausible, well-formed, wrong answer:
    the CLI accepts it and silently runs Opus 5, so the error must send the reader to the
    alias rather than to their spelling."""
    with pytest.raises(ModelPolicyError) as excinfo:
        validate_model("claude-fable-5")

    message = str(excinfo.value)
    assert "use `fable`" in message
    assert "silently resolves to Opus 5" in message
    assert "claude-opus-5" in message, "and it says what is allowed instead"


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-8", "opus", ""],
)
def test_anything_below_or_beside_the_floor_is_refused(model: str):
    with pytest.raises(ModelPolicyError):
        validate_model(model)


def test_a_non_string_model_is_refused_rather_than_coerced():
    for value in (None, 5, ["fable"]):
        with pytest.raises(ModelPolicyError, match="non-empty string"):
            validate_model(value)


@pytest.mark.parametrize("effort", EFFORTS)
def test_every_documented_effort_is_accepted(effort: str):
    assert validate_effort(effort) == effort


@pytest.mark.parametrize("effort", ["", "none", "highest", "LOW", None, 3])
def test_an_unknown_effort_is_refused(effort):
    with pytest.raises(ModelPolicyError, match="unknown effort"):
        validate_effort(effort)


# --- escalation is a floor ----------------------------------------------------------------


def test_an_escalation_raises_a_role_that_is_below_it():
    assert effort_at_least("medium", "high") == "high"
    assert effort_at_least("low", "high") == "high"


def test_an_escalation_never_lowers_a_role_that_is_already_above_it():
    """The bug this exists to prevent: the orchestrator escalates round-1 generation and a
    decisive match to `high`. Assigning that would *downgrade* a role a scientist had
    deliberately overridden to `xhigh` — quietly weakening the single call the engine just
    identified as the most important one in the run."""
    assert effort_at_least("xhigh", "high") == "xhigh"
    assert effort_at_least("max", "high") == "max"


def test_an_escalation_to_the_level_a_role_already_runs_at_changes_nothing():
    """True of every reasoning role now that the baseline table is high throughout."""
    assert effort_at_least("high", "high") == "high"


def test_an_escalation_against_an_unknown_effort_is_refused_rather_than_guessed():
    with pytest.raises(ModelPolicyError, match="unknown effort"):
        effort_at_least("blistering", "high")


# --- display metadata ---------------------------------------------------------------------


def test_every_choice_carries_what_a_chooser_needs():
    for entry in model_catalog():
        assert entry["id"] in ALLOWED_MODELS
        assert entry["label"] and entry["recommendation"]
        assert entry["provider"] in ("anthropic", "openai")
        assert entry["model_class"] in ("heavy", "light")
        assert entry["relative_cost"] > 0
        assert 0 < entry["usd_per_call_low"] < entry["usd_per_call_high"]
        assert entry["efforts"], "an effort picker reads this list, not the provider's"


def test_the_labels_are_the_current_choices_the_top_bar_shows():
    assert {entry["label"] for entry in model_catalog()} == {
        "Fable 5.1",
        "Opus 5",
        "Sol",
        "Luna",
        "Astra",
    }


def test_a_model_with_no_published_price_says_so_instead_of_quoting_one():
    """Codex runs on a ChatGPT account: there is no per-token list price to quote. Absent is
    reported as absent, and the planning bracket that remains is labelled as a parity
    estimate rather than left to read as measured."""
    by_id = {entry["id"]: entry for entry in model_catalog()}

    assert by_id["claude-opus-5"]["price_basis"] == "list"
    assert by_id["claude-opus-5"]["input_usd_per_mtok"] == 5.00
    for slug in ("gpt-5.6-sol", "gpt-5.6-luna"):
        assert by_id[slug]["price_basis"] == "class_parity"
        assert by_id[slug]["input_usd_per_mtok"] is None
        assert by_id[slug]["output_usd_per_mtok"] is None
        assert by_id[slug]["usd_per_call_low"] > 0, "a bracket is still offered"


def test_the_provider_catalog_only_offers_providers_this_build_can_run():
    catalog = {row["id"]: row for row in provider_catalog()}

    assert set(catalog) == {"anthropic", "openai"}
    assert catalog["anthropic"]["harness"] == "claude"
    assert catalog["openai"]["harness"] == "codex"
    assert len(catalog["anthropic"]["models"]) == 2
    assert len(catalog["openai"]["models"]) == 3


# --- the per-model effort ladder ------------------------------------------------------------


def test_every_model_publishes_its_own_ladder():
    """A ladder is a property of a model, not of a provider: Codex publishes one per slug in
    its own catalog. A picker keyed on the provider will eventually offer a rung the selected
    model does not have, which is a 400 in the middle of a paid run."""
    for model in ALLOWED_MODELS:
        ladder = supported_efforts(model)
        assert ladder, model
        assert set(ladder) <= set(EFFORTS)
        assert list(ladder) == [effort for effort in EFFORTS if effort in ladder], "weakest first"


def test_codex_accepts_xhigh_so_nothing_is_clamped_today():
    """The plan said Codex effort topped out at `high`. The live probe ran `xhigh` on Luna and
    it exited 0, so a provider-level clamp would have silently downgraded every heavy role at
    the max tier."""
    for model in ("gpt-5.6-sol", "gpt-5.6-luna"):
        assert "xhigh" in supported_efforts(model)
        assert resolve_effort(model, "xhigh").clamped is False


def test_ultra_is_never_in_the_vocabulary_at_all():
    """It exists in Sol's CLI catalog and in no API enum: sending it is a 400. An effort no
    tier can request has no business being offered."""
    assert "ultra" not in EFFORTS
    for model in ALLOWED_MODELS:
        assert "ultra" not in supported_efforts(model)
    with pytest.raises(ModelPolicyError, match="unknown effort"):
        validate_effort("ultra")


def test_an_effort_above_a_models_ladder_clamps_down_to_the_top_of_it():
    """The case with no example in today's catalog and one the day a shorter-laddered model
    ships. Clamping *down* is the honest direction: asking for more thinking than a model can
    do is a preference it cannot fully honour, and clamping up would spend more than was
    asked for."""
    assert clamp_effort("max", ("low", "medium", "high")) == "high"
    assert clamp_effort("xhigh", ("low", "medium", "high")) == "high"
    assert clamp_effort("medium", ("low", "medium", "high")) == "medium"


def test_a_model_whose_floor_is_above_the_request_runs_at_its_floor_rather_than_refusing():
    assert clamp_effort("low", ("high", "xhigh")) == "high"


def test_a_clamp_is_never_silent():
    resolution = resolve_effort("gpt-5.6-sol", "max")

    assert resolution.clamped is False, "sol's ladder reaches max"
    assert resolution.effort == "max"


def test_relative_cost_is_a_multiple_of_the_floor():
    by_id = {choice.id: choice for choice in MODEL_CHOICES}

    assert by_id["claude-opus-5"].relative_cost == 1.0
    assert by_id["fable"].relative_cost == 2.0, "$10/$50 against opus-5's $5/$25"


def test_the_per_call_bracket_scales_exactly_with_price():
    """Every allowed model prices output at 5× input, so one scalar covers input, output,
    cache reads and cache writes alike — which is what makes a bracket measured on a
    sonnet-5 run transferable rather than a guess."""
    opus = price_for("claude-opus-5").usd_per_call
    fable = price_for("fable").usd_per_call

    # Approximate only because each bound is rounded to the cent-ish place it is displayed
    # at; the underlying ratio is exactly 2.
    assert fable[0] / opus[0] == pytest.approx(2.0, abs=0.01)
    assert fable[1] / opus[1] == pytest.approx(2.0, abs=0.01)
    assert usd_per_call_bracket(3.00) == (0.15, 0.40), "the measured sonnet-5 baseline"


def test_pricing_is_only_published_for_models_a_role_may_run():
    with pytest.raises(ModelPolicyError):
        price_for("claude-sonnet-5")


# --- capability ranking -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("claude-fable-5-20260601", 3),
        ("claude-opus-5", 2),
        ("claude-opus-5-20260601", 2),
        ("claude-opus-4-8", 1),
        ("claude-sonnet-5-20260601", 1),
        ("claude-haiku-4-5-20251001", 0),
        ("claude-something-that-ships-in-2027", None),
    ],
)
def test_a_reported_model_is_ranked_against_the_floor(reported: str, expected: int | None):
    assert model_rank(reported) == expected


def test_the_floor_sits_at_opus_5():
    assert model_rank("claude-opus-5") == OPUS_FLOOR_RANK
    assert model_rank("claude-opus-4-8") < OPUS_FLOOR_RANK
    assert model_rank("claude-fable-5") > OPUS_FLOOR_RANK


def test_the_alias_resolves_to_the_family_the_cli_reports():
    assert canonical_family("fable") == "claude-fable-5-1"
    assert canonical_family("claude-opus-5") == "claude-opus-5"


# --- the substitution guard ---------------------------------------------------------------

HOUSEKEEPING = {"claude-haiku-4-5-20251001": {"inputTokens": 40}}


def test_a_matching_model_is_not_a_substitution():
    usage = {"claude-opus-5-20260601": {"inputTokens": 12}, **HOUSEKEEPING}

    assert check_substitution("claude-opus-5", usage) is None


def test_the_alias_matching_its_family_id_is_not_a_substitution():
    usage = {"claude-fable-5-1": {"inputTokens": 12}, **HOUSEKEEPING}

    assert check_substitution("fable", usage) is None


def test_old_fable_and_sol_cannot_silently_stand_in_for_the_upgrades():
    for requested, actual in [("fable", "claude-fable-5"), ("gpt-6-astra", "gpt-5.6-sol")]:
        substitution = check_substitution(requested, {actual: {"inputTokens": 12}})
        assert substitution is not None and substitution.below_request
    assert check_substitution("gpt-6-astra", {"gpt-6-astra": {"inputTokens": 12}}) is None
    assert price_for("gpt-6-astra").efforts == ("low", "medium", "high", "xhigh", "max")


def test_matching_usage_cannot_hide_an_older_model_in_the_same_response():
    swap = check_substitution("fable", {
        "claude-fable-5-1": {"inputTokens": 12},
        "claude-fable-5": {"inputTokens": 12},
    })
    assert swap is not None and swap.below_request
    assert check_substitution("fable", {
        "claude-fable-5-1-20260901": {"inputTokens": 12},
    }) is None


@pytest.mark.parametrize("usage", [None, {}, HOUSEKEEPING, "not a map", {"<synthetic>": {}}])
def test_nothing_to_compare_against_is_not_a_substitution(usage):
    """Absent evidence is not evidence of a swap. Failing calls on a missing or renamed
    field would turn a CLI release into an outage."""
    assert check_substitution("claude-opus-5", usage) is None


def test_a_sideways_swap_is_reported_without_breaching_the_floor():
    usage = {"claude-opus-5-20260601": {"inputTokens": 12}, **HOUSEKEEPING}

    swap = check_substitution("fable", usage)

    assert swap is not None
    assert swap.requested == "fable"
    assert swap.ran == ("claude-opus-5-20260601",)
    assert swap.below_floor is False
    assert swap.fatal is False, "still above the floor, so the answer is worth keeping"
    assert swap.below_request is True, "but it is not the model the run asked for"
    assert swap.detail == (
        "requested fable, ran claude-opus-5-20260601 — a weaker model than the run asked for"
    )


# --- the guard across two providers -------------------------------------------------------


def test_an_openai_model_answering_a_claude_call_is_fatal_however_capable_it_is():
    """Not a ranking question. A step routed to the other CLI ran under a different sandbox,
    a different search mechanism and a different set of suppressed host config — nothing
    about that call is the call that was configured, so "is Sol as good as Fable" never
    arises."""
    swap = check_substitution("fable", {"gpt-5.6-sol": {"inputTokens": 12}})

    assert swap is not None
    assert swap.cross_provider is True
    assert swap.fatal is True
    assert "another provider" in swap.detail


def test_a_name_belonging_to_no_known_provider_is_fatal_rather_than_a_degrade():
    """An unknown id *within* a known provider is probably a model newer than this table, and
    failing runs for being current would be worse than the swap. A name that belongs to no
    provider at all is a different claim — the invocation reached something this engine has
    no account of — and degrading on that would be inferring "probably fine" from "no idea"."""
    swap = check_substitution("claude-opus-5", {"llama-4-400b": {"inputTokens": 12}})

    assert swap is not None and swap.fatal


def test_the_floor_is_looked_up_per_provider_so_luna_is_not_a_breach():
    """Ranking Luna against the Anthropic line-up would make every legitimate Luna call read
    as a floor breach — a hard failure on a correct call, the one error class that looks fine
    in review."""
    assert check_substitution("gpt-5.6-luna", {"gpt-5.6-luna": {"inputTokens": 12}}) is None

    swap = check_substitution("gpt-5.6-sol", {"gpt-5.6-luna": {"inputTokens": 12}})
    assert swap is not None
    assert swap.below_floor is False, "luna is the OpenAI floor, not below it"
    assert swap.below_request is True
    assert swap.fatal is False


def test_the_openai_floor_is_a_floor_and_not_only_a_label():
    """The other half of the per-provider floor: OpenAI has one too, and something under it
    fails the call exactly as a sub-Opus model does on the Anthropic lane. Without this the
    lookup could return the right number for luna by accident and never be exercised."""
    swap = check_substitution("gpt-5.6-luna", {"gpt-5.5-turbo": {"inputTokens": 12}})

    assert swap is not None
    assert swap.below_floor is True
    assert swap.cross_provider is False, "gpt-5.5 is OpenAI's; this is a floor breach"
    assert swap.fatal is True
    assert "below the Luna floor" in swap.detail


def test_anthropic_housekeeping_is_not_filtered_out_of_an_openai_comparison():
    """Filtering haiku everywhere would hide a Claude model answering a Codex call, which is
    the loudest thing this guard has to say."""
    swap = check_substitution("gpt-5.6-sol", {"claude-haiku-4-5-20251001": {"inputTokens": 40}})

    assert swap is not None and swap.cross_provider and swap.fatal


def test_a_downgrade_below_the_floor_is_reported_as_one():
    usage = {"claude-sonnet-5-20260601": {"inputTokens": 12}, **HOUSEKEEPING}

    swap = check_substitution("claude-opus-5", usage)

    assert swap is not None and swap.below_floor
    assert "below the Opus 5 floor" in swap.detail


def test_an_unrecognised_model_degrades_rather_than_failing_the_run():
    """A model released after this table was written is a swap we cannot rank. Reporting
    it is right; calling it a downgrade and killing the run is not.

    `below_floor is False` alone does not say that: an unrankable id can never breach a
    floor (the comparison skips it for want of a rank), so asserting only that passes even
    when the guard is failing the run through `cross_provider`. `fatal` is the assertion
    that has teeth, and it is the one the Anthropic lane's time-bomb hangs on — the day the
    CLI reports an id newer than `_FAMILY_RANK`, every role call in the run either survives
    or dies on this line."""
    swap = check_substitution("claude-opus-5", {"claude-opus-6": {"inputTokens": 12}})

    assert swap is not None
    assert swap.below_floor is False
    assert swap.cross_provider is False, "claude-opus-6 is Anthropic's, table or no table"
    assert swap.fatal is False
    assert swap.unrecognised is True
    assert swap.below_request is False, "unknown is not weaker; there is no rank to compare"


def test_an_unrecognised_openai_id_degrades_on_the_openai_lane_too():
    """The same rule, stated on the other provider, because a fix that special-cases
    `claude-` would pass the test above and still fail every Codex run the day OpenAI
    renames a model."""
    swap = check_substitution("gpt-5.6-sol", {"gpt-5.7-nova": {"inputTokens": 12}})

    assert swap is not None
    assert swap.cross_provider is False
    assert swap.fatal is False
    assert swap.unrecognised is True


def test_the_report_for_an_unrankable_id_does_not_claim_the_provider_changed():
    """The verdict and the sentence have to agree. Reading "claude-opus-6 — from another
    provider entirely" in a failed run sends the reader looking for a routing bug that is
    not there, and the sentence must not claim it is *weaker* either: nothing here knows
    that."""
    swap = check_substitution("fable", {"claude-opus-6": {"inputTokens": 12}})

    assert swap is not None
    assert "another provider" not in swap.detail
    assert "weaker" not in swap.detail
    assert swap.detail == (
        "requested fable, ran claude-opus-6 — an id this engine does not recognise"
    )


@pytest.mark.parametrize("reported", ["gemini-9-pro", "llama-7", "mistral-large-3"])
def test_an_id_in_no_known_namespace_stays_fatal(reported: str):
    """The lenient reading is bounded by the vendor namespace and nothing wider. A name in
    neither `claude-` nor `gpt-` is not "a model newer than the table" — it is an invocation
    that reached something this engine has no account of."""
    swap = check_substitution("fable", {reported: {"inputTokens": 12}})

    assert swap is not None
    assert swap.cross_provider is True
    assert swap.fatal is True
    assert swap.unrecognised is False, "not unrecognised-within-a-provider; unplaceable"


def test_the_coarse_provider_table_is_derived_from_the_ranked_one():
    """`_PROVIDER_PREFIX` decides *placement* and `_FAMILY_RANK` decides *rank*, and the two
    can only disagree by drifting apart. Adding a provider to the rank table without a stem
    here would make every id of it unplaceable and fatal; a stem here that claims ids the
    rank table gives to somebody else would do the reverse, quietly."""
    for family, provider, _rank in _FAMILY_RANK:
        assert reported_provider(family) == provider
        stems = [stem for stem, _ in _PROVIDER_PREFIX if family.startswith(stem)]
        assert stems, f"{family} matches no provider stem"
        assert len(stems) == 1, f"{family} is claimed by more than one stem: {stems}"

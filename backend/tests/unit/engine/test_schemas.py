"""The role contracts, and the validator that enforces them on the way back in."""

from __future__ import annotations

import pytest

from app.engine.core import HYPOTHESIS_FIELD_ORDER, compose_hypothesis_md
from app.engine.schemas import (
    HYPOTHESIS_FIELDS,
    SCHEMA_BY_ROLE,
    TITLE_MAX_LENGTH,
    hypothesis_fields,
    schema_for,
    unwrap_prose,
    validate_role_output,
)


def a_hypothesis(**overrides):
    fields = {
        "title": "Sediment turnover sets the ceiling",
        "claim": "Turnover rate, not nutrient load, bounds the bloom.",
        "mechanism": "Resuspension returns phosphorus faster than uptake can consume it.",
        "novelty": "Existing models treat turnover as a constant.",
        "test": "Manipulate turnover in mesocosms at fixed nutrient load.",
        "assumptions": "Turnover can be varied without changing oxygenation.",
    }
    fields.update(overrides)
    return fields


# --- the contract itself ----------------------------------------------------------------


def test_hypothesis_fields_match_the_body_composer_exactly():
    # A field here that core does not compose would validate and then vanish from the body.
    assert tuple(HYPOTHESIS_FIELDS) == HYPOTHESIS_FIELD_ORDER
    assert compose_hypothesis_md(a_hypothesis()).count("**") == 2 * (len(HYPOTHESIS_FIELDS) - 1)


def test_every_role_the_engine_calls_has_a_schema():
    assert set(SCHEMA_BY_ROLE) == {
        "framing", "verification", "synthesis", "challenge",
        "generation",
        "reflection",
        "proximity",
        "ranking",
        "evolution",
        "meta_review",
        "overview",
        "cartographer",
        "workshop",
    }


def test_schema_for_names_the_unknown_role():
    with pytest.raises(KeyError, match="nonsense"):
        schema_for("nonsense")


# --- validation -------------------------------------------------------------------------


def test_a_well_formed_generation_payload_passes():
    assert validate_role_output("generation", {"hypotheses": [a_hypothesis()]}) is None


def test_a_missing_hypothesis_field_is_named_in_the_error():
    payload = {"hypotheses": [a_hypothesis()]}
    del payload["hypotheses"][0]["mechanism"]

    error = validate_role_output("generation", payload)

    assert error is not None
    assert "mechanism" in error


def test_an_empty_hypothesis_array_is_rejected():
    assert "at least 1" in validate_role_output("generation", {"hypotheses": []})


def test_no_structured_output_at_all_is_a_contract_violation():
    assert validate_role_output("generation", None) == "no structured output was returned"


def test_reflection_requires_the_nested_novelty_object():
    payload = {
        "verdict": "pass",
        "novelty": {"level": "sky-high"},
        "correctness": "fine",
        "testability": "fine",
        "key_risk": "none",
        "note": "fine",
    }

    error = validate_role_output("reflection", payload)

    assert "novelty is missing required field 'note'" in error
    assert "novelty.level must be one of" in error


def test_ranking_winner_is_a_position_not_a_boolean():
    assert validate_role_output("ranking", {"debate": "…", "winner": 2}) is None
    assert "winner" in validate_role_output("ranking", {"debate": "…", "winner": True})
    assert "winner" in validate_role_output("ranking", {"debate": "…", "winner": "1"})


def test_proximity_rejects_a_cluster_label_that_is_not_a_string():
    assert validate_role_output("proximity", {"clusters": {"h001": "energetics"}}) is None
    assert "h001" in validate_role_output("proximity", {"clusters": {"h001": 3}})


def test_evolution_requires_lineage():
    variant = {**a_hypothesis(), "derived_from": ["h001"], "operator": "combination"}
    assert validate_role_output("evolution", {"hypotheses": [variant]}) is None

    del variant["operator"]
    assert "operator" in validate_role_output("evolution", {"hypotheses": [variant]})


def test_evolution_operator_is_one_of_the_four_strategies():
    variant = {**a_hypothesis(), "derived_from": ["h001"], "operator": "vibes"}

    error = validate_role_output("evolution", {"hypotheses": [variant]})

    assert "grounding" in error and "out_of_box" in error


def test_workshop_demands_exactly_two_options():
    option = {
        "prompt": "…",
        "strategy": "Mechanism-first",
        "optimizes_for": "depth",
        "excludes": "breadth",
        "rationale": "…",
        "recommended_settings": {
            "rounds": 3,
            "budget_calls": 90,
            "matches_per_round": 6,
            "grounding_depth": "standard",
        },
    }
    assert validate_role_output("workshop", {"options": [option, option]}) is None
    assert "at least 2" in validate_role_output("workshop", {"options": [option]})
    assert "at most 2" in validate_role_output("workshop", {"options": [option] * 3})


def test_the_error_lists_several_problems_at_once():
    error = validate_role_output("meta_review", {})

    assert "recurring_issues" in error
    assert "what_wins" in error
    assert "guidance_next_round" in error


# --- field extraction -------------------------------------------------------------------


def test_hypothesis_fields_strips_and_clips_rather_than_failing_over_one_character():
    fields = hypothesis_fields(a_hypothesis(title="  " + "t" * (TITLE_MAX_LENGTH + 40) + "  "))

    assert len(fields["title"]) == TITLE_MAX_LENGTH
    assert fields["title"].endswith("…")
    assert set(fields) == set(HYPOTHESIS_FIELD_ORDER)


def test_hypothesis_fields_fills_a_missing_field_with_empty_string():
    fields = hypothesis_fields({"title": "only a title"})

    assert fields["claim"] == ""
    assert compose_hypothesis_md(fields).startswith("# only a title")


# --- reflection's prose fields ----------------------------------------------------------
#
# In the owner's real run c4566ed2 every one of these five fields was corrupted in every one
# of the six reviews, and every one validated cleanly. For h006 the whole critique was four
# fields each holding the literal two-character string `{}`, which was then rendered verbatim
# into both of its ranking prompts, into the meta-review, and into the GUI.


def a_review(**overrides):
    review = {
        "verdict": "pass",
        "novelty": {
            "level": "moderate",
            "note": "The framing is not the standard one for this system.",
        },
        "correctness": "No fundamental flaw; the causal story holds together as stated.",
        "testability": "The proposed perturbation discriminates against the obvious rival.",
        "key_risk": "The effect may be an artefact of how the quantity is measured.",
        "note": "Worth ranking: specific, testable, and not a restatement of the standard.",
    }
    review.update(overrides)
    return review


def test_a_healthy_reflection_still_validates():
    assert validate_role_output("reflection", a_review()) is None


@pytest.mark.parametrize("field", ["correctness", "testability", "key_risk", "note"])
def test_an_empty_critique_is_refused_rather_than_stored(field):
    """`{}` and `""` are strings, so a schema with no floor accepted both."""
    for empty in ("{}", "", "   ", "n/a"):
        error = validate_role_output("reflection", a_review(**{field: empty}))
        assert error is not None, f"{field}={empty!r} validated as a critique"
        assert field in error


@pytest.mark.parametrize(
    "value",
    [
        '{"summary":"No fundamental flaw; several verified anchors (METR, and others).xxx"}',
        '{"note":"The claim survives the correctness screen on the evidence given here."}',
    ],
)
def test_a_json_encoded_critique_is_refused_even_when_it_is_long_enough(value):
    """h001's correctness was exactly this shape and passed: long, a string, unusable."""
    error = validate_role_output("reflection", a_review(correctness=value))

    assert error is not None
    assert "prose" in error and "correctness" in error


def test_the_novelty_note_has_its_own_shorter_floor():
    assert validate_role_output("reflection", a_review(novelty={"level": "high", "note": "{}"}))
    assert (
        validate_role_output(
            "reflection",
            a_review(novelty={"level": "high", "note": "Nobody has framed it this way."}),
        )
        is None
    )


def test_unwrap_prose_recovers_the_sentence_from_an_accidental_wrapper():
    assert unwrap_prose('{"summary":"No fundamental flaw."}') == "No fundamental flaw."
    assert unwrap_prose('{"note":"Worth ranking."}') == "Worth ranking."
    assert unwrap_prose('"Strong on the capability leg."') == "Strong on the capability leg."
    assert unwrap_prose("{}") == ""
    assert unwrap_prose(None) == ""


def test_unwrap_prose_leaves_anything_it_cannot_be_sure_about_exactly_as_it_found_it():
    """Guessing at a two-key object would invent a critique. Plain prose is untouched."""
    plain = "No fundamental flaw; the causal story holds."
    assert unwrap_prose(plain) == plain
    ambiguous = '{"a":"one","b":"two"}'
    assert unwrap_prose(ambiguous) == ambiguous
    assert unwrap_prose('{"n":1}No fundamental flaw: it holds.') == (
        '{"n":1}No fundamental flaw: it holds.'
    )


def test_proximity_refuses_an_empty_cluster_map():
    """`{}` satisfied the old schema, and the orchestrator then filtered nothing to nothing."""
    assert validate_role_output("proximity", {"clusters": {}}) is not None
    assert validate_role_output("proximity", {"clusters": {"h001": "energetics"}}) is None

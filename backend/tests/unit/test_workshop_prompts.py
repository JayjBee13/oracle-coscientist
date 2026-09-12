"""The workshop's instructions: say what makes an answer good, never what the answer is.

Run `c4566ed2` is the regression fixture. The owner asked a deliberately wide question and
the workshop answered it with a goal that named six families of answer; the run returned six
hypotheses, one per family, in order. Nothing in the engine collapsed the idea space — this
prompt did, before the engine started.

Two detectors below encode the defect (`enumerated_answer_leads`, `out_of_scope_clause`).
They are test-owned on purpose: the service runs no heuristics over the model's text and is
not about to start. Their job here is to prove the fixture really is the defect, to
discriminate it from a criteria-first goal for the same question, and to keep the
prohibition in the system prompt honest — the leads the detector hunts for are exactly the
ones the prompt has to name.
"""

from __future__ import annotations

import re

import pytest

from app.services.workshop.prompts import (
    WORKSHOP_SYSTEM_PROMPT,
    question_openness,
    workshop_prompt,
)
from tests.fixtures.workshop_c4566ed2 import INVENTED_EXCLUSIONS, QUESTION, REWRITE

# A narrow question of the kind the workshop is genuinely allowed to sharpen.
FOCUSED_QUESTION = (
    "Which mechanisms link sleep fragmentation to metabolic dysfunction in adults "
    "without changing total sleep duration?"
)

# The same question as `QUESTION`, answered the way the rewritten instructions ask for:
# the bar an answer must clear, and not one member of the answer space.
CRITERIA_FIRST_REWRITE = (
    "A solo operator has $50,000, twelve months, no team and no outside funding, and can "
    "build and run fleets of coding and computer-use agents. Frontier capability and agent "
    "reliability improve substantially every three months, and the binding constraints are "
    "attention and distribution rather than capital. Find the highest-expected-value ways "
    "to turn that position into life-changing wealth, keeping a non-trivial probability of "
    "$10M inside twelve months.\n\n"
    "A hypothesis is taken seriously here only if it carries all of the following. It "
    "states who pays and the mechanism by which the money actually moves. It states the "
    "capability milestone it bets on, the observable that would confirm or deny that "
    "milestone by a named month, and what survives if capability stalls. It gives a "
    "first-90-day plan and the two-week test that would falsify it. It says why the edge is "
    "not already competed away and why it survives a frontier lab shipping the obvious "
    "feature. It states calibrated probabilities of $250k, $1M and $10M within twelve "
    "months with the reference class those numbers came from, and is honest that a 200x is "
    "rare. It names the failure modes that end it, including the ones that end it "
    "permanently rather than expensively.\n\n"
    "Rank on expected value adjusted for probability of ruin, then on the size of the "
    "achievable tail, then on speed to first revenue. Out of scope, and only because the "
    "scientist said so: generic index-fund advice, MLM and affiliate churn, work requiring "
    "credentials they do not have, anything illegal or fraudulent, and anything whose "
    "entire edge evaporates when a model provider ships an obvious feature. Nothing else is "
    "ruled out."
)

# Phrases that announce a list of candidate answers. The first three are illustration leads
# the system prompt has to name by hand (see `test_the_prohibition_names_every_lead_...`);
# the rest are the shapes the c4566ed2 rewrite actually used.
ILLUSTRATION_LEADS: tuple[str, ...] = (
    "such as",
    "for example",
    "the answer being sought is",
    # A list of fields to borrow from is the same enumeration in methodologist's clothing,
    # and on c4566ed2 it was the more damaging of the two: "insurance and liability
    # underwriting for autonomous work" appears in no other sentence of the goal, and the
    # run returned two of its six hypotheses (`h002`, `h005`) as guarantee books.
    "deliberately transfer reasoning from",
    "draw on",
    "borrow from",
    "take inspiration from",
    "apply the logic of",
)

_ANSWER_LEAD_RE = re.compile(
    r"the answers? being sought (?:is|are)"
    r"|candidate (?:answers|approaches|solutions)"
    r"|(?:answer|solution|business|approach) families"
    r"|(?:options|directions|approaches|candidates) include"
    r"|(?:deliberately )?transfer reasoning from"
    r"|draw(?:ing)? on|borrow(?:ing)? from|take inspiration from|apply the logic of"
    r"|such as|for example|e\.g\.",
    re.IGNORECASE,
)

_SENTENCE_END = re.compile(r"\n\n|(?<=[.!?])\s+(?=[A-Z])")

_OUT_OF_SCOPE_RE = re.compile(
    r"(?:out of scope|not in scope|deliberately out(?: of scope)?|excluded?)\b[:,]?\s*",
    re.IGNORECASE,
)


def enumerated_answer_leads(goal: str) -> list[str]:
    """Every place `goal` announces candidate answers and then lists three or more.

    The list requirement is what keeps this off legitimate prose: a goal may say "in
    systems such as shallow lakes" without enumerating an answer space. Three items after
    an announcing phrase is an enumeration.
    """
    found: list[str] = []
    for match in _ANSWER_LEAD_RE.finditer(goal):
        clause = _SENTENCE_END.split(goal[match.end() :])[0]
        items = [part.strip() for part in re.split(r"[;,]", clause) if part.strip()]
        if len(items) >= 3:
            found.append(match.group(0).lower())
    return found


def flat(text: str) -> str:
    """Lowercased, whitespace-collapsed: the prompts are hard-wrapped, phrases are not."""
    return re.sub(r"\s+", " ", text).strip().lower()


def out_of_scope_clause(goal: str) -> str:
    """What the goal rules out, as written — empty when it rules nothing out."""
    match = _OUT_OF_SCOPE_RE.search(goal)
    if match is None:
        return ""
    return _SENTENCE_END.split(goal[match.end() :])[0].strip()


# --------------------------------------------------------- the fixture is the defect


def test_the_historical_rewrite_enumerated_the_answer():
    """`c4566ed2`: the goal named the families, and the run returned them one per clause."""
    leads = enumerated_answer_leads(REWRITE)

    assert leads == ["the answer being sought is", "deliberately transfer reasoning from"]
    enumeration = REWRITE.split("The answer being sought is", 1)[1].split("\n\n", 1)[0]
    assert enumeration.count(";") >= 4, "the fixture must still carry the six-family list"


def test_the_historical_rewrite_also_enumerated_the_fields_to_borrow_from():
    """The second enumeration, and the one the first version of this fix did not forbid.

    "Deliberately transfer reasoning from AI-enabled roll-up economics, marketplace
    take-rate design, insurance and liability underwriting for autonomous work, and
    distribution economics" reads as methodology, not as an answer list — and it behaved
    exactly like one. Two of the run's six round-one hypotheses (`h002` "re-performance-
    capped guarantees", `h005` "correlation-aware guarantee book") are the underwriting
    family, and `insurance`, `underwriting`, `roll-up` and `take-rate` are words the owner
    never wrote. A prohibition that covers only the first sentence leaves a third of the
    run's answer space still dictated by the goal.
    """
    assert "deliberately transfer reasoning from" in enumerated_answer_leads(REWRITE)
    for invented in ("insurance", "underwriting", "roll-up", "take-rate"):
        assert invented not in QUESTION.lower(), f"{invented!r} is the owner's word after all"

    prompt = flat(WORKSHOP_SYSTEM_PROMPT)
    assert "the same prohibition covers the domains to borrow from" in prompt
    assert "never say which fields those are" in prompt


def test_the_historical_rewrite_invented_exclusions_the_scientist_never_wrote():
    clause = out_of_scope_clause(REWRITE)

    assert clause, "the fixture must still carry its out-of-scope list"
    for invented in INVENTED_EXCLUSIONS:
        assert invented in clause
    # Not a paraphrase of anything the owner wrote: the words are simply not in the brief.
    for absent in ("hourly", "consulting", "ceiling"):
        assert absent not in QUESTION.lower(), f"{absent!r} is the owner's word after all"


def test_a_criteria_first_goal_for_the_same_question_is_clean():
    """The detectors discriminate; they are not a rubber stamp that flags every goal."""
    assert enumerated_answer_leads(CRITERIA_FIRST_REWRITE) == []

    clause = out_of_scope_clause(CRITERIA_FIRST_REWRITE)
    assert clause, "a goal that carries the owner's own exclusions still has the clause"
    for invented in INVENTED_EXCLUSIONS:
        assert invented not in clause
    assert "because the scientist said so" in CRITERIA_FIRST_REWRITE


# ------------------------------------------------------- what the instructions now say


def test_the_goal_instructions_forbid_naming_the_answer():
    prompt = flat(WORKSHOP_SYSTEM_PROMPT)

    # The instruction that asked the model to name the answer, gone.
    assert "says what kind of answer counts" not in prompt
    assert "draws a boundary" not in prompt
    # ...replaced by the criteria, and by an explicit prohibition.
    assert "says what makes an answer good, never what the answer is" in prompt
    assert "never enumerates candidate answers" in prompt
    assert "naming the candidates is the run's job" in prompt


@pytest.mark.parametrize("lead", ILLUSTRATION_LEADS)
def test_the_prohibition_names_every_illustration_lead_the_detector_hunts_for(lead: str):
    """Detector and prohibition move together, or the prompt stops covering the defect."""
    assert lead in flat(WORKSHOP_SYSTEM_PROMPT)


def test_the_goal_instructions_carry_exclusions_forward_only():
    prompt = flat(WORKSHOP_SYSTEM_PROMPT)

    assert "only the constraints and exclusions the scientist actually stated" in prompt
    assert "invent none" in prompt
    assert "the scientist has ruled nothing out" in prompt
    # `excludes` is option metadata; the previous prompt let it read as licence to cut.
    assert "not a licence to rule answers out inside the goal text" in prompt


def test_the_length_rule_no_longer_forces_a_wide_goal_to_name_members():
    prompt = flat(WORKSHOP_SYSTEM_PROMPT)

    assert "is self-contained finished prose of roughly 120-350 words" not in prompt
    assert "a wide-open one is described by its criteria" in prompt


# --------------------------------------------------------------------- the breadth axis


def test_the_option_axes_include_a_breadth_axis():
    prompt = flat(WORKSHOP_SYSTEM_PROMPT)

    assert "one family of answer explored in depth vs. deliberately spanning unrelated" in prompt
    assert "one of the two options must be the wide one" in prompt
    assert "widening the criteria" in prompt


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (QUESTION, "open"),
        ("What should I do with a fully automated test lab and no fixed research agenda?", "open"),
        ("Which strategies keep a small fab profitable as nodes shrink?", "open"),
        (FOCUSED_QUESTION, "focused"),
        ("Why do some shallow lakes bloom when nutrient loading is falling?", "focused"),
        ("What is the mechanism of the anomalous Hall effect in this alloy?", "focused"),
        # Unmarked: neither a breadth request nor a named phenomenon to explain. Open, by
        # the asymmetry — a needlessly wide option costs half an A/B, a needlessly narrow
        # goal costs the whole run.
        ("Improve the cycle life of sodium-ion cathodes.", "open"),
    ],
)
def test_openness_reads_the_scientists_own_words(question: str, expected: str):
    assert question_openness(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        # The owner's question with the brief stripped off. This read `focused` — the
        # expensive error, on the exact shape of the documented failure — because
        # "how do" was a focus marker meant for "how does tau propagate between neurons".
        "How do I turn $50,000 and twelve months into life-changing wealth?",
        "How do I get my first thousand users?",
        "How do we grow revenue tenfold in a year?",
        "What do I do with a year of free compute?",
        "Where do I look for an edge nobody else has?",
        "Which should we back, given one engineer and no runway?",
    ],
)
def test_a_question_whose_subject_is_the_asker_is_a_question_about_what_to_do(question: str):
    """And a question about what to do has an open answer space until told otherwise."""
    assert question_openness(question) == "open"


def test_the_pronoun_rule_does_not_swallow_a_mechanism_question():
    """The marker it competes with still has to work: `how does X …` stays focused."""
    assert question_openness("How does tau propagate between neurons in early Alzheimer's?") == (
        "focused"
    )
    assert question_openness("How do circadian genes gate hepatic lipogenesis?") == "focused"


def test_an_open_question_requires_one_wide_option():
    prompt = flat(workshop_prompt(QUESTION))

    assert "breadth of this question: open" in prompt
    assert "one of the two options must be the wide one" in prompt
    assert "span unrelated families of answer rather than to develop one" in prompt
    assert "neither goal may name the candidate answers" in prompt


def test_a_focused_question_is_sharpened_rather_than_widened():
    prompt = flat(workshop_prompt(FOCUSED_QUESTION))

    assert "breadth of this question: focused" in prompt
    assert "one of the two options must be the wide one" not in prompt
    assert "sharpen the criteria, never the list of candidate answers" in prompt
    # The escape hatch runs one way only: the model may widen, never narrow.
    assert "treat the question as open instead" in prompt


def test_the_open_block_resolves_the_depth_options_own_contradiction():
    """"One family in depth" is an offered axis; "name no families" is a hard rule.

    Left as they were, an open question told the model to make one option deliberately wide
    and left the counterweight with only one obvious way to be narrow: pick the family. The
    block now says how to narrow without doing that, and says that a commitment to one
    family belongs in `strategy` — which the scientist reads before choosing — rather than
    arriving unannounced inside the goal.
    """
    prompt = flat(workshop_prompt(QUESTION))

    assert "the other option is narrowed by raising the bar" in prompt
    assert "not by picking the run's answer for it" in prompt
    assert "stated in its `strategy`" in prompt


@pytest.mark.parametrize("question", [QUESTION, FOCUSED_QUESTION])
def test_both_breadth_blocks_forbid_naming_the_candidates(question: str):
    """A misclassification must never be able to re-create the c4566ed2 defect."""
    prompt = flat(workshop_prompt(question))

    assert "candidate answers" in prompt
    assert "only the exclusions the scientist wrote" in prompt
    assert "they ruled nothing out" in prompt


@pytest.mark.parametrize("question", [QUESTION, FOCUSED_QUESTION])
def test_both_breadth_blocks_forbid_naming_the_fields_to_borrow_from(question: str):
    """The disguised enumeration is barred in both regimes, as the plain one already was."""
    prompt = flat(workshop_prompt(question))

    assert "transfer reasoning from" in prompt


def test_the_breadth_block_survives_a_refinement():
    """A refine that dropped it would let the second pair come back narrower than the first."""
    prompt = flat(
        workshop_prompt(
            QUESTION,
            base="merge",
            note="Both were too close together.",
            history=[
                {"strategy": "Roll-up economics", "prompt": "Acquire a services business."}
            ],
        )
    )

    assert "breadth of this question: open" in prompt
    assert "one of the two options must be the wide one" in prompt

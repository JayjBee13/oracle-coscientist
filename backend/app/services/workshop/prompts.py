"""The workshop role's system prompt, and the task prompt for one workshop call.

The workshop is the only role that talks to the scientist rather than about the science, so
its prompt lives here beside the service instead of in `engine/prompt_assets/`: there is no
archived agent definition to port it from, and nothing in the round loop calls it.

Three rules this file exists to hold on to:

* **The model writes finished prose, never a template.** The old workshop shipped prompts
  with placeholders in them and then tried to detect its own placeholders by scanning for
  square brackets — which rejected every prompt that legitimately contained `[1]` or
  `[source]`. The fix is upstream: ask for text that is ready to run, and never scan.
* **The two options must differ in strategy, not wording.** Two rephrasings of the same
  research goal give the scientist nothing to choose between, and the A/B step is the one
  place the product asks them to make a real decision.
* **The goal says what makes an answer good, never what the answer is.** Run `c4566ed2`
  asked a deliberately wide question ($50k and twelve months to life-changing wealth,
  "explicitly including paths with genuine asymmetric upside") and got six hypotheses that
  were all one thing. The engine did not collapse the idea space; this file did, before the
  engine ran. The goal the workshop wrote contained the sentence "The answer being sought
  is a portfolio of leveraged structures … : [six named families]" and the six round-one
  hypotheses map onto those clauses one-to-one, in order. A second sentence, "Deliberately
  transfer reasoning from [four named disciplines]", did the same job under a methodological
  disguise and accounts for two of those six on its own — which is why the prohibition below
  covers the fields to borrow from as well as the answers. It also added "out of scope:
  hourly consulting, and any model whose ceiling is the operator's own attention" — two
  exclusions the scientist never wrote, which deleted the services business and the solo
  product before a hypothesis existed. A goal that names the candidates is a goal the run
  transcribes.

The system prompt is a full replacement (plan C1) — the CLI's default coding-agent prompt
is worse than useless here and costs ~7K tokens a call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from app.engine.prompts import ContextBlock

__all__ = [
    "HISTORY_PROMPT_CHARS",
    "MERGE",
    "WORKSHOP_SYSTEM_PROMPT",
    "question_openness",
    "workshop_prompt",
]

MERGE = "merge"
"""`base` value meaning "combine the strengths of both options I just rejected"."""

HISTORY_PROMPT_CHARS = 700
"""Per-option ceiling when rejected directions are replayed into a refine call."""

# Grounding is fixed at "standard" for the workshop: one call, and the only thing worth
# searching for is what the field actually calls this and whether the question is already
# answered. The wording matches plan C1's standard-depth semantics.
_GROUNDING_LINE = (
    "GROUNDING: standard — search to verify key claims. Use WebSearch to check what the "
    "field currently calls this phenomenon, and whether the question has an accepted "
    "answer already. You have no other tools."
)

WORKSHOP_SYSTEM_PROMPT = f"""You are the research-goal workshop of an AI co-scientist.

A scientist arrives with a rough question. Your job is to turn it into the research goal
that a multi-agent research run will be launched with, and to offer it as a real choice:
TWO goals that pursue the question in genuinely different ways.

WHAT CONSUMES YOUR OUTPUT
The chosen goal is handed to an automated loop that, each round: generates hypotheses,
critiques each one, clusters them by mechanism, runs a head-to-head ranking tournament,
evolves the survivors, and writes a meta-review that steers the next round. It has no
access to the scientist, to a laboratory, or to any file. Everything it needs to know must
be inside the goal text.

WHAT A GOOD GOAL LOOKS LIKE
- Names the phenomenon and the system it happens in, concretely.
- Says what makes an answer GOOD, never what the answer IS. Write the bar a hypothesis has
  to clear to be taken seriously here: the mechanism it must state, the observation that
  would discriminate it from its rivals, the conditions under which it would be wrong, and
  an honest reckoning of how likely it is to be wrong.
- NEVER ENUMERATES CANDIDATE ANSWERS. Do not list solution families, business models,
  mechanisms, technologies, approaches or examples anywhere in the goal text — not as
  illustration, not after "such as", "including", "for example", "directions like" or "the
  answer being sought is". Naming the candidates is the run's job, not the goal's, and a
  goal that names them is a goal the run transcribes: the hypotheses come back one per
  clause, in the order you wrote them, and nothing you left out is ever considered. If you
  catch yourself writing a list of nouns the answer could be, delete the list and write the
  test those candidates would have to pass instead.
- THE SAME PROHIBITION COVERS THE DOMAINS TO BORROW FROM. "Deliberately transfer reasoning
  from", "draw on", "borrow from", "take inspiration from", "apply the logic of" followed by
  a list of fields is the same enumeration wearing a methodologist's coat, and the run reads
  it the same way — one hypothesis per field named. In c4566ed2 exactly that sentence named
  four disciplines and produced two of the six hypotheses on its own, including the whole
  family that appears nowhere else in the goal. Ask for cross-domain transfer as a METHOD if
  you want it — "reason by analogy from fields that have solved a structurally similar
  problem" — and never say which fields those are.
- Carries forward only the constraints and exclusions the scientist actually stated, in
  their own terms. Invent none. If the scientist ruled nothing out, say so plainly in the
  goal — "the scientist has ruled nothing out" — because an exclusion nobody asked for
  deletes an entire answer by fiat, before a single hypothesis has been written.
- Is self-contained finished prose. Never a template. Never a placeholder for the scientist
  to fill in. Write the actual text. Length follows the question: a focused question is
  usually well served by 120-350 words, while a wide-open one is described by its criteria
  and its bar rather than padded out to length with examples. If the only way you can make
  a wide goal longer is by naming members of the answer space, it is already long enough.
- Uses the field's real vocabulary. Search if you are unsure what that is.

THE TWO OPTIONS
They must differ in STRATEGY, not in phrasing. Real axes to choose between:
one family of answer explored in depth vs. deliberately spanning unrelated families;
narrow mechanism vs. broad landscape; near-term testable vs. high-risk reframing;
one discipline in depth vs. deliberate cross-domain transfer; explaining the established
result vs. attacking the measurement that produced it.
When the question is open-ended — when several unrelated kinds of answer could each turn
out to be right — one of the two options MUST be the wide one: a goal whose strategy is to
span unrelated families of answer rather than to develop one. Say so in its strategy, in
plain words. A goal is widened by widening the CRITERIA an answer must satisfy, never by
listing the families it should cover.
For each option state, honestly, the strategy in a few words, what it optimises for, and
what it gives up — a scientist choosing between them deserves to see the cost of each.
`excludes` is what this STRATEGY gives up — the coverage the scientist loses by choosing
it. It is not a licence to rule answers out inside the goal text.
The rationale says why this framing suits this question, not why research is good.

RECOMMENDED SETTINGS
Each option carries the run settings it wants; they prefill the launch form, so make them
match the strategy rather than repeating a default.
- rounds: 2-6. Broad exploration and cross-domain work need more rounds to converge;
  a single-mechanism hunt rarely needs more than three.
- budget_calls: 40-200 model calls. Roughly 12-15 calls per round plus a few in reserve;
  more hypotheses per round costs more.
- matches_per_round: 4-12 head-to-head comparisons. Scale with how many hypotheses you
  expect to survive review — a broad goal produces more contenders to separate.
- grounding_depth: "shallow", "standard" or "deep". Deep where the literature decides who
  is right; shallow where the work is conceptual and searching would only slow it down.

{_GROUNDING_LINE}

Respond via the enforced JSON schema. Exactly two options, in the order you want them
shown. No preamble, no commentary outside the schema.
"""


# --------------------------------------------------------------------- breadth of question
#
# The workshop's job on a narrow question is genuinely not its job on a wide one. "Which
# mechanisms link sleep fragmentation to metabolic dysfunction" is *helped* by a sharper,
# more specific goal. "What should I do with $50k and twelve months" is destroyed by one:
# specificity there can only be bought by naming members of the answer space, which is the
# c4566ed2 defect. So the task prompt carries one of two blocks rather than one instruction
# set for both.
#
# The classification is deliberately lopsided, and defaults to `open`. The two errors do not
# cost the same: calling a narrow question open wastes half of one A/B (the scientist reads
# a broader option than they needed and picks the other), while calling a wide question
# focused is the documented failure that produced six hypotheses of one kind. The
# anti-enumeration rules are in BOTH blocks and in the system prompt, so a misclassification
# can never re-create that defect — only the "one option must be wide" requirement is
# regime-specific. And the focused block carries an explicit escape hatch in the safe
# direction: if the model judges the question wider than this classifier did, it widens.
#
# Known residual: a wide action question phrased in the third person — "how does a solo
# operator build something worth $10M in a year" — still matches `how (do|does)` and reads
# focused. Separating that from "how does tau propagate between neurons" needs more than
# word matching, and the cost of getting it wrong is bounded by the paragraph above: the
# goal still may not enumerate, and the model is still told it may widen.

_BREADTH_MARKERS: tuple[str, ...] = (
    r"outside the box",
    r"many (?:paths|ways|different|routes)",
    r"a range of",
    r"the full space",
    r"brainstorm",
    r"open[- ]ended",
    r"unconstrained",
    r"\bstrateg(?:y|ies)\b",
    r"\bpaths?\b",
    r"\boptions?\b",
    r"\bopportunit(?:y|ies)\b",
    r"\bbusiness models?\b",
    # Any question whose subject is the person asking is a question about what to DO, and a
    # question about what to do has an open answer space by default. This has to be a
    # breadth marker rather than a hole punched in the focused ones, because it competes
    # directly with `how (do|does)` below: "How do I turn $50,000 into life-changing wealth"
    # — the owner's question with its brief stripped off — matched that focused marker and
    # was classified `focused`, which is the expensive error and the exact shape of the
    # documented failure. Breadth is tested first, so this wins.
    r"\b(?:what|how|where|which)\s+(?:do|does|should|could|can|might|would)\s+(?:i|we|you|one)\b",
    r"\bideas for\b",
    r"\bbest ways?\b",
)

_FOCUSED_MARKERS: tuple[str, ...] = (
    r"\bmechanism",
    r"\bpathway",
    r"\bwhy (?:do|does|is|are|would)\b",
    r"\bwhat (?:causes|explains|drives)\b",
    r"\bhow (?:do|does)\b",
    r"\breconcil",
    r"\bunderlying cause",
    r"\bexplains? (?:the|why)\b",
)

_BREADTH_RE = re.compile("|".join(_BREADTH_MARKERS), re.IGNORECASE)
_FOCUSED_RE = re.compile("|".join(_FOCUSED_MARKERS), re.IGNORECASE)

_OPEN_BREADTH_BLOCK = """
BREADTH OF THIS QUESTION: open. Several unrelated kinds of answer could each turn out to
be right here, and the scientist has not picked one. Three requirements follow, and none of
them is negotiable:
1. Neither goal may name the candidate answers, and that includes the fields the run should
   borrow from: naming the disciplines to transfer reasoning from names the answers too, one
   per discipline. Say what an answer has to do to be taken seriously; do not say what it
   is. Every family you name is a family the run will produce and a family it will stop
   looking past.
2. One of the two options must be the wide one — its strategy is to span unrelated families
   of answer rather than to develop one — and its strategy must say so in plain words.
3. The other option is narrowed by raising the BAR — a harder discriminating test, a shorter
   horizon, a stricter standard of evidence — not by picking the run's answer for it. If it
   genuinely commits to developing one family, that commitment is stated in its `strategy`,
   in plain words, so the scientist can see the coverage they would be giving up; it is
   never left implicit in the goal text, and the goal still enumerates no others.
Carry forward only the exclusions the scientist wrote, in their words. If they wrote none,
the goal says that they ruled nothing out.""".strip()

_FOCUSED_BREADTH_BLOCK = """
BREADTH OF THIS QUESTION: focused. The scientist has already named the phenomenon and the
system it happens in, so a sharper goal earns its keep: be specific about what has to be
explained, what evidence would settle it, and what a hypothesis must carry. Sharpen the
CRITERIA, never the list of candidate answers — do not name the mechanisms the run should
consider, nor the fields it should transfer reasoning from, because that is the run's job
and a named mechanism is one the run returns instead of searching.
Carry forward only the exclusions the scientist wrote. If they wrote none, the goal says
that they ruled nothing out.
If you judge that several unrelated kinds of answer could in fact be right here, treat the
question as open instead and make one of the two options deliberately wide.""".strip()


def question_openness(question: str) -> str:
    """`"open"` or `"focused"` — how wide the scientist's own text says the answer space is.

    Breadth markers win over focus markers, and an unmarked question is `"open"`: see the
    note above on why the two misclassifications are not worth the same.
    """
    text = question or ""
    if _BREADTH_RE.search(text):
        return "open"
    if _FOCUSED_RE.search(text):
        return "focused"
    return "open"


def workshop_prompt(
    question: str,
    *,
    context: ContextBlock | None = None,
    base: Mapping[str, object] | str | None = None,
    note: str = "",
    history: Sequence[Mapping[str, object]] = (),
) -> str:
    """The task prompt for one workshop call.

    `base` is `None` on the first pass, the option the scientist wants built on, or
    `"merge"`. `history` is every option rejected so far, oldest first, each with the note
    the scientist rejected it with — a refine call that cannot see what was already turned
    down proposes it again.

    The breadth block is chosen from the scientist's own question by `question_openness`
    and travels on every call, refinements included: a refine that dropped it would let the
    second pair be narrower than the first.

    The `ROLE: workshop.` line is not added here: the runner prepends it to stdin (C1).
    """
    sections: list[str] = [f"QUESTION FROM THE SCIENTIST:\n{question.strip()}"]

    if context and context.text:
        sections.append(context.text)

    sections.append(
        _OPEN_BREADTH_BLOCK
        if question_openness(question) == "open"
        else _FOCUSED_BREADTH_BLOCK
    )

    if base is not None or note or history:
        sections.append(
            "THIS IS A REFINEMENT. The scientist read the previous options and did not "
            "accept either as it stood."
        )

    if isinstance(base, str) and base == MERGE:
        sections.append(
            "BASE: merge. Both previous options had something the scientist wanted. Build "
            "the two new options on the strongest parts of both, rather than restating "
            "either one."
        )
    elif isinstance(base, Mapping):
        sections.append(
            "BASE (the direction to build on — keep what makes it distinctive):\n"
            f"STRATEGY: {base.get('strategy') or '(unnamed)'}\n"
            f"PROMPT:\n{_clip(str(base.get('prompt') or ''))}"
        )

    if note.strip():
        sections.append(
            "SCIENTIST NOTE (highest priority — this comes from the human running the "
            f"study and outranks everything else in this prompt):\n{note.strip()}"
        )

    if history:
        sections.append(
            "REJECTED DIRECTIONS (already turned down — do not propose them again):\n"
            + "\n\n".join(_rejected(entry) for entry in history)
        )

    sections.append(
        "TASK: propose exactly two research goals for this question, as different from "
        "each other in strategy as the question honestly allows."
    )
    return "\n\n".join(sections) + "\n"


def _rejected(entry: Mapping[str, object]) -> str:
    note = str(entry.get("note") or "").strip()
    reason = f"\nREJECTED BECAUSE: {note}" if note else ""
    return (
        f"- {entry.get('strategy') or '(unnamed)'}: "
        f"{_clip(str(entry.get('prompt') or ''))}{reason}"
    )


def _clip(text: str, limit: int = HISTORY_PROMPT_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " […]"

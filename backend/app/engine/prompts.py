"""Role prompts: the ported system prompts, and the task prompt for each call.

Two halves.

**System prompts** live as markdown in `prompt_assets/`, ported from the archived agent
definitions (`archive/engine-source/v1/.claude/agents/*.md`, plus v2's cartographer) under
the rules in plan C2:

* The YAML front matter goes — it configured a Claude Code subagent, and these are now
  full `--system-prompt` replacements.
* Every output-format block is replaced by "respond via the enforced JSON schema", because
  the schema is enforced by the CLI and parsing prose is what made the archived runs
  undebuggable.
* Each Perplexity-versus-built-in conditional paragraph is **replaced** — not appended to —
  by a single grounding line rendered from the run's grounding depth. Perplexity is out of
  scope, and a prompt describing tools the call does not have wastes tokens and invites the
  model to claim it searched when it could not.
* The passages listed in `PRESERVED_PASSAGES` are carried over verbatim: they are the parts
  that make the roles work, and `tests/unit/engine/test_prompts.py` diffs them against the
  archived originals on every run.

**Task prompts** are composed here from run state. They begin `ROLE: <name>.` (C1) and are
delivered on stdin, never argv. The orchestrator owns what goes in them; this module owns
how it is laid out, so prompt shape can be tested without a database.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from app.engine.runners import tools_for

__all__ = [
    "CONTEXT_CHAR_CAP",
    "DIRECTIVE_ORDER",
    "EXPLORATION_DIRECTIVES",
    "GROUNDING_DEPTHS",
    "META_REVIEW_CHAR_BUDGET",
    "OVERVIEW_CHAR_BUDGET",
    "OVERVIEW_DEBATE_BUDGET",
    "PRESERVED_PASSAGES",
    "TRAJECTORY_FLOOR",
    "ContextBlock",
    "asset_for_role",
    "build_context_block",
    "cartographer_prompt",
    "evolution_prompt",
    "exploration_directive",
    "generation_prompt",
    "meta_review_prompt",
    "overview_prompt",
    "proximity_prompt",
    "ranking_prompt",
    "reflection_prompt",
    "render_system_prompt",
    "repair_suffix",
]

log = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).parent / "prompt_assets"
GROUNDING_TOKEN = "{{GROUNDING}}"

CONTEXT_CHAR_CAP = 20_000
"""Characters of scientist-supplied context documents per call (plan W1)."""

META_REVIEW_CHAR_BUDGET = 60_000
"""Soft ceiling for the meta-review prompt. Debates are dropped to fit; reviews never are."""

OVERVIEW_CHAR_BUDGET = 60_000
"""Soft ceiling for the overview prompt, which used to have none at all.

The guidance trajectory is what gives way — it is the section the reader of the report
needs least, and the one that scales with rounds. Composed from live data, the real
one-round prompt for run c4566ed2 is already 61,208 characters; replaying the same guidance
across five rounds gives 98,320, of which 46,466 is trajectory."""

TRAJECTORY_FLOOR = 20_000
"""What the guidance trajectory keeps even when the rest of the prompt has eaten the budget.

`OVERVIEW_CHAR_BUDGET` is applied to one section only, and on a real run the sections it is
*not* applied to already exceed it on their own — the recomposed c4566ed2 prompt is 102,831
characters of which the fixed part is ~73,000. `char_budget - len(fixed)` therefore goes
negative, clamps to zero, and the trajectory is cut unconditionally however short it is: the
budget never bounded the prompt, it only ever destroyed guidance. This floor is the
allowance the trajectory keeps regardless, so the truncation fires on the trajectory's own
size rather than on its neighbours'."""

OVERVIEW_DEBATE_BUDGET = 20_000
"""Characters of judge debate the report author is shown, strongest match first.

The overview used to receive no debates at all while its own instructions asked it to say
"why it ranked highly" per top hypothesis — the only ranking rationale in the payload was an
Elo integer and the meta-review's second-hand summary of the same matches."""

GROUNDING_DEPTHS: tuple[str, ...] = ("shallow", "standard", "deep")

# C1's grounding semantics, rendered into the one line that replaced the Perplexity
# paragraphs. Depth is a per-run setting, so the system prompt is rendered per run.
_GROUNDING_LINES: dict[str, str] = {
    "shallow": (
        "GROUNDING: shallow — at most 1 search, and only if a claim genuinely turns on it. "
        "Use WebSearch; you have no other tools."
    ),
    "standard": (
        "GROUNDING: standard — search to verify key claims. Use WebSearch; you have no "
        "other tools."
    ),
    "deep": (
        "GROUNDING: deep — search thoroughly; verify each major claim independently. "
        "Use WebSearch; you have no other tools."
    ),
}

_NO_TOOLS_LINE = (
    "GROUNDING: none — you have no tools on this call. Reason from what you already know, "
    "and say plainly when a claim would need checking."
)

_SOURCE_SELECTION = """SOURCE SELECTION: Use internal knowledge to reason, identify relevant
literature and formulate searches; do not treat recalled citations or uncertain claims as
verified evidence. Ask whether this particular claim needs published scholarly research.
For empirical scientific questions, actively search peer-reviewed journal articles,
systematic reviews and relevant conference papers, including disconfirming results.
For implementation, current facts or institutional requirements, seek the appropriate
official documentation, standards, original datasets, filings or primary reporting.
Use the general WebSearch tool for both scholarly and other sources: targeted queries
can include paper titles, authors, DOI terms, journal names or relevant publisher and
repository domains. Distinguish peer-reviewed papers from preprints and commentary;
record that distinction and whether only an abstract or snippet was accessible in the
source finding or review rationale. Never claim to have read an inaccessible full text.
Publication prestige and search rank do not establish a claim. Prefer relevant methods,
independent corroboration and counterevidence over filling a source quota. Search depth
and tool limits still apply; a source plan does not authorize extra searches or tools."""


def source_strategy_prompt(strategy: dict | None) -> str:
    """A persisted topic-specific search emphasis, not measured or enforced source counts."""
    if not strategy:
        return ""
    share = strategy["published_research_percent"]
    return (
        f"\n\nTOPIC SOURCE STRATEGY: approximately {share}% published scholarly research / "
        f"{100 - share}% other authoritative web sources, across this run's search effort.\n"
        f"Reason: {strategy['rationale']}\n"
        f"Scholarly query starting points: {strategy['scholarly_queries']}\n"
        f"Other source query starting points: {strategy['other_source_queries']}\n"
        "Choose sources for the claim at hand; a narrow call need not use both categories. "
        "Adapt when evidence warrants it and explain consequential departures within the "
        "role's existing output fields (such as mechanism, review rationale, source findings "
        "or open questions); do not add schema fields. These are planned priorities, "
        "not actual source counts. Internal knowledge is not part of the percentages."
    )

# The three angles a sharded generation batch covers. One directive per shard, cycled, so
# ceil(batch/3) parallel calls explore three different levels of analysis instead of
# producing three variants of the same idea.
#
# Each directive carries a second sentence for goals that ask what to DO rather than what
# is true. Read as prose on a strategy question, the original 0 and 1 were not orthogonal:
# both asked for the machinery of the same kind of answer, one zoomed in and one zoomed
# out, and directive 1's vocabulary ("aggregate structure, distributions, feedback between
# components") steers a business question straight at market structure. The owner's run
# came back six-for-six on B2B market microstructure with 0 and 1 as its only two angles.
# The added sentences change what *kind* of thing each shard is allowed to return, which is
# the axis a wide question needs and the one none of the three used to name.
EXPLORATION_DIRECTIVES: tuple[str, str, str] = (
    "Mechanism level. Propose hypotheses about the specific causal machinery — what acts "
    "on what, in what order, at what scale. Name parts and interactions. If the goal asks "
    "what to do rather than what is true, this is the step-by-step account of how the "
    "outcome is actually produced by one actor.",
    "Population or system level. Propose hypotheses about aggregate structure, "
    "distributions, feedback between components, or effects that exist only at scale and "
    "vanish when you look at a single unit. If the goal asks what to do rather than what "
    "is true, do NOT return the mechanism-level answer at a larger size: change who acts "
    "and what resource they act on — a different kind of agent, a different asset, a "
    "different counterparty — so your answers could not be mistaken for the other shard's.",
    "Adversarial reframe. Attack the framing itself: what if the accepted direction of "
    "causation is backwards, the effect is an artefact of how it is measured, or the "
    "phenomenon is better described as something else entirely? If the goal asks what to "
    "do rather than what is true, attack the goal's own premises: what if the stated "
    "constraint is not the binding one, the stated objective is the wrong thing to "
    "maximise, or the obvious route is crowded precisely because it is obvious?",
)

DIRECTIVE_ORDER: tuple[int, ...] = (0, 2, 1)
"""The order shards claim directives in, which is not the order the directives are written.

Shards are `ceil(batch/3)`, so at every batch size up to 6 — the setting 25 of 29 real runs
used — a round has exactly two shards and gets the first two entries of this sequence.
Written order gave those two rounds directives 0 and 1: two scale settings inside one
frame, with the only directive that attacks the framing never firing in the round that sets
the run's ceiling. Ordering the *claim* rather than the list gives a two-shard round 0 and
2, and leaves the directives themselves in their readable order for everything else."""


def exploration_directive(shard_index: int, round_number: int) -> str:
    """The directive shard `shard_index` explores in round `round_number` (1-based).

    Rotated by round as well as by shard, so a run whose batch only ever affords one or two
    shards still reaches every angle within three rounds instead of being permanently short
    of the same one.
    """
    slot = DIRECTIVE_ORDER[(shard_index + round_number - 1) % len(DIRECTIVE_ORDER)]
    return EXPLORATION_DIRECTIVES[slot]

# Passages that must survive the port unchanged, and the archived file each came from.
# Checked against the archive by test_prompts.py — the port is only trustworthy if the
# parts that carry the roles' judgement are provably identical.
PRESERVED_PASSAGES: dict[str, tuple[str, ...]] = {
    "reflection": (
        "Reject only for a fundamental flaw, non-novelty (if the goal demands novelty), or\n"
        "untestability. Otherwise pass, even if imperfect — weak-but-valid ideas get ranked, not\n"
        "discarded.",
    ),
    "ranking": (
        "1. Correctness / plausibility\n2. Novelty\n3. Testability and practicality\n"
        "4. Potential impact if true",
        "Ignore any numeric scores inside the reviews — they are not comparable across "
        "hypotheses.",
    ),
    "proximity": (
        "Cluster by underlying mechanism/idea — not surface wording. Two hypotheses share a "
        "cluster\nonly if confirming one would essentially confirm the other. Distinct "
        "mechanisms that target\nthe same goal are DIFFERENT clusters; preserving that "
        "diversity is the point.",
    ),
    "evolution": (
        "- **Grounding:** fix a weakness by pulling in specific evidence.",
        "- **Combination:** merge the best parts of two top hypotheses into a stronger one.",
        "- **Simplification:** strip a hypothesis to its most testable core.",
        "- **Out-of-the-box:** use a top idea only as analogical inspiration to leap somewhere new",
    ),
    "meta_review": ("== MODE: feedback ==", "== MODE: overview =="),
    "cartographer": (
        "far enough to\n   force genuinely new structure, but not so alien that no mapping "
        "exists (avoid the\n   maximize-distance trap; aim for a domain with a rich, "
        "transferable relational structure).",
    ),
}

# Where each ported asset came from, relative to `archive/engine-source/`.
ARCHIVE_ORIGINALS: dict[str, str] = {
    "generation": "v1/.claude/agents/generation.md",
    "reflection": "v1/.claude/agents/reflection.md",
    "proximity": "v1/.claude/agents/proximity.md",
    "ranking": "v1/.claude/agents/ranking.md",
    "evolution": "v1/.claude/agents/evolution.md",
    "meta_review": "v1/.claude/agents/meta-review.md",
    "cartographer": "v2/.claude/agents/cartographer.md",
}

# `overview` is the meta-review agent in its second mode — same instructions, different
# model and effort (C1), so it is its own role with its own budget line.
_ASSET_BY_ROLE: dict[str, str] = {
    "framing": "framing",
    "verification": "verification",
    "synthesis": "synthesis",
    "challenge": "challenge",
    "generation": "generation",
    "reflection": "reflection",
    "proximity": "proximity",
    "ranking": "ranking",
    "evolution": "evolution",
    "meta_review": "meta_review",
    "overview": "meta_review",
    "cartographer": "cartographer",
}


def asset_for_role(role: str) -> str:
    try:
        return _ASSET_BY_ROLE[role]
    except KeyError:
        raise KeyError(f"no prompt asset for role {role!r}") from None


@cache
def load_asset(name: str) -> str:
    path = ASSET_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing prompt asset {path}")
    return path.read_text(encoding="utf-8")


def render_system_prompt(
    role: str, *, grounding_depth: str = "standard", body: str | None = None
) -> str:
    """The full `--system-prompt` for a role, with its grounding line resolved.

    Tool-less roles get an explicit "you have no tools" line in the same slot: the depth
    setting is meaningless to them, and a prompt that implies they can search invites an
    invented citation.
    """
    body = load_asset(asset_for_role(role)) if body is None else body
    if GROUNDING_TOKEN not in body:
        return body
    if not tools_for(role):
        line = _NO_TOOLS_LINE
    elif grounding_depth not in _GROUNDING_LINES:
        raise ValueError(
            f"unknown grounding depth {grounding_depth!r}; expected one of {GROUNDING_DEPTHS}"
        )
    else:
        line = _GROUNDING_LINES[grounding_depth] + "\n\n" + _SOURCE_SELECTION
    return body.replace(GROUNDING_TOKEN, line)


def repair_suffix(error: str) -> str:
    """Appended to the prompt for the single repair re-ask after a contract violation."""
    return (
        "\n\nYOUR PREVIOUS RESPONSE DID NOT MATCH THE REQUIRED SCHEMA:\n"
        f"{error}\n"
        "Return the same content, corrected to satisfy the schema exactly. "
        "Do not add fields and do not drop required ones."
    )


# ------------------------------------------------------------------- context documents


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """The CONTEXT DOCUMENTS section, and what had to be cut to fit."""

    text: str = ""
    included: tuple[str, ...] = ()
    truncated: tuple[str, ...] = ()
    omitted: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.text)

    @property
    def lossy(self) -> bool:
        """Whether anything the scientist attached did not reach the call intact."""
        return bool(self.truncated or self.omitted)

    def delivery(self) -> dict[str, str]:
        """Per-document delivery state, for the surface the scientist actually reads."""
        states = {name: "full" for name in self.included}
        states.update({name: "truncated" for name in self.truncated})
        states.update({name: "omitted" for name in self.omitted})
        return states


_DOC_TRUNCATION_MARKER = "\n… [document truncated to fit the context cap]"

_CONTEXT_HEADING = "CONTEXT DOCUMENTS (supplied by the scientist for this run):\n"

_MIN_DOC_SHARE = 500
"""Below this many characters a document's share is not worth spending — it is omitted."""


def build_context_block(
    docs: Sequence[Mapping[str, Any]], *, cap: int = CONTEXT_CHAR_CAP
) -> ContextBlock:
    """Inline the scientist's context documents, capped at `cap` characters per call.

    Documents are inlined as bytes — nothing here ever becomes a path the model could read,
    because the model has no filesystem tools. Over the cap, a document is cut with a
    marker rather than dropped silently, and what was cut is reported back to the caller,
    which puts it on the run so the scientist can see it rather than only in the log.

    Allocation is a **share per document**, not first-fit. The product accepts five
    documents of 200,000 characters each and delivers 20,000 to any one call; under greedy
    first-fit the first document ate the entire cap and documents two onward were dropped
    whole, so a scientist who attached a protocol and their priors sent only the protocol
    and was told nothing. Every document now gets `cap / n` to start with, and the headroom
    the short ones do not use is redistributed to the long ones — so a small file is always
    delivered in full, and a large one is cut rather than deleted.

    The `--- name (N chars) ---` header is charged against the cap too. It was not, which
    is why a 20,000-character cap produced a 20,141-character block.
    """
    if not docs:
        return ContextBlock()

    entries = [
        (str(doc.get("name") or "document"), str(doc.get("content") or doc.get("text") or ""))
        for doc in docs
    ]
    entries = [(name, content) for name, content in entries if content]
    if not entries:
        return ContextBlock()

    # The section heading and the blank lines between documents come out of the cap too, so
    # the block a call receives is never larger than the number this module advertises.
    room = max(cap - len(_CONTEXT_HEADING) - 2 * (len(entries) - 1), 0)
    budgets = _share_out(
        [len(_render_doc(name, content, len(content))) for name, content in entries], room
    )

    parts: list[str] = []
    included: list[str] = []
    truncated: list[str] = []
    omitted: list[str] = []

    for (name, content), budget in zip(entries, budgets, strict=True):
        overhead = len(_render_doc(name, "", len(content)))
        if overhead + len(content) <= budget:
            body = content
        elif budget - overhead - len(_DOC_TRUNCATION_MARKER) >= _MIN_DOC_SHARE:
            keep = budget - overhead - len(_DOC_TRUNCATION_MARKER)
            body = content[:keep].rstrip() + _DOC_TRUNCATION_MARKER
            truncated.append(name)
        else:
            # Too little room left to be worth the header. Named as omitted rather than
            # included with a stub, because "we sent you two sentences of your protocol" is
            # not a fact the run should assert about itself.
            omitted.append(name)
            continue
        included.append(name)
        parts.append(_render_doc(name, body, len(content)))

    if not parts:
        return ContextBlock(omitted=tuple(omitted))
    if truncated or omitted:
        log.info(
            "context documents did not fit the %d-char cap: truncated=%s omitted=%s",
            cap,
            truncated,
            omitted,
        )
    return ContextBlock(
        text=_CONTEXT_HEADING + "\n\n".join(parts),
        included=tuple(included),
        truncated=tuple(truncated),
        omitted=tuple(omitted),
    )


def _render_doc(name: str, body: str, chars: int) -> str:
    return f"--- {name} ({chars} chars) ---\n{body}"


def _share_out(wanted: Sequence[int], cap: int) -> list[int]:
    """Split `cap` across claimants, smallest claim first, redistributing what is unused.

    Anyone asking for no more than an equal share of what is left gets exactly what they
    asked for; the rest divide the remainder equally. This is the allocation that makes a
    small document safe from a large one sitting beside it.
    """
    order = sorted(range(len(wanted)), key=lambda index: wanted[index])
    granted = [0] * len(wanted)
    remaining = max(cap, 0)
    left = len(wanted)
    for index in order:
        share = remaining // left if left else 0
        take = min(wanted[index], share)
        granted[index] = take
        remaining -= take
        left -= 1
    return granted


# ------------------------------------------------------------------------ task prompts


@dataclass(frozen=True, slots=True)
class Seed:
    """A divergence seed produced by the Cartographer, waiting to be consumed."""

    seed_id: str | None = None
    source_domain: str = ""
    skeleton: str = ""
    seed_framing: str = ""
    round: int | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> Seed | None:
        if not data:
            return None
        return cls(
            seed_id=data.get("seed_id"),
            source_domain=str(data.get("source_domain") or ""),
            skeleton=str(data.get("skeleton") or ""),
            seed_framing=str(data.get("seed_framing") or ""),
            round=data.get("round"),
        )

    def render(self) -> str:
        return (
            "DIVERGENCE SEED (the idea pool has collapsed; this call must pull it out):\n"
            f"SOURCE DOMAIN: {self.source_domain}\n"
            f"SKELETON:\n{self.skeleton}\n"
            f"SEED FRAMING: {self.seed_framing}"
        )


@dataclass(frozen=True, slots=True)
class RoundContext:
    """What every generative call in a round shares: guidance, notes, documents, seed."""

    goal: str
    guidance: str = ""
    scientist_notes: tuple[str, ...] = ()
    context: ContextBlock = field(default_factory=ContextBlock)
    seed: Seed | None = None
    round: int | None = None
    guidance_round: int | None = None
    """Which round's meta-review produced `guidance`. `None` means none has.

    Carried because the label the prompt prints used to be *asserted*: the composer said
    "recurring critiques from the last round" over whatever the newest entry of
    `feedback_history` happened to be. A meta-review that fails records nothing, so the
    following round was told that round-(N-2) guidance — including its standings-derived
    WHAT WINS, about a tournament state that has since moved — came from the last round."""


def generation_prompt(
    ctx: RoundContext,
    *,
    count: int,
    directive: str,
    existing: Sequence[Mapping[str, Any]],
) -> str:
    return _assemble(
        "generation",
        _goal(ctx.goal),
        f"N: {count}",
        f"DIRECTIVE: {directive}",
        _feedback(ctx.guidance, ctx.guidance_round, ctx.round),
        _existing(existing),
        _notes(ctx.scientist_notes),
        ctx.seed.render() if ctx.seed else "",
        ctx.context.text,
    )


def reflection_prompt(
    goal: str,
    hypothesis: Mapping[str, Any],
    context: ContextBlock,
    *,
    parents: Sequence[Mapping[str, Any]] = (),
    scientist_notes: Sequence[str] = (),
) -> str:
    """Compose one hypothesis' critique prompt.

    `parents` is the lineage block, and it is not decoration. The reviewer is asked to
    judge `novelty`, is told to reject for non-novelty, and used to be shown the goal and
    the body and nothing else — so the one kind of non-novelty this loop actually
    manufactures, a variant that restates the parent it was bred from, was the one it could
    not see. In run c4566ed2 both of round 2's offspring passed at novelty=moderate and
    were archived as duplicates of their own cluster three steps later.
    """
    return _assemble(
        "reflection",
        _goal(goal),
        _notes(scientist_notes),
        f"HYPOTHESIS {hypothesis['hid']}:\n{hypothesis.get('body_md') or ''}".rstrip(),
        _lineage(hypothesis, parents),
        context.text,
    )


def _lineage(hypothesis: Mapping[str, Any], parents: Sequence[Mapping[str, Any]]) -> str:
    if not parents:
        return ""
    operator = str(hypothesis.get("operator") or "").strip() or "unstated"
    listed = "\n".join(_summary_line(row) for row in parents)
    return (
        f"LINEAGE (this hypothesis is a variant produced by the `{operator}` operator from "
        f"the hypotheses below):\n{listed}\n"
        "Judge novelty against these parents as well as against the literature: a variant "
        "that restates a parent, or that would be confirmed by whatever confirms a parent, "
        "is not novel however well written it is."
    )


def proximity_prompt(goal: str, rows: Sequence[Mapping[str, Any]]) -> str:
    """Cluster the pool, showing each row's current label so the vocabulary can hold.

    The label is rendered because it was not: the call saw `id | title | claim` and minted
    a fresh vocabulary every round, so four of run c4566ed2's six unchanged round-1
    hypotheses had a different cluster string by round 3 with no change to their text. That
    churn splits one idea-family across rounds in every reading built on the label — the
    birth-rate collapse vote, the archived-duplicate labels `_pool_concentration` counts,
    and the graph's hue.
    """
    return _assemble(
        "proximity",
        _goal(goal),
        "HYPOTHESES (`id | title | current label | claim`; the current label is what the "
        "last clustering call named this row, or `(unlabelled)` for a new one):\n"
        + "\n".join(_labelled_line(row) for row in rows),
    )


def _labelled_line(row: Mapping[str, Any]) -> str:
    label = str(row.get("cluster") or "").strip() or "(unlabelled)"
    return f"{row['hid']} | {row.get('title', '')} | {label} | {claim_of(row)}"


def ranking_prompt(
    goal: str,
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    reviews: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> str:
    reviews = reviews or {}
    return _assemble(
        "ranking",
        _goal(goal),
        _contender(1, first, reviews.get(first["hid"])),
        _contender(2, second, reviews.get(second["hid"])),
    )


def evolution_prompt(
    ctx: RoundContext,
    *,
    count: int,
    top: Sequence[Mapping[str, Any]],
    reviews: Mapping[str, Mapping[str, Any] | None] | None = None,
    concentration: float | None = None,
    outsider: str | None = None,
    existing: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Compose the evolution prompt; `concentration` demands one divergent variant.

    The caller passes the dominant cluster's share of the active pool **only when it judged
    the pool concentrated** — passing `None` is how it says "the pool is still wide, pick
    whatever operator each parent deserves". Left to itself the agent picks convergent
    operators: the whole corpus of evolved hypotheses is 4 `grounding` and 4 `combination`,
    with `out_of_box` and `simplification` never once chosen, so a narrow pool was only ever
    refined and never widened.

    `outsider` names the parent the orchestrator swapped in from a cluster the leaders do
    not occupy (`Orchestrator._parents`). It is rendered because the swap was invisible: the
    block is headed "TOP HYPOTHESES" and every row looked identical, so the one piece of
    structural anti-convergence pressure in the loop arrived as an unexplained lower-Elo
    entry in a list labelled "top".

    `existing` is the pool the offspring must not merely restate. Without it the call could
    only be told what to breed *from*, never what already exists, and two of run c4566ed2's
    three round-2 offspring were archived as duplicates after each had already been paid for
    twice — once to evolve, once for a grounded critique.
    """
    reviews = reviews or {}
    blocks = [
        f"--- {row['hid']} | {row.get('title', '')} | Elo {round(float(row.get('elo', 0)))}"
        f"{_parent_marker(row, outsider)} ---\n"
        f"{row.get('body_md') or ''}\n{_render_review(reviews.get(row['hid']))}".rstrip()
        for row in top
    ]
    return _assemble(
        "evolution",
        _goal(ctx.goal),
        f"N: {count}",
        _feedback(ctx.guidance, ctx.guidance_round, ctx.round),
        _notes(ctx.scientist_notes),
        "TOP HYPOTHESES:\n" + "\n\n".join(blocks),
        _existing(existing),
        _divergence(concentration, count),
        ctx.context.text,
    )


def _parent_marker(row: Mapping[str, Any], outsider: str | None) -> str:
    cluster = str(row.get("cluster") or "").strip()
    parts = [f"cluster: {cluster}"] if cluster else []
    if outsider is not None and row["hid"] == outsider:
        parts.append(
            "DIFFERENT CLUSTER — included deliberately, so at least one variant is bred "
            "outside the leading cluster rather than from the leaders alone"
        )
    return (" | " + " | ".join(parts)) if parts else ""


def _divergence(concentration: float | None, count: int) -> str:
    if concentration is None:
        return ""
    return (
        "DIVERGENCE REQUIREMENT (the pool has narrowed — "
        f"{concentration:.0%} of every hypothesis this run has produced, whatever became "
        "of it, now carries a single cluster label):\n"
        f"At least one of the {count} variant(s) must use a DIVERGENT operator and say so "
        "in its `operator` field — `out_of_box` (take a top idea as analogical inspiration "
        "only and land somewhere the pool does not already occupy) or `simplification` "
        "(strip an idea to a testable core that no longer depends on the leading frame). "
        "A variant labelled `out_of_box` that is really a recombination of what is already "
        "here is worse than none, because it hides the narrowing instead of correcting it."
    )


def cartographer_prompt(goal: str, champions: Sequence[Mapping[str, Any]]) -> str:
    return _assemble(
        "cartographer",
        _goal(goal),
        "CURRENT CLUSTERS (the champion of each currently-active cluster):\n"
        + "\n".join(_summary_line(row) for row in champions),
    )


def meta_review_prompt(
    goal: str,
    *,
    round: int,
    reviews: Sequence[Mapping[str, Any]],
    debates: Sequence[Mapping[str, Any]],
    standings: Sequence[Mapping[str, Any]],
    previous_guidance: str = "",
    previous_guidance_round: int | None = None,
    scientist_notes: Sequence[str] = (),
    round_health: str = "",
    char_budget: int = META_REVIEW_CHAR_BUDGET,
) -> str:
    """Compose the feedback-mode prompt under C3's truncation rule.

    Every review of the round goes in, in full, always. When the prompt is too long it is
    the **debates** that give way, lowest-Elo match first — a debate is a judgement between
    two hypotheses we already have the reviews for, whereas a missing review means a whole
    critique never reaches the next round. The count of what was dropped is stated in the
    prompt so the agent knows it is generalising from a subset.

    `round_health` is the one input the meta-review used to lack entirely. This is the only
    component that can steer the next round away from a failure mode, and it was never told
    that a step had failed: in a round where generation timed out, the prompt said "(no
    reviews this round.)" and the agent synthesised guidance from debates alone, over an
    unchanged pool, with no idea why.
    """
    header = [
        _goal(goal),
        "MODE: feedback",
        f"ROUND: {round}",
        _round_health(round_health),
        *_standings_sections(standings),
        _notes(scientist_notes),
        _previous_guidance(previous_guidance, previous_guidance_round, round),
        "REVIEWS FROM THIS ROUND (all of them):\n"
        + ("\n\n".join(_review_entry(row) for row in reviews) or "(no reviews this round.)"),
    ]
    fixed = _assemble("meta_review", *header)

    ordered = sorted(debates, key=_debate_strength, reverse=True)
    kept: list[str] = []
    used = len(fixed)
    for entry in ordered:
        rendered = _debate_entry(entry)
        if used + len(rendered) > char_budget and kept:
            # `break`, not `continue`. Skipping onward could admit a later, shorter,
            # *lower*-Elo debate after a higher-Elo one had been dropped, while the note
            # below went on claiming the omissions were the lowest-Elo matches. The kept
            # set is now a strict prefix of the Elo ordering, which makes the note true by
            # construction rather than usually.
            break
        kept.append(rendered)
        used += len(rendered)
    dropped = len(ordered) - len(kept)

    if not debates:
        section = "DEBATES FROM THIS ROUND:\n(no matches were judged this round.)"
    else:
        note = (
            f"\n\n({dropped} further debate(s) omitted for length — the lowest-Elo matches.)"
            if dropped
            else ""
        )
        section = "DEBATES FROM THIS ROUND:\n" + "\n\n".join(kept) + note
    return _assemble("meta_review", *header, section)


def overview_prompt(
    goal: str,
    *,
    top: Sequence[Mapping[str, Any]],
    also_ranked: Sequence[Mapping[str, Any]] = (),
    standings: Sequence[Mapping[str, Any]],
    unranked: Sequence[Mapping[str, Any]] = (),
    reviews: Mapping[str, Mapping[str, Any] | None] | None = None,
    debates: Sequence[Mapping[str, Any]] = (),
    feedback_history: Sequence[Mapping[str, Any]] = (),
    counts: Mapping[str, Any] | None = None,
    rounds_completed: int = 0,
    health: str = "",
    nothing_survived: bool = False,
    context: ContextBlock | None = None,
    char_budget: int = OVERVIEW_CHAR_BUDGET,
) -> str:
    """Compose the report prompt, including what the run failed to do.

    `health` is mandatory in the sense that matters: when it is non-empty it is rendered as
    its own section with an instruction attached, because the report author could not
    caveat a run it was never told anything about. The old input set was goal, mode, a
    summary line built purely from successes, standings, top hypotheses and the guidance
    trajectory — no field existed in which a failure could be expressed.

    `unranked` is listed separately from `top` for the same reason: a hypothesis at the
    default 1200 with no matches is not a ranking result, and printing it as one put ideas
    that never competed above ideas that competed and lost.

    `also_ranked` is the tail of the ranked pool the top-k cut left out. The cut is
    hard-coded in the caller and used to be unstated, under a header reading "these played
    matches" — which reads as though the listed rows *are* the ones that played. Nine of
    run c4566ed2's fourteen competed hypotheses reached the report as a standings line and
    nothing else, and the report's author had no way to know they existed in fuller form.
    """
    reviews = reviews or {}
    counts = counts or {}
    summary = (
        f"RUN SUMMARY: {rounds_completed} round(s) completed; "
        f"{counts.get('active', 0)} active, {counts.get('rejected', 0)} rejected, "
        f"{counts.get('archived', 0)} archived hypotheses; "
        f"{counts.get('matches', 0)} matches judged."
    )
    blocks = [
        f"{_summary_line(row)}\n{row.get('body_md') or ''}\n"
        f"{_render_review(reviews.get(row['hid']))}".rstrip()
        for row in top
    ]
    if nothing_survived:
        top_section = (
            "TOP HYPOTHESES:\n(none survived review — every hypothesis this run produced "
            "was rejected or archived. Say so plainly; the ideas below are listed for the "
            "record, not as results.)"
        )
    else:
        competed = len(top) + len(also_ranked)
        top_section = (
            f"TOP HYPOTHESES (the {len(top)} highest-rated of the {competed} that played "
            f"matches, in full; the other {len(also_ranked)} are summarised in the section "
            "below and appear in STANDINGS):\n"
        ) + ("\n\n".join(blocks) or "(no hypothesis in this run has played a match.)")

    sections = [
        _goal(goal),
        "MODE: overview",
        summary,
        _health(health),
        *_standings_sections(standings),
        top_section,
        _also_ranked(also_ranked, reviews),
        *_unranked(unranked, reviews),
        _debates(debates),
        context.text if context else "",
    ]
    fixed = _assemble("overview", *sections)
    budget = max(char_budget - len(fixed), TRAJECTORY_FLOOR)
    return _assemble("overview", *sections, _trajectory(feedback_history, budget))


def _debates(debates: Sequence[Mapping[str, Any]]) -> str:
    """The judges' comparative reasoning, strongest match first, under its own allowance.

    The report is asked to say why each top hypothesis ranked highly and was given an Elo
    integer to say it with. These are the only documents in the run that answer that
    question directly.
    """
    if not debates:
        return ""
    kept: list[str] = []
    used = 0
    for entry in sorted(debates, key=_debate_strength, reverse=True):
        rendered = _debate_entry(entry)
        if used + len(rendered) > OVERVIEW_DEBATE_BUDGET and kept:
            break
        kept.append(rendered)
        used += len(rendered)
    dropped = len(debates) - len(kept)
    note = (
        f"\n\n({dropped} further debate(s) omitted for length — the lowest-rated matches.)"
        if dropped
        else ""
    )
    return (
        "MATCH DEBATES (what the judge actually said, highest-rated match first — this is "
        "the evidence for 'why it ranked highly'):\n" + "\n\n".join(kept) + note
    )


def _also_ranked(
    rows: Sequence[Mapping[str, Any]], reviews: Mapping[str, Mapping[str, Any] | None]
) -> str:
    if not rows:
        return ""
    listed = "\n".join(
        f"{_summary_line(row)} | Elo {round(float(row.get('elo', 0)))}, "
        f"{row.get('matches', 0)} match(es) | {_verdict_line(reviews.get(row['hid']))}"
        for row in rows
    )
    return (
        "OTHER HYPOTHESES THAT PLAYED MATCHES (earned ratings, summarised rather than "
        "shown in full — name them where they matter; do not present them as unexamined):\n"
        + listed
    )


def _verdict_line(review: Mapping[str, Any] | None) -> str:
    if not review:
        return "not reviewed"
    return (
        f"review: {review.get('verdict', 'unknown')}, "
        f"novelty {review.get('novelty_level') or 'unknown'}"
    )


def _health(health: str) -> str:
    text = (health or "").strip()
    if not text:
        return ""
    return (
        "RUN HEALTH (things that did not go to plan; the report must say so):\n"
        f"{text}\n"
        "State these limitations plainly in the report's opening paragraph and again "
        "under ## Caveats. A reader who acts on this document has to know what it is "
        "missing."
    )


def _unranked(
    rows: Sequence[Mapping[str, Any]], reviews: Mapping[str, Mapping[str, Any] | None]
) -> list[str]:
    """The zero-match pool, split on whether a critique for it exists.

    One section used to serve both, and its text asserted as fact that every row it listed
    was "created after the last tournament — never reviewed, never ranked". The caller
    partitions on `matches == 0` alone, so a hypothesis that was generated early, reviewed
    in full and simply never drawn into a pair landed there too: in run c4566ed2 five of
    the eight listed rows (h017–h021) carried a completed `pass` review, ~12,000 characters
    of paid critique that the report's author was both denied and told did not exist.

    Both sentences are now derived from the data instead of hard-coded, and the reviewed
    rows carry their review — the only thing that ever separated the two groups.
    """
    reviewed = [row for row in rows if reviews.get(row["hid"])]
    unreviewed = [row for row in rows if not reviews.get(row["hid"])]
    sections: list[str] = []
    if reviewed:
        sections.append(
            "REVIEWED BUT NEVER PAIRED (these passed review and were never drawn into a "
            "match, so they hold the default 1200 rather than an earned rating. The "
            "critique below stands; the rating does not — treat them as examined ideas "
            "with no tournament evidence either way):\n"
            + "\n\n".join(
                f"{_summary_line(row)}\n{_render_review(reviews.get(row['hid']))}"
                for row in reviewed
            )
        )
    if unreviewed:
        sections.append(
            "UNVERIFIED NEW VARIANTS (no review and no match — nobody has critiqued these "
            "and nothing has been ranked against them; they hold the default rating rather "
            "than an earned one. Mention them only as leads, never as results):\n"
            + "\n".join(_summary_line(row) for row in unreviewed)
        )
    return sections


def _trajectory(feedback_history: Sequence[Mapping[str, Any]], budget: int) -> str:
    """The guidance trail, trimmed from the far end when it will not fit.

    This section is the one the reader needs least and the one that grows fastest: a real
    five-round trail is ~46K characters, 47% of the prompt — more than the top five
    hypotheses, their reviews and the whole standings table combined. Recent rounds stay
    whole; older ones keep only the paragraph that says what they steered towards.
    """
    heading = "GUIDANCE TRAJECTORY (what the meta-review steered towards, round by round):\n"
    entries = list(feedback_history)
    if not entries:
        return heading + "(no guidance was recorded.)"

    rendered = [
        f"Round {entry.get('round')}: {str(entry.get('guidance') or '').strip()}"
        for entry in entries
    ]
    if sum(len(line) for line in rendered) <= max(budget, 0):
        body = "\n".join(rendered)
        marker = ""
    else:
        kept = [
            f"Round {entry.get('round')}: {_guidance_tail(str(entry.get('guidance') or ''))}"
            for entry in entries[:-2]
        ]
        kept += rendered[-2:]
        body = "\n".join(kept)
        marker = "\n(earlier rounds summarised to their closing guidance.)"
    return heading + body + marker


_GUIDANCE_TAIL = re.compile(r"GUIDANCE FOR THE NEXT ROUND:\s*(.*)", re.DOTALL)


def _guidance_tail(guidance: str) -> str:
    match = _GUIDANCE_TAIL.search(guidance)
    return (match.group(1) if match else guidance).strip()


# ------------------------------------------------------------------------------ layout


def _assemble(role: str, *sections: str) -> str:
    body = "\n\n".join(section.strip() for section in sections if section and section.strip())
    return f"ROLE: {role}.\n\n{body}\n"


def _goal(goal: str) -> str:
    return f"GOAL:\n{goal.strip()}"


def _round_health(health: str) -> str:
    text = (health or "").strip()
    if not text:
        return ""
    return (
        "THIS ROUND'S HEALTH (treat this as a first-class input — guidance that corrects "
        f"a failing step is worth more than guidance about the ideas):\n{text}"
    )


def _feedback(guidance: str, from_round: int | None, for_round: int | None) -> str:
    """The FEEDBACK block, headed with the round its guidance actually came from."""
    text = (guidance or "").strip()
    if not text:
        if for_round is not None and for_round > 1:
            return (
                "FEEDBACK:\n(no guidance is available — the meta-review for the previous "
                "round did not complete. Do not assume this is the first round; consult "
                "EXISTING for what the run has already produced.)"
            )
        return (
            "FEEDBACK (recurring critiques from the last round):\n"
            "(none yet — this is the first round.)"
        )
    if from_round is None:
        return f"FEEDBACK (recurring critiques from an earlier round):\n{text}"
    stale = for_round is not None and from_round < for_round - 1
    header = (
        f"FEEDBACK (recurring critiques from round {from_round} — note that this is NOT "
        f"the previous round; round {for_round - 1}'s meta-review did not complete, so the "
        "standings this describes have moved since)"
        if stale
        else f"FEEDBACK (recurring critiques from round {from_round}, the round before this one)"
    )
    return f"{header}:\n{text}"


def _previous_guidance(guidance: str, from_round: int | None, for_round: int) -> str:
    text = (guidance or "").strip()
    if not text:
        if for_round > 1:
            return (
                "PREVIOUS GUIDANCE:\n(none — the meta-review for the previous round did "
                "not complete.)"
            )
        return "PREVIOUS GUIDANCE (the round before this one):\n(none — this is the first round.)"
    if from_round is None:
        return f"PREVIOUS GUIDANCE (from an earlier round):\n{text}"
    if from_round < for_round - 1:
        return (
            f"PREVIOUS GUIDANCE (from round {from_round}; no guidance was produced for "
            f"round {for_round - 1}):\n{text}"
        )
    return f"PREVIOUS GUIDANCE (from round {from_round}, the round before this one):\n{text}"


def _existing(rows: Sequence[Mapping[str, Any]]) -> str:
    """What the pool already holds, by hid, and honestly about each row's status.

    The hid is rendered because the FEEDBACK block delivered in the same prompt names
    hypotheses by hid dozens of times — 34, 53 and 70 mentions across run c4566ed2's three
    rounds — while EXISTING listed bare titles, so guidance saying "cross h001's fixed-strike
    option with h004's exception-coverage claim" arrived with no way to resolve either id.

    Rejected and retired rows are listed apart rather than under "already in the pool": a
    hypothesis reflection rejected is not in the pool, and calling it so both states
    something false and forecloses a corrected version of an idea killed for a fixable
    reason.
    """
    if not rows:
        return "EXISTING (already in the pool — do not duplicate):\n(the pool is empty.)"
    live = [row for row in rows if str(row.get("status") or "active") == "active"]
    gone = [row for row in rows if str(row.get("status") or "active") != "active"]
    sections = [
        "EXISTING (already in the pool — do not duplicate):\n"
        + ("\n".join(_existing_line(row) for row in live) or "(the pool is empty.)")
    ]
    if gone:
        sections.append(
            "ALREADY TRIED AND SET ASIDE (rejected in review, or retired as a duplicate — "
            "do not re-propose these as they stand; a version that repairs what was wrong "
            "with one is welcome):\n"
            + "\n".join(_existing_line(row) for row in gone)
        )
    return "\n\n".join(sections)


def _existing_line(row: Mapping[str, Any]) -> str:
    return f"- {row.get('hid', '?')} | {row.get('title', '')}"


def _notes(notes: Sequence[str]) -> str:
    kept = [note.strip() for note in notes if note and note.strip()]
    if not kept:
        return ""
    listed = "\n".join(f"- {note}" for note in kept)
    return (
        "SCIENTIST GUIDANCE (highest priority — this comes from the human running the "
        f"study and outranks everything else in this prompt):\n{listed}"
    )


def _standings_sections(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """STANDINGS, with the hypotheses that never played listed apart.

    One flat Elo-ordered table put never-played rows sitting at the untouched 1200 default
    above veterans that competed and lost — at round 2 of run c4566ed2, six newcomers
    ranked above three hypotheses with three judged matches each. The overview path was
    given this split; the meta-review, which synthesises "what wins" off exactly this
    table, was not.
    """
    if not rows:
        return ["STANDINGS:\n(no active hypotheses.)"]
    played = [row for row in rows if int(row.get("matches") or 0) > 0]
    unplayed = [row for row in rows if int(row.get("matches") or 0) == 0]
    sections = [
        "STANDINGS (hypotheses that played matches, by Elo within this run):\n"
        + (_standing_lines(played) or "(none has played a match yet.)")
    ]
    if unplayed:
        sections.append(
            "NEVER PLAYED (no match, so the 1200 beside each of these is the default "
            "starting rating and not a result — do not read it as a placing):\n"
            + _standing_lines(unplayed)
        )
    return sections


def _standing_lines(rows: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"{index}. {row['hid']} | {row.get('title', '')} | Elo "
        f"{round(float(row.get('elo', 0)))} | {row.get('matches', 0)} match(es), "
        f"{row.get('wins', 0)} win(s)"
        for index, row in enumerate(rows, start=1)
    )


def _summary_line(row: Mapping[str, Any]) -> str:
    return f"{row['hid']} | {row.get('title', '')} | {claim_of(row)}"


def _contender(position: int, row: Mapping[str, Any], review: Mapping[str, Any] | None) -> str:
    return (
        f"HYPOTHESIS {position} ({row['hid']}):\n{row.get('body_md') or ''}\n"
        f"REVIEW OF HYPOTHESIS {position}:\n{_render_review(review)}"
    ).rstrip()


def _render_review(review: Mapping[str, Any] | None) -> str:
    if not review:
        return "REVIEW: (not reviewed)"
    novelty = review.get("novelty_level") or "unknown"
    return (
        f"REVIEW: verdict={review.get('verdict', 'unknown')} novelty={novelty}\n"
        f"  novelty note: {review.get('novelty_note') or ''}\n"
        f"  correctness: {review.get('correctness') or ''}\n"
        f"  testability: {review.get('testability') or ''}\n"
        f"  key risk: {review.get('key_risk') or ''}\n"
        f"  note: {review.get('note') or ''}"
    )


def _review_entry(review: Mapping[str, Any]) -> str:
    return f"{review.get('hid', '?')} | {_render_review(review)}"


def _debate_entry(entry: Mapping[str, Any]) -> str:
    winner = entry.get("winner")
    won = entry.get("hid_a") if winner == 1 else entry.get("hid_b")
    return (
        f"{entry.get('hid_a')} vs {entry.get('hid_b')} — winner {won}:\n"
        f"{(entry.get('debate_md') or '').strip()}"
    )


def _debate_strength(entry: Mapping[str, Any]) -> float:
    """Rank a debate by the stronger side's rating after the match; drop the weakest first."""
    ratings = [
        float(entry.get(key) or 0)
        for key in ("elo_a_after", "elo_b_after", "elo_a_before", "elo_b_before")
    ]
    return max(ratings) if ratings else 0.0


_CLAIM = re.compile(r"^\*\*Claim:\*\*\s*(.+)$", re.MULTILINE)


def claim_of(row: Mapping[str, Any]) -> str:
    """The one-line claim out of a composed body, for `id | title | claim` listings."""
    match = _CLAIM.search(row.get("body_md") or "")
    if match:
        return " ".join(match.group(1).split())
    body = " ".join((row.get("body_md") or "").split())
    return body[:200]


def titles_of(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(row.get("title") or "") for row in rows]

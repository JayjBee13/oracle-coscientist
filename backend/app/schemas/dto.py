"""The frozen response shapes of plan C5, one model per DTO.

These are declarative on purpose. Wave 4 emits this API's OpenAPI document and generates
the frontend's TypeScript from it, and Wave 6 fails the build if regenerating produces a
diff — so a field added here without a reason shows up as a red gate, and a field quietly
dropped shows up as a compile error in the app. The field lists below are the contract;
`app/services/runs/reads.py` is what fills them.

Nothing here is a database model. `Run`, `Hypothesis` and friends live in
`app/db/engine_models.py`, and the two are allowed to differ: `HypothesisRow` deliberately
omits `body_md`, `MatchRow` names its sides `a`/`b` and carries their titles, and every
timestamp is an ISO string rather than a datetime, because that is what crosses the wire.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.research import ResearchView

__all__ = [
    "CompareAnalytics",
    "CompareDelta",
    "CompareMovement",
    "Event",
    "GraftEvent",
    "GraphEdge",
    "GraphMeta",
    "GraphNode",
    "HaltAllResponse",
    "HypothesisDetail",
    "HypothesisRow",
    "ImportReportResponse",
    "MatchRow",
    "RunBudget",
    "RunDetail",
    "RunGraph",
    "RunList",
    "RunSummary",
    "RoundSummary",
]

# --- Vocabularies ------------------------------------------------------------------------
#
# Named enums rather than bare `str`, wherever the value comes from a closed set the
# database or this app already guarantees. The OpenAPI document is where the frontend's
# types come from, so `lifecycle: string` on the wire means every switch over it in the app
# has a default arm the compiler cannot check — and the app's own list of lifecycles, kept
# by hand, is free to disagree with this one.
#
# Fields the *model* fills — a review's verdict, a hypothesis's operator, an event type —
# stay `str` on purpose. Nothing constrains them at the database, and a response model that
# raises on an unexpected value would turn a surprising word into a 500 on the run list.
#
# `StrEnum` members are strings, so `run.lifecycle == "running"` still reads true and
# serialisation is unchanged.


class Lifecycle(StrEnum):
    """Plan C4's canonical set. Transitions live with the model, not here."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FINISHING = "finishing"
    COMPLETED = "completed"
    FAILED = "failed"
    LOST = "lost"


class Harness(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    DEMO = "demo"


class RunSource(StrEnum):
    APP = "app"
    IMPORTED = "imported"


class HypothesisStatus(StrEnum):
    ACTIVE = "active"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class HypothesisSource(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class MatchStatus(StrEnum):
    PLANNED = "planned"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class WorkshopState(StrEnum):
    REFINING = "refining"
    OPTIONS_READY = "options_ready"
    CHOSEN = "chosen"
    FAILED = "failed"


class GroundingDepth(StrEnum):
    SHALLOW = "shallow"
    STANDARD = "standard"
    DEEP = "deep"


class Provider(StrEnum):
    """Whose CLI runs a step. A quick-set for the whole table, never a lock on one row:
    steps mix providers freely, and the runner for a call is chosen from that row's model."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class ModelTier(StrEnum):
    """A preset that moves thinking effort — and, for `low` alone, the model too.

    For `max`, `high` and `med`, which model a step runs on is fixed by how hard the step is
    — heavy steps take the provider's frontier model, light steps take its workhorse — so
    `med` is a run that thinks less, never a run that thinks with something weaker.
    Clustering is pinned to low effort in every tier.

    `low` is the speed preset and the one exception: it pins **every** step to `gpt-5.6-luna`
    at high effort whatever the run's provider says, so a Low run drives the Codex CLI and
    needs it installed. That is published rather than implied — `/api/capabilities` carries
    the pin and the CLI requirement per tier in `models.tier_catalog` — because a tier
    changing models is only a problem when nobody can see it.

    The older names `standard`/`maximum` and the pre-floor `balanced`/`quality` are gone
    from the wire but are still accepted on the way in and mapped by
    `engine/runners.normalise_tier`, because stored run configs say them."""

    MAX = "max"
    HIGH = "high"
    MED = "med"
    LOW = "low"


class RunnerKind(StrEnum):
    """Which runner executes a run's calls. `demo` is the fake; the other two are real.

    `claude`/`codex` name the *default* lane rather than a restriction — a real run
    dispatches each call to the runner its row's model belongs to, so a table mixing
    providers works whichever of the two this says."""

    CLAUDE = "claude"
    CODEX = "codex"
    DEMO = "demo"


class ControlAction(StrEnum):
    """Plan C4's legal actions. Which are legal *when* is the lifecycle's business."""

    PAUSE = "pause"
    RESUME = "resume"
    CONTINUE = "continue"
    """Give a run that has already ended more rounds, keeping everything it learned.

    Distinct from `resume`, which restarts a *paused* run that never finished, and from
    `POST /runs {from_run}`, which copies a run's setup into a brand new one that starts
    from no hypotheses at all."""

    STOP = "stop"
    FINISH = "finish"
    FORCE_STOP = "force_stop"


class DirectionHint(StrEnum):
    """Which way a larger challenger number reads."""

    UP = "up"
    """Larger is the better outcome."""

    DOWN = "down"
    """Larger is the worse outcome."""

    FLAT = "flat"
    """The metric describes a run rather than grading it."""


class RunCounts(BaseModel):
    active: int = 0
    rejected: int = 0
    archived: int = 0
    matches: int = 0


class RunTop(BaseModel):
    hid: str
    title: str
    elo: float
    status: HypothesisStatus


class RunGraft(BaseModel):
    enabled: bool = False
    fired_count: int = 0
    pending: bool = False


class RunSummary(BaseModel):
    id: str
    engine_run_id: str
    source: RunSource
    source_version: str | None = None
    title: str
    question: str
    owner_display_name: str | None = None
    harness: Harness
    lifecycle: Lifecycle
    round: int
    rounds_target: int
    calls_used: int
    budget_calls: int
    spend_usd: float
    tokens_total: int
    model_level: Literal["low", "med", "high", "max"] | None = None
    model_level_custom: bool = False
    elapsed_seconds: int | None = None
    counts: RunCounts
    # Three at most, and never empty for a run that produced anything: an all-rejected run
    # falls back past status rather than rendering as a run with no ideas.
    top: list[RunTop] = []
    graft: RunGraft
    archived: bool
    has_overview: bool

    # --- what the run lost ---------------------------------------------------------
    #
    # On `RunSummary` and not only on `RunDetail`, because this object is behind the run
    # header *and* every row of the run list, and until these existed neither could say
    # that anything had gone wrong. A run that lost three whole steps, produced no report
    # and blew its cost ceiling rendered as "Completed", tone `go`, exactly like a run that
    # did everything it was asked. Deliberately not a new lifecycle: lifecycle drives
    # control gating and lane filtering, and forking it would multiply four tables to
    # express one adjective.

    failed_calls: int = 0
    """Model calls that ended with an error, retries included."""

    lost_steps: int = 0
    """Units the engine gave up on: work this run was supposed to do and did not."""

    ended_reason: str | None = None
    """`rounds_done`, `budget_calls`, `budget_usd`, `wall_clock`, `stop_requested`,
    `finish_requested` — or null for a run that predates the field."""

    overview_skipped_reason: str | None = None
    """Why there is no report, when there is none. The Report tab says so in words."""

    # Not nullable: both columns are NOT NULL, and a run list that cannot sort by recency
    # because a timestamp might be missing is a worse contract than the truth.
    created_at: str
    updated_at: str


class RunList(BaseModel):
    items: list[RunSummary]
    total: int


class HypothesisRow(BaseModel):
    id: str
    hid: str
    title: str
    status: HypothesisStatus
    elo: float
    matches: int
    wins: int
    cluster: str | None = None
    duplicate_of: str | None = None
    parent_ids: list[str] = []
    operator: str | None = None
    created_round: int
    source: HypothesisSource
    novelty_level: str | None = None


class ReviewRow(BaseModel):
    """Imported runs carry only `verdict` and `note` — history recorded nothing else."""

    verdict: str
    novelty_level: str | None = None
    novelty_note: str | None = None
    correctness: str | None = None
    testability: str | None = None
    key_risk: str | None = None
    note: str | None = None
    model: str | None = None


class MatchSide(BaseModel):
    hid: str
    title: str


class MatchRow(BaseModel):
    id: str
    round: int
    a: MatchSide
    b: MatchSide
    status: MatchStatus
    winner: int | None = None
    # Null on every imported match: the old engines stored the winner and nothing else, so
    # the rating curve behind a historical tournament is not reconstructable.
    elo_a_before: float | None = None
    elo_a_after: float | None = None
    elo_b_before: float | None = None
    elo_b_after: float | None = None
    judge_model: str | None = None
    debate_md: str | None = None
    ts: str | None = None


class Lineage(BaseModel):
    parents: list[HypothesisRow] = []
    children: list[HypothesisRow] = []


class HypothesisDetail(HypothesisRow):
    body_md: str
    reviews: list[ReviewRow] = []
    match_history: list[MatchRow] = []
    lineage: Lineage


class GraphNode(BaseModel):
    """One idea as the genealogy canvas draws it.

    Narrower than `HypothesisRow` on purpose in one direction and wider in another: no
    `body_md` and no `parent_ids` (descent is edges, so a node and a link are not two
    disagreeing statements of the same fact), plus `is_leader`, which no single row can
    know because it is a fact about the run.
    """

    hid: str
    id: UUID
    """The hypothesis id, so clicking a node can fetch its detail."""

    title: str
    status: HypothesisStatus
    elo: float
    matches: int
    wins: int
    created_round: int
    operator: str | None = None
    """`grounding`, `combination`, `simplification`, `out_of_box` — or null for every
    imported idea, because the old engines never recorded which move produced one."""

    cluster: str | None = None
    duplicate_of: str | None = None
    """The hid of the survivor this idea merged into, when clustering archived it."""

    source: HypothesisSource
    is_leader: bool
    """Highest Elo among the *active* ideas, ties broken by the lowest hid. False for
    every node in a run whose ideas were all rejected."""


class GraphEdge(BaseModel):
    """One descent link, parent → child.

    The operator is copied from the child so an edge can be styled without a lookup — the
    two arrows converging on a combination are the visual signature of evolution.
    """

    parent: str
    child: str
    operator: str | None = None


class GraphMeta(BaseModel):
    """What the legend needs to describe this run without overclaiming."""

    rounds: int
    """At least 1, even for the oldest imported runs, which recorded no iteration."""

    has_lineage: bool
    """Whether any idea in this run descended from another. False for a run that has
    generated its first round and evolved nothing yet, which is not a gap in the record."""

    lineage_recorded: bool
    """Whether descent *could* have been recorded. False only for the imported runs that
    predate `parent_ids` — the view says "lineage was not recorded" for those and "nothing
    was evolved from anything" for the rest, rather than one sentence for two facts."""

    has_clusters: bool
    """False for the runs that skipped proximity, where hue carries no meaning."""

    elo_min: float
    elo_max: float
    """Node size scales between these two — **within this run only**. Elo is not
    comparable across runs, so the range travels with the graph."""

    node_count: int


class RunGraph(BaseModel):
    run_id: UUID
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    meta: GraphMeta


class RoundSummary(BaseModel):
    round: int
    started_at: str | None = None
    completed_at: str | None = None
    hypotheses_added: int = 0
    matches_completed: int = 0
    matches_planned: int = 0
    reviews: int = 0
    graft_fired: bool = False
    status: str


class Event(BaseModel):
    seq: int
    run_id: str
    round: int | None = None
    type: str
    payload: dict[str, Any] = {}
    ts: str | None = None


class GraftEvent(BaseModel):
    round: int
    fired: bool
    votes: int | None = None
    n_clusters: int | None = None
    hhi: float | None = None
    abstained_reason: str | None = None
    source_domain: str | None = None
    seed_framing: str | None = None


class BudgetByRole(BaseModel):
    role: str
    calls: int
    tokens: int
    usd: float


class RunBudget(BaseModel):
    calls_used: int
    budget_calls: int
    spend_usd: float
    budget_usd: float
    by_role: list[BudgetByRole] = []


class ModelTableEntry(BaseModel):
    role: str
    model: str
    effort: str | None = None


class ContextDocRef(BaseModel):
    name: str
    chars: int

    delivered: Literal["full", "truncated", "omitted", "pending"] = "pending"
    """How much of this document actually reached the model's prompts.

    `chars` is what the scientist uploaded, and on its own it reads as confirmation that
    all of it was used. It was not: every call is capped at `cap_chars`, and until this
    field existed the loss was recorded in one `log.info` line inside the run's workdir and
    nowhere else. `pending` means the run has not started composing prompts yet."""

    cap_chars: int | None = None
    """The per-call character cap this run's documents were fitted into."""


class FeedbackEntry(BaseModel):
    round: int
    guidance: str


class RunDetail(BaseModel):
    research: ResearchView | None = None
    run: RunSummary
    config: dict[str, Any] = {}
    model_table: list[ModelTableEntry] = []
    leaderboard: list[HypothesisRow] = []
    rounds: list[RoundSummary] = []
    recent_events: list[Event] = []
    budget: RunBudget
    graft_events: list[GraftEvent] = []
    context_docs: list[ContextDocRef] = []
    feedback_history: list[FeedbackEntry] = []

    degraded_count: int = 0
    """Model or effort *substitutions*. Not a loss counter, and never was — it is 0 for
    every run this engine has ever executed, because a call that times out or returns
    unusable output cannot increment it by construction."""

    failed_calls: int = 0
    lost_steps: int = 0
    retried_calls: int = 0
    """`failed_calls` a retry rescued: time and budget spent, but no work lost."""

    problems: list[Event] = []
    """Every failure event, unfiltered by the 50-row `recent_events` window.

    On a 401-event run, 87% of the log falls outside that window and is reachable only
    through the live SSE stream, which a finished run never mounts. This is the floor: the
    forensics are complete even when the log is truncated."""


class CompareDelta(BaseModel):
    metric: str
    base: float
    challenger: float
    direction_hint: DirectionHint


class CompareMovement(BaseModel):
    hid: str
    title: str
    rank: int
    previous_rank: int | None = None
    delta: int | None = None


class ImportReportResponse(BaseModel):
    """`POST /admin/reimport`. Counts of what one pass over the archive did."""

    imported: int = 0
    created: int = 0
    updated: int = 0
    hypotheses: int = 0
    reviews: int = 0
    matches: int = 0
    graft_events: int = 0
    overviews: int = 0
    legacy_bodies: int = 0
    repaired_strings: int = 0
    warnings: list[str] = []
    run_ids: list[str] = []


class HaltAllResponse(BaseModel):
    """`POST /admin/halt-all`. The runs the kill switch actually stopped."""

    stopped: list[str] = []


class CompareGraft(BaseModel):
    enabled: bool
    fired_count: int
    collapse_events: int


class CompareAnalytics(BaseModel):
    baseline: RunSummary
    challenger: RunSummary
    # False whenever either side lacks a recorded base prompt hash, which is every imported
    # run — the UI warns rather than implying the two runs answered the same question.
    shared_prompt: bool
    deltas: list[CompareDelta] = []
    movement: list[CompareMovement] = []
    graft_summary: dict[str, CompareGraft | None] = {}

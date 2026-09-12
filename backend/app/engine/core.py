"""Pure engine math for the co-scientist loop.

Every function here is a pure function of its arguments: no database, no filesystem, no
subprocess, no network, no clock. The orchestrator passes state in — including the round
number and a seeded `random.Random` — and persists whatever comes back. That is what
makes a resumed run reproducible and what lets these decisions be tested without a model.

The four families:

* **Elo** — `expected_score`, `k_for`, `elo_update`. Rating moves between the two sides of
  a match and is never created, so a leaderboard is comparable within a run (never across
  runs).
* **Pairing** — `make_pairs` builds the round's match plan. The orchestrator persists the
  plan before executing any of it, so a resumed run replays the plan rather than
  recomputing it.
* **Collapse detection** — `herfindahl`, `collapse_signals`, `should_fire_graft` port the
  v2 Cartographer Graft trigger (`archive/engine-source/v2/coscientist.py`), with one
  correction: the decision abstains when clustering produced no clusters at all. In the
  archived runs that skipped proximity, `n_clusters == 0` made two of the three signals
  vacuously true and the graft fired on an artefact.
* **Planning** — `compose_hypothesis_md` turns schema fields into the stored markdown body,
  and `estimate_calls` / `estimate_minutes` / `estimate_usd` / `suggested_budget_calls` are
  the single source of truth behind the wizard's presets, its estimate and the Confirm step.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.engine.models import DEFAULT_MODEL, price_for

__all__ = [
    "ABSTAIN_COOLDOWN",
    "ABSTAIN_DISABLED",
    "ABSTAIN_NO_CLUSTERS",
    "ABSTAIN_PARTIAL_CLUSTERS",
    "ABSTAIN_QUORUM",
    "ABSTAIN_STALE_CLUSTERS",
    "BUDGET_HEADROOM",
    "CONCENTRATED_POOL_SHARE",
    "GENERATION_SHARD_SIZE",
    "HYPOTHESIS_FIELD_ORDER",
    "INITIAL_ELO",
    "K_INITIAL",
    "K_SETTLE_AFTER",
    "K_SETTLED",
    "MIN_CLUSTER_COVERAGE",
    "NEWCOMER_MATCHES",
    "OVERVIEW_RESERVED_CALLS",
    "PARALLEL_CALLS",
    "CollapseSnapshot",
    "GraftConfig",
    "GraftDecision",
    "HypRow",
    "Pair",
    "RunConfig",
    "Side",
    "Signals",
    "TopShare",
    "calls_by_role",
    "collapse_signals",
    "compose_hypothesis_md",
    "elo_update",
    "estimate_calls",
    "estimate_minutes",
    "estimate_usd",
    "expected_score",
    "herfindahl",
    "k_for",
    "make_pairs",
    "meeting_key",
    "presentation_order",
    "presentation_swap",
    "should_fire_graft",
    "suggested_budget_calls",
    "top_share",
    "winner_side",
]

Side = Literal["a", "b"]

INITIAL_ELO = 1200.0
K_INITIAL = 32
K_SETTLED = 16
K_SETTLE_AFTER = 10
"""Matches each side must have played before its K-factor decays to `K_SETTLED`."""

NEWCOMER_MATCHES = 2
"""Below this many matches a hypothesis is a newcomer and is guaranteed a slot."""

GENERATION_SHARD_SIZE = 3
"""Hypotheses requested per generation shard; the batch is split into ceil(batch/3) calls."""

PARALLEL_CALLS = 3
"""Concurrent role calls the orchestrator allows (C3's Semaphore(3))."""

OVERVIEW_RESERVED_CALLS = 2
"""Calls reserved at run start so a stopped or exhausted run can still write its report."""

BUDGET_HEADROOM = 1.25
"""Preset budgets are the estimate plus this much slack (retries, contract repairs)."""

HYPOTHESIS_FIELD_ORDER = ("title", "claim", "mechanism", "novelty", "test", "assumptions")
"""Body field order. Must stay in step with `HYPOTHESIS_FIELDS` in `engine/schemas.py`."""

CONCENTRATED_POOL_SHARE = 0.50
"""Share of one label above which a pool counts as concentrated.

Used for two different populations, deliberately at the same number. As a *cluster* share
of the active pool it is what makes evolution reserve a divergent operator; as a *family*
share of the ideas born in one round it is the threshold the audit validated — 0.50 cleanly
separates the collapsed run (0.58, 0.55, 0.50, 1.00 across its later rounds) from the
healthy one (0.11 … 0.33). Nothing fires on the family reading yet: it is recorded and
watched first, because the validation used a taxonomy the user's own prompt supplied rather
than labels the proximity agent produced."""

ABSTAIN_DISABLED = "graft_disabled"
ABSTAIN_NO_CLUSTERS = "no_clusters"
ABSTAIN_QUORUM = "quorum_not_met"
ABSTAIN_COOLDOWN = "cooldown"
ABSTAIN_STALE_CLUSTERS = "clusters_stale"
"""The proximity call failed, so this round has no cluster measurement of its own.

`no_clusters` covers a pool that was never labelled. This covers the opposite trap: a pool
whose labels are all *last* round's, which is bit-identical to last round's by
construction — a plateau and a zero birth rate, which is exactly the quorum."""

ABSTAIN_PARTIAL_CLUSTERS = "clusters_partial"
"""Proximity returned a map covering only part of the pool it was shown.

The third face of the same problem, and the one that had no guard. `clusters_stale` covers
a call that failed outright and `no_clusters` a pool that was never labelled, but a call
that answered for six rows out of twelve produces a reading that is *quieter* the more of
the pool it missed: unlabelled rows sit in HHI's denominator and form no cluster, so
concentration is diluted and `n_clusters` under-counts. Deciding off that would let a
half-answered call vote."""

MIN_CLUSTER_COVERAGE = 0.75
"""Share of the measured pool that must carry a label before the votes are trusted."""

# Planning heuristics for `estimate_minutes` only — never used to decide anything at
# runtime. Grounded roles (generation, reflection, evolution) search the web and are the
# slow ones; the spread is wide because a call's length depends on the goal.
GROUNDED_CALL_SECONDS = (45.0, 150.0)
TOOLLESS_CALL_SECONDS = (20.0, 75.0)
GROUNDING_TIME_FACTOR = {"shallow": 0.7, "standard": 1.0, "deep": 1.4}

# Effort is the other thing that moves wall clock, and on a subscription it is the only
# axis that costs anything real: a higher effort call thinks for longer before it answers.
# The baseline is `medium`, because that is what the per-call seconds above were measured
# at. These are the same shape of heuristic as `GROUNDING_TIME_FACTOR` and just as
# approximate — the point is that an estimate stops being a lie when a run is configured
# at high effort throughout, not that the factor is exact.
EFFORT_TIME_FACTOR = {"low": 0.6, "medium": 1.0, "high": 1.6, "xhigh": 2.2, "max": 3.0}


# --------------------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class HypRow:
    """The engine's view of a hypothesis: everything the pure functions read, nothing else.

    The API's `HypothesisRow` DTO is a superset — this stays minimal so a change to the
    presentation layer cannot alter pairing or collapse detection.
    """

    hid: str
    elo: float = INITIAL_ELO
    matches: int = 0
    wins: int = 0
    cluster: str | None = None
    status: str = "active"


@dataclass(frozen=True, slots=True)
class Pair:
    """One planned match. `hid_a` is the higher-ranked side at planning time.

    `swapped` is presentation only: on a rematch the judge sees the sides in the opposite
    order, which cancels the position bias the archived runs baked in by always showing
    the same hypothesis first. Elo is always recorded against a/b, never against position.
    """

    hid_a: str
    hid_b: str
    swapped: bool = False


@dataclass(frozen=True, slots=True)
class CollapseSnapshot:
    """One round of cluster telemetry; the history these signals are computed over."""

    round: int
    n_active: int
    n_clusters: int
    hhi: float
    clusters: tuple[str, ...] = ()
    n_labelled: int = 0
    """How many of `n_active` carried a cluster label.

    Recorded because the gap is invisible in every other number here and reads backwards:
    an unlabelled row counts in HHI's denominator and forms no cluster, so the *worse* the
    clustering, the more diverse the pool appears. Run c4566ed2's round 2 read
    `n_active 12, n_clusters 6, hhi 0.042` with six rows carrying no label at all — had
    those six shared the six existing labels the reading would have been 0.167, four times
    higher. `coverage` is what says which of the two a reader is looking at."""

    @property
    def coverage(self) -> float:
        """Share of the measured population that carried a label. 1.0 when complete."""
        if self.n_active <= 0:
            return 0.0
        return round(self.n_labelled / self.n_active, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "n_active": self.n_active,
            "n_clusters": self.n_clusters,
            "hhi": self.hhi,
            "clusters": list(self.clusters),
            "n_labelled": self.n_labelled,
            "coverage": self.coverage,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CollapseSnapshot:
        return cls(
            round=_as_int(data.get("round"), 0),
            n_active=_as_int(data.get("n_active"), 0),
            n_clusters=_as_int(data.get("n_clusters"), 0),
            hhi=_as_float(data.get("hhi"), 0.0),
            clusters=tuple(data.get("clusters") or ()),
            # Snapshots written before coverage was recorded do not claim a coverage they
            # never measured: their labelled count falls back to the population size, which
            # is what the reading they produced assumed anyway.
            n_labelled=_as_int(data.get("n_labelled"), _as_int(data.get("n_active"), 0)),
        )


@dataclass(frozen=True, slots=True)
class Signals:
    """The three collapse votes for one round, plus the telemetry they were read from."""

    snapshot: CollapseSnapshot
    plateau: bool
    concentration: bool
    birth_rate: bool
    born: int
    have_window: bool

    @property
    def votes(self) -> int:
        return int(self.plateau) + int(self.concentration) + int(self.birth_rate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plateau": self.plateau,
            "concentration": self.concentration,
            "birth_rate": self.birth_rate,
            "born": self.born,
            "have_window": self.have_window,
            "votes": self.votes,
            "coverage": self.snapshot.coverage,
            "n_labelled": self.snapshot.n_labelled,
            "n_measured": self.snapshot.n_active,
        }


@dataclass(frozen=True, slots=True)
class GraftDecision:
    """Whether the Cartographer fires this round, and — when it does not — why."""

    fire: bool
    votes: int
    signals: Signals
    abstained_reason: str | None = None

    @property
    def round(self) -> int:
        return self.signals.snapshot.round

    @property
    def n_clusters(self) -> int:
        return self.signals.snapshot.n_clusters

    @property
    def hhi(self) -> float:
        return self.signals.snapshot.hhi

    def to_dict(self) -> dict[str, Any]:
        """Shaped for the `graft_events` row and the `graft_fired`/`graft_abstained` events."""
        return {
            "round": self.round,
            "fired": self.fire,
            "votes": self.votes,
            "n_clusters": self.n_clusters,
            "hhi": self.hhi,
            "abstained_reason": self.abstained_reason,
            "signals": self.signals.to_dict(),
            "clusters": list(self.signals.snapshot.clusters),
            "n_active": self.signals.snapshot.n_active,
        }


@dataclass(frozen=True, slots=True)
class GraftConfig:
    """Cartographer Graft knobs. Defaults are off and hardened, per the calibration spec."""

    enabled: bool = False
    quorum_k: int = 3  # calibrated against the 12 archived runs: the only false-fire-free
    # quorum (S1/S3 double-count "cluster picture static"); see docs/graft-calibration.md
    window: int = 3
    cooldown: int = 2
    hhi_threshold: float = 0.50
    birth_rate_threshold: int = 0
    plateau_slack: int = 0

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("graft window must be at least 1 round")
        if self.quorum_k < 1:
            raise ValueError("graft quorum_k must be at least 1 vote")
        if self.cooldown < 0:
            raise ValueError("graft cooldown cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "quorum_k": self.quorum_k,
            "window": self.window,
            "cooldown": self.cooldown,
            "hhi_threshold": self.hhi_threshold,
            "birth_rate_threshold": self.birth_rate_threshold,
            "plateau_slack": self.plateau_slack,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> GraftConfig:
        """Tolerant of both the API shape and v2's nested `thresholds` block."""
        if data is None:
            return cls()
        thresholds = data.get("thresholds") or {}
        defaults = cls()
        return cls(
            enabled=bool(data.get("enabled", defaults.enabled)),
            quorum_k=_as_int(data.get("quorum_k"), defaults.quorum_k),
            window=_as_int(data.get("window"), defaults.window),
            cooldown=_as_int(data.get("cooldown"), defaults.cooldown),
            hhi_threshold=_as_float(
                data.get("hhi_threshold", thresholds.get("hhi")), defaults.hhi_threshold
            ),
            birth_rate_threshold=_as_int(
                data.get("birth_rate_threshold", thresholds.get("birth_rate")),
                defaults.birth_rate_threshold,
            ),
            plateau_slack=_as_int(
                data.get("plateau_slack", thresholds.get("plateau_slack")), defaults.plateau_slack
            ),
        )


@dataclass(frozen=True, slots=True)
class RunConfig:
    """The immutable per-run configuration, as stored in `runs.config`.

    Only `rounds`, `generation_batch`, `matches_per_round`, `evolve_top_k` and
    `grounding_depth` affect the estimates; the rest travel together so callers can pass
    one object around.
    """

    rounds: int = 5
    generation_batch: int = 8
    matches_per_round: int = 6
    evolve_top_k: int = 3
    budget_calls: int = 150
    """The governor that actually stops a run. Always set, always enforced."""

    budget_usd: float | None = None
    """Optional dollar ceiling. `None` means no ceiling, which is the default.

    These are headless CLI calls on a subscription: nothing is billed per token, so
    `total_cost_usd` is API-equivalent telemetry rather than money. A dollar ceiling's only
    real effect on such a run is the failure it caused in practice — stopping one step short
    of the report it had already paid for. The enforcement path is kept intact for whoever
    later meters a metered API key; it is simply not imposed by default."""

    wall_clock_minutes: float | None = None
    """Optional elapsed-time ceiling. `None` means no ceiling.

    The guard against a pathological loop now that dollars are not one. Exceeding it is not
    a kill: the run finishes the way a `finish` control action does, writing its overview
    first."""

    grounding_depth: str = "standard"
    graft: GraftConfig = field(default_factory=GraftConfig)
    provider: str = "anthropic"
    """Which provider's models the tier assigns to rows the scientist did not override.

    A quick-set, not a lock: `model_table` is the truth, an override may name either
    provider's model, and the runner for a call is chosen from that row's model rather than
    from this field. What it *does* decide is the run's harness lane."""

    model_tier: str = "max"
    model_overrides: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """Per-role `{model, effort}` chosen for this run, merged over the tier's table.

    Kept in the config as well as baked into `model_table` so that "run again" inherits the
    choice rather than silently reverting to the tier default, and so the Settings tab can
    show which rows the scientist changed."""

    runner: str = "claude"
    workflow: str = "tournament"
    """Missing version means legacy resume. The launcher defaults NEW runs to adaptive."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "generation_batch": self.generation_batch,
            "matches_per_round": self.matches_per_round,
            "evolve_top_k": self.evolve_top_k,
            "budget_calls": self.budget_calls,
            "budget_usd": self.budget_usd,
            "wall_clock_minutes": self.wall_clock_minutes,
            "grounding_depth": self.grounding_depth,
            "graft": self.graft.to_dict(),
            "provider": self.provider,
            "model_tier": self.model_tier,
            "model_overrides": {
                role: dict(value) for role, value in self.model_overrides.items()
            },
            "runner": self.runner,
            "workflow": self.workflow,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RunConfig:
        """Missing keys fall back to defaults and unknown keys are ignored.

        `model_tier` is normalised through the tier alias table on the way in, so the
        stored configs that still say `balanced`/`quality`/`standard`/`maximum` read back as
        the tiers those names became. Their own `model_table` is untouched: this is what a
        *new* run cloned from them resolves, not a rewrite of what they ran. `provider` is
        absent from every config written before 2026-08-11 and defaults to `anthropic`,
        which is what those runs used.
        """
        # Imported here rather than at module scope on purpose: `schemas` imports this
        # module, and `runners` imports `schemas`, so a top-level import would close the
        # cycle and break `import app.engine.core`.
        from app.engine.models import normalise_provider
        from app.engine.runners import normalise_tier

        defaults = cls()
        return cls(
            rounds=_as_int(data.get("rounds"), defaults.rounds),
            generation_batch=_as_int(data.get("generation_batch"), defaults.generation_batch),
            matches_per_round=_as_int(data.get("matches_per_round"), defaults.matches_per_round),
            evolve_top_k=_as_int(data.get("evolve_top_k"), defaults.evolve_top_k),
            budget_calls=_as_int(data.get("budget_calls"), defaults.budget_calls),
            # A stored 0 is what "no ceiling" has always looked like in the database, and
            # an absent key now means the same thing. Both read back as None so there is
            # one representation of "unlimited" above the storage layer.
            budget_usd=_as_ceiling(data.get("budget_usd")),
            wall_clock_minutes=_as_ceiling(data.get("wall_clock_minutes")),
            grounding_depth=str(data.get("grounding_depth") or defaults.grounding_depth),
            graft=GraftConfig.from_mapping(data.get("graft")),
            provider=normalise_provider(data.get("provider") or defaults.provider),
            model_tier=normalise_tier(data.get("model_tier")),
            model_overrides=_as_overrides(data.get("model_overrides")),
            runner=str(data.get("runner") or defaults.runner),
            workflow=str(data.get("workflow") or "tournament"),
        )


def _as_overrides(value: Any) -> dict[str, dict[str, str]]:
    """Read the per-role override block off a stored config, keeping only stated fields.

    Nothing is validated here — this module is pure math and does not own the allowlist.
    `resolve_model_table` refuses a bad model, effort or role name when it builds the table.
    """
    if not isinstance(value, Mapping):
        return {}
    cleaned: dict[str, dict[str, str]] = {}
    for role, choice in value.items():
        if not isinstance(choice, Mapping):
            continue
        row = {
            key: str(choice[key])
            for key in ("model", "effort")
            if choice.get(key) not in (None, "")
        }
        if row:
            cleaned[str(role)] = row
    return cleaned


def _as_ceiling(value: Any) -> float | None:
    """An optional ceiling. Absent or unreadable means "no ceiling"; a number is kept as-is.

    A zero is deliberately *not* folded into None here. Zero would then mean "unlimited",
    so a caller who asked for a ceiling of nothing would silently get the opposite of what
    they asked for — the launcher refuses it instead, which is the answer that tells them.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int) -> int:
    return default if value is None else int(value)


def _as_float(value: Any, default: float) -> float:
    return default if value is None else float(value)


def _as_run_config(config: RunConfig | Mapping[str, Any] | Any) -> RunConfig:
    """Accept the engine's own config, a plain mapping, or a pydantic/attr object."""
    if isinstance(config, RunConfig):
        return config
    if isinstance(config, Mapping):
        return RunConfig.from_mapping(config)
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        return RunConfig.from_mapping(dump())
    data = getattr(config, "__dict__", None)
    if isinstance(data, Mapping):
        return RunConfig.from_mapping(data)
    raise TypeError(f"cannot read a run config from {type(config).__name__}")


# ----------------------------------------------------------------------------- elo


def expected_score(elo_a: float, elo_b: float) -> float:
    """Probability that A beats B under the standard logistic Elo curve."""
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def k_for(matches_a: int, matches_b: int) -> int:
    """K decays only once BOTH sides are settled.

    A veteran meeting a newcomer keeps the high K: the newcomer's rating is the uncertain
    one, and halving K there would strand it near its starting rating for the whole run.
    """
    if matches_a >= K_SETTLE_AFTER and matches_b >= K_SETTLE_AFTER:
        return K_SETTLED
    return K_INITIAL


def elo_update(elo_a: float, elo_b: float, winner: Side, k: int) -> tuple[float, float]:
    """Apply one match result. Returns the new (elo_a, elo_b).

    The rating change is rounded once and then applied symmetrically — A gains exactly
    what B loses — so the pool's total rating is conserved no matter how many matches a
    run plays. Rounding each side independently would leak up to 0.05 per match.
    """
    if winner not in ("a", "b"):
        raise ValueError(f"winner must be 'a' or 'b', got {winner!r}")
    score_a = 1.0 if winner == "a" else 0.0
    delta = round(k * (score_a - expected_score(elo_a, elo_b)), 1)
    return round(elo_a + delta, 1), round(elo_b - delta, 1)


# ------------------------------------------------------------------------- pairing


def meeting_key(hid_a: str, hid_b: str) -> tuple[str, str]:
    """Order-independent identity of a matchup, for de-duplication and rematch counts."""
    return (hid_a, hid_b) if hid_a <= hid_b else (hid_b, hid_a)


def presentation_order(pair: Pair) -> tuple[str, str]:
    """The hids in the order the judge sees them (position 1, position 2)."""
    return (pair.hid_b, pair.hid_a) if pair.swapped else (pair.hid_a, pair.hid_b)


def presentation_swap(hid_a: str, hid_b: str, meetings_played: int) -> bool:
    """Whether this meeting of `hid_a` and `hid_b` shows `hid_b` first.

    Anchored on the **canonical** meeting key, not on the row's a/b order, and that is the
    whole content of this function. `swapped` used to be `meetings % 2` against whichever
    side the plan happened to put first, and the plan puts the better-placed side first *at
    that round's planning time* — so when the two swap places in the standings between
    meetings, the a/b order flips too, and the flag that exists to reverse the presentation
    order restores it instead. The judge then sees the identical order twice, which is
    exactly the position bias the mechanism is for. Two of the 30 rematches in the live
    database are in that state (`b41853cd` rounds 1/2, `49d9444d` rounds 2/7).

    Deriving the order from the pair's own identity makes it independent of a/b churn:
    meeting 0 shows the lower hid first, meeting 1 the higher, whatever the standings did
    in between. Winner mapping is untouched — `winner_side` still resolves a position
    against `hid_a`.
    """
    return meeting_key(hid_a, hid_b)[meetings_played % 2] != hid_a


def winner_side(pair: Pair, winner_position: int) -> Side:
    """Map the judge's `winner` (1 or 2, a position) onto the pair's a/b side."""
    if winner_position not in (1, 2):
        raise ValueError(f"winner position must be 1 or 2, got {winner_position!r}")
    first, second = presentation_order(pair)
    return "a" if (first if winner_position == 1 else second) == pair.hid_a else "b"


def make_pairs(
    standings: Sequence[HypRow],
    per_round: int,
    rng: random.Random,
    *,
    prior_meetings: Mapping[tuple[str, str], int] | None = None,
) -> list[Pair]:
    """Plan this round's matches.

    Candidates are adjacent pairs in the standings — the closest-rated opponents, whose
    result carries the most information about the order. On top of that:

    * **No same-cluster pairs.** Two hypotheses the proximity agent called the same idea
      tell us nothing by fighting each other. Unclustered hypotheses are never assumed
      similar, so two `cluster=None` rows may meet.
    * **Newcomers are guaranteed a slot** (fewer than `NEWCOMER_MATCHES` matches). If a
      newcomer's neighbours all share its cluster, it reaches further out in the standings
      for the nearest legal opponent, rather than sitting the round out at 1200.
    * **Least-played first** for the remaining slots, so compute goes where the ranking is
      still uncertain.
    * **Rematches swap presentation order** (`Pair.swapped`), cancelling judge position bias.

    Ties are broken by `rng`, so a run seeded from its own id plans the same matches on
    replay. Returns at most `per_round` pairs, each distinct; an empty list is a legal
    outcome (fewer than two active hypotheses, or every legal opponent filtered out) and
    means the round has no tournament.
    """
    if per_round <= 0:
        return []
    ranked = sorted(
        (row for row in standings if row.status == "active"),
        key=lambda row: (-row.elo, row.hid),
    )
    if len(ranked) < 2:
        return []

    by_hid = {row.hid: row for row in ranked}
    position = {row.hid: index for index, row in enumerate(ranked)}

    def legal(left: HypRow, right: HypRow) -> bool:
        return left.cluster is None or left.cluster != right.cluster

    # candidate pool: adjacent in the standings, keyed so a pair can only appear once
    candidates: dict[tuple[str, str], tuple[str, str]] = {}
    for index in range(len(ranked) - 1):
        left, right = ranked[index], ranked[index + 1]
        if legal(left, right):
            candidates[meeting_key(left.hid, right.hid)] = (left.hid, right.hid)

    # a newcomer with no candidate yet reaches outward for its nearest legal opponent
    for row in ranked:
        if row.matches >= NEWCOMER_MATCHES:
            continue
        if any(row.hid in pair for pair in candidates.values()):
            continue
        partner = _nearest_legal_partner(ranked, position[row.hid], legal)
        if partner is not None:
            ahead = position[row.hid] < position[partner]
            ordered = (row.hid, partner) if ahead else (partner, row.hid)
            candidates[meeting_key(*ordered)] = ordered

    if not candidates:
        return []

    pool = list(candidates.values())
    rng.shuffle(pool)  # shuffle first so the stable sort below breaks ties by seed

    # Slots already handed out *this round* count too. Reading only the frozen start-of-round
    # counts let phase 2 give one hypothesis two or three of the round's matches while an
    # equally new one sat the round out at its starting rating: 6.4% of filled rounds in a
    # 20,000-pool fuzz, and confirmed on real rounds — in run 146282fa round 2 both matches
    # went to h005 while h001 and h003 were still newcomers and played nothing.
    taken_count: Counter[str] = Counter()

    def least_played(pair: tuple[str, str]) -> tuple[int, int]:
        played_a = by_hid[pair[0]].matches + taken_count[pair[0]]
        played_b = by_hid[pair[1]].matches + taken_count[pair[1]]
        return (min(played_a, played_b), played_a + played_b)

    ordered_pool = sorted(pool, key=least_played)

    selected: list[tuple[str, str]] = []
    taken: set[tuple[str, str]] = set()

    def take(pair: tuple[str, str]) -> None:
        selected.append(pair)
        taken.add(meeting_key(*pair))
        taken_count[pair[0]] += 1
        taken_count[pair[1]] += 1

    # phase 1: one slot each for the newcomers, least-played first
    newcomers = [row for row in ranked if row.matches < NEWCOMER_MATCHES]
    newcomers.sort(key=lambda row: (row.matches, position[row.hid]))
    for row in newcomers:
        if len(selected) >= per_round:
            break
        if any(row.hid in pair for pair in selected):
            continue
        options = [
            pair
            for pair in ordered_pool
            if row.hid in pair and meeting_key(*pair) not in taken
        ]
        if options:
            # The least-played *partner*, counting slots already handed out this round.
            # Taking the first candidate instead gave a newcomer that had already been
            # paired a second match while another newcomer sat the round out entirely.
            take(min(options, key=least_played))

    # phase 2: fill the remaining slots by least-played, re-sorted against what this round
    # has already handed out. The pool is at most N-1 entries and the tie-break shuffle
    # already happened, so re-sorting per slot is free and stays deterministic.
    while len(selected) < per_round:
        remaining = [pair for pair in ordered_pool if meeting_key(*pair) not in taken]
        if not remaining:
            break
        take(min(remaining, key=least_played))

    meetings = prior_meetings or {}
    return [
        Pair(
            hid_a=pair[0],
            hid_b=pair[1],
            swapped=presentation_swap(*pair, meetings.get(meeting_key(*pair), 0)),
        )
        for pair in selected
    ]


def _nearest_legal_partner(ranked: Sequence[HypRow], index: int, legal: Any) -> str | None:
    """Walk outward from `index` in the standings for the closest legal opponent."""
    for distance in range(1, len(ranked)):
        for neighbour in (index - distance, index + distance):
            if 0 <= neighbour < len(ranked) and legal(ranked[index], ranked[neighbour]):
                return ranked[neighbour].hid
    return None


# ------------------------------------------------------------------------ collapse


def herfindahl(active: Sequence[HypRow]) -> float:
    """Concentration of the active pool across clusters: 1.0 = one cluster, ~1/k = even.

    Unclustered hypotheses count in the denominator but form no cluster of their own, so a
    pool the proximity agent never labelled scores 0.0 rather than looking diverse.
    """
    total = len(active)
    if total == 0:
        return 0.0
    sizes = Counter(row.cluster for row in active if row.cluster is not None)
    if not sizes:
        return 0.0
    return round(sum((size / total) ** 2 for size in sizes.values()), 3)


@dataclass(frozen=True, slots=True)
class TopShare:
    """How much of one population sits under its single most common label.

    Deliberately not Herfindahl. HHI over the *cumulative* pool is the instrument that
    failed: replayed over the collapsed run it peaks at 0.137, below every threshold, while
    the run's own reader could see it had stopped producing different kinds of idea. A
    plain top-label share over a *flow* — the ideas one round created — is the reading that
    separated the two runs, so this measures exactly that and nothing more.

    `n_labelled` is the denominator and `n_total` is the population it was drawn from. They
    differ when a label is missing, and the gap has to stay visible: a share of 1.00 over
    one labelled idea out of six is not a collapsed round, it is an unlabelled one.
    """

    label: str | None = None
    share: float = 0.0
    n_labelled: int = 0
    n_total: int = 0
    counts: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "share": self.share,
            "n_labelled": self.n_labelled,
            "n_total": self.n_total,
            "counts": {label: count for label, count in self.counts},
        }


def top_share(labels: Sequence[str | None]) -> TopShare:
    """The most common label's share of the labelled members of `labels`.

    Ties break on the label text so the reading is deterministic across replays; a caller
    that needs the tie itself has `counts`.
    """
    total = len(labels)
    kept = [label for label in labels if label]
    if not kept:
        return TopShare(n_total=total)
    sizes = Counter(kept)
    counts = tuple(sorted(sizes.items(), key=lambda item: (-item[1], item[0])))
    label, size = counts[0]
    return TopShare(
        label=label,
        share=round(size / len(kept), 3),
        n_labelled=len(kept),
        n_total=total,
        counts=counts,
    )


def collapse_signals(
    current_round: int,
    active: Sequence[HypRow],
    history: Sequence[CollapseSnapshot],
    graft: GraftConfig,
) -> Signals:
    """Compute this round's three collapse votes over `history` plus the current pool.

    Ported from v2's `cmd_collapse_check`, including the rule that S1 and S3 need a full
    window before they may vote (`history` holding at least `graft.window` earlier rounds):

    * **S1 plateau** — the distinct-cluster count has not moved by more than
      `plateau_slack` across the window.
    * **S2 concentration** — Herfindahl over cluster sizes is at or above `hhi_threshold`.
    * **S3 birth rate** — at most `birth_rate_threshold` cluster labels are new since the
      start of the window.

    `history` is the earlier rounds only; the snapshot for `current_round` is computed here
    and returned on the result for the caller to append. The signals stay honest when
    clustering produced nothing — `should_fire_graft` is where that abstains.
    """
    clusters = sorted({row.cluster for row in active if row.cluster is not None})
    snapshot = CollapseSnapshot(
        round=current_round,
        n_active=len(active),
        n_clusters=len(clusters),
        hhi=herfindahl(active),
        clusters=tuple(clusters),
        n_labelled=sum(1 for row in active if row.cluster is not None),
    )

    window = [*history, snapshot][-(graft.window + 1) :]
    have_window = len(window) > graft.window
    counts = [record.n_clusters for record in window]
    oldest = set(window[0].clusters) if have_window else set()
    born = len(set(snapshot.clusters) - oldest)

    return Signals(
        snapshot=snapshot,
        plateau=have_window and (max(counts) - min(counts)) <= graft.plateau_slack,
        concentration=snapshot.hhi >= graft.hhi_threshold,
        birth_rate=have_window and born <= graft.birth_rate_threshold,
        born=born,
        have_window=have_window,
    )


def should_fire_graft(
    signals: Signals,
    graft: GraftConfig,
    current_round: int,
    last_fired_round: int | None,
) -> GraftDecision:
    """Decide whether to invoke the Cartographer this round.

    Guards, in order — the reason recorded is the first one that applies:

    1. `graft_disabled` — the run turned the organ off (the default).
    2. `no_clusters` — **the abstain that v2 lacked.** With no clusters at all the plateau
       and birth-rate signals are vacuously true, so a run whose proximity step was
       skipped would fire the graft every round on nothing. Four of the twelve archived
       runs are in exactly that state.
    3. `clusters_partial` — proximity answered for less than `MIN_CLUSTER_COVERAGE` of the
       pool. A partial map dilutes concentration and under-counts clusters, so its votes
       are readings of the call's incompleteness rather than of the pool.
    4. `quorum_not_met` — fewer than `quorum_k` votes; no collapse detected.
    5. `cooldown` — collapse detected, but the organ fired too recently. Firing on
       consecutive rounds thrashes the generation prompt faster than the tournament can
       judge the result.
    """
    votes = signals.votes
    if not graft.enabled:
        return GraftDecision(False, votes, signals, ABSTAIN_DISABLED)
    if signals.snapshot.n_clusters == 0:
        return GraftDecision(False, votes, signals, ABSTAIN_NO_CLUSTERS)
    if signals.snapshot.coverage < MIN_CLUSTER_COVERAGE:
        return GraftDecision(False, votes, signals, ABSTAIN_PARTIAL_CLUSTERS)
    if votes < graft.quorum_k:
        return GraftDecision(False, votes, signals, ABSTAIN_QUORUM)
    cooled = last_fired_round is None or (current_round - last_fired_round) > graft.cooldown
    if not cooled:
        return GraftDecision(False, votes, signals, ABSTAIN_COOLDOWN)
    return GraftDecision(True, votes, signals, None)


# --------------------------------------------------------------------- composition


_SELF_RANK = re.compile(
    r"^\s*(?:rank|idea|hypothesis|option)\s*#?\s*\d+"
    r"(?:\s+of\s+(?:\d+|this\s+shard))?\s*[:.)\-–—]\s*",
    re.I,
)
"""The `of N` tail is not decoration: run c4566ed2's shards wrote `Rank 1 of this shard.`
and `Rank 1 of 3.`, neither of which the delimiter-only pattern could see."""


def _without_self_rank(title: str) -> str:
    """Strip a generation shard's own numbering out of a title.

    A shard asked for three hypotheses sometimes numbers them, and that numbering is then
    planted in the ranking judge's prompt as part of the hypothesis text — where the role
    prompt has just told the judge to ignore scores in the reviews and to ignore position.
    Run c4566ed2's second shard emitted titles beginning `Rank 1:`, `Rank 2:` and `Rank 3:`,
    so three of its six hypotheses carried a self-declared ordering into their matches and
    the other three did not, which is an asymmetry between the two sides of a match.
    """
    return _SELF_RANK.sub("", title.strip()).strip()


def compose_hypothesis_md(fields: Mapping[str, Any]) -> str:
    """Render the schema fields as the hypothesis body the run stores and exports.

    The orchestrator owns every file write, so this is the only place a hypothesis body
    takes shape — the archived engines let each agent write its own markdown, which is how
    five of twelve runs ended up with mojibake and unusable titles.
    """
    missing = [name for name in HYPOTHESIS_FIELD_ORDER if name not in fields]
    if missing:
        raise ValueError(f"missing hypothesis fields: {', '.join(missing)}")

    title = _without_self_rank(str(fields["title"])) or "untitled"
    lines = [f"# {title}"]
    for name in HYPOTHESIS_FIELD_ORDER[1:]:
        value = str(fields[name]).strip()
        if name == "claim":
            # The same strip as the title, for the same reason and one line further down.
            # Stripping only the title left run c4566ed2's h016–h018 with clean titles and
            # claims beginning "Rank 1 of this shard." / "Rank 2 of this shard." — and the
            # claim is the line `claim_of` lifts into the proximity list and the one the
            # ranking judge reads first, so the shard's self-declared ordering survived
            # into exactly the two prompts the strip exists to keep it out of.
            value = _without_self_rank(value)
        lines.append(f"**{name.capitalize()}:** {value}")
    return "\n\n".join(lines) + "\n"


# ----------------------------------------------------------------------- estimates


def estimate_calls(config: RunConfig | Mapping[str, Any] | Any) -> int:
    """Model calls a run of this shape will make, following C3's step list exactly.

    Per round, with `B` = generation_batch, `M` = matches_per_round, `V` = evolve_top_k:

    | step | calls |
    |---|---|
    | 1. generation, sharded | `ceil(B / 3)` |
    | 2. reflection, one per hypothesis lacking a review | `B` this round `+ V` carried in |
    | 3. proximity | `1` |
    | 4. collapse check | `0` — pure function |
    | 5. ranking | `M` |
    | 6. evolution | `1` |
    | 7. meta-review | `1` |

    Step 6 produces `V` hypotheses *after* step 2 has run, so their reflections land in the
    next round: reflection costs `B` in round 1 and `B + V` in every round after it. Over
    `R` rounds that is `R·B + (R−1)·V` — the final round's evolved hypotheses are never
    reviewed, which is also why they cannot win a match they were never ranked in.

        estimate = 2 + R·(ceil(B/3) + B + M + 3) + (R−1)·V

    The leading 2 is `OVERVIEW_RESERVED_CALLS`, reserved at run start so that stopping or
    exhausting the budget still writes a report.

    **Excluded: the cartographer.** It costs one call and only when the graft fires, which
    on a healthy pool is never. Estimating it would inflate every run's budget for an event
    the collapse guard exists to make rare; a fire draws from the same budget as any other
    call and is reported in the ledger.

    Rejections are not modelled either: a rejected hypothesis has already cost its
    reflection call, and it stops costing ranking calls, so the estimate is an upper bound
    on the tournament side.
    """
    cfg = _as_run_config(config)
    if cfg.workflow == "adaptive":
        return sum(calls_by_role(cfg).values())
    rounds = max(0, cfg.rounds)
    if rounds == 0:
        return OVERVIEW_RESERVED_CALLS

    batch = max(0, cfg.generation_batch)
    matches = max(0, cfg.matches_per_round)
    evolved = max(0, cfg.evolve_top_k)
    shards = math.ceil(batch / GENERATION_SHARD_SIZE)

    per_round = shards + batch + matches + 3  # + proximity, evolution, meta-review
    return OVERVIEW_RESERVED_CALLS + rounds * per_round + (rounds - 1) * evolved


def calls_by_role(config: RunConfig | Mapping[str, Any] | Any) -> dict[str, int]:
    """`estimate_calls` broken out per role. The two always sum to the same number.

    Needed because the roles no longer share a model: once a tier or an override puts a
    more expensive model on generation than on proximity, a run's cost depends on *which*
    roles the calls land on, not just how many there are.
    """
    cfg = _as_run_config(config)
    if cfg.workflow == "adaptive":
        rounds = max(0, cfg.rounds)
        # Conservative planning envelope: up to four routes and one reframe per
        # checkpoint; choose the more expensive creative branch. Retries are headroom.
        batch, evolved = max(4, cfg.generation_batch), max(1, cfg.evolve_top_k)
        exploration = 4 + batch >= 1 + evolved
        return {"framing": rounds, "generation": 4 * rounds if exploration else 0,
                "evolution": 0 if exploration else rounds,
                "reflection": rounds * (batch if exploration else evolved),
                "verification": rounds * max(3, min(cfg.matches_per_round, 6)),
                "synthesis": rounds + 1, "challenge": rounds + 1}
    rounds = max(0, cfg.rounds)
    counts = {
        "generation": 0,
        "reflection": 0,
        "proximity": 0,
        "ranking": 0,
        "evolution": 0,
        "meta_review": 0,
        # Reserved at run start, so a stopped or exhausted run can still write its report.
        "overview": OVERVIEW_RESERVED_CALLS,
    }
    if rounds == 0:
        return counts

    batch = max(0, cfg.generation_batch)
    matches = max(0, cfg.matches_per_round)
    evolved = max(0, cfg.evolve_top_k)

    counts["generation"] = rounds * math.ceil(batch / GENERATION_SHARD_SIZE)
    # Evolved hypotheses are reviewed in the round after the one that made them, so every
    # round but the first carries `evolve_top_k` extra reviews in.
    counts["reflection"] = rounds * batch + (rounds - 1) * evolved
    counts["proximity"] = rounds
    counts["ranking"] = rounds * matches
    counts["evolution"] = rounds
    counts["meta_review"] = rounds
    return counts


def estimate_usd(
    config: RunConfig | Mapping[str, Any] | Any,
    model_table: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[float, float]:
    """Dollar bracket (low, high) for a run, weighted by which model each role runs on.

    A planning heuristic, and labelled as one everywhere it is shown — but a *calibrated*
    one. The per-call brackets in `engine/models.py` come from two real runs, scaled by
    list price, and every model in the allowlist prices output at exactly 5× input, so the
    scaling is a single exact ratio rather than an assumption about the token mix.

    What it deliberately does not model: the cartographer (one call, only when the graft
    fires), retries, and the fact that a rejected hypothesis stops costing ranking calls.
    The first two push the real number up and the third pushes it down. The ceiling that
    actually stops a run is `budget_usd`, which the engine enforces; this only says where
    to set it.

    With no `model_table`, every role is priced at the floor — which is what a caller
    asking about a run that does not exist yet should get.
    """
    models = {
        str(row["role"]): str(row["model"])
        for row in (model_table or [])
        if row.get("role") and row.get("model")
    }
    low = high = 0.0
    for role, calls in calls_by_role(config).items():
        if not calls:
            continue
        bracket = price_for(models.get(role, DEFAULT_MODEL)).usd_per_call
        low += calls * bracket[0]
        high += calls * bracket[1]
    return (round(low, 2), round(high, 2))


def suggested_budget_calls(config: RunConfig | Mapping[str, Any] | Any) -> int:
    """The estimate plus `BUDGET_HEADROOM`. The wizard's presets are exactly this."""
    return math.ceil(estimate_calls(config) * BUDGET_HEADROOM)


def estimate_minutes(
    config: RunConfig | Mapping[str, Any] | Any,
    model_table: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[int, int]:
    """Wall-clock bracket (low, high) in minutes for a run of this shape.

    Calls are not serial: generation shards, reflections and rankings each run
    `PARALLEL_CALLS` at a time, so the cost is the number of *waves*, not of calls.
    Grounded roles (generation, reflection, evolution) search the web and dominate;
    grounding depth stretches them by `GROUNDING_TIME_FACTOR`.

    Given a `model_table`, effort stretches them too. This matters more than it used to:
    on a subscription, minutes are the scarce resource rather than dollars, and
    `wall_clock_minutes` is a ceiling somebody has to pick a number for. An estimate that
    ignored the difference between a table at `medium` and one at `high` would send them
    into the same failure the dollar ceiling used to cause — a run stopped one step short
    of its report, by a limit set from a number that was never true.

    The per-call seconds are planning heuristics, not measurements, and the bracket is
    wide on purpose — a run's length depends on the goal. Nothing at runtime reads this;
    it exists so the Confirm step can tell a scientist roughly what they are committing to.
    """
    cfg = _as_run_config(config)
    if cfg.workflow == "adaptive":
        # Planning envelope, not a promise about model/search latency.
        from app.engine.runners import GROUNDED_ROLES

        counts = calls_by_role(cfg)
        efforts = {str(row["role"]): EFFORT_TIME_FACTOR.get(str(row.get("effort")), 1.0)
                   for row in model_table or []}
        depth = GROUNDING_TIME_FACTOR.get(cfg.grounding_depth, 1.0)
        def duration(bound: int) -> float:
            return sum(count * efforts.get(role, 1.0) * (
                GROUNDED_CALL_SECONDS[bound] * depth / PARALLEL_CALLS
                if role in GROUNDED_ROLES and role != "challenge" else
                GROUNDED_CALL_SECONDS[bound] * depth if role == "challenge" else
                TOOLLESS_CALL_SECONDS[bound]) for role, count in counts.items()) / 60
        low = max(1, math.floor(duration(0)))
        return low, max(low + 1, math.ceil(duration(1)))
    rounds = max(0, cfg.rounds)
    batch = max(0, cfg.generation_batch)
    matches = max(0, cfg.matches_per_round)
    evolved = max(0, cfg.evolve_top_k)
    shards = math.ceil(batch / GENERATION_SHARD_SIZE)
    effort = _effort_factors(model_table)

    # Waves are counted separately per role, because effort is a per-role setting: a table
    # with proximity at low and reflection at high stretches by different amounts in the
    # two halves of a round.
    grounded = 0.0
    toolless = float(effort["overview"])  # the report at the end of the run
    for round_index in range(rounds):
        reflected = batch + (evolved if round_index else 0)
        grounded += (
            _waves(shards) * effort["generation"]
            + _waves(reflected) * effort["reflection"]
            + effort["evolution"]
        )
        toolless += (
            effort["proximity"]
            + _waves(matches) * effort["ranking"]
            + effort["meta_review"]
        )

    depth = GROUNDING_TIME_FACTOR.get(cfg.grounding_depth, 1.0)

    def seconds(bound: int) -> float:
        return (
            grounded * GROUNDED_CALL_SECONDS[bound] * depth
            + toolless * TOOLLESS_CALL_SECONDS[bound]
        )

    low_minutes = max(1, math.floor(seconds(0) / 60))
    return low_minutes, max(low_minutes + 1, math.ceil(seconds(1) / 60))


def _effort_factors(model_table: Sequence[Mapping[str, Any]] | None) -> dict[str, float]:
    """Per-role time multipliers from a resolved table, defaulting to the `medium` baseline.

    Without a table every role comes back at 1.0, so the estimate is exactly what it was
    before effort entered it — a caller that has not resolved a table yet is not punished
    with a number invented from a tier it did not choose.
    """
    factors = dict.fromkeys(
        (
            "generation",
            "reflection",
            "proximity",
            "ranking",
            "evolution",
            "meta_review",
            "overview",
        ),
        1.0,
    )
    for row in model_table or []:
        role = str(row.get("role") or "")
        if role in factors:
            factors[role] = EFFORT_TIME_FACTOR.get(str(row.get("effort") or ""), 1.0)
    return factors


def _waves(calls: int) -> int:
    """Rounds of `PARALLEL_CALLS` concurrent calls needed to run `calls` of them."""
    return math.ceil(calls / PARALLEL_CALLS) if calls > 0 else 0

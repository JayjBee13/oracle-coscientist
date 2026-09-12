"""Replay the archived runs through the collapse detector to calibrate the graft.

The Cartographer Graft fires a divergence seed into generation when the idea pool is
detected to be collapsing. `cartographer_graft_spec.md` §6 says the trigger's parameters
must be *fit on logged runs*, not asserted — v2 shipped `quorum_k=1` uncalibrated against
its own spec, and one archived run fired on an artefact because of it.

This module is the fit. It reads the twelve archived `state.json` files, reconstructs what
the active pool looked like at each round, and feeds that through `engine.core`'s
`collapse_signals` / `should_fire_graft` across a parameter grid. It computes no signal of
its own: the point is to tune the functions the engine actually runs, so any drift between
"what we calibrated" and "what ships" is impossible by construction.

**Reconstruction, and what it costs.** An archived `state.json` stores one row per
hypothesis holding its *final* cluster label and *final* status — not a per-round history.
Rebuilding the rounds therefore relies on three rules, each of which is a documented
approximation:

1. A hypothesis is active in round `r` if `created_iter <= r` and its final status is
   `active`. Reflection rejects in the same round a hypothesis is born, so a rejected row
   was never in a tournament; a row archived as a proximity duplicate *was* active until
   some unrecorded round, and counting it as never-active matched the recorded telemetry
   better than counting it as always-active (17 exact rounds against 12 of 22).
2. Evolution runs *after* the cluster step, so its offspring are not in the pool the
   collapse check saw that round. Rows carrying a `parent` are therefore held back one
   round. This single correction is what lifts agreement with v2's recorded `n_active`
   from 12 to 17 of 22 rounds.
3. Cluster labels are the final ones. Where the proximity agent re-minted its label
   namespace between rounds — it did in one of the four runs where this is measurable —
   earlier rounds are reconstructed with labels they did not carry at the time. Cluster
   *counts* and Herfindahl survive this; label *identity*, which the birth-rate signal
   reads, does not.

The six v2 runs logged their own telemetry, so rules 1–3 are scored rather than assumed:
`reconstruction_fidelity` compares reconstruction against those recordings, and
`legacy_v2_fires` reproduces the archived engine's decision rule so the one real historical
fire can be checked round-for-round. See `docs/graft-calibration.md` for the results.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.engine.core import (
    CollapseSnapshot,
    GraftConfig,
    GraftDecision,
    HypRow,
    collapse_signals,
    should_fire_graft,
)

__all__ = [
    "ARCHIVE_VERSIONS",
    "COLLAPSED",
    "DEGENERATE",
    "DEFAULT_COOLDOWNS",
    "DEFAULT_QUORUMS",
    "DEFAULT_WINDOWS",
    "HEALTHY",
    "RECOMMENDED",
    "RUN_LABELS",
    "FidelityRow",
    "GridCell",
    "Label",
    "ReplayResult",
    "ReplayRun",
    "RoundReconstruction",
    "RunLabel",
    "grid_search",
    "legacy_v2_fires",
    "load_archive",
    "load_state",
    "recorded_snapshots",
    "reconstruct_active",
    "reconstruct_run",
    "reconstruction_fidelity",
    "replay_run",
]

Label = Literal["healthy", "collapsed", "degenerate"]

HEALTHY: Label = "healthy"
"""The pool kept opening regions; the graft should never fire here."""

COLLAPSED: Label = "collapsed"
"""The pool visibly converged onto one region; the graft should fire, and promptly."""

DEGENERATE: Label = "degenerate"
"""Clustering never produced a label, so there is no collapse evidence either way — only a
test of the abstain guard. These runs validate that the engine stays silent when its own
instrumentation failed."""

ARCHIVE_VERSIONS = ("v1", "v2")

DEFAULT_QUORUMS = (1, 2, 3)
DEFAULT_WINDOWS = (2, 3, 4)
DEFAULT_COOLDOWNS = (1, 2, 3)

RECOMMENDED = GraftConfig(enabled=False, quorum_k=3, window=3, cooldown=2)
"""The calibrated defaults. Rationale in full in `docs/graft-calibration.md`; in short:

no archived run collapsed, so no setting can be validated for recall, and the only thing
the corpus *can* measure is false fires. `quorum_k=3` is the only quorum that produces
zero of them across all twelve runs at every window and cooldown, because it is the only
one that forces the concentration signal — the sole signal that measures the thing the
organ exists to detect — to participate. Every false fire in the corpus is the plateau and
birth-rate signals voting together while concentration sits at 0.03–0.04, i.e. while the
pool is at its *most* diverse. The organ still ships disabled.
"""


@dataclass(frozen=True, slots=True)
class RunLabel:
    """A ground-truth label plus the reasoning that produced it.

    §6 of the spec asks for hand-labelled collapse episodes and names none, so every label
    here was assigned by inspecting the reconstructed telemetry. The rationale travels with
    the label so a reader can disagree with a specific call rather than the whole table.
    """

    label: Label
    rationale: str


RUN_LABELS: Mapping[str, RunLabel] = {
    "run-20260530-222448": RunLabel(
        HEALTHY,
        "Clusters grow 5 to 9 over three rounds while Herfindahl falls 0.200 to 0.074. "
        "No signal votes in any round. The pool was still opening regions when the run ended.",
    ),
    "run-20260530-231933": RunLabel(
        HEALTHY,
        "Five rounds, clusters 6 to 16, Herfindahl 0.167 to 0.036, zero votes throughout. "
        "The most clearly diverging run in the corpus.",
    ),
    "run-20260531-002116": RunLabel(
        HEALTHY,
        "Proximity ran once, in round 1, labelling 7 of 18 hypotheses and never running again. "
        "The frozen label set makes the plateau and birth-rate signals vote by round 4 while "
        "Herfindahl actually falls 0.143 to 0.031 as the pool grows. Stale instrumentation, "
        "not convergence: the concentration signal never votes.",
    ),
    "run-20260602-064935": RunLabel(
        DEGENERATE,
        "All 23 hypotheses carry cluster=null; the proximity step never ran. There is no "
        "cluster structure to read a collapse off, so this run only tests the abstain guard.",
    ),
    "run-20260602-120529": RunLabel(
        DEGENERATE,
        "All 16 hypotheses carry cluster=null; the proximity step never ran. Abstain-guard "
        "validation only, no collapse evidence in either direction.",
    ),
    "run-20260603-104323": RunLabel(
        HEALTHY,
        "Partially labelled (10 of 26 hypotheses) but monotonically diverging: clusters 1 to 4 "
        "and Herfindahl 0.111 to 0.020 across five rounds, with no signal voting in any round.",
    ),
    "run-20260531-155548": RunLabel(
        HEALTHY,
        "Clusters 4 to 11 and Herfindahl 0.250 to 0.091 over three rounds. Reconstruction "
        "reproduces v2's own recorded telemetry exactly on all three, so the label rests on "
        "logged numbers rather than on inference.",
    ),
    "run-20260531-170330": RunLabel(
        HEALTHY,
        "Clusters 2 to 15 and Herfindahl 0.500 to 0.067 over four rounds. The round-1 "
        "Herfindahl of 0.5 is a reconstruction artefact — v2 recorded n_clusters=0 there, and "
        "final labels are back-dated onto a two-hypothesis pool — and it is the only round in "
        "the whole corpus where the concentration signal votes.",
    ),
    "run-20260601-145835": RunLabel(
        HEALTHY,
        "The largest run: 59 hypotheses over six rounds, clusters 7 to 26, Herfindahl 0.143 to "
        "0.038, no votes. Reconstruction matches the recorded telemetry on all five logged rounds.",
    ),
    "run-20260603-152041": RunLabel(
        DEGENERATE,
        "All 30 hypotheses carry cluster=null, and this is the run whose archived state records "
        "last_fired_iter=4 — the real false fire that motivated the no-clusters abstain. Replay "
        "of the archived decision rule reproduces that round exactly.",
    ),
    "run-20260603-215608": RunLabel(
        DEGENERATE,
        "All 18 hypotheses were rejected and no match was ever played, so the active pool is "
        "empty in every round and no cluster exists. Abstain-guard validation only.",
    ),
    "run-faithtech-20260601": RunLabel(
        HEALTHY,
        "Clusters 5 to 18 and Herfindahl 0.200 to 0.041 across four productive rounds. Iteration "
        "continued to 8 without a single new hypothesis, and it is that frozen tail — not any "
        "convergence — that makes the plateau and birth-rate signals vote from round 7.",
    ),
}


# ------------------------------------------------------------------- reconstruction


@dataclass(frozen=True, slots=True)
class RoundReconstruction:
    """One reconstructed round: the active pool, its telemetry, and v2's own if it logged any."""

    round: int
    active: tuple[HypRow, ...]
    snapshot: CollapseSnapshot
    recorded: CollapseSnapshot | None = None

    @property
    def n_active_matches(self) -> bool:
        return self.recorded is not None and self.snapshot.n_active == self.recorded.n_active

    @property
    def signals_match(self) -> bool:
        """Whether the two fields the signals actually read agree with the recording."""
        return (
            self.recorded is not None
            and self.snapshot.n_clusters == self.recorded.n_clusters
            and self.snapshot.hhi == self.recorded.hhi
        )


@dataclass(frozen=True, slots=True)
class ReplayRun:
    """An archived run, reconstructed round by round and labelled."""

    run_id: str
    version: str
    goal: str
    label: Label
    rounds: tuple[RoundReconstruction, ...]
    n_hypotheses: int
    recorded_fired_round: int | None = None

    @property
    def clustered(self) -> bool:
        """Whether any round saw a cluster label at all."""
        return any(round_data.snapshot.n_clusters > 0 for round_data in self.rounds)

    @property
    def rationale(self) -> str:
        entry = RUN_LABELS.get(self.run_id)
        return entry.rationale if entry else "(not part of the archived corpus)"


def load_state(path: str | Path) -> dict[str, Any]:
    """Read an archived `state.json`. UTF-8 always — the archive is never re-encoded."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hypothesis_rows(
    hypotheses: Mapping[str, Any] | Sequence[Any] | None,
) -> list[Mapping[str, Any]]:
    """v1 and v2 both key hypotheses by id, but tolerate a list for defensiveness."""
    if hypotheses is None:
        return []
    if isinstance(hypotheses, Mapping):
        return list(hypotheses.values())
    return list(hypotheses)


def reconstruct_active(
    hypotheses: Mapping[str, Any] | Sequence[Any] | None, round_number: int
) -> list[HypRow]:
    """The active pool as the collapse check would have seen it in `round_number`.

    Rules 1 and 2 from the module docstring: final status `active`, born on or before this
    round, with evolution offspring (anything carrying a `parent`) held back one round
    because evolution runs after the cluster step.
    """
    rows: list[HypRow] = []
    for hypothesis in _hypothesis_rows(hypotheses):
        if hypothesis.get("status") != "active":
            continue
        born = int(hypothesis.get("created_iter") or 0)
        if hypothesis.get("parent"):
            born += 1
        if born > round_number:
            continue
        rows.append(
            HypRow(
                hid=str(hypothesis.get("id") or ""),
                elo=float(hypothesis.get("elo") or 0.0),
                matches=int(hypothesis.get("matches") or 0),
                wins=int(hypothesis.get("wins") or 0),
                cluster=hypothesis.get("cluster"),
                status="active",
            )
        )
    rows.sort(key=lambda row: row.hid)
    return rows


def recorded_snapshots(state: Mapping[str, Any]) -> dict[int, CollapseSnapshot]:
    """v2's own per-round telemetry, keyed by round. Empty for every v1 run."""
    history = (state.get("collapse") or {}).get("history") or []
    snapshots: dict[int, CollapseSnapshot] = {}
    for record in history:
        round_number = int(record.get("iter") or 0)
        snapshots[round_number] = CollapseSnapshot(
            round=round_number,
            n_active=int(record.get("n_active") or 0),
            n_clusters=int(record.get("n_clusters") or 0),
            hhi=float(record.get("hhi") or 0.0),
            clusters=tuple(str(label) for label in (record.get("clusters") or ())),
        )
    return snapshots


def _snapshot(round_number: int, active: Sequence[HypRow]) -> CollapseSnapshot:
    """Build the round's telemetry through `engine.core`, never with a local copy of the math.

    The snapshot depends only on the round and the pool, so history and config here are
    placeholders; going through `collapse_signals` guarantees the calibration measures
    exactly what the engine will compute at runtime.
    """
    return collapse_signals(round_number, active, (), GraftConfig()).snapshot


def reconstruct_run(
    state: Mapping[str, Any],
    *,
    run_id: str | None = None,
    version: str = "",
    label: Label | None = None,
) -> ReplayRun:
    """Rebuild every round of one archived run.

    The label defaults to the corpus table, then to `degenerate` when no round produced a
    cluster, then to `healthy`. **A run is never auto-labelled `collapsed`** — a positive
    is a human judgement and has to be passed in, which is why the corpus table has none.
    """
    resolved_id = run_id or str(state.get("run_id") or "")
    hypotheses = state.get("hypotheses")
    recorded = recorded_snapshots(state)
    n_rounds = int(state.get("iteration") or 0)

    rounds: list[RoundReconstruction] = []
    for round_number in range(1, n_rounds + 1):
        active = tuple(reconstruct_active(hypotheses, round_number))
        rounds.append(
            RoundReconstruction(
                round=round_number,
                active=active,
                snapshot=_snapshot(round_number, active),
                recorded=recorded.get(round_number),
            )
        )

    resolved_label = label or _label_for(resolved_id, rounds)
    collapse = state.get("collapse") or {}
    fired = collapse.get("last_fired_iter")

    return ReplayRun(
        run_id=resolved_id,
        version=version,
        goal=str(state.get("goal") or ""),
        label=resolved_label,
        rounds=tuple(rounds),
        n_hypotheses=len(_hypothesis_rows(hypotheses)),
        recorded_fired_round=None if fired is None else int(fired),
    )


def _label_for(run_id: str, rounds: Sequence[RoundReconstruction]) -> Label:
    entry = RUN_LABELS.get(run_id)
    if entry is not None:
        return entry.label
    if not any(round_data.snapshot.n_clusters > 0 for round_data in rounds):
        return DEGENERATE
    return HEALTHY


def load_archive(root: str | Path) -> list[ReplayRun]:
    """Load every historical run under `archive/imported-runs/{v1,v2}`, read-only.

    The `gui/` root is deliberately skipped: those three runs are GUI-created content-only
    runs with a single hypothesis and no tournament, so they carry no cluster history.
    """
    base = Path(root)
    runs: list[ReplayRun] = []
    for version in ARCHIVE_VERSIONS:
        for state_path in sorted((base / version).glob("*/state.json")):
            runs.append(
                reconstruct_run(
                    load_state(state_path),
                    run_id=state_path.parent.name,
                    version=version,
                )
            )
    return runs


@dataclass(frozen=True, slots=True)
class FidelityRow:
    """How closely one run's reconstruction reproduces the telemetry v2 logged at the time."""

    run_id: str
    rounds_compared: int
    n_active_exact: int
    signals_exact: int

    @property
    def signal_fidelity(self) -> float:
        return self.signals_exact / self.rounds_compared if self.rounds_compared else 0.0


def reconstruction_fidelity(runs: Iterable[ReplayRun]) -> list[FidelityRow]:
    """Score reconstruction against recorded telemetry, skipping runs that logged none."""
    rows: list[FidelityRow] = []
    for run in runs:
        comparable = [r for r in run.rounds if r.recorded is not None]
        if not comparable:
            continue
        rows.append(
            FidelityRow(
                run_id=run.run_id,
                rounds_compared=len(comparable),
                n_active_exact=sum(r.n_active_matches for r in comparable),
                signals_exact=sum(r.signals_match for r in comparable),
            )
        )
    return rows


# --------------------------------------------------------------------------- replay


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Every round's decision for one run under one parameter set."""

    run_id: str
    label: Label
    decisions: tuple[GraftDecision, ...]

    @property
    def fired_rounds(self) -> tuple[int, ...]:
        return tuple(d.round for d in self.decisions if d.fire)

    @property
    def first_fire_round(self) -> int | None:
        fired = self.fired_rounds
        return fired[0] if fired else None

    @property
    def abstained_reasons(self) -> Counter[str]:
        return Counter(d.abstained_reason for d in self.decisions if d.abstained_reason)


def replay_run(run: ReplayRun, graft: GraftConfig) -> ReplayResult:
    """Run one reconstructed run through the engine's own collapse decision, round by round.

    History accumulates exactly as the orchestrator accumulates it: `collapse_signals` is
    handed the *earlier* rounds and computes the current snapshot itself, and a fire moves
    the cooldown anchor.
    """
    history: list[CollapseSnapshot] = []
    decisions: list[GraftDecision] = []
    last_fired: int | None = None

    for round_data in run.rounds:
        signals = collapse_signals(round_data.round, round_data.active, history, graft)
        decision = should_fire_graft(signals, graft, round_data.round, last_fired)
        if decision.fire:
            last_fired = round_data.round
        decisions.append(decision)
        history.append(signals.snapshot)

    return ReplayResult(run_id=run.run_id, label=run.label, decisions=tuple(decisions))


def legacy_v2_fires(run: ReplayRun, graft: GraftConfig) -> list[int]:
    """The rounds the *archived* v2 engine would have fired on — its rule, not the new one.

    v2's `cmd_collapse_check` had no no-clusters abstain: it fired on votes and cooldown
    alone. This reproduces that so the guard's effect can be measured, and so the one real
    historical fire (`run-20260603-152041`, `last_fired_iter: 4`) can be checked against the
    reconstruction. It is a model of deleted behaviour, deliberately kept out of
    `engine.core`, and nothing at runtime calls it.
    """
    if not graft.enabled:
        return []

    history: list[CollapseSnapshot] = []
    fires: list[int] = []
    last_fired: int | None = None

    for round_data in run.rounds:
        signals = collapse_signals(round_data.round, round_data.active, history, graft)
        cooled = last_fired is None or (round_data.round - last_fired) > graft.cooldown
        if signals.votes >= graft.quorum_k and cooled:
            fires.append(round_data.round)
            last_fired = round_data.round
        history.append(signals.snapshot)

    return fires


# ---------------------------------------------------------------------- grid search


@dataclass(frozen=True, slots=True)
class GridCell:
    """One parameter set, scored over the whole corpus.

    Three numbers decide a cell, in the order they matter: it must not fire on a healthy
    run, it must abstain on every degenerate round, and it should fire — early — on a
    collapsed run. The corpus supplies plenty of the first two and none of the third.
    """

    quorum_k: int
    window: int
    cooldown: int
    collapsed_total: int
    collapsed_fired: int
    healthy_total: int
    healthy_fired: int
    healthy_rounds: int
    healthy_fire_rounds: int
    degenerate_rounds: int
    degenerate_abstained: int
    fires: tuple[tuple[str, int], ...]
    first_fires: tuple[tuple[str, int], ...]

    @property
    def recall(self) -> float | None:
        """Share of collapsed runs that fired at least once. `None` when there are none."""
        if not self.collapsed_total:
            return None
        return self.collapsed_fired / self.collapsed_total

    @property
    def false_fire_rate(self) -> float:
        """Share of healthy runs that fired at least once. The number to drive to zero."""
        if not self.healthy_total:
            return 0.0
        return self.healthy_fired / self.healthy_total

    @property
    def abstain_rate(self) -> float:
        """Share of degenerate rounds that abstained. Anything below 1.0 is a bug."""
        if not self.degenerate_rounds:
            return 1.0
        return self.degenerate_abstained / self.degenerate_rounds

    @property
    def clean(self) -> bool:
        return self.false_fire_rate == 0.0 and self.abstain_rate == 1.0


def grid_search(
    runs: Sequence[ReplayRun],
    *,
    quorums: Sequence[int] = DEFAULT_QUORUMS,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    cooldowns: Sequence[int] = DEFAULT_COOLDOWNS,
) -> list[GridCell]:
    """Score every (quorum_k, window, cooldown) combination over the corpus."""
    cells: list[GridCell] = []
    for quorum in quorums:
        for window in windows:
            for cooldown in cooldowns:
                config = GraftConfig(
                    enabled=True, quorum_k=quorum, window=window, cooldown=cooldown
                )
                cells.append(_score(runs, config))
    return cells


def _score(runs: Sequence[ReplayRun], config: GraftConfig) -> GridCell:
    collapsed_total = collapsed_fired = 0
    healthy_total = healthy_fired = healthy_rounds = healthy_fire_rounds = 0
    degenerate_rounds = degenerate_abstained = 0
    fires: list[tuple[str, int]] = []
    first_fires: list[tuple[str, int]] = []

    for run in runs:
        result = replay_run(run, config)
        fired = result.fired_rounds
        fires.extend((run.run_id, round_number) for round_number in fired)
        if fired:
            first_fires.append((run.run_id, fired[0]))

        if run.label == COLLAPSED:
            collapsed_total += 1
            collapsed_fired += bool(fired)
        elif run.label == HEALTHY:
            healthy_total += 1
            healthy_fired += bool(fired)
            healthy_rounds += len(result.decisions)
            healthy_fire_rounds += len(fired)
        else:
            degenerate_rounds += len(result.decisions)
            degenerate_abstained += sum(1 for d in result.decisions if not d.fire)

    return GridCell(
        quorum_k=config.quorum_k,
        window=config.window,
        cooldown=config.cooldown,
        collapsed_total=collapsed_total,
        collapsed_fired=collapsed_fired,
        healthy_total=healthy_total,
        healthy_fired=healthy_fired,
        healthy_rounds=healthy_rounds,
        healthy_fire_rounds=healthy_fire_rounds,
        degenerate_rounds=degenerate_rounds,
        degenerate_abstained=degenerate_abstained,
        fires=tuple(fires),
        first_fires=tuple(first_fires),
    )

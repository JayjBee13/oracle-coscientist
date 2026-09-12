"""Replay the archived co-scientist runs through the collapse detector and print the grid.

Zero LLM cost and read-only: it reads `archive/imported-runs/{v1,v2}/*/state.json` and runs
them through `app.engine.core`'s `collapse_signals` / `should_fire_graft` across the
parameter grid the plan calls for (quorum_k 1-3 x window 2-4 x cooldown 1-3).

    python scripts/calibrate_graft.py                # full report
    python scripts/calibrate_graft.py --markdown     # the same tables as markdown
    python scripts/calibrate_graft.py --archive PATH # a different corpus

The narrative version, with the labelling rationale and the caveats, lives in
`docs/architecture.md`; this script is what regenerates its numbers.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
DEFAULT_ARCHIVE = REPO_ROOT / "archive" / "imported-runs"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.engine.calibration import (  # noqa: E402
    COLLAPSED,
    DEGENERATE,
    HEALTHY,
    RECOMMENDED,
    RUN_LABELS,
    GridCell,
    ReplayRun,
    grid_search,
    legacy_v2_fires,
    load_archive,
    reconstruction_fidelity,
    replay_run,
)
from app.engine.core import GraftConfig  # noqa: E402

SHIPPED_V2 = GraftConfig(enabled=True, quorum_k=1, window=3, cooldown=2)
"""What v2 actually ran with: uncalibrated quorum 1, and no no-clusters abstain."""


def _heading(title: str, markdown: bool, level: int = 2) -> None:
    if markdown:
        print(f"\n{'#' * level} {title}\n")
    else:
        print(f"\n{title}")
        print("=" * len(title))


def _table(headers: list[str], rows: list[list[str]], markdown: bool) -> None:
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
              for i in range(len(headers))]
    if markdown:
        print("| " + " | ".join(headers) + " |")
        print("|" + "|".join("---" for _ in headers) + "|")
        for row in rows:
            print("| " + " | ".join(row) + " |")
        return
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def corpus_table(runs: list[ReplayRun], markdown: bool) -> None:
    _heading("Corpus and labels", markdown)
    rows = []
    for run in sorted(runs, key=lambda r: (r.label, r.run_id)):
        last = run.rounds[-1].snapshot if run.rounds else None
        rows.append(
            [
                run.run_id,
                run.version,
                run.label,
                str(len(run.rounds)),
                str(run.n_hypotheses),
                f"{last.n_clusters}" if last else "-",
                f"{last.hhi:.3f}" if last else "-",
                "yes" if run.rounds and run.rounds[0].recorded is not None else "no",
            ]
        )
    _table(
        ["run", "ver", "label", "rounds", "hyps", "clusters@end", "hhi@end", "logged"],
        rows,
        markdown,
    )
    counts = {
        label: sum(1 for r in runs if r.label == label)
        for label in (HEALTHY, COLLAPSED, DEGENERATE)
    }
    print(
        f"\n{counts[HEALTHY]} healthy, {counts[COLLAPSED]} collapsed, "
        f"{counts[DEGENERATE]} degenerate."
    )


def fidelity_table(runs: list[ReplayRun], markdown: bool) -> None:
    _heading("Reconstruction fidelity against v2's recorded telemetry", markdown)
    rows = reconstruction_fidelity(runs)
    if not rows:
        print("No run in this corpus logged its own collapse history.")
        return
    _table(
        ["run", "rounds compared", "n_active exact", "n_clusters+hhi exact"],
        [
            [r.run_id, str(r.rounds_compared), str(r.n_active_exact), str(r.signals_exact)]
            for r in rows
        ],
        markdown,
    )
    compared = sum(r.rounds_compared for r in rows)
    active = sum(r.n_active_exact for r in rows)
    signals = sum(r.signals_exact for r in rows)
    print(
        f"\nTotals: {active}/{compared} rounds reproduce n_active exactly; "
        f"{signals}/{compared} reproduce the two fields the signals read "
        f"({signals / compared:.0%})."
    )


def telemetry(runs: list[ReplayRun], markdown: bool) -> None:
    _heading("Reconstructed per-round telemetry", markdown)
    config = replace(RECOMMENDED, enabled=True)
    for run in runs:
        print(f"\n{run.version}/{run.run_id}  [{run.label}]  {run.goal[:70]}")
        result = replay_run(run, config)
        rows = []
        for round_data, decision in zip(run.rounds, result.decisions, strict=True):
            snapshot = decision.signals.snapshot
            signals = decision.signals
            recorded = round_data.recorded
            rows.append(
                [
                    str(snapshot.round),
                    str(snapshot.n_active),
                    str(snapshot.n_clusters),
                    f"{snapshot.hhi:.3f}",
                    f"{int(signals.plateau)}{int(signals.concentration)}{int(signals.birth_rate)}",
                    str(decision.votes),
                    "FIRE" if decision.fire else (decision.abstained_reason or "-"),
                    (
                        "match"
                        if round_data.signals_match
                        else (
                            f"logged {recorded.n_clusters}/{recorded.hhi:.3f}"
                            if recorded
                            else "-"
                        )
                    ),
                ]
            )
        _table(
            ["round", "active", "clusters", "hhi", "S1S2S3", "votes", "decision", "vs logged"],
            rows,
            markdown,
        )


def grid_table(runs: list[ReplayRun], markdown: bool) -> list[GridCell]:
    _heading("Parameter grid", markdown)
    cells = grid_search(runs)
    rows = []
    for cell in cells:
        recall = "n/a" if cell.recall is None else f"{cell.recall:.0%}"
        detail = ", ".join(f"{run_id.removeprefix('run-')}@r{r}" for run_id, r in cell.fires) or "-"
        rows.append(
            [
                str(cell.quorum_k),
                str(cell.window),
                str(cell.cooldown),
                recall,
                f"{cell.healthy_fired}/{cell.healthy_total}",
                str(cell.healthy_fire_rounds),
                f"{cell.abstain_rate:.0%}",
                "yes" if cell.clean else "no",
                detail,
            ]
        )
    _table(
        ["k", "win", "cool", "recall", "false-fire runs", "false rounds",
         "abstain", "clean", "fires"],
        rows,
        markdown,
    )
    return cells


def counterfactual(runs: list[ReplayRun], markdown: bool) -> None:
    _heading("What v2's shipped configuration would have done", markdown)
    print(
        "quorum_k=1, window=3, cooldown=2, and no no-clusters abstain - the settings the\n"
        "archived engine ran with. `legacy` is that rule; `guarded` is the same parameters\n"
        "through the engine's current `should_fire_graft`.\n"
    )
    rows = []
    for run in runs:
        legacy = legacy_v2_fires(run, SHIPPED_V2)
        guarded = replay_run(run, SHIPPED_V2).fired_rounds
        if not legacy and not guarded:
            continue
        rows.append(
            [
                run.run_id,
                run.label,
                ", ".join(f"r{r}" for r in legacy) or "-",
                ", ".join(f"r{r}" for r in guarded) or "-",
                f"r{run.recorded_fired_round}" if run.recorded_fired_round else "-",
            ]
        )
    _table(["run", "label", "legacy fires", "guarded fires", "logged fire"], rows, markdown)


def recommendation(runs: list[ReplayRun], cells: list[GridCell], markdown: bool) -> None:
    _heading("Recommendation", markdown)
    clean = [c for c in cells if c.clean]
    quorums = sorted({c.quorum_k for c in clean})
    print(
        f"{len(clean)} of {len(cells)} cells fire on no healthy run and abstain on every\n"
        f"degenerate round. They are exactly the cells with quorum_k in {quorums}.\n"
    )
    switched_on = replace(RECOMMENDED, enabled=True)
    fired = [r.run_id for r in runs if replay_run(r, switched_on).fired_rounds]
    print(
        f"Recommended: enabled={RECOMMENDED.enabled}, quorum_k={RECOMMENDED.quorum_k}, "
        f"window={RECOMMENDED.window}, cooldown={RECOMMENDED.cooldown}\n"
        f"  runs that fire at these parameters (switched on): {fired or 'none'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--markdown", action="store_true", help="emit tables as markdown")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not args.archive.is_dir():
        print(f"archive not found: {args.archive}", file=sys.stderr)
        return 2

    runs = load_archive(args.archive)
    if not runs:
        print(f"no runs found under {args.archive}", file=sys.stderr)
        return 2

    unlabelled = [r.run_id for r in runs if r.run_id not in RUN_LABELS]
    if unlabelled:
        print(f"warning: no written label for {unlabelled}", file=sys.stderr)

    print(f"Graft calibration replay over {len(runs)} archived runs")
    print(f"archive: {args.archive}")

    corpus_table(runs, args.markdown)
    fidelity_table(runs, args.markdown)
    telemetry(runs, args.markdown)
    cells = grid_table(runs, args.markdown)
    counterfactual(runs, args.markdown)
    recommendation(runs, cells, args.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

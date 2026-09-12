/**
 * Comparing two runs without inventing a result.
 *
 * The rule everything here obeys: **Elo is only meaningful inside the
 * tournament that produced it.** A 1300 from a run of six hypotheses and a 1300
 * from a run of fifty are not the same number, so the previous UI's merged
 * cross-run leaderboard was not a ranking of anything. There is no merged
 * ranking in this module and none on the page — each run's list is normalised
 * against its own best and worst, and the two sit side by side for a reader to
 * compare, which is a judgement a person can make and arithmetic cannot.
 *
 * What *does* survive the crossing is a directional delta (more active
 * hypotheses, fewer calls) and the Elo **spread** — how far a run separated its
 * best idea from its median one is a property of that run's own tournament.
 */

import type {
  CompareAnalytics,
  CompareDelta,
  CompareGraft,
  DirectionHint,
  HypothesisRow,
} from "../api/types";
import type { Tone } from "./status";

/* --- Deltas ---------------------------------------------------------------
   `direction_hint` used to be read defensively, because this file and the API
   disagreed about how to spell it. They cannot disagree any more: the types
   below are generated from the API's own schema, so the vocabulary here IS the
   server's. What is left is the reading of it.
   ------------------------------------------------------------------------- */

export type DeltaVerdict = "better" | "worse" | "changed" | "same";

export type DeltaView = {
  metric: string;
  base: number;
  challenger: number;
  /** Challenger minus baseline. */
  change: number;
  direction: DirectionHint;
  verdict: DeltaVerdict;
  /** A word, because an arrow and a colour are not enough on their own. */
  label: string;
  arrow: "▲" | "▼" | "–";
  tone: Tone;
};

export function readDelta(delta: CompareDelta): DeltaView {
  const change = delta.challenger - delta.base;
  const direction = delta.direction_hint;

  let verdict: DeltaVerdict = "same";
  if (change !== 0) {
    if (direction === "flat") verdict = "changed";
    else if (direction === "up") verdict = change > 0 ? "better" : "worse";
    else verdict = change < 0 ? "better" : "worse";
  }

  return {
    metric: delta.metric,
    base: delta.base,
    challenger: delta.challenger,
    change,
    direction,
    verdict,
    label: VERDICT_LABEL[verdict],
    arrow: change === 0 ? "–" : change > 0 ? "▲" : "▼",
    tone: VERDICT_TONE[verdict],
  };
}

const VERDICT_LABEL: Record<DeltaVerdict, string> = {
  better: "better",
  worse: "worse",
  changed: "different",
  same: "no change",
};

const VERDICT_TONE: Record<DeltaVerdict, Tone> = {
  better: "go",
  worse: "danger",
  changed: "info",
  same: "neutral",
};

export function readDeltas(deltas: readonly CompareDelta[]): DeltaView[] {
  return deltas.map(readDelta);
}

/** `+3`, `−1.4`, `0`. A signed number is a sentence's worth of meaning here. */
export function formatChange(change: number): string {
  if (change === 0) return "0";
  const rounded = Math.round(change * 10) / 10;
  return `${rounded > 0 ? "+" : "−"}${Math.abs(rounded)}`;
}

export function formatMetricValue(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 10) / 10);
}

/* --- The headline --------------------------------------------------------- */

export type Verdict = {
  headline: string;
  detail: string;
  tone: Tone;
};

/**
 * One sentence for someone who opened this page to find out whether the change
 * they made helped. It counts only the metrics that carry a direction — rounds
 * and matches judged describe a run, they do not grade it — and it says "no
 * clear winner" rather than manufacturing one.
 */
export function headlineVerdict(
  views: readonly DeltaView[],
  options: { sharedPrompt: boolean },
): Verdict {
  const scored = views.filter(
    (view) => view.verdict === "better" || view.verdict === "worse",
  );
  const better = scored.filter((view) => view.verdict === "better");
  const worse = scored.filter((view) => view.verdict === "worse");

  const caveat = options.sharedPrompt
    ? "Both runs started from the same prompt, so the difference is down to the settings."
    : "These runs did not start from the same prompt, so any difference may be the question rather than the settings.";

  if (scored.length === 0) {
    return {
      headline: "Nothing separates these two runs",
      detail: `No measure with a better-or-worse direction moved. ${caveat}`,
      tone: "neutral",
    };
  }
  if (better.length > worse.length) {
    return {
      headline: `The challenger came out ahead on ${better.length} of ${scored.length} measures`,
      detail: `Ahead on ${list(better)}. ${worse.length > 0 ? `Behind on ${list(worse)}. ` : ""}${caveat}`,
      tone: "go",
    };
  }
  if (worse.length > better.length) {
    return {
      headline: `The baseline came out ahead on ${worse.length} of ${scored.length} measures`,
      detail: `The challenger is behind on ${list(worse)}. ${better.length > 0 ? `Ahead on ${list(better)}. ` : ""}${caveat}`,
      tone: "caution",
    };
  }
  return {
    headline: "No clear winner",
    detail: `The challenger is ahead on ${list(better)} and behind on ${list(worse)}. ${caveat}`,
    tone: "info",
  };
}

function list(views: readonly DeltaView[]): string {
  const names = views.map((view) => view.metric.toLowerCase());
  if (names.length <= 1) return names[0] ?? "nothing";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/* --- Ranked lists, normalised inside one run ------------------------------ */

export type RankedRow = {
  hid: string;
  title: string;
  elo: number | null;
  status: string;
  matches: number;
  wins: number;
  rank: number;
  /**
   * Bar length, 0–1, of this hypothesis between its own run's weakest and
   * strongest. It is meaningless across runs, which is exactly why it never
   * leaves the list it was computed for.
   */
  share: number;
};

export type RankedList = {
  rows: RankedRow[];
  /** Which population was ranked: the run's active ideas, or everything it has. */
  basis: "active" | "all" | "none";
  best: number | null;
  worst: number | null;
  total: number;
};

/** The shortest bar still has to be visible, or the last row reads as absent. */
const MIN_SHARE = 0.08;

/**
 * Ranks a run's hypotheses against themselves.
 *
 * An all-rejected run — there is one in the archive, 18 of 18 — has no active
 * hypotheses and no tournament. It falls back to every hypothesis it has and
 * reports `basis: "all"`, so the page can say what it is showing instead of
 * rendering an empty column that looks like a failure.
 */
export function rankWithinRun(rows: readonly HypothesisRow[], limit = 10): RankedList {
  const active = rows.filter((row) => row.status === "active");
  const basis: RankedList["basis"] =
    active.length > 0 ? "active" : rows.length > 0 ? "all" : "none";
  const population = basis === "active" ? active : rows;

  const ranked = [...population].sort((a, b) => (b.elo ?? 0) - (a.elo ?? 0));
  const scores = ranked
    .map((row) => row.elo)
    .filter((elo): elo is number => typeof elo === "number");
  const best = scores.length > 0 ? Math.max(...scores) : null;
  const worst = scores.length > 0 ? Math.min(...scores) : null;
  const span = best !== null && worst !== null ? best - worst : 0;

  return {
    basis,
    best,
    worst,
    total: population.length,
    rows: ranked.slice(0, limit).map((row, index) => ({
      hid: row.hid,
      title: row.title,
      elo: row.elo,
      status: row.status,
      matches: row.matches,
      wins: row.wins,
      rank: index + 1,
      share:
        row.elo === null || best === null || worst === null
          ? MIN_SHARE
          : span === 0
            ? 1
            : MIN_SHARE + (1 - MIN_SHARE) * ((row.elo - worst) / span),
    })),
  };
}

/* --- Graft summaries ------------------------------------------------------
   Null means "this run's engine had no such concept" — a v1 run predates the
   diversity injection entirely. Rendering zeros there would claim it never
   fired, which is a different and false statement.
   ------------------------------------------------------------------------- */

export type GraftView = {
  enabled: boolean;
  firedCount: number;
  collapseEvents: number;
};

export function readGraft(summary: CompareGraft | null | undefined): GraftView | null {
  if (!summary) return null;
  return {
    enabled: summary.enabled,
    firedCount: summary.fired_count,
    collapseEvents: summary.collapse_events,
  };
}

export function hasGraftData(analytics: CompareAnalytics): boolean {
  return (
    readGraft(analytics.graft_summary.baseline) !== null ||
    readGraft(analytics.graft_summary.challenger) !== null
  );
}

/* --- Movement -------------------------------------------------------------
   Two independent runs share no hypothesis ids — `h003` is a per-run counter —
   so movement is matched on title and labelled as such wherever it is shown.
   ------------------------------------------------------------------------- */

export type MovementRow = {
  hid: string;
  title: string;
  rank: number;
  previousRank: number;
  delta: number | null;
};

/** Only a hypothesis the baseline also had can have moved; the rest are new. */
export function readMovement(analytics: CompareAnalytics): MovementRow[] {
  return analytics.movement
    .filter((row) => row.previous_rank !== null)
    .map((row) => ({
      hid: row.hid,
      title: row.title,
      rank: row.rank,
      previousRank: row.previous_rank as number,
      delta: row.delta,
    }));
}

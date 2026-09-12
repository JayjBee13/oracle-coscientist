/**
 * What a run will cost, before a penny of it is spent.
 *
 * Every number the wizard shows — preset budgets, the Configure strip, the
 * Confirm step — comes from here, and every one of them mirrors
 * `backend/app/engine/core.py`. That parity is the point: the old launch form
 * showed five raw numbers and no estimate at all, so nobody could tell a
 * fifteen-call run from a hundred-and-fifty-call one until the bill arrived.
 *
 * Two of these numbers are measurements of the engine and two are guesses:
 *
 *   - `estimateCalls` is **exact**. It counts C3's step list, the same way the
 *     orchestrator will execute it, and the budget ceiling a preset picks is
 *     that count plus `BUDGET_HEADROOM`.
 *   - `estimateMinutes` and `estimateUsd` are **planning heuristics**, labelled
 *     as estimates everywhere they appear on screen. Nothing at runtime reads
 *     them; they exist so a scientist knows roughly what they are committing to.
 *
 * If the backend's formula changes, change it here in the same commit — the
 * preset fixtures in `estimates.test.ts` are the tripwire.
 */

import type { GroundingDepth, ModelTier, RunConfig } from "../api/types";

/**
 * The tier a run gets when nothing chooses one.
 *
 * Mirrors `RunConfigIn.model_tier`'s default, because the wizard sends a tier on
 * every launch and must not quietly hand someone a cheaper one than a bare API
 * launch would get. `/api/capabilities` reports the same value as
 * `models.default_tier` and is authoritative at runtime; this constant exists
 * for `defaultConfig()`, which builds a draft before any request has returned.
 * `estimates.test.ts` pins it against the backend.
 */
export const DEFAULT_MODEL_TIER: ModelTier = "max";

/* --- Engine constants (mirror of engine/core.py) -------------------------- */

/** Hypotheses per generation shard; the batch splits into `ceil(batch/3)` calls. */
export const GENERATION_SHARD_SIZE = 3;

/** Concurrent role calls the orchestrator allows (C3's Semaphore(3)). */
export const PARALLEL_CALLS = 3;

/** Reserved at run start so a stopped or exhausted run can still write a report. */
export const OVERVIEW_RESERVED_CALLS = 2;

/**
 * The most rounds a run may ever hold, mirroring `launcher.MAX_ROUNDS`.
 *
 * Not the same number as the wizard's rounds field, which stops at 20 because
 * nobody should reach for a twenty-five-round run by dragging past it. This is
 * the wall: `continue` refuses an increment that would carry a run past it, and
 * the Continue dialog has to say so before the request rather than after.
 */
export const MAX_ROUNDS = 25;

/** Preset budgets are the estimate plus this much slack (retries, repairs). */
export const BUDGET_HEADROOM = 1.25;

/** Seconds a grounded (web-searching) call takes: [optimistic, pessimistic]. */
export const GROUNDED_CALL_SECONDS: readonly [number, number] = [45, 150];

/** Seconds a tool-less call takes: [optimistic, pessimistic]. */
export const TOOLLESS_CALL_SECONDS: readonly [number, number] = [20, 75];

/** Deeper grounding means more searching, so grounded calls stretch. */
export const GROUNDING_TIME_FACTOR: Record<GroundingDepth, number> = {
  shallow: 0.7,
  standard: 1.0,
  deep: 1.4,
};

/* --- The cost estimate ----------------------------------------------------
   THE one place a dollar figure per call is written down. It is an estimate,
   not a price: a call's cost swings with prompt size, cache reuse and how much
   the model thinks. The bracket is wide on purpose and every figure derived
   from it is rendered as a range with the word "estimated" beside it.

   The per-model brackets come from `backend/app/engine/models.py`, which is the
   published source for model ids, list prices and per-call brackets — opus-5 and
   Luna at $0.25–0.667 a call, fable and Sol at $0.50–1.333. The tier figures
   below are those brackets weighted by how the roles actually divide up.

   **`max`, `high` and `med` move thinking effort only.** Which model a step runs
   on is fixed by how hard the step is, so those three brackets do not separate a
   cheap table from an expensive one. They separate a run that thinks hard from a
   run that thinks less, and the spread narrows accordingly: the five
   judgment-heavy roles are ~90% of the calls a run makes (52 reflections and 30
   rankings in a standard-preset run against 10 calls of clustering and
   evolution), and at `max` they are all running at the top of their ladder. So
   `max` is the undiscounted mix — 0.9·0.50 + 0.1·0.25 = 0.475 and 0.9·1.333 +
   0.1·0.667 = 1.266 — and `high` and `med` are that mix at 0.75 and ~0.525 of it.

   **`low` is outside that ladder, and it is a speed tier rather than a cheap
   one.** It pins `gpt-5.6-luna` on *every* role, at high effort, so there is no
   two-model mix to weight: its bracket is Luna's own published bracket, which is
   `usd_per_call_bracket(5.00)` = (0.15·5/3, 0.40·5/3) = $0.25–0.667 — the same
   figure as opus-5, because Luna has no list price and is bracketed at
   `class_parity` with the light Anthropic model. No effort discount applies to
   it either: high effort is the rung `max` already gives the light model, and
   `max`'s figures carry no discount. Clustering is still pinned to low effort in
   `low`, which is roughly one call in ten and is not modelled here, exactly as
   no other per-role effort is.

   Which lands `low` on the same per-call figure as `med`, to the cent. That is
   the honest answer and not a coincidence worth hiding: a Low run buys wall
   clock, not money, and anything on screen that implied otherwise would be
   selling it wrongly.
   ------------------------------------------------------------------------- */
export const USD_PER_CALL_ESTIMATE: Record<ModelTier, readonly [number, number]> = {
  max: [0.48, 1.27],
  high: [0.36, 0.95],
  med: [0.25, 0.67],
  low: [0.25, 0.67],
};

/** A demo run makes no model calls, so it cannot cost anything. */
export const DEMO_USD_PER_CALL: readonly [number, number] = [0, 0];

/* --- Which model runs which step ------------------------------------------
   Not here, on purpose. There used to be a hand-written mirror of
   `engine/runners.py` in this file so the Confirm step could print a table
   before a run existed; it went stale the week the engine moved four roles to
   high effort, and the Confirm step then contradicted the diagram directly
   above it. `GET /api/capabilities` publishes the same table, and `lib/models`
   folds it. Nothing in the frontend types a model id.
   ------------------------------------------------------------------------- */

/* --- The shape the estimates read ----------------------------------------- */

/** Only these five fields move any estimate; the rest of `RunConfig` is inert. */
export type EstimateInput = Pick<
  RunConfig,
  "rounds" | "generation_batch" | "matches_per_round" | "evolve_top_k"
> & { grounding_depth?: GroundingDepth; workflow?: "adaptive" | "tournament" };

/* --- Calls ---------------------------------------------------------------- */

/**
 * Model calls a run of this shape will make, following C3's step list exactly.
 *
 *     estimate = 2 + R·(ceil(B/3) + B + M + 3) + (R−1)·V
 *
 * with `R` rounds, `B` generation_batch, `M` matches_per_round, `V`
 * evolve_top_k. Per round: generation shards, one reflection per new
 * hypothesis, proximity, ranking, evolution, meta-review. Evolution's output is
 * reviewed in the *next* round, which is the `(R−1)·V` term — the final round's
 * evolved hypotheses are never reviewed. The leading 2 is the reserved overview.
 *
 * The cartographer is deliberately excluded: it costs one call and only when
 * the diversity injection fires, which on a healthy pool is never. Rejections
 * are not modelled either, so this is an upper bound on the tournament side.
 */
export function estimateCalls(config: EstimateInput): number {
  if (config.workflow === "adaptive") {
    const creative = Math.max(
      4 + Math.max(4, config.generation_batch),
      1 + Math.max(1, config.evolve_top_k),
    );
    return (
      2 +
      Math.max(0, config.rounds) *
        (1 + creative + Math.max(3, Math.min(config.matches_per_round, 6)) + 2)
    );
  }
  const rounds = Math.max(0, config.rounds);
  if (rounds === 0) return OVERVIEW_RESERVED_CALLS;

  const batch = Math.max(0, config.generation_batch);
  const matches = Math.max(0, config.matches_per_round);
  const evolved = Math.max(0, config.evolve_top_k);
  const shards = Math.ceil(batch / GENERATION_SHARD_SIZE);

  // + proximity, evolution, meta-review
  const perRound = shards + batch + matches + 3;
  return OVERVIEW_RESERVED_CALLS + rounds * perRound + (rounds - 1) * evolved;
}

/** The estimate plus headroom — exactly what a preset sets `budget_calls` to. */
export function suggestedBudgetCalls(config: EstimateInput): number {
  return Math.ceil(estimateCalls(config) * BUDGET_HEADROOM);
}

/* --- Wall clock ----------------------------------------------------------- */

/** Waves of `PARALLEL_CALLS` concurrent calls needed to run `calls` of them. */
function waves(calls: number): number {
  return calls > 0 ? Math.ceil(calls / PARALLEL_CALLS) : 0;
}

/**
 * Wall-clock bracket `[low, high]` in minutes.
 *
 * Calls are not serial: generation shards, reflections and rankings run three
 * at a time, so the cost is the number of *waves*. Grounded roles dominate and
 * grounding depth stretches them.
 */
export function estimateMinutes(config: EstimateInput): [number, number] {
  if (config.workflow === "adaptive") {
    const calls = estimateCalls(config);
    const depth = GROUNDING_TIME_FACTOR[config.grounding_depth ?? "standard"];
    return [
      Math.max(1, Math.floor((calls * depth) / 3)),
      Math.max(2, Math.ceil(calls * depth * 2)),
    ];
  }
  const rounds = Math.max(0, config.rounds);
  const batch = Math.max(0, config.generation_batch);
  const matches = Math.max(0, config.matches_per_round);
  const evolved = Math.max(0, config.evolve_top_k);
  const shards = Math.ceil(batch / GENERATION_SHARD_SIZE);

  let groundedWaves = 0;
  let toollessWaves = 1; // the overview at the end of the run
  for (let index = 0; index < rounds; index += 1) {
    const reflected = batch + (index > 0 ? evolved : 0);
    groundedWaves += waves(shards) + waves(reflected) + 1; // + evolution
    toollessWaves += 1 + waves(matches) + 1; // proximity + ranking + meta-review
  }

  const factor = GROUNDING_TIME_FACTOR[config.grounding_depth ?? "standard"] ?? 1;
  const seconds = (bound: 0 | 1): number =>
    groundedWaves * GROUNDED_CALL_SECONDS[bound] * factor +
    toollessWaves * TOOLLESS_CALL_SECONDS[bound];

  const low = Math.max(1, Math.floor(seconds(0) / 60));
  return [low, Math.max(low + 1, Math.ceil(seconds(1) / 60))];
}

/* --- Money ---------------------------------------------------------------- */

/**
 * Estimated API-equivalent bracket in USD for a run of this shape and tier.
 *
 * Never a limit and never a gauge: these calls run on a subscription and are not
 * billed per token, so the figure exists only to say roughly how much work a run
 * is. Every caller renders it as a labelled footnote.
 *
 * The tier is defended rather than trusted. A run cloned from before a tier was
 * retired carries a `model_tier` that is not a key of the table, and destructuring
 * `undefined` here used to take the whole wizard down with it.
 */
export function estimateUsd(
  config: EstimateInput,
  options: { tier?: string | null; demo?: boolean } = {},
): [number, number] {
  if (options.demo) return [0, 0];
  const calls = estimateCalls(config);
  const bracket =
    (options.tier != null
      ? USD_PER_CALL_ESTIMATE[options.tier as ModelTier]
      : undefined) ?? USD_PER_CALL_ESTIMATE[DEFAULT_MODEL_TIER];
  const [low, high] = bracket;
  return [calls * low, calls * high];
}

/* --- Presets --------------------------------------------------------------
   Three shapes of run, from "show me this works" to "spend the afternoon on
   it". Their budgets are *computed* from the formula above rather than typed
   in — the previous UI's Quick preset offered 8 calls for a round that needs
   12, so it could not finish by construction.

   No preset sets a cost ceiling. Calls are the governor: they are what the
   engine counts and what it stops on. A dollar ceiling is a thing the backend
   accepts and nothing in this app suggests, because these calls run on a
   subscription and are not billed per token.
   ------------------------------------------------------------------------- */

export type PresetName = "quick" | "standard" | "deep";

export type PresetShape = EstimateInput & {
  grounding_depth: GroundingDepth;
};

export type Preset = {
  name: PresetName;
  label: string;
  /** One line a scientist can choose between without opening Advanced. */
  summary: string;
  shape: PresetShape;
};

export const PRESETS: readonly Preset[] = [
  {
    name: "quick",
    label: "Quick look",
    summary: "One round. Enough to see the shape of an answer.",
    shape: {
      workflow: "adaptive",
      rounds: 1,
      generation_batch: 3,
      matches_per_round: 2,
      evolve_top_k: 2,
      grounding_depth: "shallow",
    },
  },
  {
    name: "standard",
    label: "Standard",
    summary:
      "Three checkpoints. Explore, develop, verify and challenge a complete answer.",
    shape: {
      workflow: "adaptive",
      rounds: 3,
      generation_batch: 6,
      matches_per_round: 4,
      evolve_top_k: 3,
      grounding_depth: "standard",
    },
  },
  {
    name: "deep",
    label: "Deep",
    summary:
      "Five checkpoints, wider search and deeper evidence review. Unproven claims stay explicit.",
    shape: {
      workflow: "adaptive",
      rounds: 5,
      generation_batch: 8,
      matches_per_round: 6,
      evolve_top_k: 4,
      grounding_depth: "deep",
    },
  },
];

export function getPreset(name: PresetName): Preset {
  const preset = PRESETS.find((candidate) => candidate.name === name);
  if (!preset) throw new Error(`unknown preset ${name}`);
  return preset;
}

/** The config fields a preset sets, call ceiling included, ready to merge. */
export function presetConfig(
  preset: Preset,
): Pick<
  RunConfig,
  | "rounds"
  | "workflow"
  | "generation_batch"
  | "matches_per_round"
  | "evolve_top_k"
  | "grounding_depth"
  | "budget_calls"
> {
  return {
    workflow: preset.shape.workflow ?? "adaptive",
    rounds: preset.shape.rounds,
    generation_batch: preset.shape.generation_batch,
    matches_per_round: preset.shape.matches_per_round,
    evolve_top_k: preset.shape.evolve_top_k,
    grounding_depth: preset.shape.grounding_depth,
    budget_calls: suggestedBudgetCalls(preset.shape),
  };
}

/** Which preset a config matches, or null when it has been hand-tuned. */
export function matchPreset(config: RunConfig): PresetName | null {
  for (const preset of PRESETS) {
    const shaped = presetConfig(preset);
    const same =
      shaped.rounds === config.rounds &&
      shaped.generation_batch === config.generation_batch &&
      shaped.matches_per_round === config.matches_per_round &&
      shaped.evolve_top_k === config.evolve_top_k &&
      shaped.grounding_depth === config.grounding_depth &&
      shaped.budget_calls === config.budget_calls;
    if (same) return preset.name;
  }
  return null;
}

/* --- Formatting ----------------------------------------------------------- */

/** `$1.20 – $3.60`, or "No cost" for a demo. Never renders a bare "$0.00". */
export function formatUsdRange([low, high]: [number, number]): string {
  if (low === 0 && high === 0) return "No cost";
  const money = (value: number): string =>
    value >= 10 ? `$${value.toFixed(0)}` : `$${value.toFixed(2)}`;
  return `${money(low)} – ${money(high)}`;
}

/** `12 – 30 min`, collapsing to a single figure when the bracket is tight. */
export function formatMinutesRange([low, high]: [number, number]): string {
  if (low === high) return `${low} min`;
  return `${low} – ${high} min`;
}

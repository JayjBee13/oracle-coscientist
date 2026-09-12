/**
 * The ONE status vocabulary.
 *
 * Every enum the backend can hand us — lifecycle, event type, hypothesis and
 * match status, verdict, novelty, operator, workshop state, harness, role,
 * grounding depth, connection state — is translated here into a label a
 * scientist can read and a tone the design system understands.
 *
 * Rule: **no raw enum value is ever rendered.** If a screen needs to show a
 * backend value, it comes through a `describe*` function in this file.
 * `status.test.ts` asserts totality over every declared enum, so adding a value
 * to `api/types.ts` without a label here fails the suite.
 */

import {
  CONTROL_ACTIONS,
  EVENT_TYPES,
  GROUNDING_DEPTHS,
  HARNESSES,
  HYPOTHESIS_STATUSES,
  LIFECYCLES,
  MATCH_STATUSES,
  NOVELTY_LEVELS,
  OPERATORS,
  ROLES,
  RUN_SOURCES,
  VERDICTS,
  WORKSHOP_STATES,
} from "../api/types";
import type {
  ControlAction,
  EventType,
  GroundingDepth,
  Harness,
  HypothesisStatus,
  Lifecycle,
  MatchStatus,
  NoveltyLevel,
  Operator,
  Role,
  RunSource,
  Verdict,
  WorkshopState,
} from "../api/types";

/** Design-system tones. Mirrors the `[data-tone]` blocks in tokens.css. */
export type Tone = "neutral" | "accent" | "go" | "caution" | "danger" | "info";

/** Status mark shapes. Mirrors `.status-mark[data-mark]` in base.css. */
export type StatusMark =
  | "queued"
  | "running"
  | "transitional"
  | "paused"
  | "completed"
  | "stopped"
  | "failed"
  | "lost";

export type StatusDescriptor = {
  label: string;
  tone: Tone;
  mark: StatusMark;
  /** One plain sentence for a tooltip or an empty state. */
  hint: string;
};

export type Labelled = { label: string; tone: Tone };

/** Fallback for values the backend grows before the frontend catches up. */
export function humanize(value: string): string {
  const cleaned = value.replace(/[_-]+/g, " ").trim();
  if (!cleaned) return "Unknown";
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

/* --- Lifecycle ------------------------------------------------------------ */

const LIFECYCLE: Record<Lifecycle, StatusDescriptor> = {
  queued: {
    label: "Queued",
    tone: "neutral",
    mark: "queued",
    hint: "Waiting for its turn to start.",
  },
  running: {
    label: "Running",
    tone: "go",
    mark: "running",
    hint: "Working through a round right now.",
  },
  pausing: {
    label: "Waiting to pause",
    tone: "caution",
    mark: "transitional",
    hint: "Finishing the calls already in flight, then it will pause.",
  },
  paused: {
    label: "Paused",
    tone: "caution",
    mark: "paused",
    hint: "Stopped between steps. Resume picks up where it left off.",
  },
  stopping: {
    label: "Stopping",
    tone: "caution",
    mark: "transitional",
    hint: "Winding down — a report will still be written from what exists.",
  },
  stopped: {
    label: "Stopped",
    tone: "neutral",
    mark: "stopped",
    hint: "Ended early on request.",
  },
  finishing: {
    label: "Finishing up",
    tone: "accent",
    mark: "transitional",
    hint: "Completing the current round, then writing the report.",
  },
  completed: {
    label: "Completed",
    tone: "go",
    mark: "completed",
    hint: "Ran to the end and wrote a report.",
  },
  failed: {
    label: "Failed",
    tone: "danger",
    mark: "failed",
    hint: "Ended on an error. The Activity tab has the diagnosis.",
  },
  lost: {
    label: "Lost contact",
    tone: "danger",
    mark: "lost",
    hint: "The process stopped reporting in and could not be recovered.",
  },
};

/** Lifecycles where work is still in flight. */
export const ACTIVE_LIFECYCLES: readonly Lifecycle[] = [
  "queued",
  "running",
  "pausing",
  "paused",
  "stopping",
  "finishing",
];

/** Lifecycles that hold a harness lane (C4 partial unique index). */
export const LANE_BLOCKING_LIFECYCLES: readonly Lifecycle[] = [
  "queued",
  "running",
  "pausing",
  "stopping",
  "finishing",
];

export const TERMINAL_LIFECYCLES: readonly Lifecycle[] = [
  "completed",
  "failed",
  "lost",
  "stopped",
];

export function isLifecycle(value: string): value is Lifecycle {
  return (LIFECYCLES as readonly string[]).includes(value);
}

export function isActiveLifecycle(value: string): boolean {
  return (ACTIVE_LIFECYCLES as readonly string[]).includes(value);
}

export function isLaneBlocking(value: string): boolean {
  return (LANE_BLOCKING_LIFECYCLES as readonly string[]).includes(value);
}

export function isTerminalLifecycle(value: string): boolean {
  return (TERMINAL_LIFECYCLES as readonly string[]).includes(value);
}

export function describeLifecycle(value: string): StatusDescriptor {
  if (isLifecycle(value)) return LIFECYCLE[value];
  return {
    label: humanize(value),
    tone: "neutral",
    mark: "queued",
    hint: "This state is newer than the interface. Reload to pick up changes.",
  };
}

/** Why a run stopped, as the backend records it on `RunSummary.ended_reason`. */
const ENDED_REASON: Record<string, string> = {
  rounds_done: "It ran every round it was given.",
  budget_calls: "It reached its model-call ceiling.",
  budget_usd: "It reached its cost ceiling.",
  wall_clock: "It reached its time limit.",
  stop_requested: "You stopped it.",
  finish_requested: "You asked it to finish.",
  force_stopped: "The run process was killed.",
};

/**
 * Status of a run as the user should read it — one vocabulary for live,
 * imported and demo runs alike. Imported history reads "Completed · Imported"
 * and never borrows the go tone from a run we actually watched.
 *
 * **Losses qualify the label; they do not fork the lifecycle.** A run that lost
 * three whole steps and wrote no report used to render identically to one that
 * did everything asked — "Completed", tone `go` — because the chip was a pure
 * function of `lifecycle`. It still is, plus one adjective: lifecycle drives
 * control gating, lane filtering and the totality tests, and forking it would
 * multiply four tables to express that adjective.
 *
 * One function, so the header, the run list and the lane bar all change at once.
 */
export function describeRunStatus(run: {
  lifecycle: string;
  source?: RunSource | string;
  lost_steps?: number;
  failed_calls?: number;
  has_overview?: boolean;
  ended_reason?: string | null;
}): StatusDescriptor {
  const base = describeLifecycle(run.lifecycle);
  if (run.source === "imported") {
    return {
      label: `${base.label} · Imported`,
      tone: "neutral",
      mark: base.mark,
      hint: "Imported from an earlier engine run. It cannot be controlled or resumed.",
    };
  }

  const lost = run.lost_steps ?? 0;
  const failed = run.failed_calls ?? 0;
  const noReport = run.has_overview === false && isTerminalLifecycle(run.lifecycle);
  const endedWhy = run.ended_reason ? ENDED_REASON[run.ended_reason] : undefined;
  if (lost === 0 && failed === 0 && !noReport) {
    return endedWhy ? { ...base, hint: `${base.hint} ${endedWhy}` } : base;
  }

  const parts: string[] = [];
  if (lost > 0) parts.push(`${lost} step${lost === 1 ? "" : "s"} failed`);
  else if (failed > 0)
    parts.push(`${failed} call${failed === 1 ? "" : "s"} failed and retried`);
  if (noReport) parts.push("no report was written");
  const why = endedWhy ? ` ${endedWhy}` : "";

  return {
    label: lost > 0 || noReport ? `${base.label} with losses` : base.label,
    tone: lost > 0 || noReport ? "caution" : base.tone,
    mark: base.mark,
    hint: `${sentence(parts.join(" and "))}.${why}`,
  };
}

function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/* --- Controls (C4 action table) ------------------------------------------- */

export type ControlDescriptor = Labelled & {
  /** Shown inside the confirm dialog; empty when no confirmation is needed. */
  confirm: string;
  destructive: boolean;
};

const CONTROL: Record<ControlAction, ControlDescriptor> = {
  pause: {
    label: "Pause",
    tone: "caution",
    confirm: "",
    destructive: false,
  },
  resume: {
    label: "Resume",
    tone: "go",
    confirm: "",
    destructive: false,
  },
  continue: {
    // Never "Continue" alone, and deliberately not near the wizard's "Run again".
    // Those are the two things a scientist has to tell apart on a finished run —
    // this one keeps the idea pool and its ratings, that one throws them away and
    // asks the question afresh — and getting it wrong costs a whole run of calls.
    label: "Run more rounds",
    tone: "accent",
    confirm:
      "The run keeps every hypothesis, rating and review it already has. The next round starts from the guidance the last one ended on.",
    destructive: false,
  },
  stop: {
    label: "Stop",
    tone: "danger",
    confirm:
      "Stopping ends the run after the calls already in flight. A report is still written from what exists.",
    destructive: true,
  },
  finish: {
    label: "Finish after this round",
    tone: "accent",
    confirm: "The current round completes, then the report is written and the run ends.",
    destructive: false,
  },
  force_stop: {
    label: "Force stop",
    tone: "danger",
    confirm:
      "Force stop kills the run process immediately. Work in flight is lost and the report may be incomplete.",
    destructive: true,
  },
};

/**
 * Takes a `string`, not a `ControlAction`, and degrades like every other
 * describer here. The legal-action list inside a 409 is server data — a backend
 * that grows an action this build has never heard of must render as a word
 * rather than as `undefined.label`, which is a blank line in an error message.
 */
export function describeControlAction(action: string): ControlDescriptor {
  if ((CONTROL_ACTIONS as readonly string[]).includes(action)) {
    return CONTROL[action as ControlAction];
  }
  return { label: humanize(action), tone: "neutral", confirm: "", destructive: false };
}

/**
 * C4: pause(running) · resume(paused) · continue(completed|stopped) ·
 * stop(running|paused|pausing) · finish(running|paused) · force_stop(any active).
 *
 * `source` is not decoration. Every imported run is `completed`, which used to be
 * a lifecycle with no controls at all and since `continue` exists is not — and an
 * imported run is the one thing continuing must never touch, because it carries no
 * model table for a supervisor to run against. The backend refuses on source too
 * (`services/runs/controls.allowed_actions`); this mirrors it so the button never
 * appears rather than appearing and erroring.
 */
export function controlActionsFor(
  lifecycle: string,
  source?: RunSource | string,
): ControlAction[] {
  if (source === "imported") return [];
  const actions: ControlAction[] = [];
  if (lifecycle === "running") actions.push("pause");
  if (lifecycle === "paused") actions.push("resume");
  if (lifecycle === "completed" || lifecycle === "stopped") actions.push("continue");
  if (lifecycle === "running" || lifecycle === "paused") actions.push("finish");
  if (lifecycle === "running" || lifecycle === "paused" || lifecycle === "pausing") {
    actions.push("stop");
  }
  if (isActiveLifecycle(lifecycle)) actions.push("force_stop");
  return actions;
}

export function isControlAllowed(
  lifecycle: string,
  action: ControlAction,
  source?: RunSource | string,
): boolean {
  return controlActionsFor(lifecycle, source).includes(action);
}

/* --- Connection ----------------------------------------------------------- */

export type ConnectionState = "idle" | "connecting" | "live" | "reconnecting" | "offline";

const CONNECTION: Record<ConnectionState, StatusDescriptor> = {
  idle: {
    label: "Connected",
    tone: "neutral",
    mark: "completed",
    hint: "Talking to the backend. No run is streaming right now.",
  },
  connecting: {
    label: "Connecting",
    tone: "neutral",
    mark: "transitional",
    hint: "Opening the live event stream.",
  },
  live: {
    label: "Live",
    tone: "go",
    mark: "running",
    hint: "Receiving events as they happen.",
  },
  reconnecting: {
    label: "Reconnecting",
    tone: "caution",
    mark: "transitional",
    hint: "The stream dropped. Retrying — no events will be missed.",
  },
  offline: {
    label: "Offline",
    tone: "danger",
    mark: "failed",
    hint: "Cannot reach the backend. Check that it is running.",
  },
};

export function describeConnection(state: ConnectionState): StatusDescriptor {
  return CONNECTION[state];
}

/* --- Events --------------------------------------------------------------- */

const EVENT: Record<EventType, Labelled> = {
  round_started: { label: "Round started", tone: "accent" },
  hypothesis_added: { label: "Hypothesis added", tone: "go" },
  review_recorded: { label: "Review recorded", tone: "neutral" },
  cluster_applied: { label: "Themes updated", tone: "neutral" },
  graft_fired: { label: "Diversity injection fired", tone: "info" },
  graft_abstained: { label: "Diversity injection held back", tone: "neutral" },
  match_completed: { label: "Match decided", tone: "accent" },
  feedback_recorded: { label: "Guidance for next round", tone: "info" },
  round_completed: { label: "Round complete", tone: "go" },
  call_started: { label: "Model call started", tone: "neutral" },
  call_finished: { label: "Model call finished", tone: "neutral" },
  rate_limited: { label: "Rate limited — waiting", tone: "caution" },
  role_degraded: { label: "Model degraded", tone: "caution" },
  contract_violation: { label: "Unusable model output", tone: "caution" },
  step_failed: { label: "Step produced less than asked", tone: "caution" },
  schema_repaired: { label: "Unusable output — asked again", tone: "caution" },
  context_truncated: { label: "Context documents did not all fit", tone: "caution" },
  report_skipped: { label: "No report was written", tone: "danger" },
  note_added: { label: "Note added", tone: "info" },
  hypothesis_archived: { label: "Hypothesis archived", tone: "neutral" },
  budget_warning: { label: "Budget warning", tone: "caution" },
  lifecycle_changed: { label: "Status changed", tone: "neutral" },
  run_extended: { label: "More rounds added", tone: "info" },
  run_finished: { label: "Run finished", tone: "go" },
  run_failed: { label: "Run failed", tone: "danger" },
};

export function describeEventType(type: string): Labelled {
  if ((EVENT_TYPES as readonly string[]).includes(type)) {
    return EVENT[type as EventType];
  }
  return { label: humanize(type), tone: "neutral" };
}

/* --- Hypotheses, matches, reviews ----------------------------------------- */

const HYPOTHESIS_STATUS: Record<HypothesisStatus, Labelled> = {
  active: { label: "In play", tone: "go" },
  rejected: { label: "Rejected", tone: "danger" },
  archived: { label: "Set aside", tone: "neutral" },
};

export function describeHypothesisStatus(value: string): Labelled {
  if ((HYPOTHESIS_STATUSES as readonly string[]).includes(value)) {
    return HYPOTHESIS_STATUS[value as HypothesisStatus];
  }
  return { label: humanize(value), tone: "neutral" };
}

const MATCH_STATUS: Record<MatchStatus, Labelled> = {
  planned: { label: "Scheduled", tone: "neutral" },
  completed: { label: "Decided", tone: "go" },
  skipped: { label: "Skipped", tone: "caution" },
};

export function describeMatchStatus(value: string): Labelled {
  if ((MATCH_STATUSES as readonly string[]).includes(value)) {
    return MATCH_STATUS[value as MatchStatus];
  }
  return { label: humanize(value), tone: "neutral" };
}

const VERDICT: Record<Verdict, Labelled> = {
  pass: { label: "Passed review", tone: "go" },
  reject: { label: "Rejected in review", tone: "danger" },
};

export function describeVerdict(value: string): Labelled {
  if ((VERDICTS as readonly string[]).includes(value)) {
    return VERDICT[value as Verdict];
  }
  return { label: humanize(value), tone: "neutral" };
}

const NOVELTY: Record<NoveltyLevel, Labelled> = {
  high: { label: "Highly novel", tone: "go" },
  moderate: { label: "Moderately novel", tone: "info" },
  low: { label: "Familiar territory", tone: "neutral" },
};

export function describeNovelty(value: string): Labelled {
  if ((NOVELTY_LEVELS as readonly string[]).includes(value)) {
    return NOVELTY[value as NoveltyLevel];
  }
  return { label: humanize(value), tone: "neutral" };
}

const OPERATOR: Record<Operator, Labelled> = {
  grounding: { label: "Grounded in evidence", tone: "info" },
  combination: { label: "Combined two ideas", tone: "info" },
  simplification: { label: "Simplified", tone: "info" },
  out_of_box: { label: "Out-of-the-box leap", tone: "accent" },
};

export function describeOperator(value: string): Labelled {
  if ((OPERATORS as readonly string[]).includes(value)) {
    return OPERATOR[value as Operator];
  }
  return { label: humanize(value), tone: "neutral" };
}

/* --- Workshop, harness, roles, settings ----------------------------------- */

const WORKSHOP: Record<WorkshopState, StatusDescriptor> = {
  refining: {
    label: "Drafting options",
    tone: "accent",
    mark: "running",
    hint: "Writing two candidate research prompts.",
  },
  options_ready: {
    label: "Options ready",
    tone: "go",
    mark: "completed",
    hint: "Pick a direction, merge the two, or ask for another pass.",
  },
  chosen: {
    label: "Prompt chosen",
    tone: "go",
    mark: "completed",
    hint: "The final prompt is set.",
  },
  failed: {
    label: "Could not draft options",
    tone: "danger",
    mark: "failed",
    hint: "The model did not return a usable pair of prompts.",
  },
};

export function describeWorkshopState(value: string): StatusDescriptor {
  if ((WORKSHOP_STATES as readonly string[]).includes(value)) {
    return WORKSHOP[value as WorkshopState];
  }
  return {
    label: humanize(value),
    tone: "neutral",
    mark: "queued",
    hint: "",
  };
}

const HARNESS: Record<Harness, Labelled> = {
  claude: { label: "Claude", tone: "accent" },
  codex: { label: "Codex", tone: "neutral" },
  demo: { label: "Demo", tone: "info" },
};

export function describeHarness(value: string): Labelled {
  if ((HARNESSES as readonly string[]).includes(value)) {
    return HARNESS[value as Harness];
  }
  return { label: humanize(value), tone: "neutral" };
}

const SOURCE: Record<RunSource, Labelled> = {
  app: { label: "Run here", tone: "neutral" },
  imported: { label: "Imported", tone: "neutral" },
};

export function describeSource(value: string): Labelled {
  if ((RUN_SOURCES as readonly string[]).includes(value)) {
    return SOURCE[value as RunSource];
  }
  return { label: humanize(value), tone: "neutral" };
}

const ROLE: Record<Role, string> = {
  generation: "Generation",
  framing: "Problem framing",
  verification: "Evidence verification",
  synthesis: "Solution integration",
  challenge: "Independent challenge",
  reflection: "Reflection",
  proximity: "Clustering",
  ranking: "Tournament",
  evolution: "Evolution",
  meta_review: "Meta-review",
  cartographer: "Diversity injection",
  overview: "Report",
  workshop: "Prompt workshop",
};

export function describeRole(value: string): string {
  if ((ROLES as readonly string[]).includes(value)) return ROLE[value as Role];
  return humanize(value);
}

const GROUNDING: Record<GroundingDepth, Labelled> = {
  shallow: { label: "Light — search only if essential", tone: "neutral" },
  standard: { label: "Standard — verify key claims", tone: "info" },
  deep: { label: "Deep — verify every major claim", tone: "accent" },
};

export function describeGroundingDepth(value: string): Labelled {
  if ((GROUNDING_DEPTHS as readonly string[]).includes(value)) {
    return GROUNDING[value as GroundingDepth];
  }
  return { label: humanize(value), tone: "neutral" };
}

/**
 * Every tier name this app can meet, including the ones it no longer offers:
 * a run's config keeps whatever the tier was called on the day it launched, and
 * the Settings tab of a run from before a rename must still read as English.
 *
 * Keyed by string rather than by `ModelTier` deliberately. A tier added on the
 * backend reaches this file as an unlabelled value first, and the lookup below
 * must degrade to a humanised name — not to `undefined.label`, which is a blank
 * screen wherever a tier is shown.
 */
const MODEL_TIER: Record<string, Labelled> = {
  // The current four. `max`, `high` and `med` move thinking effort and nothing
  // else — every step keeps the model its job asks for — so those names are
  // degrees rather than qualities, and only the top one is worth an accent.
  //
  // `low` is the speed preset and the one tier that also pins the model every
  // step runs on. Still neutral: it is not a downgrade and it is not cheaper —
  // it costs the same per call as `med` — so an accent or a caution here would
  // be this file editorialising about a choice somebody made deliberately. The
  // tier's own words, and the model it pins, are served per tier in
  // `/api/capabilities` and rendered from there; this map is only the name.
  max: { label: "Max", tone: "accent" },
  high: { label: "High", tone: "neutral" },
  med: { label: "Med", tone: "neutral" },
  low: { label: "Low", tone: "neutral" },
  // Retired names, kept because a run's config holds whatever the tier was
  // called on the day it launched.
  standard: { label: "Standard", tone: "neutral" },
  maximum: { label: "Maximum", tone: "accent" },
  balanced: { label: "Balanced", tone: "neutral" },
  quality: { label: "Highest quality", tone: "accent" },
};

export function describeModelTier(value: string): Labelled {
  return MODEL_TIER[value] ?? { label: humanize(value), tone: "neutral" };
}

/**
 * How hard a step is thinking, in words.
 *
 * The vocabularies are the providers' own and they do not agree — Anthropic's
 * ladder runs `low…max`, OpenAI's adds `none` and `minimal` below it — so this
 * is a union of both rather than one scale pretending to cover the two. Which
 * rungs a given model actually offers is served per model by
 * `/api/capabilities`; this file only knows how to *say* them.
 *
 * It exists because `humanize` renders the one value that matters most as
 * "Xhigh". Everything it does not know still falls through to `humanize`, so a
 * provider that grows a rung reads as a word rather than as a blank.
 */
const EFFORT: Record<string, string> = {
  none: "None",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Maximum",
};

export function describeEffort(value: string): string {
  return EFFORT[value] ?? humanize(value);
}

/**
 * Who runs the call. Two vendors, two CLIs, one word each.
 *
 * Mapped rather than humanised for one reason that is not cosmetic:
 * `humanize("openai")` is "Openai", and a screen that miscapitalises the vendor
 * it is about to spend your rate limit on does not read as an instrument. The
 * tones match the harness lane's, so Anthropic is the accent in the top bar and
 * in the lane bar at the same time.
 */
const PROVIDER: Record<string, Labelled> = {
  anthropic: { label: "Anthropic", tone: "accent" },
  openai: { label: "OpenAI", tone: "neutral" },
};

export function describeProvider(value: string): Labelled {
  return PROVIDER[value] ?? { label: humanize(value), tone: "neutral" };
}

/* --- Metric glossary ------------------------------------------------------
   Jargon the previous UI put on screen unexplained. Attach these as tooltips
   wherever the number appears; never show the bare term.
   ------------------------------------------------------------------------- */
export const METRIC_HINTS = {
  elo: "Elo — relative strength from head-to-head matches within this run. Comparable inside one run only.",
  matches: "How many head-to-head comparisons this hypothesis has been through.",
  calls:
    "Model calls used and the maximum allowance set for this run. The maximum is a stop limit, not completion progress.",
  spend: "Estimated cost of the model calls this run has made.",
  tokens: "Total tokens sent and received, including cached prompt reuse.",
  graft:
    "Diversity injection — when ideas converge too tightly, a fresh framing from another field is grafted in.",
  cluster: "Theme — hypotheses the clustering step judged to be about the same idea.",
  round: "One full pass: generate, review, cluster, compete, evolve, then meta-review.",
  degraded:
    "Non-fatal problems the run worked around, such as a malformed model response that was retried.",
} as const;

/**
 * A cluster as it is named on screen.
 *
 * The proximity step names clusters itself, and on most runs it names them `0`,
 * `1`, `12`. A bare "12" beside a colour swatch is an index leaking onto the
 * screen; "Cluster 12" is the same fact in the reader's language. When the model
 * wrote a real name — "energetics-0", or a whole clause — that name is what the
 * reader sees, untouched.
 *
 * It lives here, beside `METRIC_HINTS.cluster`, because three surfaces show this
 * one column: the graph legend, the graph's own tooltips, and the detail rail.
 * They said "Cluster 0" and "Theme: 0" for the same node for as long as the
 * function had a home in only one of them.
 */
export function clusterLabel(name: string): string {
  return /^\d+$/.test(name) ? `Cluster ${name}` : name;
}

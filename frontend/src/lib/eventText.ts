/**
 * Turning an event payload into a sentence.
 *
 * The old live view appended five lines of raw JSON and called it a log. Every
 * event here has a documented payload shape (`engine/events.py`), so each one
 * can say what actually happened in the words the rest of the app uses —
 * "h002 beat h005 · 1246 → 1262" rather than a blob with `elo_a_after` in it.
 *
 * Unknown types and missing fields degrade to something readable rather than
 * throwing: a newer backend is a normal thing to meet, and the Activity tab is
 * exactly where you look when something is wrong.
 */

import type { RunEvent } from "../api/types";
import { compactNumber, formatDuration, previewText } from "./format";
import {
  describeLifecycle,
  describeNovelty,
  describeOperator,
  describeRole,
  describeVerdict,
} from "./status";

/** Filter groups for the Activity tab. Twenty chips is not a filter. */
export const EVENT_FAMILIES = [
  { id: "all", label: "Everything", types: [] as string[] },
  {
    id: "science",
    label: "Ideas",
    types: ["hypothesis_added", "review_recorded", "hypothesis_archived", "note_added"],
  },
  {
    id: "tournament",
    label: "Tournament",
    types: ["match_completed", "cluster_applied", "graft_fired", "graft_abstained"],
  },
  {
    id: "rounds",
    label: "Rounds",
    types: [
      "research_updated",
      "round_started",
      "round_completed",
      "feedback_recorded",
      "run_extended",
      "run_finished",
    ],
  },
  { id: "calls", label: "Model calls", types: ["call_started", "call_finished"] },
  {
    id: "problems",
    label: "Problems",
    types: [
      "rate_limited",
      "role_degraded",
      "contract_violation",
      "budget_warning",
      "run_failed",
      "malformed",
    ],
  },
] as const;

export type EventFamilyId = (typeof EVENT_FAMILIES)[number]["id"];

export function eventMatchesFamily(type: string, family: EventFamilyId): boolean {
  if (family === "all") return true;
  const entry = EVENT_FAMILIES.find((candidate) => candidate.id === family);
  return entry ? (entry.types as readonly string[]).includes(type) : true;
}

function text(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number") return String(value);
  return null;
}

function num(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A cost ceiling in an audit line: a figure, or the honest word for no ceiling. */
function describeCeiling(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? `$${value.toFixed(2)}`
    : "none";
}

/* --- Calls that never happened -------------------------------------------- */

/** What a demo run's call events say instead of naming a model. */
export const NO_MODEL_CALL = "Demo · no model call";

/** The harness value that means "scripted; nothing was ever invoked". */
const DEMO_HARNESS = "demo";

/**
 * Strips the model a demo run never called.
 *
 * A demo run is scripted end to end and makes no model calls at all — but the
 * engine still stamps `call_started` and `call_finished` with the model a *real*
 * run of the same settings would have used. Rendering that names a model that
 * was never invoked, which is the bug this exists to kill: the model chip
 * already refuses to name one, and the event stream, the trust line and the
 * per-call telemetry must agree with it.
 *
 * The harness belongs to the run, not to the event, so the pages that own the
 * run strip the claim once — here — and everything downstream reads the same
 * honest stream. What it leaves behind is `harness` on the payload: the field
 * the backend would use if it ever carried this itself, so a backend that
 * starts sending it needs no second code path.
 */
export function withoutFalseModelClaims(
  events: readonly RunEvent[],
  harness: string,
): readonly RunEvent[] {
  if (harness !== DEMO_HARNESS) return events;
  return events.map((event) => {
    if (event.type !== "call_started" && event.type !== "call_finished") return event;
    const { model: _model, ...rest } = event.payload ?? {};
    return { ...event, payload: { ...rest, harness: DEMO_HARNESS } };
  });
}

/** True when this call event belongs to a run that calls no models. */
export function isDemoCall(payload: Record<string, unknown>): boolean {
  return text(payload, "harness") === DEMO_HARNESS;
}

/** The model half of a call line — or the truth when there is no model to name. */
export function callModelText(payload: Record<string, unknown>): string {
  if (isDemoCall(payload)) return NO_MODEL_CALL;
  return text(payload, "model") ?? "model not recorded";
}

/**
 * Per-call telemetry lives under `telemetry` on a current backend and directly
 * on the payload on an older one. Read both rather than showing nothing.
 */
export function callTelemetry(payload: Record<string, unknown>): Record<string, unknown> {
  const nested = payload.telemetry;
  if (nested && typeof nested === "object" && !Array.isArray(nested)) {
    return { ...payload, ...(nested as Record<string, unknown>) };
  }
  return payload;
}

/**
 * "ran fable, not the model that was asked for" — a substitution the CLI made
 * silently. A run that quietly used a different model is the one thing a
 * results-first reader has no way to notice.
 */
export function substitutionText(payload: Record<string, unknown>): string | null {
  const telemetry = callTelemetry(payload);
  const ran = text(telemetry, "model_ran");
  const requested = text(telemetry, "model_requested");
  if (telemetry.model_substituted !== true && !(ran && requested && ran !== requested)) {
    return null;
  }
  const floor = telemetry.model_below_floor === true ? " — below the model floor" : "";
  if (ran && requested) return `ran ${ran}, not ${requested}${floor}`;
  if (ran) return `ran ${ran}, which is not what was asked for${floor}`;
  return `ran a different model than requested${floor}`;
}

/** One line describing the event, or "" when the type label already says it. */
export function eventBody(event: RunEvent): string {
  const payload = event.payload ?? {};

  switch (event.type) {
    case "research_updated":
      return String(event.payload.detail ?? "Research workspace updated");
    case "round_started":
      return `Round ${text(payload, "round") ?? "?"}`;

    case "hypothesis_added": {
      const operator = text(payload, "operator");
      const lineage = operator ? ` · ${describeOperator(operator).label}` : "";
      return `${text(payload, "hid") ?? "?"} — ${text(payload, "title") ?? "untitled"}${lineage}`;
    }

    case "review_recorded": {
      const verdict = text(payload, "verdict");
      const novelty = text(payload, "novelty_level");
      return [
        text(payload, "hid") ?? "?",
        verdict ? describeVerdict(verdict).label.toLowerCase() : null,
        novelty ? describeNovelty(novelty).label.toLowerCase() : null,
      ]
        .filter(Boolean)
        .join(" · ");
    }

    case "cluster_applied": {
      const clusters = num(payload, "n_clusters");
      const duplicates = num(payload, "duplicates");
      return `${clusters ?? "?"} themes${duplicates ? `, ${duplicates} duplicates set aside` : ""}`;
    }

    case "graft_fired":
      return `A fresh framing from ${text(payload, "source_domain") ?? "another field"} (${
        text(payload, "votes") ?? "?"
      } signals, ${text(payload, "n_clusters") ?? "?"} themes)`;

    case "graft_abstained":
      return `Held back: ${text(payload, "reason") ?? "conditions not met"}`;

    case "match_completed": {
      const a = text(payload, "hid_a") ?? "?";
      const b = text(payload, "hid_b") ?? "?";
      const winner = num(payload, "winner");
      const won = winner === 1 ? a : winner === 2 ? b : null;
      const lost = winner === 1 ? b : winner === 2 ? a : null;
      const eloAfter =
        winner === 1 ? num(payload, "elo_a_after") : num(payload, "elo_b_after");
      const eloBefore =
        winner === 1 ? num(payload, "elo_a_before") : num(payload, "elo_b_before");
      const movement =
        eloBefore != null && eloAfter != null
          ? ` · ${Math.round(eloBefore)} → ${Math.round(eloAfter)}`
          : "";
      return won && lost ? `${won} beat ${lost}${movement}` : `${a} vs ${b}`;
    }

    case "feedback_recorded":
      return previewText(text(payload, "guidance") ?? "", 140);

    case "round_completed":
      return [
        `${text(payload, "hypotheses_added") ?? 0} new`,
        `${text(payload, "reviews") ?? 0} reviewed`,
        `${text(payload, "matches_completed") ?? 0} matches`,
      ].join(" · ");

    case "call_started":
      return `${describeRole(text(payload, "role") ?? "")} · ${callModelText(payload)}`;

    case "call_finished": {
      const duration = num(payload, "duration_ms");
      const ok = payload.ok;
      const denials = callTelemetry(payload).permission_denials;
      const denied = Array.isArray(denials) ? denials.length : 0;
      return [
        describeRole(text(payload, "role") ?? ""),
        isDemoCall(payload) ? NO_MODEL_CALL : null,
        duration != null ? formatDuration(duration) : null,
        ok === false ? "failed" : null,
        substitutionText(payload),
        denied > 0 ? `${denied} tool ${denied === 1 ? "denial" : "denials"}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
    }

    case "rate_limited":
      return `${describeRole(text(payload, "role") ?? "")} — waiting for the limit to clear`;

    case "role_degraded":
      return `${describeRole(text(payload, "role") ?? "")}: ${
        text(payload, "reason") ?? "fell back"
      }`;

    case "contract_violation":
      return `${describeRole(text(payload, "role") ?? "")}: ${previewText(
        text(payload, "error") ?? "unusable output",
        160,
      )}`;

    case "note_added":
      return previewText(text(payload, "text") ?? "", 160);

    case "hypothesis_archived":
      return `${text(payload, "hid") ?? "?"} set aside`;

    case "budget_warning": {
      const used = num(payload, "calls_used");
      const total = num(payload, "budget_calls");
      const reason = text(payload, "reason") ?? "approaching the ceiling";
      return `${reason}${used != null && total != null ? ` · ${used}/${total} calls` : ""}`;
    }

    case "lifecycle_changed": {
      const next = text(payload, "lifecycle");
      return next ? describeLifecycle(next).label : "";
    }

    case "run_extended": {
      // The audit line for the one thing that may change a launched run's config.
      // Both sides of both numbers, because "extended" without them is not a record.
      const added = num(payload, "added_rounds");
      const target = num(payload, "rounds_target");
      const before = num(payload, "previous_rounds_target");
      const budget = num(payload, "budget_calls");
      const budgetBefore = num(payload, "previous_budget_calls");
      const parts: string[] = [
        added != null ? `+${added} round${added === 1 ? "" : "s"}` : "more rounds",
      ];
      if (target != null && before != null) parts.push(`target ${before} → ${target}`);
      if (budget != null && budgetBefore != null && budget !== budgetBefore) {
        parts.push(`budget ${budgetBefore} → ${budget} calls`);
      }
      // The cost ceiling appears only when it moved, and `null` on either side
      // is "none" rather than $0.00 — a run continued out of an obsolete cap is
      // the case this line exists to record, and "cost ceiling $5.00 → $0.00"
      // would record the opposite of what happened.
      const usd = payload["budget_usd"];
      const usdBefore = payload["previous_budget_usd"];
      if (usd !== undefined && usdBefore !== undefined && usd !== usdBefore) {
        parts.push(
          `cost ceiling ${describeCeiling(usdBefore)} → ${describeCeiling(usd)}`,
        );
      }
      return parts.join(" · ");
    }

    case "run_finished":
      return [
        `${text(payload, "rounds_completed") ?? "?"} rounds`,
        `${text(payload, "hypotheses") ?? "?"} hypotheses`,
        `${text(payload, "calls_used") ?? "?"} calls`,
      ].join(" · ");

    case "run_failed":
      return previewText(errorText(payload.error), 220);

    default: {
      const tokens = num(payload, "tokens");
      if (tokens != null) return `${compactNumber(tokens)} tokens`;
      return "";
    }
  }
}

/** Which step of the loop the run is in, in the scientist's words. */
const PHASE_BY_EVENT: Record<string, string> = {
  round_started: "Starting the round",
  hypothesis_added: "Writing hypotheses",
  review_recorded: "Reviewing",
  cluster_applied: "Grouping themes",
  graft_fired: "Injecting a fresh framing",
  match_completed: "Running the tournament",
  feedback_recorded: "Summarising the round",
  round_completed: "Between rounds",
};

const PHASE_BY_ROLE: Record<string, string> = {
  generation: "Writing hypotheses",
  reflection: "Reviewing",
  proximity: "Grouping themes",
  ranking: "Running the tournament",
  evolution: "Evolving the leaders",
  meta_review: "Summarising the round",
  cartographer: "Injecting a fresh framing",
  overview: "Writing the report",
};

/**
 * The in-flight call is the best evidence of what is happening now; the last
 * step-bearing event is the fallback for the gaps between calls.
 */
export function currentPhase(
  events: readonly RunEvent[],
  inFlightRole: string | null,
): string | null {
  if (inFlightRole && PHASE_BY_ROLE[inFlightRole]) return PHASE_BY_ROLE[inFlightRole];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const phase = PHASE_BY_EVENT[events[index].type];
    if (phase) return phase;
  }
  return null;
}

/** Errors arrive as a string or as a structured payload; both must read. */
export function errorText(value: unknown): string {
  if (value == null) return "No detail was recorded.";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const message = record.message ?? record.error ?? record.detail ?? record.reason;
    if (typeof message === "string" && message.trim()) return message;
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

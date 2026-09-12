/**
 * The trust line: everything that answers "is this thing still alive?"
 *
 * A research run is twenty minutes of near-silence punctuated by events. The
 * old UI showed a status word that was set at launch and never moved, so a
 * wedged run and a working one looked identical. These derivations exist so the
 * screen can say which call is in flight, how long it has been quiet, what it
 * has spent, and roughly how much longer it will take — and can say "we do not
 * know" where that is the truth.
 *
 * Pure functions over the event buffer, so the states that matter (idle,
 * in-call, stale, silent, degraded) are unit-testable without a clock or a
 * server.
 */

import type { RunEvent } from "../api/types";
import type { Tone } from "./status";
import { callTelemetry, isDemoCall } from "./eventText";
import { parseTs } from "./format";

/** Quiet longer than this and the age chip turns amber. */
export const STALE_MS = 90_000;

/** Quiet longer than this and it turns red — something is probably wrong. */
export const SILENT_MS = 7 * 60_000;

/** Calls needed before an estimate is worth showing at all. */
const MIN_CALLS_FOR_ETA = 3;

export type InFlightCall = {
  role: string;
  model: string | null;
  /** True when the run makes no model calls at all, so there is no model to name. */
  noModelCall: boolean;
  round: number | null;
  startedAt: number;
  elapsedMs: number;
};

export type TrustState = {
  /** The call the run is inside right now — the spinner's subject. */
  inCall: InFlightCall | null;
  /** How many calls are open at once (generation shards, parallel reviews). */
  inFlightCount: number;
  /** Milliseconds since the most recent event of any kind. */
  lastActivityMs: number | null;
  lastActivityTone: Tone;
  /** True once silence has passed the amber threshold on a live run. */
  stale: boolean;
  /** Typical completed-call duration, from the events we have. */
  medianCallMs: number | null;
  /** Rough wall-clock remaining. Null until there is evidence for a number. */
  etaMs: number | null;
};

export type TrustInput = {
  events: readonly RunEvent[];
  lifecycle: string;
  now: number;
  callsUsed: number;
  budgetCalls: number;
  roundsTarget: number;
  roundsCompleted: number;
};

function payloadString(event: RunEvent, key: string): string | null {
  const value = event.payload?.[key];
  return typeof value === "string" && value ? value : null;
}

/**
 * Pairs `call_started` with `call_finished` in arrival order, preferring a
 * finish for the same role. Anything still unpaired at the end is in flight —
 * which is also how a call that died without a finish event stays visible
 * instead of quietly disappearing.
 */
export function deriveTrust(input: TrustInput): TrustState {
  const { events, now } = input;
  const open: InFlightCall[] = [];
  const durations: number[] = [];
  let lastTs: number | null = null;

  for (const event of events) {
    const ts = parseTs(event.ts);
    if (!Number.isNaN(ts) && (lastTs === null || ts > lastTs)) lastTs = ts;

    if (event.type === "call_started") {
      open.push({
        role: payloadString(event, "role") ?? "unknown",
        model: payloadString(event, "model"),
        noModelCall: isDemoCall(event.payload ?? {}),
        round: typeof event.round === "number" ? event.round : null,
        startedAt: Number.isNaN(ts) ? now : ts,
        elapsedMs: 0,
      });
      continue;
    }

    if (event.type === "call_finished") {
      const role = payloadString(event, "role");
      const index = role ? open.findIndex((call) => call.role === role) : 0;
      const matched = open.splice(index >= 0 ? index : 0, 1)[0];
      const reported = event.payload?.duration_ms;
      if (typeof reported === "number" && reported > 0) durations.push(reported);
      else if (matched && !Number.isNaN(ts))
        durations.push(Math.max(0, ts - matched.startedAt));
    }
  }

  const newest = open.length > 0 ? open[open.length - 1] : null;
  const inCall = newest
    ? { ...newest, elapsedMs: Math.max(0, now - newest.startedAt) }
    : null;

  const lastActivityMs = lastTs === null ? null : Math.max(0, now - lastTs);
  const medianCallMs = median(durations);

  return {
    inCall,
    inFlightCount: open.length,
    lastActivityMs,
    lastActivityTone: activityTone(input.lifecycle, lastActivityMs),
    stale: isStale(input.lifecycle, lastActivityMs),
    medianCallMs,
    etaMs: estimateRemainingMs({ ...input, medianCallMs, sampleSize: durations.length }),
  };
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

/**
 * Silence only means something while the run is supposed to be working. A
 * completed run has been quiet for three weeks and that is fine.
 */
function activityTone(lifecycle: string, lastActivityMs: number | null): Tone {
  if (lastActivityMs === null) return "neutral";
  if (lifecycle !== "running" && lifecycle !== "finishing") return "neutral";
  if (lastActivityMs > SILENT_MS) return "danger";
  if (lastActivityMs > STALE_MS) return "caution";
  return "neutral";
}

function isStale(lifecycle: string, lastActivityMs: number | null): boolean {
  return activityTone(lifecycle, lastActivityMs) !== "neutral";
}

/**
 * Median call duration times the calls we still expect to make. Deliberately
 * crude and labelled as such on screen: calls run three-wide, so this reads
 * long — but a number that errs towards "longer than you think" is the honest
 * direction for something that spends money while you are away.
 */
function estimateRemainingMs(
  input: TrustInput & { medianCallMs: number | null; sampleSize: number },
): number | null {
  const { medianCallMs, sampleSize, roundsCompleted, roundsTarget } = input;
  if (medianCallMs === null || sampleSize < MIN_CALLS_FOR_ETA) return null;
  if (roundsCompleted < 1) return null;
  if (input.lifecycle !== "running" && input.lifecycle !== "finishing") return null;

  const roundsLeft = Math.max(0, roundsTarget - roundsCompleted);
  if (roundsLeft === 0) return null;

  const callsPerRound = input.callsUsed / roundsCompleted;
  const projected = callsPerRound * roundsLeft;
  const budgetLeft = Math.max(0, input.budgetCalls - input.callsUsed);
  const remainingCalls = Math.max(0, Math.min(projected, budgetLeft));
  if (remainingCalls <= 0) return null;

  return medianCallMs * remainingCalls;
}

/* --- Failure forensics ----------------------------------------------------- */

/** A single call that came back with a tool it was refused. */
export type DeniedCall = {
  seq: number;
  role: string | null;
  round: number | null;
  denials: unknown[];
};

/** A call the CLI served with a model other than the one that was asked for. */
export type SubstitutedCall = {
  seq: number;
  role: string | null;
  round: number | null;
  /** The model the run's table asked for. */
  requested: string | null;
  /** The model that actually answered. */
  ran: string | null;
  /** True when what ran is weaker than the floor this app promises. */
  belowFloor: boolean;
};

export type Forensics = {
  /** The `run_failed` payload, verbatim — this is the diagnosis. */
  error: unknown;
  /** Every denial seen anywhere, deduplicated: did this happen at all? */
  permissionDenials: unknown[];
  /** The same denials attributed to the call that hit them: which one, and when? */
  deniedCalls: DeniedCall[];
  /** Calls that silently ran a different model than the one requested. */
  substitutedCalls: SubstitutedCall[];
  /** Non-fatal problems worth reading even when the run survived them. */
  problems: RunEvent[];
};

const PROBLEM_TYPES = new Set([
  "run_failed",
  "contract_violation",
  "role_degraded",
  "rate_limited",
  "budget_warning",
]);

/**
 * Everything needed to explain a failure from the Activity tab alone. The
 * previous UI showed "error failed" and nothing else, which is why the plan
 * requires a failed run to be diagnosable here without a log file.
 *
 * Denials are read from `call_finished` telemetry as well as from the terminal
 * `run_failed` payload. A denial is a configuration fault that belongs to one
 * call — reading it only off the run's obituary lost both the role that hit it
 * and the round it happened in, and said nothing at all on a run that survived.
 *
 * The same telemetry carries which model actually answered. A CLI that quietly
 * serves a different model than the one the run's table asked for changes what
 * the research *is*, and until it is read here that substitution is invisible:
 * nothing fails, nothing warns, and the Settings tab still prints the model
 * that was requested.
 */
export function deriveForensics(events: readonly RunEvent[]): Forensics {
  const deniedCalls: DeniedCall[] = [];
  const substitutedCalls: SubstitutedCall[] = [];
  for (const event of events) {
    if (event.type !== "call_finished") continue;
    const payload = event.payload ?? {};
    const round = typeof event.round === "number" ? event.round : null;
    const role = payloadString(event, "role");

    const denials = collectDenials(payload.telemetry) ?? collectDenials(payload);
    if (denials) deniedCalls.push({ seq: event.seq, role, round, denials });

    const swap = readSubstitution(payload);
    if (swap) substitutedCalls.push({ seq: event.seq, role, round, ...swap });
  }

  const flagged = new Set([
    ...deniedCalls.map((call) => call.seq),
    ...substitutedCalls.map((call) => call.seq),
  ]);
  const problems = events.filter(
    (event) => PROBLEM_TYPES.has(event.type) || flagged.has(event.seq),
  );
  const failure = [...problems].reverse().find((event) => event.type === "run_failed");
  const error = failure?.payload?.error ?? null;
  const fatal = collectDenials(error) ?? collectDenials(failure?.payload) ?? [];

  return {
    error,
    permissionDenials: dedupe([...fatal, ...deniedCalls.flatMap((call) => call.denials)]),
    deniedCalls,
    substitutedCalls,
    problems,
  };
}

/**
 * `model_substituted` is the backend's own verdict; the requested/ran pair is
 * the fallback for a payload that carries the names but not the flag.
 */
function readSubstitution(
  payload: Record<string, unknown>,
): Omit<SubstitutedCall, "seq" | "role" | "round"> | null {
  const telemetry = callTelemetry(payload);
  const requested = stringOrNull(telemetry.model_requested);
  const ran = stringOrNull(telemetry.model_ran);
  const flagged = telemetry.model_substituted === true;
  if (!flagged && !(requested && ran && requested !== ran)) return null;
  return { requested, ran, belowFloor: telemetry.model_below_floor === true };
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function collectDenials(value: unknown): unknown[] | null {
  if (!value || typeof value !== "object") return null;
  const denials = (value as Record<string, unknown>).permission_denials;
  return Array.isArray(denials) && denials.length > 0 ? denials : null;
}

/**
 * The run's failure payload usually repeats the denial that caused it, so the
 * same refusal arrives twice. Identity is by value, not reference.
 */
function dedupe(values: unknown[]): unknown[] {
  const seen = new Set<string>();
  return values.filter((value) => {
    let key: string;
    try {
      key = JSON.stringify(value) ?? String(value);
    } catch {
      return true;
    }
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Pretty-prints an error payload for the forensics block. */
export function formatPayload(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

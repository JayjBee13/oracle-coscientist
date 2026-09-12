import { describe, expect, it } from "vitest";

import type { RunEvent } from "../api/types";
import { agoIso, makeEvent } from "../test/runFixtures";
import { SILENT_MS, STALE_MS, deriveForensics, deriveTrust } from "./trust";

const NOW = Date.UTC(2026, 7, 1, 12, 0, 0);

function at(offsetSeconds: number): string {
  return new Date(NOW + offsetSeconds * 1000).toISOString();
}

function trustFor(
  events: RunEvent[],
  overrides: Partial<Parameters<typeof deriveTrust>[0]> = {},
) {
  return deriveTrust({
    events,
    lifecycle: "running",
    now: NOW,
    callsUsed: 20,
    budgetCalls: 60,
    roundsTarget: 5,
    roundsCompleted: 2,
    ...overrides,
  });
}

describe("what is in flight", () => {
  it("finds nothing when no call has started", () => {
    const trust = trustFor([makeEvent({ seq: 1, type: "round_started", ts: at(-10) })]);
    expect(trust.inCall).toBeNull();
    expect(trust.inFlightCount).toBe(0);
  });

  it("reports the open call and how long it has been open", () => {
    const trust = trustFor([
      makeEvent({
        seq: 1,
        type: "call_started",
        ts: at(-47),
        payload: { role: "reflection", model: "claude-sonnet-5", round: 2 },
      }),
    ]);
    expect(trust.inCall?.role).toBe("reflection");
    expect(trust.inCall?.model).toBe("claude-sonnet-5");
    expect(trust.inCall?.elapsedMs).toBe(47_000);
  });

  it("closes a call against a finish for the same role", () => {
    const trust = trustFor([
      makeEvent({
        seq: 1,
        type: "call_started",
        ts: at(-40),
        payload: { role: "reflection", model: "m", round: 2 },
      }),
      makeEvent({
        seq: 2,
        type: "call_started",
        ts: at(-30),
        payload: { role: "ranking", model: "m", round: 2 },
      }),
      makeEvent({
        seq: 3,
        type: "call_finished",
        ts: at(-20),
        payload: { role: "reflection", model: "m", round: 2, ok: true },
      }),
    ]);
    expect(trust.inFlightCount).toBe(1);
    expect(trust.inCall?.role).toBe("ranking");
  });

  it("refuses to name a model for a run that calls none", () => {
    const trust = trustFor([
      makeEvent({
        seq: 1,
        type: "call_started",
        ts: at(-5),
        payload: { role: "generation", harness: "demo", round: 1 },
      }),
    ]);
    expect(trust.inCall?.noModelCall).toBe(true);
    expect(trust.inCall?.model).toBeNull();
  });

  it("keeps a call that never reported finishing visible", () => {
    const trust = trustFor([
      makeEvent({
        seq: 1,
        type: "call_started",
        ts: at(-600),
        payload: { role: "generation", model: "m", round: 1 },
      }),
    ]);
    expect(trust.inCall?.role).toBe("generation");
  });
});

describe("how quiet it has been", () => {
  it("stays neutral while events are arriving", () => {
    const trust = trustFor([makeEvent({ seq: 1, type: "round_started", ts: at(-10) })]);
    expect(trust.lastActivityTone).toBe("neutral");
    expect(trust.stale).toBe(false);
  });

  it("turns amber past ninety seconds", () => {
    const trust = trustFor([
      makeEvent({ seq: 1, type: "round_started", ts: at(-(STALE_MS / 1000 + 10)) }),
    ]);
    expect(trust.lastActivityTone).toBe("caution");
    expect(trust.stale).toBe(true);
  });

  it("turns red past seven minutes", () => {
    const trust = trustFor([
      makeEvent({ seq: 1, type: "round_started", ts: at(-(SILENT_MS / 1000 + 10)) }),
    ]);
    expect(trust.lastActivityTone).toBe("danger");
  });

  it("does not scold a finished run for being quiet", () => {
    const trust = trustFor(
      [makeEvent({ seq: 1, type: "run_finished", ts: at(-86_400) })],
      { lifecycle: "completed" },
    );
    expect(trust.lastActivityTone).toBe("neutral");
    expect(trust.stale).toBe(false);
  });
});

describe("the estimate", () => {
  const calls = (count: number): RunEvent[] =>
    Array.from({ length: count }, (_, index) => [
      makeEvent({
        seq: index * 2 + 1,
        type: "call_started",
        ts: at(-1000 + index * 60),
        payload: { role: "ranking", model: "m", round: 2 },
      }),
      makeEvent({
        seq: index * 2 + 2,
        type: "call_finished",
        ts: at(-1000 + index * 60 + 30),
        payload: { role: "ranking", model: "m", round: 2, ok: true, duration_ms: 30_000 },
      }),
    ]).flat();

  it("says nothing before there is evidence", () => {
    expect(trustFor(calls(2)).etaMs).toBeNull();
  });

  it("says nothing before a round has completed", () => {
    expect(trustFor(calls(5), { roundsCompleted: 0 }).etaMs).toBeNull();
  });

  it("multiplies the median call by the calls still expected", () => {
    const trust = trustFor(calls(5));
    // 20 calls over 2 rounds = 10 per round, 3 rounds left = 30 calls at 30s.
    expect(trust.medianCallMs).toBe(30_000);
    expect(trust.etaMs).toBe(30 * 30_000);
  });

  it("never promises more calls than the budget allows", () => {
    const trust = trustFor(calls(5), { callsUsed: 55, budgetCalls: 60 });
    expect(trust.etaMs).toBe(5 * 30_000);
  });

  it("says nothing once the run has stopped", () => {
    expect(trustFor(calls(5), { lifecycle: "stopped" }).etaMs).toBeNull();
  });
});

describe("forensics", () => {
  it("returns the failure payload verbatim and the denials inside it", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        type: "role_degraded",
        payload: { role: "ranking", reason: "x" },
      }),
      makeEvent({
        seq: 2,
        type: "run_failed",
        payload: {
          error: {
            message: "WebSearch denied",
            permission_denials: [{ tool: "WebSearch" }],
          },
        },
      }),
    ]);

    expect(forensics.permissionDenials).toEqual([{ tool: "WebSearch" }]);
    expect(forensics.problems).toHaveLength(2);
  });

  it("attributes a denial to the call that hit it", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        round: 2,
        type: "call_finished",
        payload: {
          role: "generation",
          ok: false,
          telemetry: { permission_denials: [{ tool: "WebSearch" }], num_turns: 1 },
        },
      }),
    ]);

    expect(forensics.deniedCalls).toEqual([
      { seq: 1, role: "generation", round: 2, denials: [{ tool: "WebSearch" }] },
    ]);
    expect(forensics.permissionDenials).toEqual([{ tool: "WebSearch" }]);
    // A denied call is a problem even though `call_finished` is not a problem type.
    expect(forensics.problems.map((event) => event.seq)).toEqual([1]);
  });

  it("reports the same denial once when the failure payload repeats it", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        round: 1,
        type: "call_finished",
        payload: {
          role: "reflection",
          telemetry: { permission_denials: [{ tool: "WebSearch" }] },
        },
      }),
      makeEvent({
        seq: 2,
        type: "run_failed",
        payload: { error: { permission_denials: [{ tool: "WebSearch" }] } },
      }),
    ]);

    expect(forensics.permissionDenials).toEqual([{ tool: "WebSearch" }]);
    expect(forensics.deniedCalls).toHaveLength(1);
    expect(forensics.deniedCalls[0].role).toBe("reflection");
  });

  it("keeps distinct denials from different calls apart", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        round: 1,
        type: "call_finished",
        payload: {
          role: "generation",
          telemetry: { permission_denials: [{ tool: "A" }] },
        },
      }),
      makeEvent({
        seq: 2,
        round: 1,
        type: "call_finished",
        payload: {
          role: "evolution",
          telemetry: { permission_denials: [{ tool: "B" }] },
        },
      }),
    ]);

    expect(forensics.deniedCalls.map((call) => call.role)).toEqual([
      "generation",
      "evolution",
    ]);
    expect(forensics.permissionDenials).toEqual([{ tool: "A" }, { tool: "B" }]);
  });

  it("ignores a call that finished cleanly", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        type: "call_finished",
        payload: { role: "ranking", ok: true, telemetry: { permission_denials: [] } },
      }),
    ]);
    expect(forensics.deniedCalls).toHaveLength(0);
    expect(forensics.permissionDenials).toHaveLength(0);
    expect(forensics.problems).toHaveLength(0);
  });

  it("finds nothing to report on a clean run", () => {
    const forensics = deriveForensics([
      makeEvent({ seq: 1, type: "round_completed", ts: agoIso(10) }),
    ]);
    expect(forensics.error).toBeNull();
    expect(forensics.deniedCalls).toHaveLength(0);
    expect(forensics.substitutedCalls).toHaveLength(0);
    expect(forensics.problems).toHaveLength(0);
  });
});

/**
 * A substitution fails nothing: the call returns, the schema validates, the
 * round completes. It is only ever visible if somebody reads the telemetry.
 */
describe("a model the run did not ask for", () => {
  const REQUESTED = "model-asked-for";
  const RAN = "model-that-answered";

  it("attributes the swap to the call, and counts it as a problem", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 4,
        round: 3,
        type: "call_finished",
        payload: {
          role: "ranking",
          ok: true,
          telemetry: {
            model_requested: REQUESTED,
            model_ran: RAN,
            model_substituted: true,
            model_below_floor: true,
          },
        },
      }),
    ]);

    expect(forensics.substitutedCalls).toEqual([
      {
        seq: 4,
        role: "ranking",
        round: 3,
        requested: REQUESTED,
        ran: RAN,
        belowFloor: true,
      },
    ]);
    expect(forensics.problems.map((event) => event.seq)).toEqual([4]);
  });

  it("notices the swap from the names alone when the flag is absent", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        type: "call_finished",
        payload: { role: "evolution", model_requested: REQUESTED, model_ran: RAN },
      }),
    ]);
    expect(forensics.substitutedCalls).toHaveLength(1);
    expect(forensics.substitutedCalls[0].belowFloor).toBe(false);
  });

  it("stays quiet when the model that ran is the one that was asked for", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 1,
        type: "call_finished",
        payload: {
          role: "evolution",
          ok: true,
          telemetry: { model_requested: REQUESTED, model_ran: REQUESTED },
        },
      }),
    ]);
    expect(forensics.substitutedCalls).toHaveLength(0);
    expect(forensics.problems).toHaveLength(0);
  });

  it("reports a denial and a swap on one call without double-listing it", () => {
    const forensics = deriveForensics([
      makeEvent({
        seq: 2,
        round: 1,
        type: "call_finished",
        payload: {
          role: "generation",
          telemetry: {
            permission_denials: [{ tool: "WebSearch" }],
            model_requested: REQUESTED,
            model_ran: RAN,
          },
        },
      }),
    ]);

    expect(forensics.deniedCalls).toHaveLength(1);
    expect(forensics.substitutedCalls).toHaveLength(1);
    expect(forensics.problems).toHaveLength(1);
  });
});

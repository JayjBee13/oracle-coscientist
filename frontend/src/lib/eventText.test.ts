import { describe, expect, it } from "vitest";

import { makeEvent } from "../test/runFixtures";
import {
  NO_MODEL_CALL,
  callModelText,
  eventBody,
  substitutionText,
  withoutFalseModelClaims,
} from "./eventText";

/**
 * The model names below are deliberately nonsense. Nothing in the UI may depend
 * on a real model id: the table moves, and a test that pins one is wrong within
 * the week.
 */
const REQUESTED = "model-asked-for";
const RAN = "model-that-answered";

describe("the line that records a run being extended", () => {
  /**
   * This is the audit record for the one thing allowed to change a launched
   * run's config, so the sentence has to carry both sides of both numbers.
   * "More rounds added" on its own answers none of the questions somebody
   * reading the log a week later is asking.
   */
  const extended = makeEvent({
    seq: 9,
    type: "run_extended",
    payload: {
      added_rounds: 2,
      rounds_target: 3,
      previous_rounds_target: 1,
      budget_calls: 220,
      previous_budget_calls: 60,
      previous_lifecycle: "completed",
    },
  });

  it("says how many rounds, from what target, and what the budget became", () => {
    expect(eventBody(extended)).toBe("+2 rounds · target 1 → 3 · budget 60 → 220 calls");
  });

  it("leaves the budget out when it was not raised", () => {
    const sameBudget = makeEvent({
      ...extended,
      payload: { ...extended.payload, budget_calls: 60 },
    });
    expect(eventBody(sameBudget)).toBe("+2 rounds · target 1 → 3");
  });

  it("records a cost ceiling that was removed as none, never as $0.00", () => {
    // The line somebody comes looking for a year later: this run was capped in
    // dollars, that cap is what stopped it, and continuing took the cap off.
    // "$5.00 → $0.00" would say the exact opposite of what happened.
    const uncapped = makeEvent({
      ...extended,
      payload: {
        ...extended.payload,
        budget_calls: 60,
        budget_usd: null,
        previous_budget_usd: 5,
      },
    });
    expect(eventBody(uncapped)).toBe(
      "+2 rounds · target 1 → 3 · cost ceiling $5.00 → none",
    );
  });

  it("leaves the cost ceiling out when it did not move", () => {
    const same = makeEvent({
      ...extended,
      payload: {
        ...extended.payload,
        budget_calls: 60,
        budget_usd: null,
        previous_budget_usd: null,
      },
    });
    expect(eventBody(same)).toBe("+2 rounds · target 1 → 3");
  });

  it("does not pluralise a single round", () => {
    const one = makeEvent({
      ...extended,
      payload: {
        ...extended.payload,
        added_rounds: 1,
        rounds_target: 2,
        budget_calls: 60,
      },
    });
    expect(eventBody(one)).toBe("+1 round · target 1 → 2");
  });

  it("still says something when a newer backend sends less than expected", () => {
    const bare = makeEvent({ ...extended, payload: {} });
    expect(eventBody(bare)).toBe("more rounds");
  });
});

describe("a demo run names no model", () => {
  const started = makeEvent({
    seq: 1,
    type: "call_started",
    payload: { role: "generation", model: REQUESTED, round: 1 },
  });

  it("leaves a real run's events exactly as they arrived", () => {
    const events = [started];
    expect(withoutFalseModelClaims(events, "claude")).toBe(events);
    expect(eventBody(started)).toBe(`Generation · ${REQUESTED}`);
  });

  it("strips the model a demo run never called", () => {
    const [event] = withoutFalseModelClaims([started], "demo");
    expect(event.payload?.model).toBeUndefined();
    expect(eventBody(event)).toBe(`Generation · ${NO_MODEL_CALL}`);
  });

  it("says the same thing when the call finishes", () => {
    const [event] = withoutFalseModelClaims(
      [
        makeEvent({
          seq: 2,
          type: "call_finished",
          payload: { role: "generation", model: REQUESTED, round: 1, ok: true },
        }),
      ],
      "demo",
    );
    expect(eventBody(event)).toContain(NO_MODEL_CALL);
    expect(eventBody(event)).not.toContain(REQUESTED);
  });

  it("leaves events that are not calls alone", () => {
    const [event] = withoutFalseModelClaims(
      [makeEvent({ seq: 3, type: "round_started", payload: { round: 2 } })],
      "demo",
    );
    expect(eventBody(event)).toBe("Round 2");
  });

  it("still admits when a real run did not record its model", () => {
    expect(callModelText({ role: "ranking" })).toBe("model not recorded");
  });
});

describe("a call that ran on another model", () => {
  it("names both sides from the telemetry", () => {
    expect(
      substitutionText({
        telemetry: {
          model_requested: REQUESTED,
          model_ran: RAN,
          model_substituted: true,
        },
      }),
    ).toBe(`ran ${RAN}, not ${REQUESTED}`);
  });

  it("says when what ran was below the floor", () => {
    expect(
      substitutionText({
        telemetry: {
          model_requested: REQUESTED,
          model_ran: RAN,
          model_substituted: true,
          model_below_floor: true,
        },
      }),
    ).toContain("below the model floor");
  });

  it("reads telemetry sitting directly on the payload too", () => {
    expect(substitutionText({ model_requested: REQUESTED, model_ran: RAN })).toBe(
      `ran ${RAN}, not ${REQUESTED}`,
    );
  });

  it("says nothing when the model that ran is the one asked for", () => {
    expect(
      substitutionText({
        telemetry: { model_requested: REQUESTED, model_ran: REQUESTED },
      }),
    ).toBeNull();
  });

  it("reaches the event line, alongside the denials on the same call", () => {
    const body = eventBody(
      makeEvent({
        seq: 1,
        type: "call_finished",
        payload: {
          role: "ranking",
          ok: true,
          duration_ms: 12_000,
          telemetry: {
            model_requested: REQUESTED,
            model_ran: RAN,
            model_substituted: true,
            permission_denials: [{ tool: "WebSearch" }],
          },
        },
      }),
    );

    expect(body).toContain("Tournament");
    expect(body).toContain(`ran ${RAN}, not ${REQUESTED}`);
    expect(body).toContain("1 tool denial");
  });
});

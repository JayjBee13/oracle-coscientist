import { describe, expect, it } from "vitest";

import {
  CONTROL_ACTIONS,
  EVENT_TYPES,
  GROUNDING_DEPTHS,
  HARNESSES,
  HYPOTHESIS_STATUSES,
  LIFECYCLES,
  MATCH_STATUSES,
  MODEL_TIERS,
  NOVELTY_LEVELS,
  OPERATORS,
  ROLES,
  RUN_SOURCES,
  VERDICTS,
  WORKSHOP_STATES,
} from "../api/types";
import type { ControlAction } from "../api/types";
import {
  controlActionsFor,
  describeConnection,
  describeControlAction,
  describeEffort,
  describeEventType,
  describeGroundingDepth,
  describeHarness,
  describeHypothesisStatus,
  describeLifecycle,
  describeMatchStatus,
  describeModelTier,
  describeNovelty,
  describeOperator,
  describeProvider,
  describeRole,
  describeRunStatus,
  describeSource,
  describeVerdict,
  describeWorkshopState,
  isControlAllowed,
} from "./status";

const TONES = ["neutral", "accent", "go", "caution", "danger", "info"];

/**
 * The lint-adjacent guarantee: every enum the backend can send has a human
 * label. If someone adds a lifecycle value or an event type to `api/types.ts`
 * without a mapping here, this file fails before the raw value can reach a
 * screen.
 */
describe("every enum has a label", () => {
  const cases: [
    string,
    readonly string[],
    (value: string) => { label: string; tone: string },
  ][] = [
    ["lifecycle", LIFECYCLES, describeLifecycle],
    ["event type", EVENT_TYPES, describeEventType],
    ["hypothesis status", HYPOTHESIS_STATUSES, describeHypothesisStatus],
    ["match status", MATCH_STATUSES, describeMatchStatus],
    ["verdict", VERDICTS, describeVerdict],
    ["novelty level", NOVELTY_LEVELS, describeNovelty],
    ["operator", OPERATORS, describeOperator],
    ["workshop state", WORKSHOP_STATES, describeWorkshopState],
    ["harness", HARNESSES, describeHarness],
    ["run source", RUN_SOURCES, describeSource],
    ["grounding depth", GROUNDING_DEPTHS, describeGroundingDepth],
    ["model tier", MODEL_TIERS, describeModelTier],
    [
      "control action",
      CONTROL_ACTIONS,
      (value: string) => describeControlAction(value as ControlAction),
    ],
  ];

  for (const [name, values, describeFn] of cases) {
    it(`covers every ${name}`, () => {
      for (const value of values) {
        const result = describeFn(value);
        expect(result.label, `${name} "${value}" has no label`).toBeTruthy();
        expect(result.label).not.toBe(value);
        expect(TONES).toContain(result.tone);
      }
    });
  }

  it("covers every role", () => {
    for (const role of ROLES) {
      const label = describeRole(role);
      expect(label).toBeTruthy();
      expect(label).not.toBe(role);
    }
  });

  it("covers every connection state", () => {
    for (const state of [
      "idle",
      "connecting",
      "live",
      "reconnecting",
      "offline",
    ] as const) {
      const result = describeConnection(state);
      expect(result.label).toBeTruthy();
      expect(TONES).toContain(result.tone);
    }
  });

  it("never leaves an underscore on screen for a value it has not seen", () => {
    expect(describeLifecycle("brand_new_state").label).toBe("Brand new state");
    expect(describeEventType("something_novel").label).toBe("Something novel");
  });
});

/**
 * Two vendors run the loop now, and the words for both of them live here for
 * the same reason every other enum does: the moment a screen writes "Openai" or
 * "Xhigh" itself, there is a second vocabulary to keep in step with this one.
 */
describe("providers, tiers and thinking effort", () => {
  it("spells the vendor the way the vendor spells it", () => {
    // `humanize` would produce "Openai", which is the tell of a UI printing an
    // identifier rather than a name.
    expect(describeProvider("openai").label).toBe("OpenAI");
    expect(describeProvider("anthropic").label).toBe("Anthropic");
    for (const provider of ["openai", "anthropic"]) {
      expect(TONES).toContain(describeProvider(provider).tone);
    }
  });

  it("humanises a vendor it has never met rather than showing nothing", () => {
    expect(describeProvider("brand_new_vendor").label).toBe("Brand new vendor");
    expect(describeProvider("brand_new_vendor").tone).toBe("neutral");
  });

  it("names every current tier, the speed preset included", () => {
    // Read off the generated vocabulary rather than listed here, so a tier added
    // on the backend fails this test instead of quietly rendering as a raw id in
    // the top bar. `low` is the one this caught: it reached the frontend as a
    // fourth `MODEL_TIER_VALUES` entry with no label of its own.
    for (const tier of MODEL_TIERS) {
      const described = describeModelTier(tier);
      expect(described.label).toBeTruthy();
      expect(described.label).not.toBe(tier);
      expect(TONES).toContain(described.tone);
    }
    expect(describeModelTier("max").label).toBe("Max");
  });

  it("still labels the tier names runs were launched under before the rename", () => {
    for (const legacy of ["balanced", "quality", "standard", "maximum"]) {
      expect(describeModelTier(legacy).label).not.toBe(legacy);
    }
  });

  it("says Extra high rather than Xhigh", () => {
    // The rung a Max run spends most of its calls at. `humanize` renders it
    // "Xhigh", which reads as a typo in the one row people look at.
    expect(describeEffort("xhigh")).toBe("Extra high");
    expect(describeEffort("max")).toBe("Maximum");
  });

  it("covers both providers' ladders, including the rungs only one of them has", () => {
    for (const effort of ["none", "minimal", "low", "medium", "high", "xhigh", "max"]) {
      expect(describeEffort(effort)).toBeTruthy();
      expect(describeEffort(effort)).not.toBe(effort);
    }
  });

  it("humanises an effort it has not seen", () => {
    expect(describeEffort("ultra_deep")).toBe("Ultra deep");
  });
});

describe("run status", () => {
  it("uses one vocabulary for imported history", () => {
    const status = describeRunStatus({ lifecycle: "completed", source: "imported" });
    expect(status.label).toBe("Completed · Imported");
    expect(status.tone).toBe("neutral");
  });

  it("leaves app runs alone", () => {
    expect(describeRunStatus({ lifecycle: "completed", source: "app" }).label).toBe(
      "Completed",
    );
  });

  it("explains why a clean run ended in its status tooltip", () => {
    const status = describeRunStatus({
      lifecycle: "completed",
      source: "app",
      has_overview: true,
      ended_reason: "budget_calls",
    });

    expect(status.label).toBe("Completed");
    expect(status.hint).toContain("model-call ceiling");
  });

  it("says what a pausing run is actually doing", () => {
    expect(describeLifecycle("pausing").label).toBe("Waiting to pause");
    expect(describeLifecycle("pausing").mark).toBe("transitional");
  });

  /**
   * Run c4566ed2 lost three whole steps, produced no report and blew its cost
   * ceiling, and its header chip read "Completed", tone `go`, hint "Ran to the
   * end and wrote a report." — because the chip was a pure function of
   * `lifecycle` and nothing about failure reached this file at all.
   */
  it("qualifies a run that lost work rather than calling it an unqualified success", () => {
    const status = describeRunStatus({
      lifecycle: "completed",
      source: "app",
      lost_steps: 3,
      failed_calls: 8,
      has_overview: false,
      ended_reason: "budget_usd",
    });

    expect(status.label).toBe("Completed with losses");
    expect(status.tone).toBe("caution");
    expect(status.hint).toContain("3 steps failed");
    expect(status.hint).toContain("no report was written");
    expect(status.hint).toContain("cost ceiling");
  });

  it("does not call a run a loss when the retries got the work done", () => {
    const status = describeRunStatus({
      lifecycle: "completed",
      source: "app",
      lost_steps: 0,
      failed_calls: 2,
      has_overview: true,
    });

    expect(status.label).toBe("Completed");
    expect(status.hint).toContain("2 calls failed and retried");
  });

  it("leaves the mark alone so lane filtering and control gating are untouched", () => {
    const clean = describeRunStatus({ lifecycle: "completed", source: "app" });
    const lossy = describeRunStatus({
      lifecycle: "completed",
      source: "app",
      lost_steps: 1,
      has_overview: true,
    });

    expect(lossy.mark).toBe(clean.mark);
  });

  it("says nothing about losses for a run that is still going", () => {
    const status = describeRunStatus({
      lifecycle: "running",
      source: "app",
      has_overview: false,
    });

    expect(status.label).toBe("Running");
    expect(status.tone).toBe("go");
  });
});

describe("control enablement (C4 action table)", () => {
  it("matches the table exactly", () => {
    expect(controlActionsFor("running").sort()).toEqual(
      ["finish", "force_stop", "pause", "stop"].sort(),
    );
    expect(controlActionsFor("paused").sort()).toEqual(
      ["finish", "force_stop", "resume", "stop"].sort(),
    );
    expect(controlActionsFor("pausing").sort()).toEqual(["force_stop", "stop"].sort());
    expect(controlActionsFor("queued")).toEqual(["force_stop"]);
    expect(controlActionsFor("stopping")).toEqual(["force_stop"]);
    expect(controlActionsFor("finishing")).toEqual(["force_stop"]);
  });

  it("offers only more rounds once a run has ended cleanly", () => {
    expect(controlActionsFor("completed")).toEqual(["continue"]);
    expect(controlActionsFor("stopped")).toEqual(["continue"]);
  });

  it("offers nothing on an ending nobody has diagnosed", () => {
    // A failed run stopped for a reason still sitting in `runs.error` and a lost
    // one died without saying anything. Neither wrote a report, so extending
    // either would make this button mean "resurrect" half the time.
    for (const lifecycle of ["failed", "lost"]) {
      expect(controlActionsFor(lifecycle)).toEqual([]);
    }
  });

  it("offers nothing at all on an imported run, whatever its lifecycle", () => {
    // Every imported run is `completed`. It carries no model table, so a Continue
    // button on one could only ever error against a historical record.
    for (const lifecycle of LIFECYCLES) {
      expect(controlActionsFor(lifecycle, "imported")).toEqual([]);
    }
    expect(isControlAllowed("completed", "continue", "imported")).toBe(false);
    expect(isControlAllowed("completed", "continue", "app")).toBe(true);
  });

  it("never offers resume and continue together", () => {
    // "Carry on a run that never finished" and "give a finished one more rounds"
    // are different questions, and a bar showing both asks the scientist to tell
    // them apart from the labels alone.
    for (const lifecycle of LIFECYCLES) {
      const offered = controlActionsFor(lifecycle);
      expect(offered.includes("resume") && offered.includes("continue")).toBe(false);
    }
  });

  it("guards each action individually", () => {
    expect(isControlAllowed("running", "pause")).toBe(true);
    expect(isControlAllowed("running", "resume")).toBe(false);
    expect(isControlAllowed("completed", "force_stop")).toBe(false);
  });

  it("demands confirmation for anything that loses work", () => {
    expect(describeControlAction("stop").confirm).toBeTruthy();
    expect(describeControlAction("force_stop").destructive).toBe(true);
    expect(describeControlAction("pause").confirm).toBe("");
  });
});

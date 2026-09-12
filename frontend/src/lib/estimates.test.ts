import { describe, expect, it } from "vitest";

import { MODEL_TIERS } from "../api/types";
import { makeRunConfig } from "../test/fixtures";
import {
  BUDGET_HEADROOM,
  DEFAULT_MODEL_TIER,
  PRESETS,
  USD_PER_CALL_ESTIMATE,
  estimateCalls,
  estimateMinutes,
  estimateUsd,
  formatMinutesRange,
  formatUsdRange,
  getPreset,
  matchPreset,
  presetConfig,
  suggestedBudgetCalls,
} from "./estimates";

/**
 * Parity fixture. Produced by running `engine.core.estimate_calls`,
 * `suggested_budget_calls` and `estimate_minutes` against the same shapes —
 * if the backend's formula changes, these fail here first and the wizard never
 * gets to quote a budget the engine cannot finish inside.
 */
const BACKEND_PARITY = [
  {
    name: "quick",
    shape: {
      rounds: 1,
      generation_batch: 3,
      matches_per_round: 2,
      evolve_top_k: 2,
      grounding_depth: "shallow" as const,
    },
    calls: 11,
    budget: 14,
    minutes: [2, 11] as [number, number],
  },
  {
    name: "standard",
    shape: {
      rounds: 3,
      generation_batch: 6,
      matches_per_round: 4,
      evolve_top_k: 3,
      grounding_depth: "standard" as const,
    },
    calls: 53,
    budget: 67,
    minutes: [14, 52] as [number, number],
  },
  {
    name: "deep",
    shape: {
      rounds: 5,
      generation_batch: 8,
      matches_per_round: 6,
      evolve_top_k: 4,
      grounding_depth: "deep" as const,
    },
    calls: 118,
    budget: 148,
    minutes: [37, 128] as [number, number],
  },
  {
    name: "engine defaults",
    shape: {
      rounds: 5,
      generation_batch: 8,
      matches_per_round: 6,
      evolve_top_k: 3,
      grounding_depth: "standard" as const,
    },
    calls: 114,
    budget: 143,
    minutes: [28, 99] as [number, number],
  },
];

describe("estimateCalls", () => {
  it.each(BACKEND_PARITY)("matches the backend for $name", (fixture) => {
    expect(estimateCalls(fixture.shape)).toBe(fixture.calls);
    expect(suggestedBudgetCalls(fixture.shape)).toBe(fixture.budget);
    expect(estimateMinutes(fixture.shape)).toEqual(fixture.minutes);
  });

  it("follows C3's step list: 2 + R·(ceil(B/3) + B + M + 3) + (R−1)·V", () => {
    const shape = {
      rounds: 4,
      generation_batch: 7,
      matches_per_round: 5,
      evolve_top_k: 2,
    };
    const perRound = Math.ceil(7 / 3) + 7 + 5 + 3;
    expect(estimateCalls(shape)).toBe(2 + 4 * perRound + 3 * 2);
  });

  it("still reserves the overview calls for a zero-round run", () => {
    expect(
      estimateCalls({
        rounds: 0,
        generation_batch: 6,
        matches_per_round: 4,
        evolve_top_k: 3,
      }),
    ).toBe(2);
  });
});

describe("presets", () => {
  it("computes every budget from the formula rather than hard-coding it", () => {
    for (const preset of PRESETS) {
      const config = presetConfig(preset);
      expect(config.budget_calls).toBe(suggestedBudgetCalls(preset.shape));
      expect(config.budget_calls).toBe(
        Math.ceil(estimateCalls(preset.shape) * BUDGET_HEADROOM),
      );
    }
  });

  it("gives every preset a budget it can actually finish inside", () => {
    // The old Quick preset offered 8 calls for a round that needs 12.
    for (const preset of PRESETS) {
      expect(presetConfig(preset).budget_calls).toBeGreaterThanOrEqual(
        estimateCalls(preset.shape),
      );
    }
  });

  it("uses the shapes the plan specifies", () => {
    expect(getPreset("quick").shape).toMatchObject({
      rounds: 1,
      generation_batch: 3,
      matches_per_round: 2,
    });
    expect(getPreset("standard").shape).toMatchObject({
      rounds: 3,
      generation_batch: 6,
      matches_per_round: 4,
    });
    expect(getPreset("deep").shape).toMatchObject({
      rounds: 5,
      generation_batch: 8,
      matches_per_round: 6,
    });
  });

  it("recognises a preset it produced, and calls a tuned config custom", () => {
    for (const preset of PRESETS) {
      expect(matchPreset(makeRunConfig(presetConfig(preset)))).toBe(preset.name);
    }
    const tuned = makeRunConfig({ ...presetConfig(getPreset("standard")), rounds: 4 });
    expect(matchPreset(tuned)).toBeNull();
  });
});

describe("cost and duration", () => {
  it("scales the estimate with the number of calls and the tier", () => {
    const shape = getPreset("standard").shape;
    // The effort-only tiers are a ladder: `MODEL_TIERS` declares them
    // hardest-thinking first and they run the same models, so the estimate has to
    // fall along them. Compared pairwise rather than end to end: a tier landing
    // in the middle with a wrong bracket passes an ends-only check.
    //
    // `low` is deliberately not in this list — it pins a different model on every
    // role, so it is not a rung of this ladder and the test below is its own.
    const ladder = MODEL_TIERS.filter((tier) => tier !== "low");
    const brackets = ladder.map((tier) => estimateUsd(shape, { tier }));
    for (const bracket of brackets) expect(bracket[0]).toBeLessThan(bracket[1]);
    for (let index = 1; index < brackets.length; index += 1) {
      expect(brackets[index][0]).toBeLessThan(brackets[index - 1][0]);
      expect(brackets[index][1]).toBeLessThan(brackets[index - 1][1]);
    }
    // And the other half of the same claim: the same tier over more calls costs
    // more, which is what makes these numbers an estimate rather than a label.
    const smaller = estimateUsd({ ...shape, rounds: 1 }, { tier: ladder[0] });
    expect(smaller[0]).toBeLessThan(brackets[0][0]);
  });

  it("prices low as a speed tier rather than as the cheapest one", () => {
    // `low` sits outside the ladder above, by design and not by omission. It
    // pins `gpt-5.6-luna` on **every** role at high effort, and Luna's published
    // bracket is $0.25–0.667 — identical to opus-5's, because it is bracketed at
    // class parity with it. So a Low run costs per call what the cheapest effort
    // tier costs and buys wall clock rather than money.
    //
    // Asserted separately and explicitly, because the only way to keep a
    // strict pairwise descent across all four tiers would be to write a `low`
    // bracket chosen to make the assertion pass rather than derived from the
    // published figures.
    const shape = getPreset("standard").shape;
    const low = estimateUsd(shape, { tier: "low" });
    expect(low).toEqual(estimateUsd(shape, { tier: "med" }));
    for (const dearer of ["max", "high"] as const) {
      const bracket = estimateUsd(shape, { tier: dearer });
      expect(low[0]).toBeLessThan(bracket[0]);
      expect(low[1]).toBeLessThan(bracket[1]);
    }
    // Every tier the contract publishes needs a bracket of its own. One that
    // fell through to the default would be priced as the *dearest* tier, which
    // is exactly what `low` was quoted at before it got a bracket here.
    for (const tier of MODEL_TIERS) {
      expect(USD_PER_CALL_ESTIMATE[tier]).toBeDefined();
    }
  });

  it("charges a demo run nothing at all", () => {
    expect(estimateUsd(getPreset("deep").shape, { demo: true })).toEqual([0, 0]);
    expect(formatUsdRange([0, 0])).toBe("No cost");
  });

  it("formats ranges without ever printing a bare $0.00", () => {
    expect(formatUsdRange([1.06, 3.18])).toBe("$1.06 – $3.18");
    expect(formatUsdRange([5.9, 17.7])).toBe("$5.90 – $18");
    expect(formatMinutesRange([14, 52])).toBe("14 – 52 min");
    expect(formatMinutesRange([9, 9])).toBe("9 min");
  });
});

describe("the tier an estimate is taken at", () => {
  it("defaults to the tier a bare API launch would get", () => {
    // The engine's own `DEFAULT_MODEL_TIER`, which is what a launch that names
    // no tier resolves to and what the stored default starts at. If someone
    // moves it and not this, the wizard quietly draws its first estimate at the
    // wrong tier before `/api/capabilities` answers.
    expect(DEFAULT_MODEL_TIER).toBe("max");
    expect(MODEL_TIERS).toContain(DEFAULT_MODEL_TIER);
    // And it is the hardest-thinking one: a default that silently thinks less
    // than a bare API launch would is the substitution this app refuses to make.
    expect(MODEL_TIERS[0]).toBe(DEFAULT_MODEL_TIER);
  });

  it("falls back to the default rather than throwing on a retired tier", () => {
    // A run stored before a tier was retired carries its name. Destructuring a
    // missing table entry used to take the whole wizard down with the clone.
    const shape = getPreset("standard").shape;
    const retired = estimateUsd(shape, { tier: "balanced" });
    expect(retired).toEqual(estimateUsd(shape, { tier: DEFAULT_MODEL_TIER }));
    expect(estimateUsd(shape, { tier: null })).toEqual(retired);
  });

  it("keeps a cost ceiling out of every preset", () => {
    // Calls are the governor. A preset that quietly set a dollar ceiling meant
    // every run the wizard launched carried one nobody asked for.
    for (const preset of PRESETS) {
      expect(Object.keys(presetConfig(preset))).not.toContain("budget_usd");
    }
  });
});

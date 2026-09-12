import { describe, expect, it } from "vitest";

import type { CompareDelta, HypothesisRow } from "../api/types";
import {
  formatChange,
  headlineVerdict,
  rankWithinRun,
  readDeltas,
  readGraft,
} from "./compare";

function makeHypothesis(overrides: Partial<HypothesisRow> = {}): HypothesisRow {
  return {
    id: "hyp-1",
    hid: "h001",
    title: "Plasmonic nitrogen fixation",
    status: "active",
    elo: 1200,
    matches: 4,
    wins: 2,
    cluster: null,
    duplicate_of: null,
    parent_ids: [],
    operator: null,
    created_round: 1,
    source: "agent",
    novelty_level: null,
    ...overrides,
  };
}

describe("readDeltas", () => {
  const deltas: CompareDelta[] = [
    {
      metric: "Active hypotheses",
      base: 6,
      challenger: 9,
      direction_hint: "up",
    },
    { metric: "Model calls", base: 40, challenger: 61, direction_hint: "down" },
    { metric: "Rounds", base: 3, challenger: 5, direction_hint: "flat" },
    { metric: "Elo spread", base: 80, challenger: 80, direction_hint: "up" },
  ];

  it("reads more-is-better and less-is-better in opposite directions", () => {
    const [active, calls, rounds, spread] = readDeltas(deltas);

    expect(active.change).toBe(3);
    expect(active.verdict).toBe("better");
    expect(active.tone).toBe("go");
    expect(active.arrow).toBe("▲");

    // More calls for the same work is worse, even though the number went up.
    expect(calls.change).toBe(21);
    expect(calls.verdict).toBe("worse");
    expect(calls.arrow).toBe("▲");
    expect(calls.tone).toBe("danger");

    // A descriptive measure gets no verdict at all.
    expect(rounds.verdict).toBe("changed");
    expect(rounds.label).toBe("different");

    expect(spread.change).toBe(0);
    expect(spread.verdict).toBe("same");
  });

  it("labels every change in words, so colour is never the only signal", () => {
    for (const view of readDeltas(deltas)) {
      expect(view.label).toMatch(/better|worse|different|no change/);
    }
  });

  it("formats a signed change without a bare minus sign", () => {
    expect(formatChange(3)).toBe("+3");
    expect(formatChange(-1.44)).toBe("−1.4");
    expect(formatChange(0)).toBe("0");
  });
});

describe("headlineVerdict", () => {
  const better: CompareDelta = {
    metric: "Active hypotheses",
    base: 4,
    challenger: 9,
    direction_hint: "up",
  };
  const worse: CompareDelta = {
    metric: "Model calls",
    base: 40,
    challenger: 90,
    direction_hint: "down",
  };
  const flat: CompareDelta = {
    metric: "Rounds",
    base: 3,
    challenger: 5,
    direction_hint: "flat",
  };

  it("names the winner when the directional measures agree", () => {
    const verdict = headlineVerdict(readDeltas([better, flat]), { sharedPrompt: true });
    expect(verdict.headline).toMatch(/challenger came out ahead on 1 of 1/i);
    expect(verdict.detail).toMatch(/same prompt/i);
  });

  it("refuses to pick one when they disagree", () => {
    const verdict = headlineVerdict(readDeltas([better, worse]), { sharedPrompt: true });
    expect(verdict.headline).toBe("No clear winner");
  });

  it("says so when nothing with a direction moved", () => {
    const verdict = headlineVerdict(readDeltas([flat]), { sharedPrompt: false });
    expect(verdict.headline).toMatch(/nothing separates/i);
  });

  it("carries the not-the-same-prompt caveat into the verdict itself", () => {
    const verdict = headlineVerdict(readDeltas([better]), { sharedPrompt: false });
    expect(verdict.detail).toMatch(/did not start from the same prompt/i);
  });
});

describe("rankWithinRun", () => {
  it("normalises against this run's own best and worst, and nothing else", () => {
    const list = rankWithinRun([
      makeHypothesis({ hid: "h001", elo: 1400 }),
      makeHypothesis({ hid: "h002", elo: 1300 }),
      makeHypothesis({ hid: "h003", elo: 1200 }),
    ]);

    expect(list.rows.map((row) => row.hid)).toEqual(["h001", "h002", "h003"]);
    expect(list.best).toBe(1400);
    expect(list.worst).toBe(1200);
    expect(list.rows[0].share).toBe(1);
    expect(list.rows[2].share).toBeGreaterThan(0); // the last bar is still visible
    expect(list.rows[1].share).toBeLessThan(list.rows[0].share);
  });

  it("gives the same shape to a weaker run — the scales never meet", () => {
    const strong = rankWithinRun([
      makeHypothesis({ hid: "h001", elo: 1400 }),
      makeHypothesis({ hid: "h002", elo: 1200 }),
    ]);
    const weak = rankWithinRun([
      makeHypothesis({ hid: "h001", elo: 1150 }),
      makeHypothesis({ hid: "h002", elo: 1050 }),
    ]);
    // Both leaders fill their own bar: a full bar means "best in this run",
    // never "best overall". That is the whole point of not merging them.
    expect(strong.rows[0].share).toBe(1);
    expect(weak.rows[0].share).toBe(1);
  });

  it("falls back to every hypothesis when a run rejected all of them", () => {
    const list = rankWithinRun([
      makeHypothesis({ hid: "h001", status: "rejected", elo: 1200 }),
      makeHypothesis({ hid: "h002", status: "rejected", elo: 1200 }),
    ]);
    expect(list.basis).toBe("all");
    expect(list.rows).toHaveLength(2);
  });

  it("reports an empty run rather than pretending it ranked something", () => {
    const list = rankWithinRun([]);
    expect(list.basis).toBe("none");
    expect(list.rows).toEqual([]);
    expect(list.best).toBeNull();
  });

  it("keeps the list short enough to read", () => {
    const many = Array.from({ length: 25 }, (_, index) =>
      makeHypothesis({ hid: `h${index}`, elo: 1000 + index }),
    );
    expect(rankWithinRun(many).rows).toHaveLength(10);
    expect(rankWithinRun(many).total).toBe(25);
  });
});

describe("readGraft", () => {
  it("stays null for a run whose engine had no such concept", () => {
    expect(readGraft(null)).toBeNull();
    expect(readGraft(undefined)).toBeNull();
  });

  it("reads a summary field for field", () => {
    expect(readGraft({ enabled: true, fired_count: 2, collapse_events: 3 })).toEqual({
      enabled: true,
      firedCount: 2,
      collapseEvents: 3,
    });
  });
});

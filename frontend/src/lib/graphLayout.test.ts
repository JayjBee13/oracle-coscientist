import { describe, expect, it, vi } from "vitest";

import {
  hashSeed,
  layeredLayout,
  mulberry32,
  organicLayout,
  radiusFor,
  type Placed,
} from "./graphLayout";
import {
  makeAllRejectedGraph,
  makeEdges,
  makeNodes,
  makeLargeGraph,
  makeSingleNodeGraph,
} from "../test/graphFixtures";

const BOX = { width: 640, height: 360 };

/**
 * The plot the component actually solves 59 nodes into, at any window size.
 *
 * Worth spelling out rather than picking a roomy box: the overlap tests used to
 * run in 1,100x620, which is 44% more area than the shipped view was giving that
 * run, so they constrained a configuration nobody could see. The organic canvas
 * is now sized from the run alone — `ceil(59/10)*10 * 22000` px of area, an
 * aspect of 1.9, a height ceiling of 760 — and inset top and bottom for labels.
 */
const LARGE_PLOT = { width: 1737, height: 724 };

function byHid(placed: Placed[]): Map<string, Placed> {
  return new Map(placed.map((p) => [p.hid, p]));
}

function pinsFrom(placed: Placed[]): Record<string, { x: number; y: number }> {
  return Object.fromEntries(placed.map((p) => [p.hid, { x: p.x, y: p.y }]));
}

describe("hashSeed", () => {
  it("is a stable unsigned 32-bit hash of the text", () => {
    expect(hashSeed("run-abc")).toBe(hashSeed("run-abc"));
    expect(hashSeed("run-abc")).not.toBe(hashSeed("run-xyz"));
    expect(Number.isInteger(hashSeed("run-abc"))).toBe(true);
    expect(hashSeed("run-abc")).toBeGreaterThanOrEqual(0);
    expect(hashSeed("")).toBeGreaterThanOrEqual(0);
  });
});

describe("mulberry32", () => {
  it("replays the same sequence from the same seed", () => {
    const a = mulberry32(12345);
    const b = mulberry32(12345);
    expect([a(), a(), a()]).toEqual([b(), b(), b()]);
  });

  it("stays in [0, 1)", () => {
    const rng = mulberry32(hashSeed("run-abc"));
    for (let i = 0; i < 200; i += 1) {
      const value = rng();
      expect(value).toBeGreaterThanOrEqual(0);
      expect(value).toBeLessThan(1);
    }
  });
});

describe("radiusFor", () => {
  it("returns the same radius for every node when a run has one Elo", () => {
    expect(radiusFor(1200, 1200, 1200)).toBe(radiusFor(1200, 1200, 1200));
    expect(Number.isFinite(radiusFor(1200, 1200, 1200))).toBe(true);
    expect(radiusFor(1200, 1200, 1200)).toBeGreaterThan(0);
  });

  it("spans the radius range across a run's own Elo spread", () => {
    const low = radiusFor(1100, 1100, 1300);
    const high = radiusFor(1300, 1100, 1300);
    expect(high).toBeGreaterThan(low);
    expect(radiusFor(1200, 1100, 1300)).toBeCloseTo((low + high) / 2, 6);
  });

  it("never produces a NaN or a zero radius from a degenerate range", () => {
    for (const r of [
      radiusFor(1200, 1300, 1100),
      radiusFor(Number.NaN, 1100, 1300),
      radiusFor(1200, Number.NaN, Number.NaN),
    ]) {
      expect(Number.isFinite(r)).toBe(true);
      expect(r).toBeGreaterThan(0);
    }
  });
});

describe("organicLayout", () => {
  it("is identical for the same seed", () => {
    const a = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" });
    const b = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" });
    expect(a).toEqual(b);
  });

  it("differs for a different seed", () => {
    const a = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" });
    const b = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-xyz" });
    expect(a).not.toEqual(b);
  });

  it("leaves pinned nodes exactly where they were", () => {
    const pinned = { h001: { x: 100, y: 120 } };
    const out = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc", pinned });
    const h001 = out.find((p) => p.hid === "h001")!;
    expect(h001.x).toBe(100);
    expect(h001.y).toBe(120);
  });

  it("keeps every node inside the box", () => {
    for (const p of organicLayout(makeNodes(), makeEdges(), BOX, { seed: "s" })) {
      expect(p.x).toBeGreaterThanOrEqual(p.r);
      expect(p.x).toBeLessThanOrEqual(BOX.width - p.r);
      expect(p.y).toBeGreaterThanOrEqual(p.r);
      expect(p.y).toBeLessThanOrEqual(BOX.height - p.r);
    }
  });

  it("does not reshuffle settled nodes when a new one arrives", () => {
    // Pin-on-place: this is the property that lets the graph grow during a live
    // run instead of rearranging itself under the reader every few seconds.
    const settled = organicLayout(makeNodes().slice(0, 5), makeEdges(), BOX, {
      seed: "run-abc",
    });
    const grown = organicLayout(makeNodes(), makeEdges(), BOX, {
      seed: "run-abc",
      pinned: pinsFrom(settled),
    });
    const after = byHid(grown);
    for (const before of settled) {
      expect(after.get(before.hid)!.x).toBe(before.x);
      expect(after.get(before.hid)!.y).toBe(before.y);
    }
    const newcomer = after.get("h006")!;
    expect(newcomer.x).toBeGreaterThanOrEqual(newcomer.r);
    expect(newcomer.y).toBeGreaterThanOrEqual(newcomer.r);
  });

  it("settles and then stops, so more iterations change nothing", () => {
    const short = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" });
    const long = organicLayout(makeNodes(), makeEdges(), BOX, {
      seed: "run-abc",
      iterations: 4000,
    });
    expect(long).toEqual(short);
  });

  it("settles the largest run in the database too", () => {
    // 59 nodes is today's maximum. If the annealing schedule only converged for
    // toy graphs, the real ones would come out wherever the loop ran out.
    const big = makeLargeGraph(59);
    const short = organicLayout(big.nodes, big.edges, LARGE_PLOT, { seed: "run-abc" });
    const long = organicLayout(big.nodes, big.edges, LARGE_PLOT, {
      seed: "run-abc",
      iterations: 2000,
    });
    expect(long).toEqual(short);
  });

  it("does not let two ideas overlap", () => {
    const big = makeLargeGraph(59);
    const placed = organicLayout(big.nodes, big.edges, LARGE_PLOT, { seed: "run-abc" });
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        const d = Math.hypot(placed[i].x - placed[j].x, placed[i].y - placed[j].y);
        expect(d).toBeGreaterThan(placed[i].r + placed[j].r);
      }
    }
  });

  it("keeps a disc off its neighbour's hid, not only off its neighbour", () => {
    // The label hangs below the circle, and a collision term that knows only
    // about circles lets one node park squarely on another's only identifier.
    const big = makeLargeGraph(59);
    const placed = organicLayout(big.nodes, big.edges, LARGE_PLOT, { seed: "run-abc" });
    let covered = 0;
    for (const label of placed) {
      // Where the hid is actually drawn, from IdeaGraph's `y={spot.r + 14}`.
      const lx = label.x;
      const ly = label.y + label.r + 14;
      for (const other of placed) {
        if (other === label) continue;
        if (Math.hypot(other.x - lx, other.y - ly) < other.r) covered += 1;
      }
    }
    expect(covered).toBe(0);
  });

  it("centres a lone idea rather than stranding it in a corner", () => {
    const [only] = organicLayout(makeSingleNodeGraph().nodes, [], BOX, {
      seed: "run-abc",
    });
    expect(only.x).toBe(BOX.width / 2);
    expect(only.y).toBe(BOX.height / 2);
    expect(only.r).toBeGreaterThan(0);
  });

  it("gives every node a finite, positive radius when a run has one Elo", () => {
    // The all-rejected run: 18 ideas, zero matches, so every rating is 1200.
    const graph = makeAllRejectedGraph();
    for (const p of organicLayout(graph.nodes, graph.edges, BOX, { seed: "run-abc" })) {
      expect(Number.isFinite(p.x)).toBe(true);
      expect(Number.isFinite(p.y)).toBe(true);
      expect(p.r).toBeGreaterThan(0);
    }
  });

  it("separates nodes instead of stacking them on one point", () => {
    const placed = organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" });
    for (let i = 0; i < placed.length; i += 1) {
      for (let j = i + 1; j < placed.length; j += 1) {
        expect(
          Math.hypot(placed[i].x - placed[j].x, placed[i].y - placed[j].y),
        ).toBeGreaterThan(4);
      }
    }
  });

  it("survives a run with no lineage at all", () => {
    const placed = organicLayout(makeNodes(), [], BOX, { seed: "run-abc" });
    expect(placed).toHaveLength(6);
  });

  it("ignores an edge whose endpoint is not in the graph", () => {
    const withGhost = [...makeEdges(), { parent: "h999", child: "h001", operator: null }];
    expect(organicLayout(makeNodes(), withGhost, BOX, { seed: "run-abc" })).toEqual(
      organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" }),
    );
  });

  it("returns nothing for an empty graph", () => {
    expect(organicLayout([], [], BOX, { seed: "run-abc" })).toEqual([]);
  });
});

describe("layeredLayout", () => {
  it("places a node by its round, in a fixed band", () => {
    const placed = layeredLayout(makeNodes(), makeEdges(), BOX);
    const r1 = placed.filter((p) => p.hid === "h001" || p.hid === "h002");
    expect(new Set(r1.map((p) => p.y)).size).toBe(1);
  });

  it("puts a later round below an earlier one", () => {
    const placed = byHid(layeredLayout(makeNodes(), makeEdges(), BOX));
    expect(placed.get("h005")!.y).toBeGreaterThan(placed.get("h001")!.y);
  });

  it("does not move a node when a later-round node is added", () => {
    const before = layeredLayout(makeNodes().slice(0, 4), makeEdges(), BOX);
    const after = layeredLayout(makeNodes(), makeEdges(), BOX);
    const h001Before = before.find((p) => p.hid === "h001")!;
    const h001After = after.find((p) => p.hid === "h001")!;
    expect(h001After.x).toBe(h001Before.x);
  });

  it("is a pure function of round and hid, not of the input order", () => {
    const forwards = layeredLayout(makeNodes(), makeEdges(), BOX);
    const backwards = layeredLayout([...makeNodes()].reverse(), makeEdges(), BOX);
    expect(byHid(backwards).get("h003")).toEqual(byHid(forwards).get("h003"));
  });

  it("keeps every node inside the box", () => {
    for (const p of layeredLayout(makeNodes(), makeEdges(), BOX)) {
      expect(p.x).toBeGreaterThanOrEqual(p.r);
      expect(p.x).toBeLessThanOrEqual(BOX.width - p.r);
      expect(p.y).toBeGreaterThanOrEqual(p.r);
      expect(p.y).toBeLessThanOrEqual(BOX.height - p.r);
    }
  });

  it("centres a lone idea in its band", () => {
    const [only] = layeredLayout(makeSingleNodeGraph().nodes, [], BOX);
    expect(only.x).toBeCloseTo(BOX.width / 2, 0);
  });

  it("returns nothing for an empty graph", () => {
    expect(layeredLayout([], [], BOX)).toEqual([]);
  });
});

describe("purity", () => {
  it("never reaches for Math.random or Date.now", () => {
    const random = vi.spyOn(Math, "random").mockImplementation(() => {
      throw new Error("layout must not use Math.random");
    });
    const now = vi.spyOn(Date, "now").mockImplementation(() => {
      throw new Error("layout must not use Date.now");
    });
    expect(() =>
      organicLayout(makeNodes(), makeEdges(), BOX, { seed: "run-abc" }),
    ).not.toThrow();
    expect(() => layeredLayout(makeNodes(), makeEdges(), BOX)).not.toThrow();
    expect(random).not.toHaveBeenCalled();
    expect(now).not.toHaveBeenCalled();
  });

  it("does not mutate the nodes or edges it was given", () => {
    const nodes = makeNodes();
    const edges = makeEdges();
    organicLayout(nodes, edges, BOX, { seed: "run-abc" });
    layeredLayout(nodes, edges, BOX);
    expect(nodes).toEqual(makeNodes());
    expect(edges).toEqual(makeEdges());
  });
});

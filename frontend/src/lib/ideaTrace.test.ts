import { describe, expect, it } from "vitest";

import type { RunGraph } from "../components/graph/graphModel";
import {
  makeAllRejectedGraph,
  makeGraph,
  makeGraphNode,
  makeNoClusterGraph,
  makeUnevolvedGraph,
} from "../test/graphFixtures";
import { buildIdeaTrace } from "./ideaTrace";

/**
 * The biography is the one thing on /how-it-works that claims to be a record
 * rather than a diagram, so every claim it makes has to come off the payload.
 * These tests are about which idea gets told and what is said about it — the
 * two places where a plausible-looking invention would be indistinguishable
 * from the truth on screen.
 */

function graphOf(nodes: RunGraph["nodes"], edges: RunGraph["edges"] = []): RunGraph {
  return makeGraph({ nodes, edges });
}

describe("buildIdeaTrace — choosing whose life to tell", () => {
  it("follows the strongest survivor that actually bred", () => {
    // h001 leads on Elo *and* has two children, which is the ordinary case.
    const trace = buildIdeaTrace(makeGraph())!;

    expect(trace.hid).toBe("h001");
    expect(trace.children.map((child) => child.hid)).toEqual(["h005", "h006"]);
    expect(trace.example).toBe(false);
  });

  it("prefers a lower-rated parent over a stronger idea with no offspring", () => {
    // Breeding is the step that makes the loop a loop. A subject with no
    // children ends the story at step four, however far ahead it finished.
    const trace = buildIdeaTrace(
      graphOf(
        [
          makeGraphNode({ hid: "h001", elo: 1300, is_leader: true }),
          makeGraphNode({ hid: "h002", elo: 1250 }),
          makeGraphNode({ hid: "h003", elo: 1200, created_round: 2 }),
        ],
        [{ parent: "h002", child: "h003", operator: "grounding" }],
      ),
    )!;

    expect(trace.hid).toBe("h002");
    expect(trace.rank).toBe(2);
    expect(trace.survivors).toBe(3);
    expect(trace.isLeader).toBe(false);
  });

  it("falls back to the strongest survivor when nothing was ever bred", () => {
    const trace = buildIdeaTrace(makeUnevolvedGraph())!;

    expect(trace.hid).toBe("h001");
    expect(trace.children).toEqual([]);
  });

  it("breaks a tie on the lowest hid, as the leader flag does", () => {
    const trace = buildIdeaTrace(
      graphOf([
        makeGraphNode({ hid: "h007", elo: 1200 }),
        makeGraphNode({ hid: "h002", elo: 1200 }),
      ])!,
    )!;

    expect(trace.hid).toBe("h002");
  });

  it("has nothing to tell about an empty graph", () => {
    expect(buildIdeaTrace(graphOf([]))).toBeNull();
    expect(buildIdeaTrace(null)).toBeNull();
  });

  it("has nothing to tell when every idea was rejected", () => {
    // A real outcome, and the one where inventing a subject would put a corpse
    // at the top of a page about how the loop works.
    expect(buildIdeaTrace(makeAllRejectedGraph())).toBeNull();
  });
});

describe("buildIdeaTrace — what it says about the subject", () => {
  it("reads the record off the node rather than restating it", () => {
    const trace = buildIdeaTrace(makeGraph())!;

    expect(trace.title).toBe("Plasmonic nitrogen fixation on gold nanostructures");
    expect(trace.bornRound).toBe(1);
    expect(trace.origin).toBe("generation");
    expect(trace.cluster).toBe("Photocatalysis");
    expect(trace.matches).toBe(4);
    expect(trace.wins).toBe(3);
    expect(trace.losses).toBe(1);
    expect(trace.elo).toBe(1246);
    expect(trace.rank).toBe(1);
    expect(trace.isLeader).toBe(true);
  });

  it("counts an idea's offspring and names how each was made", () => {
    const trace = buildIdeaTrace(makeGraph())!;

    expect(trace.children).toEqual([
      expect.objectContaining({ hid: "h005", operator: "grounding" }),
      expect.objectContaining({ hid: "h006", operator: "combination" }),
    ]);
    // The label the genealogy canvas draws, not a second derivation of it.
    expect(trace.children[0].label).toBe(
      makeGraph().nodes.find((node) => node.hid === "h005")!.label,
    );
  });

  it("calls an idea with parents offspring, and names them", () => {
    const trace = buildIdeaTrace(
      graphOf(
        [
          makeGraphNode({ hid: "h001", elo: 1100 }),
          makeGraphNode({ hid: "h002", elo: 1100 }),
          makeGraphNode({
            hid: "h003",
            elo: 1300,
            created_round: 2,
            operator: "combination",
          }),
          makeGraphNode({ hid: "h004", elo: 1000, created_round: 3 }),
        ],
        [
          { parent: "h001", child: "h003", operator: "combination" },
          { parent: "h002", child: "h003", operator: "combination" },
          { parent: "h003", child: "h004", operator: "grounding" },
        ],
      ),
    )!;

    expect(trace.hid).toBe("h003");
    expect(trace.origin).toBe("evolution");
    expect(trace.parents).toEqual(["h001", "h002"]);
    expect(trace.operator).toBe("combination");
  });

  it("counts the near-duplicates merged into it", () => {
    // h004 was merged into h003 as a duplicate; h003 is the only bred survivor
    // once h001's descent is removed.
    const nodes = makeGraph().nodes.map((node) =>
      node.hid === "h001" ? { ...node, status: "archived" as const } : node,
    );
    const trace = buildIdeaTrace(
      graphOf(nodes, [{ parent: "h003", child: "h006", operator: "combination" }]),
    )!;

    expect(trace.hid).toBe("h003");
    expect(trace.mergedIn).toBe(1);
  });

  it("says a run recorded no themes rather than inventing one", () => {
    const trace = buildIdeaTrace(makeNoClusterGraph())!;

    expect(trace.cluster).toBeNull();
  });
});

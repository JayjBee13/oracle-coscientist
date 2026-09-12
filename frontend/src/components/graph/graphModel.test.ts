import { describe, expect, it } from "vitest";

import type { RunEvent } from "../../api/types";
import type { GraphEdge, GraphNode, RunGraph } from "./graphModel";
import {
  START_ELO,
  applyEvent,
  graphMeta,
  needsRefetchFor,
  withLabels,
} from "./graphModel";
import { shortLabel } from "./shortLabel";

const RUN_ID = "11111111-1111-1111-1111-111111111111";

let seq = 0;

/** A wire event, built whole so no test has to cast an incomplete literal. */
function ev(
  type: string,
  payload: Record<string, unknown>,
  round: number | null = 1,
): RunEvent {
  seq += 1;
  return { payload, round, run_id: RUN_ID, seq, ts: null, type };
}

function node(hid: string, over: Partial<GraphNode> = {}): GraphNode {
  const base = {
    hid,
    id: `id-${hid}`,
    title: `Idea ${hid}`,
    status: "active" as const,
    elo: START_ELO,
    matches: 0,
    wins: 0,
    created_round: 1,
    operator: null,
    cluster: null,
    duplicate_of: null,
    source: "agent" as const,
    is_leader: false,
    ...over,
  };
  // Derived rather than written, so a test node cannot carry a label the model
  // would never have given it.
  return { ...base, label: over.label ?? shortLabel(base.title, base.hid) };
}

function graph(nodes: GraphNode[], edges: GraphEdge[] = []): RunGraph {
  return { run_id: RUN_ID, nodes, edges, meta: graphMeta(nodes, edges, true) };
}

const empty = graph([]);

/** h001 leads on Elo; h002 is one match behind it. The plan's two-node fixture. */
const twoNodes = graph([
  node("h001", { elo: 1210, is_leader: true }),
  node("h002", { elo: 1190 }),
]);

const byHid = (model: RunGraph, hid: string): GraphNode =>
  model.nodes.find((n) => n.hid === hid)!;

describe("hypothesis_added", () => {
  it("inserts a node with its parent edges", () => {
    const seeded = graph([node("h001"), node("h003")]);
    const next = applyEvent(
      seeded,
      ev(
        "hypothesis_added",
        {
          hid: "h006",
          title: "Combined",
          round: 2,
          source: "agent",
          operator: "combination",
          parent_ids: ["h001", "h003"],
        },
        2,
      ),
    );

    expect(next.nodes.map((n) => n.hid)).toContain("h006");
    expect(next.edges.filter((e) => e.child === "h006")).toHaveLength(2);
    expect(next.edges.every((e) => e.operator === "combination")).toBe(true);
  });

  it("starts a new idea active, unrated and unplayed", () => {
    const next = applyEvent(
      empty,
      ev("hypothesis_added", { hid: "h001", title: "First", round: 1, source: "agent" }),
    );
    const added = byHid(next, "h001");

    expect(added.status).toBe("active");
    expect(added.elo).toBe(START_ELO);
    expect(added.matches).toBe(0);
    expect(added.wins).toBe(0);
    expect(added.created_round).toBe(1);
    expect(added.source).toBe("agent");
    expect(added.is_leader).toBe(true);
  });

  it("keeps nodes ordered by hid so the layout does not reshuffle", () => {
    const seeded = graph([node("h001"), node("h003")]);
    const next = applyEvent(
      seeded,
      ev(
        "hypothesis_added",
        { hid: "h002", title: "Late", round: 2, source: "agent" },
        2,
      ),
    );

    expect(next.nodes.map((n) => n.hid)).toEqual(["h001", "h002", "h003"]);
  });

  it("keeps a child's edges grouped in hid order, whatever the arrival order", () => {
    let model = graph([node("h001"), node("h002")]);
    model = applyEvent(
      model,
      ev(
        "hypothesis_added",
        { hid: "h009", title: "Ninth", round: 2, source: "agent", parent_ids: ["h002"] },
        2,
      ),
    );
    model = applyEvent(
      model,
      ev(
        "hypothesis_added",
        { hid: "h003", title: "Third", round: 2, source: "agent", parent_ids: ["h001"] },
        2,
      ),
    );

    expect(model.edges.map((e) => e.child)).toEqual(["h003", "h009"]);
  });

  it("ignores a replayed insert rather than duplicating the idea", () => {
    const event = ev("hypothesis_added", {
      hid: "h001",
      title: "First",
      round: 1,
      source: "agent",
    });
    const once = applyEvent(empty, event);

    expect(applyEvent(once, event)).toBe(once);
  });

  it("drops an edge to a parent the graph does not have", () => {
    const next = applyEvent(
      empty,
      ev(
        "hypothesis_added",
        {
          hid: "h006",
          title: "Orphan",
          round: 2,
          source: "agent",
          parent_ids: ["h001"],
        },
        2,
      ),
    );

    expect(next.edges).toEqual([]);
    expect(next.meta.has_lineage).toBe(false);
    expect(next.nodes).toHaveLength(1);
  });

  it("updates the meta the graph endpoint would have returned", () => {
    const seeded = graph([node("h001", { elo: 1180 })]);
    const next = applyEvent(
      seeded,
      ev(
        "hypothesis_added",
        {
          hid: "h002",
          title: "Second",
          round: 3,
          source: "human",
          parent_ids: ["h001"],
        },
        3,
      ),
    );

    expect(next.meta.node_count).toBe(2);
    expect(next.meta.rounds).toBe(3);
    expect(next.meta.has_lineage).toBe(true);
    expect(next.meta.elo_min).toBe(1180);
    expect(next.meta.elo_max).toBe(START_ELO);
  });

  it("ignores an insert with no hid to name it", () => {
    expect(applyEvent(twoNodes, ev("hypothesis_added", { title: "Nameless" }))).toBe(
      twoNodes,
    );
  });
});

describe("review_recorded", () => {
  it("rejects the idea a reject verdict names", () => {
    const next = applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h002", verdict: "reject", novelty_level: "low" }),
    );

    expect(byHid(next, "h002").status).toBe("rejected");
    expect(byHid(next, "h001").status).toBe("active");
  });

  it("leaves a passing review alone", () => {
    const next = applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h002", verdict: "pass", novelty_level: "high" }),
    );

    expect(next).toBe(twoNodes);
  });

  it("hands leadership to the best surviving idea when the leader is rejected", () => {
    const next = applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h001", verdict: "reject", novelty_level: "low" }),
    );

    expect(byHid(next, "h001").is_leader).toBe(false);
    expect(byHid(next, "h002").is_leader).toBe(true);
  });

  it("leaves an all-rejected run with no leader at all", () => {
    let model = applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h001", verdict: "reject", novelty_level: "low" }),
    );
    model = applyEvent(
      model,
      ev("review_recorded", { hid: "h002", verdict: "reject", novelty_level: "low" }),
    );

    expect(model.nodes.every((n) => n.status === "rejected")).toBe(true);
    expect(model.nodes.some((n) => n.is_leader)).toBe(false);
  });

  it("ignores a review of an idea the graph does not have", () => {
    expect(
      applyEvent(
        twoNodes,
        ev("review_recorded", { hid: "h404", verdict: "reject", novelty_level: "low" }),
      ),
    ).toBe(twoNodes);
  });
});

describe("match_completed", () => {
  const match = (over: Record<string, unknown> = {}) =>
    ev("match_completed", {
      match_id: "m1",
      round: 1,
      hid_a: "h002",
      hid_b: "h001",
      winner: 1,
      k: 24,
      elo_a_before: 1190,
      elo_a_after: 1240,
      elo_b_before: 1210,
      elo_b_after: 1180,
      ...over,
    });

  it("recomputes the leader when a match moves Elo past it", () => {
    const next = applyEvent(twoNodes, match());

    expect(byHid(next, "h002").is_leader).toBe(true);
    expect(byHid(next, "h001").is_leader).toBe(false);
  });

  it("moves both sides' rating, match count and win count", () => {
    const next = applyEvent(twoNodes, match());

    expect(byHid(next, "h002")).toMatchObject({ elo: 1240, matches: 1, wins: 1 });
    expect(byHid(next, "h001")).toMatchObject({ elo: 1180, matches: 1, wins: 0 });
  });

  it("credits the win to side b when b wins", () => {
    const next = applyEvent(twoNodes, match({ winner: 2 }));

    expect(byHid(next, "h002").wins).toBe(0);
    expect(byHid(next, "h001").wins).toBe(1);
  });

  it("rescales the run's Elo band so node size stays comparable", () => {
    const next = applyEvent(twoNodes, match());

    expect(next.meta.elo_min).toBe(1180);
    expect(next.meta.elo_max).toBe(1240);
  });

  it("never makes a rejected idea the leader, however high its rating", () => {
    const rejected = applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h002", verdict: "reject", novelty_level: "low" }),
    );
    const next = applyEvent(rejected, match());

    expect(byHid(next, "h002").is_leader).toBe(false);
    expect(byHid(next, "h001").is_leader).toBe(true);
  });

  it("ignores a match between ideas the graph does not have", () => {
    expect(applyEvent(twoNodes, match({ hid_a: "h404", hid_b: "h405" }))).toBe(twoNodes);
  });
});

describe("hypothesis_archived", () => {
  it("archives the idea and stops it leading", () => {
    const next = applyEvent(twoNodes, ev("hypothesis_archived", { hid: "h001" }));

    expect(byHid(next, "h001").status).toBe("archived");
    expect(byHid(next, "h001").is_leader).toBe(false);
    expect(byHid(next, "h002").is_leader).toBe(true);
  });

  it("ignores a second archive of the same idea", () => {
    const once = applyEvent(twoNodes, ev("hypothesis_archived", { hid: "h001" }));

    expect(applyEvent(once, ev("hypothesis_archived", { hid: "h001" }))).toBe(once);
  });
});

describe("events that carry no idea to apply", () => {
  // Verified against backend/app/engine/events.py: both payloads are run-level counters,
  // so there is nothing per-node to apply. A refetch of /graph is the only way to pick
  // up cluster hues, duplicate_of pointers and grafted territory while a run is live.
  it("leaves the graph untouched on cluster_applied", () => {
    const event = ev("cluster_applied", {
      round: 1,
      n_clusters: 2,
      labelled: 2,
      duplicates: 1,
    });

    expect(applyEvent(twoNodes, event)).toBe(twoNodes);
    expect(needsRefetchFor(event)).toBe(true);
  });

  it("leaves the graph untouched on graft_fired", () => {
    const event = ev("graft_fired", {
      round: 2,
      votes: 3,
      n_clusters: 2,
      hhi: 0.5,
      source_domain: "metallurgy",
      seed_id: "seed-r2",
    });

    expect(applyEvent(twoNodes, event)).toBe(twoNodes);
    expect(needsRefetchFor(event)).toBe(true);
  });

  it("does not ask for a refetch after an event it applied itself", () => {
    expect(needsRefetchFor(ev("match_completed", {}))).toBe(false);
    expect(needsRefetchFor(ev("some_future_event", {}))).toBe(false);
  });
});

describe("tolerance and purity", () => {
  it("ignores an event type it does not know", () => {
    expect(applyEvent(twoNodes, ev("some_future_event", {}))).toBe(twoNodes);
  });

  it("does not throw on a payload that is missing everything", () => {
    for (const type of [
      "hypothesis_added",
      "review_recorded",
      "match_completed",
      "cluster_applied",
      "graft_fired",
      "hypothesis_archived",
    ]) {
      expect(() => applyEvent(twoNodes, ev(type, {}))).not.toThrow();
    }
  });

  it("never mutates the model it was handed", () => {
    const before = structuredClone(twoNodes);
    applyEvent(
      twoNodes,
      ev("hypothesis_added", {
        hid: "h003",
        title: "Third",
        round: 2,
        source: "agent",
        parent_ids: ["h001", "h002"],
      }),
    );
    applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h001", verdict: "reject", novelty_level: "low" }),
    );

    expect(twoNodes).toEqual(before);
  });

  it("returns a new model whenever anything changed, so the store sees it", () => {
    const next = applyEvent(
      twoNodes,
      ev("review_recorded", { hid: "h002", verdict: "reject", novelty_level: "low" }),
    );

    expect(next).not.toBe(twoNodes);
    expect(next.nodes).not.toBe(twoNodes.nodes);
    expect(byHid(next, "h001")).toBe(byHid(twoNodes, "h001"));
  });
});

describe("graphMeta", () => {
  it("mirrors the endpoint's empty-run answer", () => {
    expect(graphMeta([], [], true)).toEqual({
      rounds: 1,
      has_lineage: false,
      lineage_recorded: true,
      has_clusters: false,
      elo_min: 0,
      elo_max: 0,
      node_count: 0,
    });
  });

  it("reports no clusters for a run that skipped proximity", () => {
    expect(graphMeta([node("h001"), node("h002")], [], true).has_clusters).toBe(false);
    expect(graphMeta([node("h001", { cluster: "a" })], [], true).has_clusters).toBe(true);
  });

  it("carries whether the engine could record descent, which nodes cannot say", () => {
    // A run in its first round has no edges either way. "Nothing has descended
    // from anything" and "we never recorded what descended from what" are two
    // different sentences, and only the endpoint knows which one is true.
    expect(graphMeta([node("h001")], [], true).lineage_recorded).toBe(true);
    expect(graphMeta([node("h001")], [], false).lineage_recorded).toBe(false);
    expect(graphMeta([node("h001")], [], false).has_lineage).toBe(false);
  });
});

describe("the canvas label", () => {
  it("labels every node of a fetched graph, once, from its title", () => {
    const fetched = {
      run_id: RUN_ID,
      nodes: [
        node("h001", { title: "Plasmonic nitrogen fixation on gold nanostructures" }),
        node("h002", { title: "The lithium nitride loop" }),
      ],
      edges: [],
      meta: graphMeta([], [], true),
    };

    expect(withLabels(fetched).nodes.map((n) => n.label)).toEqual([
      "Plasmonic nitrogen",
      "lithium nitride loop",
    ]);
  });

  it("labels an idea that arrived on the stream by the same rule", () => {
    const model = applyEvent(
      empty,
      ev("hypothesis_added", {
        hid: "h001",
        title: "Electrochemical nitride cycling at a lithium interface",
        round: 1,
        source: "agent",
      }),
    );

    expect(byHid(model, "h001").label).toBe("Electrochemical nitride");
    expect(byHid(model, "h001").label).toBe(
      shortLabel(byHid(model, "h001").title, "h001"),
    );
  });

  it("falls back to the hid for an idea that arrived with no title at all", () => {
    const model = applyEvent(
      empty,
      ev("hypothesis_added", { hid: "h001", round: 1, source: "agent" }),
    );

    expect(byHid(model, "h001").title).toBe("h001");
    expect(byHid(model, "h001").label).toBe("h001");
  });
});

/**
 * Builders for the idea graph — the payload of `GET /api/runs/{id}/graph`.
 *
 * Kept beside `fixtures.ts` and `runFixtures.ts` rather than inside them so the
 * layout tests, the live-model tests and the component tests can share one
 * story without three workers fighting over one file.
 *
 * The default run is a *small honest run*, not a happy path: six ideas of which
 * one was rejected in review, one was merged away as a duplicate, and one is a
 * two-parent combination. Every named edge case below is a condition that
 * actually exists in the database today — imported runs with no lineage, runs
 * that skipped clustering, a run that rejected everything, and content-only
 * runs that produced a single hypothesis. Tests that only ever see the happy
 * shape are how those runs end up rendering as a broken empty canvas.
 *
 * Every builder returns fresh objects, so calling `makeNodes()` twice yields
 * two arrays that are equal but not shared — which is exactly what the
 * determinism tests need in order to mean anything.
 */

import type {
  GraphEdge,
  GraphMeta,
  GraphNode,
  RunGraph,
} from "../components/graph/graphModel";
import { shortLabel } from "../components/graph/shortLabel";

const RUN_ID = "6f1c9a2e-0000-4000-8000-000000000001";

export function makeGraphNode(overrides: Partial<GraphNode> = {}): GraphNode {
  const base = {
    hid: "h001",
    id: "hyp-uuid-1",
    title: "Plasmonic nitrogen fixation",
    status: "active" as const,
    elo: 1200,
    matches: 0,
    wins: 0,
    created_round: 1,
    operator: null,
    cluster: null,
    duplicate_of: null,
    source: "agent" as const,
    is_leader: false,
    ...overrides,
  };
  // Derived, never written: a fixture that carried a hand-picked label could
  // pass a test the real derivation would fail.
  return { ...base, label: overrides.label ?? shortLabel(base.title, base.hid) };
}

/**
 * Six ideas across two rounds, ordered by hid exactly as the endpoint returns
 * them. h001 leads, h002 was rejected, h004 merged into h003, and h006 is the
 * combination of h001 and h003 — the visual signature of evolution.
 *
 * The first four are the whole of round 1, which is what lets the layered
 * stability test slice this array and assert that adding round 2 does not move
 * an earlier idea sideways.
 */
export function makeNodes(): GraphNode[] {
  return [
    makeGraphNode({
      hid: "h001",
      id: "hyp-uuid-1",
      title: "Plasmonic nitrogen fixation on gold nanostructures",
      elo: 1246,
      matches: 4,
      wins: 3,
      cluster: "Photocatalysis",
      is_leader: true,
    }),
    makeGraphNode({
      hid: "h002",
      id: "hyp-uuid-2",
      title: "Ambient-pressure Haber variant with a sacrificial reductant",
      status: "rejected",
      elo: 1180,
      matches: 2,
      wins: 0,
      cluster: "Photocatalysis",
    }),
    makeGraphNode({
      hid: "h003",
      id: "hyp-uuid-3",
      title: "Electrochemical nitride cycling at a lithium interface",
      elo: 1220,
      matches: 3,
      wins: 2,
      cluster: "Electrochemistry",
    }),
    makeGraphNode({
      hid: "h004",
      id: "hyp-uuid-4",
      title: "Lithium-mediated nitride loop with a solid electrolyte",
      status: "archived",
      elo: 1200,
      matches: 1,
      wins: 0,
      cluster: "Electrochemistry",
      duplicate_of: "h003",
    }),
    makeGraphNode({
      hid: "h005",
      id: "hyp-uuid-5",
      title: "Plasmonic fixation grounded on a titanium nitride support",
      elo: 1160,
      matches: 2,
      wins: 0,
      created_round: 2,
      operator: "grounding",
      cluster: "Photocatalysis",
    }),
    makeGraphNode({
      hid: "h006",
      id: "hyp-uuid-6",
      title: "Plasmon-driven electrochemical nitride loop",
      elo: 1235,
      matches: 4,
      wins: 3,
      created_round: 2,
      operator: "combination",
      cluster: "Electrochemistry",
    }),
  ];
}

/** Descent for {@link makeNodes}: h005 from h001, h006 from h001 *and* h003. */
export function makeEdges(): GraphEdge[] {
  return [
    { parent: "h001", child: "h005", operator: "grounding" },
    { parent: "h001", child: "h006", operator: "combination" },
    { parent: "h003", child: "h006", operator: "combination" },
  ];
}

/** Meta consistent with {@link makeNodes}; override only what a test is about. */
export function makeMeta(overrides: Partial<GraphMeta> = {}): GraphMeta {
  return {
    rounds: 2,
    has_lineage: true,
    lineage_recorded: true,
    has_clusters: true,
    elo_min: 1160,
    elo_max: 1246,
    node_count: 6,
    ...overrides,
  };
}

export function makeGraph(overrides: Partial<RunGraph> = {}): RunGraph {
  return {
    run_id: RUN_ID,
    nodes: makeNodes(),
    edges: makeEdges(),
    meta: makeMeta(),
    ...overrides,
  };
}

/**
 * Twelve imported runs predate `parent_ids`. Their ideas are real; only the
 * descent between them was never recorded. The view must say so rather than
 * draw an empty canvas and let the reader conclude nothing happened.
 */
export function makeNoLineageGraph(): RunGraph {
  const nodes = makeNodes().map((node) =>
    makeGraphNode({ ...node, created_round: 1, operator: null }),
  );
  return {
    run_id: RUN_ID,
    nodes,
    edges: [],
    meta: makeMeta({ rounds: 1, has_lineage: false, lineage_recorded: false }),
  };
}

/**
 * A modern run that generated one round and evolved nothing — no descent, but
 * the descent was recorded perfectly well, there simply was none. Distinct from
 * {@link makeNoLineageGraph}: these two runs look identical in the nodes and
 * edges and must not be described by the same sentence.
 */
export function makeUnevolvedGraph(): RunGraph {
  const nodes = makeNodes()
    .slice(0, 4)
    .map((node) => makeGraphNode({ ...node, created_round: 1, operator: null }));
  return {
    run_id: RUN_ID,
    nodes,
    edges: [],
    meta: makeMeta({
      rounds: 1,
      has_lineage: false,
      lineage_recorded: true,
      node_count: nodes.length,
    }),
  };
}

/**
 * A run whose clusters are named by index — `"0"`, `"1"`, `"2"` — which is what
 * the proximity step actually writes on most runs. Every other fixture here has
 * worded cluster names, which is how a bare index leaked onto the screen in one
 * surface while another said "Cluster 0" for the same node.
 */
export function makeNumericClusterGraph(): RunGraph {
  const names = ["0", "1", "0", "1", "2", "2"];
  const nodes = makeNodes().map((node, i) =>
    makeGraphNode({ ...node, cluster: names[i] }),
  );
  return { run_id: RUN_ID, nodes, edges: makeEdges(), meta: makeMeta() };
}

/**
 * Six ideas of twenty-six were set aside without being merged into anything —
 * `archived` with no `duplicate_of`, which is what "Set aside" means and what
 * five real runs contain. Drawn hollow but not dashed, so the legend has to
 * name that mark separately from "Merged away".
 */
export function makeSetAsideGraph(): RunGraph {
  const nodes = makeNodes().map((node, i) =>
    i === 1 ? makeGraphNode({ ...node, status: "archived", duplicate_of: null }) : node,
  );
  return { run_id: RUN_ID, nodes, edges: makeEdges(), meta: makeMeta() };
}

/** Four historical runs skipped proximity entirely, so hue carries no meaning. */
export function makeNoClusterGraph(): RunGraph {
  const nodes = makeNodes().map((node) => makeGraphNode({ ...node, cluster: null }));
  return {
    run_id: RUN_ID,
    nodes,
    edges: makeEdges(),
    meta: makeMeta({ has_clusters: false }),
  };
}

/**
 * One run rejected 18 of 18 and never ran a match, so every rating is the
 * starting one. An honest research outcome, and the case where a naive Elo
 * scale divides by zero.
 */
export function makeAllRejectedGraph(): RunGraph {
  const nodes = Array.from({ length: 18 }, (_, i) =>
    makeGraphNode({
      hid: `h${String(i + 1).padStart(3, "0")}`,
      id: `hyp-uuid-${i + 1}`,
      title: `Rejected idea ${i + 1}`,
      status: "rejected",
      elo: 1200,
      matches: 0,
      wins: 0,
      cluster: null,
    }),
  );
  return {
    run_id: RUN_ID,
    nodes,
    edges: [],
    meta: makeMeta({
      rounds: 1,
      has_lineage: false,
      lineage_recorded: false,
      has_clusters: false,
      elo_min: 1200,
      elo_max: 1200,
      node_count: nodes.length,
    }),
  };
}

/** Content-only historical runs produced exactly one hypothesis. */
export function makeSingleNodeGraph(): RunGraph {
  return {
    run_id: RUN_ID,
    nodes: [makeGraphNode({ is_leader: true, cluster: null })],
    edges: [],
    meta: makeMeta({
      rounds: 1,
      has_lineage: false,
      lineage_recorded: false,
      has_clusters: false,
      elo_min: 1200,
      elo_max: 1200,
      node_count: 1,
    }),
  };
}

/**
 * A run large enough to leave the organic view's useful range. The real
 * maximum today is 59 nodes; this exists so the fallback above ~150 can be
 * exercised without pretending such a run is ordinary.
 */
export function makeLargeGraph(count: number): RunGraph {
  const nodes = Array.from({ length: count }, (_, i) =>
    makeGraphNode({
      hid: `h${String(i + 1).padStart(3, "0")}`,
      id: `hyp-uuid-${i + 1}`,
      title: `Idea ${i + 1}`,
      elo: 1150 + (i % 40) * 3,
      created_round: 1 + Math.floor(i / 12),
      is_leader: i === 0,
    }),
  );
  const edges: GraphEdge[] = nodes
    .slice(12)
    .map((node, i) => ({ parent: nodes[i].hid, child: node.hid, operator: null }));
  return {
    run_id: RUN_ID,
    nodes,
    edges,
    meta: makeMeta({
      rounds: 1 + Math.floor((count - 1) / 12),
      has_lineage: edges.length > 0,
      has_clusters: false,
      elo_min: Math.min(...nodes.map((n) => n.elo)),
      elo_max: Math.max(...nodes.map((n) => n.elo)),
      node_count: count,
    }),
  };
}

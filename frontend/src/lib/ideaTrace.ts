/**
 * One idea's whole life, read out of a run's genealogy graph.
 *
 * `/how-it-works` explains the loop twice: once as a circuit, and once as the
 * biography of a single hypothesis that went round it. The second telling is
 * only worth reading if it is *true* — a made-up example teaches the shape but
 * proves nothing, and this app's whole argument is that the record is kept. So
 * the biography is derived, here, from the same `GET /api/runs/{id}/graph`
 * payload the Ideas tab draws, and every number in it is a field of that payload.
 *
 * Three rules, all of them the reason this is a module rather than a `useMemo`:
 *
 * * **Pure.** A graph in, a trace or null out. No fetch, no clock, no DOM. The
 *   page does the fetching and decides what to draw when there is nothing.
 * * **Null is a real answer.** A run with no surviving idea has no biography,
 *   and inventing one from a rejected idea would put a corpse at the top of a
 *   page about how the loop works. The caller falls back to a labelled example.
 * * **Nothing is inferred that the payload does not carry.** Descent comes from
 *   `edges`, not from a guess about titles; a null cluster means clustering did
 *   not label this run, not "unclustered".
 *
 * The subject is chosen the way a reader would choose it: the strongest idea
 * that actually *bred*, because breeding is the step that makes the loop a loop
 * and a subject with no children ends the story three steps early. Failing that,
 * the strongest survivor. Ties break on the lowest hid, exactly as `graphModel`
 * breaks them when it flags the leader, so the trace and the graph never
 * disagree about which idea is in front.
 */

import type { GraphNode, RunGraph } from "../components/graph/graphModel";

/** One offspring of the traced idea, in the order the graph records descent. */
export type TraceChild = {
  hid: string;
  /** The evolution operator that made it — `null` on runs that recorded none. */
  operator: string | null;
  /** The child's short label, as the genealogy canvas draws it. */
  label: string;
  title: string;
};

/**
 * The biography, as fields rather than as sentences.
 *
 * Deliberately not pre-rendered strings: the lane draws hids as code, operators
 * as chips and the cluster through `clusterLabel`, and a helper that returned
 * "Elo climbs to 1232" would be a second copy of the vocabulary in `status.ts`.
 */
export type IdeaTrace = {
  /** The run it came from, or null for the worked example. */
  runId: string | null;
  runTitle: string | null;
  /** True when this is the illustration rather than a record. */
  example: boolean;

  hid: string;
  title: string;
  label: string;
  bornRound: number;
  /** Drafted by Generation, or bred by Evolution from an earlier idea. */
  origin: "generation" | "evolution";
  /** The operator that made it, when it is offspring. */
  operator: string | null;
  parents: string[];

  /** False only for an idea Reflection rejected — never true of a chosen subject. */
  survivedReflection: boolean;
  /** The theme label as the payload wrote it; null when the run skipped clustering. */
  cluster: string | null;
  /** How many near-duplicates were merged into this idea. */
  mergedIn: number;

  matches: number;
  wins: number;
  losses: number;
  elo: number;

  children: TraceChild[];

  /** Where it finished among the survivors, 1-based. */
  rank: number;
  survivors: number;
  isLeader: boolean;
};

/**
 * The life of one idea from this run, or null when the run has none to tell.
 *
 * Null for an empty graph, and for a run whose every idea was rejected or set
 * aside — both of which exist in the database, and neither of which has a
 * survivor to follow.
 */
export function buildIdeaTrace(graph: RunGraph | null | undefined): IdeaTrace | null {
  if (!graph || graph.nodes.length === 0) return null;

  const active = graph.nodes.filter((node) => node.status === "active");
  if (active.length === 0) return null;

  const childrenOf = new Map<string, TraceChild[]>();
  for (const edge of graph.edges) {
    const child = graph.nodes.find((node) => node.hid === edge.child);
    if (!child) continue;
    const list = childrenOf.get(edge.parent) ?? [];
    list.push({
      hid: child.hid,
      operator: edge.operator ?? child.operator,
      label: child.label,
      title: child.title,
    });
    childrenOf.set(edge.parent, list);
  }

  const ranked = [...active].sort(byStrength);
  // The strongest idea that bred, because breeding is what makes this a loop.
  // Falling back to the strongest survivor tells a shorter but equally true
  // story: plenty of honest runs never got round to evolving anything.
  const subject =
    ranked.find((node) => (childrenOf.get(node.hid)?.length ?? 0) > 0) ?? ranked[0];

  const parents = graph.edges
    .filter((edge) => edge.child === subject.hid)
    .map((edge) => edge.parent);
  const bredBy = graph.edges.find((edge) => edge.child === subject.hid);

  return {
    runId: graph.run_id,
    runTitle: null,
    example: false,

    hid: subject.hid,
    title: subject.title,
    label: subject.label,
    bornRound: subject.created_round,
    // Offspring is a fact about descent, not about the round it landed in: a
    // round-2 idea with no parent is a fresh draft, and a graft recorded in
    // round 1 is still a graft.
    origin: parents.length > 0 ? "evolution" : "generation",
    operator: subject.operator ?? bredBy?.operator ?? null,
    parents,

    survivedReflection: true,
    cluster: subject.cluster,
    mergedIn: graph.nodes.filter((node) => node.duplicate_of === subject.hid).length,

    matches: subject.matches,
    wins: subject.wins,
    losses: Math.max(0, subject.matches - subject.wins),
    elo: subject.elo,

    children: childrenOf.get(subject.hid) ?? [],

    rank: ranked.findIndex((node) => node.hid === subject.hid) + 1,
    survivors: active.length,
    isLeader: subject.is_leader,
  };
}

/** Highest Elo first, ties to the lowest hid — `graphModel`'s own leader rule. */
function byStrength(a: GraphNode, b: GraphNode): number {
  if (a.elo !== b.elo) return b.elo - a.elo;
  return a.hid < b.hid ? -1 : a.hid > b.hid ? 1 : 0;
}

/**
 * The worked example, shown when this machine has never finished a run.
 *
 * Real numbers from a real run, frozen — and labelled as an example on screen,
 * because a page whose argument is "the record is kept" cannot afford to have
 * one illustration mistaken for a record. It is the same shape as a derived
 * trace so the lane has one renderer rather than two.
 */
export const EXAMPLE_TRACE: IdeaTrace = {
  runId: null,
  runTitle: "$50k wide sweep",
  example: true,

  hid: "h005",
  title: "Sell the negative space: an assay office for the slop flood",
  label: "Sell the negative space",
  bornRound: 1,
  origin: "generation",
  operator: null,
  parents: [],

  survivedReflection: true,
  cluster: "verification / assay of AI output",
  mergedIn: 0,

  matches: 2,
  wins: 2,
  losses: 0,
  elo: 1232,

  children: [
    {
      hid: "h009",
      operator: "grounding",
      label: "underwrite, don't inspect",
      title: "Underwrite, don't inspect — a warranty on agent-built software",
    },
    {
      hid: "h012",
      operator: "grounding",
      label: "crossed h005 × h009",
      title: "Crossed h005 × h009",
    },
    {
      hid: "h029",
      operator: "combination",
      label: "three-parent graft",
      title: "A three-parent graft of h005 × h009 × h021",
    },
  ],

  rank: 1,
  survivors: 4,
  isLeader: true,
};

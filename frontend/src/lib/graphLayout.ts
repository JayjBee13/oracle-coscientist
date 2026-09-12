/**
 * Where the ideas sit.
 *
 * Two layouts, both **pure**: no DOM, no `Math.random`, no `Date.now`, no
 * mutation of their arguments. That is not tidiness for its own sake. A force
 * layout seeded from `Math.random` puts the same run in a different shape every
 * time it is opened, which is the standard and correct objection to force
 * layouts as a default view. Seeding the PRNG from the run id instead makes the
 * organic view *reproducible* — the same run always looks the same, so a
 * reader can build a mental map of it and a screenshot still means something a
 * week later. Purity is what makes that testable rather than merely claimed.
 *
 * Three properties are the whole argument for organic being the default:
 *
 * 1. **Deterministic seeding** — `mulberry32(hashSeed(seed))` decides every
 *    starting position, so `seed` in, layout out.
 * 2. **Pin-on-place** — a caller passes the positions it already drew as
 *    `pinned`; those nodes are held exactly and still push on their neighbours,
 *    so a hypothesis arriving mid-run makes the graph *grow* rather than
 *    rearrange itself under the reader.
 * 3. **Settle then stop** — the simulation runs to convergence synchronously
 *    and freezes. The component animates the *result*; there is no ticking
 *    simulation behind the view, and nothing to stop when a run ends.
 *
 * Node size encodes Elo, and Elo is comparable **only within one run**: the
 * scale here is built from the Elo range of the nodes handed in and nothing
 * else. A run whose ratings are all identical — the run that rejected 18 of 18
 * without a single match — gets one uniform radius rather than a divide by zero
 * dressed up as information.
 */

/* --- The graph contract ---------------------------------------------------
   The shapes below are the endpoint's own, generated from the backend's OpenAPI
   document by `npm run gen:api`. They were hand-written copies while Task 1 was
   still in flight; re-exporting them here instead means a field renamed in
   pydantic is a compile error in this file rather than a layout quietly solving
   against a shape the server stopped sending.
   ------------------------------------------------------------------------- */

import type { GraphEdge, GraphNode } from "../api/types";
/* The one thing this file takes from the components tree, and only because the
   alternative is worse: `labelReach` below is a measurement of what `wrapLabel`
   draws, and a second hand-copied 5.6 is how it came to disagree with it.
   `shortLabel` is a leaf — no imports, no DOM, no React — so the dependency is
   on two numbers rather than on the view. */
import { LABEL_CHAR_PX, MAX_LINE_CHARS } from "../components/graph/shortLabel";

export type { GraphEdge, GraphMeta } from "../api/types";

/* `GraphNode` and `RunGraph` are deliberately *not* re-exported here. The app's
   node carries one field the endpoint knows nothing about — the canvas label —
   and it is defined once, in `components/graph/graphModel`. Two types called
   `GraphNode` reachable from two modules is exactly how a node without a label
   reaches a canvas that assumes one. The layouts read only the endpoint's own
   fields, so a labelled node is accepted here without either side knowing. */

/* --- What a layout returns ------------------------------------------------ */

export interface Placed {
  hid: string;
  x: number;
  y: number;
  r: number;
}

export interface LayoutBox {
  width: number;
  height: number;
}

export interface OrganicOptions {
  /** Anything stable per run — the engine run id. Same seed, same layout. */
  seed: string;
  /** Positions to hold exactly. Pinned nodes still push on their neighbours. */
  pinned?: Record<string, { x: number; y: number }>;
  /** Upper bound on steps; the simulation stops earlier once it settles. */
  iterations?: number;
}

/** Radius range. Small enough that 59 nodes fit; large enough to read. */
const R_MIN = 11;
const R_MAX = 22;

/**
 * Breathing room at the edge of the canvas, in px.
 *
 * Exported because a caller sizing a box for a *column pitch* has to subtract it
 * to get the width the columns are actually spread over — see `BAND_SLOT` in
 * `IdeaGraph`, which was off by exactly this before.
 */
export const PADDING = 28;

/**
 * The force model. Three forces, chosen for legibility rather than physics:
 * repulsion opens the graph up, springs keep descent short enough to follow,
 * and a weak pull to the centre stops a disconnected component drifting into a
 * corner (or, for a single lonely idea, leaves it centred).
 */
const FORCE = {
  /**
   * Repulsion is `repulsion / d²`, where the coefficient is derived from the
   * room actually available (see `spacingUnit`) rather than fixed. A constant
   * would either pack 59 nodes into an unreadable knot or push six of them
   * into the corners of a wide canvas.
   */
  repulsionOfSpacing: 0.004,
  /** Cap on the inverse-square term, so two near-coincident nodes cannot explode. */
  repulsionCap: 45,
  /** Extra push when two circles overlap. Nodes must not sit on top of each other. */
  collide: 0.9,
  /** Clear space demanded between two circles, in px. */
  collideGap: 6,
  /**
   * The label is drawn below its circle, and the collision term above knows
   * only about circles: two nodes perfectly clear of each other could still
   * have one disc land squarely on the other's label, which costs an idea the
   * only words it carries on the canvas. So a *second*, softer clearance asks
   * for the label's box as well, and only between nodes close enough
   * horizontally for their labels to be in each other's way. Softer because a
   * covered label is a nuisance and overlapping circles are a lie; when the two
   * pull against each other the circles must win.
   *
   * Both numbers are measurements of the label, and both grew when the label
   * stopped being a four-character hid and became a two-to-four word phrase.
   *
   * `labelRoom` is how far below the rim the label reaches: two lines at a
   * 10px font, the first baseline 12px under the circle and the second 11px
   * under that, plus a descender — 26px, where a one-line hid needed 15.
   *
   * `labelReach` is how wide it is, because two labels are only in each other's
   * way while their centres are within about one label width — so it is exactly
   * the widest line `wrapLabel` can return, computed from that function's own
   * constants rather than copied as a number. `wrapLabel` cuts a line to
   * `MAX_LINE_CHARS`, so this is a bound and not an estimate; the previous 104
   * was an estimate, and a label with no space in it drew half again as wide as
   * it. A four-character hid needed 30.
   */
  labelRoom: 26,
  labelReach: MAX_LINE_CHARS * LABEL_CHAR_PX,
  collideLabel: 0.3,
  /** Spring constant along an edge. */
  spring: 0.045,
  /** Rest length of an edge as a fraction of the spacing unit, before radii. */
  springRestOfSpacing: 0.6,
  /**
   * Bounds on the spacing unit: close enough to read, far enough to tell apart.
   *
   * Raised by a quarter along with `AREA_PER_NODE`, for the same reason: a
   * wrapped label reaches up to `labelReach / 2` either side of its centre where
   * a hid reached ~12px, so two neighbours need tens of px more between them
   * than they used to.
   */
  spacingMin: 58,
  spacingMax: 220,
  /** Pull toward the box centre, per px of offset. */
  centre: 0.008,
  /** Velocity retained each step. Below 1, so the simulation loses energy. */
  damping: 0.82,
  /**
   * Annealing: every force is scaled by `cooling ** step`. A schedule that
   * depends on the step index and nothing else — not on `iterations` — is what
   * makes "settle then stop" true rather than "stop wherever we ran out".
   */
  cooling: 0.985,
  /** Largest distance a node may move in one step, in px. */
  maxStep: 14,
  /** Settled: no node moved further than this in a step. */
  settleEpsilon: 0.02,
  /**
   * Ceiling, not a target: every run in the database settles and breaks out
   * long before it. The plan sketched 300, which turned out to be short of
   * convergence for a fifty-nine-node run under this cooling schedule — and a
   * layout that stops where the loop ran out rather than where it settled is
   * exactly the thing "settle then stop" promises not to be. Raising the
   * ceiling costs nothing when the break fires first; the largest run in the
   * database lays out in well under a tenth of a second.
   */
  iterations: 700,
} as const;

const TAU = Math.PI * 2;

/* --- Determinism ---------------------------------------------------------- */

/** FNV-1a over the seed text. Not cryptographic; just stable and well spread. */
export function hashSeed(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** mulberry32: a small, fast, seeded PRNG. Same seed, same sequence, forever. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* --- Encoding ------------------------------------------------------------- */

/**
 * Node radius from Elo, scaled against **this run's** own range.
 *
 * When a run has a single rating — every idea rejected before a match could be
 * played — there is no spread to show, so every node gets the middle radius.
 * Inventing variation there would be inventing a ranking the run never made.
 */
export function radiusFor(elo: number, min: number, max: number): number {
  const mid = (R_MIN + R_MAX) / 2;
  if (!Number.isFinite(elo) || !Number.isFinite(min) || !Number.isFinite(max)) return mid;
  if (!(max > min)) return mid;
  const t = Math.min(1, Math.max(0, (elo - min) / (max - min)));
  return R_MIN + t * (R_MAX - R_MIN);
}

/* --- Shared helpers ------------------------------------------------------- */

function eloRange(nodes: readonly GraphNode[]): { min: number; max: number } {
  let min = Infinity;
  let max = -Infinity;
  for (const node of nodes) {
    if (!Number.isFinite(node.elo)) continue;
    if (node.elo < min) min = node.elo;
    if (node.elo > max) max = node.elo;
  }
  return Number.isFinite(min) ? { min, max } : { min: 0, max: 0 };
}

/**
 * The side of the square each node would get if the canvas were shared out
 * equally — the layout's one length scale, bounded so that a nearly empty
 * canvas does not fling six ideas into the corners and a crowded one does not
 * fuse them into a knot.
 */
function spacingUnit(count: number, box: LayoutBox): number {
  const inner =
    Math.max(1, box.width - PADDING * 2) * Math.max(1, box.height - PADDING * 2);
  const unit = Math.sqrt(inner / Math.max(2, count));
  return Math.min(FORCE.spacingMax, Math.max(FORCE.spacingMin, unit));
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/**
 * Round first, clamp second — so the returned numbers satisfy
 * `r <= x <= width - r` exactly, not to within a rounding error. The radius is
 * shrunk too if the box is smaller than a node, which is the only way to keep
 * the invariant honest in a box that cannot hold what it was asked to.
 */
function place(hid: string, x: number, y: number, r: number, box: LayoutBox): Placed {
  const fit = round2(Math.max(1, Math.min(r, box.width / 2, box.height / 2)));
  return {
    hid,
    x: Math.min(Math.max(round2(x), fit), round2(box.width) - fit),
    y: Math.min(Math.max(round2(y), fit), round2(box.height) - fit),
    r: fit,
  };
}

/** Rounds are 1-based; a node that lost its round still belongs to the first. */
function roundOf(node: GraphNode): number {
  const round = Math.floor(node.created_round);
  return Number.isFinite(round) && round >= 1 ? round : 1;
}

/* --- Layered -------------------------------------------------------------- */

/**
 * Bands by round, ordered by hid inside each band.
 *
 * A node's position is a pure function of `(round, hid, band population)` and
 * nothing else — no simulation, no re-solving. That is what makes this the view
 * where lineage is unambiguous, and the fallback when the organic view degrades
 * past ~150 nodes. Adding a round-3 idea cannot move a round-1 idea sideways,
 * because nothing about round 1 depends on round 3.
 *
 * The vertical scale divides the box between the rounds present. The caller
 * owns the box: to keep bands at a fixed pitch as a live run grows, size the
 * box from `meta.rounds` and let the canvas scroll.
 *
 * `edges` is part of the signature because callers pass the same pair to both
 * layouts, but descent deliberately does not move a node here.
 */
export function layeredLayout(
  nodes: GraphNode[],
  _edges: GraphEdge[],
  box: LayoutBox,
): Placed[] {
  if (nodes.length === 0) return [];

  const { min, max } = eloRange(nodes);
  const rounds = nodes.reduce((acc, node) => Math.max(acc, roundOf(node)), 1);

  const bands = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const band = bands.get(roundOf(node));
    if (band) band.push(node);
    else bands.set(roundOf(node), [node]);
  }

  const innerHeight = Math.max(1, box.height - PADDING * 2);
  const innerWidth = Math.max(1, box.width - PADDING * 2);
  const bandHeight = innerHeight / rounds;

  const placed: Placed[] = [];
  for (const [round, members] of bands) {
    const ordered = [...members].sort((a, b) =>
      a.hid < b.hid ? -1 : a.hid > b.hid ? 1 : 0,
    );
    const y = PADDING + (round - 0.5) * bandHeight;
    ordered.forEach((node, i) => {
      // (i + 1) / (n + 1) spreads members evenly *with* margins, which puts a
      // single member in the middle of its band rather than hard left.
      const x = PADDING + ((i + 1) / (ordered.length + 1)) * innerWidth;
      placed.push(place(node.hid, x, y, radiusFor(node.elo, min, max), box));
    });
  }

  // Returned in the caller's node order so the SVG's DOM order matches the
  // text alternative's, which is what keeps tab order sane.
  const byHid = new Map(placed.map((p) => [p.hid, p]));
  return nodes.map((node) => byHid.get(node.hid)!);
}

/* --- Organic -------------------------------------------------------------- */

/**
 * Force-directed, seeded, run to convergence, then frozen.
 *
 * Three forces per step — pairwise repulsion capped near zero distance, a
 * spring along each recorded descent, and a weak pull to the centre — then a
 * damped velocity integration with a per-step displacement cap. The loop exits
 * as soon as the largest displacement falls under `settleEpsilon`, so asking
 * for more iterations than convergence needs returns the identical layout
 * rather than a slightly different one: settle, then stop.
 *
 * Pinned nodes are excluded from integration but included in every force sum,
 * so a newcomer is pushed away from the settled graph while the settled graph
 * stays exactly where the reader last saw it.
 */
export function organicLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  box: LayoutBox,
  opts: OrganicOptions,
): Placed[] {
  const n = nodes.length;
  if (n === 0) return [];

  const { min, max } = eloRange(nodes);
  const radii = nodes.map((node) => radiusFor(node.elo, min, max));
  const indexOf = new Map(nodes.map((node, i) => [node.hid, i]));

  const cx = box.width / 2;
  const cy = box.height / 2;
  const spread = Math.max(1, Math.min(box.width, box.height) / 3);

  // How far apart two unconnected neighbours should end up: the side of the
  // square each node would get if the canvas were divided equally. Repulsion
  // and edge length are both expressed in this unit, which is what lets one
  // set of constants serve a six-idea run and a fifty-nine-idea one.
  const spacing = spacingUnit(n, box);
  const repulsion = FORCE.repulsionOfSpacing * spacing ** 3;
  const springRest = FORCE.springRestOfSpacing * spacing;

  // The pull to the centre is weaker along the longer axis, so the settled
  // cloud takes the shape of the canvas instead of leaving a wide screen
  // two-thirds empty around a circle.
  const shortSide = Math.max(1, Math.min(box.width, box.height));
  const centreX = (FORCE.centre * shortSide) / Math.max(1, box.width);
  const centreY = (FORCE.centre * shortSide) / Math.max(1, box.height);
  const rng = mulberry32(hashSeed(opts.seed));

  const xs = new Float64Array(n);
  const ys = new Float64Array(n);
  const vx = new Float64Array(n);
  const vy = new Float64Array(n);
  const fx = new Float64Array(n);
  const fy = new Float64Array(n);
  const isPinned = new Array<boolean>(n).fill(false);

  for (let i = 0; i < n; i += 1) {
    // Draw for every node, pinned or not, so that pinning one does not shift
    // the PRNG sequence the others depend on.
    const angle = rng() * TAU;
    const t = Math.sqrt(rng());
    const pin = opts.pinned?.[nodes[i].hid];
    if (pin) {
      xs[i] = pin.x;
      ys[i] = pin.y;
      isPinned[i] = true;
    } else {
      xs[i] = cx + Math.cos(angle) * spread * t;
      ys[i] = cy + Math.sin(angle) * spread * t;
    }
  }

  // A lone idea has nothing to be arranged against, and the centring force is
  // deliberately weak enough that annealing would freeze it a few pixels shy of
  // the middle. Content-only historical runs produced exactly one hypothesis,
  // and it belongs in the centre of the canvas, not near it.
  if (n === 1 && !isPinned[0]) {
    xs[0] = cx;
    ys[0] = cy;
  }

  // Only edges whose endpoints are both present pull on anything; a dangling
  // parent (a hid outside this run's graph) is silently no force at all.
  const links: Array<[number, number]> = [];
  for (const edge of edges) {
    const a = indexOf.get(edge.parent);
    const b = indexOf.get(edge.child);
    if (a !== undefined && b !== undefined && a !== b) links.push([a, b]);
  }

  const iterations = Math.max(0, opts.iterations ?? FORCE.iterations);
  for (let step = 0; step < iterations; step += 1) {
    fx.fill(0);
    fy.fill(0);

    for (let i = 0; i < n; i += 1) {
      for (let j = i + 1; j < n; j += 1) {
        let dx = xs[j] - xs[i];
        let dy = ys[j] - ys[i];
        let d2 = dx * dx + dy * dy;
        if (d2 < 1e-6) {
          // Coincident: nudge along a fixed, index-derived direction rather
          // than a random one, so the tie-break stays deterministic.
          dx = 0.01 * (j - i);
          dy = 0.01;
          d2 = dx * dx + dy * dy;
        }
        const d = Math.sqrt(d2);
        let mag = Math.min(repulsion / d2, FORCE.repulsionCap);
        const clear = radii[i] + radii[j] + FORCE.collideGap;
        if (d < clear) mag += (clear - d) * FORCE.collide;
        if (Math.abs(dx) < FORCE.labelReach) {
          const room = clear + FORCE.labelRoom;
          if (d < room) mag += (room - d) * FORCE.collideLabel;
        }
        const ux = (dx / d) * mag;
        const uy = (dy / d) * mag;
        fx[i] -= ux;
        fy[i] -= uy;
        fx[j] += ux;
        fy[j] += uy;
      }
    }

    for (const [a, b] of links) {
      const dx = xs[b] - xs[a];
      const dy = ys[b] - ys[a];
      const d = Math.sqrt(dx * dx + dy * dy) || 1e-6;
      const rest = springRest + radii[a] + radii[b];
      const mag = FORCE.spring * (d - rest);
      const ux = (dx / d) * mag;
      const uy = (dy / d) * mag;
      fx[a] += ux;
      fy[a] += uy;
      fx[b] -= ux;
      fy[b] -= uy;
    }

    for (let i = 0; i < n; i += 1) {
      fx[i] += (cx - xs[i]) * centreX;
      fy[i] += (cy - ys[i]) * centreY;
    }

    const alpha = FORCE.cooling ** step;
    let moved = 0;
    for (let i = 0; i < n; i += 1) {
      if (isPinned[i]) {
        vx[i] = 0;
        vy[i] = 0;
        continue;
      }
      vx[i] = (vx[i] + fx[i] * alpha) * FORCE.damping;
      vy[i] = (vy[i] + fy[i] * alpha) * FORCE.damping;
      const speed = Math.sqrt(vx[i] * vx[i] + vy[i] * vy[i]);
      if (speed > FORCE.maxStep) {
        vx[i] *= FORCE.maxStep / speed;
        vy[i] *= FORCE.maxStep / speed;
      }
      xs[i] += vx[i];
      ys[i] += vy[i];
      moved = Math.max(moved, Math.min(speed, FORCE.maxStep));
    }

    if (moved < FORCE.settleEpsilon) break; // settled; nothing left to solve
  }

  return nodes.map((node, i) => place(node.hid, xs[i], ys[i], radii[i], box));
}

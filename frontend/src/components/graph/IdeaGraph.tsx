import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { formatElo, plural } from "../../lib/format";
import type { LayoutBox, Placed } from "../../lib/graphLayout";
import { PADDING, layeredLayout, organicLayout } from "../../lib/graphLayout";
import {
  clusterLabel,
  describeHypothesisStatus,
  describeOperator,
} from "../../lib/status";
import "../../styles/graph.css";
import type { GraphNode, RunGraph } from "./graphModel";
import { LABEL_CHAR_PX, MAX_LINE_CHARS, wrapLabel } from "./shortLabel";

/**
 * How the ideas are related, drawn.
 *
 * This is the Ideas tab's main surface, and the thing a researcher stares at
 * while a run thinks. Its subject is **attrition**, in that order of priority:
 * what survived, what it descended from, and — a distant third — which region
 * of idea-space it came out of. The tournament is deliberately not the subject;
 * who beat whom belongs in the detail rail for one idea, not in the structure
 * of the graph.
 *
 * Five rules shape the code.
 *
 * 1. **The drawing owns no truth.** Positions come from the pure layouts in
 *    `lib/graphLayout`, the live model from `graphModel`, and the numbers from
 *    the endpoint. This file decides only how a fact is rendered, which is why
 *    it can be swapped between two layouts without re-teaching the reader a
 *    single symbol: the encoding table below is identical in both views.
 * 2. **Colour is never the only signal.** A leader is green *and* double-ringed;
 *    a rejection is faded *and* struck through; a merged duplicate is hollow
 *    *and* dashed; cluster hue is repeated as a named swatch in the legend and
 *    as words in the text alternative. Print this in greyscale and it still
 *    reads.
 * 3. **The label says what the idea is; the hid is one hover away.** A canvas
 *    labelled `h001 … h059` is unambiguous and tells a reader nothing: finding
 *    the idea about lithium meant hovering fifty-nine circles. So each node
 *    carries a two-to-four word phrase cut from its title by
 *    `shortLabel` — derived once in `graphModel`, never in a render, because
 *    the spacing constants below are tuned against the widest phrase it can
 *    produce. The full title is still nowhere near the canvas: it is in the
 *    `<title>` element (a tooltip with no JavaScript at all), in the accessible
 *    name, and in the detail rail. The **hid** is in the tooltip and the
 *    accessible name too, so the identifier every other surface in the app uses
 *    is one hover or one Tab away from any circle. Nothing is truncated to
 *    uselessness and nothing wrecks the layout, which is what the old
 *    hid-only rule was really protecting.
 * 4. **Silence is a lie, and so is the wrong sentence.** Six conditions in the
 *    real database would otherwise render as a broken canvas or a false claim:
 *    a run whose descent was never recorded, a run that recorded descent and
 *    evolved nothing, a run that never clustered, a run that rejected
 *    everything, that same run with its rejections filtered away, and a run
 *    with one idea. Each says what is true instead of drawing nothing — and
 *    nothing is said about descent while a live run could still produce some.
 * 5. **The legend describes this run.** A mark is drawn only when a node on the
 *    canvas carries it. Hue was gated this way from the start; status and
 *    descent are now, because "Leading" beside a run that rejected all
 *    eighteen of its ideas is a claim about a circle that is not there.
 *
 * Elo is comparable **only within a run**, so node size is scaled by
 * `radiusFor` against the Elo range of the nodes on this canvas and nothing
 * else. There is no cross-run size meaning and no axis pretending otherwise.
 */

export interface IdeaGraphProps {
  graph: RunGraph;
  view: "organic" | "layered";
  selectedHid: string | null;
  /** `null` clears the selection — Escape, from anywhere in the graph. */
  onSelect: (hid: string | null) => void;
  /** Anything stable per run — the engine run id. Same seed, same layout. */
  seed: string;
  /** While a run is still producing ideas: settled nodes are pinned in place. */
  live: boolean;
}

/**
 * Past this the organic view is a hairball rather than a picture, so it hands
 * over to layered and says so. The largest run in the database today is 59.
 */
const ORGANIC_LIMIT = 150;

/** Used before layout has measured anything — jsdom, and the first paint. */
const FALLBACK_WIDTH = 960;
const FALLBACK_HEIGHT = 460;

/** Smallest canvas worth solving into, whatever the container reports. */
const MIN_WIDTH = 520;
const MIN_HEIGHT = 260;

/**
 * Roughly the room one idea needs to stay separable, and the shape the cloud
 * is asked to take.
 *
 * The organic canvas is sized *to the run* and then centred, rather than
 * stretched to whatever the panel happens to be: six ideas spread across
 * 1,400px are six ideas nobody can see a relationship between, and the
 * centring force is deliberately weak, so a too-large box leaves the graph
 * adrift in one corner of it.
 *
 * It is also sized **only** to the run. Clamping the box down to the panel is
 * how `AREA_PER_NODE` came to be ignored above about twenty ideas: the largest
 * run in the database asks for 1.3M px² and was being given 0.45M, which is the
 * hairball the organic view is supposed not to be. The layered view has always
 * grown past the viewport and scrolled inside `.ig__canvas`; this one now does
 * the same. Dropping the viewport from the calculation has a second benefit
 * that matters more during a live run than the first: the box no longer changes
 * when the window is resized, so the pins below survive it.
 *
 * `AREA_PER_NODE` had to grow when the label became a phrase, and what it buys
 * is measured on the **plot** — the area is spent on `box`, but the layouts
 * solve into `box` less `LABEL_SIDE` either side and `LABEL_ROOM` top and
 * bottom, so the side each node actually gets is smaller than `sqrt(34,000)`.
 * Realised at the largest run in the database (59 ideas, quantised to 60): a
 * 2,488×820 box, a 2,386×760 plot, 30.7k px² and so ~175px of side per node,
 * against ~146px under the old 22,000. That +29px is the room a wrapped label
 * needs beside a hid, and the ceilings below were raised so that the run is
 * actually given it: clamping the box back down is precisely how
 * `AREA_PER_NODE` came to be ignored the first time.
 */
const AREA_PER_NODE = 34000;
const ORGANIC_ASPECT = 1.9;
const SMALL_RUN_FLOOR = 10;

/**
 * Node counts are rounded up to a multiple of this before the box is sized.
 *
 * The box is a function of the node count, and during a live run the node count
 * changes every generation batch — so an unquantised box changed on every
 * arrival, which changed the pin key, which dropped every pin and re-solved the
 * whole graph from scratch. That is the exact reshuffle pin-on-place exists to
 * prevent, and it happened for every run under ~34 ideas. Quantising means the
 * box holds still for a batch or two at a time; `rescalePins` covers the few
 * steps where it does move.
 */
const NODE_QUANTUM = 10;

/**
 * Ceilings on the organic canvas, past which it would be a map, not a picture.
 *
 * Both moved with the label: the width because the largest run now asks for
 * 2.0M px² and being clamped back to 1.8M is the hairball this view exists not
 * to be, the height because a two-line label is ~13px taller than a one-line
 * hid and fifty-nine of them need somewhere to go.
 */
const MAX_ORGANIC_WIDTH = 3000;
const MAX_ORGANIC_HEIGHT = 820;

/**
 * Vertical pitch of a round, and horizontal room per idea inside one, layered.
 *
 * A band's slot is the column pitch, so it has to hold the widest line a label
 * can draw — `MAX_LINE_CHARS * LABEL_CHAR_PX`, the same bound `FORCE.labelReach`
 * is computed from — plus a little air, because two labels exactly one label
 * wide apart touch. The vertical pitch is untouched: at 132px a band already had
 * eighty-odd pixels of clear air under its largest circle, which two lines of
 * label do not come close to spending.
 *
 * "Is the column pitch" is a claim about the *box*, and it has to be arranged
 * for — see the layered branch of `box` below. `layeredLayout` spreads n members
 * over n+1 gaps, so a box sized for n slots realises a pitch of n/(n+1) of one,
 * which is how twelve ideas in a band came to overlap their neighbours' labels
 * while the constant here said they could not.
 */
const BAND_HEIGHT = 132;
const BAND_AIR = 8;
const BAND_SLOT = Math.ceil(MAX_LINE_CHARS * LABEL_CHAR_PX) + BAND_AIR;

const MAX_LAYERED_HEIGHT = 4000;

/**
 * Room around the plot for the labels, kept clear at every edge of the canvas.
 *
 * `place()` guarantees a *circle* inside the box it is given, and a label hangs
 * outside that circle in two directions. Below it, by 26px: two lines at 10px,
 * the first baseline 12px under the rim and the second 11px under that, plus a
 * descender. Sideways, by half its width: labels are centred on their circle, so
 * the widest line `wrapLabel` can return overhangs by half of it either side —
 * an inset the canvas never had at all while the label was a 25px hid that the
 * minimum radius happened to cover.
 *
 * Half of the *bound*, not half of an estimate. `wrapLabel` cuts every line it
 * returns to `MAX_LINE_CHARS`, so there is no label this inset does not cover;
 * `.ig__svg` does not set `overflow: visible`, so anything it failed to cover
 * would be clipped away rather than merely untidy.
 */
const LABEL_ROOM = 30;
const LABEL_SIDE = Math.ceil((MAX_LINE_CHARS * LABEL_CHAR_PX) / 2);

/** Baseline of the first label line under the rim, and the pitch of a second. */
const LABEL_BASELINE = 12;
const LABEL_LINE = 11;

/**
 * How many clusters get a hue.
 *
 * Not a palette limit — an honesty limit. One imported run has 26 clusters
 * across 59 ideas; six recycled hues over 26 groups would be a legend that
 * claims a distinction the eye cannot make. So the **largest** six are shaded
 * and the rest are left unshaded and counted in the legend, which keeps hue
 * meaning "this clump" rather than meaning nothing.
 */
const CLUSTER_SLOTS = 6;

export function IdeaGraph({
  graph,
  view,
  selectedHid,
  onSelect,
  seed,
  live,
}: IdeaGraphProps) {
  const markerId = useId().replace(/:/g, "");
  const viewportRef = useRef<HTMLDivElement>(null);
  const [measured, setMeasured] = useState({ width: 0, height: 0 });
  const [hideRejected, setHideRejected] = useState(false);
  const [activeHid, setActiveHid] = useState<string | null>(null);

  /* --- the box -------------------------------------------------------------
     The canvas is drawn at 1:1 rather than scaled by a viewBox: a graph that
     shrinks to fit takes its labels down with it, and an 8px phrase is not a
     label. It takes the room the run needs and scrolls inside its own
     container when that is more than the panel has — the page body never
     scrolls sideways for it.

     The measurement below is the *layered* view's business only. That view
     stretches to the panel and grows past it; the organic view is sized from
     the run alone, so what the container reports cannot move a settled node.
     ---------------------------------------------------------------------- */
  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const measure = (): void =>
      setMeasured((current) =>
        current.width === element.clientWidth && current.height === element.clientHeight
          ? current
          : { width: element.clientWidth, height: element.clientHeight },
      );
    measure();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const visible = useMemo(
    () =>
      hideRejected ? graph.nodes.filter((n) => n.status !== "rejected") : graph.nodes,
    [graph.nodes, hideRejected],
  );
  const visibleHids = useMemo(() => new Set(visible.map((n) => n.hid)), [visible]);
  const edges = useMemo(
    () =>
      graph.edges.filter(
        (edge) => visibleHids.has(edge.parent) && visibleHids.has(edge.child),
      ),
    [graph.edges, visibleHids],
  );

  const oversized = graph.meta.node_count > ORGANIC_LIMIT;
  const effectiveView = oversized ? "layered" : view;

  const box = useMemo<LayoutBox>(() => {
    const availWidth = Math.max(MIN_WIDTH, measured.width || FALLBACK_WIDTH);
    const availHeight = Math.max(MIN_HEIGHT, measured.height || FALLBACK_HEIGHT);

    if (effectiveView === "layered") {
      // Rounds set the height; the busiest round sets the width. A band that
      // cannot give each idea its own slot is a row of overlapping circles, so
      // the canvas widens and scrolls rather than crushing them together.
      const bands = new Map<number, number>();
      let rounds = 1;
      for (const idea of visible) {
        const round = Math.max(1, idea.created_round);
        rounds = Math.max(rounds, round);
        bands.set(round, (bands.get(round) ?? 0) + 1);
      }
      const busiest = Math.max(1, ...bands.values());
      return {
        // The slots are what the *plot* needs; the side insets are what the
        // labels at either end of the busiest band hang into.
        //
        // `busiest + 1`, not `busiest`: `layeredLayout` places member i of n at
        // (i + 1) / (n + 1) of the width it is given, so the realised pitch is
        // that width over n + 1 gaps, not n. Asking for n slots delivered n/(n+1)
        // of one — 96.6px against a 104px slot for a band of twelve, which is
        // adjacent labels overlapping. The plot is the box less both insets less
        // the layout's own `PADDING` at each end, so all four come off here and
        // the pitch is >= BAND_SLOT by construction.
        width: Math.max(
          availWidth,
          BAND_SLOT * (busiest + 1) + PADDING * 2 + LABEL_SIDE * 2,
        ),
        height: Math.min(
          MAX_LAYERED_HEIGHT,
          Math.max(availHeight, rounds * BAND_HEIGHT + 56),
        ),
      };
    }

    // The floor is why a four-idea run does not get a postage stamp: below
    // about ten nodes the repulsion has already reached its maximum spread, so
    // a smaller box only crops the margin rather than tightening the graph.
    // Neither `availWidth` nor `availHeight` appears below, on purpose — see
    // the note on AREA_PER_NODE.
    const counted = Math.max(
      SMALL_RUN_FLOOR,
      Math.ceil(visible.length / NODE_QUANTUM) * NODE_QUANTUM,
    );
    const area = counted * AREA_PER_NODE;
    // Height first, so that a run past the ceiling spends its remaining area on
    // width: a canvas that scrolls sideways is ordinary, one that scrolls both
    // ways is a map of a graph rather than a graph.
    const height = clamp(
      MIN_HEIGHT,
      Math.sqrt(area / ORGANIC_ASPECT),
      MAX_ORGANIC_HEIGHT,
    );
    return {
      width: Math.round(clamp(MIN_WIDTH, area / height, MAX_ORGANIC_WIDTH)),
      height: Math.round(height),
    };
  }, [measured, effectiveView, visible]);

  /**
   * The area the layouts actually solve into.
   *
   * `place()` guarantees a *circle* inside the box it is given, but the label
   * hangs below its circle and half of it hangs either side — so a node clamped
   * to an edge of the canvas would have its words drawn off it. Inset by the
   * label's reach on all four sides, then translate the whole plot back by the
   * same amounts: the geometry stays symmetrical (a lone idea is still exactly
   * centred in the canvas) and nothing a node draws can leave it.
   */
  // Memoised on the two numbers rather than on `box`, so a resize that lands on
  // the same dimensions — every resize, now, in the organic view — hands the
  // layout the object it already solved against instead of an equal new one.
  const plot = useMemo<LayoutBox>(
    () => ({
      width: Math.max(MIN_WIDTH / 2, box.width - LABEL_SIDE * 2),
      height: Math.max(MIN_HEIGHT / 2, box.height - LABEL_ROOM * 2),
    }),
    [box.width, box.height],
  );

  /* --- pin on place --------------------------------------------------------
     The positions already on screen are handed back to the next solve as pins,
     so an arriving idea makes the graph *grow* rather than rearrange itself
     under the reader's cursor.

     Two things this deliberately does *not* key on, both of which broke the
     property it exists to provide.

     The **box** is not in the key. It used to be, and since the organic box was
     a function of the live node count, every batch of arriving ideas changed
     it, missed the cache and re-solved the whole graph from a different seeded
     start. A box change now rescales the pins into the new canvas instead of
     discarding them, which is a graph that grew rather than a graph re-rolled.

     **Liveness** is not in the key either, and the pins are kept whether or not
     the run is still running. Dropping them when `run_finished` landed threw
     away the exact picture the reader had just watched settle, at the moment of
     the result. A fresh page load still re-solves deterministically from the
     seed, so reproducibility is untouched; what survives is the arrangement a
     reader was looking at a second ago.
     ---------------------------------------------------------------------- */
  const pinKey = `${effectiveView}|${seed}|${hideRejected}`;

  /**
   * Where everything was last drawn, and the canvas it was drawn for.
   *
   * A cache, not state: it must never cause a render of its own, because it is
   * only ever read by the *next* solve — the one a real change (an idea
   * arriving, a rejection landing) has already triggered. `useState` with an
   * initialiser and no setter is the idiomatic way to hold a per-instance
   * mutable collection that React neither re-creates nor reacts to.
   *
   * The map holds one entry at a time: a view switch or a filter change is a
   * different problem, and pinning to coordinates from one is no help to the
   * other.
   */
  const lastDrawn = useState(() => new Map<string, Remembered>())[0];

  const placed = useMemo<Placed[]>(() => {
    if (visible.length === 0) return [];
    if (effectiveView === "layered") return layeredLayout(visible, edges, plot);
    return organicLayout(visible, edges, plot, {
      seed,
      pinned: rescalePins(lastDrawn.get(pinKey), plot),
    });
  }, [visible, edges, plot, effectiveView, seed, pinKey, lastDrawn]);

  useEffect(() => {
    lastDrawn.clear();
    const at: Pins = {};
    for (const spot of placed) at[spot.hid] = { x: spot.x, y: spot.y };
    lastDrawn.set(pinKey, { box: plot, at });
  }, [placed, plot, pinKey, lastDrawn]);

  /* --- derived reading ---------------------------------------------------- */

  const byHid = useMemo(() => new Map(graph.nodes.map((n) => [n.hid, n])), [graph.nodes]);
  const spots = useMemo(() => new Map(placed.map((spot) => [spot.hid, spot])), [placed]);
  const parents = useMemo(
    () =>
      groupBy(
        graph.edges,
        (edge) => edge.child,
        (edge) => edge.parent,
      ),
    [graph.edges],
  );
  const children = useMemo(
    () =>
      groupBy(
        graph.edges,
        (edge) => edge.parent,
        (edge) => edge.child,
      ),
    [graph.edges],
  );
  const { slots: clusterSlots, total: clusterCount } = useMemo(
    () => clusterHues(graph.nodes),
    [graph.nodes],
  );
  const rejectedCount = useMemo(
    () => graph.nodes.filter((n) => n.status === "rejected").length,
    [graph.nodes],
  );
  const allRejected = graph.nodes.length > 0 && rejectedCount === graph.nodes.length;

  /**
   * Which channels are actually on the canvas.
   *
   * The legend describes the drawing, not the component: a run that rejected all
   * eighteen of its ideas has nothing leading, nothing in play and nothing
   * merged, and three marks claiming otherwise are three small lies beside a
   * canvas that shows none of them. Read from `visible` rather than from the
   * whole run, so the filter takes the marks it hides with it.
   */
  const marks = useMemo(() => {
    let leader = false;
    let active = false;
    let rejected = false;
    let merged = false;
    let setAside = false;
    for (const node of visible) {
      if (node.is_leader) leader = true;
      if (node.status === "active") active = true;
      if (node.status === "rejected") rejected = true;
      if (node.status === "archived") {
        if (node.duplicate_of) merged = true;
        else setAside = true;
      }
    }
    return { leader, active, rejected, merged, setAside };
  }, [visible]);

  /**
   * The rounds the layered view draws, each labelled by the rounds inside it.
   *
   * Read off the *placed* nodes rather than recomputed, so a band's label
   * cannot drift from the band it names. The label says what is in the band and
   * nothing more: `layeredLayout` files a round-0 idea in the first band, which
   * several imported runs really do contain, and a band holding both reads
   * "Rounds 0–1" rather than picking one of them and contradicting the rail.
   */
  const bands = useMemo(() => {
    if (effectiveView !== "layered") return [];
    const rows = new Map<number, { y: number; rounds: Set<number> }>();
    for (const idea of visible) {
      const spot = spots.get(idea.hid);
      if (!spot) continue;
      const band = Math.max(1, Math.floor(idea.created_round) || 1);
      const row = rows.get(band) ?? { y: spot.y, rounds: new Set<number>() };
      row.rounds.add(idea.created_round);
      rows.set(band, row);
    }
    return [...rows.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([band, row]) => {
        const seen = [...row.rounds].sort((a, b) => a - b);
        return {
          band,
          y: row.y,
          label:
            seen.length > 1
              ? `Rounds ${seen[0]}–${seen[seen.length - 1]}`
              : `Round ${seen[0]}`,
        };
      });
  }, [effectiveView, visible, spots]);

  /* --- keyboard ------------------------------------------------------------
     One tab stop, not fifty-nine. A graph is a composite widget: Tab reaches it
     and the arrows move inside it, which is the only way a fifty-nine node run
     does not swallow a keyboard user's afternoon. Left and right walk the ideas
     in hid order; up and down follow descent, which is what this view is about.
     ---------------------------------------------------------------------- */
  const refs = useRef(new Map<string, SVGGElement>());
  const tabStop =
    (activeHid && visibleHids.has(activeHid) ? activeHid : null) ??
    (selectedHid && visibleHids.has(selectedHid) ? selectedHid : null) ??
    visible.find((n) => n.is_leader)?.hid ??
    visible[0]?.hid ??
    null;

  const focusHid = useCallback((hid: string | undefined): void => {
    if (!hid) return;
    setActiveHid(hid);
    refs.current.get(hid)?.focus();
  }, []);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<SVGGElement>, hid: string): void => {
      const order = visible.map((n) => n.hid);
      const at = order.indexOf(hid);
      const step = (delta: number): void =>
        focusHid(order[Math.min(order.length - 1, Math.max(0, at + delta))]);

      switch (event.key) {
        case "Enter":
        case " ":
        case "Spacebar":
          event.preventDefault();
          onSelect(hid);
          return;
        case "ArrowRight":
          event.preventDefault();
          step(1);
          return;
        case "ArrowLeft":
          event.preventDefault();
          step(-1);
          return;
        case "ArrowUp":
          event.preventDefault();
          focusHid(
            (parents.get(hid) ?? []).find((p) => visibleHids.has(p)) ??
              order[Math.max(0, at - 1)],
          );
          return;
        case "ArrowDown":
          event.preventDefault();
          focusHid(
            (children.get(hid) ?? []).find((c) => visibleHids.has(c)) ??
              order[Math.min(order.length - 1, at + 1)],
          );
          return;
        case "Escape":
          // The way back out. Without it a reader who opened an idea has no
          // keyboard route to the rail's neutral state, because there is
          // nowhere on the canvas that deselects. Focus stays on the node.
          event.preventDefault();
          onSelect(null);
          return;
        case "Home":
          event.preventDefault();
          focusHid(order[0]);
          return;
        case "End":
          event.preventDefault();
          focusHid(order[order.length - 1]);
          return;
        default:
      }
    },
    [visible, visibleHids, parents, children, focusHid, onSelect],
  );

  /* --- the drawing -------------------------------------------------------- */

  const drawnEdges = useMemo(() => {
    const rank = new Map<string, number>();
    return edges.flatMap((edge) => {
      const from = spots.get(edge.parent);
      const to = spots.get(edge.child);
      if (!from || !to) return [];
      const seen = rank.get(edge.child) ?? 0;
      rank.set(edge.child, seen + 1);
      const siblings = (parents.get(edge.child) ?? []).filter((p) =>
        visibleHids.has(p),
      ).length;
      const d =
        effectiveView === "layered"
          ? layeredPath(from, to)
          : organicPath(from, to, bowOf(seen, siblings, from, to));
      return [{ edge, d }];
    });
  }, [edges, spots, parents, visibleHids, effectiveView]);

  if (graph.nodes.length === 0) {
    return (
      <div className="ig">
        <p className="tab-note">
          No ideas were recorded for this run, so there is no genealogy to draw.
        </p>
      </div>
    );
  }

  return (
    // A run with one or two ideas gets a canvas its own size. Stretched to the
    // full panel it reads as an empty graph with a speck in it — which is the
    // opposite of what a content-only historical run actually did.
    <div className="ig" data-scale={graph.nodes.length <= 3 ? "tiny" : undefined}>
      {oversized ? (
        <p className="notice" data-tone="info">
          <span>
            Too many ideas for the organic view to stay readable ({graph.meta.node_count}
            ), so this run is shown in rounds.
          </span>
        </p>
      ) : null}
      {/*
        Two absences, two sentences, and neither is said while the run can still
        disprove it. A run in its first round has no descent yet and has not
        failed to record any: saying otherwise put a claim on screen at three
        seconds and took it back at eleven.
      */}
      {!live && !graph.meta.has_lineage ? (
        <p className="notice" data-tone="neutral">
          <span>
            {graph.meta.lineage_recorded
              ? "No idea in this run descended from another — every one was written from scratch."
              : "Lineage was not recorded for this run, so its ideas are drawn as one generation with no descent between them."}
          </span>
        </p>
      ) : null}
      {allRejected ? (
        <p className="notice" data-tone="neutral">
          <span>
            Every idea in this run was rejected in review. That is a research outcome, not
            a failure of the run.
          </span>
        </p>
      ) : null}

      {/*
        The filter can empty the canvas, and on one real run it always does: an
        imported run rejected all eighteen of its ideas, so "hide rejected"
        leaves nothing at all. A full-size blank grid under a pressed toggle is
        the reader wondering what they broke; the words below are what happened.
        The toggle stays where it was, so undoing it is one click.
      */}
      {visible.length === 0 ? (
        <div className="ig__canvas ig__canvas--empty">
          <p className="tab-note">
            {allRejected
              ? `Every one of these ${graph.nodes.length} ideas was rejected, so hiding rejected ideas leaves nothing to draw.`
              : "Nothing is left to draw with rejected ideas hidden."}
          </p>
        </div>
      ) : (
        <div className="ig__canvas" ref={viewportRef}>
          {/*
            The stage exists so the canvas can be centred *and* scrolled.
            Centring a child directly inside the scroll box either squeezes it —
            an SVG asked for less width than its viewBox letterboxes itself,
            silently shrinking every label — or pushes its top edge above the
            scroll origin where nothing can reach it. A stage that is never
            smaller than either the graph or the viewport does both jobs and
            neither harm.
          */}
          <div className="ig__stage">
            <svg
              className="ig__svg"
              data-view={effectiveView}
              width={box.width}
              height={box.height}
              viewBox={`0 0 ${box.width} ${box.height}`}
              role="group"
              // Counts what is drawn, not what the run holds: with the filter on
              // it would otherwise announce eighteen ideas over a canvas showing
              // none of them. The full set is in the lineage list below either way.
              aria-label={`Idea genealogy — ${plural(visible.length, "idea")}${
                hideRejected ? `, ${rejectedCount} rejected hidden` : ""
              }`}
            >
              <defs>
                <marker
                  id={`${markerId}-arrow`}
                  viewBox="0 0 8 8"
                  refX="7.5"
                  refY="4"
                  markerWidth="8"
                  markerHeight="8"
                  markerUnits="userSpaceOnUse"
                  orient="auto"
                >
                  <path className="ig-arrow" d="M1,1 L7.5,4 L1,7 Z" />
                </marker>
                <marker
                  id={`${markerId}-arrow-hot`}
                  viewBox="0 0 8 8"
                  refX="7.5"
                  refY="4"
                  markerWidth="9"
                  markerHeight="9"
                  markerUnits="userSpaceOnUse"
                  orient="auto"
                >
                  <path className="ig-arrow ig-arrow--hot" d="M1,1 L7.5,4 L1,7 Z" />
                </marker>
              </defs>

              <g
                className="ig__plot"
                transform={`translate(${LABEL_SIDE} ${LABEL_ROOM})`}
              >
                {/*
                The layered view's whole subject is the round, and it used to
                carry it in vertical position alone with nothing on the canvas
                naming it. Aria-hidden because every lineage sentence already
                says which round its idea was written in.
              */}
                {bands.length > 0 ? (
                  <g className="ig__bands" aria-hidden="true">
                    {bands.map((row) => (
                      <g key={row.band}>
                        {/* The plot is inset for the labels, and a band rule is
                            about the canvas rather than about the plot — so it
                            is drawn back out to both real edges. */}
                        <line
                          className="ig-band__rule"
                          x1={-LABEL_SIDE}
                          x2={box.width - LABEL_SIDE}
                          y1={row.y}
                          y2={row.y}
                        />
                        <text
                          className="ig-band__label"
                          x={10 - LABEL_SIDE}
                          y={row.y - 8}
                        >
                          {row.label}
                        </text>
                      </g>
                    ))}
                  </g>
                ) : null}

                {/* Decoration: the same descent is in the lineage list as words. */}
                <g className="ig__edges" aria-hidden="true">
                  {drawnEdges.map(({ edge, d }) => {
                    const hot = selectedHid === edge.parent || selectedHid === edge.child;
                    return (
                      <path
                        key={`${edge.parent}->${edge.child}`}
                        className="ig-edge"
                        d={d}
                        data-child={edge.child}
                        data-parent={edge.parent}
                        data-operator={edge.operator ?? undefined}
                        data-hot={hot ? "true" : undefined}
                        markerEnd={`url(#${markerId}-arrow${hot ? "-hot" : ""})`}
                      />
                    );
                  })}
                </g>

                <g className="ig__nodes">
                  {visible.map((idea) => {
                    const spot = spots.get(idea.hid);
                    if (!spot) return null;
                    const selected = selectedHid === idea.hid;
                    const slot = idea.cluster
                      ? clusterSlots.get(idea.cluster)
                      : undefined;
                    return (
                      <g
                        key={idea.hid}
                        ref={(element) => {
                          if (element) refs.current.set(idea.hid, element);
                          else refs.current.delete(idea.hid);
                        }}
                        className="ig-node"
                        role="button"
                        tabIndex={tabStop === idea.hid ? 0 : -1}
                        aria-label={nodeLabel(idea)}
                        aria-current={selected ? "true" : undefined}
                        data-hid={idea.hid}
                        data-status={idea.status}
                        data-leader={idea.is_leader ? "true" : undefined}
                        data-duplicate={idea.duplicate_of ? "true" : undefined}
                        data-cluster={slot === undefined ? undefined : String(slot)}
                        data-selected={selected ? "true" : undefined}
                        transform={`translate(${spot.x} ${spot.y})`}
                        onClick={() => onSelect(idea.hid)}
                        onFocus={() => setActiveHid(idea.hid)}
                        onKeyDown={(event) => onKeyDown(event, idea.hid)}
                      >
                        <title>{tooltip(idea)}</title>
                        <circle className="ig-node__focus" r={spot.r + 7} />
                        {idea.is_leader || selected ? (
                          <circle className="ig-node__ring" r={spot.r + 4.5} />
                        ) : null}
                        <circle className="ig-node__disc" r={spot.r} />
                        {idea.status === "rejected" ? (
                          <line
                            className="ig-node__strike"
                            x1={-spot.r - 2.5}
                            x2={spot.r + 2.5}
                            y1={0}
                            y2={0}
                          />
                        ) : null}
                      </g>
                    );
                  })}
                </g>

                {/*
                  Every label, painted after every disc.

                  A label inside its own node's group is painted over by any
                  node later in DOM order — eight of fifty-nine on the largest
                  run, each one an idea that lost the only words it carries on
                  the canvas. Drawn as one layer on top, the halo in
                  `.ig-node__label` keeps each one readable over whatever it
                  crosses. The group still carries the node's status so the
                  encoding table in the CSS is unchanged, and takes no pointer
                  events so the circle beneath is still what a click lands on.

                  `idea.label` is read, not derived: the phrase was cut from the
                  title once, in `graphModel`, so the rule lives in one place and
                  the spacing above can be sized against it. What happens here is
                  formatting — `wrapLabel` is pure, cheap and total, and breaking
                  one string into at most two runs at paint time is the same kind
                  of work as choosing a `y`. A long label is drawn on two lines
                  rather than run wide, because horizontal room is what a force
                  layout is short of and `LABEL_ROOM` has already paid for the
                  second line at both ends of the canvas; a line that still will
                  not fit is cut by `wrapLabel` to the width `LABEL_SIDE` and
                  `BAND_SLOT` are derived from, so nothing drawn here can reach
                  past the canvas.
                */}
                <g className="ig__labels" aria-hidden="true">
                  {visible.map((idea) => {
                    const spot = spots.get(idea.hid);
                    if (!spot) return null;
                    const lines = wrapLabel(idea.label);
                    return (
                      <g
                        key={idea.hid}
                        className="ig-label"
                        data-status={idea.status}
                        data-leader={idea.is_leader ? "true" : undefined}
                        data-selected={selectedHid === idea.hid ? "true" : undefined}
                        transform={`translate(${spot.x} ${spot.y})`}
                      >
                        <text className="ig-node__label" y={spot.r + LABEL_BASELINE}>
                          {lines.map((line, index) => (
                            <tspan
                              key={line + String(index)}
                              x={0}
                              dy={index === 0 ? 0 : LABEL_LINE}
                            >
                              {line}
                            </tspan>
                          ))}
                        </text>
                      </g>
                    );
                  })}
                </g>
              </g>
            </svg>
          </div>
        </div>
      )}

      {/*
        The same story as words. It is not a second copy that can go stale — it
        is derived from `graph.nodes` in the same render as the circles are, and
        it is the only route to the information for a reader who is not looking
        at pixels. It lists every idea, including any the filter is hiding:
        hiding is a view, not a deletion.
      */}
      <ul className="visually-hidden" aria-label="Idea lineage">
        {graph.nodes.map((idea) => (
          <li key={idea.hid}>
            {lineageSentence(
              idea,
              parents.get(idea.hid) ?? [],
              byHid,
              graph.meta.lineage_recorded,
            )}
          </li>
        ))}
      </ul>

      <div className="ig__foot">
        {/* A legend for a canvas with nothing on it describes nothing. The
            toggle beside it stays, because that is the way back. */}
        <ul className="ig-legend" aria-label="Legend" hidden={visible.length === 0}>
          {marks.leader ? <LegendMark kind="leader" label="Leading" /> : null}
          {marks.active ? <LegendMark kind="active" label="In play" /> : null}
          {marks.rejected ? <LegendMark kind="rejected" label="Rejected" /> : null}
          {marks.merged ? <LegendMark kind="merged" label="Merged away" /> : null}
          {marks.setAside ? <LegendMark kind="archived" label="Set aside" /> : null}
          {edges.length > 0 ? <LegendMark kind="descent" label="Descent" /> : null}
          {graph.meta.has_clusters
            ? [...clusterSlots].map(([name, slot]) => (
                <LegendMark
                  key={name}
                  kind="cluster"
                  slot={slot}
                  label={clusterLabel(name)}
                />
              ))
            : null}
          {clusterCount > clusterSlots.size ? (
            <li className="ig-legend__item ig-legend__item--aside">
              {clusterCount - clusterSlots.size} smaller clusters, left unshaded
            </li>
          ) : null}
        </ul>

        <span className="spacer" />

        {rejectedCount > 0 ? (
          <button
            type="button"
            className="chip chip--button chip--quiet"
            aria-pressed={hideRejected}
            onClick={() => setHideRejected((on) => !on)}
          >
            {/* The label names what the next click does. Left as "Hide
                rejected" while pressed, the one control that would restore the
                graph reads as the control that emptied it. */}
            {hideRejected ? "Show rejected" : "Hide rejected"} · {rejectedCount}
          </button>
        ) : null}
      </div>

      <p className="ig__hint">
        Size is strength within this run only. Arrow keys move between ideas, up and down
        follow descent, Enter opens one.
      </p>
    </div>
  );
}

/* --- the legend ------------------------------------------------------------
   Every channel the drawing uses, named. The cluster swatches are the reason
   hue is allowed to mean anything at all: without the names beside them a
   colour-blind reader has six shades of nothing.
   ------------------------------------------------------------------------- */

function LegendMark({
  kind,
  label,
  slot,
}: {
  kind: "leader" | "active" | "rejected" | "merged" | "archived" | "descent" | "cluster";
  label: string;
  slot?: number;
}) {
  // `archived` and `merged` are two marks, not one. An idea set aside by hand is
  // hollow; a duplicate merged away is hollow *and* dashed. Six real runs draw
  // the first, and with only "Merged away" in the legend those six read as
  // ordinary survivors.
  const archived = kind === "merged" || kind === "archived";
  return (
    <li
      className={
        kind === "cluster"
          ? "ig-legend__item ig-legend__item--cluster"
          : "ig-legend__item"
      }
    >
      <svg
        className="ig-legend__mark"
        width="20"
        height="20"
        viewBox="-10 -10 20 20"
        aria-hidden="true"
      >
        {kind === "descent" ? (
          <path className="ig-legend__edge" d="M-8,4 L8,-4" />
        ) : (
          <g
            className="ig-node"
            data-status={
              kind === "rejected" ? "rejected" : archived ? "archived" : "active"
            }
            data-leader={kind === "leader" ? "true" : undefined}
            data-duplicate={kind === "merged" ? "true" : undefined}
            data-cluster={slot === undefined ? undefined : String(slot)}
          >
            {kind === "leader" ? <circle className="ig-node__ring" r={7.5} /> : null}
            <circle className="ig-node__disc" r={5.5} />
            {kind === "rejected" ? (
              <line className="ig-node__strike" x1={-7} x2={7} y1={0} y2={0} />
            ) : null}
          </g>
        )}
      </svg>
      <span>{label}</span>
    </li>
  );
}

/* --- words -----------------------------------------------------------------
   Three registers of the same fact, on purpose: the accessible name is what a
   screen reader announces when focus lands on a circle, the `<title>` is the
   tooltip a mouse gets with no JavaScript involved, and the lineage sentence is
   the full record in the text alternative. They are generated from one node, so
   they cannot disagree.
   ------------------------------------------------------------------------- */

function fateOf(idea: GraphNode): string {
  if (idea.status === "archived" && idea.duplicate_of) {
    return `set aside, merged into ${idea.duplicate_of}`;
  }
  const status = describeHypothesisStatus(idea.status).label.toLowerCase();
  return idea.is_leader ? `${status}, currently leading` : status;
}

function nodeLabel(idea: GraphNode): string {
  return `${idea.hid} — ${idea.title}. ${sentence(fateOf(idea))} Strength ${formatElo(idea.elo)}.`;
}

function tooltip(idea: GraphNode): string {
  const parts = [
    `${idea.hid} · ${idea.title}`,
    sentence(fateOf(idea)),
    `Strength ${formatElo(idea.elo)}`,
  ];
  if (idea.matches > 0)
    parts.push(`${idea.wins} of ${plural(idea.matches, "match", "matches")} won`);
  if (idea.cluster) parts.push(clusterLabel(idea.cluster));
  return parts.join(" — ");
}

/**
 * One idea's whole record: where it came from, by which operator, and how it
 * ended. When a run never recorded descent, this says nothing about parentage —
 * "written from scratch" would be a claim the database cannot support, and the
 * note above the canvas has already said the descent is missing rather than
 * absent.
 */
function lineageSentence(
  idea: GraphNode,
  parentHids: string[],
  byHid: Map<string, GraphNode>,
  lineageRecorded: boolean,
): string {
  const parts = [`${idea.hid} — ${idea.title}.`, `Round ${idea.created_round}.`];

  if (lineageRecorded) {
    if (parentHids.length > 0) {
      parts.push(`Evolved from ${andList(parentHids)}.`);
      // The operator gets its own sentence rather than being glued on with
      // "by": `lib/status` maps it to a whole clause ("Combined two ideas"),
      // and no raw enum is allowed on screen to make the grammar tidier.
      if (idea.operator) parts.push(`${describeOperator(idea.operator).label}.`);
    } else {
      parts.push("Written from scratch.");
    }
  }

  parts.push(`${sentence(fateOf(idea))}`);
  parts.push(
    idea.matches > 0
      ? `Strength ${formatElo(idea.elo)} from ${plural(idea.matches, "match", "matches")}, ${idea.wins} won.`
      : `Strength ${formatElo(idea.elo)}, no matches played.`,
  );
  if (idea.cluster) parts.push(`${clusterLabel(idea.cluster)}.`);
  if (idea.source === "human") parts.push("Added by hand.");
  if (idea.duplicate_of && !byHid.has(idea.duplicate_of)) {
    parts.push("The idea it merged into is not part of this run.");
  }
  return parts.join(" ");
}

function sentence(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

function andList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/* --- geometry -------------------------------------------------------------- */

/**
 * Parent to child, trimmed to the two circles so the arrowhead lands on the
 * rim rather than under the node, and bowed so that the two parents of a
 * combination arrive as two visibly separate arrows. That convergence is the
 * signature of evolution in this view; drawn straight, both edges would overlap
 * into one thick line and the run would look like it never combined anything.
 */
function organicPath(from: Placed, to: Placed, bow: number): string {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;

  const sx = from.x + ux * (from.r + 1.5);
  const sy = from.y + uy * (from.r + 1.5);
  const ex = to.x - ux * (to.r + 2.5);
  const ey = to.y - uy * (to.r + 2.5);

  if (bow === 0) return `M${r1(sx)},${r1(sy)} L${r1(ex)},${r1(ey)}`;
  const cx = (sx + ex) / 2 - uy * bow;
  const cy = (sy + ey) / 2 + ux * bow;
  return `M${r1(sx)},${r1(sy)} Q${r1(cx)},${r1(cy)} ${r1(ex)},${r1(ey)}`;
}

/**
 * The same descent in the layered view, where a straight line is a trap.
 *
 * Between bands the edge leaves the parent downward and enters the child
 * downward, so the arrowhead reads as "this became that" rather than as a
 * diagonal crossing three other ideas. Inside a band — imported runs routinely
 * record a parent in the same round, and a run with one round has nothing but
 * these — it hops *over* the row instead of drawing a straight line through
 * every idea standing between the two.
 */
function layeredPath(from: Placed, to: Placed): string {
  const dy = to.y - from.y;

  if (Math.abs(dy) > 18) {
    const down = dy > 0 ? 1 : -1;
    const sy = from.y + down * (from.r + 1.5);
    const ey = to.y - down * (to.r + 2.5);
    const my = (sy + ey) / 2;
    return `M${r1(from.x)},${r1(sy)} C${r1(from.x)},${r1(my)} ${r1(to.x)},${r1(my)} ${r1(to.x)},${r1(ey)}`;
  }

  const dir = to.x >= from.x ? 1 : -1;
  const sx = from.x + dir * from.r * 0.72;
  const sy = from.y - from.r * 0.72;
  const ex = to.x - dir * to.r * 0.86;
  const ey = to.y - to.r * 0.86;
  const reach = Math.abs(ex - sx);
  const lift = Math.min(
    66,
    Math.max(24, reach * 0.3),
    Math.max(12, Math.min(sy, ey) - 6),
  );
  return `M${r1(sx)},${r1(sy)} Q${r1((sx + ex) / 2)},${r1(Math.min(sy, ey) - lift)} ${r1(ex)},${r1(ey)}`;
}

/** Spread the parents of one child evenly either side of the straight line. */
function bowOf(index: number, siblings: number, from: Placed, to: Placed): number {
  if (siblings < 2) return 0;
  const span = Math.min(30, Math.hypot(to.x - from.x, to.y - from.y) * 0.16);
  return (index - (siblings - 1) / 2) * span;
}

function r1(value: number): number {
  return Math.round(value * 10) / 10;
}

/* --- pins ------------------------------------------------------------------ */

type Pins = Record<string, { x: number; y: number }>;

/** The last solved positions, and the canvas they were solved in. */
interface Remembered {
  box: LayoutBox;
  at: Pins;
}

/**
 * Pins carried into a canvas of a different size.
 *
 * A pin is a coordinate, and a coordinate means nothing without the box it was
 * measured in. The old code drew the right conclusion from that — pins from a
 * 1,440px canvas are meaningless on a 900px one — and then acted on it in the
 * one way that guarantees the worst outcome, by throwing them away and
 * re-solving from a fresh seeded start. Scaling them instead keeps every
 * neighbour relationship the reader has already learned; the graph looks like
 * it grew, which is what it did. Nodes still push each other apart afterwards,
 * so a rescale that crowds two of them is corrected in the same solve.
 */
function rescalePins(
  remembered: Remembered | undefined,
  box: LayoutBox,
): Pins | undefined {
  if (!remembered) return undefined;
  const { box: was, at } = remembered;
  if (was.width === box.width && was.height === box.height) return at;
  if (was.width <= 0 || was.height <= 0) return undefined;
  const sx = box.width / was.width;
  const sy = box.height / was.height;
  const out: Pins = {};
  for (const [hid, spot] of Object.entries(at)) {
    out[hid] = { x: spot.x * sx, y: spot.y * sy };
  }
  return out;
}

function clamp(low: number, value: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/* --- grouping -------------------------------------------------------------- */

function groupBy<T>(
  items: readonly T[],
  keyOf: (item: T) => string,
  valueOf: (item: T) => string,
): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const item of items) {
    const key = keyOf(item);
    const bucket = out.get(key);
    if (bucket) bucket.push(valueOf(item));
    else out.set(key, [valueOf(item)]);
  }
  return out;
}

/**
 * Cluster name to hue slot, assigned in sorted order so the same run always
 * paints the same cluster the same colour — including after a refetch that
 * returns the clusters in a different order.
 */
function clusterHues(nodes: readonly GraphNode[]): {
  slots: Map<string, number>;
  total: number;
} {
  const counts = new Map<string, number>();
  for (const node of nodes) {
    if (node.cluster) counts.set(node.cluster, (counts.get(node.cluster) ?? 0) + 1);
  }
  const ranked = [...counts.entries()].sort(
    (a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0),
  );
  return {
    slots: new Map(ranked.slice(0, CLUSTER_SLOTS).map(([name], index) => [name, index])),
    total: counts.size,
  };
}

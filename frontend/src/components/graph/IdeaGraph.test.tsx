import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RunGraph } from "./graphModel";
import {
  makeAllRejectedGraph,
  makeGraph,
  makeGraphNode,
  makeLargeGraph,
  makeNoClusterGraph,
  makeNoLineageGraph,
  makeNumericClusterGraph,
  makeSetAsideGraph,
  makeSingleNodeGraph,
  makeUnevolvedGraph,
} from "../../test/graphFixtures";
import { IdeaGraph } from "./IdeaGraph";
import { wrapLabel } from "./shortLabel";

/**
 * What the canvas has to be right about.
 *
 * jsdom cannot tell anybody whether this is pretty — that is what the browser
 * walk is for. What it *can* hold to account is everything the design says the
 * drawing must never lie about: that every idea is drawn and reachable, that a
 * fate is legible without colour, that the lineage exists as text for a reader
 * who is not looking at pixels, and that the four honest-but-awkward runs in
 * the real database (no lineage, no clusters, all rejected, a single idea)
 * render as themselves rather than as a broken empty canvas.
 *
 * Every expectation is derived from the fixture handed in, so a change to the
 * fixture cannot quietly stop a test from asserting anything.
 */

function props(overrides: Partial<Parameters<typeof IdeaGraph>[0]> = {}) {
  return {
    graph: makeGraph(),
    view: "organic" as const,
    selectedHid: null,
    onSelect: vi.fn(),
    seed: "run-20260808-idea-graph",
    live: false,
    ...overrides,
  };
}

/** Every drawn idea, as the accessibility tree sees it. */
function nodes(): HTMLElement[] {
  return screen.getAllByRole("button", { name: /^h\d{3}/ });
}

function node(hid: string): HTMLElement {
  return screen.getByRole("button", { name: new RegExp(`^${hid}\\b`) });
}

function lineageList(): HTMLElement {
  return screen.getByRole("list", { name: /idea lineage/i });
}

/** The one lineage sentence that is about `hid`, not merely mentions it. */
function lineageItem(hid: string): HTMLElement {
  const item = within(lineageList())
    .getAllByRole("listitem")
    .find((row) => row.textContent?.startsWith(`${hid} `));
  if (!item) throw new Error(`the lineage list never mentions ${hid}`);
  return item;
}

function translateOf(element: Element | null): { x: number; y: number } {
  const match = /translate\(\s*([-\d.]+)[ ,]+([-\d.]+)\s*\)/.exec(
    element?.getAttribute("transform") ?? "",
  );
  return match ? { x: Number(match[1]), y: Number(match[2]) } : { x: 0, y: 0 };
}

/** A node's position in canvas coordinates — the plot is inset for the labels. */
function positionOf(element: Element): { x: number; y: number } {
  if (!element.getAttribute("transform")) {
    throw new Error(`no transform on ${element.getAttribute("data-hid")}`);
  }
  const node = translateOf(element);
  const plot = translateOf(element.closest("g.ig__plot"));
  return { x: node.x + plot.x, y: node.y + plot.y };
}

/** The drawing itself. Typed as an HTMLElement so `within` will scope to it. */
function canvas(): HTMLElement {
  const svg = document.querySelector("svg.ig__svg");
  if (!svg) throw new Error("the graph drew no canvas");
  return svg as unknown as HTMLElement;
}

describe("IdeaGraph — what is drawn", () => {
  it("renders one node per hypothesis and one edge per parent link", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    expect(nodes()).toHaveLength(base.graph.nodes.length);
    expect(document.querySelectorAll("path.ig-edge")).toHaveLength(
      base.graph.edges.length,
    );
  });

  it("labels a node with a short phrase, never with its 90-character title", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const drawn = [...document.querySelectorAll("text.ig-node__label")].map(
      (text) => text.textContent,
    );
    // The label the model derived, not the hid and not the title. Read off the
    // fixture rather than written out, so the assertion cannot drift from it.
    expect(drawn).toEqual(base.graph.nodes.map((idea) => wrapLabel(idea.label).join("")));

    for (const idea of base.graph.nodes) {
      expect(idea.label).not.toBe(idea.hid);
      expect(idea.label.length).toBeLessThan(idea.title.length);
      expect(within(canvas()).queryByText(idea.title)).not.toBeInTheDocument();
    }
  });

  it("keeps the hid reachable from every circle, in words", () => {
    // The canvas stopped saying `h001`, so the identifier every other surface
    // in the app uses has to survive somewhere a reader can get to without one.
    const base = props();
    render(<IdeaGraph {...base} />);

    for (const idea of base.graph.nodes) {
      const drawn = node(idea.hid);
      expect(drawn).toHaveAccessibleName(new RegExp(`^${idea.hid}\\b`));
      expect(drawn.querySelector("title")?.textContent).toContain(idea.hid);
    }
  });

  it("wraps a long label onto two lines rather than running it wide", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const long = base.graph.nodes.filter((idea) => idea.label.length > 14);
    expect(long.length).toBeGreaterThan(0);

    const lines = new Map(
      [...document.querySelectorAll("g.ig__labels g.ig-label")].map((group) => [
        [...group.querySelectorAll("tspan")].map((t) => t.textContent).join(" "),
        group.querySelectorAll("tspan").length,
      ]),
    );
    // Two lines, and the pieces still rejoin into exactly the derived label.
    for (const idea of long) expect(lines.get(idea.label)).toBe(2);
    for (const idea of base.graph.nodes) {
      if (idea.label.length <= 14) expect(lines.get(idea.label)).toBe(1);
    }
  });

  it("carries the full title in the hover tooltip and the accessible name", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const first = base.graph.nodes[0];
    expect(node(first.hid)).toHaveAccessibleName(new RegExp(first.title));
    expect(node(first.hid).querySelector("title")?.textContent).toContain(first.title);
  });

  it("draws two arrows converging on a combination", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const combination = base.graph.nodes.find((idea) => idea.operator === "combination")!;
    const converging = document.querySelectorAll(
      `path.ig-edge[data-child="${combination.hid}"]`,
    );
    expect(converging).toHaveLength(2);
    // Bowed apart, or a two-parent combination reads as one thick line.
    const [left, right] = [...converging].map((edge) => edge.getAttribute("d"));
    expect(left).not.toEqual(right);
  });

  it("hands the edges to the drawing only, since the list carries the descent", () => {
    render(<IdeaGraph {...props()} />);
    expect(document.querySelector("g.ig__edges")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("IdeaGraph — encoding a fate without colour", () => {
  it("marks a rejected idea as rejected", () => {
    render(<IdeaGraph {...props()} />);

    const rejected = node("h002");
    expect(rejected).toHaveAttribute("data-status", "rejected");
    // Struck through: a shape, not a hue.
    expect(rejected.querySelector("line.ig-node__strike")).toBeInTheDocument();
  });

  it("marks the leader, and marks only one", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const leaders = canvas().querySelectorAll('g.ig-node[data-leader="true"]');
    expect(leaders).toHaveLength(1);
    expect(leaders[0]).toHaveAttribute(
      "data-hid",
      base.graph.nodes.find((idea) => idea.is_leader)!.hid,
    );
    expect(leaders[0]).toHaveAccessibleName(/leading/i);
  });

  it("marks a merged duplicate as hollow and names what it merged into", () => {
    render(<IdeaGraph {...props()} />);

    const merged = node("h004");
    expect(merged).toHaveAttribute("data-status", "archived");
    expect(merged).toHaveAttribute("data-duplicate", "true");
    expect(merged).toHaveAccessibleName(/merged into h003/i);
  });

  it("sizes a node from its Elo, within this run only", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const radius = (hid: string): number =>
      Number(node(hid).querySelector("circle.ig-node__disc")!.getAttribute("r"));

    const ranked = [...base.graph.nodes].sort((a, b) => a.elo - b.elo);
    expect(radius(ranked[0].hid)).toBeLessThan(radius(ranked[ranked.length - 1].hid));
  });

  it("gives every idea in one cluster the same hue slot, and different clusters different ones", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const slot = (hid: string): string | null => node(hid).getAttribute("data-cluster");
    const byCluster = new Map<string, Set<string | null>>();
    for (const idea of base.graph.nodes) {
      if (!idea.cluster) continue;
      const seen = byCluster.get(idea.cluster) ?? new Set();
      seen.add(slot(idea.hid));
      byCluster.set(idea.cluster, seen);
    }
    for (const seen of byCluster.values()) expect(seen.size).toBe(1);
    const slots = new Set([...byCluster.values()].map((seen) => [...seen][0]));
    expect(slots.size).toBe(byCluster.size);
  });

  it("names each cluster in the legend, so hue is never the only signal", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const legend = screen.getByRole("list", { name: /legend/i });
    for (const name of new Set(
      base.graph.nodes.map((idea) => idea.cluster).filter(Boolean),
    )) {
      expect(within(legend).getByText(String(name))).toBeInTheDocument();
    }
  });

  it("paints every label above every disc, so no idea loses its words", () => {
    // Inside its own node's group a label is painted over by any node later in
    // DOM order — eight of fifty-nine on the largest run in the database.
    render(<IdeaGraph {...props({ graph: makeLargeGraph(59) })} />);

    const labels = document.querySelector("g.ig__labels");
    const discs = document.querySelector("g.ig__nodes");
    expect(labels).not.toBeNull();
    expect(document.querySelectorAll("g.ig__labels text.ig-node__label")).toHaveLength(
      59,
    );
    expect(discs!.compareDocumentPosition(labels!)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(discs!.querySelector("text.ig-node__label")).toBeNull();
  });
});

describe("IdeaGraph — the text alternative", () => {
  it("names every idea, its parents and its fate", () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const list = lineageList();
    expect(within(list).getAllByRole("listitem")).toHaveLength(base.graph.nodes.length);
    expect(within(list).getByText(/h006/)).toHaveTextContent(/from h001 and h003/i);
    expect(within(list).getByText(/h005/)).toHaveTextContent(/from h001/i);
    expect(within(list).getByText(/h002/)).toHaveTextContent(/rejected/i);
    expect(within(list).getByText(/h004/)).toHaveTextContent(/merged into h003/i);
  });

  it("says an idea was written from scratch when the run did record descent", () => {
    render(<IdeaGraph {...props()} />);
    expect(lineageItem("h003")).toHaveTextContent(/written from scratch/i);
  });

  it("never claims an idea was written from scratch when descent was never recorded", () => {
    render(<IdeaGraph {...props({ graph: makeNoLineageGraph() })} />);
    expect(within(lineageList()).queryByText(/written from scratch/i)).toBeNull();
  });

  it("names the operator that produced an evolved idea", () => {
    render(<IdeaGraph {...props()} />);
    expect(within(lineageList()).getByText(/h006/)).toHaveTextContent(/combin/i);
  });
});

describe("IdeaGraph — the honest runs", () => {
  it("says so when a run has no recorded lineage", () => {
    const graph = makeNoLineageGraph();
    render(<IdeaGraph {...props({ graph })} />);

    expect(screen.getByText(/lineage was not recorded/i)).toBeInTheDocument();
    expect(nodes()).toHaveLength(graph.nodes.length);
  });

  it("says something else about a modern run that has evolved nothing yet", () => {
    // Two runs with the same nodes and the same absence of edges, and only the
    // endpoint knows which absence is which. Telling an app run its lineage was
    // "not recorded" describes a data loss that did not happen.
    render(<IdeaGraph {...props({ graph: makeUnevolvedGraph() })} />);

    expect(screen.queryByText(/lineage was not recorded/i)).not.toBeInTheDocument();
    expect(screen.getByText(/no idea in this run descended/i)).toBeInTheDocument();
    expect(lineageItem("h001")).toHaveTextContent(/written from scratch/i);
  });

  it("claims nothing about descent while the run can still produce some", () => {
    // Round one has no parents by definition. Said out loud at three seconds
    // and withdrawn at eleven, it is a claim followed by a correction.
    render(<IdeaGraph {...props({ graph: makeUnevolvedGraph(), live: true })} />);

    expect(screen.queryByText(/lineage was not recorded/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no idea in this run descended/i)).not.toBeInTheDocument();
  });

  it("reads an all-rejected run as a result rather than an error", () => {
    const graph = makeAllRejectedGraph();
    render(<IdeaGraph {...props({ graph })} />);

    expect(nodes()).toHaveLength(graph.nodes.length);
    expect(canvas().querySelectorAll('g.ig-node[data-leader="true"]')).toHaveLength(0);
    // Stated as an outcome, not as a problem: `neutral`, never `caution`.
    const notice = screen.getByText(/every idea .* was rejected/i).closest("p");
    expect(notice).toHaveAttribute("data-tone", "neutral");
  });

  it("centres a lone idea instead of drawing an empty canvas", () => {
    render(<IdeaGraph {...props({ graph: makeSingleNodeGraph() })} />);

    const drawn = nodes();
    expect(drawn).toHaveLength(1);
    const box = canvas();
    const { x, y } = positionOf(drawn[0]);
    expect(x).toBeCloseTo(Number(box.getAttribute("width")) / 2, 0);
    expect(y).toBeCloseTo(Number(box.getAttribute("height")) / 2, 0);
  });

  it("says what is true when a run recorded no ideas at all", () => {
    const graph: RunGraph = {
      run_id: "6f1c9a2e-0000-4000-8000-000000000001",
      nodes: [],
      edges: [],
      meta: {
        rounds: 1,
        has_lineage: false,
        lineage_recorded: true,
        has_clusters: false,
        elo_min: 0,
        elo_max: 0,
        node_count: 0,
      },
    };
    render(<IdeaGraph {...props({ graph })} />);

    expect(screen.getByText(/no ideas/i)).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /^h\d{3}/ })).toHaveLength(0);
  });

  it("falls back to the layered view past the organic view's useful range", () => {
    const graph = makeLargeGraph(180);
    render(<IdeaGraph {...props({ graph, view: "organic" })} />);

    expect(screen.getByText(/too many ideas/i)).toBeInTheDocument();
    expect(canvas()).toHaveAttribute("data-view", "layered");
  });

  it("does not nag about scale on a run the organic view handles", () => {
    render(<IdeaGraph {...props({ graph: makeLargeGraph(59) })} />);

    expect(screen.queryByText(/too many ideas/i)).not.toBeInTheDocument();
    expect(canvas()).toHaveAttribute("data-view", "organic");
  });
});

describe("IdeaGraph — the two views", () => {
  it("puts every idea of one round on one line in the layered view", () => {
    const base = props({ view: "layered" as const });
    render(<IdeaGraph {...base} />);

    const rowsOf = (round: number): number[] =>
      base.graph.nodes
        .filter((idea) => idea.created_round === round)
        .map((idea) => positionOf(node(idea.hid)).y);

    expect(new Set(rowsOf(1)).size).toBe(1);
    expect(new Set(rowsOf(2)).size).toBe(1);
    expect(rowsOf(1)[0]).toBeLessThan(rowsOf(2)[0]);
  });

  it("lays the same run out identically twice from the same seed", () => {
    const base = props();
    const first = render(<IdeaGraph {...base} />);
    const before = nodes().map(positionOf);
    first.unmount();

    render(<IdeaGraph {...base} graph={makeGraph()} />);
    expect(nodes().map(positionOf)).toEqual(before);
  });

  it("lays a different run out differently", () => {
    const base = props();
    const first = render(<IdeaGraph {...base} />);
    const before = nodes().map(positionOf);
    first.unmount();

    render(<IdeaGraph {...base} seed="a-different-run" />);
    expect(nodes().map(positionOf)).not.toEqual(before);
  });
});

describe("IdeaGraph — reaching an idea", () => {
  it("selects a node on Enter", async () => {
    const onSelect = vi.fn();
    render(<IdeaGraph {...props({ onSelect })} />);

    node("h001").focus();
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("h001");
  });

  it("selects a node on Space and on click", async () => {
    const onSelect = vi.fn();
    render(<IdeaGraph {...props({ onSelect })} />);

    node("h003").focus();
    await userEvent.keyboard(" ");
    expect(onSelect).toHaveBeenLastCalledWith("h003");

    await userEvent.click(node("h005"));
    expect(onSelect).toHaveBeenLastCalledWith("h005");
  });

  it("clears the selection on Escape, without losing focus", async () => {
    const onSelect = vi.fn();
    render(<IdeaGraph {...props({ onSelect, selectedHid: "h002" })} />);

    node("h002").focus();
    await userEvent.keyboard("{Escape}");
    expect(onSelect).toHaveBeenCalledWith(null);
    expect(document.activeElement).toBe(node("h002"));
  });

  it("moves between ideas with the left and right arrows", async () => {
    render(<IdeaGraph {...props()} />);

    node("h001").focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(node("h002"));
    await userEvent.keyboard("{ArrowLeft}");
    expect(document.activeElement).toBe(node("h001"));
  });

  it("climbs to a parent and drops to a child with the up and down arrows", async () => {
    render(<IdeaGraph {...props()} />);

    node("h006").focus();
    await userEvent.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(node("h001"));
    await userEvent.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(node("h005"));
  });

  it("offers one tab stop rather than fifty-nine", () => {
    render(<IdeaGraph {...props({ graph: makeLargeGraph(59) })} />);
    expect(document.querySelectorAll('g.ig-node[tabindex="0"]')).toHaveLength(1);
  });

  it("makes the selected idea the tab stop, and marks it", () => {
    render(<IdeaGraph {...props({ selectedHid: "h005" })} />);

    const selected = node("h005");
    expect(selected).toHaveAttribute("tabindex", "0");
    expect(selected).toHaveAttribute("data-selected", "true");
    expect(document.querySelectorAll('g.ig-node[tabindex="0"]')).toHaveLength(1);
  });

  it("lights the descent that touches the selected idea", () => {
    render(<IdeaGraph {...props({ selectedHid: "h006" })} />);

    const hot = document.querySelectorAll('path.ig-edge[data-hot="true"]');
    expect(hot).toHaveLength(2);
    for (const edge of hot) expect(edge.getAttribute("data-child")).toBe("h006");
  });
});

describe("IdeaGraph — hiding what was rejected", () => {
  it("shows rejected ideas by default", () => {
    const base = props();
    render(<IdeaGraph {...base} />);
    expect(nodes()).toHaveLength(base.graph.nodes.length);
  });

  it("hides them on request, and says how many it hid", async () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const rejected = base.graph.nodes.filter((idea) => idea.status === "rejected");
    const toggle = screen.getByRole("button", { name: /hide rejected/i });
    await userEvent.click(toggle);

    expect(nodes()).toHaveLength(base.graph.nodes.length - rejected.length);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    // The lineage text keeps every idea: hiding is a view, not a deletion.
    expect(within(lineageList()).getAllByRole("listitem")).toHaveLength(
      base.graph.nodes.length,
    );
  });

  it("does not offer the filter when nothing was rejected", () => {
    render(<IdeaGraph {...props({ graph: makeSingleNodeGraph() })} />);
    expect(screen.queryByRole("button", { name: /hide rejected/i })).toBeNull();
  });

  it("names what the next click does rather than what the last one did", async () => {
    render(<IdeaGraph {...props()} />);

    const toggle = screen.getByRole("button", { name: /hide rejected/i });
    await userEvent.click(toggle);
    expect(toggle).toHaveTextContent(/show rejected/i);
    await userEvent.click(toggle);
    expect(toggle).toHaveTextContent(/hide rejected/i);
  });

  it("says why the canvas is empty when the filter hides everything", async () => {
    // The real run e4fb1dc6 rejected eighteen of eighteen, so this is not a
    // contrived filter — it is the only thing that run's toggle can do.
    const graph = makeAllRejectedGraph();
    render(<IdeaGraph {...props({ graph })} />);

    await userEvent.click(screen.getByRole("button", { name: /hide rejected/i }));

    expect(screen.queryAllByRole("button", { name: /^h\d{3}/ })).toHaveLength(0);
    expect(screen.getByText(/leaves nothing to draw/i)).toBeInTheDocument();
    // And the way back is still on screen.
    expect(screen.getByRole("button", { name: /show rejected/i })).toBeInTheDocument();
  });

  it("never announces ideas it is not drawing", async () => {
    const base = props();
    render(<IdeaGraph {...base} />);

    const before = canvas().getAttribute("aria-label");
    expect(before).toMatch(/6 ideas/);
    await userEvent.click(screen.getByRole("button", { name: /hide rejected/i }));
    expect(canvas().getAttribute("aria-label")).toMatch(/5 ideas/);
  });
});

describe("IdeaGraph — the legend describes this run", () => {
  it("names the mark an idea set aside without being merged carries", () => {
    // Hollow but not dashed. Six of twenty-six nodes on run 48c73e86 look like
    // this, and with only "Merged away" in the legend they read as survivors.
    render(<IdeaGraph {...props({ graph: makeSetAsideGraph() })} />);

    const legend = screen.getByRole("list", { name: /legend/i });
    expect(within(legend).getByText(/set aside/i)).toBeInTheDocument();
  });

  it("does not offer that mark to a run with nothing set aside", () => {
    render(<IdeaGraph {...props()} />);

    const legend = screen.getByRole("list", { name: /legend/i });
    expect(within(legend).queryByText(/set aside/i)).not.toBeInTheDocument();
    expect(within(legend).getByText(/merged away/i)).toBeInTheDocument();
  });

  it("claims no channel the run does not use", () => {
    // Everything rejected, nothing clustered, no descent: one mark is true.
    render(<IdeaGraph {...props({ graph: makeAllRejectedGraph() })} />);

    const legend = screen.getByRole("list", { name: /legend/i });
    const items = within(legend).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["Rejected"]);
  });

  it("claims nothing about hue when the run never clustered", () => {
    render(<IdeaGraph {...props({ graph: makeNoClusterGraph() })} />);

    const legend = screen.getByRole("list", { name: /legend/i });
    // Five status/descent marks and not one swatch. Asserted by count because
    // the fixture's cluster names never contain the word "cluster" either, so
    // searching for it passes just as well on a run that *is* clustered.
    expect(within(legend).getAllByRole("listitem")).toHaveLength(5);
    expect(document.querySelector("g.ig-node[data-cluster]")).toBeNull();
  });

  it("names a numeric cluster in the reader's language, not the index", () => {
    render(<IdeaGraph {...props({ graph: makeNumericClusterGraph() })} />);

    const legend = screen.getByRole("list", { name: /legend/i });
    expect(within(legend).getByText("Cluster 0")).toBeInTheDocument();
    expect(within(legend).queryByText("0")).not.toBeInTheDocument();
    expect(node("h001").querySelector("title")?.textContent).toContain("Cluster 0");
  });
});

describe("IdeaGraph — growing without reshuffling", () => {
  /** Every drawn node's position, by hid. */
  function placements(): Record<string, { x: number; y: number }> {
    return Object.fromEntries(
      [...document.querySelectorAll("g.ig-node[data-hid]")].map((element) => [
        element.getAttribute("data-hid"),
        positionOf(element),
      ]),
    );
  }

  function growing(count: number) {
    const nodes = Array.from({ length: count }, (_, i) =>
      makeGraphNode({
        hid: `h${String(i + 1).padStart(3, "0")}`,
        id: `hyp-uuid-${i + 1}`,
        title: `Idea ${i + 1}`,
        elo: 1200 + (i % 7) * 4,
      }),
    );
    return makeGraph({
      nodes,
      edges: [],
      meta: { ...makeGraph().meta, has_lineage: false, node_count: count },
    });
  }

  it("leaves every settled idea exactly where it was when a new one arrives", () => {
    // The property the design calls non-negotiable, asserted through the
    // component rather than through the layout function: the unit test pinned a
    // fixed box, and the box was what the component kept changing.
    const base = props({ graph: growing(12), live: true });
    const view = render(<IdeaGraph {...base} />);
    const before = placements();

    view.rerender(<IdeaGraph {...base} graph={growing(13)} />);
    const after = placements();

    for (const hid of Object.keys(before)) {
      expect(after[hid]).toEqual(before[hid]);
    }
    expect(after.h013).toBeDefined();
  });

  it("keeps the settled picture at the instant the run finishes", () => {
    // `run_finished` flips `live` false. It used to discard every pin with it,
    // so the last thing a reader saw was the whole graph rearranging itself.
    const graph = growing(12);
    const base = props({ graph, live: true });
    const view = render(<IdeaGraph {...base} />);
    const before = placements();

    view.rerender(<IdeaGraph {...base} graph={graph} live={false} />);

    expect(placements()).toEqual(before);
  });

  it("still lays a run out from the seed alone on a fresh page", () => {
    // Pins survive within one mounted graph and never outlive it, so the same
    // run opened tomorrow is the same picture.
    const first = render(<IdeaGraph {...props({ graph: growing(12), live: true })} />);
    const before = placements();
    first.unmount();

    render(<IdeaGraph {...props({ graph: growing(12), live: false })} />);
    expect(placements()).toEqual(before);
  });
});

describe("IdeaGraph — the layered view says which round is which", () => {
  it("labels every band it draws", () => {
    const base = props({ view: "layered" as const });
    render(<IdeaGraph {...base} />);

    const labels = [...document.querySelectorAll("text.ig-band__label")].map(
      (text) => text.textContent,
    );
    expect(labels).toEqual(["Round 1", "Round 2"]);
  });

  it("names both rounds when a band holds two, rather than picking one", () => {
    // Runs 319f3128 and c6223130 hold round-0 and round-1 ideas, and the layout
    // files both in the first band. The rail says "Round 0" for one of them, so
    // a band labelled "Round 1" would put the picture and the words in conflict.
    const base = props({ view: "layered" as const });
    const graph = makeGraph({
      nodes: makeNodes0(),
      edges: [],
      meta: { ...makeGraph().meta, has_lineage: false, rounds: 2 },
    });
    render(<IdeaGraph {...base} graph={graph} />);

    const labels = [...document.querySelectorAll("text.ig-band__label")].map(
      (text) => text.textContent,
    );
    expect(labels[0]).toBe("Rounds 0–1");
  });

  it("draws no bands in the organic view, where rounds are not the axis", () => {
    render(<IdeaGraph {...props()} />);
    expect(document.querySelectorAll("text.ig-band__label")).toHaveLength(0);
  });
});

/** {@link makeNodes}, with h001 written in round 0 as several imports record it. */
function makeNodes0() {
  return makeGraph().nodes.map((idea, i) =>
    i === 0 ? makeGraphNode({ ...idea, created_round: 0 }) : idea,
  );
}

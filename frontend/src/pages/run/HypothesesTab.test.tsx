import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { RunDetail, RunSummary } from "../../api/types";
import { makeRunDetail, makeRunSummary, mockFetchRoutes } from "../../test/fixtures";
import {
  makeGraph,
  makeMeta,
  makeNoLineageGraph,
  makeNumericClusterGraph,
  makeUnevolvedGraph,
} from "../../test/graphFixtures";
import { MockEventSource } from "../../test/mockEventSource";
import { makeHypothesisDetail, makeHypothesisRow } from "../../test/runFixtures";
import { useRunEvents } from "../../store/runs";
import { HypothesesTab } from "./HypothesesTab";

/**
 * The Ideas tab as a whole: three ways of looking at one run's ideas, which one
 * a link opens on, how an idea gets read, and what happens while the run is
 * still producing them.
 *
 * What the drawing itself must be right about is `IdeaGraph.test.tsx`'s
 * business. What is asserted here is only the wiring the tab owns — the view
 * switch and its URL, the detail rail's single fetch, the live stream reaching
 * the graph without a refetch per event, and the honest fallback when the
 * genealogy cannot be loaded but the ideas can still be listed.
 */

type Route = Parameters<typeof mockFetchRoutes>[0][number];

const RUN_ID = "run-1";

function stub(extra: Route[] = []): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      ...extra,
      { match: "/graph", json: makeGraph() },
      { match: "/events/ticket", json: { ticket: "t", expires_in: 60 } },
      { match: "/hypotheses/", json: makeHypothesisDetail() },
      { match: "/detail", json: makeRunDetail({ leaderboard: rows() }) },
      { match: `/runs/${RUN_ID}`, json: makeRunSummary() },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The leaderboard the List view reads, matching the graph fixture's hids. */
function rows() {
  return [
    makeHypothesisRow({
      hid: "h001",
      id: "hyp-uuid-1",
      title: "Plasmonic nitrogen fixation on gold nanostructures",
    }),
    makeHypothesisRow({
      hid: "h002",
      id: "hyp-uuid-2",
      title: "Ambient-pressure Haber variant",
      status: "rejected",
    }),
  ];
}

function renderTab(
  options: {
    search?: string;
    run?: Partial<RunSummary>;
    detail?: Partial<RunDetail> | null;
    /** Opens the live stream the way the run workspace does. */
    watching?: boolean;
  } = {},
) {
  const run = makeRunSummary({ lifecycle: "completed", ...options.run });
  const detail =
    options.detail === null
      ? null
      : makeRunDetail({ run, leaderboard: rows(), ...options.detail });

  window.history.replaceState({}, "", `/runs/${RUN_ID}${options.search ?? ""}`);

  function Harness() {
    // The workspace holds the subscription open for a run that can still move;
    // the tab reads the events out of the store rather than opening its own.
    useRunEvents(run.id);
    return <HypothesesTab run={run} detail={detail} />;
  }

  return render(
    <BrowserRouter>
      {options.watching ? <Harness /> : <HypothesesTab run={run} detail={detail} />}
    </BrowserRouter>,
  );
}

function canvas(): Promise<HTMLElement> {
  return screen.findByRole("group", { name: /idea genealogy/i });
}

function tab(name: RegExp): HTMLElement {
  return screen.getByRole("tab", { name });
}

function graphRequests(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/graph"));
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});

describe("choosing how to look at the ideas", () => {
  it("defaults to the organic view", async () => {
    stub();
    renderTab();

    expect(await canvas()).toHaveAttribute("data-view", "organic");
    expect(tab(/organic/i)).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the chosen view in the URL", async () => {
    stub();
    renderTab();
    await canvas();

    await userEvent.click(tab(/layered/i));

    expect(window.location.search).toContain("view=layered");
    expect(await canvas()).toHaveAttribute("data-view", "layered");
  });

  it("opens on the view a shared link names", async () => {
    stub();
    renderTab({ search: "?tab=hypotheses&view=layered" });

    expect(await canvas()).toHaveAttribute("data-view", "layered");
    expect(tab(/layered/i)).toHaveAttribute("aria-selected", "true");
    // The tab the workspace is on is not this component's to lose.
    expect(window.location.search).toContain("tab=hypotheses");
  });

  it("drops the parameter again when the default view comes back", async () => {
    stub();
    renderTab({ search: "?view=layered" });
    await canvas();

    await userEvent.click(tab(/organic/i));

    expect(window.location.search).not.toContain("view=");
  });

  it("still offers the ranked list", async () => {
    stub();
    renderTab();
    await canvas();

    await userEvent.click(tab(/list/i));

    expect(
      screen.getByText("Plasmonic nitrogen fixation on gold nanostructures"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rejected 1/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/sort hypotheses/i)).toBeInTheDocument();
    expect(window.location.search).toContain("view=list");
  });

  it("says nothing about views on a run that produced no ideas", async () => {
    stub([
      {
        match: "/graph",
        json: makeGraph({
          nodes: [],
          edges: [],
          meta: makeMeta({
            rounds: 1,
            has_lineage: false,
            has_clusters: false,
            elo_min: 0,
            elo_max: 0,
            node_count: 0,
          }),
        }),
      },
    ]);
    renderTab({ detail: { leaderboard: [] } });

    expect(await screen.findByText(/no hypotheses yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /organic/i })).not.toBeInTheDocument();
  });
});

describe("the detail rail", () => {
  it("invites a choice before one is made", async () => {
    stub();
    renderTab();
    await canvas();

    expect(screen.getByText(/choose an idea/i)).toBeInTheDocument();
  });

  it("reads the chosen idea through the one hypothesis fetch there is", async () => {
    const fetchMock = stub();
    renderTab();
    await canvas();

    await userEvent.click(screen.getByRole("button", { name: /^h003/ }));

    const rail = screen.getByRole("complementary", { name: /selected idea/i });
    expect(
      await within(rail).findByText(/ammonia forms at ambient pressure/i),
    ).toBeInTheDocument();
    expect(
      within(rail).getByText("Electrochemical nitride cycling at a lithium interface"),
    ).toBeInTheDocument();
    expect(
      within(rail).getByRole("link", { name: /open on its own page/i }),
    ).toHaveAttribute("href", `/runs/${RUN_ID}/hypotheses/h003`);

    const asked = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes("/hypotheses/hyp-uuid-3"));
    expect(asked).toHaveLength(1);
  });

  it("does not print the idea's title twice", async () => {
    // The engine opens most bodies with `# <title>`, and the rail has printed
    // the title already — in a 340px column the repeat is seven lines of nothing.
    const graph = makeGraph();
    const idea = graph.nodes[2];
    stub([
      {
        match: "/hypotheses/",
        json: makeHypothesisDetail({
          hid: idea.hid,
          title: idea.title,
          body_md: `# ${idea.title}\n\n## Claim\n\nLithium mediates the loop.\n`,
        }),
      },
    ]);
    renderTab();
    await canvas();

    await userEvent.click(screen.getByRole("button", { name: /^h003/ }));
    const rail = screen.getByRole("complementary", { name: /selected idea/i });
    await within(rail).findByText(/lithium mediates the loop/i);

    expect(within(rail).getAllByText(idea.title)).toHaveLength(1);
    expect(
      within(rail).queryByRole("heading", { name: idea.title, level: 1 }),
    ).toBeNull();
    // Only the repeat goes. A heading that is the body's own structure stays.
    expect(within(rail).getByRole("heading", { name: "Claim" })).toBeInTheDocument();
  });

  it("names a cluster the way the legend names it, and not twice over", async () => {
    // The proximity step names most clusters `0`, `1`, `2`. The legend has
    // always said "Cluster 0"; the rail used to say "Theme: 0" about the same
    // node, which is the index this app exists not to show.
    stub([{ match: "/graph", json: makeNumericClusterGraph() }]);
    renderTab();
    await canvas();

    await userEvent.click(screen.getByRole("button", { name: /^h001/ }));
    const rail = screen.getByRole("complementary", { name: /selected idea/i });

    expect(within(rail).getByText("Cluster 0")).toBeInTheDocument();
    expect(within(rail).queryByText(/theme:/i)).not.toBeInTheDocument();
  });

  it("announces the selection, which changes the panel and moves no focus", async () => {
    stub();
    renderTab();
    await canvas();

    await userEvent.click(screen.getByRole("button", { name: /^h003/ }));
    const rail = screen.getByRole("complementary", { name: /selected idea/i });
    const live = within(rail).getByText(/^h003 selected/);

    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent(
      "h003 selected — Electrochemical nitride cycling at a lithium interface",
    );
  });

  it("says what it knows about an idea whose text has not arrived yet", async () => {
    // An idea inserted live carries no hypothesis id until the next refetch,
    // and the leaderboard this tab was handed predates it too.
    const graph = makeGraph();
    const fetchMock = stub([
      {
        match: "/graph",
        json: { ...graph, nodes: graph.nodes.map((node) => ({ ...node, id: "" })) },
      },
    ]);
    // The leaderboard this tab was handed is the one from before that idea
    // existed, so it cannot supply the id either.
    renderTab({ run: { lifecycle: "running" } });
    await canvas();

    await userEvent.click(screen.getByRole("button", { name: /^h003/ }));
    const rail = screen.getByRole("complementary", { name: /selected idea/i });

    expect(
      within(rail).getByText("Electrochemical nitride cycling at a lithium interface"),
    ).toBeInTheDocument();
    expect(within(rail).getByText(/has not been read back yet/i)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls
        .map((call) => String(call[0]))
        .filter((url) => url.includes("/hypotheses/")),
    ).toHaveLength(0);
  });
});

describe("a run that is still thinking", () => {
  it("grows the graph from the stream instead of refetching it", async () => {
    const fetchMock = stub();
    renderTab({ run: { lifecycle: "running" }, watching: true });
    await canvas();
    await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));

    MockEventSource.last.emitRunEvent({
      seq: 42,
      type: "hypothesis_added",
      round: 2,
      payload: {
        hid: "h007",
        title: "A newborn idea",
        round: 2,
        source: "agent",
        operator: "grounding",
        parent_ids: ["h001"],
      },
    });

    expect(await screen.findByRole("button", { name: /^h007/ })).toBeInTheDocument();
    expect(graphRequests(fetchMock)).toHaveLength(1);
  });

  it("re-reads the graph for an event that cannot say what it changed", async () => {
    const fetchMock = stub();
    renderTab({ run: { lifecycle: "running" }, watching: true });
    await canvas();
    await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));

    // `cluster_applied` carries run-level counts and identifies no idea, so the
    // only honest way to draw its result is to ask again.
    MockEventSource.last.emitRunEvent({
      seq: 43,
      type: "cluster_applied",
      round: 2,
      payload: { round: 2, n_clusters: 3, labelled: 6, duplicates: 1 },
    });

    await waitFor(() => expect(graphRequests(fetchMock)).toHaveLength(2));
  });
});

describe("when the genealogy cannot be drawn", () => {
  it("falls back to the ideas it already has, and says why", async () => {
    stub([{ match: "/graph", status: 500, json: { code: "server_error" } }]);
    renderTab();

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    expect(
      screen.getByText("Plasmonic nitrogen fixation on gold nanostructures"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /idea genealogy/i })).toBeNull();
  });

  it("gives the view switch one tab stop and the arrow keys the rest", async () => {
    // `role="tablist"` is a promise about the keyboard: three consecutive tab
    // stops and inert arrow keys is the announcement without the behaviour.
    stub();
    renderTab();
    await canvas();

    expect(tab(/organic/i)).toHaveAttribute("tabindex", "0");
    expect(tab(/layered/i)).toHaveAttribute("tabindex", "-1");
    expect(tab(/list/i)).toHaveAttribute("tabindex", "-1");

    tab(/organic/i).focus();
    await userEvent.keyboard("{ArrowRight}");

    expect(document.activeElement).toBe(tab(/layered/i));
    expect(window.location.search).toContain("view=layered");
  });

  it("does not tell a run in its first round that its lineage was lost", async () => {
    // No edges yet and none lost: an app run that has generated a round and
    // evolved nothing is not an import that predates `parent_ids`.
    stub([{ match: "/graph", json: makeUnevolvedGraph() }]);
    renderTab();
    await canvas();

    expect(screen.queryByText(/lineage was not recorded/i)).not.toBeInTheDocument();
    expect(screen.getByText(/no idea in this run descended/i)).toBeInTheDocument();
  });

  it("draws a run whose descent was never recorded", async () => {
    stub([{ match: "/graph", json: makeNoLineageGraph() }]);
    renderTab();

    await canvas();
    expect(screen.getByText(/lineage was not recorded/i)).toBeInTheDocument();
  });
});

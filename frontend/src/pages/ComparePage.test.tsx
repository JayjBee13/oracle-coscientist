import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type {
  CompareAnalytics,
  CompareDelta,
  HypothesisRow,
  RunSummary,
} from "../api/types";
import { makeRunSummary } from "../test/fixtures";
import { ComparePage } from "./ComparePage";

/* --- Fixtures -------------------------------------------------------------
   The baseline's weakest idea outscores the challenger's strongest one. If any
   part of this page merged the two tournaments, the challenger's leader would
   not be ranked first — which is exactly what the assertions below check.
   ------------------------------------------------------------------------- */

const BASELINE = makeRunSummary({
  id: "run-a",
  title: "Ammonia — mechanism first",
  lifecycle: "completed",
  counts: { active: 3, rejected: 1, archived: 0, matches: 8 },
});

const CHALLENGER = makeRunSummary({
  id: "run-b",
  title: "Ammonia — survey",
  lifecycle: "completed",
  counts: { active: 3, rejected: 0, archived: 0, matches: 12 },
});

function hyp(overrides: Partial<HypothesisRow>): HypothesisRow {
  return {
    id: `hyp-${overrides.hid}`,
    hid: "h001",
    title: "A hypothesis",
    status: "active",
    elo: 1200,
    matches: 3,
    wins: 1,
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

const BASELINE_ROWS: HypothesisRow[] = [
  hyp({ hid: "h001", title: "Plasmonic nitrogen fixation", elo: 1400 }),
  hyp({ hid: "h002", title: "Photo-driven lithium mediation", elo: 1300 }),
  hyp({ hid: "h003", title: "Defect-engineered titania", elo: 1250 }),
];

/** Every one of these scores below every baseline hypothesis. */
const CHALLENGER_ROWS: HypothesisRow[] = [
  hyp({ hid: "h001", title: "Iron carbide surface states", elo: 1150 }),
  hyp({ hid: "h002", title: "Plasmonic nitrogen fixation", elo: 1100 }),
  hyp({ hid: "h003", title: "Tandem photoelectrode stack", elo: 1050 }),
];

const DELTAS: CompareDelta[] = [
  {
    metric: "Active hypotheses",
    base: 3,
    challenger: 6,
    direction_hint: "up" as never,
  },
  { metric: "Model calls", base: 40, challenger: 92, direction_hint: "down" as never },
  { metric: "Rounds", base: 3, challenger: 3, direction_hint: "flat" as never },
  { metric: "Elo spread", base: 150, challenger: 50, direction_hint: "up" as never },
];

function makeAnalytics(overrides: Partial<CompareAnalytics> = {}): CompareAnalytics {
  return {
    baseline: BASELINE,
    challenger: CHALLENGER,
    shared_prompt: true,
    deltas: DELTAS,
    movement: [],
    graft_summary: { baseline: null, challenger: null },
    ...overrides,
  };
}

/* --- Harness -------------------------------------------------------------- */

type Handler = (url: string) => unknown;

function stubApi(handlers: { pattern: RegExp; respond: Handler }[]) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      const handler = handlers.find((candidate) => candidate.pattern.test(url));
      if (!handler) {
        return new Response(JSON.stringify({ code: "not_found", message: url }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }
      const result = handler.respond(url);
      if (result instanceof Response) return result;
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

function routes(
  analytics: CompareAnalytics = makeAnalytics(),
  runs: RunSummary[] = [BASELINE, CHALLENGER],
) {
  return [
    { pattern: /\/runs\/run-a\/hypotheses/, respond: () => BASELINE_ROWS },
    { pattern: /\/runs\/run-b\/hypotheses/, respond: () => CHALLENGER_ROWS },
    { pattern: /\/compare\?/, respond: () => analytics },
    { pattern: /\/runs\?/, respond: () => ({ items: runs, total: runs.length }) },
    { pattern: /\/runs$/, respond: () => ({ items: runs, total: runs.length }) },
  ];
}

function renderCompare(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/compare" element={<ComparePage />} />
        <Route path="/compare/:a/:b" element={<ComparePage />} />
        <Route path="/runs/:id" element={<div>RUN WORKSPACE</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The panel for one side, found by its accessible section label. */
function side(name: "Baseline" | "Challenger", run: RunSummary): HTMLElement {
  return screen.getByRole("region", { name: `${name}: ${run.title}` });
}

/* --- Tests ---------------------------------------------------------------- */

describe("compare — picking two runs", () => {
  it("asks for two runs before it shows anything", async () => {
    stubApi(routes());
    renderCompare("/compare");

    expect(await screen.findByText(/pick two runs/i)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("filters the pickers by search, over one loaded page of runs", async () => {
    stubApi(routes());
    const user = userEvent.setup();
    renderCompare("/compare");

    const picker = await screen.findByRole("listbox", { name: "Baseline" });
    expect(within(picker).getByText("Ammonia — mechanism first")).toBeInTheDocument();
    expect(within(picker).getByText("Ammonia — survey")).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Baseline" }), "survey");

    const filtered = screen.getByRole("listbox", { name: "Baseline" });
    expect(within(filtered).queryByText("Ammonia — mechanism first")).toBeNull();
    expect(within(filtered).getByText("Ammonia — survey")).toBeInTheDocument();
  });

  it("refuses to compare a run with itself", async () => {
    const calls = stubApi(routes());
    renderCompare("/compare/run-a/run-a");

    expect(
      await screen.findByText(/that is the same run on both sides/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/every difference would be zero/i)).toBeInTheDocument();
    // ...and it does not even ask the backend.
    expect(calls.some((url) => url.includes("/compare?"))).toBe(false);
  });
});

describe("compare — the shared-prompt banner", () => {
  it("confirms it when both runs started from the same wording", async () => {
    stubApi(routes(makeAnalytics({ shared_prompt: true })));
    renderCompare("/compare/run-a/run-b");

    expect(await screen.findByText(/same prompt on both sides/i)).toBeInTheDocument();
    expect(
      screen.getByText(/what differs between them is the settings/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/did not start from the same prompt/i)).toBeNull();
  });

  it("warns when they did not, and says why that matters", async () => {
    stubApi(routes(makeAnalytics({ shared_prompt: false })));
    renderCompare("/compare/run-a/run-b");

    const banner = await screen.findByRole("alert");
    expect(
      within(banner).getByText(/these runs did not start from the same prompt/i),
    ).toBeInTheDocument();
    expect(
      within(banner).getByText(/may be the question rather than anything you changed/i),
    ).toBeInTheDocument();
  });
});

describe("compare — deltas and the verdict", () => {
  it("reads each measure in its own direction", async () => {
    stubApi(routes());
    renderCompare("/compare/run-a/run-b");

    const table = await screen.findByRole("table");
    const activeRow = within(table).getByText("Active hypotheses").closest("tr")!;
    expect(within(activeRow).getByText("+3")).toBeInTheDocument();
    expect(within(activeRow).getByText("better")).toBeInTheDocument();

    // More calls is a bigger number and a worse outcome.
    const callsRow = within(table).getByText("Model calls").closest("tr")!;
    expect(within(callsRow).getByText("+52")).toBeInTheDocument();
    expect(within(callsRow).getByText("worse")).toBeInTheDocument();

    // A descriptive measure gets no verdict.
    const roundsRow = within(table).getByText("Rounds").closest("tr")!;
    expect(within(roundsRow).getByText("no change")).toBeInTheDocument();
  });

  it("gives a headline that refuses to invent a winner", async () => {
    stubApi(routes());
    renderCompare("/compare/run-a/run-b");

    // One better (active hypotheses), two worse (calls, Elo spread).
    expect(
      await screen.findByText(/baseline came out ahead on 2 of 3 measures/i),
    ).toBeInTheDocument();
  });
});

describe("compare — the two ranked lists", () => {
  it("ranks each run inside itself and never merges the two", async () => {
    stubApi(routes());
    renderCompare("/compare/run-a/run-b");

    await screen.findByRole("region", { name: `Baseline: ${BASELINE.title}` });
    const left = side("Baseline", BASELINE);
    const right = side("Challenger", CHALLENGER);

    // Each side ranks 1, 2, 3 — the challenger's leader is first in its own
    // list even though all three of its scores are below the baseline's worst.
    expect(
      within(left)
        .getAllByText(/^[123]$/)
        .map((node) => node.textContent),
    ).toEqual(["1", "2", "3"]);
    expect(
      within(right)
        .getAllByText(/^[123]$/)
        .map((node) => node.textContent),
    ).toEqual(["1", "2", "3"]);
    expect(within(right).getByText("Iron carbide surface states")).toBeInTheDocument();

    // A merged ranking would have to put all six in one list. Neither side has
    // a row from the other, and there is no third list anywhere on the page.
    expect(within(left).queryByText("Iron carbide surface states")).toBeNull();
    expect(within(right).queryByText("Defect-engineered titania")).toBeNull();
    expect(screen.getAllByRole("region")).toHaveLength(2);

    // Both leaders fill their own bar: full means "best here", not "best overall".
    const leaders = [left, right].map(
      (panel) => (panel.querySelector(".rank__fill") as HTMLElement).style.width,
    );
    expect(leaders).toEqual(["100%", "100%"]);
  });

  it("says what each bar is measured against", async () => {
    stubApi(routes());
    renderCompare("/compare/run-a/run-b");

    await screen.findByRole("region", { name: `Baseline: ${BASELINE.title}` });
    const left = side("Baseline", BASELINE);
    expect(within(left).getByText(/elo does not carry across runs/i)).toBeInTheDocument();
    expect(
      within(left).getByText(/weakest \(1250\) and strongest \(1400\)/i),
    ).toBeInTheDocument();
  });

  it("is honest about a run that rejected everything it produced", async () => {
    stubApi([
      { pattern: /\/runs\/run-a\/hypotheses/, respond: () => BASELINE_ROWS },
      {
        pattern: /\/runs\/run-b\/hypotheses/,
        respond: () =>
          CHALLENGER_ROWS.map((row) => ({ ...row, status: "rejected" as const })),
      },
      { pattern: /\/compare\?/, respond: () => makeAnalytics() },
      { pattern: /\/runs/, respond: () => ({ items: [BASELINE, CHALLENGER], total: 2 }) },
    ]);
    renderCompare("/compare/run-a/run-b");

    await screen.findByRole("region", { name: `Challenger: ${CHALLENGER.title}` });
    expect(
      within(side("Challenger", CHALLENGER)).getByText(
        /none survived review, so there was no tournament to rank/i,
      ),
    ).toBeInTheDocument();
  });
});

describe("compare — diversity injection", () => {
  it("hides the panel entirely when neither engine had the concept", async () => {
    stubApi(
      routes(makeAnalytics({ graft_summary: { baseline: null, challenger: null } })),
    );
    renderCompare("/compare/run-a/run-b");

    await screen.findByRole("table");
    expect(screen.queryByText(/diversity injection/i)).toBeNull();
  });

  it("shows only the side that has it, rather than zeros for the side that cannot", async () => {
    stubApi(
      routes(
        makeAnalytics({
          graft_summary: {
            baseline: null,
            challenger: {
              enabled: true,
              fired_count: 2,
              collapse_events: 5,
            } as never,
          },
        }),
      ),
    );
    renderCompare("/compare/run-a/run-b");

    expect(
      await screen.findByText(/when ideas converge too tightly/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/fired 2 times/i)).toBeInTheDocument();
    const panel = screen.getByText(/when ideas converge too tightly/i).closest(".panel")!;
    expect(within(panel as HTMLElement).queryByText("Baseline")).toBeNull();
  });
});

describe("compare — failure", () => {
  it("names the problem and offers a retry instead of showing empty columns", async () => {
    stubApi([
      {
        pattern: /\/runs\?/,
        respond: () => ({ items: [BASELINE, CHALLENGER], total: 2 }),
      },
      {
        pattern: /\/compare\?/,
        respond: () =>
          new Response(
            JSON.stringify({
              code: "run_not_found",
              message: "No run with that id on the challenger side.",
            }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
      },
      { pattern: /\/hypotheses/, respond: () => [] },
    ]);
    renderCompare("/compare/run-a/run-b");

    expect(
      await screen.findByText("No run with that id on the challenger side."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    // No zeros beside the error.
    expect(screen.queryByRole("table")).toBeNull();
  });
});

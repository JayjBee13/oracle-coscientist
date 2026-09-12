import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { Lifecycle, RunDetail, RunSource, RunSummary } from "../../api/types";
import { getToasts } from "../../lib/toast";
import { makeRunDetail, makeRunSummary, mockFetchRoutes } from "../../test/fixtures";
import { ReportTab } from "./ReportTab";

/**
 * The report's own entry point into "run more rounds".
 *
 * Reading the conclusions is where the thought "there is more in this" actually
 * arrives, and the control bar that can act on it is two tabs away. So the
 * action gets a second door here — and everything worth asserting about it is
 * about *when* the door exists, because the lifecycle table is the only thing
 * standing between an imported run and a `failed` written over a historical
 * result nobody can regenerate.
 *
 * The dialog itself is the control bar's `ContinueDialog`, tested where it
 * lives; what these tests hold to account is that this entry point opens that
 * dialog and sends through the same control call rather than growing a second
 * way to continue a run.
 */

type Route = Parameters<typeof mockFetchRoutes>[0][number];

function stub(extra: Route[] = []): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      ...extra,
      { match: "/controls", json: { accepted: true, lifecycle: "queued" } },
      { match: "/overview", text: "## What the run concluded\n\nAmmonia, mostly.\n" },
      { match: "/detail", json: makeRunDetail() },
      { match: "/runs/run-1", json: makeRunSummary() },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderReport(
  options: {
    lifecycle?: Lifecycle;
    source?: RunSource;
    run?: Partial<RunSummary>;
    detail?: Partial<RunDetail> | null;
  } = {},
) {
  const run = makeRunSummary({
    lifecycle: options.lifecycle ?? "completed",
    source: options.source ?? "app",
    rounds_target: 5,
    has_overview: true,
    ...options.run,
  });
  const detail =
    options.detail === null
      ? null
      : makeRunDetail({
          run,
          rounds: [
            { round: 1, status: "completed", hypotheses_added: 6 },
            { round: 2, status: "completed", hypotheses_added: 4 },
          ].map((round) => ({
            started_at: "2026-08-01T10:00:00Z",
            completed_at: "2026-08-01T10:20:00Z",
            matches_completed: 4,
            matches_planned: 4,
            reviews: 6,
            graft_fired: false,
            ...round,
          })),
          ...options.detail,
        });

  return render(
    <MemoryRouter>
      <ReportTab run={run} detail={detail} />
    </MemoryRouter>,
  );
}

function moreRoundsButton(): HTMLElement {
  return screen.getByRole("button", { name: /run more rounds/i });
}

describe("report downloads", () => {
  it("offers the whole report and a top-five export as distinct downloads", () => {
    stub();
    renderReport();

    const report = screen.getByRole("link", { name: "Download report" });
    const topFive = screen.getByRole("link", { name: "Download top 5" });
    expect(report).toHaveAttribute("download");
    expect(report.getAttribute("href")).toMatch(/\/runs\/run-1\/export\.md$/);
    expect(topFive).toHaveAttribute("download");
    expect(topFive.getAttribute("href")).toMatch(/\/runs\/run-1\/export\.md\?top=5$/);
  });
});

describe("running more rounds from the report", () => {
  it("offers more rounds on a finished app run", async () => {
    stub();
    renderReport({ lifecycle: "completed", source: "app" });

    expect(moreRoundsButton()).toBeInTheDocument();
    expect(await screen.findByText(/ammonia, mostly/i)).toBeInTheDocument();
  });

  it("offers them on a run that was stopped early, which is the same decision", () => {
    stub();
    renderReport({ lifecycle: "stopped", source: "app" });

    expect(moreRoundsButton()).toBeInTheDocument();
  });

  it("does not offer more rounds on an imported run", () => {
    stub();
    renderReport({ lifecycle: "completed", source: "imported" });

    expect(screen.queryByRole("button", { name: /run more rounds/i })).toBeNull();
  });

  it("does not offer more rounds while the run is still going", () => {
    stub();
    renderReport({ lifecycle: "running", source: "app" });

    expect(screen.queryByRole("button", { name: /run more rounds/i })).toBeNull();
  });

  it("waits for the settings the dialog does its arithmetic on", () => {
    stub();
    renderReport({ detail: null });

    expect(screen.queryByRole("button", { name: /run more rounds/i })).toBeNull();
  });

  it("opens the control bar's own dialog and sends one continue", async () => {
    const fetchMock = stub();
    renderReport();
    const user = userEvent.setup();

    await user.click(moreRoundsButton());

    const dialog = screen.getByRole("dialog");
    // The dialog counts from what the run *completed*, as the backend does.
    expect(dialog).toHaveTextContent(/it has completed 2/i);
    expect(dialog).toHaveTextContent(/every hypothesis, rating and review/i);

    await user.click(within(dialog).getByRole("button", { name: /run more rounds/i }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter((call) => String(call[0]).includes("/controls")),
      ).toHaveLength(1),
    );
    const [, init] = fetchMock.mock.calls.find((call) =>
      String(call[0]).includes("/controls"),
    )!;
    expect(JSON.parse(String((init as RequestInit).body))).toMatchObject({
      action: "continue",
      add_rounds: 2,
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("says the run is going again, because this panel is about to vanish", async () => {
    stub();
    renderReport();
    const user = userEvent.setup();

    await user.click(moreRoundsButton());
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: /run more rounds/i,
      }),
    );

    // The gate is the lifecycle, which is `queued` the moment this succeeds, so
    // the button that was just pressed unmounts itself. Silence would read as a
    // click that did nothing.
    await waitFor(() => expect(getToasts()).toHaveLength(1));
    expect(getToasts()[0]).toMatchObject({ tone: "go", title: "Running 2 more rounds" });
  });

  it("keeps the dialog open with the reason when the harness is busy", async () => {
    stub([
      {
        match: "/controls",
        status: 409,
        json: {
          code: "lane_busy",
          message: "The claude harness is busy.",
          details: { harness: "claude", conflicting_run_id: "run-9" },
        },
      },
    ]);
    renderReport();
    const user = userEvent.setup();

    await user.click(moreRoundsButton());
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /run more rounds/i }));

    expect(await within(dialog).findByText(/one run at a time/i)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("never says the decision in money", async () => {
    stub();
    renderReport();
    const user = userEvent.setup();

    const section = moreRoundsButton().closest("section")!;
    expect(section.textContent).not.toMatch(/\$/);

    await user.click(moreRoundsButton());
    // The dialog governs in calls; the only dollar figure it may print is the
    // obsolete ceiling it is offering to remove, and this run carries none.
    expect(screen.getByRole("dialog")).toHaveTextContent(/call budget/i);
  });
});

/**
 * A missing report is the most consequential thing that can happen to a run, and
 * for two of the twenty-nine runs this app has executed it was reported as the
 * blank sentence "This run has no research overview stored." Both had ended
 * `completed`, so the branch that links to Activity was never taken; both had
 * simply run out of money one call short of the deliverable they had paid for.
 */
describe("when there is no report", () => {
  function stubMissing(): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        mockFetchRoutes([
          { match: "/overview", status: 404, json: { code: "overview_missing" } },
          { match: "/detail", json: makeRunDetail() },
          { match: "/runs/run-1", json: makeRunSummary() },
        ]),
      ),
    );
  }

  it("says the cost ceiling was reached, and where the work went", async () => {
    stubMissing();
    renderReport({
      run: { has_overview: false, overview_skipped_reason: "budget_usd" },
    });

    expect(await screen.findByText(/hit its cost ceiling/i)).toBeInTheDocument();
    expect(screen.getByText(/Hypotheses tab/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /see what went wrong/i }),
    ).toBeInTheDocument();
  });

  it("says the call budget ran out when that is what happened", async () => {
    stubMissing();
    renderReport({
      run: { has_overview: false, overview_skipped_reason: "budget_calls" },
    });

    expect(await screen.findByText(/every model call it had/i)).toBeInTheDocument();
  });

  it("still offers a way into the log when nothing recorded a reason", async () => {
    stubMissing();
    renderReport({ run: { has_overview: false } });

    expect(await screen.findByText(/no report was written/i)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /see what this run did/i }),
    ).toBeInTheDocument();
  });
});

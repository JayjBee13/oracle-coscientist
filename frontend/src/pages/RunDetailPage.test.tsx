import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { Lifecycle, RunConfig, RunDetail } from "../api/types";
import { modelLabel } from "../lib/models";
import {
  makeRunConfig,
  makeRunDetail,
  makeRunSummary,
  mockFetchRoutes,
} from "../test/fixtures";
import { makeGraph, makeGraphNode, makeMeta } from "../test/graphFixtures";
import { agoIso, makeEvent, makeHypothesisRow } from "../test/runFixtures";
import { RunDetailPage } from "./RunDetailPage";

type Route_ = Parameters<typeof mockFetchRoutes>[0][number];

function stub(detail: RunDetail, extra: Route_[] = []) {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      ...extra,
      { match: "/events/ticket", json: { ticket: "t", expires_in: 60 } },
      { match: "/controls", json: { accepted: true, lifecycle: detail.run.lifecycle } },
      { match: "/detail", json: detail },
      { match: "/runs/run-1", json: detail.run },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderRun(path = "/runs/run-1") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/runs/:id" element={<RunDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("run workspace header", () => {
  it("leads with the title and offers to run it again", async () => {
    stub(makeRunDetail());
    renderRun();

    expect(
      await screen.findByRole("heading", { name: "Photocatalytic routes to ammonia" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /run again/i })).toHaveAttribute(
      "href",
      "/new?from=run-1",
    );
  });

  it("names the problem instead of showing an empty workspace", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    renderRun();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not reach the oracle backend/i,
    );
  });
});

describe("control enablement follows the lifecycle table", () => {
  const cases: { lifecycle: Lifecycle; shown: string[]; hidden: string[] }[] = [
    {
      lifecycle: "running",
      shown: ["Pause", "Finish after this round", "Stop"],
      hidden: ["Resume", "Force stop"],
    },
    {
      lifecycle: "paused",
      shown: ["Resume", "Finish after this round", "Stop"],
      hidden: ["Pause", "Force stop"],
    },
    {
      lifecycle: "pausing",
      shown: ["Stop"],
      hidden: ["Pause", "Resume", "Finish after this round"],
    },
    {
      lifecycle: "completed",
      shown: ["Run more rounds"],
      hidden: ["Pause", "Resume", "Stop", "Finish after this round", "Force stop"],
    },
    {
      lifecycle: "stopped",
      shown: ["Run more rounds"],
      hidden: ["Pause", "Resume", "Stop", "Finish after this round", "Force stop"],
    },
    {
      // Failed and lost runs stopped for a reason nobody has diagnosed and wrote
      // no report. Offering more rounds there would be salvage wearing the
      // clothes of science.
      lifecycle: "failed",
      shown: [],
      hidden: [
        "Pause",
        "Resume",
        "Stop",
        "Finish after this round",
        "Force stop",
        "Run more rounds",
      ],
    },
    {
      lifecycle: "lost",
      shown: [],
      hidden: ["Run more rounds"],
    },
  ];

  for (const testCase of cases) {
    it(`offers ${testCase.shown.join(", ") || "nothing"} while ${testCase.lifecycle}`, async () => {
      stub(makeRunDetail({ run: makeRunSummary({ lifecycle: testCase.lifecycle }) }));
      renderRun();
      await screen.findByRole("heading", { name: /photocatalytic/i });

      for (const label of testCase.shown) {
        expect(screen.getByRole("button", { name: label })).toBeEnabled();
      }
      for (const label of testCase.hidden) {
        expect(screen.queryByRole("button", { name: label })).not.toBeInTheDocument();
      }
    });
  }

  it("hides force stop until a stop has had its twenty seconds", async () => {
    stub(makeRunDetail({ run: makeRunSummary({ lifecycle: "running" }) }));
    renderRun();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Stop" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));

    expect(screen.queryByRole("button", { name: /force stop/i })).not.toBeInTheDocument();
  });
});

describe("stopping asks first", () => {
  it("names the run and how far it has got, and does nothing on cancel", async () => {
    const fetchMock = stub(
      makeRunDetail({ run: makeRunSummary({ lifecycle: "running" }) }),
    );
    renderRun();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Stop" }));

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Photocatalytic routes to ammonia");
    expect(dialog).toHaveTextContent("23");
    // Calls, not dollars. This is the highest-stakes moment in the product and a
    // bolded dollar figure here frames the stake as money nobody is being
    // charged; what stopping reclaims is wall clock and plan-window headroom.
    expect(dialog).not.toHaveTextContent("$");
    expect(dialog).toHaveTextContent(/report is still written/i);

    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/controls")),
    ).toBe(false);
  });

  it("sends the control once confirmed", async () => {
    const fetchMock = stub(
      makeRunDetail({ run: makeRunSummary({ lifecycle: "running" }) }),
    );
    renderRun();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Stop" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Stop" }),
    );

    await vi.waitFor(() => {
      const control = fetchMock.mock.calls.find((call) =>
        String(call[0]).includes("/controls"),
      );
      expect(control?.[1]?.body).toContain("stop");
    });
  });

  it("pauses without a confirmation — it is reversible", async () => {
    const fetchMock = stub(
      makeRunDetail({ run: makeRunSummary({ lifecycle: "running" }) }),
    );
    renderRun();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Pause" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await vi.waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).includes("/controls")),
      ).toBe(true);
    });
  });
});

describe("running more rounds of a finished run", () => {
  function finished(config: Partial<RunConfig> = {}) {
    return makeRunDetail({
      run: makeRunSummary({
        lifecycle: "completed",
        round: 5,
        rounds_target: 5,
        calls_used: 23,
        budget_calls: 60,
        has_overview: true,
      }),
      // A run launched the way runs are launched now: governed by calls, with no
      // cost ceiling at all. The dialog's dollar control is for the older ones.
      config: makeRunConfig({ budget_usd: null, ...config }) as unknown as Record<
        string,
        unknown
      >,
      rounds: [
        { round: 1, status: "completed", hypotheses_added: 6 },
        { round: 2, status: "completed", hypotheses_added: 6 },
      ].map((round) => ({
        started_at: null,
        completed_at: "2026-08-01T10:20:00Z",
        matches_completed: 4,
        matches_planned: 4,
        reviews: 6,
        graft_fired: false,
        ...round,
      })),
    });
  }

  async function openDialog() {
    const fetchMock = stub(finished());
    renderRun();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Run more rounds" }));
    return { fetchMock, user, dialog: screen.getByRole("dialog") };
  }

  it("promises what a clone cannot: the ideas and ratings survive", async () => {
    const { dialog } = await openDialog();

    expect(dialog).toHaveTextContent("Photocatalytic routes to ammonia");
    expect(dialog).toHaveTextContent(/every hypothesis, rating and review/i);
    expect(dialog).toHaveTextContent(/guidance the last one ended on/i);
    // Same rule as the stop dialog: these are subscription calls, and a dollar
    // figure would frame the decision as money nobody is being charged.
    expect(dialog).not.toHaveTextContent("$");
  });

  it("counts the increment from the rounds the run actually completed", async () => {
    const { dialog } = await openDialog();

    // Two `round_completed` rounds in the fixture, so +2 takes it to 4 — not to
    // 7, which is what counting from `rounds_target` would have claimed.
    expect(dialog).toHaveTextContent("It has completed 2. This takes it to 4.");
  });

  it("offers a budget that covers the rounds being added", async () => {
    const { dialog } = await openDialog();

    const budget = within(dialog).getByLabelText("Call budget") as HTMLInputElement;
    // Whatever the estimate works out to, a run that finished at 23 of 60 calls
    // cannot be given more rounds on the twelve it has left.
    expect(Number(budget.value)).toBeGreaterThan(60);
  });

  it("quotes what the added rounds cost, not the headroom that happens to be left", async () => {
    // A run whose ceiling already covers the extra rounds: 9 of 60 calls spent,
    // and two more rounds of this shape estimate at 27. The budget therefore
    // stays at 60 and needs no raise — but the leftover headroom is 51, and a
    // hint quoting *that* would tell somebody these rounds cost twice what they
    // do, which is how you talk a scientist into raising a budget for no reason.
    const detail = makeRunDetail({
      run: makeRunSummary({
        lifecycle: "completed",
        calls_used: 9,
        budget_calls: 60,
        has_overview: true,
      }),
      config: makeRunConfig({
        generation_batch: 3,
        matches_per_round: 2,
        evolve_top_k: 1,
      }),
      rounds: [
        {
          round: 1,
          status: "completed",
          started_at: null,
          completed_at: "2026-08-01T10:20:00Z",
          hypotheses_added: 3,
          matches_completed: 2,
          matches_planned: 2,
          reviews: 3,
          graft_fired: false,
        },
      ],
    });
    stub(detail);
    renderRun();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Run more rounds" }));
    const dialog = screen.getByRole("dialog");

    expect(within(dialog).getByLabelText("Call budget")).toHaveValue(60);
    expect(dialog).toHaveTextContent("The rounds above need about 27 calls.");
    expect(dialog).not.toHaveTextContent("51");
  });

  it("will not take a ceiling with no room left for the report", async () => {
    const { dialog, user } = await openDialog();

    const budget = within(dialog).getByLabelText("Call budget") as HTMLInputElement;
    expect(dialog).toHaveTextContent(/cannot go below 26/i);

    // 23 used + one call for the next step + the two the engine always holds
    // back for the report. Typing under that snaps back rather than sending a
    // request the backend would refuse.
    await user.clear(budget);
    await user.type(budget, "24");
    await user.tab();
    expect(Number(budget.value)).toBe(26);
  });

  it("sends the increment and the ceiling once confirmed", async () => {
    const { dialog, user, fetchMock } = await openDialog();

    const rounds = within(dialog).getByLabelText("Rounds to add");
    await user.clear(rounds);
    await user.type(rounds, "3");
    await user.click(within(dialog).getByRole("button", { name: "Run more rounds" }));

    await vi.waitFor(() => {
      const control = fetchMock.mock.calls.find((call) =>
        String(call[0]).includes("/controls"),
      );
      const body = JSON.parse(String(control?.[1]?.body)) as Record<string, unknown>;
      expect(body.action).toBe("continue");
      expect(body.add_rounds).toBe(3);
      expect(body.budget_calls).toBeGreaterThan(23);
    });
  });

  /**
   * The cost ceiling only exists on runs launched before dollars stopped
   * governing, so the control that removes it only exists there too — and it
   * arrives already ticked, because no ceiling is what every run launched since
   * has, and because a run sitting at its old cap is sitting there for no reason
   * anybody is being charged for.
   */
  describe("the cost ceiling an older run was launched with", () => {
    async function openCapped(config: Partial<RunConfig> = { budget_usd: 5 }) {
      const fetchMock = stub(finished(config));
      renderRun();
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Run more rounds" }));
      return { fetchMock, user, dialog: screen.getByRole("dialog") };
    }

    async function sentBody(fetchMock: ReturnType<typeof vi.fn>) {
      let body: Record<string, unknown> = {};
      await vi.waitFor(() => {
        const control = fetchMock.mock.calls.find((call) =>
          String(call[0]).includes("/controls"),
        );
        body = JSON.parse(String(control?.[1]?.body)) as Record<string, unknown>;
        expect(body.action).toBe("continue");
      });
      return body;
    }

    it("is not mentioned at all on a run that has no ceiling", async () => {
      const { dialog } = await openDialog();

      expect(within(dialog).queryByLabelText(/cost ceiling/i)).not.toBeInTheDocument();
      expect(dialog).not.toHaveTextContent("$");
    });

    it("offers to remove it, ticked, on a run that has one", async () => {
      const { dialog } = await openCapped();

      const box = within(dialog).getByRole("checkbox", {
        name: /remove the old cost ceiling/i,
      });
      expect(box).toBeChecked();
      expect(dialog).toHaveTextContent("capped at $5.00 of API-equivalent cost");
      // Never as money, and never as something being authorised.
      expect(dialog).toHaveTextContent(/telemetry, not a bill/i);
      expect(dialog).toHaveTextContent(/not charged per token/i);
      expect(dialog).not.toHaveTextContent(/spend|approve|authorise/i);
    });

    it("says the ceiling is what stopped the run when the run is past it", async () => {
      // The owner's case: $5.056 recorded against a $5.00 cap.
      const detail = finished({ budget_usd: 5 });
      stub(
        makeRunDetail({
          ...detail,
          run: { ...detail.run, spend_usd: 5.056 },
        }),
      );
      renderRun();
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Run more rounds" }));

      expect(screen.getByRole("dialog")).toHaveTextContent(
        /reaching it is what stopped the run — \$5\.06 recorded/,
      );
    });

    it("sends an explicit null, which is what asks the server to remove it", async () => {
      const { dialog, user, fetchMock } = await openCapped();

      await user.click(within(dialog).getByRole("button", { name: "Run more rounds" }));

      const body = await sentBody(fetchMock);
      expect("budget_usd" in body).toBe(true);
      expect(body.budget_usd).toBeNull();
    });

    it("leaves the field out entirely when the ceiling is to be kept", async () => {
      // Absent and null are different requests: one leaves the ceiling alone,
      // the other takes it off. Unticking has to send neither a number nor null.
      const { dialog, user, fetchMock } = await openCapped();

      await user.click(
        within(dialog).getByRole("checkbox", { name: /remove the old cost ceiling/i }),
      );
      await user.click(within(dialog).getByRole("button", { name: "Run more rounds" }));

      const body = await sentBody(fetchMock);
      expect("budget_usd" in body).toBe(false);
      expect(body.add_rounds).toBe(2);
    });

    it("still talks about the run's budget in calls", async () => {
      const { dialog } = await openCapped();

      expect(within(dialog).getByLabelText("Call budget")).toBeInTheDocument();
      expect(dialog).toHaveTextContent(/It has used 23 of 60\./);
    });
  });

  it("does nothing on cancel", async () => {
    const { dialog, user, fetchMock } = await openDialog();

    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/controls")),
    ).toBe(false);
  });

  it("opens the event stream again, because the run is live once more", async () => {
    // `{accepted: true, lifecycle: "queued"}` puts the run back into an active
    // lifecycle, and the workspace only holds an SSE subscription open while it
    // is in one. A ticket request after the control and none before it is that
    // subscription being mounted.
    const detail = finished();
    const fetchMock = vi.fn(
      mockFetchRoutes([
        { match: "/events/ticket", json: { ticket: "t", expires_in: 60 } },
        { match: "/controls", json: { accepted: true, lifecycle: "queued" } },
        { match: "/detail", json: detail },
        { match: "/runs/run-1", json: detail.run },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderRun();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Run more rounds" }));

    const ticketsBefore = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes("/events/ticket"),
    ).length;
    expect(ticketsBefore).toBe(0);

    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Run more rounds" }),
    );

    await vi.waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("/events/ticket")),
      ).toBe(true);
    });
  });
});

describe("a continue the server refuses", () => {
  /** The dialog stays open on a refusal — every one of them is actionable. */
  async function refuse(status: number, body: Record<string, unknown>) {
    const detail = makeRunDetail({
      run: makeRunSummary({ lifecycle: "completed", calls_used: 23, budget_calls: 60 }),
      rounds: [
        {
          round: 1,
          status: "completed",
          started_at: null,
          completed_at: "2026-08-01T10:20:00Z",
          hypotheses_added: 6,
          matches_completed: 4,
          matches_planned: 4,
          reviews: 6,
          graft_fired: false,
        },
      ],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        mockFetchRoutes([
          { match: "/events/ticket", json: { ticket: "t", expires_in: 60 } },
          { match: "/controls", status, json: body },
          { match: "/detail", json: detail },
          { match: "/runs/run-1", json: detail.run },
        ]),
      ),
    );
    renderRun();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Run more rounds" }));
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Run more rounds" }),
    );
    return screen.getByRole("dialog");
  }

  it("names the run holding the harness lane and offers to open it", async () => {
    const dialog = await refuse(409, {
      code: "lane_busy",
      message: "The claude lane is already held by run other-run.",
      details: { harness: "claude", conflicting_run_id: "other-run" },
    });

    await vi.waitFor(() => {
      expect(dialog).toHaveTextContent(/harness is busy/i);
    });
    expect(
      within(dialog).getByRole("link", { name: /open the run holding it/i }),
    ).toHaveAttribute("href", "/runs/other-run");
  });

  it("says what is legal now when the run is no longer finished", async () => {
    const dialog = await refuse(409, {
      code: "illegal_transition",
      message: "Cannot continue a run that is running.",
      details: { action: "continue", lifecycle: "running", allowed: ["pause", "stop"] },
    });

    await vi.waitFor(() => {
      expect(dialog).toHaveTextContent(/no longer finished/i);
    });
    // The scientist's words for those actions, never the wire values.
    expect(dialog).toHaveTextContent("Pause, Stop");
    expect(dialog).not.toHaveTextContent("force_stop");
  });

  it("puts the budget refusal beside the field it is about", async () => {
    const dialog = await refuse(400, {
      code: "invalid_run_request",
      message:
        "This run has used 23 of its 24 calls. Continuing needs budget_calls of at least 26.",
      details: null,
    });

    await vi.waitFor(() => {
      expect(dialog).toHaveTextContent(/at least 26/);
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps the dialog open so the numbers can be corrected", async () => {
    const dialog = await refuse(422, {
      code: "http_422",
      message: "add_rounds must be at least 1.",
      details: null,
    });

    await vi.waitFor(() => {
      expect(dialog).toHaveTextContent(/add_rounds must be at least 1/);
    });
    expect(within(dialog).getByLabelText("Rounds to add")).toBeInTheDocument();
  });
});

describe("the trust line", () => {
  it("shows the call in flight, its model and how long it has been running", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({ lifecycle: "running" }),
        recent_events: [
          makeEvent({
            seq: 1,
            type: "call_started",
            ts: agoIso(47),
            payload: { role: "reflection", model: "claude-sonnet-5", round: 2 },
          }),
        ],
      }),
    );
    renderRun();

    const trust = await screen.findByRole("status");
    expect(trust).toHaveTextContent("Reflection");
    expect(trust).toHaveTextContent("claude-sonnet-5");
    expect(trust).toHaveTextContent(/4[5-9]s/);
  });

  it("says nothing is in flight once the call has finished", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({ lifecycle: "running" }),
        recent_events: [
          makeEvent({
            seq: 1,
            type: "call_started",
            ts: agoIso(20),
            payload: { role: "reflection", model: "claude-sonnet-5", round: 2 },
          }),
          makeEvent({
            seq: 2,
            type: "call_finished",
            ts: agoIso(5),
            payload: { role: "reflection", model: "claude-sonnet-5", round: 2, ok: true },
          }),
        ],
      }),
    );
    renderRun();

    expect(await screen.findByRole("status")).toHaveTextContent("Between steps");
  });

  it("turns the quiet chip amber past ninety seconds", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({ lifecycle: "running" }),
        recent_events: [
          makeEvent({
            seq: 1,
            type: "round_started",
            ts: agoIso(200),
            payload: { round: 2 },
          }),
        ],
      }),
    );
    renderRun();

    const chip = await screen.findByTitle(/nothing has happened for a while/i);
    expect(chip).toHaveAttribute("data-tone", "caution");
  });

  /**
   * `degraded_count` counts a model or effort *substitution* and has been 0 for
   * every run this engine has ever executed — a timed-out or unusable call goes
   * down a disjoint path and cannot increment it. It was the only failure signal
   * the UI read, which is why run c4566ed2 showed nothing at all after losing
   * three whole steps.
   */
  it("counts the work the run lost and points at the log", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({ lifecycle: "running", lost_steps: 3, failed_calls: 8 }),
        degraded_count: 0,
        lost_steps: 3,
        failed_calls: 8,
      }),
    );
    renderRun();

    expect(await screen.findByRole("link", { name: /3 lost/i })).toHaveAttribute(
      "href",
      "/runs/run-1?tab=activity",
    );
  });

  it("says a failure was retried rather than lost when nothing was actually lost", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({ lifecycle: "running", lost_steps: 0, failed_calls: 2 }),
        lost_steps: 0,
        failed_calls: 2,
      }),
    );
    renderRun();

    const chip = await screen.findByRole("link", { name: /2 retried/i });
    expect(chip).toHaveAttribute("data-tone", "caution");
  });

  it("shows no failure badge at all on a clean run", async () => {
    stub(makeRunDetail({ run: makeRunSummary({ lifecycle: "running" }) }));
    renderRun();

    await screen.findByRole("tab", { name: /live/i });
    expect(screen.queryByRole("link", { name: /lost|retried/i })).toBeNull();
  });
});

/**
 * Money is not a constraint on this app: the calls run on a subscription and
 * nothing is billed per token. The governor on screen is therefore calls, and a
 * dollar figure is at most a labelled footnote — never a ring, never a ceiling,
 * never "$0.00".
 */
describe("calls, not money, are the governor", () => {
  it("counts calls in the ring and never puts a price in the header", async () => {
    stub(makeRunDetail({ run: makeRunSummary({ spend_usd: 4.2 }) }));
    renderRun();

    const ring = await screen.findByRole("img", { name: /23 of 60 model calls used/i });
    expect(ring).toHaveTextContent("23");
    expect(ring).toHaveTextContent("calls");

    const header = screen.getByLabelText("Run progress");
    expect(header).toHaveTextContent("37 left in budget");
    expect(header).toHaveTextContent("Elapsed");
    expect(header).toHaveTextContent("Tokens");
    expect(header).not.toHaveTextContent("$");
  });

  it("shows no ceiling and no gauge when the run has no call budget", async () => {
    stub(makeRunDetail({ run: makeRunSummary({ calls_used: 11, budget_calls: 0 }) }));
    renderRun();

    const header = await screen.findByLabelText("Run progress");
    expect(header).toHaveTextContent("no call ceiling set");
    expect(header).not.toHaveTextContent("11/0");
    expect(
      screen.queryByRole("img", { name: /model calls used/i }),
    ).not.toBeInTheDocument();
  });

  it("does not invent a cost ceiling for a run that has none", async () => {
    stub(
      makeRunDetail({
        config: { ...makeRunConfig(), budget_usd: null } as unknown as Record<
          string,
          unknown
        >,
      }),
    );
    renderRun("/runs/run-1?tab=settings");

    expect(await screen.findByText(/cost does not limit this run/i)).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
    expect(screen.queryByText(/of \$/)).not.toBeInTheDocument();
  });

  it("keeps the API-equivalent figure as a labelled footnote, not a column", async () => {
    stub(
      makeRunDetail({
        budget: {
          calls_used: 23,
          budget_calls: 60,
          spend_usd: 2.12,
          budget_usd: null as unknown as number,
          by_role: [{ role: "generation", calls: 9, tokens: 120_000, usd: 0.9 }],
        },
      }),
    );
    renderRun("/runs/run-1?tab=settings");

    const footnote = await screen.findByText(/API-equivalent/);
    expect(footnote).toHaveTextContent("$2.12");
    expect(footnote).toHaveTextContent(/not billed per token/i);
    expect(footnote).toHaveClass("footnote");
    expect(screen.queryByRole("columnheader", { name: /cost/i })).not.toBeInTheDocument();
  });

  it("says nothing rather than pricing a run at zero", async () => {
    stub(
      makeRunDetail({
        budget: {
          calls_used: 4,
          budget_calls: 60,
          spend_usd: 0,
          budget_usd: null as unknown as number,
          by_role: [{ role: "generation", calls: 4, tokens: 1000, usd: 0 }],
        },
      }),
    );
    renderRun("/runs/run-1?tab=settings");

    expect(
      await screen.findByText(/no api-equivalent cost was recorded/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument();
  });
});

/** A demo run is scripted end to end. It never invoked anything, so it may not
 *  name anything — the model chip already refuses to, and so must the log. */
describe("a demo run names no model anywhere", () => {
  const NEVER_CALLED = "model-a-real-run-would-have-used";

  const demoDetail = makeRunDetail({
    run: makeRunSummary({ harness: "demo", lifecycle: "running" }),
    model_table: [{ role: "generation", model: NEVER_CALLED, effort: "high" }],
    recent_events: [
      makeEvent({
        seq: 1,
        type: "call_started",
        ts: agoIso(6),
        payload: { role: "generation", model: NEVER_CALLED, round: 2 },
      }),
    ],
  });

  it("says so in the event log instead of naming the model", async () => {
    stub(demoDetail);
    renderRun("/runs/run-1?tab=activity");

    expect(
      await screen.findByText(/Generation · Demo · no model call/),
    ).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(NEVER_CALLED))).not.toBeInTheDocument();
  });

  it("agrees with itself on the trust line", async () => {
    stub(demoDetail);
    renderRun();

    const trust = await screen.findByRole("status");
    expect(trust).toHaveTextContent("Generation");
    expect(trust).toHaveTextContent("Demo · no model call");
    expect(trust).not.toHaveTextContent(NEVER_CALLED);
  });

  it("prints no resolved model table in settings", async () => {
    stub(demoDetail);
    renderRun("/runs/run-1?tab=settings");

    expect(await screen.findByText(/makes no model calls at all/i)).toBeInTheDocument();
    expect(screen.queryByText(NEVER_CALLED)).not.toBeInTheDocument();
  });
});

/** A substitution fails nothing and warns nobody. Activity is where it shows. */
describe("a run that quietly used a different model", () => {
  it("names what was asked for and what actually ran", async () => {
    stub(
      makeRunDetail({
        recent_events: [
          makeEvent({
            seq: 1,
            round: 3,
            type: "call_finished",
            payload: {
              role: "ranking",
              ok: true,
              telemetry: {
                model_requested: "model-asked-for",
                model_ran: "model-that-answered",
                model_substituted: true,
                model_below_floor: true,
              },
            },
          }),
        ],
      }),
    );
    renderRun("/runs/run-1?tab=activity");

    const panel = (
      await screen.findByText(/calls that ran on a different model/i)
    ).closest("section");
    expect(panel).not.toBeNull();
    expect(panel).toHaveTextContent("Tournament");
    expect(panel).toHaveTextContent("model-asked-for");
    expect(panel).toHaveTextContent("model-that-answered");
    expect(panel).toHaveTextContent(/below the floor/i);
  });

  it("puts a denied call from the same telemetry into the forensics panel", async () => {
    stub(
      makeRunDetail({
        recent_events: [
          makeEvent({
            seq: 1,
            round: 1,
            type: "call_finished",
            payload: {
              role: "generation",
              ok: false,
              telemetry: { permission_denials: [{ tool: "WebSearch" }] },
            },
          }),
        ],
      }),
    );
    renderRun("/runs/run-1?tab=activity");

    const panel = (await screen.findByText(/problems it worked around/i)).closest(
      "section",
    );
    expect(panel).not.toBeNull();
    expect(panel).toHaveTextContent(/denied a tool it had been given/i);
    expect(panel).toHaveTextContent(/"tool": "WebSearch"/);
  });

  it("shows no such panel when every call ran on the model it was given", async () => {
    stub(makeRunDetail());
    renderRun("/runs/run-1?tab=activity");

    await screen.findByText(/event log/i);
    expect(
      screen.queryByText(/calls that ran on a different model/i),
    ).not.toBeInTheDocument();
  });
});

describe("imported runs", () => {
  const importedDetail = makeRunDetail({
    run: makeRunSummary({
      source: "imported",
      lifecycle: "completed",
      calls_used: 129,
      spend_usd: 0,
      budget_calls: 0,
    }),
    leaderboard: [makeHypothesisRow()],
  });

  it("state their calls as history and never price them", async () => {
    stub(importedDetail);
    renderRun();

    expect(await screen.findByText(/recorded \(historical\)/i)).toBeInTheDocument();
    expect(screen.getByText("129")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Run progress")).not.toHaveTextContent("$");
  });

  it("offer no controls and no budget ring", async () => {
    stub(importedDetail);
    renderRun();
    await screen.findByRole("heading", { name: /photocatalytic/i });

    expect(screen.queryByRole("button", { name: /^stop$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("img", { name: /model calls used/i }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("state the call ceiling as absent rather than as zero", async () => {
    // Imported runs carry `budget_calls: 0` and no stored config. Printed flat,
    // that reads as a run allowed no calls at all — on the one screen that
    // states the ceiling of the thing now governing the whole interface.
    stub({ ...importedDetail, config: {} });
    renderRun("/runs/run-1?tab=settings");

    await screen.findByText(/how it was set up/i);
    const setup = screen.getByText(/how it was set up/i).closest("section")!;
    expect(within(setup).getByText(/none recorded — imported run/i)).toBeVisible();
    expect(within(setup).queryByText("0")).toBeNull();
    // And with no config there is no cost ceiling to name either.
    expect(within(setup).getByText(/cost does not limit this run/i)).toBeVisible();
  });

  it("explain why there is no live feed", async () => {
    stub(importedDetail);
    renderRun();

    expect(
      await screen.findByText(/imported from an earlier engine/i),
    ).toBeInTheDocument();
  });
});

describe("the leaderboard", () => {
  const detail = makeRunDetail({
    leaderboard: [
      makeHypothesisRow({ hid: "h003", title: "Strongest idea", elo: 1290, matches: 5 }),
      makeHypothesisRow({ hid: "h001", title: "Middle idea", elo: 1210, matches: 4 }),
      makeHypothesisRow({ hid: "h007", title: "Weakest idea", elo: 1140, matches: 3 }),
    ],
  });

  it("ranks strongest first and links each row to the hypothesis", async () => {
    stub(detail);
    renderRun();

    await screen.findByText("Strongest idea");
    const rows = screen
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href")?.includes("/hypotheses/"));
    expect(rows.map((row) => row.getAttribute("href"))).toEqual([
      "/runs/run-1/hypotheses/h003",
      "/runs/run-1/hypotheses/h001",
      "/runs/run-1/hypotheses/h007",
    ]);
  });

  it("prints the scale it is drawing against", async () => {
    stub(detail);
    renderRun();

    const scale = (await screen.findByText("Elo 1140")).parentElement;
    expect(scale).toHaveTextContent("Elo 1140");
    expect(scale).toHaveTextContent("1290");
  });

  /**
   * Every run ends with `evolve_top_k` hypotheses created after the final
   * tournament: never reviewed, never matched, sitting at the untouched default
   * of 1200. The panel is headed "Standings — Ranked by Elo" and printed rank
   * ordinals over them, so an idea that never competed appeared above every idea
   * that competed and lost, with no visual signal of any kind.
   */
  it("gives no rank and no rating to an idea that has never played a match", async () => {
    stub(
      makeRunDetail({
        leaderboard: [
          makeHypothesisRow({
            hid: "h003",
            title: "Fought and won",
            elo: 1290,
            matches: 5,
          }),
          makeHypothesisRow({
            hid: "h001",
            title: "Fought and lost",
            elo: 1140,
            matches: 3,
          }),
          makeHypothesisRow({
            hid: "h009",
            title: "Never played",
            elo: 1200,
            matches: 0,
            wins: 0,
          }),
        ],
      }),
    );
    renderRun();

    const unranked = (await screen.findByText("Never played")).closest("a")!;
    expect(unranked).toHaveTextContent(/unranked/i);
    expect(unranked).not.toHaveTextContent("1200");
    expect(unranked).not.toHaveTextContent("3");

    const ranked = screen.getByText("Fought and lost").closest("a")!;
    expect(ranked).toHaveTextContent("1140");
  });

  it("gives no rank to an idea review rejected, whatever rating it kept", async () => {
    stub(
      makeRunDetail({
        leaderboard: [
          makeHypothesisRow({
            hid: "h003",
            title: "Still in play",
            elo: 1210,
            matches: 5,
          }),
          makeHypothesisRow({
            hid: "h005",
            title: "Rejected in review",
            status: "rejected",
            elo: 1300,
            matches: 2,
          }),
        ],
      }),
    );
    renderRun();

    const removed = (await screen.findByText("Rejected in review")).closest("a")!;
    expect(removed).toHaveTextContent(/rejected/i);
    expect(removed).not.toHaveTextContent("1300");
  });

  it("says plainly when every hypothesis was rejected", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({
          counts: { active: 0, rejected: 18, archived: 0, matches: 0 },
        }),
        leaderboard: Array.from({ length: 18 }, (_, index) =>
          makeHypothesisRow({
            hid: `h${String(index + 1).padStart(3, "0")}`,
            id: `hyp-${index}`,
            status: "rejected",
            elo: 1200,
            matches: 0,
            wins: 0,
          }),
        ),
      }),
    );
    renderRun();

    expect(
      await screen.findByText(/all 18 hypotheses were rejected in review/i),
    ).toBeInTheDocument();
  });
});

describe("tabs", () => {
  it("keep the open tab in the URL", async () => {
    stub(makeRunDetail());
    renderRun("/runs/run-1?tab=settings");

    expect(await screen.findByText(/how it was set up/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /settings/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("show the configuration a run cannot change", async () => {
    stub(makeRunDetail());
    renderRun("/runs/run-1?tab=settings");

    expect(await screen.findByText("Fixed at launch")).toBeInTheDocument();
    expect(screen.getByText("Standard — verify key claims")).toBeInTheDocument();
    // The stored id, printed through the same catalog the wizard prints through
    // — one vocabulary for a model across the whole app. A model the catalog no
    // longer lists still shows what the run used, shortened.
    expect(screen.getByText(modelLabel("claude-sonnet-5", [])!)).toBeInTheDocument();
  });

  it("open the hypotheses tab with every hypothesis in it", async () => {
    stub(
      makeRunDetail({
        leaderboard: [
          makeHypothesisRow({ hid: "h001", title: "First idea" }),
          makeHypothesisRow({
            hid: "h002",
            id: "hyp-2",
            title: "Rejected idea",
            status: "rejected",
          }),
        ],
      }),
      // The tab opens on the genealogy now, which is a second request.
      [
        {
          match: "/graph",
          json: makeGraph({
            nodes: [
              makeGraphNode({ hid: "h001", title: "First idea", is_leader: true }),
              makeGraphNode({
                hid: "h002",
                id: "hyp-2",
                title: "Rejected idea",
                status: "rejected",
              }),
            ],
            edges: [],
            meta: makeMeta({ rounds: 1, has_lineage: false, node_count: 2 }),
          }),
        },
      ],
    );
    renderRun("/runs/run-1?tab=hypotheses");
    const user = userEvent.setup();

    // Both ideas are on the canvas, the rejected one legibly rejected.
    expect(
      await screen.findByRole("button", { name: /^h001 — First idea/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^h002 — Rejected idea/ })).toHaveAttribute(
      "data-status",
      "rejected",
    );

    // And both are still readable as the ranked list this tab used to be.
    await user.click(screen.getByRole("tab", { name: /list/i }));
    expect(screen.getByText("First idea")).toBeInTheDocument();
    expect(screen.getByText("Rejected idea")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rejected 1" })).toBeInTheDocument();
  });
});

describe("the activity tab", () => {
  it("filters by what kind of thing happened", async () => {
    stub(
      makeRunDetail({
        recent_events: [
          makeEvent({
            seq: 1,
            type: "hypothesis_added",
            payload: { hid: "h001", title: "An idea", round: 1, source: "agent" },
          }),
          makeEvent({
            seq: 2,
            type: "call_started",
            payload: { role: "ranking", model: "claude-sonnet-5", round: 1 },
          }),
        ],
      }),
    );
    renderRun("/runs/run-1?tab=activity");
    const user = userEvent.setup();

    expect(await screen.findByText(/h001 — An idea/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Model calls" }));

    expect(screen.queryByText(/h001 — An idea/)).not.toBeInTheDocument();
    expect(screen.getByText(/Tournament · claude-sonnet-5/)).toBeInTheDocument();
  });

  it("shows a line it could not parse rather than swallowing it", async () => {
    stub(
      makeRunDetail({
        recent_events: [
          makeEvent({ seq: 1, type: "malformed", payload: { raw: "{not json" } }),
        ],
      }),
    );
    renderRun("/runs/run-1?tab=activity");

    expect(await screen.findByText("Unreadable line")).toBeInTheDocument();
    expect(screen.getByText("{not json")).toBeInTheDocument();
    expect(screen.getByText(/could not be parsed/i)).toBeInTheDocument();
  });

  it("makes a failed run diagnosable", async () => {
    stub(
      makeRunDetail({
        run: makeRunSummary({ lifecycle: "failed" }),
        recent_events: [
          makeEvent({
            seq: 1,
            type: "run_failed",
            payload: {
              error: {
                message: "Grounded call was denied WebSearch",
                permission_denials: [{ tool: "WebSearch", reason: "not allowed" }],
              },
            },
          }),
        ],
      }),
    );
    renderRun("/runs/run-1?tab=activity");

    const panel = (await screen.findByText(/why this run ended/i)).closest("section");
    expect(panel).not.toBeNull();
    expect(panel).toHaveTextContent(/grounded call was denied websearch/i);
    expect(panel).toHaveTextContent(/denied a tool it had been given/i);
    expect(panel).toHaveTextContent(/"tool": "WebSearch"/);
  });
});

describe("the report tab", () => {
  it("reads the overview as a document", async () => {
    stub(makeRunDetail({ run: makeRunSummary({ has_overview: true }) }), [
      {
        match: "/overview",
        text: "# Findings\n\nAmmonia at ambient pressure is **plausible**.\n",
      },
    ]);
    renderRun("/runs/run-1?tab=report");

    expect(await screen.findByRole("heading", { name: "Findings" })).toBeInTheDocument();
    expect(screen.getByText("plausible")).toBeInTheDocument();
  });

  it("does not call a missing report an error", async () => {
    stub(makeRunDetail({ run: makeRunSummary({ lifecycle: "running" }) }));
    renderRun("/runs/run-1?tab=report");

    expect(await screen.findByText(/no report yet/i)).toBeInTheDocument();
    expect(screen.getByText(/also when you stop it/i)).toBeInTheDocument();
  });

  it("does not ask for a report the run says it never wrote", async () => {
    const fetchMock = stub(
      makeRunDetail({ run: makeRunSummary({ lifecycle: "completed" }) }),
    );
    renderRun("/runs/run-1?tab=report");

    expect(await screen.findByText(/no report was written/i)).toBeInTheDocument();
    const asked = fetchMock.mock.calls.some(([url]) => String(url).includes("/overview"));
    expect(asked).toBe(false);
  });

  it("shows the guidance trail between rounds", async () => {
    stub(
      makeRunDetail({
        feedback_history: [
          { round: 1, guidance: "Push harder on mechanisms." },
          { round: 2, guidance: "Two ideas are converging; diversify." },
        ],
      }),
      [{ match: "/overview", text: "# Findings" }],
    );
    renderRun("/runs/run-1?tab=report");

    expect(await screen.findByText("Push harder on mechanisms.")).toBeInTheDocument();
    expect(screen.getByText("Round 2")).toBeInTheDocument();
  });
});

/**
 * `run.round` is the round *in progress*, not the last one finished. On a
 * terminal run the track coloured every step `<= run.round` as done, so a run
 * whose last round died mid-way claimed a round of work it never did — run
 * c4566ed2 rendered "2 of 3" against a recorded `rounds_completed: 1`. The tab
 * was already inconsistent with itself: `LiveTab` computes the right number and
 * hands it to the control bar.
 */
describe("the round track", () => {
  function withRounds(statuses: string[], round: number, lifecycle: Lifecycle) {
    return makeRunDetail({
      run: makeRunSummary({ lifecycle, round, rounds_target: 3 }),
      rounds: statuses.map((status, index) => ({
        round: index + 1,
        status,
        started_at: "2026-08-01T10:00:00Z",
        completed_at: status === "completed" ? "2026-08-01T10:20:00Z" : null,
        hypotheses_added: 6,
        matches_completed: 4,
        matches_planned: 4,
        reviews: 6,
        graft_fired: false,
      })),
    });
  }

  it("does not call an abandoned round done", async () => {
    const detail = withRounds(["completed", "incomplete"], 2, "completed");
    stub({ ...detail, run: detail.run });
    renderRun();

    const track = await screen.findByRole("img", { name: /round 2 of 3/i });
    const steps = Array.from(track.querySelectorAll(".round-step"));
    expect(steps.map((step) => step.getAttribute("data-state"))).toEqual([
      "done",
      "abandoned",
      "todo",
    ]);
  });

  it("still marks the round in flight as current on a live run", async () => {
    const detail = withRounds(["completed", "running"], 2, "running");
    stub({ ...detail, run: detail.run });
    renderRun();

    const track = await screen.findByRole("img", { name: /round 2 of 3/i });
    const steps = Array.from(track.querySelectorAll(".round-step"));
    expect(steps.map((step) => step.getAttribute("data-state"))).toEqual([
      "done",
      "current",
      "todo",
    ]);
  });
});

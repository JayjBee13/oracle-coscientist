import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import {
  makeCapabilities,
  makeRunDetail,
  makeRunSummary,
  mockFetchRoutes,
} from "../test/fixtures";
import * as api from "../api/client";
import { THEME_KEY } from "../lib/theme";
import { fetchIdentity } from "../store/identity";
import { makeHypothesisDetail, makeHypothesisRow } from "../test/runFixtures";
import { App } from "./App";

function stubRuns(items = [makeRunSummary()]) {
  const detail = makeRunDetail({
    run: items[0] ?? makeRunSummary(),
    leaderboard: [makeHypothesisRow()],
  });
  const fetchMock = vi.fn(
    mockFetchRoutes([
      { match: "/capabilities", json: makeCapabilities() },
      { match: "/admin/halt-all", json: { stopped: items.map((item) => item.id) } },
      { match: "/hypotheses/", json: makeHypothesisDetail() },
      { match: "/events/ticket", json: { ticket: "t", expires_in: 60 } },
      { match: "/detail", json: detail },
      { match: "/runs", json: { items, total: items.length } },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderApp(path = "/", strict = false) {
  const tree = (
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

describe("app shell", () => {
  it("shows the signed-in person and their personal workspace", async () => {
    const fetchMock = vi.fn(
      mockFetchRoutes([
        {
          match: "/me",
          json: {
            username: "alice",
            email: "alice@example.test",
            display_name: "Alice Researcher",
            groups: [],
            is_admin: false,
            source: "gateway",
          },
        },
        { match: "/capabilities", json: makeCapabilities() },
        { match: "/runs", json: { items: [], total: 0 } },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderApp();

    expect(
      await screen.findByLabelText(/signed in as Alice Researcher, personal workspace/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Your research runs" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /stop all/i })).not.toBeInTheDocument();
  });

  it("fails closed on a gateway 401 and offers no Oracle password form", async () => {
    const fetchMock = vi.fn(
      mockFetchRoutes([
        {
          match: "/me",
          status: 401,
          json: { code: "gateway_auth_required", message: "Gateway identity required." },
        },
        {
          match: "/runs",
          json: { items: [makeRunSummary({ title: "Private run" })], total: 1 },
        },
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    renderApp();

    expect(
      await screen.findByText(/sign in through the Oracle gateway/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /open Oracle through the gateway/i }),
    ).toHaveAttribute("href", "/");
    expect(screen.queryByText("Private run")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/runs"))).toBe(
      false,
    );
  });

  it("removes cached workspace data as soon as identity revalidation fails", async () => {
    let rejectIdentity = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/me")) {
          return new Response(
            JSON.stringify(
              rejectIdentity
                ? { code: "gateway_auth_required", message: "Gateway identity required." }
                : {
                    username: "local-owner",
                    email: null,
                    display_name: "Local Owner",
                    groups: ["admin"],
                    is_admin: true,
                    source: "local",
                  },
            ),
            {
              status: rejectIdentity ? 401 : 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        if (url.includes("/capabilities")) {
          return new Response(JSON.stringify(makeCapabilities()), { status: 200 });
        }
        if (url.includes("/runs")) {
          return new Response(
            JSON.stringify({
              items: [makeRunSummary({ title: "Cached private run" })],
              total: 1,
            }),
            { status: 200 },
          );
        }
        return new Response("{}", { status: 404 });
      }),
    );
    renderApp();
    expect((await screen.findAllByText("Cached private run")).length).toBeGreaterThan(0);

    rejectIdentity = true;
    await act(async () => {
      await fetchIdentity({ force: true });
    });

    expect(await screen.findByText(/workspace session is missing/i)).toBeInTheDocument();
    expect(screen.queryByText("Cached private run")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/signed in as Local Owner/i)).not.toBeInTheDocument();
  });

  it("does not let a delayed identity response undo a protected-request 401", async () => {
    let meCalls = 0;
    let resolveRevalidation: (response: Response) => void = () => {
      throw new Error("Revalidation did not start.");
    };
    const identityBody = {
      username: "alice",
      email: null,
      display_name: "Alice",
      groups: [],
      is_admin: false,
      source: "gateway",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/me")) {
          meCalls += 1;
          if (meCalls === 1) {
            return new Response(JSON.stringify(identityBody), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            });
          }
          return new Promise<Response>((resolve) => {
            resolveRevalidation = resolve;
          });
        }
        if (url.includes("/runs/denied/detail")) {
          return new Response(
            JSON.stringify({
              code: "gateway_auth_required",
              message: "Session expired.",
            }),
            { status: 401, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/capabilities")) {
          return new Response(JSON.stringify(makeCapabilities()), { status: 200 });
        }
        if (url.includes("/runs")) {
          return new Response(JSON.stringify({ items: [], total: 0 }), { status: 200 });
        }
        return new Response("{}", { status: 404 });
      }),
    );
    renderApp();
    expect(await screen.findByLabelText(/signed in as Alice/i)).toBeInTheDocument();

    let revalidation = Promise.resolve();
    await act(async () => {
      revalidation = fetchIdentity({ force: true });
    });
    await vi.waitFor(() => expect(meCalls).toBe(2));
    await act(async () => {
      void api.getRunDetail("denied");
      await vi.waitFor(() => {
        expect(screen.getByText(/workspace session is missing/i)).toBeInTheDocument();
      });
      resolveRevalidation(
        new Response(JSON.stringify(identityBody), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
      await revalidation;
    });

    expect(
      await screen.findByText(/sign in through the Oracle gateway/i),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/signed in as Alice/i)).not.toBeInTheDocument();
  });

  it("clears administrator data when the same username loses administrator access", async () => {
    let isAdmin = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/me")) {
          return new Response(
            JSON.stringify({
              username: "alice",
              email: null,
              display_name: "Alice",
              groups: isAdmin ? ["admin"] : [],
              is_admin: isAdmin,
              source: "gateway",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/capabilities")) {
          return new Response(JSON.stringify(makeCapabilities()), { status: 200 });
        }
        if (url.includes("/runs")) {
          const title = isAdmin ? "Administrator-only cached run" : "Alice personal run";
          return new Response(
            JSON.stringify({ items: [makeRunSummary({ title })], total: 1 }),
            { status: 200 },
          );
        }
        return new Response("{}", { status: 404 });
      }),
    );
    renderApp();
    expect(
      (await screen.findAllByText("Administrator-only cached run")).length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /stop all/i })).toBeInTheDocument();

    isAdmin = false;
    await act(async () => {
      await fetchIdentity({ force: true });
    });

    expect((await screen.findAllByText("Alice personal run")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Administrator-only cached run")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /stop all/i })).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Your research runs" }),
    ).toBeInTheDocument();
  });

  it("shows a lane per harness with the run occupying it", async () => {
    stubRuns([
      makeRunSummary({ id: "a", title: "Ammonia routes", lifecycle: "running" }),
    ]);
    renderApp();

    const lanes = await screen.findByRole("navigation", { name: /harness lanes/i });
    expect(await within(lanes).findByText("Ammonia routes")).toBeInTheDocument();
    expect(within(lanes).getByText("Demo")).toBeInTheDocument();
    expect(within(lanes).getByText("Idle")).toBeInTheDocument();
  });

  it("reports the connection state in the top bar", async () => {
    stubRuns();
    renderApp();

    expect(await screen.findByTitle(/talking to the backend/i)).toHaveTextContent(
      "Connected",
    );
  });

  it("survives StrictMode's double mount without refetching", async () => {
    const fetchMock = stubRuns();
    renderApp("/", true);

    expect(
      await screen.findByRole("link", { name: "Photocatalytic routes to ammonia" }),
    ).toBeInTheDocument();
    // Two distinct questions are asked once each — the top bar's unfiltered
    // lane rail and the list's own query. Nothing is asked for twice, which is
    // what the double mount would otherwise cause.
    const runsCalls = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => new URL(url).pathname === "/api/runs");
    expect(new Set(runsCalls).size).toBe(runsCalls.length);
    expect(runsCalls).toHaveLength(2);
  });
});

describe("stop all", () => {
  it("is disabled when nothing is in flight", async () => {
    stubRuns([makeRunSummary({ lifecycle: "completed" })]);
    renderApp();

    const button = await screen.findByRole("button", { name: /stop all/i });
    await vi.waitFor(() => {
      expect(button).toBeDisabled();
    });
  });

  it("names the runs it would stop before doing it", async () => {
    const user = userEvent.setup();
    const fetchMock = stubRuns([
      makeRunSummary({ id: "a", title: "Ammonia routes", lifecycle: "running" }),
      makeRunSummary({
        id: "b",
        title: "Demo run",
        harness: "demo",
        lifecycle: "paused",
      }),
    ]);
    renderApp();

    const button = await screen.findByRole("button", { name: /stop all/i });
    await vi.waitFor(() => {
      expect(button).toBeEnabled();
    });
    await user.click(button);

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Stop 2 runs?");
    expect(within(dialog).getByText("Ammonia routes")).toBeInTheDocument();
    expect(within(dialog).getByText("Demo run")).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/still writes a report/i);

    await user.click(within(dialog).getByRole("button", { name: /stop them/i }));

    await vi.waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).includes("/admin/halt-all")),
      ).toBe(true);
    });
    expect(await screen.findByRole("status")).toHaveTextContent(/stopping 2 runs/i);
  });

  it("can be cancelled without touching anything", async () => {
    const user = userEvent.setup();
    const fetchMock = stubRuns([makeRunSummary({ lifecycle: "running" })]);
    renderApp();

    const button = await screen.findByRole("button", { name: /stop all/i });
    await vi.waitFor(() => {
      expect(button).toBeEnabled();
    });
    await user.click(button);
    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("halt-all")),
    ).toBe(false);
  });
});

describe("theme toggle", () => {
  it("names the state it is in and the one a click would give", async () => {
    stubRuns();
    renderApp();

    const button = await screen.findByRole("button", { name: /^theme:/i });
    expect(button).toHaveAccessibleName("Theme: system — click for light");
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("cycles system → light → dark → system, storing and stamping each", async () => {
    const user = userEvent.setup();
    stubRuns();
    renderApp();

    const button = await screen.findByRole("button", { name: /^theme:/i });

    await user.click(button);
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("light");
    expect(button).toHaveAccessibleName("Theme: light — click for dark");

    await user.click(button);
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(button).toHaveAccessibleName("Theme: dark — click for system");

    // Back to following the OS, which is the state a two-state toggle cannot
    // return to.
    await user.click(button);
    expect(document.documentElement).not.toHaveAttribute("data-theme");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("system");
    expect(button).toHaveAccessibleName("Theme: system — click for light");
  });

  it("paints the stored choice on mount, without a click", async () => {
    window.localStorage.setItem(THEME_KEY, "dark");
    stubRuns();
    renderApp();

    expect(
      await screen.findByRole("button", { name: "Theme: dark — click for system" }),
    ).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("ignores a malformed stored value and follows the system", async () => {
    window.localStorage.setItem(THEME_KEY, "midnight");
    stubRuns();
    renderApp();

    expect(
      await screen.findByRole("button", { name: "Theme: system — click for light" }),
    ).toBeInTheDocument();
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });
});

describe("routing", () => {
  it("routes to the launch wizard", async () => {
    stubRuns();
    renderApp("/new");
    expect(
      await screen.findByRole("heading", { name: /start a research run/i }),
    ).toBeInTheDocument();
  });

  it("routes to a run workspace by id", async () => {
    stubRuns();
    renderApp("/runs/run-1");
    expect(await screen.findByRole("tab", { name: /hypotheses/i })).toBeInTheDocument();
  });

  it("routes to a hypothesis inside a run", async () => {
    stubRuns();
    renderApp("/runs/run-1/hypotheses/h001");
    expect(
      await screen.findByRole("heading", { name: "Plasmonic nitrogen fixation" }),
    ).toBeInTheDocument();
  });

  it("asks for two runs before comparing", async () => {
    stubRuns();
    renderApp("/compare");
    expect(await screen.findByText(/pick two runs/i)).toBeInTheDocument();
  });

  it("explains an address that does not exist", async () => {
    stubRuns();
    renderApp("/nowhere");
    expect(await screen.findByText(/nothing lives at this address/i)).toBeInTheDocument();
  });
});

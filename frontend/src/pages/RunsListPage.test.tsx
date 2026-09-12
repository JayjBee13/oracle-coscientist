import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { RunSummary } from "../api/types";
import { makeRunSummary, mockFetchRoutes } from "../test/fixtures";
import { RunsListPage } from "./RunsListPage";

function stub(items: RunSummary[], extra: Parameters<typeof mockFetchRoutes>[0] = []) {
  const fetchMock = vi.fn(
    mockFetchRoutes([...extra, { match: "/runs", json: { items, total: items.length } }]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderList(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <RunsListPage />
    </MemoryRouter>,
  );
}

function lastRunsUrl(fetchMock: ReturnType<typeof vi.fn>): string {
  const calls = fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/runs"));
  return calls[calls.length - 1] ?? "";
}

describe("runs list", () => {
  it("names each run's owner", async () => {
    stub([makeRunSummary({ owner_display_name: "Alice Researcher" })]);
    renderList("/?scope=all");

    expect(await screen.findByText("Alice Researcher")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
  });

  it("defaults administrators to their own runs and requires a deliberate all-users view", async () => {
    const user = userEvent.setup();
    const fetchMock = stub([makeRunSummary()]);
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");

    expect(lastRunsUrl(fetchMock)).toContain("mine=true");
    expect(screen.getByRole("button", { name: "My runs" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.queryByText("Owner")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "All users" }));
    await vi.waitFor(() => expect(lastRunsUrl(fetchMock)).toContain("mine=false"));
    expect(screen.getByText("Owner")).toBeInTheDocument();
  });

  it("does not render ownership scope controls for a non-admin", async () => {
    const fetchMock = stub(
      [makeRunSummary()],
      [
        {
          match: "/me",
          json: {
            username: "alice",
            email: null,
            display_name: "Alice",
            groups: [],
            is_admin: false,
            source: "gateway",
          },
        },
      ],
    );
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");

    expect(screen.queryByRole("button", { name: "My runs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "All users" })).not.toBeInTheDocument();
    expect(lastRunsUrl(fetchMock)).toContain("mine=true");
    expect(
      screen.getByRole("heading", { name: "Your research runs" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/historical research runs are private/i)).toBeInTheDocument();
    expect(screen.queryByText("Owner")).not.toBeInTheDocument();
  });

  it("leads with the run's title, not its engine id", async () => {
    stub([
      makeRunSummary({
        title: "Photocatalytic routes to ammonia",
        engine_run_id: "run-20260602-064935",
      }),
    ]);
    renderList();

    expect(
      await screen.findByRole("link", { name: "Photocatalytic routes to ammonia" }),
    ).toHaveAttribute("href", "/runs/run-1");
    expect(screen.queryByText("run-20260602-064935")).not.toBeInTheDocument();
  });

  it("shows the leading idea, the calls it used and how long ago it moved", async () => {
    stub([makeRunSummary({ spend_usd: 1.42 })]);
    renderList();

    expect(await screen.findByText("Plasmonic nitrogen fixation")).toBeInTheDocument();
    expect(screen.getByText("Elo 1246")).toBeInTheDocument();
    expect(screen.getByText("Calls used / max")).toBeInTheDocument();
    const calls = screen.getByTitle(/maximum allowance/i);
    expect(calls).toHaveTextContent("23 / 60 max");
    expect(calls).toHaveAttribute("title", expect.stringMatching(/stop limit/i));
    expect(calls).toHaveAttribute(
      "title",
      expect.stringMatching(/not completion progress/i),
    );
    expect(screen.getByText("412k tokens")).toBeInTheDocument();
  });

  it("shows normalized model levels and marks custom settings as approximate", async () => {
    stub([
      makeRunSummary({ id: "low", model_level: "low" }),
      makeRunSummary({ id: "med", model_level: "med" }),
      makeRunSummary({ id: "high", model_level: "high" }),
      makeRunSummary({ id: "max", model_level: "max" }),
      makeRunSummary({
        id: "custom",
        model_level: "high",
        model_level_custom: true,
      }),
    ]);
    renderList();

    const recorded = await screen.findAllByTitle(/overall model level recorded/i);
    expect(recorded.map((node) => node.textContent)).toEqual([
      "Low",
      "Med",
      "High",
      "Max",
    ]);
    expect(screen.getByTitle(/custom model settings/i)).toHaveTextContent(
      "Custom · ~High",
    );
  });

  it("shows recorded elapsed time and an honest dash when it is missing", async () => {
    stub([
      makeRunSummary({ id: "finished", lifecycle: "completed", elapsed_seconds: 4260 }),
      makeRunSummary({ id: "legacy", lifecycle: "completed", elapsed_seconds: null }),
    ]);
    renderList();

    expect(
      await screen.findByTitle(/first recorded start to final finish/i),
    ).toHaveTextContent("1h 11m");
    expect(screen.getByTitle(/elapsed time was not recorded/i)).toHaveTextContent("—");
  });

  // Cost is not a constraint here — these are subscription CLI calls — so a
  // dollar figure must not appear as a row metric at all, however it is spelled.
  it("never prices a row, however much it spent", async () => {
    stub([makeRunSummary({ spend_usd: 1.42 })]);
    renderList();

    await screen.findByText("Plasmonic nitrogen fixation");
    expect(screen.queryByText(/\$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^cost$/i)).not.toBeInTheDocument();
  });

  it("counts an imported run's calls as history and never prices it", async () => {
    stub([
      makeRunSummary({
        source: "imported",
        lifecycle: "completed",
        calls_used: 129,
        spend_usd: 0,
      }),
    ]);
    renderList();

    expect(await screen.findByText("129 calls recorded")).toBeInTheDocument();
    expect(screen.getByText("historical")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
    expect(screen.getByTitle(/imported from an earlier engine run/i)).toHaveTextContent(
      "Completed · Imported",
    );
  });

  // The API sends 0, not null: `runs.spend_usd` is NOT NULL and an imported run never
  // had a cost recorded. Zero has to read as "unknown", never as "this was free".
  it("says so plainly when a run recorded no tokens", async () => {
    stub([makeRunSummary({ spend_usd: 0, tokens_total: 0 })]);
    renderList();

    expect(await screen.findByText("no tokens recorded")).toBeInTheDocument();
    expect(screen.queryByText(/\$0/)).not.toBeInTheDocument();
  });

  it("does not imply a call ceiling a run does not have", async () => {
    stub([makeRunSummary({ calls_used: 12, budget_calls: 0 })]);
    renderList();

    const cell = await screen.findByTitle(/maximum allowance/i);
    expect(cell).toHaveTextContent("12");
    expect(cell).not.toHaveTextContent("/");
  });

  it("explains the product before there is anything to list", async () => {
    stub([]);
    renderList();

    expect(await screen.findByText(/an oracle that shows its work/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /try a demo run/i })).toHaveAttribute(
      "href",
      "/new?runner=demo",
    );
  });

  it("distinguishes an over-filtered list from an empty one", async () => {
    stub([]);
    renderList("/?q=ammonia");

    expect(await screen.findByText(/no runs match those filters/i)).toBeInTheDocument();
    expect(screen.queryByText(/an oracle that shows its work/i)).not.toBeInTheDocument();
  });

  it("names the problem and offers a retry when the backend is down", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    vi.stubGlobal("fetch", fetchMock);
    renderList();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not reach the oracle backend/i,
    );
    expect(screen.queryByText("0")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /try again/i }));
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("searches the API rather than filtering what it already has", async () => {
    const user = userEvent.setup();
    const fetchMock = stub([makeRunSummary()]);
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");

    await user.type(screen.getByRole("searchbox", { name: /search runs/i }), "ammonia");

    await vi.waitFor(() => {
      expect(lastRunsUrl(fetchMock)).toContain("q=ammonia");
    });
  });

  it("keeps demo runs out until the toggle asks for them", async () => {
    const user = userEvent.setup();
    const fetchMock = stub([makeRunSummary()]);
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");
    expect(lastRunsUrl(fetchMock)).not.toContain("include_demo");

    await user.click(screen.getByRole("button", { name: /demo runs/i }));

    await vi.waitFor(() => {
      expect(lastRunsUrl(fetchMock)).toContain("include_demo=true");
    });
  });

  it("filters by status and sorts through the API's enum", async () => {
    const user = userEvent.setup();
    const fetchMock = stub([makeRunSummary()]);
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");

    await user.click(screen.getByRole("button", { name: "Running" }));
    await vi.waitFor(() => {
      expect(lastRunsUrl(fetchMock)).toContain("status=running");
    });

    await user.selectOptions(
      screen.getByRole("combobox", { name: /sort runs/i }),
      "title",
    );
    await vi.waitFor(() => {
      expect(lastRunsUrl(fetchMock)).toContain("sort=title");
    });
  });

  it("renames a run in place", async () => {
    const user = userEvent.setup();
    const renamed = makeRunSummary({ title: "Ammonia, ambient pressure" });
    const fetchMock = stub([makeRunSummary()], [{ match: "/runs/run-1", json: renamed }]);
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");

    await user.click(screen.getByRole("button", { name: /rename photocatalytic/i }));
    const input = screen.getByRole("textbox", { name: /run title/i });
    await user.clear(input);
    await user.type(input, "Ammonia, ambient pressure{Enter}");

    expect(
      await screen.findByRole("link", { name: "Ammonia, ambient pressure" }),
    ).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
    expect(patch?.[1]?.body).toContain("Ammonia, ambient pressure");
  });

  it("archives a run without leaving the list", async () => {
    const user = userEvent.setup();
    const fetchMock = stub(
      [makeRunSummary()],
      [{ match: "/runs/run-1", json: makeRunSummary({ archived: true }) }],
    );
    renderList();
    await screen.findByText("Photocatalytic routes to ammonia");

    await user.click(screen.getByRole("button", { name: /^archive photocatalytic/i }));

    await vi.waitFor(() => {
      const patch = fetchMock.mock.calls.find((call) => call[1]?.method === "PATCH");
      expect(patch?.[1]?.body).toContain('"archived":true');
    });
    expect(
      await screen.findByRole("button", { name: /unarchive photocatalytic/i }),
    ).toBeInTheDocument();
  });

  it("marks a run that changed since it was last opened", async () => {
    window.localStorage.setItem(
      "coscientist.seen.v1",
      JSON.stringify({ "run-1": "2026-08-01T09:00:00Z" }),
    );
    stub([makeRunSummary({ updated_at: "2026-08-01T10:30:00Z" })]);
    renderList();

    const row = await screen.findByRole("listitem");
    expect(within(row).getByText(/changed since you last looked/i)).toBeInTheDocument();
  });
});

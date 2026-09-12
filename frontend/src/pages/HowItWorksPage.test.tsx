import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ModelSettings } from "../api/types";
import { describeModelTier } from "../lib/status";
import {
  makeCapabilities,
  makeModelSettings,
  makeRunSummary,
  mockFetchRoutes,
  tablesOf,
} from "../test/fixtures";
import { makeAllRejectedGraph, makeGraph } from "../test/graphFixtures";
import { HowItWorksPage } from "./HowItWorksPage";
import { STATIONS } from "./circuit/stations";

/**
 * The explaining page. Its contract is narrow and entirely about honesty:
 * clicking a station says what that station does in the words this app uses
 * everywhere else, the model beside it is the live role table rather than a
 * paraphrase, the two stations that run no model say so, and the traced idea is
 * either a real record or is labelled as an example.
 */

const CAPABILITIES = makeCapabilities();
const TABLES = tablesOf(CAPABILITIES);
const TIERS = Object.keys(TABLES);
const SAVED_TIER = TIERS[0];
const OTHER_TIER = TIERS[1];
/**
 * The stored default, pinned to the fixture's own first tier. The casts are the
 * suite's established idiom: the generated types spell the real vendor's tiers,
 * and every fixture here is deliberately placeholder vocabulary instead.
 */
const SETTINGS = savedSettings();

function savedSettings(overrides: Record<string, unknown> = {}): ModelSettings {
  return makeModelSettings({
    provider: CAPABILITIES.models.default_provider as ModelSettings["provider"],
    tier: SAVED_TIER as ModelSettings["tier"],
    // The saved tier's own bucket, not a bare `overrides` block: the wire's
    // `overrides` is a read-only mirror of `tiers[tier]`, and the builder derives
    // it. A fixture that set the mirror alone would be a payload the backend
    // cannot serve.
    tiers: { [SAVED_TIER]: overrides } as ModelSettings["tiers"],
    table: TABLES[SAVED_TIER] as ModelSettings["table"],
  });
}

const COMPLETED_RUN = makeRunSummary({
  id: "run-9",
  lifecycle: "completed",
  title: "Photocatalytic routes to ammonia",
  has_overview: true,
});

function labelFor(model: string): string {
  return (
    CAPABILITIES.models.catalog.find((choice) => choice.id === model)?.label ?? model
  );
}

/** Order matters: `/graph` is a `/runs` URL too. */
function stub(
  options: {
    capabilities?: unknown;
    settings?: unknown;
    runs?: unknown;
    graph?: unknown;
  } = {},
) {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      { match: "/capabilities", json: options.capabilities ?? CAPABILITIES },
      { match: "/settings/models", json: options.settings ?? SETTINGS },
      { match: "/graph", json: options.graph ?? makeGraph() },
      { match: "/runs", json: options.runs ?? { items: [COMPLETED_RUN], total: 1 } },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <HowItWorksPage />
    </MemoryRouter>,
  );
}

function rail(): HTMLElement {
  return screen.getByRole("complementary", { name: /station inspector/i });
}

function station(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

describe("how it works — the circuit", () => {
  it("draws every station as a control, and describes the whole flow once", async () => {
    stub();
    renderPage();

    const board = screen.getByRole("img", { name: /flow diagram of one round/i });
    expect(board).toBeInTheDocument();
    for (const entry of STATIONS) {
      expect(station(entry.title)).toHaveAttribute("tabindex", "0");
    }
    await screen.findByText(/into the report/i);
  });

  it("fills the readout with what a station receives and emits", async () => {
    const user = userEvent.setup();
    stub();
    renderPage();

    expect(rail()).toHaveTextContent(/click a station in the circuit/i);

    await user.click(station("Generation"));

    const generation = STATIONS.find((entry) => entry.id === "generation")!;
    expect(rail()).toHaveTextContent(generation.receives);
    expect(rail()).toHaveTextContent(generation.emits);
    expect(within(rail()).getByText("×3 shards")).toBeInTheDocument();
    expect(station("Generation")).toHaveAttribute("aria-pressed", "true");
  });

  it("clears the selection on Escape", async () => {
    const user = userEvent.setup();
    stub();
    renderPage();

    await user.click(station("Tournament"));
    expect(rail()).toHaveTextContent(/head-to-head/i);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(rail()).toHaveTextContent(/click a station in the circuit/i);
    expect(station("Tournament")).toHaveAttribute("aria-pressed", "false");
  });

  it("closes a station when it is chosen twice", async () => {
    const user = userEvent.setup();
    stub();
    renderPage();

    await user.click(station("Evolution"));
    await user.click(station("Evolution"));

    expect(rail()).toHaveTextContent(/click a station in the circuit/i);
  });
});

describe("how it works — the models are live", () => {
  it("names the model and effort the engine would run this station on", async () => {
    const user = userEvent.setup();
    stub();
    renderPage();

    const row = TABLES[SAVED_TIER].find((entry) => entry.role === "generation")!;
    await user.click(station("Generation"));

    await waitFor(() => {
      expect(rail()).toHaveTextContent(labelFor(row.model));
    });
    // The engine's own sentence about the setting, not a paraphrase of it.
    expect(rail()).toHaveTextContent(row.note);
  });

  it("re-describes a station when another tier is previewed", async () => {
    const user = userEvent.setup();
    stub();
    renderPage();

    const saved = TABLES[SAVED_TIER].find((entry) => entry.role === "ranking")!;
    const other = TABLES[OTHER_TIER].find((entry) => entry.role === "ranking")!;
    expect(other.model).not.toBe(saved.model);

    await user.click(station("Tournament"));
    await waitFor(() => expect(rail()).toHaveTextContent(labelFor(saved.model)));

    await user.click(
      await screen.findByRole("button", { name: describeModelTier(OTHER_TIER).label }),
    );

    expect(rail()).toHaveTextContent(labelFor(other.model));
    expect(rail()).not.toHaveTextContent(labelFor(saved.model));
    // A preview is not a change: the page says so rather than letting a reader
    // believe they have just re-configured their runs.
    expect(
      screen.getByText(
        new RegExp(`saved default is ${describeModelTier(SAVED_TIER).label}`),
      ),
    ).toBeInTheDocument();
  });

  it("marks a station your settings have moved off its tier", async () => {
    const user = userEvent.setup();
    const moved = CAPABILITIES.models.catalog[1].id;
    stub({
      settings: savedSettings({ evolution: { model: moved, effort: "max" } }),
    });
    renderPage();

    await user.click(station("Evolution"));

    await waitFor(() => expect(rail()).toHaveTextContent(labelFor(moved)));
    expect(within(rail()).getByText(/set by you/i)).toBeInTheDocument();
  });

  it("says plainly that nothing runs a model at the two stations that do not", async () => {
    const user = userEvent.setup();
    stub();
    renderPage();

    await user.click(station("Your question"));
    expect(within(rail()).queryByText("Model · effort")).toBeNull();
    expect(rail()).toHaveTextContent(/no model runs here/i);

    await user.click(station("Workshop"));
    expect(within(rail()).queryByText("Model · effort")).toBeNull();
    expect(rail()).toHaveTextContent(/before a run exists/i);
  });

  it("says the role table failed rather than drawing blanks on it", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        mockFetchRoutes([
          {
            match: "/capabilities",
            json: { code: "boom", message: "Backend is down" },
            status: 503,
          },
          { match: "/settings/models", json: SETTINGS },
          { match: "/graph", json: makeGraph() },
          { match: "/runs", json: { items: [COMPLETED_RUN], total: 1 } },
        ]),
      ),
    );
    renderPage();

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be loaded/i);
    await user.click(station("Generation"));
    expect(rail()).not.toHaveTextContent(/·\s*Medium/);
  });
});

describe("how it works — the life of one idea", () => {
  it("traces a real idea out of the most recent completed run", async () => {
    stub();
    renderPage();

    expect(await screen.findByText(/led the final standings/i)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /h001, from the last completed run/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/3 won/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /genealogy/i })).toHaveAttribute(
      "href",
      "/runs/run-9?tab=hypotheses",
    );
    expect(screen.queryByText(/^Example —/)).toBeNull();
  });

  it("falls back to a labelled example when no run has ever finished", async () => {
    stub({ runs: { items: [], total: 0 } });
    renderPage();

    expect(await screen.findByText(/^Example —/)).toBeInTheDocument();
    expect(screen.getByText(/assay office for the slop flood/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /genealogy/i })).toBeNull();
  });

  it("falls back to the example when the last run left no survivor", async () => {
    // A run that rejected all eighteen of its ideas has a graph and no
    // biography. Telling one anyway would mean promoting a rejected idea.
    stub({ graph: makeAllRejectedGraph() });
    renderPage();

    expect(await screen.findByText(/^Example —/)).toBeInTheDocument();
  });
});

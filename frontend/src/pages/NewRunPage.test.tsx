import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MODEL_TIERS, PROVIDERS } from "../api/types";
import type {
  Capabilities,
  ModelSettings,
  RecommendedSettings,
  RunConfig,
  Workshop,
  WorkshopOption,
} from "../api/types";
import { Toaster } from "../components/Toaster";
import {
  DEFAULT_MODEL_TIER,
  estimateCalls,
  getPreset,
  presetConfig,
  suggestedBudgetCalls,
} from "../lib/estimates";
import { runRoles } from "../lib/models";
import { describeRole } from "../lib/status";
import { primeIdentity } from "../store/identity";
import {
  makeCapabilities,
  makeRunConfig,
  makeRunDetail,
  makeRunSummary,
  tablesOf,
} from "../test/fixtures";
import { primeModelSettings, resetModelSettingsStore } from "../store/settings";
import { NewRunPage } from "./NewRunPage";
import { REASSURANCE } from "./wizard/ConfirmStep";
import { draftKey } from "./wizard/state";

/** The stored system default is a module-level store; a test that leaves one
 *  behind decides what the next test's wizard starts from. */
beforeEach(() => {
  resetModelSettingsStore();
});

/* --- Fixtures ------------------------------------------------------------- */

function makeOption(overrides: Partial<WorkshopOption> = {}): WorkshopOption {
  return {
    id: "opt-a",
    ordinal: 0,
    prompt: "Investigate photocatalytic ammonia synthesis at ambient pressure.",
    strategy: "Mechanism first",
    optimizes_for: "Depth on the catalytic step",
    excludes: "Economics and scale-up",
    rationale: "The bottleneck is the nitrogen triple bond, not the plant.",
    recommended_settings: {
      rounds: 4,
      budget_calls: 120,
      matches_per_round: 6,
      grounding_depth: "deep",
    },
    chosen: false,
    rejected: false,
    note: null,
    ...overrides,
  };
}

function makeWorkshop(overrides: Partial<Workshop> = {}): Workshop {
  return {
    id: "ws-1",
    question: "How could ammonia be made photocatalytically at ambient pressure?",
    state: "options_ready",
    harness: "claude",
    options: [
      makeOption(),
      makeOption({
        id: "opt-b",
        ordinal: 1,
        strategy: "Survey the field",
        optimizes_for: "Breadth across catalyst families",
        excludes: "Deep mechanism",
        prompt: "Survey candidate photocatalysts for nitrogen fixation.",
        recommended_settings: {
          rounds: 2,
          budget_calls: 40,
          matches_per_round: 3,
          grounding_depth: "standard",
        },
      }),
    ],
    error: null,
    created_at: "2026-08-01T10:00:00Z",
    ...overrides,
  };
}

/* --- Harness -------------------------------------------------------------- */

type Handler = (url: string, init: RequestInit | undefined) => unknown;

/**
 * Routes by method and URL pattern — the wizard POSTs and GETs the same paths,
 * so matching on the URL alone would answer `POST /workshops` with a workshop
 * that already has its options.
 */
function stubApi(handlers: { method: string; pattern: RegExp; respond: Handler }[]) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body });

    const handler = handlers.find(
      (candidate) => candidate.method === method && candidate.pattern.test(url),
    );
    if (!handler) {
      return new Response(JSON.stringify({ code: "not_found", message: url }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    const result = handler.respond(url, init);
    if (result instanceof Response) return result;
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function ok(payload: unknown): Handler {
  return () => payload;
}

/** The launch request, typed to the config the assertions read off it. */
function createCall(
  calls: { method: string; url: string; body: unknown }[],
): { body: { config: RunConfig } } | undefined {
  return calls.find((call) => call.method === "POST" && /\/runs$/.test(call.url)) as
    { body: { config: RunConfig } } | undefined;
}

const here = { url: "" };

function LocationProbe() {
  const location = useLocation();
  useEffect(() => {
    here.url = `${location.pathname}${location.search}`;
  }, [location]);
  return null;
}

/** Lets a test change the URL without remounting the wizard, which is the only
 *  way to reach the in-place clone path: navigating from `/new` to
 *  `/new?from=<id>` is the same route, so `NewRunPage` keeps its state and
 *  `initialDraft` — which blanks the draft on a fresh mount — never runs again. */
const nav = { go: (_url: string) => {} };

function NavProbe() {
  const navigate = useNavigate();
  useEffect(() => {
    nav.go = (url: string) => navigate(url);
  }, [navigate]);
  return null;
}

function renderWizard(url = "/new", username = "local-owner") {
  primeIdentity({
    username,
    email: null,
    display_name: username,
    groups: [],
    is_admin: false,
    source: "gateway",
  });
  return render(
    <MemoryRouter initialEntries={[url]}>
      <LocationProbe />
      <NavProbe />
      <Routes>
        <Route path="/new" element={<NewRunPage />} />
        <Route path="/runs/:id" element={<div>RUN WORKSPACE</div>} />
      </Routes>
      {/* The shell renders this; tests need it to see what the wizard reports. */}
      <Toaster />
    </MemoryRouter>,
  );
}

/**
 * The engine's role table, republished under the tier the wizard's config
 * actually carries. The model ids stay the fixture's placeholders: what these
 * tests assert is that the Confirm step prints *the payload*, never which model
 * is in it.
 */
const BASE_CAPABILITIES = makeCapabilities();
const WIZARD_CAPABILITIES: Capabilities = {
  ...BASE_CAPABILITIES,
  models: {
    ...BASE_CAPABILITIES.models,
    default_tier: DEFAULT_MODEL_TIER,
    tiers: {
      [BASE_CAPABILITIES.models.default_provider]: {
        [DEFAULT_MODEL_TIER]:
          tablesOf(BASE_CAPABILITIES)[BASE_CAPABILITIES.models.default_tier],
      },
    },
  },
};
const CAPABILITY_ROUTE = {
  method: "GET",
  pattern: /\/capabilities$/,
  respond: ok(WIZARD_CAPABILITIES),
};

/* --- Two vendors, for the clone tests -------------------------------------
   A run records which vendor's table its tier was filled from, and cloning it
   has to reproduce that: the wizard's own starting point is the *other* one, so
   losing the field is invisible on screen and decisive at launch — the backend
   picks the CLI lane off it.

   The names are taken by position out of the generated vocabularies rather than
   typed here, so this file still names no vendor and no tier. All that matters
   is that the source run's pair is not the one a blank draft starts from.
   ------------------------------------------------------------------------- */
const HOME_PROVIDER = PROVIDERS[0];
const OTHER_PROVIDER = PROVIDERS[PROVIDERS.length - 1];
const OTHER_TIER = MODEL_TIERS[MODEL_TIERS.length - 1];

const CLONE_TABLE = tablesOf(BASE_CAPABILITIES)[BASE_CAPABILITIES.models.default_tier];
const CLONE_CAPABILITIES: Capabilities = {
  ...BASE_CAPABILITIES,
  models: {
    ...BASE_CAPABILITIES.models,
    default_provider: HOME_PROVIDER,
    default_tier: DEFAULT_MODEL_TIER,
    providers: [HOME_PROVIDER, OTHER_PROVIDER].map((id, index) => ({
      ...BASE_CAPABILITIES.models.providers[0],
      id,
      label: `Vendor ${index}`,
    })),
    tiers: Object.fromEntries(
      [HOME_PROVIDER, OTHER_PROVIDER].map((id) => [
        id,
        { [DEFAULT_MODEL_TIER]: CLONE_TABLE, [OTHER_TIER]: CLONE_TABLE },
      ]),
    ),
  },
};
const CLONE_CAPABILITY_ROUTE = {
  method: "GET",
  pattern: /\/capabilities$/,
  respond: ok(CLONE_CAPABILITIES),
};

/** The stored system default, disagreeing with the source run about everything. */
const SYSTEM_DEFAULT = {
  provider: HOME_PROVIDER,
  tier: DEFAULT_MODEL_TIER,
  overrides: { generation: { effort: "low" } },
  table: CLONE_TABLE,
  source: "stored",
  updated_at: "2026-08-11T09:00:00Z",
} as unknown as ModelSettings;

const WORKSHOP_ROUTES = [
  CAPABILITY_ROUTE,
  { method: "POST", pattern: /\/workshops$/, respond: ok(makeWorkshop()) },
  { method: "GET", pattern: /\/workshops\/ws-1$/, respond: ok(makeWorkshop()) },
  {
    method: "POST",
    pattern: /\/workshops\/ws-1\/choose$/,
    respond: ok({ prompt: "chosen" }),
  },
];

/** Walks from an empty wizard to the settings step with direction A chosen. */
async function walkToSettings(user: ReturnType<typeof userEvent.setup>) {
  await user.type(
    screen.getByLabelText(/what should oracle investigate/i),
    "How could ammonia be made photocatalytically?",
  );
  await user.click(screen.getByRole("button", { name: /draft two directions/i }));
  await screen.findByText("Mechanism first");
  await user.click(screen.getByRole("button", { name: /mechanism first/i }));
  await user.click(screen.getByRole("button", { name: /continue with this direction/i }));
  await screen.findByLabelText(/this is what every generation call/i);
  await user.click(screen.getByRole("button", { name: /continue to settings/i }));
  await screen.findByText(/how much work/i);
}

/* --- Tests ---------------------------------------------------------------- */

describe("launch wizard — steps and persistence", () => {
  it("keeps drafts private when two users share a browser", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    const alice = renderWizard("/new", "alice");
    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "Alice private research question",
    );
    await waitFor(() => {
      expect(window.localStorage.getItem(draftKey("alice"))).toContain(
        "Alice private research question",
      );
    });
    alice.unmount();

    const bob = renderWizard("/new", "bob");
    expect(screen.getByLabelText(/what should oracle investigate/i)).toHaveValue("");
    expect(window.localStorage.getItem(draftKey("bob"))).not.toContain(
      "Alice private research question",
    );
    bob.unmount();

    renderWizard("/new", "alice");
    expect(screen.getByLabelText(/what should oracle investigate/i)).toHaveValue(
      "Alice private research question",
    );
  });

  it("does not assign a legacy unscoped draft to the first gateway user", () => {
    window.localStorage.setItem(
      "coscientist.wizard.v1",
      JSON.stringify({ version: 1, question: "Earlier local owner's question" }),
    );
    stubApi(WORKSHOP_ROUTES);
    renderWizard("/new", "new-gateway-user");

    expect(screen.getByLabelText(/what should oracle investigate/i)).toHaveValue("");
    expect(window.localStorage.getItem("coscientist.wizard.v1")).toContain(
      "Earlier local owner's question",
    );
  });

  it("puts the step and the workshop in the URL, and survives a reload", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    const first = renderWizard();

    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "How could ammonia be made photocatalytically?",
    );
    await user.click(screen.getByRole("button", { name: /draft two directions/i }));
    await screen.findByText("Mechanism first");

    expect(here.url).toContain("step=options");
    expect(here.url).toContain("workshop=ws-1");

    // Reload: same URL, fresh component tree, nothing in memory.
    const reloadUrl = here.url;
    first.unmount();
    renderWizard(reloadUrl);

    expect(await screen.findByText("Mechanism first")).toBeInTheDocument();

    // ...and the question itself came back from the draft, not the URL.
    await user.click(screen.getByRole("button", { name: /^back$/i }));
    expect(await screen.findByLabelText(/what should oracle investigate/i)).toHaveValue(
      "How could ammonia be made photocatalytically?",
    );
  });

  it("refuses to render a step the work has not reached yet", async () => {
    stubApi(WORKSHOP_ROUTES);
    renderWizard("/new?step=confirm");

    // No prompt exists, so Confirm is not reachable and the wizard falls back.
    expect(
      await screen.findByLabelText(/what should oracle investigate/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^launch run$/i })).toBeNull();
  });

  it("asks for a longer question instead of silently disabling the button", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();

    await user.type(screen.getByLabelText(/what should oracle investigate/i), "ammonia");
    expect(screen.getByText(/3 more characters/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /draft two directions/i })).toBeDisabled();
  });
});

describe("launch wizard — choosing a direction", () => {
  it("marks the chosen option selected, and only that one", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();
    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "How could ammonia be made photocatalytically?",
    );
    await user.click(screen.getByRole("button", { name: /draft two directions/i }));
    await screen.findByText("Mechanism first");

    const a = screen.getByRole("button", { name: /mechanism first/i });
    const b = screen.getByRole("button", { name: /survey the field/i });
    expect(a).toHaveAttribute("aria-pressed", "false");

    await user.click(a);

    expect(a).toHaveAttribute("aria-pressed", "true");
    expect(a).toHaveAttribute("data-selected", "true");
    expect(within(a).getByText(/✓ Selected/)).toBeInTheDocument();
    expect(b).toHaveAttribute("aria-pressed", "false");
    expect(b).toHaveAttribute("data-selected", "false");
    expect(within(b).getByText(/^Select$/)).toBeInTheDocument();
  });

  it("merges both directions without needing one selected", async () => {
    const calls = stubApi([
      ...WORKSHOP_ROUTES,
      {
        method: "POST",
        pattern: /\/workshops\/ws-1\/refine$/,
        respond: ok(
          makeWorkshop({
            options: [
              makeOption({ id: "opt-a", rejected: true }),
              makeOption({ id: "opt-b", rejected: true, strategy: "Survey the field" }),
              makeOption({ id: "opt-c", strategy: "Merged direction" }),
              makeOption({ id: "opt-d", strategy: "Merged alternative" }),
            ],
          }),
        ),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "How could ammonia be made photocatalytically?",
    );
    await user.click(screen.getByRole("button", { name: /draft two directions/i }));
    await screen.findByText("Mechanism first");

    await user.click(screen.getByRole("button", { name: /merge both directions/i }));
    await screen.findByText("Merged direction");

    const refine = calls.find((call) => call.url.includes("/refine"));
    expect(refine?.body).toMatchObject({ base: "merge" });
  });

  it("sends a note with another pass, and keeps the rejected pair in history", async () => {
    const calls = stubApi([
      ...WORKSHOP_ROUTES,
      {
        method: "POST",
        pattern: /\/workshops\/ws-1\/refine$/,
        respond: ok(
          makeWorkshop({
            options: [
              makeOption({
                id: "opt-a",
                rejected: true,
                note: "Too narrow — widen it to other catalysts.",
              }),
              makeOption({ id: "opt-b", rejected: true, strategy: "Survey the field" }),
              makeOption({ id: "opt-c", strategy: "Second pass" }),
              makeOption({ id: "opt-d", strategy: "Second alternative" }),
            ],
          }),
        ),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "How could ammonia be made photocatalytically?",
    );
    await user.click(screen.getByRole("button", { name: /draft two directions/i }));
    await screen.findByText("Mechanism first");

    await user.click(screen.getByRole("button", { name: /mechanism first/i }));
    await user.type(
      screen.getByLabelText(/what should change/i),
      "Too narrow — widen it to other catalysts.",
    );
    await user.click(screen.getByRole("button", { name: /another pass on/i }));
    await screen.findByText("Second pass");

    const refine = calls.find((call) => call.url.includes("/refine"));
    expect(refine?.body).toMatchObject({
      base: "opt-a",
      note: "Too narrow — widen it to other catalysts.",
    });

    // The directions that were refined away are still recoverable.
    const summary = screen.getByText(/earlier directions/i);
    await user.click(summary);
    const history = summary.closest("details") as HTMLElement;
    expect(within(history).getByText(/Your note: Too narrow/)).toBeInTheDocument();
    expect(within(history).getByText("Survey the field")).toBeInTheDocument();
  });

  it("shows the workshop working, with an elapsed timer, rather than a bare spinner", async () => {
    stubApi([
      {
        method: "POST",
        pattern: /\/workshops$/,
        respond: ok(makeWorkshop({ state: "refining", options: [] })),
      },
      {
        method: "GET",
        pattern: /\/workshops\/ws-1$/,
        respond: ok(makeWorkshop({ state: "refining", options: [] })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "How could ammonia be made photocatalytically?",
    );
    await user.click(screen.getByRole("button", { name: /draft two directions/i }));

    expect(await screen.findByText(/drafting two directions/i)).toBeInTheDocument();
    expect(screen.getByText(/^0s$/)).toBeInTheDocument();
  });
});

describe("launch wizard — settings", () => {
  it("prefills from the chosen direction's recommended settings", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();
    await walkToSettings(user);

    const recommended: RecommendedSettings = makeOption().recommended_settings;
    expect(
      screen.getByText(/these settings came from the direction you chose/i),
    ).toBeInTheDocument();

    await user.click(screen.getByText(/advanced settings/i));
    expect(screen.getByLabelText(/^rounds$/i)).toHaveValue(recommended.rounds);
    expect(screen.getByLabelText(/evidence checks per checkpoint/i)).toHaveValue(
      recommended.matches_per_round,
    );
    expect(screen.getByLabelText(/call budget/i)).toHaveValue(recommended.budget_calls);
    expect(screen.getByLabelText(/^grounding$/i)).toHaveValue(
      recommended.grounding_depth,
    );
  });

  it("sets preset budgets from the estimate formula, not from a magic number", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();
    await walkToSettings(user);

    const deep = getPreset("deep");
    await user.click(screen.getByRole("button", { name: /deep/i }));

    await user.click(screen.getByText(/advanced settings/i));
    expect(screen.getByLabelText(/call budget/i)).toHaveValue(
      suggestedBudgetCalls(deep.shape),
    );
    expect(screen.getByLabelText(/^rounds$/i)).toHaveValue(deep.shape.rounds);
    // The same figure on the preset card and in the estimate strip — they are
    // one function, so they cannot disagree.
    expect(
      screen.getAllByText(new RegExp(`~${estimateCalls(deep.shape)}\\b`)),
    ).toHaveLength(2);
    expect(suggestedBudgetCalls(deep.shape)).toBe(presetConfig(deep).budget_calls);
  });

  it("lets a numeric field be empty while it is being edited", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();
    await walkToSettings(user);
    await user.click(screen.getByText(/advanced settings/i));

    const rounds = screen.getByLabelText(/^rounds$/i);
    await user.clear(rounds);
    expect(rounds).toHaveValue(null); // empty, and no NaN anywhere on screen
    expect(screen.queryByText(/NaN/)).toBeNull();

    await user.type(rounds, "2");
    expect(rounds).toHaveValue(2);
  });

  it("warns when the call budget cannot cover the settings, and offers the fix", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();
    await walkToSettings(user);
    await user.click(screen.getByText(/advanced settings/i));

    const budget = screen.getByLabelText(/call budget/i);
    await user.clear(budget);
    await user.type(budget, "5");

    expect(
      await screen.findByText(/the run would stop before the last round/i),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /raise it to/i }));
    expect(screen.queryByText(/the run would stop before the last round/i)).toBeNull();
  });
});

describe("launch wizard — confirm and launch", () => {
  async function atConfirm(user: ReturnType<typeof userEvent.setup>) {
    await walkToSettings(user);
    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
  }

  it("states the harness, the models, the estimates and the promise", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard();
    await atConfirm(user);

    // The sentence, verbatim — it is the reason someone is willing to press Launch.
    expect(screen.getByText(REASSURANCE)).toBeInTheDocument();
    expect(
      screen.getByText(
        "You can pause or stop at any time — stopping still writes a report from what exists.",
      ),
    ).toBeInTheDocument();

    // The model table is the capabilities payload for the tier this config
    // carries — the same rows the diagram above it draws — labelled through the
    // catalog. Which models those are is the engine's business and changes; that
    // every step it lists names one, in the engine's own words for it, is this
    // screen's promise.
    const rows = runRoles(tablesOf(WIZARD_CAPABILITIES)[DEFAULT_MODEL_TIER]);
    const table = await screen.findByRole("table");
    for (const row of rows) {
      expect(within(table).getByText(describeRole(row.role))).toBeInTheDocument();
      const label = WIZARD_CAPABILITIES.models.catalog.find(
        (choice) => choice.id === row.model,
      )!.label;
      expect(within(table).getAllByText(label).length).toBeGreaterThan(0);
    }
    // Raw ids never reach the screen; the catalog's name for them does.
    for (const choice of WIZARD_CAPABILITIES.models.catalog) {
      expect(within(table).queryByText(choice.id)).toBeNull();
    }
    // The cartographer only runs when diversity injection is on; it is off here.
    expect(within(table).queryByText(describeRole("cartographer"))).toBeNull();
    // The pre-run role is not part of a run at all.
    expect(within(table).queryByText(describeRole("workshop"))).toBeNull();

    // The escalations come from the payload rather than a hand-written sentence.
    for (const note of WIZARD_CAPABILITIES.models.notes) {
      expect(screen.getByText(note)).toBeInTheDocument();
    }

    // Calls and wall clock: the two things that actually run out. Cost is a
    // footnote, never a ceiling, and never the word "stops".
    expect(screen.getAllByText(/model calls/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/wall clock/i)).toBeInTheDocument();
    expect(screen.getByText(/estimates, not quotes/i)).toHaveTextContent(
      /stops itself at \d+ model calls\.$/,
    );
    expect(screen.getByText(/API-equivalent/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/stops at \$/);
    expect(document.body.textContent).not.toContain("$null");
    expect(screen.getByText(/Claude/)).toBeInTheDocument();
  });

  it("shows a demo run no model anywhere on the last screen", async () => {
    stubApi(WORKSHOP_ROUTES);
    const user = userEvent.setup();
    renderWizard("/new?runner=demo");
    await atConfirm(user);
    await screen.findByRole("table");

    // A demo run makes no model calls at all. The table, the diagram and the
    // estimate strip all have to agree about that.
    for (const choice of WIZARD_CAPABILITIES.models.catalog) {
      expect(document.body).not.toHaveTextContent(choice.label);
      expect(document.body).not.toHaveTextContent(choice.id);
    }
    expect(screen.getByText(/costs nothing at all/i)).toBeInTheDocument();
    expect(screen.queryByText(/estimates, not quotes/i)).toBeNull();
  });

  it("launches and lands on the run it just started", async () => {
    const calls = stubApi([
      ...WORKSHOP_ROUTES,
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-42" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await atConfirm(user);

    await user.click(screen.getByRole("button", { name: /^launch run$/i }));

    expect(await screen.findByText("RUN WORKSPACE")).toBeInTheDocument();
    expect(here.url).toBe("/runs/run-42");

    const create = calls.find(
      (call) => call.method === "POST" && /\/runs$/.test(call.url),
    );
    expect(create?.body).toMatchObject({
      harness: "claude",
      prompt: makeOption().prompt,
      config: { runner: "claude", rounds: 4 },
    });

    // The draft is gone: coming back to /new starts a new run, not the old one.
    expect(window.localStorage.getItem(draftKey("local-owner"))).toBeNull();
  });

  it("explains a busy lane and links to the run holding it", async () => {
    stubApi([
      ...WORKSHOP_ROUTES,
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: () =>
          new Response(
            JSON.stringify({
              code: "lane_busy",
              message: "A Claude run is already in flight.",
              details: { harness: "claude", conflicting_run_id: "run-7" },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await atConfirm(user);

    await user.click(screen.getByRole("button", { name: /^launch run$/i }));

    expect(
      await screen.findByText(/something is already running here/i),
    ).toBeInTheDocument();
    expect(screen.getByText("A Claude run is already in flight.")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /open the run that is holding the lane/i }),
    ).toHaveAttribute("href", "/runs/run-7");
    // ...and the way out is offered, because a demo has its own lane.
    expect(
      screen.getAllByRole("button", { name: /run as a demo instead/i }).length,
    ).toBeGreaterThan(0);
  });

  it("runs as a demo when asked, and promises no model calls", async () => {
    const calls = stubApi([
      ...WORKSHOP_ROUTES,
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-demo" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard("/new?runner=demo");
    await atConfirm(user);

    expect(screen.getByText(/no model calls at all/i)).toBeInTheDocument();
    expect(screen.getByText(/costs nothing at all/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^launch demo run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = calls.find(
      (call) => call.method === "POST" && /\/runs$/.test(call.url),
    );
    expect(create?.body).toMatchObject({
      harness: "demo",
      config: { runner: "demo" },
    });
    // The workshop ran on the demo harness too, so no real call was made anywhere.
    const workshop = calls.find(
      (call) => call.method === "POST" && /\/workshops$/.test(call.url),
    );
    expect(workshop?.body).toMatchObject({ harness: "demo" });
  });

  it("clones an earlier run: settings prefilled, prompt inherited server-side", async () => {
    const calls = stubApi([
      {
        method: "GET",
        pattern: /\/runs\/run-old\/detail$/,
        respond: ok(
          makeRunDetail({
            run: makeRunSummary({
              id: "run-old",
              title: "Ammonia, first attempt",
              question: "How could ammonia be made photocatalytically?",
            }),
          }),
        ),
      },
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-new" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard("/new?from=run-old");

    expect(
      await screen.findByText(/running an earlier question again/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/title for the runs list/i)).toHaveValue(
      "Ammonia, first attempt (again)",
    );

    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
    expect(
      screen.getByText(/inherited from the run this was started from/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^launch run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = calls.find(
      (call) => call.method === "POST" && /\/runs$/.test(call.url),
    );
    expect(create?.body).toMatchObject({
      from_run: "run-old",
      prompt: "",
      question: "How could ammonia be made photocatalytically?",
    });
  });

  it("clones without carrying the documents attached before the clone", async () => {
    // Reachable only in place: navigating from `/new` to `/new?from=<id>` is the
    // same route, so the wizard keeps its state and `initialDraft` — which blanks
    // a draft on a fresh mount — never runs. The clone reset then rebuilt every
    // field except `contextDocs`, and `launcher._inherit` prefers a caller-supplied
    // document list over the source run's whenever it is non-empty — so the clone
    // reached generation with one scientist's documents attached to another
    // scientist's question, and without the ones it was supposed to reuse.
    const calls = stubApi([
      {
        method: "GET",
        pattern: /\/runs\/run-old\/detail$/,
        respond: ok(
          makeRunDetail({
            run: makeRunSummary({ id: "run-old", question: "The real question?" }),
          }),
        ),
      },
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-new" }) })),
      },
    ]);
    const user = userEvent.setup();
    const { container } = renderWizard("/new");

    await user.type(
      screen.getByLabelText(/what should oracle investigate/i),
      "An abandoned question about something else entirely",
    );
    const picker = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    await user.upload(
      picker,
      new File(["Not this run's material."], "someone-elses.md", {
        type: "text/markdown",
      }),
    );
    await screen.findByText("someone-elses.md");

    nav.go("/new?from=run-old");

    await screen.findByText(/running an earlier question again/i);
    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
    await user.click(screen.getByRole("button", { name: /^launch run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = calls.find(
      (call) => call.method === "POST" && /\/runs$/.test(call.url),
    );
    expect(create?.body).toMatchObject({ from_run: "run-old" });
    expect((create?.body as { context_docs?: unknown }).context_docs).toBeUndefined();
  });

  it("clones a run launched on the other vendor onto that vendor", async () => {
    // `normaliseConfig` keeps only the keys it names, and `provider` was not one
    // of them: a clone started from `blankDraft()`, whose provider is `null`, and
    // the wizard posts its whole config — so the field arrived at the backend as
    // an explicit "no opinion" and the clone silently resolved against whatever
    // the top bar said that day, in the other CLI's lane. "Run again" on a run
    // launched at one vendor produced a run at the other, with nothing on either
    // screen to explain the difference.
    const calls = stubApi([
      CLONE_CAPABILITY_ROUTE,
      {
        method: "GET",
        pattern: /\/runs\/run-other\/detail$/,
        respond: ok(
          makeRunDetail({
            run: makeRunSummary({ id: "run-other", title: "On the other vendor" }),
            config: makeRunConfig({
              provider: OTHER_PROVIDER,
              model_tier: OTHER_TIER,
              model_overrides: {},
              rounds: 7,
            }),
          }),
        ),
      },
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-new" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard("/new?from=run-other");

    await screen.findByText(/running an earlier question again/i);
    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
    await user.click(screen.getByRole("button", { name: /^launch run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = createCall(calls);
    expect(create?.body.config.provider).toBe(OTHER_PROVIDER);
    expect(create?.body.config.model_tier).toBe(OTHER_TIER);
    expect(create?.body.config.rounds).toBe(7);
  });

  it("keeps a clone's settings when the system default arrives after them", async () => {
    // Two independent requests fill the same three fields — `GET /runs/{id}/detail`
    // and `GET /settings/models` — and only one of them is about *this* run. The
    // stored default is seeded here after the clone has landed, which is the order
    // a cold load on `/new?from=` produces whenever the run detail answers first.
    //
    // The source run is one written before the provider field existed, so the
    // clone has no provider of its own to state: that is exactly the draft
    // `inheritDefault` used to read as untouched and refill from the top bar.
    const calls = stubApi([
      CLONE_CAPABILITY_ROUTE,
      {
        method: "GET",
        pattern: /\/runs\/run-early\/detail$/,
        respond: ok(
          makeRunDetail({
            run: makeRunSummary({ id: "run-early", title: "Before the field existed" }),
            config: makeRunConfig({
              provider: null,
              model_tier: OTHER_TIER,
              model_overrides: {},
            }),
          }),
        ),
      },
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-new" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard("/new?from=run-early");

    await screen.findByText(/running an earlier question again/i);
    act(() => {
      primeModelSettings(SYSTEM_DEFAULT);
    });

    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
    await user.click(screen.getByRole("button", { name: /^launch run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = createCall(calls);
    expect(create?.body.config.model_tier).toBe(OTHER_TIER);
    expect(create?.body.config.model_overrides).toEqual({});
  });

  it("still starts a run that is not a clone from the system default", async () => {
    // The guard above must not cost the feature it sits next to: a fresh draft
    // still inherits what the top bar says, whenever that arrives.
    const calls = stubApi([
      CLONE_CAPABILITY_ROUTE,
      ...WORKSHOP_ROUTES,
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-new" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await walkToSettings(user);

    act(() => {
      primeModelSettings(SYSTEM_DEFAULT);
    });
    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
    await user.click(screen.getByRole("button", { name: /^launch run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = createCall(calls);
    expect(create?.body.config.provider).toBe(SYSTEM_DEFAULT.provider);
    expect(create?.body.config.model_tier).toBe(SYSTEM_DEFAULT.tier);
    expect(create?.body.config.model_overrides).toEqual(SYSTEM_DEFAULT.overrides);
  });

  it("clones a run whose stored tier the engine no longer offers", async () => {
    // The four pre-floor runs store a tier that is not in `MODEL_TIERS`. Copying
    // it through took the wizard down before a request was ever made: the cost
    // estimate destructured a table entry that did not exist and the error
    // boundary replaced the page. The backend keeps those runs launchable, so
    // the client must not be the thing that breaks.
    const calls = stubApi([
      CAPABILITY_ROUTE,
      {
        method: "GET",
        pattern: /\/runs\/run-legacy\/detail$/,
        respond: ok(
          makeRunDetail({
            run: makeRunSummary({ id: "run-legacy", title: "Before the floor" }),
            config: {
              ...makeRunDetail().config,
              model_tier: "balanced" as never,
              budget_usd: null,
            },
          }),
        ),
      },
      {
        method: "POST",
        pattern: /\/runs$/,
        respond: ok(makeRunDetail({ run: makeRunSummary({ id: "run-new" }) })),
      },
    ]);
    const user = userEvent.setup();
    renderWizard("/new?from=run-legacy");

    expect(await screen.findByLabelText(/title for the runs list/i)).toHaveValue(
      "Before the floor (again)",
    );
    // The page is alive rather than replaced by the error boundary...
    expect(screen.queryByText(/this screen stopped working/i)).toBeNull();

    await user.click(screen.getByRole("button", { name: /review and launch/i }));
    await screen.findByText(/what is about to run/i);
    // ...and the retired tier is dropped rather than carried into the launch.
    expect(document.body.textContent).not.toContain("$null");

    await user.click(screen.getByRole("button", { name: /^launch run$/i }));
    await screen.findByText("RUN WORKSPACE");

    const create = calls.find(
      (call) => call.method === "POST" && /\/runs$/.test(call.url),
    ) as { body: { config: { model_tier: string } } } | undefined;
    expect(create?.body.config.model_tier).toBe(DEFAULT_MODEL_TIER);
  });

  it("keeps going when the workshop cannot record the choice", async () => {
    stubApi([
      { method: "POST", pattern: /\/workshops$/, respond: ok(makeWorkshop()) },
      { method: "GET", pattern: /\/workshops\/ws-1$/, respond: ok(makeWorkshop()) },
      {
        method: "POST",
        pattern: /\/workshops\/ws-1\/choose$/,
        respond: () =>
          new Response(
            JSON.stringify({ code: "server_error", message: "workshop unavailable" }),
            { status: 500, headers: { "Content-Type": "application/json" } },
          ),
      },
    ]);
    const user = userEvent.setup();
    renderWizard();
    await walkToSettings(user);

    await waitFor(() => {
      expect(screen.getByText(/did not record your choice/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/how much work/i)).toBeInTheDocument();
  });
});

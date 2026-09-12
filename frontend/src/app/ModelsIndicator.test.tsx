import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { ModelChoice, ModelSettings, ModelSettingsChoice } from "../api/types";
import { describeEffort, describeModelTier, describeRole } from "../lib/status";
import {
  makeCapabilities,
  makeModelSettings,
  makeProviderCapabilities,
  makeRunDetail,
  makeRunSummary,
  mockFetchRoutes,
  tablesOf,
} from "../test/fixtures";
import { App } from "./App";

/**
 * The chip is only interesting in context, so these drive the whole shell: what
 * it says on the runs list is not what it must say on a run page, and the bug
 * this feature exists to prevent — a finished run described by today's default
 * models — only appears when both are on screen in the same session.
 *
 * Tier *names* and model ids are the engine's to change, so nothing here asserts
 * one it did not get from the fixture or from `describeModelTier`.
 */

const DEFAULTS = makeCapabilities().models;
const DEFAULT_TIER = describeModelTier(DEFAULTS.default_tier).label;
const DEFAULT_ROWS = tablesOf(makeCapabilities())[DEFAULTS.default_tier];
/** The chip names a model the way the catalog does; the table keeps the id. */
const DEFAULT_HEADLINE = DEFAULTS.catalog.find(
  (choice) => choice.id === DEFAULT_ROWS.find((row) => row.role === "generation")!.model,
)!.label;

/**
 * A run from before the current baseline: models the allowlist no longer offers
 * and a tier name that no longer exists. Historical runs keep the table they
 * were launched with, so the run page has to render exactly this.
 */
const RUN_TIER = "balanced";
const RUN_TABLE = [
  { role: "generation", model: "claude-sonnet-5", effort: "high" },
  { role: "ranking", model: "claude-sonnet-5", effort: "medium" },
  { role: "proximity", model: "claude-haiku-4-5", effort: "low" },
];
// Not in the catalog any more, so the chip falls back to the shortened id.
const RUN_HEADLINE = RUN_TABLE[0].model;

function stub({
  run = makeRunSummary(),
  detail = makeRunDetail({ run, model_table: RUN_TABLE }),
  capabilities = makeCapabilities(),
  capabilitiesStatus = 200,
  settings = makeModelSettings({
    provider: makeCapabilities().models.default_provider as ModelSettings["provider"],
    tier: makeCapabilities().models.default_tier as ModelSettings["tier"],
    table: tablesOf(makeCapabilities())[
      makeCapabilities().models.default_tier
    ] as ModelSettings["table"],
  }),
  settingsStatus = 200,
}: {
  run?: ReturnType<typeof makeRunSummary>;
  detail?: ReturnType<typeof makeRunDetail>;
  capabilities?: unknown;
  capabilitiesStatus?: number;
  settings?: unknown;
  settingsStatus?: number;
} = {}) {
  const fetchMock = vi.fn(
    mockFetchRoutes([
      { match: "/settings/models", json: settings, status: settingsStatus },
      { match: "/capabilities", json: capabilities, status: capabilitiesStatus },
      { match: "/events/ticket", json: { ticket: "t", expires_in: 60 } },
      { match: "/detail", json: detail },
      { match: "/runs", json: { items: [run], total: 1 } },
    ]),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The editor's panel, named so the segments inside it are not mistaken for it. */
function editor(): HTMLElement {
  return screen.getByRole("group", { name: /models for a new run/i });
}

function modelSelect(role: string): HTMLSelectElement {
  return screen.getByLabelText(`Model for ${describeRole(role)}`) as HTMLSelectElement;
}

function effortSelect(role: string): HTMLSelectElement {
  return screen.getByLabelText(
    `Thinking effort for ${describeRole(role)}`,
  ) as HTMLSelectElement;
}

function optionValues(select: HTMLSelectElement): string[] {
  return within(select)
    .getAllByRole("option")
    .map((option) => (option as HTMLOptionElement).value);
}

function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

/** The chip drops the vendor prefix; the table keeps the full id. */
function short(model: string): string {
  return model.replace(/^claude-/, "");
}

function trigger(): Promise<HTMLElement> {
  return screen.findByRole("button", { name: /^(Models|Run models):/ });
}

describe("model indicator — the system default", () => {
  it("uses 90% of the available viewport and grows beyond the old height cap", async () => {
    stub();
    vi.stubGlobal("innerHeight", 1000);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 100, 600, 400),
    );
    renderApp();
    await userEvent.setup().click(await trigger());
    expect(editor().style.getPropertyValue("--models-panel-max-height")).toBe("810px");
    vi.stubGlobal("innerHeight", 1600);
    fireEvent(window, new Event("resize"));
    expect(editor().style.getPropertyValue("--models-panel-max-height")).toBe("1350px");
    vi.stubGlobal("innerHeight", 400);
    fireEvent(window, new Event("resize"));
    expect(editor().style.getPropertyValue("--models-panel-max-height")).toBe("270px");
  });
  it("shows the global default read-only to a non-admin", async () => {
    const user = userEvent.setup();
    const capabilities = makeCapabilities();
    const settings = makeModelSettings({
      provider: capabilities.models.default_provider as ModelSettings["provider"],
      tier: capabilities.models.default_tier as ModelSettings["tier"],
      table: tablesOf(capabilities)[
        capabilities.models.default_tier
      ] as ModelSettings["table"],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        mockFetchRoutes([
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
          { match: "/settings/models", json: settings },
          { match: "/capabilities", json: capabilities },
        ]),
      ),
    );
    renderApp();

    await user.click(await trigger());
    expect(
      await screen.findByText(
        /only an administrator can change the system-wide model default/i,
      ),
    ).toBeInTheDocument();
    expect(
      within(editor())
        .getAllByRole("combobox")
        .every((select) => select.hasAttribute("disabled")),
    ).toBe(true);
    expect(
      screen.queryByRole("button", { name: /save as the new/i }),
    ).not.toBeInTheDocument();
  });
  it("names the default tier and its headline model", async () => {
    stub();
    renderApp();

    const button = await trigger();
    expect(button).toHaveAccessibleName(`Models: ${DEFAULT_TIER}, ${DEFAULT_HEADLINE}`);
    expect(screen.getByText(`${DEFAULT_TIER} · ${DEFAULT_HEADLINE}`)).toBeInTheDocument();
    // The tier in force is half the sentence: which models a run gets is decided
    // by the tier, and a bare model name leaves out the part that costs money.
    expect(button).toHaveTextContent(DEFAULT_TIER);
    expect(button).toHaveTextContent(DEFAULT_HEADLINE);
  });

  it("says nothing about the source when a saved default was read", async () => {
    // `source: "stored"` is somebody's choice, which is the ordinary case and
    // the one that must stay quiet — a mark on every chip is a mark nobody reads.
    stub();
    renderApp();

    const button = await trigger();
    expect(button).not.toHaveTextContent(/built-in/i);
    expect(button).toHaveAttribute("data-tone", "neutral");
  });

  it("marks the chip when the API served the built-in fallback", async () => {
    // `source: "built_in"` is the endpoint's deliberate total-read fallback: the
    // table is real, but nobody chose it. Presented identically to a stored
    // default, it hides the expensive failure — a save that never persisted.
    stub({
      settings: makeModelSettings({
        provider: DEFAULTS.default_provider as ModelSettings["provider"],
        tier: DEFAULTS.default_tier as ModelSettings["tier"],
        table: DEFAULT_ROWS as ModelSettings["table"],
        source: "built_in",
      }),
    });
    renderApp();

    const button = await screen.findByRole("button", { name: /^Models, Built-in:/ });
    expect(button).toHaveAccessibleName(
      `Models, Built-in: ${DEFAULT_TIER}, ${DEFAULT_HEADLINE}`,
    );
    // The word, and a tone beside it — never the tone alone.
    expect(button).toHaveTextContent("Built-in");
    expect(button).toHaveAttribute("data-tone", "caution");
    // And it still says which models, because they are still the models a run
    // launched now would use.
    expect(button).toHaveTextContent(`${DEFAULT_TIER} · ${DEFAULT_HEADLINE}`);
  });

  it("marks the chip built-in when the stored default cannot be read at all", async () => {
    stub({
      settings: { code: "boom", message: "Settings are unreachable." },
      settingsStatus: 500,
    });
    renderApp();

    // A failed read reaches the same state by another road, so it reads the same
    // way: the engine's own table, and the chip saying that nobody chose it.
    const button = await screen.findByRole("button", { name: /^Models, Built-in:/ });
    expect(button).toHaveTextContent(`${DEFAULT_TIER} · ${DEFAULT_HEADLINE}`);
    expect(button).toHaveAttribute("data-tone", "caution");
  });

  it("opens a table covering every step, with the escalations spelled out", async () => {
    const user = userEvent.setup();
    stub();
    renderApp();

    const button = await trigger();
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");

    const panel = editor();
    // Every step the payload names, spelled the way the app spells roles, each
    // set to the model and effort the payload gave it — and no invented tenth
    // row. The cells are controls now rather than text, which is the whole
    // point of the panel: this is where the default is changed.
    for (const row of DEFAULT_ROWS) {
      expect(modelSelect(row.role)).toHaveValue(row.model);
      expect(effortSelect(row.role)).toHaveValue(row.effort);
    }
    expect(within(panel).getAllByRole("rowheader")).toHaveLength(DEFAULT_ROWS.length);
    expect(within(panel).getByText(/high effort in round 1/i)).toBeInTheDocument();
    expect(within(panel).getByText(/top 5/i)).toBeInTheDocument();
  });

  it("says so, and offers a retry, when the backend cannot be asked", async () => {
    const user = userEvent.setup();
    stub({
      capabilities: { code: "boom", message: "Backend is down." },
      capabilitiesStatus: 500,
    });
    renderApp();

    const button = await screen.findByRole("button", { name: /^Models: unavailable$/ });
    expect(button).toHaveTextContent("Unavailable");
    await user.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent("Backend is down.");
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});

describe("model indicator — a run", () => {
  it("shows the run's own table, not today's default", async () => {
    const user = userEvent.setup();
    const run = makeRunSummary({ id: "run-1", lifecycle: "completed" });
    stub({
      run,
      detail: makeRunDetail({
        run,
        model_table: RUN_TABLE,
        config: { model_tier: RUN_TIER },
      }),
    });
    renderApp("/runs/run-1");

    const button = await screen.findByRole("button", { name: /^Run models:/ });
    // A tier the current vocabulary has never heard of still reads as English.
    const tier = describeModelTier(RUN_TIER).label;
    expect(tier).not.toBe(RUN_TIER);
    expect(button).toHaveAccessibleName(`Run models: ${tier}, ${short(RUN_HEADLINE)}`);
    await user.click(button);

    const panel = screen.getByRole("group", { name: /models for this run/i });
    expect(panel).toHaveTextContent(/fixed at launch/i);
    for (const model of new Set(RUN_TABLE.map((row) => row.model))) {
      expect(within(panel).getAllByRole("cell", { name: model })).toHaveLength(
        RUN_TABLE.filter((row) => row.model === model).length,
      );
    }
    // The default table is longer and names different models. Neither leaks in.
    expect(within(panel).getAllByRole("row")).toHaveLength(RUN_TABLE.length + 1);
    for (const model of new Set(DEFAULT_ROWS.map((row) => row.model))) {
      expect(within(panel).queryByText(model)).not.toBeInTheDocument();
    }
  });

  it("refuses to invent models for a demo run", async () => {
    const user = userEvent.setup();
    const run = makeRunSummary({ id: "run-1", harness: "demo" });
    stub({ run, detail: makeRunDetail({ run, model_table: RUN_TABLE }) });
    renderApp("/runs/run-1");

    const button = await screen.findByRole("button", { name: /^Run models:/ });
    expect(button).toHaveTextContent("Demo · no model calls");
    await user.click(button);

    const panel = screen.getByRole("group", { name: /models for this run/i });
    expect(panel).toHaveTextContent(/makes no model calls/i);
    expect(within(panel).queryByRole("table")).not.toBeInTheDocument();
    for (const model of RUN_TABLE.map((row) => row.model)) {
      expect(within(panel).queryByText(model)).not.toBeInTheDocument();
    }
  });

  it("is honest about an imported run that recorded no table", async () => {
    const user = userEvent.setup();
    const run = makeRunSummary({ id: "run-1", source: "imported" });
    stub({ run, detail: makeRunDetail({ run, model_table: [] }) });
    renderApp("/runs/run-1");

    const button = await screen.findByRole("button", { name: /^Run models:/ });
    expect(button).toHaveTextContent("Not recorded");
    await user.click(button);

    expect(screen.getByRole("group")).toHaveTextContent(/predate this field/i);
  });
});

describe("model indicator — keyboard", () => {
  it("opens from the keyboard and closes on Escape, giving focus back", async () => {
    const user = userEvent.setup();
    stub();
    renderApp();

    const button = await trigger();
    button.focus();
    await user.keyboard("{Enter}");

    const panel = screen.getByRole("group", { name: /models for a new run/i });
    expect(panel).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("group", { name: /models for a new run/i }),
    ).not.toBeInTheDocument();
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveFocus();
  });

  it("closes when the pointer goes somewhere else", async () => {
    const user = userEvent.setup();
    stub();
    renderApp();

    await user.click(await trigger());
    // Named, because the editor's provider and tier segments are groups too.
    expect(editor()).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: /new run/i }));
    expect(
      screen.queryByRole("group", { name: /models for a new run/i }),
    ).not.toBeInTheDocument();
  });
});

/* --- The editor ------------------------------------------------------------
   Driven through the whole shell, like everything else in this file, and against
   the two-provider payload — because the behaviour under test only exists once
   the backend publishes a second vendor, and the tests above already prove the
   single-provider payload still renders.

   Nothing here names a real model, provider, tier or effort: every expected
   string is read out of the fixture, whose vocabulary is deliberately
   placeholder. A test that hardcoded a real id would be asserting our allowlist
   rather than the engine's.
   ------------------------------------------------------------------------- */

const TWO = makeProviderCapabilities().models;
const STORED = makeModelSettings();
/** The table the stored default resolves to. */
const STORED_ROWS = TWO.tiers[STORED.provider][STORED.tier];
const OTHER_PROVIDER = TWO.providers!.find((entry) => entry.id !== STORED.provider)!;
const OTHER_TIER = Object.keys(TWO.tiers[STORED.provider]).find(
  (name) => name !== STORED.tier,
)!;
const storedRow = (role: string) => STORED_ROWS.find((entry) => entry.role === role)!;
/**
 * A tier's label the way the payload gives it, which is the only way the editor
 * is allowed to print one. The fixture's labels are deliberately *not* its ids
 * humanised, so a component that fell back to its own vocabulary would fail here.
 */
const tierLabel = (id: string): string =>
  TWO.tier_catalog.find((entry) => entry.id === id)!.label;
/** The tier that pins a model on every step, which is the one the payload flags. */
const PINNING = TWO.tier_catalog.find(
  (entry) => Object.keys(entry.pinned_models).length > 0,
)!;
/** One row of a *different* tier's table, and an effort its model would accept. */
const OTHER_ROW = TWO.tiers[STORED.provider][OTHER_TIER].find(
  (entry) => entry.role === "reflection",
)!;
const OTHER_EFFORT = TWO.catalog
  .find((choice) => choice.id === OTHER_ROW.model)!
  // The cast is the suite's established idiom at this boundary: the payload's
  // efforts are strings and the generated override type is the real allowlist,
  // and the fixture's vocabulary is deliberately not that allowlist.
  .efforts!.find(
    (effort) => effort !== OTHER_ROW.effort,
  )! as ModelSettingsChoice["effort"];
/** A preset for a tier that is not the selected one, as the wire carries it. */
const OTHER_BUCKET: Record<string, ModelSettingsChoice> = {
  [OTHER_ROW.role]: { effort: OTHER_EFFORT },
};
/** A step the engine says searches the web. */
const GROUNDED_ROLE = STORED_ROWS.find((entry) => entry.grounded)!.role;
/** The one model the payload says cannot reach the web. */
const CANNOT_SEARCH = TWO.catalog.find((choice) => choice.grounded === false)!;
/** A step whose effort the tiers move, and one they never do. */
const MOVES = STORED_ROWS.find(
  (entry) =>
    TWO.tiers[STORED.provider][OTHER_TIER].find((other) => other.role === entry.role)!
      .effort !== entry.effort,
)!;
/** A second, different step — so "reset one row" has another row to leave alone. */
const SECOND = STORED_ROWS.find((entry) => entry.role !== "generation")!;
const SECOND_TO = TWO.catalog
  .find((choice) => choice.id === SECOND.model)!
  .efforts!.find((effort) => effort !== SECOND.effort)!;
const PINNED = STORED_ROWS.find((entry) =>
  Object.keys(TWO.tiers[STORED.provider]).every(
    (name) =>
      TWO.tiers[STORED.provider][name].find((other) => other.role === entry.role)!
        .effort === entry.effort,
  ),
)!;

function stubTwo(
  options: { settings?: unknown; settingsStatus?: number; putStatus?: number } = {},
) {
  const base = mockFetchRoutes([
    {
      match: "/settings/models",
      json: options.settings ?? STORED,
      status: options.settingsStatus ?? 200,
    },
    { match: "/capabilities", json: makeProviderCapabilities() },
    { match: "/runs", json: { items: [], total: 0 } },
  ]);
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      if (init?.method === "PUT") {
        if (options.putStatus != null && options.putStatus >= 400) {
          return new Response(
            JSON.stringify({ code: "nope", message: "The backend refused it." }),
            {
              status: options.putStatus,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        const body = JSON.parse(String(init.body)) as object;
        return new Response(JSON.stringify({ ...STORED, ...body }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return base(input, init);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function openEditor() {
  const user = userEvent.setup();
  await user.click(await trigger());
  await screen.findByLabelText("Model for Generation");
  return user;
}

describe("model editor — filling the table", () => {
  it("offers both vendors, and picking one refills every row from its table", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    expect(modelSelect("generation")).toHaveValue(storedRow("generation").model);

    const providers = within(editor()).getByRole("group", { name: "Provider" });
    await user.click(
      within(providers).getByRole("button", {
        name: new RegExp(OTHER_PROVIDER.label, "i"),
      }),
    );

    for (const entry of TWO.tiers[OTHER_PROVIDER.id][STORED.tier]) {
      expect(modelSelect(entry.role)).toHaveValue(entry.model);
      expect(effortSelect(entry.role)).toHaveValue(entry.effort);
    }
  });

  it("moves effort and nothing else when the tier changes", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    const before = modelSelect(MOVES.role).value;
    const tiers = within(editor()).getByRole("group", { name: "Effort tier" });
    await user.click(
      within(tiers).getByRole("button", { name: describeModelTier(OTHER_TIER).label }),
    );

    const after = TWO.tiers[STORED.provider][OTHER_TIER].find(
      (entry) => entry.role === MOVES.role,
    )!;
    // The contract in one assertion: a tier is an effort dial. Which model a
    // step runs on is decided by the job, and a tier must never move it.
    expect(modelSelect(MOVES.role)).toHaveValue(before);
    expect(effortSelect(MOVES.role)).toHaveValue(after.effort);
  });

  it("says which step the tier will never move, rather than looking broken", async () => {
    stubTwo();
    renderApp();
    await openEditor();

    const pinned = modelSelect(PINNED.role).closest("tr")!;
    expect(within(pinned).getByText(/same at every tier/i)).toBeInTheDocument();

    const moving = modelSelect(MOVES.role).closest("tr")!;
    expect(within(moving).queryByText(/same at every tier/i)).toBeNull();
  });

  it("offers every model both vendors publish, grouped, under catalog names", async () => {
    stubTwo();
    renderApp();
    await openEditor();

    // Steps mix providers freely, so the segment above is a quick-set and never
    // a lock: every row offers all four models whichever provider is selected.
    const select = modelSelect("generation");
    expect(optionValues(select)).toEqual(TWO.catalog.map((choice) => choice.id));
    for (const choice of TWO.catalog) {
      expect(
        within(select).getByRole("option", { name: choice.label }),
      ).toBeInTheDocument();
    }
    expect(select.querySelectorAll("optgroup")).toHaveLength(TWO.providers!.length);
  });

  it("shows the ladder of the model in the row, not one shared scale", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    const heavy = TWO.catalog.find(
      (choice) => choice.id === storedRow("generation").model,
    )!;
    expect(optionValues(effortSelect("generation"))).toEqual(heavy.efforts);

    // Two models with different ladders in the same table at the same time is
    // the property one global effort list cannot express.
    const light = TWO.catalog.find(
      (choice) => choice.id === storedRow("proximity").model,
    )!;
    expect(light.efforts).not.toEqual(heavy.efforts);
    expect(optionValues(effortSelect("proximity"))).toEqual(light.efforts);

    // And every rung is named in the app's words, not the wire's: the default
    // tier spends most of its calls on the top rung, and `humanize` renders that
    // one "Xhigh" — an identifier wearing a capital letter, in the row people
    // look at first.
    expect(
      within(effortSelect("generation"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(heavy.efforts!.map((effort) => describeEffort(effort)));

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);
    expect(optionValues(effortSelect("generation"))).toEqual([
      storedRow("generation").effort,
      ...CANNOT_SEARCH.efforts!,
    ]);
  });
});

describe("model editor — what it warns about", () => {
  it("flags a step that searches the web set to a model that cannot", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    expect(screen.queryByText(/search the web/i)).toBeNull();
    await user.selectOptions(modelSelect(GROUNDED_ROLE), CANNOT_SEARCH.id);

    const flagged = modelSelect(GROUNDED_ROLE).closest("tr")!;
    expect(within(flagged).getByText(/search the web/i)).toBeInTheDocument();
  });

  it("raises no grounding warning at all when the payload does not publish it", async () => {
    // The rule the whole warning hangs on: absent is absent. A backend that has
    // not probed a model must not have "cannot search" asserted on its behalf.
    const silent = makeProviderCapabilities();
    // `grounded` deleted rather than set false: the assertion is about a backend
    // that has not published the fact, not one that has published a denial.
    silent.models.catalog = silent.models.catalog.map((choice) => {
      const copy = { ...choice } as Partial<ModelChoice>;
      delete copy.grounded;
      return copy as ModelChoice;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        mockFetchRoutes([
          { match: "/settings/models", json: STORED },
          { match: "/capabilities", json: silent },
          { match: "/runs", json: { items: [], total: 0 } },
        ]),
      ),
    );
    renderApp();
    const user = await openEditor();

    await user.selectOptions(modelSelect(GROUNDED_ROLE), CANNOT_SEARCH.id);
    expect(screen.queryByText(/search the web/i)).toBeNull();
  });

  it("says so when the row's effort is not one the chosen model takes", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);

    const changed = modelSelect("generation").closest("tr")!;
    // Never silently lowered on screen: the value stays visible, and flagged.
    expect(effortSelect("generation")).toHaveValue(storedRow("generation").effort);
    expect(within(changed).getByText(/not on this model/i)).toBeInTheDocument();
  });
});

describe("model editor — editing, resetting and saving", () => {
  it("marks a changed row, resets it alone, and resets the whole tier", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();
    const label = tierLabel(STORED.tier);

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);
    await user.selectOptions(effortSelect(SECOND.role), SECOND_TO);
    expect(screen.getByText(`Edited — differs from ${label}`)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: `Reset Generation to the ${label} table` }),
    );
    expect(modelSelect("generation")).toHaveValue(storedRow("generation").model);
    // One row back, the other untouched — the promise every per-row reset makes.
    expect(effortSelect(SECOND.role)).toHaveValue(SECOND_TO);

    // And the whole tier back to the preset it started as. Named for the tier it
    // resets rather than "the table", because a save now writes four of them.
    await user.click(
      screen.getByRole("button", {
        name: `Reset ${label} to the built-in preset`,
      }),
    );
    for (const entry of STORED_ROWS) {
      expect(modelSelect(entry.role)).toHaveValue(entry.model);
      expect(effortSelect(entry.role)).toHaveValue(entry.effort);
    }
    expect(screen.queryByText(`Edited — differs from ${label}`)).toBeNull();
  });

  it("keeps the edit when the panel is closed and opened again", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("group", { name: /models for a new run/i })).toBeNull();

    await user.click(await trigger());
    // A table somebody has just built is not thrown away by a stray click on
    // the page behind it.
    expect(modelSelect("generation")).toHaveValue(CANNOT_SEARCH.id);
  });

  it("redefines the selected tier, sending only the changed steps", async () => {
    const fetchMock = stubTwo();
    renderApp();
    const user = await openEditor();

    // The button says which tier it is about to redefine, in the payload's own
    // word for it. "Save as the default" described half of what this does.
    const name = new RegExp(`save as the new ${tierLabel(STORED.tier)}`, "i");
    const save = screen.getByRole("button", { name });
    // Nothing to save until something changes.
    expect(save).toBeDisabled();

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);
    expect(save).toBeEnabled();
    await user.click(save);

    const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(String(put[0])).toContain("/settings/models");
    expect(JSON.parse(String((put[1] as RequestInit).body))).toEqual({
      provider: STORED.provider,
      tier: STORED.tier,
      // The edit lands in the selected tier's bucket and nowhere else, holding
      // only the cell that moved: the table is not re-sent as nine overrides,
      // and the three tiers nobody touched are absent rather than materialised
      // — absent is what keeps them following the built-in table.
      tiers: { [STORED.tier]: { generation: { model: CANNOT_SEARCH.id } } },
    });
    // The mirror is never sent. It is derived from the buckets on every response,
    // and a client that sent it back would be answering "what is this tier"
    // twice in one request.
    expect(
      JSON.parse(String((put[1] as RequestInit).body)) as Record<string, unknown>,
    ).not.toHaveProperty("overrides");
    expect(
      await screen.findByText(/new runs will use these models/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name })).toBeDisabled();
  });

  it("keeps the edit and says why when the save is refused", async () => {
    stubTwo({ putStatus: 422 });
    renderApp();
    const user = await openEditor();
    const name = new RegExp(`save as the new ${tierLabel(STORED.tier)}`, "i");

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);
    await user.click(screen.getByRole("button", { name }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The backend refused it.");
    // The work is still on screen, and still savable.
    expect(modelSelect("generation")).toHaveValue(CANNOT_SEARCH.id);
    expect(screen.getByRole("button", { name })).toBeEnabled();
  });

  it("still shows the engine's table when the stored default cannot be read", async () => {
    stubTwo({
      settings: { code: "boom", message: "Settings are unreachable." },
      settingsStatus: 500,
    });
    renderApp();
    await openEditor();

    // The two endpoints answer different questions. Capabilities says which
    // models exist and what each tier resolves to — everything the table is
    // drawn from. Settings says only which of those was last chosen. Losing the
    // second costs a preference, not the table, and a panel that turned a
    // missing preference into a red box would be dead in exactly the ordinary
    // case: a frontend running ahead of the backend that stores it.
    for (const entry of STORED_ROWS) {
      expect(modelSelect(entry.role)).toHaveValue(entry.model);
    }
    expect(screen.getByText(/no stored default was read/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /try reading it again/i }),
    ).toBeInTheDocument();
  });

  it("never asks for the stored default on a run page", async () => {
    const user = userEvent.setup();
    const run = makeRunSummary({ id: "run-1", lifecycle: "completed" });
    const fetchMock = stub({
      run,
      detail: makeRunDetail({ run, model_table: RUN_TABLE }),
    });
    renderApp("/runs/run-1");
    await user.click(await screen.findByRole("button", { name: /^Run models:/ }));

    // A run's table is frozen in its own config. Reading the system default to
    // draw it would be reading the wrong document, and asking for it at all is
    // the tell that a screen might.
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes("/settings/models")),
    ).toHaveLength(0);
  });

  it("hides the provider segment while there is only one provider", async () => {
    stub();
    renderApp();
    await openEditor();

    // A segment with one option is a statement dressed as a choice.
    expect(within(editor()).queryByRole("group", { name: "Provider" })).toBeNull();
  });
});

/* --- One table per tier ----------------------------------------------------
   The behaviour request #1 asked for: saving redefines the tier that is
   selected, so the document holds one bucket of overrides per tier and the
   editor shows one of them at a time. The three properties worth pinning are
   that switching tiers does not lose the other tier's work, that resetting one
   tier does not touch another, and that a tier nobody edited is *absent* from
   what is saved — because absent is what makes it keep following the built-in
   table after the catalog moves.
   ------------------------------------------------------------------------- */

function putBody(fetchMock: ReturnType<typeof stubTwo>): Record<string, unknown> {
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT")!;
  return JSON.parse(String((put[1] as RequestInit).body)) as Record<string, unknown>;
}

describe("model editor — one table per tier", () => {
  it("keeps each tier's edits apart, and saves every preset", async () => {
    const fetchMock = stubTwo({
      settings: makeModelSettings({ tiers: { [OTHER_TIER]: OTHER_BUCKET } }),
    });
    renderApp();
    const user = await openEditor();

    // A preset that is redefined and off screen is still going to be saved, so
    // the panel says which ones those are.
    expect(
      screen.getByText(new RegExp(`also redefined: ${tierLabel(OTHER_TIER)}`, "i")),
    ).toBeInTheDocument();

    await user.selectOptions(modelSelect("generation"), CANNOT_SEARCH.id);

    const tiers = within(editor()).getByRole("group", { name: "Effort tier" });
    await user.click(within(tiers).getByRole("button", { name: tierLabel(OTHER_TIER) }));

    // The other tier shows its *own* preset — not the edit just made, and not
    // that tier's bare table.
    expect(effortSelect(OTHER_ROW.role)).toHaveValue(OTHER_EFFORT);
    expect(modelSelect("generation")).toHaveValue(
      TWO.tiers[STORED.provider][OTHER_TIER].find((entry) => entry.role === "generation")!
        .model,
    );

    await user.click(within(tiers).getByRole("button", { name: tierLabel(STORED.tier) }));
    // And switching back is not a discard. An override is a difference from *its
    // own* tier's table, so the two never had to be measured against each other.
    expect(modelSelect("generation")).toHaveValue(CANNOT_SEARCH.id);

    await user.click(
      screen.getByRole("button", {
        name: new RegExp(`save as the new ${tierLabel(STORED.tier)}`, "i"),
      }),
    );
    expect(putBody(fetchMock)).toEqual({
      provider: STORED.provider,
      tier: STORED.tier,
      tiers: {
        [STORED.tier]: { generation: { model: CANNOT_SEARCH.id } },
        [OTHER_TIER]: OTHER_BUCKET,
      },
    });
  });

  it("resets one tier and leaves the other presets alone", async () => {
    const fetchMock = stubTwo({
      settings: makeModelSettings({
        tiers: {
          [STORED.tier]: { generation: { model: CANNOT_SEARCH.id } },
          [OTHER_TIER]: OTHER_BUCKET,
        } as ModelSettings["tiers"],
      }),
    });
    renderApp();
    const user = await openEditor();

    expect(modelSelect("generation")).toHaveValue(CANNOT_SEARCH.id);
    await user.click(
      screen.getByRole("button", {
        name: `Reset ${tierLabel(STORED.tier)} to the built-in preset`,
      }),
    );
    expect(modelSelect("generation")).toHaveValue(storedRow("generation").model);

    await user.click(
      screen.getByRole("button", {
        name: new RegExp(`save as the new ${tierLabel(STORED.tier)}`, "i"),
      }),
    );
    // The reset tier is *absent*, not present and empty: an empty bucket would
    // read as a decision that this tier has no overrides, and freeze its rows
    // against a catalog that moved. The other tier's preset is untouched.
    expect(putBody(fetchMock)).toEqual({
      provider: STORED.provider,
      tier: STORED.tier,
      tiers: { [OTHER_TIER]: OTHER_BUCKET },
    });
  });

  it("reads a payload from before the presets as that tier's preset", async () => {
    // The live shape while a frontend runs ahead of its backend: `overrides` and
    // no `tiers` at all. It is the tier's bucket — that is what it always meant,
    // there being only one tier's worth of it — and reading it as "no overrides
    // anywhere" would silently drop somebody's saved table on the next save.
    const legacy = makeModelSettings({
      tiers: {
        [STORED.tier]: { generation: { model: CANNOT_SEARCH.id } },
      } as ModelSettings["tiers"],
    }) as Partial<ModelSettings>;
    delete legacy.tiers;
    const fetchMock = stubTwo({ settings: legacy });
    renderApp();
    const user = await openEditor();

    expect(modelSelect("generation")).toHaveValue(CANNOT_SEARCH.id);
    // Nothing to save: the document says the same thing in the new shape.
    const save = screen.getByRole("button", {
      name: new RegExp(`save as the new ${tierLabel(STORED.tier)}`, "i"),
    });
    expect(save).toBeDisabled();

    await user.selectOptions(effortSelect(SECOND.role), SECOND_TO);
    await user.click(save);
    // And it is written back in the new shape, migration included.
    expect(putBody(fetchMock)).toEqual({
      provider: STORED.provider,
      tier: STORED.tier,
      tiers: {
        [STORED.tier]: {
          generation: { model: CANNOT_SEARCH.id },
          [SECOND.role]: { effort: SECOND_TO },
        },
      },
    });
  });

  it("sends no buckets at all when nothing has been overridden", async () => {
    const fetchMock = stubTwo();
    renderApp();
    const user = await openEditor();

    const tiers = within(editor()).getByRole("group", { name: "Effort tier" });
    await user.click(within(tiers).getByRole("button", { name: tierLabel(OTHER_TIER) }));
    await user.click(
      screen.getByRole("button", {
        name: new RegExp(`save as the new ${tierLabel(OTHER_TIER)}`, "i"),
      }),
    );

    // Moving the tier dial and saving is a complete, legitimate save: it changes
    // which preset new runs use and states that none of them is overridden.
    expect(putBody(fetchMock)).toEqual({
      provider: STORED.provider,
      tier: OTHER_TIER,
      tiers: {},
    });
  });

  it("drops every preset when the vendor changes, because the tables all did", async () => {
    const fetchMock = stubTwo({
      settings: makeModelSettings({ tiers: { [OTHER_TIER]: OTHER_BUCKET } }),
    });
    renderApp();
    const user = await openEditor();

    const providers = within(editor()).getByRole("group", { name: "Provider" });
    await user.click(
      within(providers).getByRole("button", {
        name: new RegExp(OTHER_PROVIDER.label, "i"),
      }),
    );

    // An override is a difference from a table. Picking a vendor refills *every*
    // tier's table, so every bucket is a difference from a table that no longer
    // applies — which is why this is the one control that clears them all.
    expect(screen.queryByText(/also redefined/i)).toBeNull();
    await user.click(
      screen.getByRole("button", {
        name: new RegExp(`save as the new ${tierLabel(STORED.tier)}`, "i"),
      }),
    );
    expect(putBody(fetchMock)).toEqual({
      provider: OTHER_PROVIDER.id,
      tier: STORED.tier,
      tiers: {},
    });
  });
});

describe("model editor — the tier that pins a model", () => {
  it("names and describes every tier from the payload, not from its own words", async () => {
    stubTwo();
    renderApp();
    const user = await openEditor();

    const tiers = within(editor()).getByRole("group", { name: "Effort tier" });
    // The payload calls this tier something its id does not spell. A control
    // hard-coding four tier names would print the id humanised instead.
    expect(PINNING.label).not.toBe(describeModelTier(PINNING.id).label);
    const button = within(tiers).getByRole("button", { name: PINNING.label });

    await user.click(button);
    // What the tier does, in the engine's sentence.
    expect(within(editor()).getByText(PINNING.note)).toBeInTheDocument();
    // And the fact a reader would otherwise have to get by diffing two tables,
    // stated: this tier moves the model and not only the effort dial.
    const pinned = TWO.catalog.find(
      (choice) => choice.id === Object.values(PINNING.pinned_models)[0],
    )!;
    const claim = within(editor()).getByText(
      new RegExp(`every step runs ${pinned.label}`, "i"),
    );
    expect(claim).toHaveTextContent(/sets the model, not just the effort/i);
    // Which the table under it agrees with, row by row.
    for (const entry of TWO.tiers[STORED.provider][PINNING.id]) {
      expect(modelSelect(entry.role)).toHaveValue(pinned.id);
    }
  });

  it("warns about a missing CLI rather than letting the first call find it", async () => {
    const unavailable = makeProviderCapabilities();
    const reason = "needs the codex CLI installed and signed in";
    unavailable.models.tier_catalog = unavailable.models.tier_catalog.map((entry) =>
      entry.id === PINNING.id
        ? { ...entry, available: false, unavailable_reason: reason }
        : entry,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(
        mockFetchRoutes([
          { match: "/settings/models", json: STORED },
          { match: "/capabilities", json: unavailable },
          { match: "/runs", json: { items: [], total: 0 } },
        ]),
      ),
    );
    renderApp();
    const user = await openEditor();

    // Nothing said about a tier that can run here.
    expect(screen.queryByText(new RegExp(reason, "i"))).toBeNull();

    const tiers = within(editor()).getByRole("group", { name: "Effort tier" });
    await user.click(within(tiers).getByRole("button", { name: PINNING.label }));

    // The backend's own words, under the tier's name — and the tier is still
    // selectable and still savable, because what cannot run it is this machine.
    const notice = screen.getByRole("status");
    expect(notice).toHaveTextContent(PINNING.label);
    expect(notice).toHaveTextContent(reason);
    expect(notice).toHaveTextContent(/fail on its first call/i);
    expect(
      screen.getByRole("button", {
        name: new RegExp(`save as the new ${PINNING.label}`, "i"),
      }),
    ).toBeEnabled();
  });
});

describe("model editor — a run is not editable", () => {
  it("shows a run's frozen table with nothing to change and nothing to save", async () => {
    const user = userEvent.setup();
    const run = makeRunSummary({ id: "run-1", lifecycle: "completed" });
    stub({ run, detail: makeRunDetail({ run, model_table: RUN_TABLE }) });
    renderApp("/runs/run-1");

    await user.click(await screen.findByRole("button", { name: /^Run models:/ }));

    const panel = screen.getByRole("group", { name: /models for this run/i });
    expect(within(panel).queryAllByRole("combobox")).toHaveLength(0);
    expect(within(panel).queryByRole("button", { name: /save/i })).toBeNull();
    expect(within(panel).queryByRole("group", { name: "Provider" })).toBeNull();
  });
});

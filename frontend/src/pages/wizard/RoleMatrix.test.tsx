import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { MODEL_TIERS } from "../../api/types";
import type { Capabilities, RunConfig } from "../../api/types";
import { PRE_RUN_ROLES, runRoles } from "../../lib/models";
import { describeEffort, describeRole } from "../../lib/status";
import { makeCapabilities, mockFetchRoutes, tablesOf } from "../../test/fixtures";
import { ConfigureStep } from "./ConfigureStep";
import { RoleMatrix } from "./RoleMatrix";
import { RESET_ALL_LABEL, asRoleModel, defaultConfig, resetRoleLabel } from "./state";
import type { RoleOverrides } from "./state";

/**
 * The matrix is a view of `/api/capabilities` and nothing else.
 *
 * So nothing in this file names a model id or an effort: every expected string
 * is read out of the fixture payload, whose model ids and tier names are
 * deliberately placeholders (`model-floor`, `floor-tier`). A test that hardcoded
 * a real id would pass today and lie the week the engine moves — and it would be
 * asserting our list of models rather than the engine's. Where a *tier* name is
 * unavoidable (the wizard's config field is typed to the backend's enum) it
 * comes from that generated enum, never from a literal.
 */

const CAPABILITIES = makeCapabilities();
const MODELS = CAPABILITIES.models;
const TIER = MODELS.default_tier;
/** The default provider's tables, keyed by tier — the payload's own shape. */
const TABLES = tablesOf(CAPABILITIES);
const OTHER_TIER = Object.keys(TABLES).find((name) => name !== TIER)!;
/** Every row the payload publishes for the default tier. */
const ALL_ROWS = TABLES[TIER];
/** The rows a launched run actually executes, which is what the matrix offers. */
const ROWS = runRoles(ALL_ROWS);
/** A role the payload publishes that no run ever calls. */
const PRE_RUN = ALL_ROWS.find((entry) => PRE_RUN_ROLES.has(entry.role))!;
/** A role that only runs when the diversity injection fires. */
const GRAFT_ONLY = ALL_ROWS.find(
  (entry) => !PRE_RUN_ROLES.has(entry.role) && !ROWS.includes(entry),
)!;

const label = (role: string): string => describeRole(role);

function otherModelThan(model: string) {
  return MODELS.catalog.find((choice) => choice.id !== model)!;
}

function otherEffortThan(effort: string): string {
  return MODELS.efforts.find((value) => value !== effort)!;
}

function stubCapabilities(payload: unknown = CAPABILITIES, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(mockFetchRoutes([{ match: "/capabilities", json: payload, status }])),
  );
}

/* --- Harnesses -------------------------------------------------------------
   Both drive the real state helpers, so what these assert is the object the
   wizard would put in `config.model_overrides` and hand to `POST /runs`.
   ------------------------------------------------------------------------- */

/** What the wizard would send, read back off the page rather than out of a spy. */
function Probe({
  tier,
  overrides,
  provider = null,
}: {
  tier: string;
  overrides: RoleOverrides;
  provider?: string | null;
}) {
  return (
    <>
      <span data-testid="tier">{tier}</span>
      <span data-testid="provider">{provider ?? ""}</span>
      <span data-testid="overrides">{JSON.stringify(overrides)}</span>
    </>
  );
}

function sentOverrides(): RoleOverrides {
  return JSON.parse(screen.getByTestId("overrides").textContent!) as RoleOverrides;
}

function sentTier(): string {
  return screen.getByTestId("tier").textContent!;
}

function Matrix({
  initial = {} as RoleOverrides,
  graft = false,
  demo = false,
  startProvider = null,
}: {
  initial?: RoleOverrides;
  graft?: boolean;
  demo?: boolean;
  /** Which vendor's table the tier fills from. Null on a single-provider payload. */
  startProvider?: string | null;
}) {
  const [provider, setProvider] = useState<string | null>(startProvider);
  const [tier, setTier] = useState<string>(TIER);
  const [overrides, setOverrides] = useState<RoleOverrides>(initial);
  return (
    <>
      <Probe tier={tier} overrides={overrides} provider={provider} />
      <RoleMatrix
        provider={provider}
        tier={tier}
        overrides={overrides}
        graft={graft}
        demo={demo}
        onProviderChange={(next) => {
          setProvider(next);
          setOverrides({});
        }}
        onTierChange={(next) => {
          setTier(next);
          setOverrides({});
        }}
        onOverridesChange={setOverrides}
      />
    </>
  );
}

function row(role: string): HTMLElement {
  return screen.getByRole("row", { name: new RegExp(escape(label(role))) });
}

function escape(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\-]/g, "\\$&");
}

function modelSelect(role: string): HTMLSelectElement {
  return screen.getByLabelText(`Model for ${label(role)}`) as HTMLSelectElement;
}

function effortSelect(role: string): HTMLSelectElement {
  return screen.getByLabelText(`Thinking effort for ${label(role)}`) as HTMLSelectElement;
}

async function renderMatrix(initial: RoleOverrides = {}) {
  stubCapabilities();
  render(<Matrix initial={initial} />);
  await screen.findByRole("table");
  return userEvent.setup();
}

/* --- The table ------------------------------------------------------------ */

describe("RoleMatrix", () => {
  it("renders one row per role from the payload, set to that tier's table", async () => {
    await renderMatrix();

    for (const entry of ROWS) {
      const cells = within(row(entry.role)).getAllByRole("cell");
      expect(within(row(entry.role)).getByRole("rowheader")).toHaveTextContent(
        label(entry.role),
      );
      expect(modelSelect(entry.role)).toHaveValue(entry.model);
      expect(effortSelect(entry.role)).toHaveValue(entry.effort);
      // Whether a step may search is the engine's call, not a setting: it is
      // shown because it changes how the row reads, and never offered.
      expect(cells[2]).toHaveTextContent(entry.grounded ? "Yes" : "No");
    }
    expect(screen.getAllByRole("rowheader")).toHaveLength(ROWS.length);
  });

  it("offers only the steps a launched run executes", async () => {
    await renderMatrix();

    // The pre-run role is published — the workshop calls it before a run exists
    // — and the orchestrator never invokes it. A model chosen for it would be
    // accepted by the backend and then silently ignored for this run.
    expect(PRE_RUN).toBeTruthy();
    expect(screen.queryByLabelText(`Model for ${label(PRE_RUN.role)}`)).toBeNull();
    expect(
      screen.queryByRole("rowheader", { name: new RegExp(escape(label(PRE_RUN.role))) }),
    ).toBeNull();

    // Same for the collapse-only role while the injection that fires it is off:
    // the Confirm step already dropped it, so the two screens now agree.
    expect(GRAFT_ONLY).toBeTruthy();
    expect(screen.queryByLabelText(`Model for ${label(GRAFT_ONLY.role)}`)).toBeNull();

    // And the count in the copy is the count on screen, not a word.
    expect(
      screen.getByText(new RegExp(`Each of the ${ROWS.length} steps`)),
    ).toBeVisible();
  });

  it("brings the collapse-only step back when the injection is on", async () => {
    stubCapabilities();
    render(<Matrix graft />);
    await screen.findByRole("table");

    expect(modelSelect(GRAFT_ONLY.role)).toHaveValue(GRAFT_ONLY.model);
    expect(screen.getAllByRole("rowheader")).toHaveLength(ROWS.length + 1);
  });

  it("names no model and offers no choice for a demo run", async () => {
    stubCapabilities();
    render(<Matrix demo />);
    await screen.findByRole("table");

    // A demo run makes no model calls at all, so this is the one screen in the
    // wizard that must not show a demo run real model names.
    for (const choice of MODELS.catalog) {
      expect(document.body).not.toHaveTextContent(choice.label);
    }
    expect(screen.queryAllByRole("combobox", { name: /^Model for/ })).toHaveLength(0);
    expect(
      screen.queryAllByRole("combobox", { name: /^Thinking effort for/ }),
    ).toHaveLength(0);
    expect(screen.getByText(/a demo run calls no model at all/i)).toBeVisible();
  });

  it("shows the engine's own reason for each row, and describes both selects with it", async () => {
    await renderMatrix();

    for (const entry of ROWS) {
      const note = within(row(entry.role)).getByText(entry.note);
      expect(note.id).toBeTruthy();
      expect(modelSelect(entry.role)).toHaveAttribute("aria-describedby", note.id);
      expect(effortSelect(entry.role)).toHaveAttribute("aria-describedby", note.id);
    }
  });

  it("offers exactly the models and efforts the payload publishes", async () => {
    await renderMatrix();

    const models = within(modelSelect(ROWS[0].role))
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value);
    expect(models).toEqual(MODELS.catalog.map((choice) => choice.id));

    const efforts = within(effortSelect(ROWS[0].role))
      .getAllByRole("option")
      .map((option) => option.textContent);
    // `describeEffort`, not `humanize`: the ladder now includes `xhigh`, which
    // `humanize` renders "Xhigh" — an identifier wearing a capital letter.
    expect(efforts).toEqual(MODELS.efforts.map((effort) => describeEffort(effort)));
  });

  it("sends no overrides at all while the defaults are accepted", async () => {
    await renderMatrix();

    expect(sentOverrides()).toEqual({});
    // Nothing to reset, so nothing offers to.
    expect(screen.queryAllByRole("button", { name: /Back to the tier/ })).toHaveLength(0);
  });

  it("writes exactly one override when one cell changes", async () => {
    const user = await renderMatrix();
    const step = ROWS[0];
    const picked = otherModelThan(step.model);

    await user.selectOptions(modelSelect(step.role), picked.id);

    expect(sentOverrides()).toEqual({ [step.role]: { model: picked.id } });
    // The cell that moved, and only that cell.
    expect(modelSelect(step.role)).toHaveValue(picked.id);
    expect(effortSelect(step.role)).toHaveValue(step.effort);
  });

  it("keeps model and effort independent on the same row", async () => {
    const user = await renderMatrix();
    const step = ROWS[0];
    const picked = otherModelThan(step.model);
    const effort = otherEffortThan(step.effort);

    await user.selectOptions(modelSelect(step.role), picked.id);
    await user.selectOptions(effortSelect(step.role), effort);

    expect(sentOverrides()).toEqual({ [step.role]: { model: picked.id, effort } });
  });

  it("stops being an override when a cell is put back to the tier's value", async () => {
    const user = await renderMatrix();
    const step = ROWS[0];
    const effort = otherEffortThan(step.effort);

    await user.selectOptions(effortSelect(step.role), effort);
    expect(sentOverrides()).toEqual({ [step.role]: { effort } });

    await user.selectOptions(effortSelect(step.role), step.effort);
    // Not `{ [role]: {} }`: changed and changed back is indistinguishable from
    // never touched, which is exactly what the reset affordances promise.
    expect(sentOverrides()).toEqual({});
  });

  it("resets one row without touching the others", async () => {
    const user = await renderMatrix();
    const [first, second] = ROWS;
    const effort = otherEffortThan(second.effort);

    await user.selectOptions(modelSelect(first.role), otherModelThan(first.model).id);
    await user.selectOptions(effortSelect(second.role), effort);
    expect(Object.keys(sentOverrides())).toEqual([first.role, second.role]);

    // WCAG 2.5.3: the accessible name starts with the visible word, so "click
    // Reset" reaches it.
    const reset = screen.getByRole("button", { name: resetRoleLabel(label(first.role)) });
    expect(resetRoleLabel(label(first.role)).startsWith(reset.textContent!)).toBe(true);
    await user.click(reset);

    expect(sentOverrides()).toEqual({ [second.role]: { effort } });
    expect(modelSelect(first.role)).toHaveValue(first.model);
    expect(effortSelect(second.role)).toHaveValue(effort);
  });

  it("resets the whole matrix, and counts what is changed while there is", async () => {
    const user = await renderMatrix();
    const [first, second] = ROWS;

    await user.selectOptions(modelSelect(first.role), otherModelThan(first.model).id);
    expect(screen.getByText("1 step changed")).toBeInTheDocument();

    await user.selectOptions(effortSelect(second.role), otherEffortThan(second.effort));
    expect(screen.getByText("2 steps changed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: RESET_ALL_LABEL }));

    expect(sentOverrides()).toEqual({});
    expect(screen.queryAllByRole("button", { name: /Back to the tier/ })).toHaveLength(0);
    for (const entry of ROWS) {
      expect(modelSelect(entry.role)).toHaveValue(entry.model);
      expect(effortSelect(entry.role)).toHaveValue(entry.effort);
    }
  });

  it("renders the escalations no table can express, and points the table at them", async () => {
    await renderMatrix();

    const described = screen.getByRole("table").getAttribute("aria-describedby");
    expect(described).toBeTruthy();
    const block = document.getElementById(described!);
    expect(MODELS.notes.length).toBeGreaterThan(0);
    for (const note of MODELS.notes) {
      expect(block).toHaveTextContent(note);
    }
  });

  it("keeps a value the catalogue no longer offers rather than swapping it", async () => {
    stubCapabilities();
    const retired = "model-from-a-retired-engine";
    render(<Matrix initial={{ [ROWS[0].role]: { model: asRoleModel(retired) } }} />);
    await screen.findByRole("table");

    expect(modelSelect(ROWS[0].role)).toHaveValue(retired);
  });

  it("says so, with a way back, when the table cannot be loaded", async () => {
    stubCapabilities({ code: "boom", message: "Backend is down." }, 500);
    render(<Matrix />);

    expect(await screen.findByText(/backend is down/i)).toBeInTheDocument();
    // The rule from the UX review: an error replaces the content rather than
    // sitting next to a table of plausible-looking defaults.
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

/* --- The tier that fills it ------------------------------------------------
   Driven through the whole settings step, because the promise being tested is
   the wiring: the tier control and the matrix are one control surface.
   ------------------------------------------------------------------------- */

/** The wizard's config field is typed to the backend's tier enum, so the tier
 *  names here come from that enum; the tables behind them stay the fixture's. */
const START_TIER = defaultConfig().model_tier;
const TARGET_TIER = MODEL_TIERS.find((name) => name !== START_TIER)!;
const WIZARD_CAPABILITIES: Capabilities = {
  ...CAPABILITIES,
  models: {
    ...MODELS,
    default_tier: START_TIER,
    tiers: {
      [MODELS.default_provider]: {
        [START_TIER]: TABLES[TIER],
        [TARGET_TIER]: TABLES[OTHER_TIER],
      },
    },
  },
};
/** A step the two tiers disagree about: the one a refill has to visibly move. */
const MOVED = runRoles(tablesOf(WIZARD_CAPABILITIES)[START_TIER]).find(
  (entry) =>
    tablesOf(WIZARD_CAPABILITIES)[TARGET_TIER].find((other) => other.role === entry.role)!
      .model !== entry.model,
)!;

function Wizard() {
  const [config, setConfig] = useState<RunConfig>(defaultConfig());
  const [provider, setProvider] = useState<string | null>(null);
  return (
    <>
      <Probe
        tier={config.model_tier}
        overrides={config.model_overrides}
        provider={provider}
      />
      <ConfigureStep
        config={config}
        title=""
        titlePlaceholder="Untitled"
        recommended={null}
        onConfigChange={(patch) => setConfig((previous) => ({ ...previous, ...patch }))}
        onProviderChange={setProvider}
        onTitleChange={() => {}}
        onContinue={() => {}}
        onBack={() => {}}
      />
    </>
  );
}

describe("the tier and the matrix together", () => {
  async function renderWizard() {
    stubCapabilities(WIZARD_CAPABILITIES);
    render(<Wizard />);
    await screen.findByRole("table");
    return userEvent.setup();
  }

  it("offers the tiers the payload publishes", async () => {
    await renderWizard();

    const options = within(screen.getByLabelText("Model tier"))
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value);
    expect(options).toEqual(Object.keys(tablesOf(WIZARD_CAPABILITIES)));
  });

  it("refills the matrix and drops the overrides when the tier changes", async () => {
    const user = await renderWizard();
    const picked = otherModelThan(MOVED.model);

    await user.selectOptions(modelSelect(MOVED.role), picked.id);
    expect(sentOverrides()).toEqual({ [MOVED.role]: { model: picked.id } });

    await user.selectOptions(screen.getByLabelText("Model tier"), TARGET_TIER);
    await waitFor(() => expect(sentTier()).toBe(TARGET_TIER));

    // Refilled from the new tier's own table, not patched with a difference
    // measured against a table that no longer applies.
    expect(sentOverrides()).toEqual({});
    for (const entry of runRoles(tablesOf(WIZARD_CAPABILITIES)[TARGET_TIER])) {
      expect(modelSelect(entry.role)).toHaveValue(entry.model);
      expect(effortSelect(entry.role)).toHaveValue(entry.effort);
    }
  });
});

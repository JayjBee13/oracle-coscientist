import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ModelsBrief } from "../api/types";
import { describeRole, humanize } from "../lib/status";
import { makeCapabilities, tablesOf } from "../test/fixtures";
import { WORKFLOW_STEPS, WorkflowDiagram } from "./WorkflowDiagram";

/**
 * The diagram's whole promise is that it describes the engine rather than a copy
 * of the engine, so nothing here names a model, a tier or an effort: every
 * expectation is derived from the same payload the component is handed. If the
 * engine moves a role to a different model, the fixture changes and not one
 * assertion below does — and if the component starts hardcoding a model id, the
 * tier test fails.
 */

const MODELS: ModelsBrief = makeCapabilities().models;
const TABLES = tablesOf(makeCapabilities());
const FLOOR_TIER = MODELS.default_tier;
const TOP_TIER = Object.keys(TABLES).find((name) => name !== FLOOR_TIER)!;

/** The steps that call a model, and the payload row that describes each. */
const MODEL_STEPS = WORKFLOW_STEPS.filter((step) => step.role !== null);
const CODE_STEPS = WORKFLOW_STEPS.filter((step) => step.role === null);

function rowFor(tier: string, role: string) {
  const row = TABLES[tier].find((entry) => entry.role === role);
  if (!row) throw new Error(`fixture has no row for ${role}`);
  return row;
}

function label(model: string): string {
  const known = MODELS.catalog.find((choice) => choice.id === model);
  return known ? known.label : model;
}

function titleOf(step: (typeof WORKFLOW_STEPS)[number]): string {
  return step.title ?? describeRole(step.role!);
}

/** The ordered list the diagram is made of, in either variant. */
function stepList(): HTMLElement {
  return screen.getByRole("list", { name: /steps of a research run/i });
}

function steps(): HTMLElement[] {
  return within(stepList()).getAllByRole("listitem");
}

describe("WorkflowDiagram — the loop", () => {
  it("draws one step per role in the payload, in engine order", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    const items = steps();
    expect(items).toHaveLength(WORKFLOW_STEPS.length);
    WORKFLOW_STEPS.forEach((step, index) => {
      expect(within(items[index]).getByText(titleOf(step))).toBeInTheDocument();
    });
  });

  it("names every role the loop uses, translated rather than raw", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    for (const step of MODEL_STEPS) {
      const item = steps()[WORKFLOW_STEPS.indexOf(step)];
      const heading = within(item).getByRole("heading");
      // The rendered title is the translation, not the raw enum. Asserting
      // `queryByText(role)` could never fail — no element's whole text equals a
      // key like `meta_review` — so this asserts the positive instead.
      expect(heading).toHaveTextContent(describeRole(step.role!));
      expect(heading.textContent).not.toBe(step.role);
      expect(heading.textContent).not.toMatch(/_/);
    }
  });

  it("separates the report from the round it is not part of", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    const items = steps();
    const last = items[items.length - 1];
    expect(within(last).getByText(describeRole("overview"))).toBeInTheDocument();
    expect(within(last).getByText(/once, at the end/i)).toBeInTheDocument();
    // The promise the reserve exists for.
    expect(within(last).getByText(/reserve/i)).toBeInTheDocument();
  });

  it("shows the collapse check as code, with the call it can trigger hung off it", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    expect(CODE_STEPS).not.toHaveLength(0);
    for (const step of CODE_STEPS) {
      const item = steps()[WORKFLOW_STEPS.indexOf(step)];
      expect(within(item).getByText(/no model call/i)).toBeInTheDocument();

      // The conditional call is the branch, and it is labelled as conditional
      // with the model the payload gives that role.
      if (!step.branch) continue;
      const branchRow = rowFor(FLOOR_TIER, step.branch.role);
      expect(within(item).getByText(/if it fires/i)).toBeInTheDocument();
      expect(within(item).getByText(label(branchRow.model))).toBeInTheDocument();
    }
  });

  it("says which steps fan out and which run alone", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    const sharded = WORKFLOW_STEPS.find((step) => step.fan === "sharded")!;
    const parallel = WORKFLOW_STEPS.find((step) => step.fan === "parallel")!;
    const single = WORKFLOW_STEPS.find((step) => step.fan === "single")!;

    expect(
      within(steps()[WORKFLOW_STEPS.indexOf(sharded)]).getByText(/sharded/i),
    ).toBeInTheDocument();
    expect(
      within(steps()[WORKFLOW_STEPS.indexOf(parallel)]).getByText(/in parallel/i),
    ).toBeInTheDocument();
    expect(
      within(steps()[WORKFLOW_STEPS.indexOf(single)]).getByText(/on its own/i),
    ).toBeInTheDocument();
  });
});

describe("WorkflowDiagram — models come from the payload", () => {
  it("prints the model, the effort and the grounding the payload gives each step", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    for (const step of MODEL_STEPS) {
      const row = rowFor(FLOOR_TIER, step.role!);
      const item = steps()[WORKFLOW_STEPS.indexOf(step)];
      expect(within(item).getByText(label(row.model))).toBeInTheDocument();
      expect(
        within(item).getByText(`${humanize(row.effort)} effort`),
      ).toBeInTheDocument();
      expect(
        within(item).getByText(row.grounded ? /searches the web/i : /no web search/i),
      ).toBeInTheDocument();
    }
  });

  it("uses the engine's own note as the recommendation for each step", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    for (const step of MODEL_STEPS) {
      const row = rowFor(FLOOR_TIER, step.role!);
      const item = steps()[WORKFLOW_STEPS.indexOf(step)];
      expect(within(item).getByText(row.note)).toBeInTheDocument();
    }
  });

  it("carries the escalations no table can express", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    for (const note of MODELS.notes) {
      expect(screen.getByText(note)).toBeInTheDocument();
    }
  });

  it("changes every model when the tier changes, and only where the tier does", () => {
    const { rerender } = render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    const before = MODEL_STEPS.map((step) => {
      const item = steps()[WORKFLOW_STEPS.indexOf(step)];
      return within(item).getByText(label(rowFor(FLOOR_TIER, step.role!).model));
    });
    expect(before).not.toHaveLength(0);

    rerender(<WorkflowDiagram models={MODELS} tier={TOP_TIER} />);

    // At least one step must actually differ, or the test proves nothing.
    const moved = MODEL_STEPS.filter(
      (step) =>
        rowFor(FLOOR_TIER, step.role!).model !== rowFor(TOP_TIER, step.role!).model,
    );
    expect(moved).not.toHaveLength(0);

    for (const step of MODEL_STEPS) {
      const item = steps()[WORKFLOW_STEPS.indexOf(step)];
      expect(
        within(item).getByText(label(rowFor(TOP_TIER, step.role!).model)),
      ).toBeInTheDocument();
    }
  });

  it("falls back to the payload's default tier when none is given", () => {
    render(<WorkflowDiagram models={MODELS} />);

    const generation = WORKFLOW_STEPS.find((step) => step.role === "generation")!;
    const item = steps()[WORKFLOW_STEPS.indexOf(generation)];
    expect(
      within(item).getByText(label(rowFor(MODELS.default_tier, "generation").model)),
    ).toBeInTheDocument();
  });

  it("falls back, and says so, when the run's stored tier is no longer offered", () => {
    // Reachable: the wizard's clone path copies `model_tier` off a stored run,
    // and a tier that is merely *unknown* used to slip past `??` and draw every
    // step with no model, no effort and no note — silently, and with the spoken
    // alternative dropping the sentence that names the model.
    render(<WorkflowDiagram models={MODELS} tier="a-tier-that-was-retired" />);

    const generation = WORKFLOW_STEPS.find((step) => step.role === "generation")!;
    const item = steps()[WORKFLOW_STEPS.indexOf(generation)];
    expect(
      within(item).getByText(label(rowFor(MODELS.default_tier, "generation").model)),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/no longer offered/i);
  });

  it("lays the run's own per-role choices over the tier's table", () => {
    // The Confirm step's table and this diagram are two views of one run. When
    // they were built from different sources the strip said one model and the
    // table three inches below said another, for the same step.
    const moved = MODEL_STEPS[0];
    const picked = MODELS.catalog.find(
      (choice) => choice.id !== rowFor(FLOOR_TIER, moved.role!).model,
    )!;
    const effort = MODELS.efforts.find(
      (value) => value !== rowFor(FLOOR_TIER, moved.role!).effort,
    )!;
    render(
      <WorkflowDiagram
        models={MODELS}
        tier={FLOOR_TIER}
        overrides={{ [moved.role!]: { model: picked.id, effort } }}
      />,
    );

    const item = steps()[WORKFLOW_STEPS.indexOf(moved)];
    expect(within(item).getByText(picked.label)).toBeInTheDocument();
    expect(within(item).getByText(`${humanize(effort)} effort`)).toBeInTheDocument();
    // Every other step is untouched.
    for (const step of MODEL_STEPS.slice(1)) {
      const other = steps()[WORKFLOW_STEPS.indexOf(step)];
      expect(
        within(other).getByText(label(rowFor(FLOOR_TIER, step.role!).model)),
      ).toBeInTheDocument();
    }
  });

  it("still teaches the shape when the payload has not arrived", () => {
    render(<WorkflowDiagram models={null} />);

    expect(steps()).toHaveLength(WORKFLOW_STEPS.length);
    for (const choice of MODELS.catalog) {
      expect(screen.queryByText(choice.label)).toBeNull();
    }
  });
});

describe("WorkflowDiagram — the text alternative", () => {
  it("names itself and lists every step, in the full diagram", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} />);

    expect(screen.getByText(/text alternative/i)).toBeInTheDocument();
    const list = stepList();
    expect(within(list).getAllByRole("listitem")).toHaveLength(WORKFLOW_STEPS.length);
    for (const step of WORKFLOW_STEPS) {
      expect(within(list).getByText(step.what)).toBeInTheDocument();
    }
  });

  it("speaks what the compact strip drops", () => {
    render(<WorkflowDiagram models={MODELS} tier={FLOOR_TIER} variant="compact" />);

    expect(screen.getByText(/text alternative/i)).toBeInTheDocument();
    const items = steps();
    expect(items).toHaveLength(WORKFLOW_STEPS.length);

    WORKFLOW_STEPS.forEach((step, index) => {
      const spoken = items[index].textContent ?? "";
      expect(spoken).toContain(step.what);
      if (step.role) {
        const row = rowFor(FLOOR_TIER, step.role);
        expect(spoken).toContain(label(row.model));
        expect(spoken).toContain(humanize(row.effort));
      } else {
        expect(spoken).toMatch(/no model call/i);
      }
    });
  });
});

describe("WorkflowDiagram — compact, on the way to launch", () => {
  it("shows the model of each step and where the loop ends", () => {
    render(<WorkflowDiagram models={MODELS} tier={TOP_TIER} variant="compact" />);

    const generation = WORKFLOW_STEPS.find((step) => step.role === "generation")!;
    const item = steps()[WORKFLOW_STEPS.indexOf(generation)];
    expect(
      within(item).getByText(label(rowFor(TOP_TIER, "generation").model)),
    ).toBeInTheDocument();
    expect(screen.getByText(/repeat every round/i)).toBeInTheDocument();
  });

  it("names no model at all for a demo run", () => {
    render(<WorkflowDiagram models={MODELS} tier={TOP_TIER} variant="compact" demo />);

    expect(steps()).toHaveLength(WORKFLOW_STEPS.length);
    for (const choice of MODELS.catalog) {
      expect(screen.queryByText(choice.label)).toBeNull();
    }
    expect(screen.getAllByText("Demo").length).toBeGreaterThan(0);
  });

  it("lets a keyboard reach the strip it scrolls", () => {
    render(<WorkflowDiagram models={MODELS} tier={TOP_TIER} variant="compact" />);

    // A scroll container with nothing focusable in it cannot be reached at all
    // (WCAG 2.1.1), and the tile that gets cut off is the report.
    const scroller = screen.getByRole("group", { name: /steps of a research run/i });
    expect(scroller).toHaveAttribute("tabindex", "0");
  });
});

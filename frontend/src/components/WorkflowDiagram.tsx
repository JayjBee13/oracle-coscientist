import type { ReactNode } from "react";

import type { ModelsPayload, RoleModelBrief } from "../api/types";
import { applyOverrides, modelLabel, resolveTier } from "../lib/models";
import type { RoleChoice } from "../lib/models";
import { describeEffort, describeModelTier, describeRole } from "../lib/status";
import "../styles/diagram.css";
import { AdaptiveWorkflowDiagram } from "./AdaptiveWorkflowDiagram";

/**
 * What actually happens during a run, drawn.
 *
 * A person who has never read `engine/orchestrator.py` should be able to look at
 * this once and know four things: that the middle of a run is a *loop*, that
 * three of its steps fan out and therefore cost wall clock rather than patience,
 * that one of them is arithmetic rather than a model, and that the report at the
 * end is written once — and is written even if you stop the run early.
 *
 * Two rules govern the code:
 *
 *   1. **The shape is ours; the models are the engine's.** The step list, the
 *      order and the fan-out are structural facts about the orchestrator and are
 *      written down here. Which model each step runs on, at what effort, whether
 *      it may search the web, and *why* it is set that way all come from
 *      `GET /api/capabilities` — the engine's own role table, served rather than
 *      restated. Nothing in this file names a model, a tier or an effort.
 *   2. **The diagram is an ordered list.** The boxes, spine, fan glyphs and the
 *      return arrow are decoration over `<ol>`/`<li>`; a screen reader gets the
 *      same seven-then-one story in the same order, and the compact variant
 *      carries the detail it drops visually in a hidden span. There is no second
 *      copy of the content to fall out of date.
 *
 * Cost is deliberately not a dimension here. Time and quality are: what a step
 * is for, how hard it is thinking, and whether it happens all at once.
 */

/** How a step spends its wall clock. */
type Fan = "single" | "parallel" | "sharded";

export type WorkflowStepSpec = {
  key: string;
  /** The engine role whose row in the payload describes this step; null = pure code. */
  role: string | null;
  /** Used only when there is no role to name the step for us. */
  title?: string;
  /** One plain sentence: what this step is for. */
  what: string;
  fan: Fan;
  /** How the fan-out is cut, when it fans out. */
  fanNote?: string;
  /** Inside the repeating round, or once at the end. */
  phase: "round" | "final";
  /** A conditional side-call hanging off this step. */
  branch?: { role: string; what: string };
  /** An extra thing worth knowing, shown under the engine's own note. */
  aside?: string;
};

/**
 * One round of the loop, then the report. Mirrors the step order in
 * `engine/orchestrator.py`; if that order changes, this list is what moves.
 *
 * Exported so the tests can walk the same list the component draws rather than
 * restating it — a test with its own copy of the step order cannot catch a step
 * being dropped. That costs this file fast refresh, which is the right trade for
 * a component with no state to lose.
 */
// eslint-disable-next-line react-refresh/only-export-components
export const WORKFLOW_STEPS: readonly WorkflowStepSpec[] = [
  {
    key: "generation",
    role: "generation",
    what: "Drafts new hypotheses from the question, the context you gave it, and the guidance the last round left behind.",
    fan: "sharded",
    fanNote:
      "Split into one call per three new ideas, each sent down a different line of enquiry.",
    phase: "round",
  },
  {
    key: "reflection",
    role: "reflection",
    what: "Reviews every new hypothesis for correctness, novelty and testability. Anything it rejects never reaches the tournament.",
    fan: "parallel",
    fanNote: "One call per new hypothesis, all in flight at once.",
    phase: "round",
  },
  {
    key: "proximity",
    role: "proximity",
    what: "Groups near-duplicate ideas together, so the tournament does not spend its matches comparing rephrasings.",
    fan: "single",
    phase: "round",
  },
  {
    key: "collapse",
    role: null,
    title: "Collapse check",
    what: "Measures whether the ideas have converged on a single shape. This step is arithmetic in the supervisor — no model is called.",
    fan: "single",
    phase: "round",
    branch: {
      role: "cartographer",
      what: "Only if collapse fires: one extra call grafts a deliberately distant idea into the next round's generation.",
    },
  },
  {
    key: "ranking",
    role: "ranking",
    what: "The tournament. Ideas are paired off, a judge reads both and picks a winner, and each side's Elo moves.",
    fan: "parallel",
    fanNote: "Matches are judged in parallel, not one after another.",
    phase: "round",
  },
  {
    key: "evolution",
    role: "evolution",
    what: "Takes the strongest surviving ideas and recombines, sharpens or simplifies them into new ones.",
    fan: "single",
    phase: "round",
  },
  {
    key: "meta_review",
    role: "meta_review",
    what: "Reads the round's reviews and verdicts and writes the guidance that the next round is generated against.",
    fan: "single",
    phase: "round",
  },
  {
    key: "overview",
    role: "overview",
    what: "Writes the research report — the artefact you actually read — from whatever the run has by then.",
    fan: "single",
    phase: "final",
    aside:
      "Two calls are held back in reserve, so a run you stop early, or one that runs out of budget, still writes this.",
  },
];

const ROUND_STEPS = WORKFLOW_STEPS.filter((step) => step.phase === "round");

/**
 * How many steps one round has. Exported so prose elsewhere can say the number
 * instead of typing a word that goes wrong the day a step is added.
 */
// eslint-disable-next-line react-refresh/only-export-components
export const ROUND_STEP_COUNT = ROUND_STEPS.length;

/** What one step resolves to once the payload is folded in. */
type ResolvedStep = {
  spec: WorkflowStepSpec;
  /** 1-based within the round; 0 for the report, which is not part of the cycle. */
  ordinal: number;
  title: string;
  row: RoleModelBrief | null;
  /** The catalog's name for the model, or a shortened id if it has been retired. */
  model: string | null;
  effort: string | null;
  grounded: boolean;
  /** The engine's own reason this step is configured the way it is. */
  note: string | null;
  branch: {
    title: string;
    what: string;
    model: string | null;
    effort: string | null;
  } | null;
};

export function WorkflowDiagram({
  models,
  provider = null,
  tier,
  overrides,
  variant = "full",
  demo = false,
  workflow = "tournament",
}: {
  /** The `models` block of `GET /api/capabilities`, or null while it loads. */
  models: ModelsPayload | null;
  /**
   * Whose tables to read. The Confirm step draws this diagram three inches above
   * the same table in prose, and with two providers `(tier, overrides)` no longer
   * identifies one — so the provider comes with it, or the two disagree again.
   */
  provider?: string | null;
  /** Which tier to describe. Falls back to the payload's own default. */
  tier?: string | null;
  /** `config.model_overrides`: the cells this run moved off the tier's table. */
  overrides?: Readonly<Record<string, RoleChoice>> | null;
  variant?: "full" | "compact";
  /** A demo run calls no models at all, so it must not be shown any. */
  demo?: boolean;
  workflow?: "adaptive" | "tournament";
}) {
  // Validated, not merely defaulted: `??` lets a tier string that is not a key
  // of `tiers` through, and every step then draws with no model, no effort and
  // no note — silently, and with the spoken alternative dropping the sentence
  // that names the model. A stored config from before a tier was retired is
  // exactly that case, so it falls back to the default and says so.
  //
  // The provider is applied by swapping in its tables before that resolution
  // runs, so the retired-tier reporting is one code path rather than two.
  const resolution = resolveTier(models, provider, tier);
  const rows = applyOverrides(resolution.rows, overrides);
  const catalog = models?.catalog ?? [];

  const steps: ResolvedStep[] = WORKFLOW_STEPS.map((spec) => {
    const row = spec.role
      ? (rows.find((entry) => entry.role === spec.role) ?? null)
      : null;
    const branchRole = spec.branch?.role ?? null;
    const branchRow = branchRole
      ? (rows.find((entry) => entry.role === branchRole) ?? null)
      : null;
    return {
      spec,
      ordinal: spec.phase === "round" ? ROUND_STEPS.indexOf(spec) + 1 : 0,
      title: spec.title ?? (spec.role ? describeRole(spec.role) : ""),
      row,
      model: demo ? null : modelLabel(row?.model, catalog),
      effort: demo ? null : row ? describeEffort(row.effort) : null,
      grounded: Boolean(row?.grounded),
      note: row?.note ?? null,
      branch: spec.branch
        ? {
            title: describeRole(spec.branch.role),
            what: spec.branch.what,
            model: demo ? null : modelLabel(branchRow?.model, catalog),
            effort: demo ? null : branchRow ? describeEffort(branchRow.effort) : null,
          }
        : null,
    };
  });

  const retired = resolution.missing ? (
    <p className="wf__retired" role="status">
      This run’s stored tier ({describeModelTier(resolution.missing).label}) is no longer
      offered. The steps below show{" "}
      <strong>{describeModelTier(resolution.tier ?? "").label}</strong> instead.
    </p>
  ) : null;

  if (workflow === "adaptive")
    return (
      <AdaptiveWorkflowDiagram
        models={models}
        provider={provider}
        tier={tier}
        overrides={overrides}
        demo={demo}
        variant={variant}
      />
    );

  return variant === "compact" ? (
    <CompactDiagram steps={steps} demo={demo} retired={retired} />
  ) : (
    <FullDiagram
      steps={steps}
      demo={demo}
      notes={models?.notes ?? []}
      retired={retired}
    />
  );
}

/* --- The full diagram ------------------------------------------------------
   A spine with the seven round steps on it, a return arrow at the bottom, and
   the report hanging off the end of the loop rather than inside it.
   ------------------------------------------------------------------------- */

function FullDiagram({
  steps,
  demo,
  notes,
  retired,
}: {
  steps: readonly ResolvedStep[];
  demo: boolean;
  notes: readonly string[];
  retired: ReactNode;
}) {
  return (
    <div className="wf" data-variant="full">
      {retired}
      <p className="visually-hidden">
        Text alternative for the workflow diagram. A run repeats a loop of{" "}
        {ROUND_STEPS.length} steps once per round, then writes one report at the end. The
        ordered list that follows gives every step in order, with what it does, the model
        it runs on, its thinking effort, whether it searches the web, whether it runs in
        parallel, and why it is configured that way.
      </p>

      <p className="wf__phase">
        <span className="wf__phase-key">One round</span>
        <span className="wf__phase-text">
          Repeats until the round target is met or the call budget runs out
        </span>
      </p>

      <ol className="wf__steps" aria-label="The steps of a research run, in order">
        {steps.map((step) => (
          <li
            key={step.spec.key}
            className="wf__step"
            data-phase={step.spec.phase}
            data-kind={step.spec.role ? "model" : "code"}
          >
            {step.spec.phase === "final" ? (
              <p className="wf__phase wf__phase--final">
                <span className="wf__phase-key">Once, at the end</span>
                <span className="wf__phase-text">
                  After the last round, or when you stop
                </span>
              </p>
            ) : null}

            <span className="wf__node" aria-hidden="true">
              {step.ordinal > 0 ? step.ordinal : <EndGlyph />}
            </span>

            <div className="wf__card">
              <div className="wf__head">
                <h3 className="wf__title">{step.title}</h3>
                <span className="wf__marks">
                  {step.spec.role ? (
                    <>
                      {step.model ? (
                        <span className="chip chip--quiet wf__model" data-tone="accent">
                          {step.model}
                        </span>
                      ) : null}
                      {step.effort ? (
                        <span className="chip chip--quiet" data-tone="neutral">
                          {step.effort} effort
                        </span>
                      ) : null}
                      {demo ? (
                        <span className="chip chip--quiet" data-tone="info">
                          Demo — no model call
                        </span>
                      ) : null}
                    </>
                  ) : (
                    <span className="chip chip--quiet" data-tone="neutral">
                      <CodeIcon />
                      No model call
                    </span>
                  )}
                </span>
              </div>

              <p className="wf__what">{step.spec.what}</p>

              <div className="wf__flags">
                <Flag on={step.spec.fan !== "single"} icon={<FanIcon />}>
                  {step.spec.fan === "single"
                    ? "One call, on its own"
                    : step.spec.fan === "sharded"
                      ? "Sharded into parallel calls"
                      : "Runs in parallel"}
                </Flag>
                {step.row && !demo ? (
                  <Flag on={step.grounded} icon={<GlobeIcon />}>
                    {step.grounded ? "Searches the web" : "No web search"}
                  </Flag>
                ) : null}
              </div>

              {step.spec.fanNote ? (
                <div className="wf__fan">
                  <FanDiagram />
                  <span className="wf__fan-text">{step.spec.fanNote}</span>
                </div>
              ) : null}

              {step.note ? (
                <p className="wf__note">
                  <span className="label">Why it is set this way</span>
                  {step.note}
                </p>
              ) : null}

              {step.spec.aside ? <p className="wf__aside">{step.spec.aside}</p> : null}

              {step.branch ? (
                <div className="wf__branch">
                  <span className="wf__branch-head">
                    <span className="wf__branch-title">
                      If it fires · {step.branch.title}
                    </span>
                    {step.branch.model ? (
                      <span className="chip chip--quiet" data-tone="accent">
                        {step.branch.model}
                      </span>
                    ) : null}
                    {step.branch.effort ? (
                      <span className="chip chip--quiet" data-tone="neutral">
                        {step.branch.effort} effort
                      </span>
                    ) : null}
                  </span>
                  <p className="wf__what">{step.spec.branch?.what}</p>
                </div>
              ) : null}
            </div>

            {step.ordinal === ROUND_STEPS.length ? (
              <div className="wf__return">
                <ReturnArrow />
                <span className="wf__return-text">
                  The guidance goes back to step 1 and the next round starts. When the
                  last round is done, the run drops out of the loop.
                </span>
              </div>
            ) : null}
          </li>
        ))}
      </ol>

      {notes.length > 0 ? (
        <section className="wf__escalations">
          <h3 className="label">Where the engine thinks harder than the table says</h3>
          <ul>
            {notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

/* --- The compact strip -----------------------------------------------------
   For the Confirm step, where the job is reassurance rather than teaching: this
   is the shape of the thing you are about to start, in one glance. Everything
   the tiles drop is still spoken.
   ------------------------------------------------------------------------- */

function CompactDiagram({
  steps,
  demo,
  retired,
}: {
  steps: readonly ResolvedStep[];
  demo: boolean;
  retired: ReactNode;
}) {
  return (
    <div className="wf wf--compact" data-variant="compact">
      {retired}
      <p className="visually-hidden">
        Text alternative for the workflow diagram. Every round runs these{" "}
        {ROUND_STEPS.length} steps in order, then one report is written at the end.
      </p>

      {/* The strip wraps rather than clipping at narrow widths, and is still
          focusable so that a keyboard alone can scroll it on the widths where
          it does not: a scroll container with nothing focusable inside it
          cannot be reached at all (WCAG 2.1.1). */}
      <div
        className="scroll-x wf__scroller"
        tabIndex={0}
        role="group"
        aria-label="The steps of a research run"
      >
        <ol className="wf__strip" aria-label="The steps of a research run, in order">
          {steps.map((step) => (
            <li
              key={step.spec.key}
              className="wf__tile"
              data-phase={step.spec.phase}
              data-kind={step.spec.role ? "model" : "code"}
            >
              <span className="wf__pip" aria-hidden="true">
                {step.ordinal > 0 ? step.ordinal : <EndGlyph />}
              </span>
              <span className="wf__tile-name">{step.title}</span>
              <span className="wf__tile-model">{tileModel(step, demo)}</span>
              <span className="wf__tile-flags" aria-hidden="true">
                {step.spec.fan !== "single" ? <FanIcon /> : null}
                {step.grounded && !demo ? <GlobeIcon /> : null}
                {step.spec.role ? null : <CodeIcon />}
              </span>
              <span className="visually-hidden">{spoken(step, demo)}</span>
            </li>
          ))}
        </ol>
      </div>

      <p className="wf__caption">
        Steps 1–{ROUND_STEPS.length} repeat every round; the report is written once at the
        end, and is written even if you stop early.
      </p>

      {/* The tiles are too small for words, so the three marks they do use are
          spelled out once, here, rather than left to be guessed at. */}
      <p className="wf__legend" aria-hidden="true">
        <span>
          <FanIcon /> fans out
        </span>
        {demo ? null : (
          <span>
            <GlobeIcon /> searches the web
          </span>
        )}
        <span>
          <CodeIcon /> no model call
        </span>
      </p>
    </div>
  );
}

/**
 * The one line under a compact tile. A step that calls no model says so; a demo
 * run says so; and a step whose model has not loaded yet says nothing rather
 * than guessing.
 */
function tileModel(step: ResolvedStep, demo: boolean): string {
  if (!step.spec.role) return "No model";
  if (demo) return "Demo";
  return step.model ?? "";
}

/** Everything a compact tile shows visually, plus everything it does not. */
function spoken(step: ResolvedStep, demo: boolean): string {
  const parts = [step.spec.what];
  if (!step.spec.role) {
    parts.push("No model call — the supervisor computes this in code.");
  } else if (demo) {
    parts.push("A demo run makes no model calls.");
  } else if (step.model) {
    parts.push(`Runs on ${step.model}${step.effort ? ` at ${step.effort} effort` : ""}.`);
    parts.push(step.grounded ? "Searches the web." : "Does not search the web.");
  }
  if (step.spec.fan === "sharded") parts.push("Sharded into parallel calls.");
  if (step.spec.fan === "parallel") parts.push("Runs in parallel.");
  return parts.join(" ");
}

/* --- Parts ---------------------------------------------------------------- */

function Flag({
  on,
  icon,
  children,
}: {
  on: boolean;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <span className="wf__flag" data-on={on ? "true" : "false"}>
      {on ? icon : null}
      {children}
    </span>
  );
}

/* --- Hand-drawn marks ------------------------------------------------------
   All decorative: every one of them is aria-hidden and every one of them
   restates something the text beside it already says. Stroke colour is
   inherited so light and dark need no second set of values.
   ------------------------------------------------------------------------- */

/** One call becoming three: the shape of a sharded or parallel step. */
function FanDiagram() {
  return (
    <svg
      className="wf__fan-svg"
      viewBox="0 0 68 28"
      role="presentation"
      aria-hidden="true"
    >
      <circle cx="5" cy="14" r="3.5" className="wf__mark-fill" />
      <path
        d="M10 14 H26 M26 14 C34 14 34 5 44 5 M26 14 H44 M26 14 C34 14 34 23 44 23"
        className="wf__mark-line"
      />
      <circle cx="48" cy="5" r="3.5" className="wf__mark-fill" />
      <circle cx="48" cy="14" r="3.5" className="wf__mark-fill" />
      <circle cx="48" cy="23" r="3.5" className="wf__mark-fill" />
    </svg>
  );
}

/** The loop closing: down off the last step, round, and back up to the first. */
function ReturnArrow() {
  return (
    <svg
      className="wf__return-svg"
      viewBox="0 0 28 46"
      role="presentation"
      aria-hidden="true"
    >
      <path
        d="M14 0 V30 C14 40 4 40 4 30 V12"
        className="wf__mark-line wf__mark-line--accent"
      />
      <path d="M4 2 L0 10 L8 10 Z" className="wf__mark-fill wf__mark-fill--accent" />
    </svg>
  );
}

function FanIcon() {
  return (
    <svg className="wf__icon" viewBox="0 0 16 16" role="presentation" aria-hidden="true">
      <path d="M2 8 H7 M7 8 C10 8 10 3 14 3 M7 8 H14 M7 8 C10 8 10 13 14 13" />
    </svg>
  );
}

function GlobeIcon() {
  return (
    <svg className="wf__icon" viewBox="0 0 16 16" role="presentation" aria-hidden="true">
      <circle cx="8" cy="8" r="6" />
      <path d="M2 8 H14 M8 2 C5 5 5 11 8 14 M8 2 C11 5 11 11 8 14" />
    </svg>
  );
}

function CodeIcon() {
  return (
    <svg className="wf__icon" viewBox="0 0 16 16" role="presentation" aria-hidden="true">
      <path d="M6 4 L2 8 L6 12 M10 4 L14 8 L10 12" />
    </svg>
  );
}

/** The report: the loop's output, drawn as a page rather than a step number. */
function EndGlyph() {
  return (
    <svg
      className="wf__icon wf__icon--node"
      viewBox="0 0 16 16"
      role="presentation"
      aria-hidden="true"
    >
      <path d="M4 2 H10 L13 5 V14 H4 Z M10 2 V5 H13 M6 8 H11 M6 11 H11" />
    </svg>
  );
}

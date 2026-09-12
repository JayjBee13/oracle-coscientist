/**
 * Everything the wizard knows, and where it survives a reload.
 *
 * Two places hold state, on purpose:
 *
 *   - **the URL** (`/new?workshop=<id>&step=<name>`) is the address of a step,
 *     so Back works, a reload lands where you were, and a link is shareable;
 *   - **localStorage** holds the draft — the question, the edited prompt, the
 *     config, the context documents — because the URL cannot carry a prompt
 *     someone has spent ten minutes wording.
 *
 * The UX review found the old notebook lost everything on reload. A draft here
 * is written on every change and cleared only when a run actually launches.
 */

import type {
  ContextDocInput,
  GroundingDepth,
  LaunchHarness,
  RecommendedSettings,
  RunConfig,
} from "../../api/types";
import { DEFAULT_MODEL_TIER, getPreset, presetConfig } from "../../lib/estimates";

export const WIZARD_STEPS = [
  "intent",
  "options",
  "prompt",
  "configure",
  "confirm",
] as const;
export type WizardStep = (typeof WIZARD_STEPS)[number];

export const STEP_LABELS: Record<WizardStep, string> = {
  intent: "Question",
  options: "Directions",
  prompt: "Prompt",
  configure: "Settings",
  confirm: "Confirm",
};

export function isWizardStep(value: string | null | undefined): value is WizardStep {
  return value != null && (WIZARD_STEPS as readonly string[]).includes(value);
}

export function stepIndex(step: WizardStep): number {
  return WIZARD_STEPS.indexOf(step);
}

/* --- The draft ------------------------------------------------------------ */

/** Legacy single-user key. It is deliberately never read in gateway mode. */
export const DRAFT_KEY = "coscientist.wizard.v1";
const SCOPED_DRAFT_PREFIX = "coscientist.wizard.v2";

export function draftKey(username: string): string {
  return `${SCOPED_DRAFT_PREFIX}.${encodeURIComponent(username)}`;
}

export type WizardDraft = {
  version: 1;
  /** The workshop this draft belongs to; null on the "write it myself" path. */
  workshopId: string | null;
  question: string;
  /** Which option seeded the prompt. `null` when there was no workshop. */
  optionId: string | null;
  prompt: string;
  /** The option's untouched wording, so "revert my edits" has something to go to. */
  promptSource: string;
  title: string;
  config: RunConfig;
  /** What the chosen option recommended, kept for "reset to recommended". */
  recommended: RecommendedSettings | null;
  contextDocs: ContextDocInput[];
  notify: boolean;
  /** The run this one was cloned from (`/new?from=`), reported to the backend. */
  fromRun: string | null;
  /** True once `POST /workshops/{id}/choose` has been accepted for this prompt. */
  chosen: boolean;
};

/** A run's settings before anything has been chosen: the Standard preset. */
export function defaultConfig(): RunConfig {
  return {
    ...presetConfig(getPreset("standard")),
    graft: { enabled: false, quorum_k: 2, window: 3, cooldown: 2 },
    model_tier: DEFAULT_MODEL_TIER,
    // Per-role model/effort choices: only the cells the role matrix was moved
    // off the tier's table. Empty means "whatever the tier resolves to", which
    // is what a run that accepts the defaults sends.
    model_overrides: {},
    // Which vendor's table the tier fills from. `null` is "whatever the stored
    // default resolves to", which is what a run nobody has set a vendor on
    // means — and what a single-provider backend always resolves to.
    provider: null,
    // Both optional ceilings are off by default, as they are on the backend:
    // `budget_calls` is the governor that actually stops a run. A cost ceiling
    // in particular is never set for you — these calls run on a subscription,
    // so a dollar figure is telemetry and stopping a run on one would be
    // stopping it on a number nobody is paying.
    budget_usd: null,
    wall_clock_minutes: null,
    runner: "claude",
  };
}

export function blankDraft(): WizardDraft {
  return {
    version: 1,
    workshopId: null,
    question: "",
    optionId: null,
    prompt: "",
    promptSource: "",
    title: "",
    config: defaultConfig(),
    recommended: null,
    contextDocs: [],
    notify: false,
    fromRun: null,
    chosen: false,
  };
}

/**
 * The wizard's starting point, taken from the stored system default.
 *
 * The top bar sets what a new run uses; this is where "a new run" reads it. Only
 * a draft nobody has touched inherits — someone who has already moved a step and
 * come back to the tab is not asking for their work to be replaced by whatever
 * the default happens to be now.
 */
export function inheritDefault(
  draft: WizardDraft,
  stored: Pick<RunConfig, "provider" | "model_tier" | "model_overrides"> | null,
): WizardDraft {
  if (!stored) return draft;
  // A clone is never untouched: it is an earlier run's setup, and reproducing that
  // setup is the entire point of "Run again". The two arrive over different
  // requests — `GET /runs/{id}/detail` and `GET /settings/models` — so without
  // this the later response won. A clone of a run at one tier, on a default at
  // another, silently launched at whichever of the two the network happened to
  // answer second, which is a difference nothing on screen could explain.
  if (draft.fromRun) return draft;
  // Untouched means all three: a provider nobody has picked, and a table nobody
  // has moved. Any one of them set is somebody's work, and inheriting over it is
  // the wizard deciding it knows better than the person using it.
  if (draft.config.provider !== null) return draft;
  if (Object.keys(draft.config.model_overrides).length > 0) return draft;
  return {
    ...draft,
    config: {
      ...draft.config,
      provider: stored.provider,
      model_tier: stored.model_tier,
      model_overrides: { ...stored.model_overrides },
    },
  };
}

/* --- Persistence ----------------------------------------------------------
   Storage throws in private windows and when the quota is full; a draft is a
   convenience, never a precondition, so every path here degrades quietly.
   ------------------------------------------------------------------------- */

export function readDraft(username: string): WizardDraft | null {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(draftKey(username));
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const draft = parsed as Partial<WizardDraft>;
    if (draft.version !== 1) return null;
    return { ...blankDraft(), ...draft, config: { ...defaultConfig(), ...draft.config } };
  } catch {
    return null;
  }
}

export function writeDraft(username: string, draft: WizardDraft): void {
  const key = draftKey(username);
  try {
    window.localStorage.setItem(key, JSON.stringify(draft));
  } catch {
    // Almost always the quota, and almost always the context documents. Keep
    // the words — those are the expensive part — and let the files go.
    try {
      window.localStorage.setItem(key, JSON.stringify({ ...draft, contextDocs: [] }));
    } catch {
      /* storage unavailable; the wizard still works, it just forgets */
    }
  }
}

export function clearDraft(username: string): void {
  try {
    window.localStorage.removeItem(draftKey(username));
  } catch {
    /* nothing to do */
  }
}

/* --- Step legality --------------------------------------------------------
   A step is reachable when the work before it exists. This is what stops a
   bookmarked `?step=confirm` from rendering a Launch button for an empty
   prompt.
   ------------------------------------------------------------------------- */

export function furthestStep(draft: WizardDraft): WizardStep {
  // A clone has no local prompt to show: `POST /runs {from_run}` inherits the
  // original's wording server-side, so the prompt step has nothing to edit and
  // the settings step is reachable without one.
  if (draft.prompt.trim() || draft.fromRun) return "confirm";
  if (draft.workshopId) return "options";
  return "intent";
}

/** True when this draft launches by inheriting an earlier run's prompt. */
export function isClone(draft: WizardDraft): boolean {
  return Boolean(draft.fromRun) && !draft.prompt.trim();
}

/** The requested step, or the furthest legal one when that is earlier. */
export function clampStep(requested: WizardStep, draft: WizardDraft): WizardStep {
  const limit = furthestStep(draft);
  return stepIndex(requested) > stepIndex(limit) ? limit : requested;
}

/* --- Prefill from a chosen direction -------------------------------------- */

/**
 * Applies an option's `recommended_settings` over a config.
 *
 * The UX review's sharpest finding: the workshop recommended 120 calls over 4
 * rounds and the launch form showed 20 over 1, with no indication the advice
 * existed. The four fields the schema carries are the four applied here.
 */
export function applyRecommended(
  config: RunConfig,
  recommended: RecommendedSettings | null | undefined,
): RunConfig {
  if (!recommended) return config;
  return {
    ...config,
    rounds: positive(recommended.rounds, config.rounds),
    matches_per_round: positive(recommended.matches_per_round, config.matches_per_round),
    budget_calls: positive(recommended.budget_calls, config.budget_calls),
    grounding_depth: (recommended.grounding_depth ??
      config.grounding_depth) as GroundingDepth,
  };
}

function positive(value: number | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.round(value)
    : fallback;
}

/* --- Per-role model and effort --------------------------------------------
   A tier is a *table*, not a setting: `/api/capabilities` publishes, for every
   tier, which model and how much thinking effort each of the nine steps gets.
   The wizard shows that table and lets any cell be changed.

   The rule that makes the rest of this work: `config.model_overrides` holds
   **only the cells that differ from the tier's own row**. A run that accepts
   the defaults therefore sends `{}`, a role put back to its tier value stops
   being an override rather than becoming a redundant one, and the launched
   config records the choice a person actually made instead of a snapshot of
   today's table.
   ------------------------------------------------------------------------- */

/**
 * The way back, written once.
 *
 * The whole-matrix control and each row's say the same thing, and one of them is
 * visible text while the other is an `aria-label` — naming them in two places is
 * how those two drift apart.
 *
 * The row label starts with the word the button actually shows. WCAG 2.5.3
 * (label in name) requires it, and the failure is concrete rather than
 * theoretical: a speech-input user says "click Reset" and nothing happens if the
 * accessible name begins "Back to the tier's defaults for…".
 */
export const RESET_ALL_LABEL = "Back to the tier’s defaults";

/** The visible word on a row's reset button; the accessible name extends it. */
export const RESET_ROLE_LABEL = "Reset";

export function resetRoleLabel(roleLabel: string): string {
  return `${RESET_ROLE_LABEL} ${roleLabel} to the tier’s default`;
}

/** `config.model_overrides`, named for what it is at the call sites. */
export type RoleOverrides = RunConfig["model_overrides"];
/** One role's `{ model?, effort? }` — both optional and independent. */
export type RoleOverride = RoleOverrides[string];

/** One row of a tier's table, as the capabilities payload publishes it. */
export type { TierRow } from "../../lib/models";

/**
 * The rules for moving a cell off the tier's table live in `lib/models.ts`.
 *
 * They did live here, when the wizard was the only thing that could move one.
 * The top bar now edits the same kind of table for the system default, and two
 * copies of "an override records only the cells that differ" is two copies of
 * the thing every reset affordance on both screens promises. The functions are
 * re-exported rather than wrapped so there is one implementation and one name.
 */
export {
  clearRoleOverride,
  isRoleOverridden,
  overriddenRoles,
  resolveRole,
  setRoleOverride,
} from "../../lib/models";

/**
 * The narrowings at the payload boundary.
 *
 * The request body types `model` and `effort` as literal unions — that is the
 * allowlist the backend validates against, and it is deliberately narrow (the
 * id the CLI silently downgrades is refused by not being in it). The values
 * passed here always come from `/api/capabilities`, which is built from the
 * same allowlist, so this is a boundary and not a guess: nothing in a component
 * ever types a model id or an effort of its own.
 */
export function asRoleModel(value: string): RoleOverride["model"] {
  return value as RoleOverride["model"];
}

export function asRoleEffort(value: string): RoleOverride["effort"] {
  return value as RoleOverride["effort"];
}

/* --- Titles --------------------------------------------------------------- */

/** The backend derives one too; showing it here means no surprise in the list. */
export function deriveTitle(question: string): string {
  const cleaned = question.replace(/\s+/g, " ").trim();
  if (cleaned.length <= 60) return cleaned;
  const cut = cleaned.slice(0, 60);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > 30 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}

/**
 * The harness a run of this config will occupy. Demo has its own lane.
 *
 * Typed from the request body rather than `Harness`: codex is a harness the health probe
 * reports on, not one a run can be launched against, and `POST /runs` refuses it.
 */
export function harnessFor(config: RunConfig): LaunchHarness {
  return config.runner === "demo" ? "demo" : "claude";
}

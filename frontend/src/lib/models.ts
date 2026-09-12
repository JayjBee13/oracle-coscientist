/**
 * The engine's role table, read the same way everywhere.
 *
 * `GET /api/capabilities` publishes, per tier, one row per role: the model, the
 * thinking effort, whether it may search the web, and the engine's own sentence
 * about why it is set that way. Four surfaces render that table — the wizard's
 * role matrix, the Confirm step, the workflow diagram and /how-it-works — and
 * before this file they each folded the payload themselves. That is how the
 * Confirm step came to print `claude-opus-5 / Medium` three inches under a
 * diagram saying `Fable 5 / High effort` for the same step.
 *
 * So the folding lives here, once:
 *
 *   - `resolveTier` picks the tier to describe and *says* when the one that was
 *     asked for is no longer published, rather than silently drawing nothing;
 *   - `runRoles` drops the roles a launched run never executes;
 *   - `applyOverrides` lays `config.model_overrides` over the tier's rows;
 *   - `modelLabel` turns an id into the catalog's name for it.
 *
 * Nothing here names a model, a tier or an effort. Every value comes off the
 * payload; the only role names written down are the two structural facts about
 * *when* a role runs, which are properties of the orchestrator rather than of
 * any model.
 */

import type { ModelChoice, ModelsPayload, RoleModelRow } from "../api/types";

/**
 * Roles the payload publishes that no run ever executes.
 *
 * `workshop` turns a rough question into research goals *before* a run exists —
 * `services/workshop` calls it and the orchestrator never does. Offering it in a
 * per-run matrix collects a choice the run cannot honour.
 */
export const PRE_RUN_ROLES: ReadonlySet<string> = new Set(["workshop"]);

/** Roles that only run when the diversity injection fires. Hidden when it is off. */
export const GRAFT_ONLY_ROLES: ReadonlySet<string> = new Set(["cartographer"]);

/** Which tier is actually being described, and whether that is the one asked for. */
export type TierResolution = {
  /** The tier whose rows follow, or null when the payload has not arrived. */
  tier: string | null;
  rows: readonly RoleModelRow[];
  /**
   * The tier that was requested and is no longer published, if any. A stored
   * config from before a tier was retired lands here; the caller shows the
   * default instead and says so.
   */
  missing: string | null;
};

/**
 * The rows for a tier, falling back to the payload's default when the requested
 * tier is not a key of `tiers` — `?? ` alone does not catch that, and a tier
 * string that is merely *unknown* used to yield an empty table with no models,
 * no efforts and no explanation.
 */
export function resolveTier(
  models: ModelsPayload | null,
  provider: string | null,
  tier?: string | null,
): TierResolution {
  if (!models) return { tier: tier ?? null, rows: [], missing: null };
  const tables = tablesFor(models, provider);
  if (tier && tables[tier]) return { tier, rows: tables[tier], missing: null };
  const fallback = models.default_tier || null;
  const rows = fallback ? (tables[fallback] ?? []) : [];
  return { tier: fallback, rows, missing: tier && tier !== fallback ? tier : null };
}

/** The rows a launched run with this configuration will actually execute. */
export function runRoles(
  rows: readonly RoleModelRow[],
  options: { graft?: boolean } = {},
): RoleModelRow[] {
  return rows.filter(
    (row) =>
      !PRE_RUN_ROLES.has(row.role) &&
      (options.graft === true || !GRAFT_ONLY_ROLES.has(row.role)),
  );
}

/** One role's `{ model, effort }` override, as the request body types it. */
export type RoleChoice = { model?: string | null; effort?: string | null };

/** The tier's row with the run's own choice for that role laid over it. */
export function applyOverride(
  row: RoleModelRow,
  override: RoleChoice | undefined,
): RoleModelRow {
  if (!override || (override.model == null && override.effort == null)) return row;
  return {
    ...row,
    model: override.model ?? row.model,
    effort: override.effort ?? row.effort,
  };
}

/** Every row with the run's overrides laid over it, in the payload's order. */
export function applyOverrides(
  rows: readonly RoleModelRow[],
  overrides: Readonly<Record<string, RoleChoice>> | null | undefined,
): RoleModelRow[] {
  return rows.map((row) => applyOverride(row, overrides?.[row.role]));
}

/**
 * The catalog's name for a model id, or the id shortened when it is not listed.
 *
 * A run stored before a model was retired still names it, and the honest thing
 * is to print what it will run rather than substitute something else.
 */
export function modelLabel(
  model: string | null | undefined,
  catalog: readonly ModelChoice[],
): string | null {
  if (!model) return null;
  const known = catalog.find((choice) => choice.id === model);
  return known ? known.label : shortModel(model);
}

/** `claude-opus-5` reads as `opus-5` when the catalog has no published name. */
export function shortModel(model: string): string {
  return model.replace(/^claude-/, "");
}

/* --- Two providers, one table ----------------------------------------------
   The engine runs on two CLIs now, and a step may be set to either. Which
   models a provider offers, which efforts each of those models accepts, and what
   each (provider, tier) pair resolves to are all facts the backend publishes —
   the job here is to read them, and to keep reading the single-provider payload
   correctly while the backend is still growing the fields.

   That last part is the whole reason these are functions rather than property
   accesses. Every one of them degrades to exactly what this app did before
   providers existed, so a partial payload renders a working screen instead of an
   empty one.
   ------------------------------------------------------------------------- */

/** A provider with its models resolved out of the catalog, in catalog order. */
export type ProviderView = {
  id: string;
  label: string;
  models: ModelChoice[];
  /**
   * The union of this provider's models' ladders. It sizes a control and nothing
   * else — a *row's* options are the ladder of the model on that row.
   */
  efforts: readonly string[];
  /** Whether its CLI is installed. `undefined` means unprobed, not absent. */
  installed?: boolean;
};

/**
 * Which provider a model runs on, or null when the catalog does not list it.
 *
 * Null is a real answer and callers must treat it as one. A run frozen before a
 * model was retired still names it, and guessing a vendor from the shape of an
 * id ("it starts with claude-") is how a screen ends up asserting a provider the
 * backend never claimed.
 */
export function providerOf(
  models: ModelsPayload | null,
  model: string | null | undefined,
): string | null {
  if (!models || !model) return null;
  return models.catalog.find((choice) => choice.id === model)?.provider ?? null;
}

/**
 * The providers to offer, each with its models.
 *
 * `harnesses` is the `harnesses` block of the same payload: a provider names the
 * CLI lane it drives, and whether that CLI is installed is the one thing about a
 * provider worth saying beside its name. Left out, nothing is claimed.
 */
export function providerViews(
  models: ModelsPayload | null,
  harnesses?: Readonly<Record<string, { installed: boolean }>>,
): ProviderView[] {
  if (!models) return [];
  return models.providers.map((provider) => ({
    id: provider.id,
    label: provider.label,
    models: models.catalog.filter((choice) => choice.provider === provider.id),
    efforts: provider.efforts,
    installed: harnesses?.[provider.harness]?.installed,
  }));
}

/**
 * The providers a person may actually pick, which can be narrower than the ones
 * the payload describes.
 *
 * A provider is selectable only once the payload carries a resolved table for
 * it. That is not caution for its own sake — it closes the one failure this
 * feature can have that nobody would notice: pick a provider whose tables have
 * not landed, every screen falls back to the tables it does have, and one
 * vendor's models render under the other vendor's name while the run quietly
 * uses the models on screen. Offering only what the payload can describe means
 * the segment cannot claim a table that does not exist.
 */
export function selectableProviders(
  models: ModelsPayload | null,
  harnesses?: Readonly<Record<string, { installed: boolean }>>,
): ProviderView[] {
  return providerViews(models, harnesses).filter(
    (view) =>
      view.models.length > 0 && Object.keys(models?.tiers[view.id] ?? {}).length > 0,
  );
}

/** The provider a new run would use. */
export function defaultProvider(models: ModelsPayload | null): string | null {
  return models?.default_provider || null;
}

/**
 * Every tier one provider publishes, keyed by tier.
 *
 * Falls back to the default provider's tables for a provider the payload has
 * never heard of — a stored default naming a vendor since retired — so the
 * screen shows a real table rather than nine blank rows. The caller that must
 * not *claim* that vendor is the one drawing its name; see `selectableProviders`
 * and the Confirm step, which name the provider off the rows on screen.
 */
export function tablesFor(
  models: ModelsPayload | null,
  provider: string | null,
): Record<string, readonly RoleModelRow[]> {
  if (!models) return {};
  return models.tiers[provider ?? ""] ?? models.tiers[models.default_provider] ?? {};
}

/** The tiers this provider publishes, in the payload's order. */
export function tiersFor(
  models: ModelsPayload | null,
  provider: string | null,
): string[] {
  return Object.keys(tablesFor(models, provider));
}

/* --- The tiers themselves ---------------------------------------------------
   `tiers` above answers "what does this tier resolve to". `tier_catalog`
   answers a different question — "what *is* this tier, and can this machine run
   it" — and a control that offers tiers needs both: the table it draws comes
   from the first, and every word it puts beside the segment (the label, the
   sentence describing the tier, the model a tier pins, the CLI that pin needs)
   comes from the second.

   Read rather than restated for the reason the role table is: four tier names
   and a description each, typed into a component, are four descriptions that go
   wrong the first time a tier changes — and one of them now changes which model
   a step runs on, which is exactly the fact a client must not have to infer.
   ------------------------------------------------------------------------- */

/** One tier a control may offer, and what selecting it does. */
export type TierView = {
  id: string;
  /**
   * The payload's own label, or null when this build's backend publishes no
   * catalog. Null is a real answer and callers must treat it as one: the caller
   * falls back to its own vocabulary rather than printing a raw tier id.
   */
  label: string | null;
  /** What choosing it does, in the engine's words. Empty when unpublished. */
  note: string;
  /**
   * Model class → the model this tier forces on it; empty for a tier that
   * forces none. The whole reason the field exists: a tier that moves the model
   * as well as the effort says so here, instead of leaving a reader to diff two
   * resolved tables to notice.
   */
  pinnedModels: Record<string, string>;
  /** The CLIs a run in this tier drives *because of the pin*, whatever its provider. */
  requiresHarness: readonly string[];
  /** False only when a CLI this tier requires is missing from this machine. */
  available: boolean;
  /** Why it cannot run here, in the backend's words. Null when it can. */
  unavailableReason: string | null;
};

/**
 * Every tier this provider publishes a table for, as the catalog describes it.
 *
 * Driven by `tiers[provider]` and not by the catalog, for the reason
 * `selectableProviders` gives: a tier with no published table is a tier whose
 * rows this app would have to invent. A tier the catalog does not describe is
 * still offered — with `label: null` and no note — because the table is the half
 * a control cannot do without, and a backend that predates the catalog still
 * renders exactly what this app rendered before it existed.
 */
export function tierViews(
  models: ModelsPayload | null,
  provider: string | null,
): TierView[] {
  const catalog = models?.tier_catalog ?? [];
  return tiersFor(models, provider).map((id) => {
    const brief = catalog.find((entry) => entry.id === id);
    return {
      id,
      label: brief?.label || null,
      note: brief?.note ?? "",
      pinnedModels: brief?.pinned_models ?? {},
      requiresHarness: brief?.requires_harness ?? [],
      // Absent is available, the same rule `canSearch` follows: a backend that
      // publishes no catalog has not claimed a tier cannot run here, and a
      // warning nobody asserted is a warning nobody can act on.
      available: brief?.available !== false,
      unavailableReason: brief?.unavailable_reason ?? null,
    };
  });
}

/** The distinct models a tier pins, deduplicated. Empty for an effort-only tier. */
export function pinnedModels(view: TierView): string[] {
  return [...new Set(Object.values(view.pinnedModels))];
}

/** The table for one (provider, tier) pair. */
export function tableFor(
  models: ModelsPayload | null,
  provider: string | null,
  tier: string | null,
): readonly RoleModelRow[] {
  if (!tier) return [];
  return tablesFor(models, provider)[tier] ?? [];
}

/**
 * The effort vocabulary for one model.
 *
 * Per-model first, because the two providers do not share a scale and a model
 * may have a shorter ladder than its provider's longest: offering a value the
 * model cannot take is offering a call the backend has to refuse or clamp.
 */
export function effortsFor(
  models: ModelsPayload | null,
  model: string | null | undefined,
): readonly string[] {
  if (!models) return [];
  const known = model ? models.catalog.find((choice) => choice.id === model) : undefined;
  if (known && known.efforts.length > 0) return known.efforts;
  // Not in the catalog: a model this engine has retired, still named by a stored
  // default. The engine's whole vocabulary is the only honest bound left.
  return models.efforts;
}

/**
 * Whether a model can search the web — `null` when the payload does not say.
 *
 * The distinction matters more here than almost anywhere else in the app: an
 * unprobed model rendered as "cannot search" is a warning about a limitation
 * nobody has established. Absent is absent.
 */
export function canSearch(
  models: ModelsPayload | null,
  model: string | null | undefined,
): boolean | null {
  if (!models || !model) return null;
  const known = models.catalog.find((choice) => choice.id === model);
  return known ? known.grounded : null;
}

/**
 * A step that wants the web, set to a model that cannot reach it.
 *
 * Both halves come off the payload: `row.grounded` is the engine saying this
 * step searches, `catalog.grounded` is the catalog saying this model can. The
 * warning exists only where the payload asserts both, so a backend that has not
 * published model grounding raises nothing at all.
 */
export function groundingLost(
  models: ModelsPayload | null,
  row: { grounded?: boolean },
  model: string | null | undefined,
): boolean {
  return row.grounded === true && canSearch(models, model) === false;
}

/**
 * An effort this model does not accept, named so the mismatch is visible.
 *
 * Returns the offending value rather than a boolean, because the sentence a
 * reader needs is "it does not take <this>" — and returns null when the
 * vocabulary is unpublished, since an empty list is not a refusal.
 */
export function effortUnsupported(
  models: ModelsPayload | null,
  model: string | null | undefined,
  effort: string | null | undefined,
): string | null {
  if (!effort) return null;
  const vocabulary = effortsFor(models, model);
  if (vocabulary.length === 0) return null;
  return vocabulary.includes(effort) ? null : effort;
}

/**
 * True when this step gets the same effort at every tier this provider publishes.
 *
 * Clustering is the case this exists for: it is mechanical, effort buys it
 * nothing, and it is pinned low in every tier — so moving the tier segment leaves
 * that one row exactly where it was. Saying so is the difference between a
 * control that looks broken and a control that is being honest. Derived from the
 * tables themselves, so it cannot claim a pinning the engine has stopped applying.
 */
export function pinnedAcrossTiers(
  models: ModelsPayload | null,
  provider: string | null,
  role: string,
): boolean {
  const tiers = tiersFor(models, provider);
  if (tiers.length < 2) return false;
  const efforts = new Set<string>();
  for (const tier of tiers) {
    const row = tableFor(models, provider, tier).find((entry) => entry.role === role);
    if (!row) return false;
    efforts.add(row.effort);
  }
  return efforts.size === 1;
}

/* --- Moving one cell off the tier's table ----------------------------------
   Two surfaces edit a role table now — the wizard, for one run, and the top bar,
   for the system default — and they must agree on what "changed" means down to
   the object they produce. So the rule lives here and both call it.

   The rule: an override records **only the cells that differ from the tier's own
   row**. Accepting the defaults sends `{}`, a cell put back stops being an
   override rather than becoming a redundant one, and "changed it and changed it
   back" is indistinguishable from never having touched it — which is exactly
   what the reset affordances promise.
   ------------------------------------------------------------------------- */

/** One row of a tier's table: the three fields an override is measured against. */
export type TierRow = { role: string; model: string; effort: string };

/** What a role will actually run as: the tier's row with the override over it. */
export function resolveRole(
  row: TierRow,
  override: RoleChoice | undefined,
): { model: string; effort: string } {
  return {
    model: override?.model ?? row.model,
    effort: override?.effort ?? row.effort,
  };
}

/** True when this role differs from its tier in at least one cell. */
export function isRoleOverridden(
  overrides: Readonly<Record<string, RoleChoice>>,
  role: string,
): boolean {
  const override = overrides[role];
  return override != null && (override.model != null || override.effort != null);
}

/** The roles with something to reset, in the order they were added. */
export function overriddenRoles(
  overrides: Readonly<Record<string, RoleChoice>>,
): string[] {
  return Object.keys(overrides).filter((role) => isRoleOverridden(overrides, role));
}

/**
 * Sets one cell of one row, keeping the overrides minimal.
 *
 * Generic over the override type so both callers keep their own: the wizard's
 * `config.model_overrides` is typed to the request body's literal unions, the
 * top bar's is the looser settings shape, and neither has to widen to share this.
 */
export function setRoleOverride<T extends RoleChoice>(
  overrides: Readonly<Record<string, T>>,
  row: TierRow,
  patch: Partial<T>,
): Record<string, T> {
  const merged = { ...(overrides[row.role] ?? ({} as T)), ...patch };
  const kept = {} as T;
  if (merged.model != null && merged.model !== row.model) kept.model = merged.model;
  if (merged.effort != null && merged.effort !== row.effort) kept.effort = merged.effort;

  const next: Record<string, T> = { ...overrides };
  if (kept.model == null && kept.effort == null) delete next[row.role];
  else next[row.role] = kept;
  return next;
}

/** Puts one role back to whatever the tier gives it. */
export function clearRoleOverride<T extends RoleChoice>(
  overrides: Readonly<Record<string, T>>,
  role: string,
): Record<string, T> {
  if (!(role in overrides)) return { ...overrides };
  const next: Record<string, T> = { ...overrides };
  delete next[role];
  return next;
}

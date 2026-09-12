import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useLocation, useMatch } from "react-router-dom";

import type {
  ModelChoice,
  ModelSettings,
  ModelSettingsChoice,
  ModelsPayload,
  RoleModelRow,
  RunDetail,
} from "../api/types";
import { Chip } from "../components/Status";
import {
  applyOverrides,
  clearRoleOverride,
  defaultProvider,
  effortUnsupported,
  effortsFor,
  groundingLost,
  isRoleOverridden,
  modelLabel,
  overriddenRoles,
  pinnedAcrossTiers,
  pinnedModels,
  resolveRole,
  selectableProviders,
  setRoleOverride,
  shortModel,
  tableFor,
  tierViews,
  tiersFor,
} from "../lib/models";
import type { ProviderView, TierView } from "../lib/models";
import {
  describeEffort,
  describeModelTier,
  describeProvider,
  describeRole,
} from "../lib/status";
import type { Tone } from "../lib/status";
import { refreshCapabilities, useCapabilities } from "../store/capabilities";
import { useIdentity } from "../store/identity";
import { refreshRunDetail, useRun } from "../store/runs";
import {
  editModelSettings,
  isModelSettingsDirty,
  otherEditedTiers,
  refreshModelSettings,
  revertModelSettings,
  saveModelSettings,
  tierBucket,
  useModelSettings,
} from "../store/settings";

/**
 * Which models this thing is about to run on — and, off a run, the place you
 * change them.
 *
 * Model and thinking effort decide both what a run is capable of and how long it
 * takes, and until this chip existed they were four clicks deep in a run's
 * Settings tab — invisible at the moment you care, which is *before* pressing
 * Launch and *while* watching a run work through its call budget.
 *
 * Two contexts, and the difference between them is the whole point:
 *
 *   - Off a run, it shows and **edits** the system default — what a run launched
 *     right now would use. The table is read from `/api/capabilities`, which the
 *     backend builds from the engine's own role table so it cannot drift from
 *     what actually runs, and the choice is stored through
 *     `GET/PUT /api/settings/models`.
 *   - On `/runs/:id`, it shows *that run's* resolved table, captured in its
 *     immutable config at launch, and nothing on it can be changed. A run
 *     started last week under a different tier must never be described by today's
 *     default — still less edited into a different one — so the panel relabels
 *     itself rather than quietly swapping the numbers underneath.
 */
export function ModelsIndicator() {
  const match = useMatch("/runs/:id/*");
  const runId = match?.params.id ?? null;

  return runId ? <RunModels key={runId} runId={runId} /> : <DefaultModels />;
}

/* --- The two contexts ------------------------------------------------------ */

function DefaultModels() {
  const capabilities = useCapabilities();
  const settings = useModelSettings();
  const identity = useIdentity();
  const models = capabilities.data?.models ?? null;

  // What a new run would use, which is the stored default when there is one and
  // the payload's own default until the first read comes back. Never the draft:
  // an edit nobody has saved is not what the next launch does, and a chip that
  // said otherwise would be the most quietly misleading thing in the top bar.
  const resolved = resolveSettings(models, settings.saved);
  const rows = resolved
    ? applyOverrides(
        tableFor(models, resolved.provider, resolved.tier),
        resolved.overrides,
      )
    : [];

  const tierLabel = resolved ? describeModelTier(resolved.tier).label : "";
  const loading =
    (capabilities.status === "idle" || (capabilities.status === "loading" && !models)) ??
    false;

  // `built_in` is not a saved default: it is the deliberate total-read fallback,
  // served when the stored value cannot be read at all — and reached the same way
  // when the read itself fails, which is what `resolveSettings` fills in. The
  // table below it is the engine's own, so a new run really would use these
  // models; what is missing is anybody's *choice* of them. The chip presented
  // that identically to a stored default for one release, and the failure it hid
  // was the expensive kind: a save that never persisted, reported as saved.
  //
  // Only once the read has settled. A request still in flight is not a fallback,
  // and a word that appeared on first paint and vanished a moment later would
  // train people to ignore it.
  const settled = settings.status === "ready" || settings.status === "error";
  const builtIn = settled && resolved?.source === "built_in";

  return (
    <ModelsPopover
      trigger={{
        scope: "Models",
        // Two words, because the eyebrow is the half of the chip small screens
        // drop: this one is the load-bearing half and stays.
        fallback: builtIn ? "Built-in" : null,
        value: resolved ? `${tierLabel} · ${headlineModel(rows, models?.catalog)}` : "",
        // Nothing to flag while this is a stored default: it *is* the default.
        tone: builtIn ? "caution" : "neutral",
        hint: builtIn
          ? "No saved default could be read — a new run would use the engine’s own models"
          : "Which model and thinking effort each step of a new run would use",
        loading,
        error: models ? null : capabilities.error,
      }}
      heading="Models for a new run"
      error={models ? null : capabilities.error}
      onRetry={() => void refreshCapabilities()}
      wide
    >
      <ModelsEditor
        models={models}
        settings={settings}
        editable={identity.data?.is_admin === true}
        identityReady={identity.status === "ready"}
      />
    </ModelsPopover>
  );
}

function RunModels({ runId }: { runId: string }) {
  const entry = useRun(runId);
  const capabilities = useCapabilities();

  // A demo run is scripted end to end. Printing the table it *would* have used
  // would be a plausible, checkable-looking lie about where the money went.
  const demo = entry.summary?.harness === "demo";
  const rows = demo ? [] : (entry.detail?.model_table ?? []);
  const tier = readTier(entry.detail);
  const tierLabel = tier && !demo ? describeModelTier(tier).label : null;
  // A run on anything but today's default is the one worth catching the eye: it is
  // either costing more than you expect or, on an older run, quietly costing less.
  const offDefault = tier !== null && tier !== capabilities.data?.models.default_tier;

  let value = [tierLabel, headlineModel(rows, capabilities.data?.models.catalog)]
    .filter(Boolean)
    .join(" · ");
  let subhead = tierLabel
    ? `${tierLabel} — fixed at launch, for this run only.`
    : "Fixed at launch, for this run only.";
  let instead: string | null = null;
  if (demo) {
    value = "Demo · no model calls";
    subhead = "A demo run makes no model calls at all.";
    instead =
      "The interface is the real one; the results are scripted, and no model is called.";
  } else if (rows.length === 0) {
    value = "Not recorded";
    instead =
      entry.summary?.source === "imported"
        ? "No model table was recorded — imported runs predate this field."
        : "No model table was recorded for this run.";
  }

  return (
    <ModelsPopover
      trigger={{
        scope: "Run models",
        value,
        tone: demo ? "info" : offDefault ? "accent" : "neutral",
        hint: demo
          ? "A demo run makes no model calls"
          : "Which model and thinking effort each step of this run uses",
        loading: entry.detail === null && entry.status !== "error",
        error: entry.detail ? null : entry.error,
      }}
      heading="Models for this run"
      error={entry.detail ? null : entry.error}
      onRetry={() => void refreshRunDetail(runId)}
    >
      <p className="models__sub">{subhead}</p>
      {instead ? (
        <p className="models__sub">{instead}</p>
      ) : (
        <div className="scroll-x">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Step</th>
                <th scope="col">Model</th>
                <th scope="col">Thinking effort</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.role}>
                  {/* The engine's own reason this step is set the way it is,
                      when the source carries one. */}
                  <td
                    title={
                      "note" in row ? ((row.note as string) ?? undefined) : undefined
                    }
                  >
                    {describeRole(row.role)}
                  </td>
                  <td className="mono models__model">{row.model}</td>
                  <td>{row.effort ? describeEffort(row.effort) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {/* The escalations are engine behaviour, so they hold for this run too. */}
      <Notes
        notes={demo || rows.length === 0 ? [] : (capabilities.data?.models.notes ?? [])}
      />
    </ModelsPopover>
  );
}

/**
 * A run's tier, out of its immutable config — which is an untyped blob, and holds
 * whatever the tier was *called* on the day the run launched. Old names are kept
 * rather than translated: `describeModelTier` labels what it knows and humanises
 * the rest, and a run is honestly described by the vocabulary it was launched with.
 */
function readTier(detail: RunDetail | null): string | null {
  const value = detail?.config?.model_tier;
  return typeof value === "string" && value.trim() ? value : null;
}

/**
 * The provider and tier a new run would use.
 *
 * The stored default when the backend has one, and the payload's own default
 * until it answers — so the chip says something true during the first paint
 * rather than nothing, and never invents a tier of its own.
 */
function resolveSettings(
  models: ModelsPayload | null,
  saved: ModelSettings | null,
): ModelSettings | null {
  if (!models) return null;
  if (saved) return saved;
  if (!models.default_tier) return null;
  return {
    provider: asProvider(defaultProvider(models) ?? ""),
    tier: asTier(models.default_tier),
    // No stored buckets, because nothing has been stored — which is the truthful
    // value here and not a placeholder: an absent bucket is a tier following the
    // built-in table, and every tier is following it. `overrides` mirrors
    // `tiers[tier]`, so an empty document mirrors to empty.
    tiers: {},
    overrides: {},
    // Never read here — every screen resolves the table from the payload — but
    // the document is one shape, and half a document is a shape nothing types.
    table: [
      ...tableFor(models, defaultProvider(models), models.default_tier),
    ] as ModelSettings["table"],
    source: "built_in",
    updated_at: null,
  };
}

/* --- The narrowings at the payload boundary --------------------------------
   The same boundary `asRoleModel` documents in `wizard/state`: every value that
   passes through here came out of `/api/capabilities`, which the backend builds
   from the same allowlist these literal unions are generated from. Named
   functions rather than inline casts, so the places the two vocabularies meet
   are greppable.
   -------------------------------------------------------------------------- */

function asProvider(value: string): ModelSettings["provider"] {
  return value as ModelSettings["provider"];
}

function asTier(value: string): ModelSettings["tier"] {
  return value as ModelSettings["tier"];
}

function asModel(value: string): ModelSettingsChoice["model"] {
  return value as ModelSettingsChoice["model"];
}

function asEffort(value: string): ModelSettingsChoice["effort"] {
  return value as ModelSettingsChoice["effort"];
}

/** A row of the table, from any of the three sources that produce one. */
type PanelRow = {
  role: string;
  model: string;
  effort: string | null;
};

/**
 * The one model to print when there is room for one.
 *
 * Generation is the role that runs most often and the one a tier is chosen for,
 * so it is the honest headline; the panel behind the chip has the other eight.
 *
 * Names it the way the API does — `fable` is an argv identifier, "Fable 5" is
 * what it is called. A model no longer in the catalog (a run launched before it
 * was retired) has no published label, so its id is shortened instead.
 */
function headlineModel(
  rows: readonly PanelRow[],
  catalog: readonly ModelChoice[] = [],
): string {
  const row = rows.find((candidate) => candidate.role === "generation") ?? rows[0];
  if (!row) return "";
  return modelLabel(row.model, catalog) ?? "";
}

/* --- The chip and its panel ------------------------------------------------ */

type TriggerView = {
  /** Eyebrow, and the first half of the accessible name. */
  scope: string;
  /**
   * One more word on the eyebrow, for a state the value cannot carry: the value
   * says which models, and is identical whether somebody chose them or nobody
   * did. Kept out of `scope` so the responsive rule can drop the generic half
   * and keep this one.
   */
  fallback?: string | null;
  value: string;
  tone: Tone;
  hint: string;
  loading: boolean;
  error: string | null;
};

function ModelsPopover({
  trigger,
  heading,
  error,
  onRetry,
  wide = false,
  children,
}: {
  trigger: TriggerView;
  heading: string;
  /** Shown in place of the body, with a retry. Never beside it. */
  error: string | null;
  onRetry: () => void;
  /** The editor needs three columns of controls; the read-only table does not. */
  wide?: boolean;
  children: ReactNode;
}) {
  const panelId = useId();
  const titleId = useId();
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();

  // Open *for a route*, rather than open plus an effect that closes it on
  // navigation: a panel describing a page you have left closes itself in the
  // same render that leaves, with no cascading state update.
  const [openedAt, setOpenedAt] = useState<string | null>(null);
  const open = openedAt === pathname;
  const close = (): void => setOpenedAt(null);

  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus();

    const fitPanel = (): void => {
      const panel = panelRef.current;
      if (!panel) return;
      const viewport = window.visualViewport;
      const bottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
      const available = Math.max(0, bottom - panel.getBoundingClientRect().top);
      panel.style.setProperty(
        "--models-panel-max-height",
        `${Math.floor(available * 0.9)}px`,
      );
    };
    fitPanel();
    window.addEventListener("resize", fitPanel);
    window.addEventListener("scroll", fitPanel, true);
    window.visualViewport?.addEventListener("resize", fitPanel);
    window.visualViewport?.addEventListener("scroll", fitPanel);

    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      close();
      triggerRef.current?.focus();
    };
    const onPointerDown = (event: Event): void => {
      if (!wrapRef.current?.contains(event.target as Node)) close();
    };

    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      window.removeEventListener("resize", fitPanel);
      window.removeEventListener("scroll", fitPanel, true);
      window.visualViewport?.removeEventListener("resize", fitPanel);
      window.visualViewport?.removeEventListener("scroll", fitPanel);
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, [open]);

  const spoken = trigger.loading
    ? "loading"
    : trigger.error
      ? "unavailable"
      : trigger.value;
  // The eyebrow's own words are part of the name, and the separator this chip
  // sets its value with is punctuation a screen reader should read as a pause.
  const name = `${[trigger.scope, trigger.fallback]
    .filter(Boolean)
    .join(" · ")}: ${spoken}`.replace(/ · /g, ", ");

  return (
    <div className="models" ref={wrapRef}>
      <button
        type="button"
        ref={triggerRef}
        className="chip chip--button models__trigger"
        data-tone={trigger.error ? "caution" : trigger.tone}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={name}
        title={trigger.error ?? trigger.hint}
        onClick={() => setOpenedAt(open ? null : pathname)}
      >
        <span className="label models__scope">{trigger.scope}</span>
        {trigger.fallback ? (
          <span className="label models__fallback">{trigger.fallback}</span>
        ) : null}
        {trigger.loading ? (
          <span className="skeleton skeleton--text models__pending" aria-hidden="true" />
        ) : (
          <span className="models__value">
            {trigger.error ? "Unavailable" : trigger.value}
          </span>
        )}
      </button>

      {open ? (
        <div
          className={wide ? "models__panel models__panel--wide" : "models__panel"}
          id={panelId}
          ref={panelRef}
          role="group"
          aria-labelledby={titleId}
          tabIndex={-1}
        >
          <h2 className="models__title" id={titleId}>
            {heading}
          </h2>
          {error ? (
            <div className="models__error" role="alert">
              <p>{error}</p>
              <button
                type="button"
                className="btn btn--sm btn--primary"
                onClick={onRetry}
              >
                Try again
              </button>
            </div>
          ) : (
            children
          )}
        </div>
      ) : null}
    </div>
  );
}

function Notes({ notes }: { notes: readonly string[] }) {
  if (notes.length === 0) return null;
  return (
    <ul className="models__notes">
      {notes.map((note) => (
        <li key={note}>{note}</li>
      ))}
    </ul>
  );
}

/* --- The editor ------------------------------------------------------------
   Two controls that fill the whole table, and a table any cell of which can then
   be moved on its own — the same shape as the wizard's role matrix, for the same
   reason: a run is a loop of model calls and they are not the same job.

   What is different here is what it writes. The wizard configures one run; this
   configures every run that has not been configured yet, so it has a Save and a
   stored value to be dirty against, and it is the one surface where "what I am
   looking at" and "what would actually launch" can differ.

   And what it writes is **one table per tier**. Saving redefines the tier that is
   selected — set the table up at High and save, and that is what High means from
   then on — so the panel shows one tier's overrides at a time, switching tiers
   switches which bucket is being edited rather than discarding it, and Save
   sends every bucket. The buckets stay sparse: a tier nobody edited is absent
   from the document, which is what tells the backend it still follows the
   built-in table, today and after the catalog moves under it.
   ------------------------------------------------------------------------- */

function ModelsEditor({
  models,
  settings,
  editable,
  identityReady,
}: {
  models: ModelsPayload | null;
  settings: ReturnType<typeof useModelSettings>;
  editable: boolean;
  identityReady: boolean;
}) {
  if (!models) return <p className="models__sub">Reading the model table…</p>;

  // A failed read of the stored default is **not** a failed panel. The two
  // endpoints answer different questions: capabilities says which models exist
  // and what each tier resolves to — everything the table is drawn from — and
  // settings says only which of those you last chose. Losing the second one
  // costs you a preference, not the table, so the editor falls back to the
  // engine's own default and says so beside the Save button.
  //
  // It was the other way round for one commit, and the case it broke is the
  // ordinary one: a tab left open across a backend restart, or a frontend built
  // ahead of the backend that serves `/settings/models`, put a bare URL in a red
  // box where nine rows of models used to be.
  const draft = settings.draft ?? resolveSettings(models, null);
  if (!draft) return <p className="models__sub">Reading the stored default…</p>;

  return (
    <EditorBody
      models={models}
      settings={settings}
      draft={draft}
      editable={editable}
      identityReady={identityReady}
    />
  );
}

function EditorBody({
  models,
  settings,
  draft,
  editable,
  identityReady,
}: {
  models: ModelsPayload;
  settings: ReturnType<typeof useModelSettings>;
  draft: ModelSettings;
  editable: boolean;
  identityReady: boolean;
}) {
  const providers = selectableProviders(models);
  const tiers = tierViews(models, draft.provider);
  const tier = tiers.find((entry) => entry.id === draft.tier) ?? null;
  const rows = tableFor(models, draft.provider, draft.tier);
  /** The selected tier's overrides. The other tiers' are kept, not shown. */
  const bucket = tierBucket(draft);
  const changed = overriddenRoles(bucket);
  const tierLabel = tierName(tier, draft.tier);
  const others = otherEditedTiers(draft).map((name) =>
    tierName(tiers.find((entry) => entry.id === name) ?? null, name),
  );
  /** The models this tier forces on every step, named the way the catalog does. */
  const pinned = (tier ? pinnedModels(tier) : []).map(
    (model) => modelLabel(model, models.catalog) ?? model,
  );
  const dirty = isModelSettingsDirty(settings);
  /** The stored default could not be read, so the table below is the engine's. */
  const unread = settings.status === "error" && !settings.saved;

  const set = (next: Partial<ModelSettings>): void =>
    editModelSettings({ ...draft, ...next });

  /** Moves the selected tier's bucket, and only that one. */
  const setBucket = (next: Record<string, ModelSettingsChoice>): void =>
    set({ tiers: { ...draft.tiers, [draft.tier]: next } });

  // Picking a provider refills every tier's table, so every tier's per-step
  // choices go with it: an override is a difference from a table, and it is
  // meaningless measured against a table that no longer applies. Picking a
  // *tier* is the opposite — each tier's overrides are measured against that
  // tier's own table, so switching shows the other bucket and keeps this one.
  const fill = (provider: string): void => {
    const published = tiersFor(models, provider);
    const tierId = published.includes(draft.tier)
      ? draft.tier
      : (published[0] ?? draft.tier);
    set({ provider: asProvider(provider), tier: asTier(tierId), tiers: {} });
  };

  return (
    <>
      <p className="models__sub">
        {editable
          ? "These are the system-wide defaults. Saving redefines the selected tier for every new run; the wizard can still override it for one run."
          : "These are the system-wide defaults for every new run. The wizard can still override them for one run."}
      </p>

      {identityReady && !editable ? (
        <div className="models__notice" role="status">
          <p>Only an administrator can change the system-wide model default.</p>
          <p>You can still choose models for an individual run in its setup wizard.</p>
          <p>
            Model credentials are managed centrally; your workspace does not need a
            separate API key or model sign-in.
          </p>
        </div>
      ) : null}

      <div className="models__controls">
        {providers.length > 1 ? (
          <Segment
            label="Provider"
            options={providers.map((provider) => ({
              value: provider.id,
              label: providerLabel(provider),
            }))}
            value={draft.provider}
            onChange={fill}
            disabled={!editable}
          />
        ) : null}
        {tiers.length > 1 ? (
          <Segment
            label="Effort tier"
            options={tiers.map((entry) => ({
              value: entry.id,
              label: tierName(entry, entry.id),
              // The reason it cannot run here, on the button that selects it as
              // well as in the notice below — the notice is the one a reader
              // cannot miss, and this is the one that is there before the click.
              title: entry.available ? undefined : (entry.unavailableReason ?? undefined),
            }))}
            value={draft.tier}
            onChange={(next) => set({ tier: asTier(next) })}
            disabled={!editable}
          />
        ) : null}
      </div>

      {/* What this tier does, in the engine's words rather than in ours. Served
          per tier precisely so a control does not describe a tier from memory
          and get it wrong the first time one changes. */}
      {tier?.note ? <p className="models__tier-note">{tier.note}</p> : null}

      {/* A tier that pins a model changes more than the effort dial, and the
          payload names the model so this can be *stated* rather than left to be
          inferred from a diff of two tables. That visibility is the whole
          condition on which a tier was allowed to name a model at all.

          What it deliberately does not say is anything about cost or speed. Which
          of those a pinned model buys is a property of that model, the note above
          says it in the engine's own words for the one tier that has one, and a
          sentence generated here would be this panel guessing on behalf of
          whatever model a future tier pins. */}
      {pinned.length > 0 ? (
        <p className="models__tier-note">
          {`Every step runs ${pinned.join(" / ")} — this tier sets the model, not just the effort.`}
        </p>
      ) : null}

      {/* The point of publishing the CLI a tier requires: warn here, rather than
          let the first call of somebody's run discover it. Saving is still
          allowed — the default is storable and resolvable, it is this machine
          that cannot run it — so it says that too. */}
      {tier && !tier.available ? (
        <div className="models__notice" role="status">
          <p>
            <strong>{tierLabel}</strong>
            {tier.unavailableReason
              ? ` — ${tier.unavailableReason}`
              : " cannot run here."}
          </p>
          <p>
            You can still save it, but a run in this tier would fail on its first call.
          </p>
        </div>
      ) : null}

      {changed.length > 0 ? (
        <div className="row-wrap models__dirty">
          <Chip tone="accent" className="chip--quiet">
            {`Edited — differs from ${tierLabel}`}
          </Chip>
          {/* Scoped in its own words, because it has to be told apart from two
              other things on this panel that also undo something: Discard
              changes, which throws the whole edit away, and the per-row Reset.
              This one drops *this tier's* overrides, which is what makes it
              follow the built-in preset again. */}
          <button
            type="button"
            className="btn btn--sm"
            disabled={!editable}
            onClick={() => setBucket({})}
          >
            {`Reset ${tierLabel} to the built-in preset`}
          </button>
        </div>
      ) : null}

      {/* One save writes every preset, so a preset that is redefined and not on
          screen still has to be visible. Otherwise "Save as the new High" is
          quietly also saving a Med somebody set up last week. */}
      {others.length > 0 ? (
        <p className="models__sub">
          {`Also redefined: ${others.join(", ")}. Saving leaves ${
            others.length > 1 ? "them" : "it"
          } as ${others.length > 1 ? "they are" : "it is"}.`}
        </p>
      ) : null}

      <div className="scroll-x">
        <table className="table models__table">
          <caption className="visually-hidden">
            One row per step of the loop: the model it runs on and how much thinking
            effort it gets. Both can be set per step.
          </caption>
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">Model</th>
              <th scope="col">Thinking effort</th>
              <th scope="col">
                <span className="visually-hidden">Back to the tier&rsquo;s value</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <EditorRow
                key={row.role}
                models={models}
                provider={draft.provider}
                providers={providers}
                row={row}
                override={bucket[row.role]}
                dirty={isRoleOverridden(bucket, row.role)}
                tierLabel={tierLabel}
                editable={editable}
                onPatch={(patch) => setBucket(setRoleOverride(bucket, row, patch))}
                onReset={() => setBucket(clearRoleOverride(bucket, row.role))}
              />
            ))}
          </tbody>
        </table>
      </div>

      <Notes notes={models.notes} />

      {/* Both failures live here, on the control they are about, rather than in
          place of the table — which needs neither of them. */}
      {unread ? (
        <div className="models__notice" role="status">
          <p>
            Your saved default could not be read, so this is the engine&rsquo;s own.
            Saving will still store it.
          </p>
          {/* The server's own words, which are frequently a status line rather
              than a sentence, are the detail on a retry — never the message. */}
          <button
            type="button"
            className="btn btn--sm"
            title={settings.error ?? undefined}
            onClick={() => void refreshModelSettings()}
          >
            Try again
          </button>
        </div>
      ) : null}

      {settings.saveError ? (
        <div className="models__error" role="alert">
          <p>
            That did not save, so new runs still use the stored default. Try again in a
            moment.
          </p>
          <p className="models__detail">{settings.saveError}</p>
        </div>
      ) : null}

      <div className="models__foot">
        {editable ? (
          <>
            <button
              type="button"
              className="btn btn--sm btn--primary"
              disabled={!dirty || settings.saving}
              onClick={() => void saveModelSettings()}
            >
              {/* Named for what it does. This is not "save my preferences": it is
              "this is what this tier means now", and the previous label — Save as
              the default — described only the half of that a reader could guess. */}
              {settings.saving ? "Saving…" : `Save as the new ${tierLabel}`}
            </button>
            {dirty && !settings.saving ? (
              <button
                type="button"
                className="btn btn--sm btn--ghost"
                onClick={() => revertModelSettings()}
              >
                Discard changes
              </button>
            ) : null}
          </>
        ) : null}
        {unread ? (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            onClick={() => void refreshModelSettings()}
          >
            Try reading it again
          </button>
        ) : null}
        <span className="spacer" />
        {/* Three different true things, and the reader needs to be told which:
            an edit nobody has saved, a stored default that could not be read
            (so the table is the engine's own), or the stored default itself. */}
        <span className="models__saved-state">
          {dirty
            ? "Not saved yet"
            : unread
              ? "No stored default was read — showing the engine’s own"
              : `${tierLabel} is what new runs use`}
        </span>
      </div>
    </>
  );
}

/**
 * A vendor's name, with its CLI's absence stated when the payload states it.
 *
 * The name comes from `lib/status` rather than from `provider.label`, for the
 * same reason no other enum is rendered raw: the payload's label is an
 * identifier the backend happens to have title-cased, and the one vocabulary
 * this app speaks lives in one file. An unprobed CLI says nothing — absent is
 * absent, and "not installed" is a claim.
 */
function providerLabel(provider: ProviderView): string {
  const name = describeProvider(provider.id).label;
  return provider.installed === false ? `${name} — not installed` : name;
}

/**
 * A tier's name, the payload's first and this app's vocabulary second.
 *
 * The opposite precedence to `providerLabel`, and deliberately so. A provider's
 * `label` is an identifier the backend title-cased, and there are two of them
 * that this app has always had words for. A tier's label is *authored* — served
 * beside the tier's note and its pinned model precisely so a control does not
 * hard-code four names — so the payload wins, and `describeModelTier` is the
 * fallback for a backend that publishes no catalog and for a run whose stored
 * tier no longer exists.
 */
function tierName(tier: TierView | null, id: string): string {
  return tier?.label ?? describeModelTier(id).label;
}

function EditorRow({
  models,
  provider,
  providers,
  row,
  override,
  dirty,
  tierLabel,
  onPatch,
  onReset,
  editable,
}: {
  models: ModelsPayload;
  provider: string;
  providers: readonly ProviderView[];
  row: RoleModelRow;
  override: ModelSettingsChoice | undefined;
  dirty: boolean;
  tierLabel: string;
  onPatch: (patch: ModelSettingsChoice) => void;
  onReset: () => void;
  editable: boolean;
}) {
  const noteId = useId();
  const modelId = useId();
  const effortId = useId();

  const label = describeRole(row.role);
  const resolved = resolveRole(row, override);

  // Every model, grouped by the vendor that runs it — steps mix providers freely,
  // so the segment above is a quick-set and never a lock.
  const groups = providers.map((entry) => ({
    label: describeProvider(entry.id).label,
    options: entry.models.map((choice) => ({ value: choice.id, label: choice.label })),
  }));
  const efforts = effortsFor(models, resolved.model);

  // Three things the reader has to be told rather than left to infer, all of them
  // read off the payload: a step that wants the web on a model that cannot reach
  // it, an effort this model does not accept, and a row the tier segment will
  // never move.
  const ungrounded = groundingLost(models, row, resolved.model);
  const unsupported = effortUnsupported(models, resolved.model, resolved.effort);
  const pinned = pinnedAcrossTiers(models, provider, row.role);

  return (
    <tr data-changed={dirty ? "true" : undefined}>
      <th scope="row" className="models__step">
        <span className="models__role">{label}</span>
        {/* The engine's plain-language reason this step is set the way it is.
            Both selects are described by it. */}
        {row.note ? (
          <span className="models__note" id={noteId} title={row.note}>
            {row.note}
          </span>
        ) : null}
        {pinned ? (
          <span className="models__pinned">
            Same at every tier — effort buys this step nothing.
          </span>
        ) : null}
      </th>
      <td>
        <label className="visually-hidden" htmlFor={modelId}>
          {`Model for ${label}`}
        </label>
        <select
          id={modelId}
          className="select models__select"
          value={resolved.model}
          aria-describedby={row.note ? noteId : undefined}
          disabled={!editable}
          onChange={(event) => onPatch({ model: asModel(event.target.value) })}
        >
          <GroupedOptions groups={groups} current={resolved.model} />
        </select>
        {ungrounded ? (
          <Chip
            tone="caution"
            className="chip--quiet models__flag"
            title={`${label} searches the web, and this model cannot.`}
          >
            Won&rsquo;t search the web
          </Chip>
        ) : null}
      </td>
      <td>
        <label className="visually-hidden" htmlFor={effortId}>
          {`Thinking effort for ${label}`}
        </label>
        <select
          id={effortId}
          className="select models__select"
          value={resolved.effort}
          aria-describedby={row.note ? noteId : undefined}
          disabled={!editable}
          onChange={(event) => onPatch({ effort: asEffort(event.target.value) })}
        >
          {withOption(
            efforts.map((effort) => ({ value: effort, label: describeEffort(effort) })),
            resolved.effort,
            describeEffort,
          ).map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        {unsupported ? (
          <Chip
            tone="caution"
            className="chip--quiet models__flag"
            title={`This model does not offer ${describeEffort(unsupported)}.`}
          >
            Not on this model
          </Chip>
        ) : null}
      </td>
      <td className="models__reset">
        {dirty && editable ? (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            aria-label={`Reset ${label} to the ${tierLabel} table`}
            onClick={onReset}
          >
            Reset
          </button>
        ) : null}
      </td>
    </tr>
  );
}

/* --- Small helpers --------------------------------------------------------- */

type Option = {
  value: string;
  label: string;
  /** Only where a segment carries a caveat its label has no room for. */
  title?: string;
};
type Group = { label: string; options: Option[] };

/**
 * The catalog as a grouped list, with the current value in it even when the
 * catalog has forgotten it.
 *
 * A stored default naming a model that has since been retired is still what a
 * run would use, so it stays on the list under its shortened id rather than the
 * select silently resolving to whatever happens to be first.
 */
function GroupedOptions({ groups, current }: { groups: Group[]; current: string }) {
  const known = groups.some((group) =>
    group.options.some((option) => option.value === current),
  );
  const real = groups.filter((group) => group.options.length > 0);
  const named = real.filter((group) => group.label);

  return (
    <>
      {!known && current ? <option value={current}>{shortModel(current)}</option> : null}
      {named.length === real.length && real.length > 1
        ? real.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.options.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </optgroup>
          ))
        : real.flatMap((group) =>
            group.options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            )),
          )}
    </>
  );
}

/** The served list, with the current value in it even when the list forgot it. */
function withOption(
  options: Option[],
  current: string,
  labelFor: (value: string) => string,
): Option[] {
  if (!current || options.some((option) => option.value === current)) return options;
  return [{ value: current, label: labelFor(current) }, ...options];
}

/**
 * A row of mutually exclusive buttons: the app's established quick-set, the same
 * one `/how-it-works` uses for its tier switch.
 *
 * `role="group"` with `aria-pressed`, rather than a radiogroup, because these are
 * not a form field being filled in — each press takes effect immediately, and
 * every button stays in the tab order so the whole editor is reachable one key
 * at a time.
 */
function Segment({
  label,
  options,
  value,
  onChange,
  disabled = false,
}: {
  label: string;
  options: Option[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="models__seg" role="group" aria-label={label}>
      <span className="label models__seg-label">{label}</span>
      <div className="row models__seg-buttons">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            className={`btn btn--sm${option.value === value ? " btn--primary" : ""}`}
            aria-pressed={option.value === value}
            title={option.title}
            disabled={disabled}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

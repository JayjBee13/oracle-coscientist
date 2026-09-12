/**
 * The persisted system default: which models a run launched right now would use.
 *
 * Two values, deliberately kept apart, and the whole design of the top-bar
 * editor rests on the difference:
 *
 *   - **saved** is what the backend holds. It is what a run launched right now
 *     would use, what the chip in the top bar names, and what the wizard starts
 *     from. Nothing but a successful `PUT` moves it.
 *   - **draft** is what the editor is showing. It survives the popover being
 *     closed — an editor that discards a table you have just built because you
 *     clicked the page behind it is an editor nobody trusts twice — and it is
 *     never what any other screen reads.
 *
 * So a half-finished edit cannot leak into a launch, and "Save" means exactly
 * one thing: make the draft the default. The two are compared field by field to
 * decide whether there is anything to save, rather than by identity, because a
 * value changed and changed back is not a change.
 *
 * **The document holds one bucket of per-role overrides per tier.** Saving in the
 * editor redefines *the tier that is selected* — set the table up at High and
 * save, and that is what High means from then on — so `tiers` is the state and
 * `overrides` is a derived mirror of `tiers[tier]`, exactly as the backend serves
 * it. Every write here goes through `normaliseModelSettings`, so the mirror
 * cannot fall out of step with what it mirrors, and an emptied bucket is dropped
 * rather than stored: sparse means "this tier follows the built-in table", and a
 * materialised empty bucket would read as a decision nobody made.
 *
 * Same shape as `store/capabilities.ts` — a module-level store with a
 * `useSyncExternalStore` hook, one request in flight at a time, and no polling.
 * This describes the machine's configuration, not run state.
 */

import { useEffect, useSyncExternalStore } from "react";

import * as api from "../api/client";
import { errorMessage } from "../api/client";
import type { ModelSettings, ModelSettingsChoice } from "../api/types";
import { pushToast } from "../lib/toast";
import type { LoadState } from "./runs";

export type ModelSettingsState = {
  status: LoadState;
  /** What the backend holds. Null until the first successful read. */
  saved: ModelSettings | null;
  /** What the editor is showing. Null until something has been read to seed it. */
  draft: ModelSettings | null;
  saving: boolean;
  /** Why the read failed. The editor shows this in place of the table. */
  error: string | null;
  /** Why the last save failed. Shown beside the button that failed. */
  saveError: string | null;
};

const IDLE: ModelSettingsState = {
  status: "idle",
  saved: null,
  draft: null,
  saving: false,
  error: null,
  saveError: null,
};

let state: ModelSettingsState = IDLE;
const listeners = new Set<() => void>();
let inFlight: Promise<void> | null = null;
let generation = 0;

function setState(next: ModelSettingsState): void {
  state = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getModelSettingsState(): ModelSettingsState {
  return state;
}

/**
 * Reads the stored default once. Repeat calls join the request in flight.
 *
 * A reload while the editor is open would throw away an edit in progress, so the
 * draft is only seeded from a response when there is no draft yet.
 */
export function fetchModelSettings(options: { force?: boolean } = {}): Promise<void> {
  if (inFlight) return inFlight;
  if (!options.force && state.status === "ready") return Promise.resolve();

  setState({ ...state, status: "loading", error: null });
  const requestGeneration = ++generation;
  const request = api
    .getModelSettings()
    // Normalised on the way in as well as on the way out, so `saved` and `draft`
    // are the same shape and the dirty comparison has one document to reason
    // about rather than two vintages of one.
    .then((response) => {
      if (requestGeneration !== generation) return;
      const saved = normaliseModelSettings(response);
      setState({
        ...state,
        status: "ready",
        saved,
        draft: state.draft ?? saved,
        error: null,
      });
    })
    .catch((error: unknown) => {
      if (requestGeneration !== generation) return;
      // Chrome-level background work, exactly as capabilities is: it reports
      // itself where it lives and offers a retry, rather than throwing a toast
      // over whatever page you are actually reading.
      setState({ ...state, status: "error", error: errorMessage(error) });
    })
    .finally(() => {
      if (requestGeneration === generation) inFlight = null;
    });

  inFlight = request;
  return request;
}

export function refreshModelSettings(): Promise<void> {
  return fetchModelSettings({ force: true });
}

/** What the editor is showing, after one edit. */
export function editModelSettings(next: ModelSettings): void {
  setState({ ...state, draft: normaliseModelSettings(next), saveError: null });
}

/* --- One bucket per tier ---------------------------------------------------
   The three functions the editor moves a tier's overrides with. They are here
   rather than in the component because the *shape* rule is the contract this
   endpoint keeps, not a detail of one panel: `tiers` is the state, `overrides`
   is a mirror of the selected tier's bucket, and an empty bucket does not
   exist.
   ------------------------------------------------------------------------- */

/**
 * This tier's overrides — `{}` when it has none, which is the ordinary case.
 *
 * `{}` and "no bucket" are deliberately the same answer: a tier with no bucket
 * follows the built-in table, today and after the catalog moves under it.
 */
export function tierBucket(
  settings: ModelSettings,
  tier: string = settings.tier,
): Record<string, ModelSettingsChoice> {
  return settings.tiers[tier] ?? {};
}

/**
 * The document with `tiers` made sparse again and `overrides` re-derived.
 *
 * Every edit passes through here, which is what makes "the editor is showing
 * High" and "High is what this document says" one fact instead of two that agree
 * until one is written and the other is not. Dropping the emptied buckets is the
 * other half: the backend does not store an empty bucket, so a draft that held
 * one would report itself dirty against a saved value that is identical to it.
 */
export function normaliseModelSettings(next: ModelSettings): ModelSettings {
  // A response with no `tiers` at all is a backend from before the presets
  // existed — the ordinary case for a frontend built ahead of its backend, which
  // this editor already defends against elsewhere. Its `overrides` block *is*
  // that tier's bucket, which is the same read migration the service performs;
  // reading it as "no overrides anywhere" would silently drop somebody's saved
  // table. Only when the key is absent: an empty object is a real, sparse answer,
  // and re-seeding from the mirror then would undo the reset that emptied it.
  const source = next.tiers ?? { [next.tier]: next.overrides ?? {} };
  const tiers: ModelSettings["tiers"] = {};
  for (const [tier, bucket] of Object.entries(source)) {
    if (Object.keys(bucket).length > 0) tiers[tier] = bucket;
  }
  return { ...next, tiers, overrides: tiers[next.tier] ?? {} };
}

/**
 * The tiers this document redefines other than the one selected.
 *
 * The editor shows one tier at a time and a save writes all of them, so what the
 * other buckets hold has to be sayable on screen — otherwise "Save as the new
 * High" is quietly also saving a Med somebody set up last week and cannot see.
 */
export function otherEditedTiers(settings: ModelSettings): string[] {
  return Object.keys(settings.tiers).filter((tier) => tier !== settings.tier);
}

/** Throw the edit away and show what is actually stored. */
export function revertModelSettings(): void {
  setState({ ...state, draft: state.saved, saveError: null });
}

/**
 * Makes the draft the default, redefining the tier it has selected.
 *
 * Returns a boolean like the run store's actions do, so a caller handles the
 * happy path only. A failure stays *inside* the editor — `saveError` beside the
 * button — rather than becoming a toast, because the thing that failed is still
 * on screen and still holds the work.
 *
 * `tiers` goes up whole and `overrides` does not go up at all. The draft holds
 * every preset, so a save is "this is what the presets are now" rather than a
 * patch on one of them — and sending the mirror as if it were state is how the
 * two come to disagree. The buckets are sparse on the way out because they are
 * sparse in the draft: a tier nobody edited is absent, which is what tells the
 * backend it still follows the built-in table.
 */
export async function saveModelSettings(): Promise<boolean> {
  const draft = state.draft;
  if (!draft) return false;

  setState({ ...state, saving: true, saveError: null });
  const requestGeneration = ++generation;
  try {
    const saved = normaliseModelSettings(
      await api.putModelSettings({
        provider: draft.provider,
        tier: draft.tier,
        tiers: draft.tiers,
      }),
    );
    if (requestGeneration !== generation) return false;
    setState({ ...state, status: "ready", saving: false, saved, draft: saved });
    pushToast({
      tone: "go",
      title: "Saved",
      message: "New runs will use these models until you change them again.",
    });
    return true;
  } catch (error) {
    if (requestGeneration !== generation) return false;
    setState({ ...state, saving: false, saveError: errorMessage(error) });
    return false;
  }
}

/**
 * Whether the draft says anything the stored default does not.
 *
 * Field by field, and the override table key by key: `{}` and `{}` are the same
 * default however many times they have been rebuilt, and a Save button lit up by
 * object identity would be lit up permanently.
 *
 * Every bucket, not only the selected tier's. A save writes all of them, so an
 * edit to Med made before switching to High is a real unsaved change and a Save
 * button that had gone quiet would lose it.
 */
export function isModelSettingsDirty(current: ModelSettingsState = state): boolean {
  const { draft, saved } = current;
  if (!draft) return false;
  if (!saved) return true;
  return (
    draft.provider !== saved.provider ||
    draft.tier !== saved.tier ||
    !sameTiers(draft.tiers, saved.tiers)
  );
}

function sameTiers(a: ModelSettings["tiers"], b: ModelSettings["tiers"]): boolean {
  const tiers = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const tier of tiers) {
    // `?? {}` rather than a presence check: absent and empty are the same
    // preset — one that follows the built-in table — and must compare equal.
    if (!sameOverrides(a[tier] ?? {}, b[tier] ?? {})) return false;
  }
  return true;
}

function sameOverrides(
  a: Record<string, ModelSettingsChoice>,
  b: Record<string, ModelSettingsChoice>,
): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) {
    const left = a[key];
    const right = b[key];
    if (!left || !right) return false;
    if ((left.model ?? null) !== (right.model ?? null)) return false;
    if ((left.effort ?? null) !== (right.effort ?? null)) return false;
  }
  return true;
}

/**
 * Calls back with the stored default, now and whenever it changes.
 *
 * The subscription form exists for the wizard. It has its own draft in React
 * state and has to *seed* it from the stored default, which arrives over the
 * network after the wizard has mounted — and reading a value out of a store in
 * an effect body and calling `setState` with it is the cascading render React
 * warns about. Subscribing to the external system and setting state from the
 * callback is the shape React asks for, and it is also the honest description:
 * the stored default is an external system, and this is an update from it.
 *
 * Fires immediately when there is already a value, so a wizard opened after the
 * top bar has read the settings does not wait for a change that never comes.
 */
export function onModelSettings(listener: (saved: ModelSettings) => void): () => void {
  void fetchModelSettings();
  if (state.saved) listener(state.saved);
  let last = state.saved;
  return subscribe(() => {
    if (state.saved && state.saved !== last) {
      last = state.saved;
      listener(state.saved);
    }
  });
}

/** The stored default, fetched on first mount. */
export function useModelSettings(): ModelSettingsState {
  const snapshot = useSyncExternalStore(
    subscribe,
    () => state,
    () => state,
  );

  useEffect(() => {
    void fetchModelSettings();
  }, []);

  return snapshot;
}

/* --- Test support --------------------------------------------------------- */

export function resetModelSettingsStore(): void {
  generation += 1;
  inFlight = null;
  setState(IDLE);
}

/** Seeds the stored default without a request. */
export function primeModelSettings(saved: ModelSettings): void {
  generation += 1;
  const normalised = normaliseModelSettings(saved);
  setState({ ...IDLE, status: "ready", saved: normalised, draft: normalised });
}

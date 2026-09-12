/**
 * The run store — one module-level cache, `useSyncExternalStore` on top.
 *
 * Why not one fetch per component: the previous UI fetched `/runs` three to
 * eight times per page load, tore its EventSource down on every state change,
 * and left the "live" view frozen at mount. Everything that reads a run reads
 * it from here, and exactly one EventSource exists per watched run no matter
 * how many components are watching.
 *
 * The rules this file exists to enforce (contract C6):
 *   - SSE events patch the cache immediately, then trigger a **trailing-edge**
 *     detail refetch at most once a second, so a burst of events costs one
 *     request and the tiles never disagree with the log.
 *   - `round_completed`, `run_finished`, `run_failed` and `lifecycle_changed`
 *     bypass the debounce entirely — those are the moments a stale number is
 *     actually misleading.
 *   - A dropped stream reconnects with exponential backoff and resumes from
 *     the last sequence number this client saw (`?after_seq=`), so nothing is
 *     lost across a blip.
 *   - Every effect here is StrictMode-safe: subscriptions are ref-counted and
 *     in-flight work is cancelled with `AbortController` and a generation
 *     token. No `mountedRef` — that pattern is what killed the old notebook.
 */

import { useEffect, useSyncExternalStore } from "react";

import * as api from "../api/client";
import { ApiError, errorMessage, isApiError } from "../api/client";
import type {
  ControlAction,
  ControlExtension,
  Harness,
  RunDetail,
  RunEvent,
  RunSummary,
  RunsQuery,
} from "../api/types";
import { markRunSeen, notifyRunFinished, setTitleAlert } from "../lib/notifications";
import type { ConnectionState } from "../lib/status";
import { describeRunStatus, isActiveLifecycle, isLaneBlocking } from "../lib/status";
import { pushToast } from "../lib/toast";

/* --- Tunables (exported so tests assert against the real numbers) --------- */

/** Trailing-edge detail refetch window. At most one refetch per second. */
export const MIN_REFETCH_INTERVAL_MS = 1000;

/** Reconnect backoff, in order. The last value repeats. */
export const RECONNECT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000] as const;

/** Events where a stale number would mislead, so the debounce is skipped. */
export const FORCED_REFETCH_EVENT_TYPES: ReadonlySet<string> = new Set([
  "round_completed",
  "run_finished",
  "run_failed",
  "lifecycle_changed",
]);

/** The last thing a run says. After one of these there is nothing left to hear. */
export const CLOSING_EVENT_TYPES: ReadonlySet<string> = new Set([
  "run_finished",
  "run_failed",
]);

/**
 * How long a run keeps its stream open after its lifecycle stops being active.
 *
 * The engine writes the overview, *then* the lifecycle, *then* `run_finished`.
 * Hanging up the moment the lifecycle turns terminal therefore drops the run's
 * closing event on the floor, and the watcher's last line is whatever happened
 * to come before it. The drain closes as soon as that event lands, and expires
 * on its own for a run that will never send one — a force-stopped run is killed
 * before it can speak.
 */
export const TERMINAL_DRAIN_MS = 15_000;

/** Harness lanes shown in the top bar. Codex is health-probe only (plan cut). */
export const LANE_HARNESSES: readonly Harness[] = ["claude", "demo"];

const MAX_EVENTS = 500;
const LANE_REFRESH_MS = 30000;
const LANE_PAGE_SIZE = 100;

/** Synthetic type for a stream line we could not parse. */
export const MALFORMED_EVENT_TYPE = "malformed";

/* --- State shapes --------------------------------------------------------- */

export type LoadState = "idle" | "loading" | "ready" | "error";

export type RunListState = {
  key: string;
  status: LoadState;
  ids: string[];
  total: number;
  /** Present alongside `status: "ready"` when a refresh failed over good data. */
  error: string | null;
};

export type RunEntryState = {
  id: string;
  summary: RunSummary | null;
  detail: RunDetail | null;
  status: LoadState;
  error: string | null;
  events: RunEvent[];
  lastSeq: number | null;
  connection: ConnectionState;
  /** Terminal, but still owed a closing event. See TERMINAL_DRAIN_MS. */
  draining: boolean;
};

export type Lane = { harness: Harness; run: RunSummary | null };

/* --- Module state --------------------------------------------------------- */

const EMPTY_EVENTS: RunEvent[] = [];

let listState: RunListState = {
  key: "",
  status: "idle",
  ids: [],
  total: 0,
  error: null,
};

const entries = new Map<string, RunEntryState>();
const listListeners = new Set<() => void>();
const entryListeners = new Map<string, Set<() => void>>();
const globalListeners = new Set<() => void>();

let backendReachable = true;
let connectionState: ConnectionState = "idle";
let summariesVersion = 0;
let lanesCache: Lane[] = LANE_HARNESSES.map((harness) => ({ harness, run: null }));
let lanesCacheVersion = -1;
let activeCache: RunSummary[] = [];
let activeCacheVersion = -1;
let laneFetchedAt = 0;
let listRequestId = 0;
let inFlightListKey: string | null = null;
let storeGeneration = 0;

function notifyList(): void {
  for (const listener of listListeners) listener();
}

function notifyGlobal(): void {
  for (const listener of globalListeners) listener();
}

function notifyEntry(id: string): void {
  const listeners = entryListeners.get(id);
  if (listeners) for (const listener of listeners) listener();
}

function blankEntry(id: string): RunEntryState {
  return {
    id,
    summary: null,
    detail: null,
    status: "idle",
    error: null,
    events: EMPTY_EVENTS,
    lastSeq: null,
    connection: "idle",
    draining: false,
  };
}

const drainTimers = new Map<string, ReturnType<typeof setTimeout>>();

function armDrain(id: string): void {
  if (drainTimers.has(id)) return;
  drainTimers.set(
    id,
    setTimeout(() => {
      drainTimers.delete(id);
      setEntry(id, { draining: false });
    }, TERMINAL_DRAIN_MS),
  );
  setEntry(id, { draining: true });
}

function endDrain(id: string): void {
  const timer = drainTimers.get(id);
  if (timer !== undefined) {
    clearTimeout(timer);
    drainTimers.delete(id);
  }
  if (ensureEntry(id).draining) setEntry(id, { draining: false });
}

function ensureEntry(id: string): RunEntryState {
  let entry = entries.get(id);
  if (!entry) {
    entry = blankEntry(id);
    entries.set(id, entry);
  }
  return entry;
}

function setEntry(id: string, patch: Partial<RunEntryState>): RunEntryState {
  const next = { ...ensureEntry(id), ...patch };
  entries.set(id, next);
  notifyEntry(id);
  notifyGlobal();
  return next;
}

function markSummariesChanged(): void {
  summariesVersion += 1;
  notifyGlobal();
}

/* --- Connection aggregation ----------------------------------------------- */

function recomputeConnection(): void {
  let next: ConnectionState = "idle";
  if (!backendReachable) {
    next = "offline";
  } else {
    let live = false;
    let connecting = false;
    let reconnecting = false;
    for (const [id, handle] of streams) {
      if (handle.refs === 0) continue;
      const state = entries.get(id)?.connection;
      if (state === "reconnecting" || state === "offline") reconnecting = true;
      else if (state === "live") live = true;
      else if (state === "connecting") connecting = true;
    }
    if (reconnecting) next = "reconnecting";
    else if (live) next = "live";
    else if (connecting) next = "connecting";
  }
  if (next === connectionState) return;
  connectionState = next;
  notifyGlobal();
}

function setBackendReachable(reachable: boolean): void {
  if (backendReachable === reachable) return;
  backendReachable = reachable;
  recomputeConnection();
}

function noteRequestOutcome(error: unknown): void {
  // "Offline" covers both never reaching the backend and reaching something
  // that is not the API — in either case there is no usable backend, and the
  // connection chip must not claim otherwise.
  const unusable =
    isApiError(error) && (error.isOffline || error.code === "invalid_response");
  setBackendReachable(!unusable);
}

/* --- Summaries ------------------------------------------------------------ */

function mergeSummaries(items: RunSummary[]): void {
  for (const summary of items) {
    const entry = ensureEntry(summary.id);
    entries.set(summary.id, { ...entry, summary });
    notifyEntry(summary.id);
  }
  if (items.length > 0) markSummariesChanged();
}

export function getRunSummary(id: string): RunSummary | null {
  return entries.get(id)?.summary ?? null;
}

/** The whole cached entry for a run, creating an empty one if it is new. */
export function getRunEntry(id: string): RunEntryState {
  return entries.get(id) ?? ensureEntry(id);
}

/* --- Runs list ------------------------------------------------------------ */

/** Stable key for a query, and the exact object `fetchRuns` should receive. */
export function runsQueryKey(query: RunsQuery): string {
  const entriesList = Object.entries(query)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .sort(([a], [b]) => a.localeCompare(b));
  return JSON.stringify(Object.fromEntries(entriesList));
}

export async function fetchRuns(query: RunsQuery = {}): Promise<void> {
  const requestGeneration = storeGeneration;
  const key = runsQueryKey(query);
  // An identical request is already on the wire: StrictMode's mount/unmount/
  // mount, or two components asking for the same page. Joining it rather than
  // racing it is what keeps a page load to one `/runs` request.
  if (inFlightListKey === key) return;

  const requestId = ++listRequestId;
  inFlightListKey = key;
  const hadData = listState.key === key && listState.status === "ready";
  listState = {
    key,
    status: hadData ? "ready" : "loading",
    ids: hadData ? listState.ids : [],
    total: hadData ? listState.total : 0,
    error: null,
  };
  notifyList();

  try {
    const page = await api.listRuns(query);
    if (requestGeneration !== storeGeneration) return;
    if (inFlightListKey === key) inFlightListKey = null;
    if (requestId !== listRequestId) return;
    setBackendReachable(true);
    mergeSummaries(page.items);
    laneFetchedAt = Date.now();
    listState = {
      key,
      status: "ready",
      ids: page.items.map((item) => item.id),
      total: page.total,
      error: null,
    };
    notifyList();
  } catch (error) {
    if (requestGeneration !== storeGeneration) return;
    if (inFlightListKey === key) inFlightListKey = null;
    if (requestId !== listRequestId) return;
    noteRequestOutcome(error);
    listState = {
      ...listState,
      status: hadData ? "ready" : "error",
      error: errorMessage(error),
    };
    notifyList();
  }
}

export function getRunsListState(): RunListState {
  return listState;
}

export function getConnectionState(): ConnectionState {
  return connectionState;
}

/** Runs currently in the list, in server order. */
export function getRunsList(): RunSummary[] {
  return listState.ids
    .map((id) => entries.get(id)?.summary)
    .filter((summary): summary is RunSummary => summary != null);
}

/* The list screen renders summaries, not ids, and a rename or an SSE patch
   must reach it without a refetch. `useSyncExternalStore` demands a stable
   snapshot, so the resolved array is cached until either the page of ids or
   any summary changes. */
let runsListCache: RunSummary[] = [];
let runsListCacheVersion = -1;
let runsListCacheIds: string[] | null = null;

function getRunsListCached(): RunSummary[] {
  if (runsListCacheVersion !== summariesVersion || runsListCacheIds !== listState.ids) {
    runsListCache = getRunsList();
    runsListCacheVersion = summariesVersion;
    runsListCacheIds = listState.ids;
  }
  return runsListCache;
}

function subscribeListAndSummaries(listener: () => void): () => void {
  listListeners.add(listener);
  globalListeners.add(listener);
  return () => {
    listListeners.delete(listener);
    globalListeners.delete(listener);
  };
}

/* --- Run detail ----------------------------------------------------------- */

const detailRequests = new Map<string, number>();
let detailRequestCounter = 0;

export async function refreshRunDetail(id: string): Promise<void> {
  const requestGeneration = storeGeneration;
  const requestId = ++detailRequestCounter;
  detailRequests.set(id, requestId);
  const existing = ensureEntry(id);
  if (existing.status === "idle") setEntry(id, { status: "loading", error: null });

  try {
    const detail = await api.getRunDetail(id);
    if (requestGeneration !== storeGeneration) return;
    if (detailRequests.get(id) !== requestId) return;
    setBackendReachable(true);
    applyDetail(id, detail);
  } catch (error) {
    if (requestGeneration !== storeGeneration) return;
    if (detailRequests.get(id) !== requestId) return;
    noteRequestOutcome(error);
    const entry = ensureEntry(id);
    // Keep good data on screen when a refresh fails; never show zeros beside
    // an error message.
    setEntry(id, {
      status: entry.detail ? "ready" : "error",
      error: errorMessage(error),
    });
  }
}

function applyDetail(id: string, detail: RunDetail): void {
  const entry = ensureEntry(id);
  const wasActive = entry.summary !== null && isActiveLifecycle(entry.summary.lifecycle);
  const recent = entry.events.length === 0 ? [...detail.recent_events] : entry.events;
  const lastSeq =
    entry.lastSeq ?? (recent.length > 0 ? recent[recent.length - 1].seq : null);
  entries.set(id, {
    ...entry,
    summary: detail.run,
    detail,
    status: "ready",
    error: null,
    events: recent,
    lastSeq,
  });
  notifyEntry(id);
  markSummariesChanged();
  markRunSeen(id, detail.run.updated_at);

  // The lifecycle turned terminal but the closing event has not arrived yet, so
  // hold the stream open long enough to hear it.
  const stillOwed = !recent.some((event) => CLOSING_EVENT_TYPES.has(event.type));
  if (wasActive && !isActiveLifecycle(detail.run.lifecycle) && stillOwed) armDrain(id);
}

/** Fetches the detail once; repeat calls while it is already loaded are free. */
export function ensureRunDetail(id: string): void {
  const entry = entries.get(id);
  if (entry && (entry.status === "loading" || entry.status === "ready")) return;
  void refreshRunDetail(id);
}

/* --- Lanes ---------------------------------------------------------------- */

function computeLanes(): Lane[] {
  const byHarness = new Map<Harness, RunSummary>();
  for (const entry of entries.values()) {
    const run = entry.summary;
    if (!run || run.archived) continue;
    if (!isActiveLifecycle(run.lifecycle)) continue;
    const current = byHarness.get(run.harness);
    if (!current || preferLaneRun(run, current)) byHarness.set(run.harness, run);
  }
  return LANE_HARNESSES.map((harness) => ({
    harness,
    run: byHarness.get(harness) ?? null,
  }));
}

function preferLaneRun(candidate: RunSummary, current: RunSummary): boolean {
  const candidateHoldsLane = isLaneBlocking(candidate.lifecycle);
  const currentHoldsLane = isLaneBlocking(current.lifecycle);
  if (candidateHoldsLane !== currentHoldsLane) return candidateHoldsLane;
  return candidate.updated_at > current.updated_at;
}

export function getLanes(): Lane[] {
  if (lanesCacheVersion !== summariesVersion) {
    lanesCache = computeLanes();
    lanesCacheVersion = summariesVersion;
  }
  return lanesCache;
}

export function getActiveRuns(): RunSummary[] {
  if (activeCacheVersion !== summariesVersion) {
    activeCache = [...entries.values()]
      .map((entry) => entry.summary)
      .filter((run): run is RunSummary => run != null && isActiveLifecycle(run.lifecycle))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    activeCacheVersion = summariesVersion;
  }
  return activeCache;
}

/**
 * Keeps the top-bar lane chips honest without owning the list page's query:
 * merges summaries into the cache and never touches `listState`.
 */
export async function refreshLanes(options: { force?: boolean } = {}): Promise<void> {
  const requestGeneration = storeGeneration;
  if (!options.force && Date.now() - laneFetchedAt < LANE_REFRESH_MS) return;
  laneFetchedAt = Date.now();
  try {
    // The top bar names live runs. Ask explicitly for the current person's
    // workspace so an administrator does not turn the shared lane rail into a
    // feed of other scientists' titles and owners.
    const page = await api.listRuns({ mine: true, page_size: LANE_PAGE_SIZE });
    if (requestGeneration !== storeGeneration) return;
    setBackendReachable(true);
    mergeSummaries(page.items);
  } catch (error) {
    if (requestGeneration !== storeGeneration) return;
    noteRequestOutcome(error);
  }
}

/* --- Event stream --------------------------------------------------------- */

type StreamHandle = {
  refs: number;
  source: EventSource | null;
  abort: AbortController | null;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  refetchTimer: ReturnType<typeof setTimeout> | null;
  lastRefetchAt: number;
  attempt: number;
  generation: number;
};

const streams = new Map<string, StreamHandle>();

function ensureStream(id: string): StreamHandle {
  let handle = streams.get(id);
  if (!handle) {
    handle = {
      refs: 0,
      source: null,
      abort: null,
      reconnectTimer: null,
      refetchTimer: null,
      lastRefetchAt: 0,
      attempt: 0,
      generation: 0,
    };
    streams.set(id, handle);
  }
  return handle;
}

/**
 * Ref-counted subscription. Returns the unsubscribe function, so a React
 * effect can be `useEffect(() => subscribeRunStream(id), [id])` and StrictMode's
 * double mount is a connect/disconnect/connect — correct, if chatty in dev.
 */
export function subscribeRunStream(id: string): () => void {
  const handle = ensureStream(id);
  handle.refs += 1;
  if (handle.refs === 1) {
    handle.attempt = 0;
    void openStream(id);
  }
  return () => {
    handle.refs -= 1;
    if (handle.refs <= 0) {
      handle.refs = 0;
      closeStream(id);
    }
  };
}

async function openStream(id: string): Promise<void> {
  const handle = streams.get(id);
  if (!handle || handle.refs === 0) return;
  const generation = ++handle.generation;

  setEntry(id, { connection: handle.attempt === 0 ? "connecting" : "reconnecting" });
  recomputeConnection();

  const abort = new AbortController();
  handle.abort = abort;

  let ticket: string | null = null;
  try {
    ticket = (await api.createEventTicket(id, abort.signal)).ticket;
  } catch (error) {
    if (handle.generation !== generation || handle.refs === 0) return;
    const authOff = isApiError(error) && error.status >= 400 && error.status < 500;
    if (!authOff) {
      // Backend down or 5xx — back off rather than opening a doomed stream.
      noteRequestOutcome(error);
      scheduleReconnect(id, generation);
      return;
    }
    // Ticketing not enabled in this deployment; the stream may still be open.
  }

  if (handle.generation !== generation || handle.refs === 0) return;

  const entry = ensureEntry(id);
  const url = api.getRunEventsUrl(id, { ticket, afterSeq: entry.lastSeq });
  const source = new EventSource(url);
  handle.source = source;

  source.addEventListener("open", () => {
    if (handle.generation !== generation) return;
    handle.attempt = 0;
    setBackendReachable(true);
    setEntry(id, { connection: "live" });
    recomputeConnection();
  });

  source.addEventListener("run_event", (event) => {
    if (handle.generation !== generation) return;
    handleStreamMessage(id, (event as MessageEvent<string>).data);
  });

  source.addEventListener("error", () => {
    if (handle.generation !== generation) return;
    source.close();
    handle.source = null;
    scheduleReconnect(id, generation);
  });
}

function scheduleReconnect(id: string, generation: number): void {
  const handle = streams.get(id);
  if (!handle || handle.refs === 0 || handle.generation !== generation) return;
  setEntry(id, { connection: "reconnecting" });
  recomputeConnection();
  const delay =
    RECONNECT_BACKOFF_MS[Math.min(handle.attempt, RECONNECT_BACKOFF_MS.length - 1)];
  handle.attempt += 1;
  handle.reconnectTimer = setTimeout(() => {
    handle.reconnectTimer = null;
    void openStream(id);
  }, delay);
}

function closeStream(id: string): void {
  const handle = streams.get(id);
  if (!handle) return;
  handle.generation += 1;
  handle.abort?.abort();
  handle.abort = null;
  handle.source?.close();
  handle.source = null;
  if (handle.reconnectTimer !== null) {
    clearTimeout(handle.reconnectTimer);
    handle.reconnectTimer = null;
  }
  if (handle.refetchTimer !== null) {
    clearTimeout(handle.refetchTimer);
    handle.refetchTimer = null;
  }
  handle.attempt = 0;
  endDrain(id);
  setEntry(id, { connection: "idle" });
  recomputeConnection();
}

function handleStreamMessage(id: string, raw: string): void {
  let event: RunEvent;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") throw new Error("not an object");
    event = parsed as RunEvent;
    if (typeof event.seq !== "number" || typeof event.type !== "string") {
      throw new Error("missing seq or type");
    }
  } catch {
    // Malformed lines are kept, not swallowed: the Activity tab renders them
    // raw with a badge so a broken producer is visible rather than silent.
    appendEvent(id, {
      seq: -1,
      run_id: id,
      round: null,
      type: MALFORMED_EVENT_TYPE,
      payload: { raw },
      ts: new Date().toISOString(),
    });
    return;
  }

  const entry = ensureEntry(id);
  if (entry.lastSeq !== null && event.seq <= entry.lastSeq) return; // replay overlap

  const before = entry.summary?.lifecycle ?? null;
  appendEvent(id, event);
  patchFromEvent(id, event);

  // The lifecycle turns terminal one event before the run's closing event. Hold
  // the stream open across that gap rather than hanging up on the last word.
  const after = ensureEntry(id).summary?.lifecycle ?? null;
  if (
    before !== null &&
    after !== null &&
    isActiveLifecycle(before) &&
    !isActiveLifecycle(after) &&
    !CLOSING_EVENT_TYPES.has(event.type)
  ) {
    armDrain(id);
  }

  if (FORCED_REFETCH_EVENT_TYPES.has(event.type)) refetchDetailNow(id);
  else scheduleDetailRefetch(id);

  if (CLOSING_EVENT_TYPES.has(event.type)) {
    endDrain(id);
    announceFinish(id);
  }
}

function appendEvent(id: string, event: RunEvent): void {
  const entry = ensureEntry(id);
  const events = [...entry.events, event];
  entries.set(id, {
    ...entry,
    events: events.length > MAX_EVENTS ? events.slice(-MAX_EVENTS) : events,
    lastSeq: event.seq >= 0 ? event.seq : entry.lastSeq,
  });
  notifyEntry(id);
}

/**
 * Cheap, always-correct patches only. Anything derived (counts, Elo, budget)
 * waits for the refetch — a log line must never invent a number.
 */
function patchFromEvent(id: string, event: RunEvent): void {
  const entry = ensureEntry(id);
  if (!entry.summary) return;

  let summary = entry.summary;
  const lifecycle = event.payload?.lifecycle;
  if (typeof lifecycle === "string" && lifecycle !== summary.lifecycle) {
    summary = { ...summary, lifecycle: lifecycle as RunSummary["lifecycle"] };
  }
  if (typeof event.round === "number" && event.round > summary.round) {
    summary = { ...summary, round: event.round };
  }
  if (summary === entry.summary) return;

  const detail = entry.detail ? { ...entry.detail, run: summary } : entry.detail;
  entries.set(id, { ...entry, summary, detail });
  notifyEntry(id);
  markSummariesChanged();
}

function announceFinish(id: string): void {
  const run = entries.get(id)?.summary;
  if (!run) return;
  const status = describeRunStatus(run);
  setTitleAlert(`${status.label}: ${run.title}`);
  notifyRunFinished(run, status.label);
}

/* --- Detail refetch scheduling -------------------------------------------- */

/**
 * Trailing edge, at most one per `MIN_REFETCH_INTERVAL_MS`. A burst of events
 * inside one tick arms a single refetch; a steady stream still refetches once a
 * second rather than starving.
 */
function scheduleDetailRefetch(id: string): void {
  const handle = ensureStream(id);
  if (handle.refetchTimer !== null) return;
  const wait = Math.max(0, handle.lastRefetchAt + MIN_REFETCH_INTERVAL_MS - Date.now());
  handle.refetchTimer = setTimeout(() => {
    handle.refetchTimer = null;
    runDetailRefetch(id);
  }, wait);
}

function refetchDetailNow(id: string): void {
  const handle = ensureStream(id);
  if (handle.refetchTimer !== null) {
    clearTimeout(handle.refetchTimer);
    handle.refetchTimer = null;
  }
  runDetailRefetch(id);
}

function runDetailRefetch(id: string): void {
  const handle = ensureStream(id);
  handle.lastRefetchAt = Date.now();
  void refreshRunDetail(id);
}

/* --- Mutations ------------------------------------------------------------ */

/**
 * What a control did, and — when it did not — what went wrong.
 *
 * The error travels back rather than only into a toast because `continue` is
 * refused in ways the caller can act on: a budget with no headroom is a
 * correction to a field the scientist is still looking at, and a busy lane names
 * a run they may want to open. A toast is the right home for "pause did not go
 * through" and the wrong one for either of those.
 */
export type ControlOutcome = { ok: boolean; error: unknown };

export async function sendRunControl(
  id: string,
  action: ControlAction,
  extension?: ControlExtension,
  options: { handled?: boolean } = {},
): Promise<ControlOutcome> {
  const requestGeneration = storeGeneration;
  try {
    const response = await api.sendControl(id, action, extension);
    if (requestGeneration !== storeGeneration) return { ok: false, error: null };
    setBackendReachable(true);
    const entry = ensureEntry(id);
    if (entry.summary && response.lifecycle) {
      // The lifecycle in the response is the one the run is in *now*, which for
      // `continue` is `queued` — an active lifecycle, so patching it here is
      // what remounts the event stream on a run that had already finished.
      mergeSummaries([{ ...entry.summary, lifecycle: response.lifecycle }]);
    }
    refetchDetailNow(id);
    return { ok: response.accepted, error: null };
  } catch (error) {
    if (requestGeneration !== storeGeneration) return { ok: false, error: null };
    noteRequestOutcome(error);
    // `handled` means the caller is going to put this in front of the user
    // itself, in a place that can say more about it than a toast can.
    if (!options.handled) {
      pushToast({
        tone: "danger",
        title: "That control did not go through",
        message: errorMessage(error),
      });
    }
    return { ok: false, error };
  }
}

export async function saveRunPatch(
  id: string,
  patch: { title?: string; archived?: boolean },
): Promise<boolean> {
  const requestGeneration = storeGeneration;
  try {
    const summary = await api.patchRun(id, patch);
    if (requestGeneration !== storeGeneration) return false;
    setBackendReachable(true);
    mergeSummaries([summary]);
    return true;
  } catch (error) {
    if (requestGeneration !== storeGeneration) return false;
    noteRequestOutcome(error);
    pushToast({
      tone: "danger",
      title: "Could not save that change",
      message: errorMessage(error),
    });
    return false;
  }
}

/**
 * Soft-deletes a run and drops it from the cache, so the list it was on updates
 * without a refetch and the workspace it was open in can navigate away.
 */
export async function deleteRunById(id: string): Promise<boolean> {
  const requestGeneration = storeGeneration;
  try {
    await api.deleteRun(id);
    if (requestGeneration !== storeGeneration) return false;
    setBackendReachable(true);
    const stream = streams.get(id);
    if (stream) {
      stream.refs = 0;
      closeStream(id);
      streams.delete(id);
    }
    entries.delete(id);
    listState = { ...listState, ids: listState.ids.filter((entryId) => entryId !== id) };
    markSummariesChanged();
    notifyList();
    pushToast({ tone: "info", title: "Run deleted" });
    return true;
  } catch (error) {
    if (requestGeneration !== storeGeneration) return false;
    noteRequestOutcome(error);
    pushToast({
      tone: "danger",
      title: "Could not delete that run",
      message: errorMessage(error),
    });
    return false;
  }
}

export async function addRunNote(id: string, text: string): Promise<boolean> {
  const requestGeneration = storeGeneration;
  try {
    await api.postNote(id, text);
    if (requestGeneration !== storeGeneration) return false;
    setBackendReachable(true);
    pushToast({
      tone: "go",
      title: "Note sent",
      message: "It will be read into the next round as top-priority guidance.",
    });
    return true;
  } catch (error) {
    if (requestGeneration !== storeGeneration) return false;
    noteRequestOutcome(error);
    pushToast({
      tone: "danger",
      title: "Could not send that note",
      message: errorMessage(error),
    });
    return false;
  }
}

export async function archiveRunHypothesis(id: string, hid: string): Promise<boolean> {
  const requestGeneration = storeGeneration;
  try {
    await api.archiveHypothesis(id, hid);
    if (requestGeneration !== storeGeneration) return false;
    setBackendReachable(true);
    refetchDetailNow(id);
    return true;
  } catch (error) {
    if (requestGeneration !== storeGeneration) return false;
    noteRequestOutcome(error);
    pushToast({
      tone: "danger",
      title: "Could not archive that hypothesis",
      message: errorMessage(error),
    });
    return false;
  }
}

export async function haltAllRuns(): Promise<string[]> {
  const requestGeneration = storeGeneration;
  try {
    const response = await api.haltAll();
    if (requestGeneration !== storeGeneration) return [];
    setBackendReachable(true);
    await refreshLanes({ force: true });
    if (requestGeneration !== storeGeneration) return [];
    pushToast({
      tone: response.stopped.length > 0 ? "caution" : "info",
      title:
        response.stopped.length > 0
          ? `Stopping ${response.stopped.length} run${response.stopped.length === 1 ? "" : "s"}`
          : "Nothing was running",
      message:
        response.stopped.length > 0
          ? "Each one still writes a report from what exists."
          : undefined,
    });
    return response.stopped;
  } catch (error) {
    if (requestGeneration !== storeGeneration) return [];
    noteRequestOutcome(error);
    pushToast({
      tone: "danger",
      title: "Stop all failed",
      message: errorMessage(error),
    });
    throw error instanceof ApiError ? error : new ApiError(errorMessage(error));
  }
}

/* --- Subscriptions -------------------------------------------------------- */

function subscribeList(listener: () => void): () => void {
  listListeners.add(listener);
  return () => {
    listListeners.delete(listener);
  };
}

function subscribeGlobal(listener: () => void): () => void {
  globalListeners.add(listener);
  return () => {
    globalListeners.delete(listener);
  };
}

function subscribeEntry(id: string): (listener: () => void) => () => void {
  return (listener) => {
    let listeners = entryListeners.get(id);
    if (!listeners) {
      listeners = new Set();
      entryListeners.set(id, listeners);
    }
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) entryListeners.delete(id);
    };
  };
}

/* --- Hooks ---------------------------------------------------------------- */

/** The runs list for a query. Refetches whenever the query changes. */
export function useRuns(query: RunsQuery = {}): RunListState {
  const key = runsQueryKey(query);
  const state = useSyncExternalStore(
    subscribeList,
    () => listState,
    () => listState,
  );

  useEffect(() => {
    void fetchRuns(JSON.parse(key) as RunsQuery);
  }, [key]);

  return state;
}

/**
 * The summaries behind `useRuns`, in server order. Separate from `useRuns` so
 * the list screen re-renders on a summary change (a rename, a live patch)
 * without refetching the page.
 */
export function useRunsSummaries(): RunSummary[] {
  return useSyncExternalStore(
    subscribeListAndSummaries,
    getRunsListCached,
    getRunsListCached,
  );
}

/** One run, fetched on demand. Does not open the event stream. */
export function useRun(id: string): RunEntryState {
  const snapshot = (): RunEntryState => entries.get(id) ?? ensureEntry(id);
  const entry = useSyncExternalStore(subscribeEntry(id), snapshot, snapshot);

  useEffect(() => {
    ensureRunDetail(id);
  }, [id]);

  return entry;
}

/** Opens (and ref-counts) the live event stream for a run. */
export function useRunEvents(id: string): {
  events: RunEvent[];
  connection: ConnectionState;
  lastSeq: number | null;
} {
  const snapshot = (): RunEntryState => entries.get(id) ?? ensureEntry(id);
  const entry = useSyncExternalStore(subscribeEntry(id), snapshot, snapshot);

  useEffect(() => subscribeRunStream(id), [id]);

  return { events: entry.events, connection: entry.connection, lastSeq: entry.lastSeq };
}

/** Aggregate connection state for the top-bar chip. */
export function useConnection(): ConnectionState {
  return useSyncExternalStore(
    subscribeGlobal,
    () => connectionState,
    () => connectionState,
  );
}

/** Harness lanes for the top bar, kept fresh while the shell is mounted. */
export function useLanes(): Lane[] {
  const lanes = useSyncExternalStore(subscribeGlobal, getLanes, getLanes);

  useEffect(() => {
    void refreshLanes();
    const timer = setInterval(() => void refreshLanes({ force: true }), LANE_REFRESH_MS);
    return () => clearInterval(timer);
  }, []);

  return lanes;
}

/** Every run still in flight, newest first — what "Stop all" would affect. */
export function useActiveRuns(): RunSummary[] {
  return useSyncExternalStore(subscribeGlobal, getActiveRuns, getActiveRuns);
}

/* --- Test support --------------------------------------------------------- */

export function resetRunStore(): void {
  storeGeneration += 1;
  for (const id of [...streams.keys()]) {
    const handle = streams.get(id);
    if (!handle) continue;
    handle.refs = 0;
    closeStream(id);
  }
  streams.clear();
  entries.clear();
  entryListeners.clear();
  detailRequests.clear();
  listState = { key: "", status: "idle", ids: [], total: 0, error: null };
  backendReachable = true;
  connectionState = "idle";
  summariesVersion = 0;
  lanesCacheVersion = -1;
  activeCacheVersion = -1;
  runsListCacheVersion = -1;
  runsListCacheIds = null;
  laneFetchedAt = 0;
  inFlightListKey = null;
  setTitleAlert(null);
  notifyList();
  notifyGlobal();
}

/** Seeds the cache without a request. Used by tests and by optimistic paths. */
export function primeRuns(items: RunSummary[]): void {
  mergeSummaries(items);
}

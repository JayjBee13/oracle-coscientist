/**
 * Typed client for the C5 API surface.
 *
 * Every request funnels through `request()` so the `{code, message, details}`
 * error envelope is parsed in exactly one place and every failure — including
 * "the backend is not running" — arrives as an `ApiError` with a sentence a
 * scientist can read. Callers never see a bare `TypeError: Failed to fetch`.
 */

import type {
  Capabilities,
  CompareAnalytics,
  ContextDocRef,
  CurrentIdentity,
  ControlAction,
  ControlExtension,
  ControlResponse,
  CreateRunInput,
  ErrorEnvelope,
  EventTicket,
  HaltAllResponse,
  Harness,
  Health,
  HypothesisDetail,
  HypothesisRow,
  MatchRow,
  ModelSettings,
  ModelSettingsUpdate,
  RunDetail,
  RunGraph,
  RunListPage,
  RunSummary,
  RunsQuery,
  Workshop,
} from "./types";

const DEV_BASE_URL = "http://127.0.0.1:8787/api";

export function getApiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL as string | undefined;
  if (configured) return configured.replace(/\/$/, "");
  return import.meta.env.DEV ? DEV_BASE_URL : "/api";
}

function getApiToken(): string | undefined {
  return import.meta.env.VITE_API_TOKEN as string | undefined;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: unknown;

  constructor(
    message: string,
    options: { code?: string; status?: number; details?: unknown } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.code = options.code ?? "unknown_error";
    this.status = options.status ?? 0;
    this.details = options.details ?? null;
  }

  /** True when the request never reached the server. */
  get isOffline(): boolean {
    return this.status === 0;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

type AuthenticationFailureListener = (error: ApiError) => void;
const authenticationFailureListeners = new Set<AuthenticationFailureListener>();
let workspaceRequestEpoch = 0;

/** Invalidates every protected request started for the previous workspace. */
export function advanceWorkspaceRequestEpoch(): void {
  workspaceRequestEpoch += 1;
}

function staleRequest<T>(): Promise<T> {
  // An obsolete component must neither receive old private data nor turn a
  // deliberate workspace transition into an error toast. Leaving its detached
  // promise unsettled prevents both continuations; the component and request
  // references are then collectible together.
  return new Promise<T>(() => {});
}

/**
 * Lets the identity boundary fail closed when any protected request discovers
 * that the gateway session is no longer valid. Ordinary permission denials do
 * not cross this boundary.
 */
export function onAuthenticationFailure(
  listener: AuthenticationFailureListener,
): () => void {
  authenticationFailureListeners.add(listener);
  return () => authenticationFailureListeners.delete(listener);
}

function isAuthenticationFailure(error: ApiError): boolean {
  return (
    error.status === 401 ||
    error.code === "gateway_auth_required" ||
    error.code === "invalid_gateway_secret" ||
    error.code === "origin_not_allowed"
  );
}

function reportAuthenticationFailure(error: ApiError): void {
  if (!isAuthenticationFailure(error)) return;
  for (const listener of authenticationFailureListeners) listener(error);
}

/** A sentence fit to put on screen. Never leaks "[object Object]". */
export function errorMessage(error: unknown): string {
  if (isApiError(error)) return error.message;
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "Something went wrong.";
}

type QueryValue = string | number | boolean | undefined | null;

export function buildQuery(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

export function apiUrl(path: string, params: Record<string, QueryValue> = {}): string {
  return `${getApiBaseUrl()}${path}${buildQuery(params)}`;
}

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, QueryValue>;
  signal?: AbortSignal;
  /** Parse the response as text instead of JSON (markdown endpoints). */
  as?: "json" | "text" | "void";
};

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, query, signal, as = "json" } = options;
  const headers: Record<string, string> = {};
  const requestEpoch =
    path === "/me" || path === "/health" ? null : workspaceRequestEpoch;
  const isStale = (): boolean =>
    requestEpoch !== null && requestEpoch !== workspaceRequestEpoch;
  const token = getApiToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetch(apiUrl(path, query ?? {}), {
      method,
      headers,
      signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    if (isStale()) return staleRequest<T>();
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(
      "Could not reach the Oracle backend. Check that it is running, then try again.",
      { code: "backend_unreachable", status: 0, details: cause },
    );
  }

  if (isStale()) return staleRequest<T>();

  if (!response.ok) {
    const error = await readErrorEnvelope(path, response);
    if (isStale()) return staleRequest<T>();
    reportAuthenticationFailure(error);
    if (isStale()) return staleRequest<T>();
    throw error;
  }

  if (as === "void") return undefined as T;
  if (as === "text") {
    const value = await response.text();
    return isStale() ? staleRequest<T>() : (value as T);
  }
  if (response.status === 204) return undefined as T;

  try {
    const value = (await response.json()) as T;
    return isStale() ? staleRequest<T>() : value;
  } catch (cause) {
    if (isStale()) return staleRequest<T>();
    // A 200 that is not JSON means we are talking to something other than the
    // API — a dev server's SPA fallback, a proxy error page. Saying so beats
    // an unhandled SyntaxError and a UI that claims to be connected.
    throw new ApiError(
      `The backend answered ${path} with something this app could not read. It may not be the Oracle API.`,
      { code: "invalid_response", status: response.status, details: cause },
    );
  }
}

async function readErrorEnvelope(path: string, response: Response): Promise<ApiError> {
  let envelope: Partial<ErrorEnvelope> | null = null;
  try {
    const parsed: unknown = await response.json();
    if (parsed && typeof parsed === "object") envelope = parsed as Partial<ErrorEnvelope>;
  } catch {
    envelope = null;
  }
  const message =
    typeof envelope?.message === "string" && envelope.message.trim()
      ? envelope.message
      : `Request to ${path} failed (${response.status}).`;
  return new ApiError(message, {
    code: typeof envelope?.code === "string" ? envelope.code : `http_${response.status}`,
    status: response.status,
    details: envelope?.details ?? null,
  });
}

/* --- Health / capabilities ------------------------------------------------ */

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return request<Health>("/health", { signal });
}

export function getCapabilities(signal?: AbortSignal): Promise<Capabilities> {
  return request<Capabilities>("/capabilities", { signal });
}

export function getCurrentIdentity(signal?: AbortSignal): Promise<CurrentIdentity> {
  return request<CurrentIdentity>("/me", { signal });
}

/* --- Settings --------------------------------------------------------------
   The persisted system default: which provider, which tier, and any step moved
   off that tier's own table. One document, read and replaced whole — there is a
   single operator, and a PATCH of a table like this is a merge nobody can
   predict from the screen they are looking at.
   -------------------------------------------------------------------------- */

export function getModelSettings(signal?: AbortSignal): Promise<ModelSettings> {
  return request<ModelSettings>("/settings/models", { signal });
}

/**
 * Replaces the stored default, every preset of it.
 *
 * The body's own generated type, narrowed to the three fields this app sends.
 * `overrides` is deliberately not among them: the response carries it as a
 * read-only mirror of `tiers[tier]`, and a client that sent the mirror back as
 * state would be answering "what is High" twice in one request.
 */
export function putModelSettings(
  body: Pick<ModelSettingsUpdate, "provider" | "tier" | "tiers">,
  signal?: AbortSignal,
): Promise<ModelSettings> {
  return request<ModelSettings>("/settings/models", { method: "PUT", body, signal });
}

/* --- Runs ----------------------------------------------------------------- */

export function listRuns(
  query: RunsQuery = {},
  signal?: AbortSignal,
): Promise<RunListPage> {
  return request<RunListPage>("/runs", { query: { ...query }, signal });
}

export function createRun(
  input: CreateRunInput,
  signal?: AbortSignal,
): Promise<RunDetail> {
  return request<RunDetail>("/runs", { method: "POST", body: input, signal });
}

export function getRun(runId: string, signal?: AbortSignal): Promise<RunSummary> {
  return request<RunSummary>(`/runs/${encodeURIComponent(runId)}`, { signal });
}

export function getRunDetail(runId: string, signal?: AbortSignal): Promise<RunDetail> {
  return request<RunDetail>(`/runs/${encodeURIComponent(runId)}/detail`, { signal });
}

/**
 * Every idea in a run, its descent and the meta the legend needs, in one round
 * trip — so the genealogy canvas never assembles itself from four calls.
 */
export function getRunGraph(runId: string, signal?: AbortSignal): Promise<RunGraph> {
  return request<RunGraph>(`/runs/${encodeURIComponent(runId)}/graph`, { signal });
}

export function patchRun(
  runId: string,
  patch: { title?: string; archived?: boolean },
  signal?: AbortSignal,
): Promise<RunSummary> {
  return request<RunSummary>(`/runs/${encodeURIComponent(runId)}`, {
    method: "PATCH",
    body: patch,
    signal,
  });
}

export function deleteRun(runId: string, signal?: AbortSignal): Promise<void> {
  return request<void>(`/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
    as: "void",
    signal,
  });
}

/**
 * `extension` belongs to `continue` alone and the server enforces that: sending
 * `add_rounds` with any other action is a 422, not a field quietly dropped.
 */
export function sendControl(
  runId: string,
  action: ControlAction,
  extension?: ControlExtension,
  signal?: AbortSignal,
): Promise<ControlResponse> {
  return request<ControlResponse>(`/runs/${encodeURIComponent(runId)}/controls`, {
    method: "POST",
    body: { action, ...(extension ?? {}) },
    signal,
  });
}

export function postNote(
  runId: string,
  text: string,
  signal?: AbortSignal,
): Promise<void> {
  return request<void>(`/runs/${encodeURIComponent(runId)}/note`, {
    method: "POST",
    body: { text },
    as: "void",
    signal,
  });
}

export function archiveHypothesis(
  runId: string,
  hid: string,
  signal?: AbortSignal,
): Promise<void> {
  return request<void>(
    `/runs/${encodeURIComponent(runId)}/hypotheses/${encodeURIComponent(hid)}/archive`,
    { method: "POST", as: "void", signal },
  );
}

export function listHypotheses(
  runId: string,
  query: { status?: string; sort?: string } = {},
  signal?: AbortSignal,
): Promise<HypothesisRow[]> {
  return request<HypothesisRow[]>(`/runs/${encodeURIComponent(runId)}/hypotheses`, {
    query: { ...query },
    signal,
  });
}

export function getHypothesis(
  hypothesisId: string,
  signal?: AbortSignal,
): Promise<HypothesisDetail> {
  return request<HypothesisDetail>(`/hypotheses/${encodeURIComponent(hypothesisId)}`, {
    signal,
  });
}

export function listMatches(
  runId: string,
  query: { round?: number } = {},
  signal?: AbortSignal,
): Promise<MatchRow[]> {
  return request<MatchRow[]>(`/runs/${encodeURIComponent(runId)}/matches`, {
    query: { ...query },
    signal,
  });
}

export function getOverview(runId: string, signal?: AbortSignal): Promise<string> {
  return request<string>(`/runs/${encodeURIComponent(runId)}/overview`, {
    as: "text",
    signal,
  });
}

export function listContextDocs(
  runId: string,
  signal?: AbortSignal,
): Promise<ContextDocRef[]> {
  return request<ContextDocRef[]>(`/runs/${encodeURIComponent(runId)}/context`, {
    signal,
  });
}

export function getExportUrl(runId: string, top?: number): string {
  return apiUrl(`/runs/${encodeURIComponent(runId)}/export.md`, { top });
}

export function getArtifactUrl(runId: string, path: string): string {
  return apiUrl(`/artifacts/${encodeURIComponent(runId)}/${path}`);
}

/* --- Event stream --------------------------------------------------------- */

export function createEventTicket(
  runId: string,
  signal?: AbortSignal,
): Promise<EventTicket> {
  return request<EventTicket>(`/runs/${encodeURIComponent(runId)}/events/ticket`, {
    method: "POST",
    signal,
  });
}

/**
 * URL for the SSE stream. `after_seq` resumes from the last event this client
 * actually saw — the reconnect path depends on it.
 */
export function getRunEventsUrl(
  runId: string,
  options: { ticket?: string | null; afterSeq?: number | null } = {},
): string {
  return apiUrl(`/runs/${encodeURIComponent(runId)}/events`, {
    ticket: options.ticket ?? undefined,
    after_seq: options.afterSeq ?? undefined,
    token: options.ticket ? undefined : getApiToken(),
  });
}

/* --- Workshops ------------------------------------------------------------ */

export function createWorkshop(
  input: {
    question: string;
    harness?: Harness;
    context_docs?: { name: string; text: string }[];
  },
  signal?: AbortSignal,
): Promise<Workshop> {
  return request<Workshop>("/workshops", { method: "POST", body: input, signal });
}

export function getWorkshop(workshopId: string, signal?: AbortSignal): Promise<Workshop> {
  return request<Workshop>(`/workshops/${encodeURIComponent(workshopId)}`, { signal });
}

export function refineWorkshop(
  workshopId: string,
  input: { base: string; note: string },
  signal?: AbortSignal,
): Promise<Workshop> {
  return request<Workshop>(`/workshops/${encodeURIComponent(workshopId)}/refine`, {
    method: "POST",
    body: input,
    signal,
  });
}

export function chooseWorkshopOption(
  workshopId: string,
  input: { option_id: string; final_prompt: string },
  signal?: AbortSignal,
): Promise<{ prompt: string }> {
  return request<{ prompt: string }>(
    `/workshops/${encodeURIComponent(workshopId)}/choose`,
    { method: "POST", body: input, signal },
  );
}

/* --- Compare -------------------------------------------------------------- */

export function getCompare(
  baseline: string,
  challenger: string,
  signal?: AbortSignal,
): Promise<CompareAnalytics> {
  return request<CompareAnalytics>("/compare", {
    query: { baseline, challenger },
    signal,
  });
}

/* --- Admin ---------------------------------------------------------------- */

export function haltAll(signal?: AbortSignal): Promise<HaltAllResponse> {
  return request<HaltAllResponse>("/admin/halt-all", { method: "POST", signal });
}

export function reimport(signal?: AbortSignal): Promise<{ imported: number }> {
  return request<{ imported: number }>("/admin/reimport", { method: "POST", signal });
}

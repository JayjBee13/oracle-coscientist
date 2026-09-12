/**
 * SUPERSEDED — the pre-overhaul API client, kept unimported for reference only
 * while Wave 5B ports the last screens. `./client.ts` + `./types.ts` are the
 * live contract. Delete this file once the wizard and compare views have landed.
 *
 * The one helper it used from `utils/runDisplay` is inlined below so nothing
 * else in the tree has to keep the old shapes alive.
 */

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8787/api";

function visibleRuns(runs: NormalizedRun[]): NormalizedRun[] {
  return runs.filter((run) => !/^(fake|dry)-/.test(run.engine_run_id));
}

export type HealthResponse = {
  status: "ok";
  app_env: string;
  database_name: string;
  fake_harness_enabled: boolean;
  engine_roots: Record<"v1" | "v2", string>;
  execution_roots: Record<"v1" | "v2", string>;
};

export type HypothesisSummary = {
  id: string;
  title: string;
  file: string | null;
  elo: number | null;
  matches: number;
  wins: number;
  status: string;
  verdict: string | null;
  cluster: string | null;
  created_iter: number | null;
  parent: string | null;
};

export type NormalizedRun = {
  id: string;
  engine_run_id: string;
  harness: "claude" | "codex";
  version: "v1" | "v2";
  lifecycle: string;
  title: string;
  goal: string;
  paths: {
    root: string;
    run_dir: string;
    state_json: string;
  };
  settings: {
    grounding_mode: string;
    grounding_depth: string;
    rounds_target: number | null;
    budget: number | null;
    matches_per_round: number | null;
    top_k: number | null;
  };
  progress: {
    iteration: number | null;
    calls_used: number | null;
    budget: number | null;
    phase: string;
    percent_budget: number | null;
  };
  counts: {
    active: number;
    rejected: number;
    archived: number;
    total: number;
    matches: number;
    clusters: number;
  };
  top: HypothesisSummary[];
  graft: {
    applicable: boolean;
    enabled: boolean;
    fired_count: number;
    pending_injection: boolean;
  };
  artifacts: Array<{
    id: string;
    path: string;
    kind: string;
    size: number | null;
    mtime: string | null;
  }>;
  process: {
    active: boolean;
    pid: number | null;
    state: string;
    last_event_at: string | null;
  };
  warnings: string[];
};

export type ComparisonGroupMemberRole = "baseline" | "challenger";

export type ComparisonGroupMember = {
  run_id: string;
  role: ComparisonGroupMemberRole | (string & {});
  engine_run_id: string;
  version: NormalizedRun["version"];
  title: string;
};

export type ComparisonGroup = {
  id: string;
  name: string;
  base_prompt: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  members: ComparisonGroupMember[];
};

export type ComparisonAnalytics = {
  baseline: ComparisonRunDigest;
  challenger: ComparisonRunDigest;
  deltas: Array<{ label: string; value: number }>;
  elo_leaders: ComparisonHypothesis[];
  top_idea_movement: Array<{
    id: string;
    title: string;
    rank: number;
    previous_rank: number | null;
    delta: number | null;
  }>;
  genealogy: Array<{
    run_role: "baseline" | "challenger";
    parent_links: number;
    seed_count: number;
    lineage: Array<{ id: string; parent: string }>;
  }>;
  cartographer: Array<{
    run_role: "baseline" | "challenger";
    applicable: boolean;
    enabled: boolean;
    fired_count: number;
    pending_injection: boolean;
    clusters: number;
    archived: number;
    collapse_events: number;
  }>;
};

export type ComparisonRunDigest = {
  run_id: string;
  engine_run_id: string;
  version: NormalizedRun["version"];
  title: string;
  hypothesis_count: number;
  active_count: number;
  match_count: number;
  calls_used: number | null;
  top_elo: number | null;
};

export type ComparisonHypothesis = {
  run_role: "baseline" | "challenger";
  rank: number;
  id: string;
  title: string;
  elo: number | null;
  matches: number;
  wins: number;
  status: string;
  cluster: string | null;
  parent: string | null;
};

export type CreateComparisonGroupInput = {
  name: string;
  base_prompt?: string | null;
  notes?: string | null;
  members: Array<{
    run_id: string;
    role: ComparisonGroupMemberRole;
  }>;
};

export type GroundingCapabilities = {
  built_in_web: {
    available: boolean;
    checked_at: string;
    notes: string | null;
  };
  perplexity: {
    available: boolean;
    checked_at: string;
    notes: string | null;
  };
};

export type CreateRunInput = {
  workshop_id?: string | null;
  title: string;
  harness: "claude" | "codex";
  version: "v1" | "v2";
  mode?: "co_scientist";
  final_prompt: string;
  constraints?: string[];
  grounding?: {
    mode: "built_in_web" | "perplexity";
    depth: "shallow" | "standard" | "deep";
    allow_fallback: boolean;
  };
  settings?: {
    rounds: number;
    budget: number;
    matches_per_round: number;
    top_k: number;
    stop_after_current_round?: boolean;
  };
};

export type RunDryRunPlan = {
  run_id: string;
  process_id: string;
  engine_run_id: string;
  harness: "claude" | "codex";
  version: "v1" | "v2";
  engine_root: string;
  cwd: string;
  argv: string[];
  real_runs_enabled: boolean;
  would_execute: boolean;
};

export type RunControlResponse = {
  run_id: string;
  action: "pause" | "stop" | "continue" | "extend" | "finish" | "clone";
  lifecycle: string;
  process_state: string | null;
};

export type RunEvent = {
  id: string;
  event: string;
  data: {
    run_id?: string;
    process_id?: string;
    engine_run_id?: string;
    stream?: string;
    phase?: string;
    lifecycle?: string;
    ts?: string;
    payload?: Record<string, unknown>;
  };
};

export type RunEventStreamOptions = {
  watch?: boolean;
  replay?: boolean;
  pollIntervalSeconds?: number;
  maxSeconds?: number;
};

export type PromptOption = {
  id: string;
  label: string;
  prompt: string;
  scoring_criteria: Record<string, unknown>;
  recommended_settings: Record<string, unknown>;
};

export type PromptWorkshop = {
  id: string;
  harness: "claude" | "codex";
  intent: string;
  status: "draft" | "refining" | "options_ready" | "prompt_ready" | "failed";
  selected_option: string | null;
  final_prompt: string | null;
  options: PromptOption[];
};

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail: unknown = null,
  ) {
    super(message);
  }
}

export function getApiBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`);

  if (!response.ok) {
    throw await apiError(path, response);
  }

  return (await response.json()) as T;
}

async function fetchText(path: string): Promise<string> {
  const response = await fetch(`${getApiBaseUrl()}${path}`);

  if (!response.ok) {
    throw await apiError(path, response);
  }

  return response.text();
}

async function sendJson<T>(path: string, method: "POST" | "PATCH", body: unknown): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw await apiError(path, response);
  }

  return (await response.json()) as T;
}

export function getHealth(): Promise<HealthResponse> {
  return fetchJson<HealthResponse>("/health");
}

export async function getRuns(): Promise<NormalizedRun[]> {
  return visibleRuns(await fetchJson<NormalizedRun[]>("/runs"));
}

export async function getRunEvents(runId: string): Promise<RunEvent[]> {
  const text = await fetchText(`/runs/${encodeURIComponent(runId)}/events`);
  return parseServerSentEvents(text);
}

export function getRunEventsUrl(
  runId: string,
  options: RunEventStreamOptions = {},
): string {
  const baseUrl = `${getApiBaseUrl()}/runs/${encodeURIComponent(runId)}/events`;
  const origin = typeof window === "undefined" ? "http://127.0.0.1" : window.location.origin;
  const url = new URL(baseUrl, origin);

  if (options.watch !== undefined) {
    url.searchParams.set("watch", String(options.watch));
  }
  if (options.replay !== undefined) {
    url.searchParams.set("replay", String(options.replay));
  }
  if (options.pollIntervalSeconds !== undefined) {
    url.searchParams.set("poll_interval_seconds", String(options.pollIntervalSeconds));
  }
  if (options.maxSeconds !== undefined) {
    url.searchParams.set("max_seconds", String(options.maxSeconds));
  }

  return url.toString();
}

export function getComparisonGroups(): Promise<ComparisonGroup[]> {
  return fetchJson<ComparisonGroup[]>("/comparison-groups");
}

export function getComparisonAnalytics(
  baselineId: string,
  challengerId: string,
): Promise<ComparisonAnalytics> {
  const params = new URLSearchParams({
    baseline_id: baselineId,
    challenger_id: challengerId,
  });
  return fetchJson<ComparisonAnalytics>(`/comparison-groups/analytics?${params.toString()}`);
}

export function createComparisonGroup(
  input: CreateComparisonGroupInput,
): Promise<ComparisonGroup> {
  return sendJson<ComparisonGroup>("/comparison-groups", "POST", input);
}

export function getGroundingCapabilities(): Promise<GroundingCapabilities> {
  return fetchJson<GroundingCapabilities>("/capabilities/grounding");
}

export type CreateRunOptions = {
  real?: boolean;
  background?: boolean;
};

export function createRun(
  input: CreateRunInput,
  options: CreateRunOptions = {},
): Promise<NormalizedRun> {
  return sendJson<NormalizedRun>(withQuery("/runs", options), "POST", input);
}

export function createRunDryRun(input: CreateRunInput): Promise<RunDryRunPlan> {
  return sendJson<RunDryRunPlan>("/runs/dry-run", "POST", input);
}

export function controlRun(
  runId: string,
  input: {
    action: "pause" | "stop" | "continue" | "extend" | "finish" | "clone";
    reason?: string;
  },
): Promise<RunControlResponse> {
  return sendJson<RunControlResponse>(`/runs/${runId}/controls`, "POST", input);
}

export function createPromptWorkshop(
  input: {
    harness: "claude" | "codex";
    intent: string;
  },
  options: { real?: boolean; background?: boolean } = {},
): Promise<PromptWorkshop> {
  return sendJson<PromptWorkshop>(withQuery("/prompt-workshops", options), "POST", input);
}

export function getPromptWorkshop(workshopId: string): Promise<PromptWorkshop> {
  return fetchJson<PromptWorkshop>(`/prompt-workshops/${workshopId}`);
}

export function selectPromptWorkshopOption(
  workshopId: string,
  input: { selected_option: string; final_prompt?: string },
): Promise<PromptWorkshop> {
  return sendJson<PromptWorkshop>(`/prompt-workshops/${workshopId}`, "PATCH", input);
}

function parseServerSentEvents(text: string): RunEvent[] {
  return text
    .split(/\r?\n\r?\n/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => parseServerSentEvent(block))
    .filter((event): event is RunEvent => event !== null);
}

async function apiError(path: string, response: Response): Promise<ApiError> {
  let detail: unknown = null;
  try {
    detail = await response.json();
  } catch {
    detail = null;
  }
  return new ApiError(`API request failed: ${path}`, response.status, detail);
}

function withQuery(path: string, params: Record<string, boolean | undefined>): string {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) {
      searchParams.set(key, String(value));
    }
  }
  const query = searchParams.toString();
  return query ? `${path}?${query}` : path;
}

function parseServerSentEvent(block: string): RunEvent | null {
  const lines = block.split(/\r?\n/);
  const id = lines.find((line) => line.startsWith("id:"))?.slice(3).trim() ?? "";
  const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim() ?? "message";
  const dataLines = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim());

  if (dataLines.length === 0) {
    return null;
  }

  try {
    return {
      id,
      event,
      data: JSON.parse(dataLines.join("\n")) as RunEvent["data"],
    };
  } catch {
    return null;
  }
}

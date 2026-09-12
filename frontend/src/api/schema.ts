/**
 * GENERATED FILE — DO NOT EDIT.
 *
 * Written by `npm run gen:api` from `openapi.json`, which the backend emits from the
 * FastAPI app (`backend/scripts/emit_openapi.py`). Every response and request shape the
 * API has is here, and nothing else is allowed to describe them: the app imports from
 * `./types`, which re-exports this file.
 *
 * To change a type, change the pydantic model and re-run `npm run gen:api`.
 * `npm run gen:api:check` fails the build when this file and the backend disagree.
 */

/** `202` body for the two fire-and-forget writes: queued, not yet applied. */
export type AcceptedResponse = {
  accepted: boolean;
};

export type BudgetByRole = {
  calls: number;
  role: string;
  tokens: number;
  usd: number;
};

export type CapabilitiesResponse = {
  grounding: GroundingBrief;
  harnesses: Record<string, HarnessBrief>;
  models: ModelsBrief;
};

export type CapabilityStatus = {
  available: boolean;
  checked_at: string;
  notes: string | null;
};

export type ChooseWorkshopRequest = {
  final_prompt?: string;
  option_id: string;
};

export type ChosenPrompt = {
  prompt: string;
};

export type CompareAnalytics = {
  baseline: RunSummary;
  challenger: RunSummary;
  deltas: CompareDelta[];
  graft_summary: Record<string, CompareGraft | null>;
  movement: CompareMovement[];
  shared_prompt: boolean;
};

export type CompareDelta = {
  base: number;
  challenger: number;
  direction_hint: DirectionHint;
  metric: string;
};

export type CompareGraft = {
  collapse_events: number;
  enabled: boolean;
  fired_count: number;
};

export type CompareMovement = {
  delta: number | null;
  hid: string;
  previous_rank: number | null;
  rank: number;
  title: string;
};

/** A document inlined into the prompts. Bytes only — no path ever reaches the CLI. */
export type ContextDocIn = {
  name: string;
  text: string;
};

export type ContextDocRef = {
  cap_chars: number | null;
  chars: number;
  delivered: "full" | "truncated" | "omitted" | "pending";
  name: string;
};

/** Plan C4's legal actions. Which are legal *when* is the lifecycle's business. */
export const CONTROL_ACTION_VALUES = ["pause", "resume", "continue", "stop", "finish", "force_stop"] as const;
export type ControlAction = (typeof CONTROL_ACTION_VALUES)[number];

export type CreateRunRequest = {
  config?: RunConfigIn;
  context_docs?: ContextDocIn[];
  from_run?: string | null;
  harness?: "claude" | "codex" | "demo";
  prompt?: string | null;
  question?: string | null;
  title?: string | null;
};

export type CreateWorkshopRequest = {
  context_docs?: WorkshopContextDoc[];
  harness?: Harness;
  question: string;
};

/** Which way a larger challenger number reads. */
export const DIRECTION_HINT_VALUES = ["up", "down", "flat"] as const;
export type DirectionHint = (typeof DIRECTION_HINT_VALUES)[number];

export type Event = {
  payload: Record<string, unknown>;
  round: number | null;
  run_id: string;
  seq: number;
  ts: string | null;
  type: string;
};

export type EventTicket = {
  expires_in: number;
  ticket: string;
};

export type FeedbackEntry = {
  guidance: string;
  round: number;
};

export type GraftConfigIn = {
  cooldown?: number;
  enabled?: boolean;
  quorum_k?: number;
  window?: number;
};

export type GraftEvent = {
  abstained_reason: string | null;
  fired: boolean;
  hhi: number | null;
  n_clusters: number | null;
  round: number;
  seed_framing: string | null;
  source_domain: string | null;
  votes: number | null;
};

/**
 * One descent link, parent → child.
 *
 * The operator is copied from the child so an edge can be styled without a lookup — the
 * two arrows converging on a combination are the visual signature of evolution.
 */
export type GraphEdge = {
  child: string;
  operator: string | null;
  parent: string;
};

/** What the legend needs to describe this run without overclaiming. */
export type GraphMeta = {
  elo_max: number;
  elo_min: number;
  has_clusters: boolean;
  has_lineage: boolean;
  lineage_recorded: boolean;
  node_count: number;
  rounds: number;
};

/**
 * One idea as the genealogy canvas draws it.
 *
 * Narrower than `HypothesisRow` on purpose in one direction and wider in another: no
 * `body_md` and no `parent_ids` (descent is edges, so a node and a link are not two
 * disagreeing statements of the same fact), plus `is_leader`, which no single row can
 * know because it is a fact about the run.
 */
export type GraphNode = {
  cluster: string | null;
  created_round: number;
  duplicate_of: string | null;
  elo: number;
  hid: string;
  id: string;
  is_leader: boolean;
  matches: number;
  operator: string | null;
  source: HypothesisSource;
  status: HypothesisStatus;
  title: string;
  wins: number;
};

export type GroundingBrief = {
  perplexity: boolean;
  web: boolean;
};

export type GroundingCapabilitiesResponse = {
  built_in_web: CapabilityStatus;
  perplexity: CapabilityStatus;
};

export const GROUNDING_DEPTH_VALUES = ["shallow", "standard", "deep"] as const;
export type GroundingDepth = (typeof GROUNDING_DEPTH_VALUES)[number];

export type HTTPValidationError = {
  detail: ValidationError[];
};

/** `POST /admin/halt-all`. The runs the kill switch actually stopped. */
export type HaltAllResponse = {
  stopped: string[];
};

export const HARNESS_VALUES = ["claude", "codex", "demo"] as const;
export type Harness = (typeof HARNESS_VALUES)[number];

export type HarnessBrief = {
  installed: boolean;
  version: string | null;
};

export type HarnessCapabilitiesResponse = {
  claude: HarnessCapabilityStatus;
  codex: HarnessCapabilityStatus;
};

export type HarnessCapabilityStatus = {
  available: boolean;
  checked_at: string;
  executable: string | null;
  installed: boolean;
  notes: string | null;
  real_runs_enabled: boolean;
  version: string | null;
};

export type HarnessHealthStatus = {
  installed: boolean;
  version: string | null;
};

export type HealthResponse = {
  active_runs: number;
  db_ok: boolean;
  harnesses: Record<string, HarnessHealthStatus>;
  status: string;
  version: string;
};

export type HypothesisDetail = {
  body_md: string;
  cluster: string | null;
  created_round: number;
  duplicate_of: string | null;
  elo: number;
  hid: string;
  id: string;
  lineage: Lineage;
  match_history: MatchRow[];
  matches: number;
  novelty_level: string | null;
  operator: string | null;
  parent_ids: string[];
  reviews: ReviewRow[];
  source: HypothesisSource;
  status: HypothesisStatus;
  title: string;
  wins: number;
};

export type HypothesisRow = {
  cluster: string | null;
  created_round: number;
  duplicate_of: string | null;
  elo: number;
  hid: string;
  id: string;
  matches: number;
  novelty_level: string | null;
  operator: string | null;
  parent_ids: string[];
  source: HypothesisSource;
  status: HypothesisStatus;
  title: string;
  wins: number;
};

export const HYPOTHESIS_SOURCE_VALUES = ["agent", "human"] as const;
export type HypothesisSource = (typeof HYPOTHESIS_SOURCE_VALUES)[number];

export const HYPOTHESIS_STATUS_VALUES = ["active", "rejected", "archived"] as const;
export type HypothesisStatus = (typeof HYPOTHESIS_STATUS_VALUES)[number];

export type IdentityResponse = {
  display_name: string | null;
  email: string | null;
  groups: string[];
  is_admin: boolean;
  source: "gateway" | "local";
  username: string;
};

/** `POST /admin/reimport`. Counts of what one pass over the archive did. */
export type ImportReportResponse = {
  created: number;
  graft_events: number;
  hypotheses: number;
  imported: number;
  legacy_bodies: number;
  matches: number;
  overviews: number;
  repaired_strings: number;
  reviews: number;
  run_ids: string[];
  updated: number;
  warnings: string[];
};

/** Plan C4's canonical set. Transitions live with the model, not here. */
export const LIFECYCLE_VALUES = ["queued", "running", "pausing", "paused", "stopping", "stopped", "finishing", "completed", "failed", "lost"] as const;
export type Lifecycle = (typeof LIFECYCLE_VALUES)[number];

export type Lineage = {
  children: HypothesisRow[];
  parents: HypothesisRow[];
};

export type MatchRow = {
  a: MatchSide;
  b: MatchSide;
  debate_md: string | null;
  elo_a_after: number | null;
  elo_a_before: number | null;
  elo_b_after: number | null;
  elo_b_before: number | null;
  id: string;
  judge_model: string | null;
  round: number;
  status: MatchStatus;
  ts: string | null;
  winner: number | null;
};

export type MatchSide = {
  hid: string;
  title: string;
};

export const MATCH_STATUS_VALUES = ["planned", "completed", "skipped"] as const;
export type MatchStatus = (typeof MATCH_STATUS_VALUES)[number];

/**
 * A model a role may be set to, with what it costs to prefer it.
 *
 * **`efforts` is this model's own effort ladder, and an effort picker must read it off the
 * selected model rather than off its provider.** The two providers' ladders happen to match
 * today and are not required to: a ladder is published per model slug by the CLI that runs
 * it, and offering a rung the model does not have is a 400 in the middle of a paid run.
 *
 * `input_usd_per_mtok`/`output_usd_per_mtok` are list prices per million tokens and are
 * **null for models that have none** — the Codex pair runs on a ChatGPT account with no
 * per-token price to quote. Their per-call bracket is still given, and `price_basis` says
 * where it came from: `list` (derived from those prices) or `class_parity` (the bracket of
 * the model of the same class that does have them). A bracket labelled as a parity estimate
 * is honest; a price nobody quoted would not be.
 */
export type ModelChoiceBrief = {
  efforts: string[];
  grounded: boolean;
  id: string;
  input_usd_per_mtok: number | null;
  label: string;
  model_class: string;
  output_usd_per_mtok: number | null;
  price_basis: string;
  provider: string;
  recommendation: string;
  relative_cost: number;
  usd_per_call_high: number;
  usd_per_call_low: number;
};

/**
 * The persisted system default every new run and workshop inherits.
 *
 * The same value `GET /api/settings/models` serves, carried here so the wizard and the top
 * bar can render on one round trip. `source` distinguishes a default somebody chose from
 * the built-in one nobody has changed yet.
 *
 * `tiers` is the stored per-tier override buckets — saving in the editor redefines the tier
 * that is selected, so there is one bucket per tier and each is sparse. `overrides` is the
 * derived mirror of the selected tier's bucket, which is what the launcher and the workshop
 * actually read, and `table` is that tier resolved.
 */
export type ModelDefaultBrief = {
  overrides: Record<string, Record<string, string>>;
  provider: string;
  source: string;
  table: RoleModelBrief[];
  tier: string;
  tiers: Record<string, Record<string, Record<string, string>>>;
  updated_at: string | null;
};

/**
 * One role's model and/or effort, replacing what the tier would have given it.
 *
 * Both fields are optional and independent: naming only an effort keeps the tier's model
 * and vice versa. The literal sets are the allowlist itself rather than a copy of it, so
 * a model this engine refuses is a 422 at the edge and never reaches the launcher — and
 * `claude-fable-5`, which the CLI silently downgrades to Opus 5, is refused here by not
 * being in the set (the launcher's error explains why if it arrives another way).
 *
 * A row may name **either provider's** model regardless of the run's `provider`: mixing
 * providers within one run is the point of the field, not an accident of it.
 *
 * `effort` is checked here only against the engine's whole vocabulary. Whether a given
 * level exists on the chosen *model* is the model's own business — every model publishes
 * its own ladder in `/api/capabilities`, and a level outside it is clamped, visibly, in
 * the resolved table. **A client offering an effort picker must read the ladder off the
 * selected model, not off its provider.**
 */
export type ModelOverrideIn = {
  effort?: "low" | "medium" | "high" | "xhigh" | "max" | null;
  model?: "claude-opus-5" | "fable" | "gpt-5.6-luna" | "gpt-5.6-sol" | "gpt-6-astra" | null;
};

/**
 * One role's model and/or effort on the **settings** body. Deliberately plain strings.
 *
 * Not `schemas.launch.ModelOverrideIn`, which types both fields as the literal allowlist.
 * That is right for a launch — a run must never reach the launcher naming a model this
 * engine refuses — and wrong here, because this endpoint's entire contract is that a
 * default which could not become a run comes back carrying the *model policy's own*
 * sentence. A literal union answers `claude-fable-5` with "Input should be
 * 'claude-opus-5', 'fable', 'gpt-5.6-luna' or 'gpt-5.6-sol'", which is true and useless:
 * the fact worth telling anybody is that the CLI accepts that id and silently runs Opus 5,
 * and it lives in `MODEL_TRAPS`, one layer below where Pydantic stops.
 *
 * So the values go through as strings and `validate_model`/`validate_effort` refuse them
 * with a reason. `extra="forbid"` stays: an override carries these two fields and nothing
 * else, and an invented field is a malformed request rather than a policy question.
 */
export type ModelOverrideSettingIn = {
  effort?: string | null;
  model?: string | null;
};

/**
 * The stored default and the table it resolves to.
 *
 * `source` is `stored` once the editor has saved anything and `built_in` before that, so a
 * UI can tell "this is what the system does" from "this is what somebody chose".
 *
 * `tiers` is the stored per-tier buckets and `overrides` is a **read-only mirror** of
 * `tiers[tier]` — the same object the launcher and the workshop read, served so a client that
 * only cares about the tier in force does not have to index the buckets itself. It is derived
 * on every response rather than stored, so the mirror cannot fall out of step with what it
 * mirrors. `table` is the resolved table for the **selected** tier.
 */
export type ModelSettingsResponse = {
  overrides: Record<string, ModelOverrideIn>;
  provider: Provider;
  source: "stored" | "built_in";
  table: ModelSettingsRow[];
  tier: ModelTier;
  tiers: Record<string, Record<string, ModelOverrideIn>>;
  updated_at: string | null;
};

/** One resolved step of the default table. */
export type ModelSettingsRow = {
  effort: "low" | "medium" | "high" | "xhigh" | "max";
  grounded: boolean;
  model: "claude-opus-5" | "fable" | "gpt-5.6-luna" | "gpt-5.6-sol" | "gpt-6-astra";
  model_class: "heavy" | "light";
  note: string;
  role: string;
};

/**
 * `PUT` body. Every field optional; an absent one keeps the built-in default's value.
 *
 * `overrides` may name either provider's model on any row — mixing providers inside one
 * table is the point of the field. The effort named for a row is checked against the
 * engine's vocabulary in the service and against **the chosen model's own ladder** when the
 * table is resolved, which is where a level the model does not have gets clamped, visibly,
 * into the table this endpoint returns.
 *
 * Provider, tier and the override role keys are strings for the same reason the two fields
 * of `ModelOverrideSettingIn` are: every one of them has an engine validator whose refusal
 * names the value *and* the alternatives, and a wire enum in front of that validator
 * replaces the message with a list. Tightening any of these back into an enum silently
 * turns this endpoint's 400s into generic 422s again.
 */
export type ModelSettingsUpdate = {
  overrides?: Record<string, ModelOverrideSettingIn>;
  provider?: string;
  tier?: string;
  tiers?: Record<string, Record<string, ModelOverrideSettingIn>>;
};

export type ModelTableEntry = {
  effort: string | null;
  model: string;
  role: string;
};

/**
 * A preset that moves thinking effort — and, for `low` alone, the model too.
 *
 * For `max`, `high` and `med`, which model a step runs on is fixed by how hard the step is
 * — heavy steps take the provider's frontier model, light steps take its workhorse — so
 * `med` is a run that thinks less, never a run that thinks with something weaker.
 * Clustering is pinned to low effort in every tier.
 *
 * `low` is the speed preset and the one exception: it pins **every** step to `gpt-5.6-luna`
 * at high effort whatever the run's provider says, so a Low run drives the Codex CLI and
 * needs it installed. That is published rather than implied — `/api/capabilities` carries
 * the pin and the CLI requirement per tier in `models.tier_catalog` — because a tier
 * changing models is only a problem when nobody can see it.
 *
 * The older names `standard`/`maximum` and the pre-floor `balanced`/`quality` are gone
 * from the wire but are still accepted on the way in and mapped by
 * `engine/runners.normalise_tier`, because stored run configs say them.
 */
export const MODEL_TIER_VALUES = ["max", "high", "med", "low"] as const;
export type ModelTier = (typeof MODEL_TIER_VALUES)[number];

/**
 * Which model and thinking effort each step gets, per provider and per tier.
 *
 * Read straight out of the engine's own `role_catalog`, so what the top bar and the wizard
 * show is what a run launched right now would actually use. `notes` carries the escalations
 * the orchestrator applies from run state, which no table can express; `catalog`,
 * `providers` and `efforts` are the vocabularies a per-role picker chooses from — served
 * rather than restated, because a client that types its own model list is a client that can
 * offer a model the engine will refuse.
 *
 * `tiers` is keyed by provider and then by tier, because the same tier names a different
 * pair of models on each provider. `efforts` is the engine's whole vocabulary and is the
 * outer bound only — **the options for one row are the `efforts` of that row's model in
 * `catalog`.**
 *
 * `tier_catalog` is the tiers themselves: label, what each one does, and — for the one tier
 * that pins a model rather than only an effort — which model and which CLI that needs. A
 * control offering tiers reads that instead of hard-coding four names and a description each.
 */
export type ModelsBrief = {
  catalog: ModelChoiceBrief[];
  default: ModelDefaultBrief | null;
  default_provider: string;
  default_tier: string;
  efforts: string[];
  notes: string[];
  providers: ProviderBrief[];
  tier_catalog: TierBrief[];
  tiers: Record<string, Record<string, RoleModelBrief[]>>;
};

/** `POST /runs/{id}/note`. Blank text is refused here rather than queued and ignored. */
export type NoteRequest = {
  text: string;
};

/**
 * `PATCH /runs/{id}`: the only two fields a scientist may change after launch.
 *
 * Everything else — question, prompt, config — is immutable once a run exists;
 * renaming or archiving it is bookkeeping, not a change to what the run did. A field left
 * out of the body is left alone, not cleared.
 */
export type PatchRunRequest = {
  archived?: boolean | null;
  title?: string | null;
};

/**
 * Whose CLI runs a step. A quick-set for the whole table, never a lock on one row:
 * steps mix providers freely, and the runner for a call is chosen from that row's model.
 */
export const PROVIDER_VALUES = ["anthropic", "openai"] as const;
export type Provider = (typeof PROVIDER_VALUES)[number];

/**
 * One provider the top bar's quick-set can choose, and the CLI lane it drives.
 *
 * `efforts` here is the *union* of this provider's models' ladders and exists only to size
 * a control. It is not a per-row vocabulary: a row's real options are the `efforts` of the
 * model on that row.
 */
export type ProviderBrief = {
  efforts: string[];
  harness: string;
  id: string;
  label: string;
  models: string[];
};

export type RefineWorkshopRequest = {
  /** An option id, or 'merge' to combine both. */
  base: string;
  note?: string;
};

export type ResearchApproach = {
  assumptions: string[];
  framing: string;
  id: string;
  method: string;
};

export type ResearchCalculation = {
  assumption: string;
  claim_id: string;
  error: string | null;
  expected: number;
  expression: string;
  passed: boolean;
  result: number | null;
  tolerance: number;
};

export type ResearchCandidate = {
  approach: string;
  calculation_results: ResearchCalculation[];
  claims: ResearchClaim[];
  dimensions: Record<string, ResearchDimension>;
  hid: string;
  next_test: string;
  readiness: string;
  title: string;
};

export type ResearchClaim = {
  claim: string;
  id: string;
  rationale: string;
  sources: ResearchSource[];
  status: string;
};

export type ResearchContradiction = {
  claims: ResearchClaim[];
  hid: string;
  title: string;
};

export type ResearchDecision = {
  action: string;
  checkpoint: number;
  reason: string;
};

export type ResearchDependency = {
  rationale: string;
  solution_hids: string[];
  status: string;
  subproblem_id: string;
};

export type ResearchDimension = {
  grade: string;
  rationale: string;
};

export type ResearchSource = {
  finding: string;
  relation: string;
  url: string;
};

export type ResearchSourceStrategy = {
  other_source_queries: string[];
  published_research_percent: number;
  rationale: string;
  scholarly_queries: string[];
};

export type ResearchSubproblem = {
  acceptance_test: string;
  depends_on: string[];
  id: string;
  question: string;
};

export type ResearchView = {
  approaches: ResearchApproach[];
  blocking_issues: string[];
  challenge_assessment: string;
  contradictions: ResearchContradiction[];
  decisions: ResearchDecision[];
  dependencies: ResearchDependency[];
  objective: string;
  phase: string;
  portfolio: ResearchCandidate[];
  recommendation_ready: boolean;
  source_strategy: ResearchSourceStrategy | null;
  subproblems: ResearchSubproblem[];
  synthesis_markdown: string;
  synthesis_round: number | null;
  unresolved: string[];
};

/** Imported runs carry only `verdict` and `note` — history recorded nothing else. */
export type ReviewRow = {
  correctness: string | null;
  key_risk: string | null;
  model: string | null;
  note: string | null;
  novelty_level: string | null;
  novelty_note: string | null;
  testability: string | null;
  verdict: string;
};

/** One step of the loop: what it runs on, and why it is set that way. */
export type RoleModelBrief = {
  effort: string;
  grounded: boolean;
  model: string;
  model_class: string;
  note: string;
  role: string;
};

export type RoundSummary = {
  completed_at: string | null;
  graft_fired: boolean;
  hypotheses_added: number;
  matches_completed: number;
  matches_planned: number;
  reviews: number;
  round: number;
  started_at: string | null;
  status: string;
};

export type RunBudget = {
  budget_calls: number;
  budget_usd: number;
  by_role: BudgetByRole[];
  calls_used: number;
  spend_usd: number;
};

/** Plan C5's config block. `model_table` is not accepted: the launcher resolves it. */
export type RunConfigIn = {
  budget_calls?: number;
  budget_usd?: number | null;
  evolve_top_k?: number;
  generation_batch?: number;
  graft?: GraftConfigIn;
  grounding_depth?: GroundingDepth;
  matches_per_round?: number;
  model_overrides?: Record<string, ModelOverrideIn> | null;
  model_tier?: ModelTier | null;
  provider?: Provider | null;
  rounds?: number;
  runner?: RunnerKind;
  wall_clock_minutes?: number | null;
  workflow?: "adaptive" | "tournament";
};

/** `POST /runs/{id}/controls`. One action, plus the three fields `continue` needs. */
export type RunControlRequest = {
  action: ControlAction;
  add_rounds?: number | null;
  budget_calls?: number | null;
  budget_usd?: number | null;
};

export type RunControlResponse = {
  accepted: boolean;
  lifecycle: Lifecycle;
};

export type RunCounts = {
  active: number;
  archived: number;
  matches: number;
  rejected: number;
};

export type RunDetail = {
  budget: RunBudget;
  config: Record<string, unknown>;
  context_docs: ContextDocRef[];
  degraded_count: number;
  failed_calls: number;
  feedback_history: FeedbackEntry[];
  graft_events: GraftEvent[];
  leaderboard: HypothesisRow[];
  lost_steps: number;
  model_table: ModelTableEntry[];
  problems: Event[];
  recent_events: Event[];
  research: ResearchView | null;
  retried_calls: number;
  rounds: RoundSummary[];
  run: RunSummary;
};

export type RunGraft = {
  enabled: boolean;
  fired_count: number;
  pending: boolean;
};

export type RunGraph = {
  edges: GraphEdge[];
  meta: GraphMeta;
  nodes: GraphNode[];
  run_id: string;
};

export type RunList = {
  items: RunSummary[];
  total: number;
};

export const RUN_SOURCE_VALUES = ["app", "imported"] as const;
export type RunSource = (typeof RUN_SOURCE_VALUES)[number];

export type RunSummary = {
  archived: boolean;
  budget_calls: number;
  calls_used: number;
  counts: RunCounts;
  created_at: string;
  elapsed_seconds: number | null;
  ended_reason: string | null;
  engine_run_id: string;
  failed_calls: number;
  graft: RunGraft;
  harness: Harness;
  has_overview: boolean;
  id: string;
  lifecycle: Lifecycle;
  lost_steps: number;
  model_level: "low" | "med" | "high" | "max" | null;
  model_level_custom: boolean;
  overview_skipped_reason: string | null;
  owner_display_name: string | null;
  question: string;
  round: number;
  rounds_target: number;
  source: RunSource;
  source_version: string | null;
  spend_usd: number;
  title: string;
  tokens_total: number;
  top: RunTop[];
  updated_at: string;
};

export type RunTop = {
  elo: number;
  hid: string;
  status: HypothesisStatus;
  title: string;
};

/**
 * Which runner executes a run's calls. `demo` is the fake; the other two are real.
 *
 * `claude`/`codex` name the *default* lane rather than a restriction — a real run
 * dispatches each call to the runner its row's model belongs to, so a table mixing
 * providers works whichever of the two this says.
 */
export const RUNNER_KIND_VALUES = ["claude", "codex", "demo"] as const;
export type RunnerKind = (typeof RUNNER_KIND_VALUES)[number];

/**
 * One tier the top bar can select, and what selecting it does.
 *
 * Served rather than restated for the same reason the role table is: a control that
 * described a tier in its own words would describe it wrongly the first time a tier changed.
 *
 * Two fields exist to keep the `low` tier's model pin **visible**, which is the condition
 * on which a tier was allowed to name a model at all:
 *
 * * `pinned_models` — model class → the model this tier forces on it, empty for the tiers
 *   that force none (`max`, `high`, `med` all leave the model to the provider). A client can
 *   therefore say "this tier runs Luna" without diffing two resolved tables to notice.
 * * `requires_harness` — the CLIs a run in this tier drives *because of the pin*, whatever
 *   its provider says. `low` requires `codex`, and `available` is that requirement checked
 *   against the probe, so the UI can warn on an install without Codex rather than let the
 *   first call of a run discover it.
 *
 * `available` is false only when a required CLI is missing. A tier that pins nothing is
 * always available: which CLI it needs is then decided by the run's provider, which the
 * `harnesses` block already reports on.
 */
export type TierBrief = {
  available: boolean;
  id: string;
  label: string;
  note: string;
  pinned_models: Record<string, string>;
  requires_harness: string[];
  unavailable_reason: string | null;
};

export type ValidationError = {
  ctx: Record<string, unknown>;
  input: unknown;
  loc: (string | number)[];
  msg: string;
  type: string;
};

export type Workshop = {
  created_at: string | null;
  error: WorkshopError | null;
  harness: Harness;
  id: string;
  options: WorkshopOption[];
  question: string;
  state: WorkshopState;
};

export type WorkshopContextDoc = {
  name: string;
  text: string;
};

/** Why a workshop stopped. `code` is for the app, `message` is for the scientist. */
export type WorkshopError = {
  code: string;
  detail: string | null;
  message: string;
};

export type WorkshopOption = {
  chosen: boolean;
  excludes: string | null;
  id: string;
  note: string | null;
  optimizes_for: string | null;
  ordinal: number;
  prompt: string;
  rationale: string | null;
  recommended_settings: WorkshopRecommendedSettings;
  rejected: boolean;
  strategy: string;
};

/** What this option wants the run configured as. Defaults match the Standard preset. */
export type WorkshopRecommendedSettings = {
  budget_calls: number;
  grounding_depth: GroundingDepth;
  matches_per_round: number;
  rounds: number;
};

export const WORKSHOP_STATE_VALUES = ["refining", "options_ready", "chosen", "failed"] as const;
export type WorkshopState = (typeof WORKSHOP_STATE_VALUES)[number];

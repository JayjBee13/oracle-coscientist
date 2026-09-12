/**
 * Builders for the C5 DTOs. Tests override only the fields they care about, so
 * a change to the contract breaks one file rather than twenty.
 */

import type {
  Capabilities,
  ModelChoice,
  ModelSettings,
  RoleModelBrief,
  RoleModelRow,
  RunConfig,
  RunDetail,
  RunSummary,
} from "../api/types";
import { DEFAULT_MODEL_TIER } from "../lib/estimates";

export function makeRunSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run-1",
    engine_run_id: "run-20260801-120000",
    source: "app",
    source_version: null,
    title: "Photocatalytic routes to ammonia",
    question: "How could ammonia be made photocatalytically at ambient pressure?",
    owner_display_name: "Local Owner",
    harness: "claude",
    lifecycle: "running",
    round: 2,
    rounds_target: 5,
    calls_used: 23,
    budget_calls: 60,
    model_level: "high",
    model_level_custom: false,
    elapsed_seconds: null,
    spend_usd: 1.42,
    tokens_total: 412000,
    counts: { active: 9, rejected: 2, archived: 1, matches: 6 },
    top: [
      { hid: "h001", title: "Plasmonic nitrogen fixation", elo: 1246, status: "active" },
    ],
    graft: { enabled: false, fired_count: 0, pending: false },
    archived: false,
    has_overview: false,
    failed_calls: 0,
    lost_steps: 0,
    ended_reason: null,
    overview_skipped_reason: null,
    created_at: "2026-08-01T10:00:00Z",
    updated_at: "2026-08-01T10:30:00Z",
    ...overrides,
  };
}

export function makeRunConfig(overrides: Partial<RunConfig> = {}): RunConfig {
  return {
    workflow: "tournament",
    rounds: 5,
    generation_batch: 6,
    matches_per_round: 4,
    evolve_top_k: 3,
    budget_calls: 60,
    budget_usd: 5,
    grounding_depth: "standard",
    graft: { enabled: false, quorum_k: 3, window: 3, cooldown: 2 },
    model_tier: DEFAULT_MODEL_TIER,
    model_overrides: {},
    provider: null,
    wall_clock_minutes: null,
    runner: "claude",
    ...overrides,
  };
}

export function makeRunDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  const run = overrides.run ?? makeRunSummary();
  return {
    research: null,
    run,
    config: makeRunConfig(),
    model_table: [{ role: "generation", model: "claude-sonnet-5", effort: "high" }],
    leaderboard: [],
    rounds: [],
    recent_events: [],
    budget: {
      calls_used: run.calls_used,
      budget_calls: run.budget_calls,
      spend_usd: run.spend_usd,
      budget_usd: 5,
      by_role: [],
    },
    graft_events: [],
    context_docs: [],
    feedback_history: [],
    degraded_count: 0,
    failed_calls: 0,
    lost_steps: 0,
    retried_calls: 0,
    problems: [],
    ...overrides,
  };
}

/**
 * `GET /capabilities`, shaped like the real payload: every step of the loop, two
 * tiers, and the escalations no table can express. The tier names and models are
 * the engine's own — but nothing in the UI may depend on them, so a rename in
 * `engine/runners.py` should change this fixture and no assertion.
 */
export function makeCapabilities(overrides: Partial<Capabilities> = {}): Capabilities {
  const FLOOR = "model-floor";
  const TOP = "model-top";
  const SOLO = "vendor-a";
  const upgraded = new Set([
    "generation",
    "reflection",
    "ranking",
    "meta_review",
    "overview",
  ]);
  const roles = [
    "generation",
    "reflection",
    "proximity",
    "ranking",
    "evolution",
    "meta_review",
    "cartographer",
    "overview",
    "workshop",
  ];
  const table = (top: boolean): RoleModelBrief[] =>
    roles.map((role) => ({
      role,
      model: top && upgraded.has(role) ? TOP : FLOOR,
      model_class: upgraded.has(role) ? "heavy" : "light",
      effort: role === "proximity" ? "low" : "medium",
      note: `Why ${role} is set this way.`,
      grounded: ["generation", "reflection", "evolution", "workshop"].includes(role),
    }));

  return {
    grounding: { web: true, perplexity: false },
    harnesses: {
      claude: { installed: true, version: "2.0.0" },
      codex: { installed: true, version: "1.0.0" },
    },
    models: {
      default_provider: SOLO,
      default_tier: "floor-tier",
      default: null,
      providers: [
        {
          id: SOLO,
          label: "Vendor A",
          harness: "claude",
          models: [FLOOR, TOP],
          efforts: ["low", "medium", "high", "xhigh", "max"],
        },
      ],
      // Keyed by provider, then by tier — the payload's own shape. A backend
      // with one vendor is one key here, which is what most of this suite is
      // now quietly asserting still renders.
      tiers: { [SOLO]: { "floor-tier": table(false), "top-tier": table(true) } },
      // One entry per published tier, both of them effort-only: `pinned_models`
      // empty is the ordinary case, and it is what says "this tier leaves the
      // model to the provider". The pinning tier lives in the two-provider
      // builder below, beside the tiers that need it.
      tier_catalog: [
        {
          id: "floor-tier",
          label: "Floor tier",
          note: "What choosing the floor tier does.",
          pinned_models: {},
          requires_harness: [],
          available: true,
          unavailable_reason: null,
        },
        {
          id: "top-tier",
          label: "Top tier",
          note: "What choosing the top tier does.",
          pinned_models: {},
          requires_harness: [],
          available: true,
          unavailable_reason: null,
        },
      ],
      catalog: [
        {
          id: FLOOR,
          label: "Floor model",
          provider: SOLO,
          model_class: "light",
          efforts: ["low", "medium", "high", "xhigh", "max"],
          grounded: true,
          recommendation: "The floor, and the right default.",
          relative_cost: 1,
          price_basis: "list",
          input_usd_per_mtok: 5,
          output_usd_per_mtok: 25,
          usd_per_call_low: 0.25,
          usd_per_call_high: 0.667,
        },
        {
          id: TOP,
          label: "Top model",
          provider: SOLO,
          model_class: "heavy",
          efforts: ["low", "medium", "high", "xhigh", "max"],
          grounded: true,
          recommendation: "The most capable, at twice the price.",
          relative_cost: 2,
          price_basis: "list",
          input_usd_per_mtok: 10,
          output_usd_per_mtok: 50,
          usd_per_call_low: 0.5,
          usd_per_call_high: 1.333,
        },
      ],
      efforts: ["low", "medium", "high", "xhigh", "max"],
      notes: [
        "Generation runs at high effort in round 1, then at the effort listed here.",
        "The tournament runs at high effort when both ideas are in the top 5, and for every match in the final round.",
      ],
    },
    ...overrides,
  };
}

/**
 * The same payload once a second vendor publishes into it.
 *
 * Deliberately a *second* builder rather than a change to the first. Both are
 * real: `makeCapabilities` is a backend with one provider, and every screen must
 * still render it — that is the property most of the suite is now quietly
 * asserting, because those tests were written before providers existed and still
 * pass untouched. This one is the payload afterwards, and it is what the provider
 * behaviour is tested against.
 *
 * Everything here is placeholder vocabulary for the same reason the first builder
 * is: `vendor-b` / `heavy-b` are stand-ins, so a test that reads a label out of
 * this fixture cannot accidentally assert the engine's allowlist. The one
 * structural fact it does model is the one the live probe established — **effort
 * ladders differ per model, not per provider** — so the two vendors' heavy models
 * have different ceilings and their light models have a rung the heavy ones lack.
 */
export function makeProviderCapabilities(
  overrides: Partial<Capabilities> = {},
): Capabilities {
  const base = makeCapabilities();
  const A = { heavy: "heavy-a", light: "light-a" };
  const B = { heavy: "heavy-b", light: "light-b" };
  const isHeavy = (row: RoleModelRow): boolean => row.model_class === "heavy";

  const table = (
    provider: string,
    models: { heavy: string; light: string },
    efforts: { heavy: string; light: string },
  ): RoleModelRow[] =>
    base.models.tiers["vendor-a"]["floor-tier"].map((row) => ({
      ...row,
      provider,
      model: isHeavy(row) ? models.heavy : models.light,
      // Clustering is mechanical and pinned low in every tier — the one row that
      // must read honestly however high the tier goes.
      effort:
        row.role === "proximity" ? "low" : isHeavy(row) ? efforts.heavy : efforts.light,
    }));

  /**
   * The model the `low` tier pins on every row, whatever the provider — B's
   * light model, so the pin is cross-vendor exactly as the engine's is, and the
   * tier needs a CLI its provider does not imply.
   */
  const PIN = B.light;

  const tiers = (
    provider: string,
    models: { heavy: string; light: string },
  ): Record<string, RoleModelRow[]> => ({
    top: table(provider, models, { heavy: "xhigh", light: "high" }),
    mid: table(provider, models, { heavy: "high", light: "medium" }),
    // The pinning tier: one model on every row, heavy class included, at the top
    // of *that model's* own ladder — which is what the engine resolves to, since
    // it clamps a tier's effort to the ladder of the model it pinned.
    low: table(
      provider,
      { heavy: PIN, light: PIN },
      { heavy: "medium", light: "medium" },
    ),
  });

  const choice = (
    id: string,
    label: string,
    provider: string,
    modelClass: string,
    efforts: string[],
    grounded: boolean,
  ): ModelChoice => ({
    id,
    label,
    provider,
    model_class: modelClass,
    efforts,
    grounded,
    recommendation: `What ${label} is for.`,
    relative_cost: 1,
    price_basis: "list",
    input_usd_per_mtok: 5,
    output_usd_per_mtok: 25,
    usd_per_call_low: 0.25,
    usd_per_call_high: 0.667,
  });

  return {
    ...base,
    models: {
      ...base.models,
      default_provider: "vendor-a",
      default_tier: "top",
      providers: [
        {
          id: "vendor-a",
          label: "Vendor A",
          harness: "claude",
          models: [A.heavy, A.light],
          efforts: ["low", "medium", "high", "xhigh"],
        },
        {
          id: "vendor-b",
          label: "Vendor B",
          harness: "codex",
          models: [B.heavy, B.light],
          efforts: ["minimal", "low", "medium", "high", "xhigh", "max"],
        },
      ],
      tiers: { "vendor-a": tiers("vendor-a", A), "vendor-b": tiers("vendor-b", B) },
      // One entry per tier, and one of them pins a model. The labels are not the
      // ids humanised — `low` is called "Speed" — because a control that reads
      // its tier names off this payload must be demonstrably reading them off
      // this payload and not off its own vocabulary. `requires_harness` names
      // the CLI the pin needs whichever provider is selected, which is the whole
      // reason the field is not derivable from the provider block.
      tier_catalog: [
        {
          id: "top",
          label: "Top",
          note: "Everything thinks at the top of its ladder.",
          pinned_models: {},
          requires_harness: [],
          available: true,
          unavailable_reason: null,
        },
        {
          id: "mid",
          label: "Mid",
          note: "Heavy steps high, light steps medium.",
          pinned_models: {},
          requires_harness: [],
          available: true,
          unavailable_reason: null,
        },
        {
          id: "low",
          label: "Speed",
          note: "The speed preset. One model on every step, whatever the provider.",
          pinned_models: { heavy: PIN, light: PIN },
          requires_harness: ["codex"],
          available: true,
          unavailable_reason: null,
        },
      ],
      catalog: [
        choice(
          A.heavy,
          "Heavy A",
          "vendor-a",
          "heavy",
          ["low", "medium", "high", "xhigh"],
          true,
        ),
        choice(
          A.light,
          "Light A",
          "vendor-a",
          "light",
          ["minimal", "low", "medium", "high"],
          true,
        ),
        choice(
          B.heavy,
          "Heavy B",
          "vendor-b",
          "heavy",
          ["low", "medium", "high", "xhigh", "max"],
          true,
        ),
        // The one model in the fixture that cannot reach the web. Every
        // grounded-warning assertion in the suite hangs off this single flag, so
        // a payload that stops publishing it raises no warning at all.
        choice(
          B.light,
          "Light B",
          "vendor-b",
          "light",
          ["minimal", "low", "medium"],
          false,
        ),
      ],
    },
    ...overrides,
  };
}

/** The default provider's tables — what a single-provider test means by "tiers". */
export function tablesOf(capabilities: Capabilities): Record<string, RoleModelRow[]> {
  return capabilities.models.tiers[capabilities.models.default_provider];
}

/**
 * `GET /settings/models` — the stored system default.
 *
 * Defaults to the two-provider fixture's own first provider and top tier, so a
 * test that stubs both endpoints gets a settings document the capabilities
 * payload can actually resolve. Nothing here names a real provider or tier.
 *
 * `tiers` is the state and `overrides` is the server's derived mirror of
 * `tiers[tier]`, so a caller sets buckets and gets the mirror for free. A fixture
 * that let the two disagree would be a payload the backend cannot serve, and the
 * mirror is what three other screens read.
 */
export function makeModelSettings(overrides: Partial<ModelSettings> = {}): ModelSettings {
  const models = makeProviderCapabilities().models;
  const provider = overrides.provider ?? models.default_provider;
  const tier = overrides.tier ?? models.default_tier;
  const tiers = overrides.tiers ?? {};
  return {
    provider,
    tier,
    tiers,
    overrides: tiers[tier] ?? {},
    table: models.tiers[provider][tier],
    source: "stored",
    updated_at: "2026-08-11T09:00:00Z",
    ...overrides,
  } as ModelSettings;
}

/** Minimal `fetch` stand-in: routes by URL substring, in order. */
export function mockFetchRoutes(
  routes: { match: string; json?: unknown; text?: string; status?: number }[],
): (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> {
  return async (input) => {
    const url = String(input);
    const route = routes.find((candidate) => url.includes(candidate.match));
    if (!route && url.includes("/me")) {
      return new Response(
        JSON.stringify({
          username: "local-owner",
          email: null,
          display_name: "Local Owner",
          groups: ["admin"],
          is_admin: true,
          source: "local",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (!route) {
      return new Response(JSON.stringify({ code: "not_found", message: url }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (route.text !== undefined) {
      return new Response(route.text, { status: route.status ?? 200 });
    }
    return new Response(JSON.stringify(route.json ?? {}), {
      status: route.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  };
}

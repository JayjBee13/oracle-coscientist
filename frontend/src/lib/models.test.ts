import { describe, expect, it } from "vitest";

import { makeCapabilities, tablesOf } from "../test/fixtures";
import {
  GRAFT_ONLY_ROLES,
  PRE_RUN_ROLES,
  applyOverrides,
  modelLabel,
  resolveTier,
  runRoles,
} from "./models";

/**
 * The one fold of `/api/capabilities`, tested once.
 *
 * Nothing here names a model id, a tier or an effort: the fixture's are
 * deliberate placeholders, and an assertion that named a real one would be
 * asserting our list of models rather than the engine's.
 */

const MODELS = makeCapabilities().models;
const TIER = MODELS.default_tier;
const TABLES = tablesOf(makeCapabilities());
const ROWS = TABLES[TIER];

describe("resolveTier", () => {
  it("uses the tier it is given when the payload publishes it", () => {
    expect(resolveTier(MODELS, null, TIER)).toEqual({
      tier: TIER,
      rows: ROWS,
      missing: null,
    });
  });

  it("uses the payload's default when no tier is asked for", () => {
    expect(resolveTier(MODELS, null, null).tier).toBe(MODELS.default_tier);
    expect(resolveTier(MODELS, null, null).missing).toBeNull();
  });

  it("falls back and reports the tier it dropped", () => {
    // `??` only catches null and undefined, so a tier string that is simply not
    // a key used to yield an empty table: every step with no model, no effort
    // and no note, silently.
    const resolved = resolveTier(MODELS, null, "a-tier-that-was-retired");
    expect(resolved.tier).toBe(MODELS.default_tier);
    expect(resolved.rows).toEqual(ROWS);
    expect(resolved.missing).toBe("a-tier-that-was-retired");
  });

  it("has nothing to say before the payload arrives", () => {
    expect(resolveTier(null, null, TIER)).toEqual({
      tier: TIER,
      rows: [],
      missing: null,
    });
  });
});

describe("runRoles", () => {
  it("drops the roles a launched run never executes", () => {
    const kept = runRoles(ROWS).map((row) => row.role);
    expect(kept.length).toBeGreaterThan(0);
    for (const role of kept) {
      expect(PRE_RUN_ROLES.has(role)).toBe(false);
      expect(GRAFT_ONLY_ROLES.has(role)).toBe(false);
    }
    // The fixture must actually contain both, or this proves nothing.
    expect(ROWS.some((row) => PRE_RUN_ROLES.has(row.role))).toBe(true);
    expect(ROWS.some((row) => GRAFT_ONLY_ROLES.has(row.role))).toBe(true);
  });

  it("keeps the collapse-only role when the injection that fires it is on", () => {
    const kept = runRoles(ROWS, { graft: true }).map((row) => row.role);
    expect(kept.some((role) => GRAFT_ONLY_ROLES.has(role))).toBe(true);
    // The pre-run role is never part of a run, whatever else is switched on.
    expect(kept.some((role) => PRE_RUN_ROLES.has(role))).toBe(false);
  });
});

describe("applyOverrides", () => {
  it("changes only the cells the run moved", () => {
    const target = ROWS[0];
    const other = MODELS.catalog.find((choice) => choice.id !== target.model)!;
    const applied = applyOverrides(ROWS, { [target.role]: { model: other.id } });

    expect(applied[0]).toEqual({ ...target, model: other.id });
    expect(applied.slice(1)).toEqual(ROWS.slice(1));
  });

  it("passes the tier's table straight through when nothing was moved", () => {
    expect(applyOverrides(ROWS, {})).toEqual(ROWS);
    expect(applyOverrides(ROWS, null)).toEqual(ROWS);
  });
});

describe("modelLabel", () => {
  it("uses the catalog's own name for a model it lists", () => {
    for (const choice of MODELS.catalog) {
      expect(modelLabel(choice.id, MODELS.catalog)).toBe(choice.label);
    }
  });

  it("shows what a retired model was rather than substituting another", () => {
    expect(modelLabel("claude-something-retired", MODELS.catalog)).toBe(
      "something-retired",
    );
    expect(modelLabel(null, MODELS.catalog)).toBeNull();
  });
});

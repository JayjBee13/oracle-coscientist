import type { RecommendedSettings, RunConfig } from "../../api/types";
import { Chip } from "../../components/Status";
import {
  PRESETS,
  estimateCalls,
  matchPreset,
  presetConfig,
  suggestedBudgetCalls,
} from "../../lib/estimates";
import { METRIC_HINTS } from "../../lib/status";
import { RoleMatrix } from "./RoleMatrix";
import { CheckboxField, EstimateStrip, NumberField, Section, SelectField } from "./parts";

/**
 * How much work to do, and how far to let it go.
 *
 * Two findings shape this screen. The workshop's `recommended_settings` used to
 * be thrown away — it recommended 120 calls over 4 rounds while the form
 * quietly showed 20 over 1 — so the recommendation is applied on arrival and
 * said out loud. And the presets are computed from the same estimate function
 * the engine uses, because the old Quick preset budgeted 8 calls for a round
 * that needs 12 and could not finish by construction.
 */
export function ConfigureStep({
  config,
  provider = null,
  title,
  titlePlaceholder,
  recommended,
  onConfigChange,
  onProviderChange,
  onTitleChange,
  onContinue,
  onBack,
}: {
  config: RunConfig;
  /**
   * Whose table the tier fills from. It sits beside the config rather than in it
   * because it is a resolution input, not a record of the run — see the field's
   * docstring on `WizardDraft`.
   */
  provider?: string | null;
  title: string;
  titlePlaceholder: string;
  recommended: RecommendedSettings | null;
  onConfigChange: (patch: Partial<RunConfig>) => void;
  onProviderChange?: (provider: string) => void;
  onTitleChange: (title: string) => void;
  onContinue: () => void;
  onBack: () => void;
}) {
  const active = matchPreset(config);
  const estimate = estimateCalls(config);
  const underBudgeted = config.budget_calls < estimate;
  const demo = config.runner === "demo";

  const recommendedApplied =
    recommended != null &&
    config.rounds === recommended.rounds &&
    config.matches_per_round === recommended.matches_per_round &&
    config.budget_calls === recommended.budget_calls &&
    config.grounding_depth === recommended.grounding_depth;

  return (
    <div className="card wiz-panel">
      {recommended ? (
        <div
          className="row-wrap"
          style={{
            marginBottom: "var(--space-5)",
            padding: "var(--space-3) var(--space-4)",
            borderRadius: "var(--radius-md)",
            background: "var(--accent-wash)",
            border: "var(--hairline) solid var(--accent-quiet)",
          }}
        >
          <span>
            {recommendedApplied
              ? "These settings came from the direction you chose."
              : "You have changed the settings the chosen direction recommended."}
          </span>
          <span className="spacer" />
          {recommendedApplied ? null : (
            <button
              type="button"
              className="btn btn--sm"
              onClick={() =>
                onConfigChange({
                  rounds: recommended.rounds,
                  matches_per_round: recommended.matches_per_round,
                  budget_calls: recommended.budget_calls,
                  grounding_depth: recommended.grounding_depth,
                })
              }
            >
              Back to recommended
            </button>
          )}
        </div>
      ) : null}

      <Section
        title="How much work"
        aside={
          active ? null : (
            <Chip tone="accent" className="chip--quiet">
              Custom
            </Chip>
          )
        }
      >
        <div className="preset-grid">
          {PRESETS.map((preset) => {
            const shaped = presetConfig(preset);
            return (
              <button
                key={preset.name}
                type="button"
                className="preset"
                aria-pressed={active === preset.name}
                onClick={() => onConfigChange(shaped)}
              >
                <span className="preset__name">{preset.label}</span>
                <span className="muted" style={{ fontSize: "var(--text-sm)" }}>
                  {preset.summary}
                </span>
                <span className="faint numeral" style={{ fontSize: "var(--text-xs)" }}>
                  ~{estimateCalls(preset.shape)} calls · budget {shaped.budget_calls}
                </span>
              </button>
            );
          })}
        </div>
      </Section>

      <Section title="What this will take">
        <EstimateStrip config={config} demo={demo} tier={config.model_tier} />
        {underBudgeted ? (
          <div className="row-wrap" style={{ marginTop: "var(--space-3) " }} role="alert">
            <span className="field__error">
              The call budget ({config.budget_calls}) is below the estimate for these
              settings (~{estimate}). The run would stop before the last round.
            </span>
            <button
              type="button"
              className="btn btn--sm"
              onClick={() =>
                onConfigChange({ budget_calls: suggestedBudgetCalls(config) })
              }
            >
              Raise it to {suggestedBudgetCalls(config)}
            </button>
          </div>
        ) : null}
      </Section>

      <Section title="Name">
        <div className="field">
          <label className="field__label" htmlFor="wizard-title">
            Title for the runs list
          </label>
          <input
            id="wizard-title"
            className="input"
            value={title}
            placeholder={titlePlaceholder}
            onChange={(event) => onTitleChange(event.target.value)}
          />
          <span className="field__hint">
            Left empty, the question's first words are used.
          </span>
        </div>
      </Section>

      <Section title="Everything else">
        <details className="advanced">
          <summary>Advanced settings</summary>
          <div className="advanced__body">
            <label className="field">
              Research workflow
              <select
                value={config.workflow}
                onChange={(event) =>
                  onConfigChange({
                    workflow:
                      event.target.value === "tournament" ? "tournament" : "adaptive",
                  })
                }
              >
                <option value="adaptive">
                  Adaptive research — independent approaches, evidence and complete
                  answers
                </option>
                <option value="tournament">
                  Earlier tournament workflow — for comparison and existing setups
                </option>
              </select>
            </label>
            <div className="num-grid">
              <NumberField
                label="Rounds"
                hint={METRIC_HINTS.round}
                value={config.rounds}
                min={1}
                max={20}
                onCommit={(rounds) => onConfigChange({ rounds })}
              />
              <NumberField
                label="New ideas a round"
                hint={
                  config.workflow === "adaptive"
                    ? "Spread across three or four independent approaches when exploration is selected."
                    : "Split into parallel calls of three."
                }
                value={config.generation_batch}
                min={1}
                max={24}
                onCommit={(generation_batch) => onConfigChange({ generation_batch })}
              />
              <NumberField
                label={
                  config.workflow === "adaptive"
                    ? "Evidence checks per checkpoint"
                    : "Matches a round"
                }
                hint={
                  config.workflow === "adaptive"
                    ? "Three to six independent candidate checks, as needed."
                    : "Head-to-head comparisons that set the Elo."
                }
                value={config.matches_per_round}
                min={0}
                max={30}
                onCommit={(matches_per_round) => onConfigChange({ matches_per_round })}
              />
              <NumberField
                label="Ideas evolved a round"
                hint={
                  config.workflow === "adaptive"
                    ? "Variants developed from a diverse portfolio when targeted development is selected."
                    : "The top few get combined, simplified or grounded."
                }
                value={config.evolve_top_k}
                min={0}
                max={12}
                onCommit={(evolve_top_k) => onConfigChange({ evolve_top_k })}
              />
              <NumberField
                label="Call budget"
                hint={METRIC_HINTS.calls}
                value={config.budget_calls}
                min={1}
                max={2000}
                onCommit={(budget_calls) => onConfigChange({ budget_calls })}
              />
              <NumberField
                label="Wall-clock ceiling (minutes)"
                hint="Optional. Empty means the call budget alone decides when it stops."
                value={config.wall_clock_minutes}
                min={1}
                max={600}
                onCommit={(wall_clock_minutes) => onConfigChange({ wall_clock_minutes })}
                onClear={() => onConfigChange({ wall_clock_minutes: null })}
              />
              {/* Cost is not a governor here — these calls run on a subscription
                  and are not billed per token — but the engine still accepts a
                  ceiling, so the field stays, off by default, named the way the
                  run's Settings tab names it. It can genuinely be emptied. */}
              <NumberField
                label="Cost ceiling (USD)"
                hint="Optional, and off unless you set it. Cost is API-equivalent telemetry, not money."
                value={config.budget_usd}
                min={1}
                max={500}
                onCommit={(budget_usd) => onConfigChange({ budget_usd })}
                onClear={() => onConfigChange({ budget_usd: null })}
              />
            </div>

            <div className="num-grid">
              <SelectField
                label="Grounding"
                hint="How hard the model works to verify claims with a web search."
                value={config.grounding_depth}
                options={[
                  { value: "shallow", label: "Light — search only if essential" },
                  { value: "standard", label: "Standard — verify key claims" },
                  { value: "deep", label: "Deep — verify every major claim" },
                ]}
                onChange={(grounding_depth) => onConfigChange({ grounding_depth })}
              />
              <SelectField
                label="Runner"
                hint="A demo run is scripted end to end and calls no model at all."
                value={config.runner}
                options={[
                  { value: "claude", label: "Claude — real research run" },
                  { value: "demo", label: "Demo — walk through it with no calls" },
                ]}
                onChange={(runner) => onConfigChange({ runner })}
              />
            </div>

            {/* The tier lives inside the matrix it fills: picking one is the
                same act as filling all nine rows, and a per-role change is an
                edit to that filling. Changing the tier drops the overrides,
                because they were differences from a table that no longer
                applies — the matrix is refilled, not patched. */}
            <RoleMatrix
              provider={provider}
              tier={config.model_tier}
              overrides={config.model_overrides}
              // The matrix shows the steps *this* run will execute: no
              // pre-run role, and no collapse-only role while the injection
              // that fires it is off. Tuning a step that cannot run collects a
              // choice the engine will ignore.
              graft={config.graft.enabled}
              demo={demo}
              onTierChange={(tier) =>
                onConfigChange({
                  // The payload's vocabulary is the same allowlist the request
                  // body declares; see `asRoleModel` in ./state for the rule.
                  model_tier: tier as RunConfig["model_tier"],
                  model_overrides: {},
                })
              }
              // Switching provider refills the table from a different vendor's,
              // so the overrides go with it for the same reason a tier change
              // drops them: they were differences from a table that no longer
              // applies.
              onProviderChange={(next) => {
                onProviderChange?.(next);
                onConfigChange({ model_overrides: {} });
              }}
              onOverridesChange={(model_overrides) => onConfigChange({ model_overrides })}
            />

            <div className="stack" style={{ gap: "var(--space-3)" }}>
              <CheckboxField
                checked={config.graft.enabled}
                title="Diversity injection"
                onChange={(enabled) =>
                  onConfigChange({ graft: { ...config.graft, enabled } })
                }
              >
                {METRIC_HINTS.graft} Off by default — it is still being calibrated.
              </CheckboxField>
              {config.graft.enabled ? (
                <div className="num-grid">
                  <NumberField
                    label="Signals needed"
                    hint="How many of the three collapse signals must agree."
                    value={config.graft.quorum_k}
                    min={1}
                    max={3}
                    onCommit={(quorum_k) =>
                      onConfigChange({ graft: { ...config.graft, quorum_k } })
                    }
                  />
                  <NumberField
                    label="Rounds watched"
                    value={config.graft.window}
                    min={2}
                    max={6}
                    onCommit={(window) =>
                      onConfigChange({ graft: { ...config.graft, window } })
                    }
                  />
                  <NumberField
                    label="Rounds between injections"
                    value={config.graft.cooldown}
                    min={1}
                    max={6}
                    onCommit={(cooldown) =>
                      onConfigChange({ graft: { ...config.graft, cooldown } })
                    }
                  />
                </div>
              ) : null}
            </div>
          </div>
        </details>
      </Section>

      <div className="wiz-actions">
        <button type="button" className="btn btn--primary btn--lg" onClick={onContinue}>
          Review and launch
        </button>
        <button type="button" className="btn btn--ghost" onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
}

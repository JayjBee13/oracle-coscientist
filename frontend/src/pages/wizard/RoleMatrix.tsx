import { useId } from "react";

import { MODEL_TIERS } from "../../api/types";
import type { ModelChoice, ModelsPayload, RoleModelRow } from "../../api/types";
import { Chip } from "../../components/Status";
import { EmptyState, ErrorState, SkeletonBlock } from "../../components/States";
import {
  effortsFor,
  groundingLost,
  providerViews,
  runRoles,
  selectableProviders,
  shortModel,
  tableFor,
  tiersFor,
} from "../../lib/models";
import type { ProviderView } from "../../lib/models";
import {
  describeEffort,
  describeModelTier,
  describeProvider,
  describeRole,
} from "../../lib/status";
import { refreshCapabilities, useCapabilities } from "../../store/capabilities";
import {
  RESET_ALL_LABEL,
  RESET_ROLE_LABEL,
  asRoleEffort,
  asRoleModel,
  clearRoleOverride,
  isRoleOverridden,
  overriddenRoles,
  resetRoleLabel,
  resolveRole,
  setRoleOverride,
} from "./state";
import type { RoleOverride, RoleOverrides } from "./state";

/**
 * Which model runs which step, and how hard it thinks.
 *
 * A run is a loop of model calls, and they are not the same job: one proposes
 * hypotheses, one decides whether an idea survives at all, one labels
 * near-duplicates. A single whole-run tier makes them all move together, which
 * is only ever right by accident — so the provider and the tier fill this table
 * and any cell in it can then be moved on its own.
 *
 * Everything on screen is the engine's, not ours. The rows, the providers, the
 * models offered, the effort vocabulary of each of those models and the per-row
 * reason all come from `/api/capabilities`, which the backend builds from the
 * same role catalogue the runner reads. Nothing here types a model id, a
 * provider, a tier name or an effort value: a client that writes its own model
 * list is a client that can offer a model the engine will refuse.
 *
 * Two rows the payload publishes are nonetheless not offered, because a choice
 * made in them cannot take effect on *this* run: the pre-run role, which is
 * called before a run exists and never by the orchestrator, and the
 * collapse-only role while the injection that fires it is switched off. The
 * rule lives in `lib/models` so the Confirm step filters identically.
 *
 * **The provider is a quick-set, not a lock.** It fills the table; every row's
 * model select still offers every model both vendors publish, grouped by vendor,
 * because a run may legitimately send its tournament to one and its clustering
 * to the other. That is why the effort vocabulary is read off the *selected
 * model* rather than off the provider segment above it — the ladders differ per
 * model, and offering a rung a model does not take is offering a call the
 * backend has to refuse.
 */
export function RoleMatrix({
  provider,
  tier,
  overrides,
  graft = false,
  demo = false,
  onProviderChange,
  onTierChange,
  onOverridesChange,
}: {
  /** Whose table the tier fills from. `null` before the payload names one. */
  provider: string | null;
  tier: string;
  overrides: RoleOverrides;
  /** Whether the diversity injection is on; its role is hidden when it is not. */
  graft?: boolean;
  /** A demo run calls no model, so it must not be shown or offered any. */
  demo?: boolean;
  /** Switching provider refills the table, so the caller also drops the overrides. */
  onProviderChange: (provider: string) => void;
  /** Choosing a tier refills the table, so the caller also drops the overrides. */
  onTierChange: (tier: string) => void;
  onOverridesChange: (overrides: RoleOverrides) => void;
}) {
  const { status, data, error } = useCapabilities();
  const models = data?.models ?? null;
  const providerId = useId();
  const providerHintId = useId();
  const tierId = useId();
  const tierHintId = useId();
  const notesId = useId();

  const rows: RoleModelRow[] = runRoles(tableFor(models, provider, tier), { graft });
  const changed = overriddenRoles(overrides);
  // The payload's own lists, so a provider or a tier added on the backend appears
  // here without an edit. Before the payload arrives, the generated tier
  // vocabulary stands in; there is no generated provider vocabulary to stand in
  // with, and inventing one would be this file naming a vendor.
  const providers = selectableProviders(models);
  const tiers = withValue(models ? tiersFor(models, provider) : [...MODEL_TIERS], tier);
  const groups = providerViews(models);
  const notes = models?.notes ?? [];

  return (
    <section className="role-matrix" aria-labelledby={`${tierId}-title`}>
      <div className="role-matrix__head">
        {/* Only once there is a second one. A segment with one option is a
            statement dressed as a choice, and on today's payload that is
            exactly what it would be. */}
        {providers.length > 1 ? (
          <div className="field role-matrix__tier">
            <label className="field__label" htmlFor={providerId}>
              Provider
            </label>
            <select
              id={providerId}
              className="select"
              value={provider ?? ""}
              aria-describedby={providerHintId}
              onChange={(event) => onProviderChange(event.target.value)}
            >
              {providers.map((entry) => (
                <option key={entry.id} value={entry.id}>
                  {describeProvider(entry.id).label}
                </option>
              ))}
            </select>
            <span className="field__hint" id={providerHintId}>
              Fills the table below. Any single step can still be sent to the other one.
            </span>
          </div>
        ) : null}

        <div className="field role-matrix__tier">
          <label className="field__label" htmlFor={tierId}>
            Model tier
          </label>
          <select
            id={tierId}
            className="select"
            value={tier}
            aria-describedby={tierHintId}
            onChange={(event) => onTierChange(event.target.value)}
          >
            {tiers.map((name) => (
              <option key={name} value={name}>
                {describeModelTier(name).label}
              </option>
            ))}
          </select>
          <span className="field__hint" id={tierHintId}>
            Fills the table below. Changing it puts every step back to that tier&rsquo;s
            own choice.
          </span>
        </div>

        <div className="role-matrix__headline">
          <h3 className="label" id={`${tierId}-title`}>
            What each step runs on
          </h3>
          <p className="muted role-matrix__lede">
            {demo ? (
              <>
                A demo run calls no model at all, so there is nothing to set. This is what
                a real run of these settings would use.
              </>
            ) : rows.length > 0 ? (
              <>
                Each of the {rows.length} steps can be set on its own. Anything you change
                is sent as an override for that step alone.
              </>
            ) : (
              <>
                Each step can be set on its own. Anything you change is sent as an
                override for that step alone.
              </>
            )}
          </p>
          {changed.length > 0 ? (
            <div className="row-wrap role-matrix__dirty">
              <Chip tone="accent" className="chip--quiet">
                {changed.length === 1
                  ? "1 step changed"
                  : `${changed.length} steps changed`}
              </Chip>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => onOverridesChange({})}
              >
                {RESET_ALL_LABEL}
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {models == null && status !== "error" ? (
        <SkeletonBlock height={200} />
      ) : models == null ? (
        <ErrorState
          title="The model table did not load"
          message={
            error ??
            "The backend did not say which model each step runs on. The tier above still applies."
          }
          onRetry={() => void refreshCapabilities()}
        />
      ) : rows.length === 0 ? (
        <EmptyState title="No table for this tier">
          The engine did not describe {describeModelTier(tier).label}, so there is nothing
          to fill the rows with. The run will still use it.
        </EmptyState>
      ) : (
        <>
          <div className="scroll-x">
            <table
              className="table role-matrix__table"
              aria-describedby={notes.length > 0 ? notesId : undefined}
            >
              <caption className="visually-hidden">
                One row per step of the loop: the model it runs on, how much thinking
                effort it gets, and whether it may search the web.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Step</th>
                  <th scope="col">Model</th>
                  <th scope="col">Thinking effort</th>
                  <th scope="col">Web search</th>
                  <th scope="col">
                    <span className="visually-hidden">Back to the default</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <RoleRow
                    key={row.role}
                    row={row}
                    models={models}
                    groups={groups}
                    demo={demo}
                    override={overrides[row.role]}
                    dirty={isRoleOverridden(overrides, row.role)}
                    onPatch={(patch) =>
                      onOverridesChange(setRoleOverride(overrides, row, patch))
                    }
                    onReset={() =>
                      onOverridesChange(clearRoleOverride(overrides, row.role))
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>

          {notes.length > 0 && !demo ? (
            <div className="role-matrix__notes" id={notesId}>
              {/* The escalations no table can express: they come out of run
                  state, not configuration, and someone who has just set the
                  tournament to a middling effort deserves to know it still
                  runs high on the matches that decide the leaderboard. */}
              <h4 className="label">Effort the engine raises on its own</h4>
              <ul>
                {notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {demo ? null : (
            <div className="role-matrix__about">
              <h4 className="label">The models</h4>
              <dl>
                {models.catalog.map((choice) => (
                  <div key={choice.id}>
                    <dt>{choice.label}</dt>
                    {/* The engine's own recommendation, rendered rather than
                        re-written. */}
                    <dd>{choice.recommendation}</dd>
                  </div>
                ))}
              </dl>
              <p className="role-matrix__foot">
                What these choices spend is wall-clock time and your plan&rsquo;s rate
                limits.
              </p>
            </div>
          )}
        </>
      )}
    </section>
  );
}

/* --- One step -------------------------------------------------------------- */

function RoleRow({
  row,
  models,
  groups,
  demo,
  override,
  dirty,
  onPatch,
  onReset,
}: {
  row: RoleModelRow;
  models: ModelsPayload;
  groups: readonly ProviderView[];
  demo: boolean;
  override: RoleOverride | undefined;
  dirty: boolean;
  onPatch: (patch: {
    model?: RoleOverride["model"];
    effort?: RoleOverride["effort"];
  }) => void;
  onReset: () => void;
}) {
  const noteId = useId();
  const modelId = useId();
  const effortId = useId();

  const label = describeRole(row.role);
  const resolved = resolveRole(row, override);

  // A tier may name a model or an effort the catalogue no longer offers — an
  // older engine, a value being retired. It is still what this step would run,
  // so it stays in the list rather than silently becoming something else.
  const effortOptions = withOption(
    effortsFor(models, resolved.model).map((effort) => ({
      value: effort,
      label: describeEffort(effort),
    })),
    resolved.effort,
    describeEffort,
  );
  // The step wants the web and the model it is set to cannot reach it. Both
  // halves are the payload's assertion; a backend that has not published model
  // grounding raises nothing here at all.
  const ungrounded = groundingLost(models, row, resolved.model);

  return (
    <tr data-changed={dirty ? "true" : undefined}>
      <th scope="row" className="role-matrix__step">
        <span className="role-matrix__role">{label}</span>
        {/* The engine's plain-language reason this step is set the way it is.
            It is the recommendation; both selects are described by it. */}
        <span className="role-matrix__note" id={noteId}>
          {row.note}
        </span>
      </th>
      {/* A demo run invokes nothing, so the cells state that rather than naming
          a model the run will not call and offering a choice it cannot honour. */}
      <td>
        {demo ? (
          <span className="muted" aria-label={`No model for ${label} — demo run`}>
            —
          </span>
        ) : (
          <>
            <label className="visually-hidden" htmlFor={modelId}>
              {`Model for ${label}`}
            </label>
            <select
              id={modelId}
              className="select role-matrix__select"
              value={resolved.model}
              aria-describedby={noteId}
              onChange={(event) => onPatch({ model: asRoleModel(event.target.value) })}
            >
              <ModelOptions groups={groups} current={resolved.model} />
            </select>
          </>
        )}
      </td>
      <td>
        {demo ? (
          <span className="muted">—</span>
        ) : (
          <>
            <label className="visually-hidden" htmlFor={effortId}>
              {`Thinking effort for ${label}`}
            </label>
            <select
              id={effortId}
              className="select role-matrix__select"
              value={resolved.effort}
              aria-describedby={noteId}
              onChange={(event) => onPatch({ effort: asRoleEffort(event.target.value) })}
            >
              {effortOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </>
        )}
      </td>
      <td className="role-matrix__grounded">
        {/* Whether a step *may* search is the engine's, and it is here because it
            changes how the row reads. Whether the model it is set to *can* is a
            different fact, and when the two disagree the row says so — a step
            marked for the web, on a model that cannot reach it, is the one
            silent downgrade this screen can produce. */}
        {demo ? (
          <span className="muted">—</span>
        ) : ungrounded ? (
          <Chip
            tone="caution"
            className="chip--quiet"
            title={`${label} searches the web, and this model cannot. It will work from the run's own material instead.`}
          >
            Not on this model
          </Chip>
        ) : row.grounded ? (
          <Chip
            tone="info"
            className="chip--quiet"
            title={`${label} may search the web to check what it writes.`}
          >
            Yes
          </Chip>
        ) : (
          <span className="muted" title={`${label} works from the run's own material.`}>
            No
          </span>
        )}
      </td>
      <td className="role-matrix__reset">
        {dirty && !demo ? (
          <button
            type="button"
            className="btn btn--sm btn--ghost"
            aria-label={resetRoleLabel(label)}
            onClick={onReset}
          >
            {RESET_ROLE_LABEL}
          </button>
        ) : null}
      </td>
    </tr>
  );
}

/**
 * Every model both vendors publish, grouped by vendor.
 *
 * Grouped only when there is more than one group to name: an `<optgroup>` around
 * the whole list is a heading that distinguishes nothing, and on a
 * single-provider payload that is what it would be. The current value is added
 * to the list when the catalog has forgotten it, so a run cloned from before a
 * model was retired still shows what it will actually run.
 */
function ModelOptions({
  groups,
  current,
}: {
  groups: readonly ProviderView[];
  current: string;
}) {
  const listed = groups.some((group) =>
    group.models.some((choice) => choice.id === current),
  );
  const retired =
    !listed && current ? <option value={current}>{shortModel(current)}</option> : null;

  if (groups.length <= 1) {
    return (
      <>
        {retired}
        {(groups[0]?.models ?? []).map((choice) => (
          <ModelOption key={choice.id} choice={choice} />
        ))}
      </>
    );
  }

  return (
    <>
      {retired}
      {groups.map((group) => (
        <optgroup key={group.id} label={describeProvider(group.id).label}>
          {group.models.map((choice) => (
            <ModelOption key={choice.id} choice={choice} />
          ))}
        </optgroup>
      ))}
    </>
  );
}

function ModelOption({ choice }: { choice: ModelChoice }) {
  return <option value={choice.id}>{choice.label}</option>;
}

/* --- Small helpers --------------------------------------------------------- */

type Option = { value: string; label: string };

/** The served list, with the current value in it even when the list forgot it. */
function withOption(
  options: Option[],
  current: string,
  labelFor: (value: string) => string,
): Option[] {
  if (!current || options.some((option) => option.value === current)) return options;
  return [{ value: current, label: labelFor(current) }, ...options];
}

function withValue(values: string[], current: string): string[] {
  return current && !values.includes(current) ? [current, ...values] : values;
}

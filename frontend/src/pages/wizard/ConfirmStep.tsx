import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { ContextDocInput, RunConfig } from "../../api/types";
import { SkeletonBlock } from "../../components/States";
import { Chip, LabelChip } from "../../components/Status";
import { WorkflowDiagram } from "../../components/WorkflowDiagram";
import {
  applyOverrides,
  groundingLost,
  modelLabel,
  providerOf,
  runRoles,
  tableFor,
  tiersFor,
} from "../../lib/models";
import {
  describeEffort,
  describeGroundingDepth,
  describeModelTier,
  describeProvider,
  describeRole,
} from "../../lib/status";
import { useCapabilities } from "../../store/capabilities";
import { EstimateStrip, Section } from "./parts";

/**
 * The promise this app makes about stopping a run. It is quoted verbatim from
 * the plan's Confirm-step copy and it is the reason someone is willing to press
 * Launch on something that will run for half an hour. The orchestrator honours
 * it: a stop still writes the overview from whatever exists.
 */
export const REASSURANCE =
  "You can pause or stop at any time — stopping still writes a report from what exists.";

export type LaneConflict = { runId: string | null; message: string };

export function ConfirmStep({
  config,
  provider = null,
  question,
  prompt,
  title,
  contextDocs,
  notify,
  launching,
  laneConflict,
  onNotifyChange,
  onUseDemo,
  onUseClaude,
  onLaunch,
  onBack,
}: {
  config: RunConfig;
  /** Whose table the tier filled from. Null on a payload with only one. */
  provider?: string | null;
  question: string;
  prompt: string;
  title: string;
  contextDocs: ContextDocInput[];
  notify: boolean;
  launching: boolean;
  laneConflict: LaneConflict | null;
  onNotifyChange: (notify: boolean) => void;
  onUseDemo: () => void;
  onUseClaude: () => void;
  onLaunch: () => void;
  onBack: () => void;
}) {
  const demo = config.runner === "demo";
  const capabilities = useCapabilities();
  const models = capabilities.data?.models ?? null;
  const grounding = describeGroundingDepth(config.grounding_depth);

  // The same fold the diagram above the table uses, from the same payload, with
  // this run's own per-role choices laid over it. The two used to be built from
  // different sources and disagreed on screen — the strip said "Generation ·
  // Fable 5 · High" while the table three inches below said "generation ·
  // claude-opus-5 · Medium" — which is what a hand-written mirror of the engine
  // buys you a fortnight after the engine moves.
  //
  // Resolved through the provider as well as the tier, because with two vendors
  // `(tier, overrides)` no longer identifies a table: the launcher resolves
  // through all three, and a Confirm step that dropped one of them would print a
  // table the run does not then use.
  const published = tiersFor(models, provider);
  const tier = published.includes(config.model_tier)
    ? config.model_tier
    : (models?.default_tier ?? config.model_tier);
  const retired = tier === config.model_tier ? null : config.model_tier;
  const modelTable = applyOverrides(
    runRoles(tableFor(models, provider, tier), { graft: config.graft.enabled }),
    config.model_overrides,
  );
  const catalog = models?.catalog ?? [];
  const notes = models?.notes ?? [];
  // Named off the rows actually on screen rather than off the segment that was
  // clicked, and that is not pedantry: if the selected provider and the table
  // being rendered ever disagree — a provider whose tables a backend has not
  // published yet, a stored default naming a vendor that has been retired — the
  // table is what the run will use, and a chip claiming the other vendor over
  // the top of it is the silent substitution this whole feature is built to
  // prevent. Shown only when the payload distinguishes vendors at all; naming
  // one on a single-provider backend is a distinction nobody can act on.
  const shownProvider = providerOf(models, modelTable[0]?.model) ?? provider;
  // Label and tone described once, off the same value. They were read off two
  // different ones — the label off the table, the tone off the segment, behind a
  // `!` asserting the two agreed — and a run whose table names a vendor while its
  // config names none took this step down outright. That is not a corner: a clone
  // of a run written before `provider` existed has no vendor of its own to state,
  // and its table resolves from the tier all the same.
  const providerChip =
    shownProvider && (models?.providers?.length ?? 0) > 1
      ? describeProvider(shownProvider)
      : null;

  return (
    <div className="stack" style={{ gap: "var(--space-5)" }}>
      <div className="card wiz-panel">
        <Section title="What is about to run">
          <div className="confirm-lines">
            <ConfirmLine label="Title">{title}</ConfirmLine>
            <ConfirmLine label="Question">{question}</ConfirmLine>
            <ConfirmLine label="Runs on">
              {demo ? (
                <>
                  <Chip tone="info">Demo</Chip>{" "}
                  <span className="muted">
                    No model calls at all — the same interface, scripted results.
                  </span>
                </>
              ) : (
                <>
                  <Chip tone="accent">Claude</Chip>{" "}
                  <span className="muted">
                    Real model calls against your subscription.
                  </span>
                </>
              )}
            </ConfirmLine>
            <ConfirmLine label="Plan">
              <span className="tabular">
                {config.rounds} round{config.rounds === 1 ? "" : "s"} ·{" "}
                {config.workflow === "adaptive"
                  ? `${config.generation_batch} ideas during exploration · ${Math.max(3, Math.min(config.matches_per_round, 6))} evidence checks per checkpoint · ${Math.max(1, config.evolve_top_k)} development variants`
                  : `${config.generation_batch} new ideas a round · ${config.matches_per_round} matches · ${config.evolve_top_k} evolved`}
              </span>
            </ConfirmLine>
            <ConfirmLine label="Grounding">{grounding.label}</ConfirmLine>
            {contextDocs.length > 0 ? (
              <ConfirmLine label="Context">
                {contextDocs.map((doc) => doc.name).join(", ")}
              </ConfirmLine>
            ) : null}
            <ConfirmLine label="Diversity injection">
              {config.graft.enabled
                ? `On — fires when ${config.graft.quorum_k} of 3 collapse signals agree`
                : "Off"}
            </ConfirmLine>
          </div>
        </Section>

        <Section title="Prompt">
          <div className="opt__prompt" style={{ maxHeight: 220 }}>
            {prompt}
          </div>
        </Section>
      </div>

      <div className="card wiz-panel">
        {/* The shape of the thing, before the table of what runs it. Reassurance
            rather than instruction: someone at this step has already decided to
            launch, and needs to recognise what they are starting — that it is a
            loop, that most of it happens at once, and that the report at the end
            is not optional. The full explanation is one link away. */}
        <Section
          title="How this run will develop an answer"
          aside={
            <Link className="btn btn--sm btn--ghost" to="/how-it-works">
              How this works
            </Link>
          }
        >
          <WorkflowDiagram
            workflow={config.workflow ?? "adaptive"}
            models={models}
            provider={provider}
            tier={config.model_tier}
            overrides={config.model_overrides}
            variant="compact"
            demo={demo}
          />
        </Section>

        <Section
          title="Models this run will use"
          aside={
            <span className="row" style={{ gap: "var(--space-2)" }}>
              {providerChip ? (
                <Chip tone={providerChip.tone} className="chip--quiet">
                  {providerChip.label}
                </Chip>
              ) : null}
              <LabelChip value={describeModelTier(tier)} />
            </span>
          }
        >
          {models == null ? (
            <SkeletonBlock height={180} />
          ) : (
            <>
              {/* A tier this run was configured with and the engine no longer
                  publishes. Naming the fallback beats drawing eight blank rows,
                  and beats drawing the fallback's rows without saying so. */}
              {retired ? (
                <p className="faint" style={{ marginBottom: "var(--space-3)" }}>
                  {describeModelTier(retired).label} is no longer offered, so this run
                  will use {describeModelTier(tier).label}.
                </p>
              ) : null}
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
                    {modelTable.map((row) => (
                      <tr key={row.role}>
                        <td>{describeRole(row.role)}</td>
                        <td>
                          {demo ? "—" : (modelLabel(row.model, catalog) ?? "—")}
                          {/* The one silent downgrade this screen can hide: a
                              step the engine marks for the web, set to a model
                              that cannot reach it. Both halves are the payload's
                              assertion, so a backend that has not published
                              model grounding shows nothing here. */}
                          {!demo && groundingLost(models, row, row.model) ? (
                            <>
                              {" "}
                              <Chip
                                tone="caution"
                                className="chip--quiet"
                                title={`${describeRole(row.role)} searches the web, and this model cannot. It will work from the run's own material instead.`}
                              >
                                No web search
                              </Chip>
                            </>
                          ) : null}
                        </td>
                        <td>{demo ? "—" : describeEffort(row.effort)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {demo ? (
                <p className="faint" style={{ marginTop: "var(--space-3)" }}>
                  A demo run calls no models at all — this table is what a real run of
                  these settings would use.
                </p>
              ) : notes.length > 0 ? (
                /* The engine's own escalations, served rather than restated: the
                   table cannot express "round 1 runs higher than this", and a
                   hand-written sentence saying so went stale within the month. */
                <ul className="faint confirm-notes">
                  {notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              ) : null}
            </>
          )}
        </Section>

        <Section title="Estimated">
          <EstimateStrip config={config} demo={demo} tier={config.model_tier} />
          {demo ? null : (
            <p className="faint" style={{ marginTop: "var(--space-3)" }}>
              Estimates, not quotes. The run stops itself at{" "}
              <span className="tabular">{config.budget_calls}</span> model calls
              {config.wall_clock_minutes != null ? (
                <>
                  {" "}
                  or after <span className="tabular">
                    {config.wall_clock_minutes}
                  </span>{" "}
                  minutes, whichever comes first
                </>
              ) : null}
              .
            </p>
          )}
        </Section>
      </div>

      <div className="card wiz-panel">
        <p className="reassurance">{REASSURANCE}</p>

        <div style={{ marginTop: "var(--space-4)" }}>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={notify}
              onChange={(event) => onNotifyChange(event.target.checked)}
            />
            <span>
              <span style={{ display: "block", color: "var(--text-strong)" }}>
                Tell me when it finishes
              </span>
              <span className="faint">
                A desktop notification, so you can close the tab and walk away. The
                browser will ask for permission once.
              </span>
            </span>
          </label>
        </div>

        {laneConflict ? (
          <div
            className="stack"
            role="alert"
            style={{
              marginTop: "var(--space-4)",
              padding: "var(--space-4)",
              borderRadius: "var(--radius-md)",
              border: "var(--hairline) solid var(--border-strong)",
              background: "var(--caution-wash)",
              gap: "var(--space-3)",
            }}
          >
            <strong style={{ color: "var(--text-strong)" }}>
              Something is already running here
            </strong>
            <span>{laneConflict.message}</span>
            <div className="row-wrap">
              {laneConflict.runId ? (
                <Link className="btn" to={`/runs/${laneConflict.runId}`}>
                  Open the run that is holding the lane
                </Link>
              ) : null}
              {demo ? null : (
                <button type="button" className="btn" onClick={onUseDemo}>
                  Run as a demo instead
                </button>
              )}
            </div>
            <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
              Real runs use shared model capacity. Try again when it becomes available; a
              demo uses no model and can run separately.
            </span>
          </div>
        ) : null}

        <div className="wiz-actions">
          <button
            type="button"
            className="btn btn--primary btn--lg"
            onClick={onLaunch}
            disabled={launching}
          >
            {launching ? "Launching…" : demo ? "Launch demo run" : "Launch run"}
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onBack}
            disabled={launching}
          >
            Back
          </button>
          <span className="spacer" />
          {demo ? (
            <button
              type="button"
              className="btn btn--sm"
              onClick={onUseClaude}
              disabled={launching}
            >
              Run for real instead
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--sm"
              onClick={onUseDemo}
              disabled={launching}
            >
              Run as a demo instead
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ConfirmLine({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="confirm-line">
      <span className="confirm-line__key">{label}</span>
      <span className="confirm-line__value">{children}</span>
    </div>
  );
}

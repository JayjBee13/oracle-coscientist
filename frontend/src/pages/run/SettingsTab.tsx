import type { RunConfig, RunDetail, RunSummary } from "../../api/types";
import { SkeletonBlock } from "../../components/States";
import { compactNumber, formatUsd } from "../../lib/format";
import { modelLabel } from "../../lib/models";
import {
  METRIC_HINTS,
  describeGroundingDepth,
  describeHarness,
  describeModelTier,
  describeRole,
  humanize,
} from "../../lib/status";
import { useCapabilities } from "../../store/capabilities";

/**
 * What this run was told to do, and which models it resolved to.
 *
 * Configuration is immutable once a run launches — that is a property of the
 * engine, so the tab says it rather than offering inputs that would silently do
 * nothing. Changing something means launching another run, which is what "Run
 * again" is for.
 *
 * Cost appears here and nowhere prominent, as one labelled footnote. These are
 * subscription CLI calls: no token is billed, so the dollar figure is
 * API-equivalent telemetry rather than money, and it is never a ceiling, never
 * a warning colour and never "$0.00". A run with no cost ceiling set says so
 * rather than implying one it does not have.
 */
export function SettingsTab({
  run,
  detail,
}: {
  run: RunSummary;
  detail: RunDetail | null;
}) {
  // Called unconditionally: the catalog is only used to turn a stored model id
  // into the same name the wizard and the diagram print for it.
  const capabilities = useCapabilities();
  const catalog = capabilities.data?.models.catalog ?? [];

  if (!detail) return <SkeletonBlock height={240} />;

  const config = detail.config as Partial<RunConfig> | null;
  const demo = run.harness === "demo";
  const spend = demo ? null : formatUsd(detail.budget.spend_usd);
  const costCeiling = formatUsd(config?.budget_usd);

  // Imported runs carry `budget_calls: 0` and no stored config, which the grid
  // printed as a flat "0" — a run allowed no calls at all, which is exactly what
  // it was not. Calls are the governor of this whole interface; the one screen
  // that states the governor's ceiling has to state it honestly.
  const callCeiling =
    config?.budget_calls ?? (run.budget_calls > 0 ? run.budget_calls : null);

  return (
    <div className="ws-grid">
      <div className="stack">
        <section className="panel">
          <header className="panel__head">
            <span className="label">The question</span>
          </header>
          <div className="panel__body">
            <p className="prose">{run.question}</p>
          </div>
        </section>

        <section className="panel">
          <header className="panel__head">
            <span className="label">How it was set up</span>
            <span className="spacer" />
            <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
              Fixed at launch
            </span>
          </header>
          <div className="panel__body">
            <dl className="kv">
              <dt>Workflow</dt>
              <dd>
                {config?.workflow === "adaptive"
                  ? "Adaptive research"
                  : "Tournament (original workflow)"}
              </dd>
              <dt>Rounds</dt>
              <dd className="numeral" title={METRIC_HINTS.round}>
                {value(config?.rounds ?? run.rounds_target)}
              </dd>

              <dt>New ideas per round</dt>
              <dd className="numeral">{value(config?.generation_batch)}</dd>

              <dt>
                {config?.workflow === "adaptive"
                  ? "Evidence review breadth (3–6)"
                  : "Matches per round"}
              </dt>
              <dd className="numeral">
                {value(
                  config?.workflow === "adaptive"
                    ? Math.max(3, Math.min(config.matches_per_round ?? 3, 6))
                    : config?.matches_per_round,
                )}
              </dd>

              <dt>
                {config?.workflow === "adaptive"
                  ? "Development variants"
                  : "Evolved from top"}
              </dt>
              <dd className="numeral">{value(config?.evolve_top_k)}</dd>

              <dt>Call ceiling</dt>
              <dd
                className={callCeiling == null ? undefined : "numeral"}
                title={METRIC_HINTS.calls}
              >
                {callCeiling == null
                  ? run.source === "imported"
                    ? "None recorded — imported run"
                    : "No call ceiling set"
                  : String(callCeiling)}
              </dd>

              {/* Only ever the *absence* of a ceiling appears in this grid. A
                  run that really had one is history, and history belongs beside
                  the API-equivalent total in the footnote below, not in the same
                  register as the call ceiling — the two are not peers. */}
              {costCeiling ? null : (
                <>
                  <dt>Cost ceiling</dt>
                  <dd title="These calls run on a subscription, so cost is telemetry rather than a limit.">
                    None — cost does not limit this run
                  </dd>
                </>
              )}

              <dt>Evidence</dt>
              <dd>
                {config?.grounding_depth
                  ? describeGroundingDepth(config.grounding_depth).label
                  : "Not recorded"}
              </dd>

              <dt>Model choice</dt>
              <dd>
                {config?.model_tier
                  ? describeModelTier(config.model_tier).label
                  : "Not recorded"}
              </dd>

              <dt>Diversity injection</dt>
              <dd title={METRIC_HINTS.graft}>
                {run.graft.enabled
                  ? `On — fires after ${config?.graft?.quorum_k ?? "?"} of 3 signals agree`
                  : "Off"}
              </dd>

              <dt>Run by</dt>
              <dd>{describeHarness(run.harness).label}</dd>
            </dl>
            {costCeiling ? (
              <p className="footnote" style={{ marginTop: "var(--space-3)" }}>
                The engine that ran this also stopped at {costCeiling}. Cost no longer
                limits a run — these calls go through a subscription and are not billed
                per token.
              </p>
            ) : null}
          </div>
        </section>

        <section className="panel">
          <header className="panel__head">
            <span className="label">Models it resolved to</span>
          </header>
          <div className="panel__body">
            {demo ? (
              <p className="tab-note">
                A demo run makes no model calls at all, so no model was resolved. The
                loop, the events and the artifacts are real; the answers are scripted.
              </p>
            ) : detail.model_table.length === 0 ? (
              <p className="tab-note">
                No model table was recorded — imported runs predate this field.
              </p>
            ) : (
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
                    {detail.model_table.map((row) => (
                      <tr key={`${row.role}-${row.model}`}>
                        <td>{describeRole(String(row.role))}</td>
                        <td title={String(row.model)}>
                          {modelLabel(String(row.model), catalog) ?? String(row.model)}
                        </td>
                        <td>{humanize(String(row.effort))}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>
      </div>

      <div className="stack">
        <section className="panel">
          <header className="panel__head">
            <span className="label">Where the calls went</span>
          </header>
          <div className="panel__body stack">
            {detail.budget.by_role.length === 0 ? (
              <p className="tab-note">
                {run.source === "imported"
                  ? "No per-call ledger survives for imported runs."
                  : demo
                    ? "A demo run is scripted, so it kept no ledger."
                    : "No model calls have been recorded yet."}
              </p>
            ) : (
              <>
                <div className="scroll-x">
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">Step</th>
                        <th scope="col" title={METRIC_HINTS.calls}>
                          Calls
                        </th>
                        <th scope="col" title={METRIC_HINTS.tokens}>
                          Tokens
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.budget.by_role.map((row) => (
                        <tr key={String(row.role)}>
                          <td>{describeRole(String(row.role))}</td>
                          <td className="numeral">{row.calls}</td>
                          <td className="numeral">{compactNumber(row.tokens)}</td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr>
                        <td>
                          <strong>Total</strong>
                        </td>
                        <td className="numeral">{detail.budget.calls_used}</td>
                        <td className="numeral">{compactNumber(run.tokens_total)}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
                <p className="footnote">
                  {demo
                    ? "A demo run invokes no model, so these steps cost nothing at all."
                    : spend
                      ? `≈ ${spend} API-equivalent — what these calls would have cost on the API. They ran on a subscription and were not billed per token.`
                      : "No API-equivalent cost was recorded for these calls."}
                </p>
              </>
            )}
          </div>
        </section>

        <section className="panel">
          <header className="panel__head">
            <span className="label">Context documents</span>
          </header>
          <div className="panel__body">
            {detail.context_docs.length === 0 ? (
              <p className="tab-note">
                None. The run worked from the question and the prompt alone.
              </p>
            ) : (
              <ul style={{ listStyle: "none", padding: 0 }}>
                {detail.context_docs.map((doc) => (
                  <li
                    key={doc.name}
                    className="row"
                    style={{ padding: "var(--space-2) 0" }}
                  >
                    <span className="truncate">{doc.name}</span>
                    <span className="spacer" />
                    <span className="faint numeral">
                      {compactNumber(doc.chars)} characters
                    </span>
                    {/* The character count on its own reads as "all of this was used".
                        Every call is capped, and a document that was cut or dropped
                        entirely used to say so nowhere the scientist could see. */}
                    <span className="chip" data-tone={deliveryTone(doc.delivered)}>
                      {describeDelivery(doc.delivered, doc.cap_chars)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function value(input: number | null | undefined): string {
  return input == null ? "Not recorded" : String(input);
}

/**
 * What actually reached the model, in the scientist's words.
 *
 * A document over the per-call cap is cut, and one with no room left is not
 * sent at all. Both used to be silent: the only trace was a log line inside the
 * run's working directory, while this panel printed the document's full size
 * back to the reader as if it had been used.
 */
function describeDelivery(
  delivered: "full" | "truncated" | "omitted" | "pending",
  cap: number | null | undefined,
): string {
  const limit = cap == null ? "the per-call cap" : `${compactNumber(cap)} characters`;
  switch (delivered) {
    case "full":
      return "sent in full";
    case "truncated":
      return `cut to fit ${limit} per call`;
    case "omitted":
      return `not sent — no room under ${limit} per call`;
    case "pending":
      return "not yet sent";
  }
}

function deliveryTone(delivered: "full" | "truncated" | "omitted" | "pending"): string {
  if (delivered === "omitted") return "danger";
  if (delivered === "truncated") return "caution";
  return "neutral";
}

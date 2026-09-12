import { Link } from "react-router-dom";

import type { RunSummary } from "../../api/types";
import { compactNumber, formatDuration, formatRoughDuration } from "../../lib/format";
import { NO_MODEL_CALL } from "../../lib/eventText";
import { METRIC_HINTS, describeRole } from "../../lib/status";
import type { TrustState } from "../../lib/trust";
import { Chip } from "../Status";

const LOST_STEPS_HINT =
  "Steps this run gave up on after retrying. That work is missing from the result — see Activity.";
const RETRIED_HINT =
  "Model calls that failed and were retried successfully. Nothing was lost, but they cost time and budget.";

/**
 * The line that says the machine is alive.
 *
 * "Reflection · claude-sonnet-5 · 47s" with a spinner while a call is open; the
 * calls used against the budget, which is the ceiling this run can actually
 * hit; the age of the last event, amber past ninety seconds and red past seven
 * minutes; tokens; a rough finish time once there is a round to extrapolate
 * from; and a badge counting the problems it worked around. When there is
 * nothing in flight it says so rather than showing a spinner that means
 * nothing.
 *
 * A demo run calls no models, so the model slot says so instead of naming one.
 */
export function TrustLine({
  run,
  trust,
  lostSteps,
  failedCalls,
  activityHref,
}: {
  run: RunSummary;
  trust: TrustState;
  /** Units the engine gave up on — work this run was supposed to do and did not. */
  lostSteps: number;
  /** Calls that ended in an error, retries included. */
  failedCalls: number;
  activityHref: string;
}) {
  const live = run.lifecycle === "running" || run.lifecycle === "finishing";
  // Pointed at real losses, not at `degraded_count`, which counts model/effort
  // substitutions and has been 0 for every run this engine has ever executed.
  const badge =
    lostSteps > 0
      ? { tone: "danger", text: `${lostSteps} lost`, title: LOST_STEPS_HINT }
      : failedCalls > 0
        ? { tone: "caution", text: `${failedCalls} retried`, title: RETRIED_HINT }
        : null;

  return (
    <div className="trust" role="status" aria-live="polite">
      {trust.inCall ? (
        <span className="trust__now">
          <span className="trust__spinner" aria-hidden="true" />
          {describeRole(trust.inCall.role)}
          <span className="trust__sep" aria-hidden="true">
            ·
          </span>
          <span className={trust.inCall.noModelCall ? undefined : "mono"}>
            {trust.inCall.noModelCall
              ? NO_MODEL_CALL
              : (trust.inCall.model ?? "model not recorded")}
          </span>
          <span className="trust__sep" aria-hidden="true">
            ·
          </span>
          <span className="numeral">{formatDuration(trust.inCall.elapsedMs)}</span>
        </span>
      ) : (
        <span className="trust__now">{live ? "Between steps" : "Nothing in flight"}</span>
      )}

      {trust.inFlightCount > 1 ? (
        <Chip tone="info">{trust.inFlightCount} calls at once</Chip>
      ) : null}

      <span className="trust__item numeral" title={METRIC_HINTS.calls}>
        {run.budget_calls > 0
          ? `${run.calls_used}/${run.budget_calls} calls`
          : `${run.calls_used} calls`}
      </span>

      {trust.lastActivityMs !== null ? (
        <Chip
          tone={trust.lastActivityTone}
          title={
            trust.stale
              ? "Nothing has happened for a while. The run may be stuck on a slow call."
              : "Time since the last thing this run did."
          }
        >
          {trust.lastActivityMs < 5000
            ? "just now"
            : `${formatDuration(trust.lastActivityMs)} quiet`}
        </Chip>
      ) : null}

      <span className="trust__item" title={METRIC_HINTS.tokens}>
        {compactNumber(run.tokens_total)} tokens
      </span>

      {trust.etaMs !== null ? (
        <span
          className="trust__item"
          title="A rough estimate from how long this run's calls have taken so far."
        >
          ≈ {formatRoughDuration(trust.etaMs)} left
        </span>
      ) : null}

      <span className="spacer" />

      {badge ? (
        <Link
          to={activityHref}
          className="chip"
          data-tone={badge.tone}
          title={badge.title}
        >
          {badge.text}
        </Link>
      ) : null}
    </div>
  );
}

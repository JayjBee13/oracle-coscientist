import type { RunSummary } from "../../api/types";
import { compactNumber, formatDuration, parseTs } from "../../lib/format";
import { METRIC_HINTS } from "../../lib/status";
import type { Tone } from "../../lib/status";

/**
 * Where the run is, what it has used, and what it is doing — the three things
 * somebody coming back to a tab needs before they read anything.
 *
 * **Calls are the governor here, not money.** These are subscription CLI calls:
 * nothing is billed per token, so a dollar figure is telemetry rather than a
 * limit and has no business in a ring or a headline. What actually runs out is
 * the call budget, the wall clock and the plan window — so the ring counts
 * calls, and elapsed time and tokens sit beside it.
 *
 * Imported runs get no ring and no live phase: there is no budget left to spend
 * and nothing in flight. Their calls are stated as history, which is the one
 * honest thing to say about a number recorded by an engine that no longer
 * exists.
 */
export function ProgressHeader({
  run,
  phase,
  now,
  roundsCompleted,
}: {
  run: RunSummary;
  phase: string | null;
  /** The workspace's clock, so elapsed ticks with everything else on the tab. */
  now: number;
  /**
   * Rounds that actually finished, counted from the round summaries. `run.round`
   * is the round *in progress*, so a terminal run whose last round died mid-way
   * used to claim a round of work it never did — c4566ed2 rendered "2 of 3"
   * against a recorded `rounds_completed: 1`.
   */
  roundsCompleted: number;
}) {
  const imported = run.source === "imported";
  const live = run.lifecycle === "running" || run.lifecycle === "finishing";
  const capped = !imported && run.budget_calls > 0;

  return (
    <section className="panel progress" aria-label="Run progress">
      {capped ? <CallRing used={run.calls_used} total={run.budget_calls} /> : null}

      <div className="progress__body">
        <RoundTrack
          round={run.round}
          target={run.rounds_target}
          active={live}
          completed={roundsCompleted}
        />

        <div className="stat-strip">
          <div className="stat">
            <span className="label">Model calls</span>
            <span className="stat__value" title={METRIC_HINTS.calls}>
              {capped ? `${run.calls_used}/${run.budget_calls}` : run.calls_used}
            </span>
            <span className="faint">
              {imported
                ? "recorded (historical)"
                : capped
                  ? `${Math.max(0, run.budget_calls - run.calls_used)} left in budget`
                  : "no call ceiling set"}
            </span>
          </div>

          <div className="stat">
            <span className="label">Elapsed</span>
            <span className="stat__value">{elapsedText(run, live, now)}</span>
            <span className="faint">{live ? "so far" : "start to last activity"}</span>
          </div>

          <div className="stat">
            <span className="label">Tokens</span>
            <span className="stat__value" title={METRIC_HINTS.tokens}>
              {run.tokens_total > 0 ? compactNumber(run.tokens_total) : "—"}
            </span>
            <span className="faint">
              {run.tokens_total > 0 ? "sent and received" : "not recorded for this run"}
            </span>
          </div>

          <div className="stat">
            <span className="label">Hypotheses</span>
            <span className="stat__value">{run.counts.active}</span>
            <span className="faint">
              {run.counts.rejected} rejected · {run.counts.archived} set aside
            </span>
          </div>

          <div className="stat">
            <span className="label">Matches</span>
            <span className="stat__value">{run.counts.matches}</span>
            <span className="faint">head to head</span>
          </div>

          {phase ? (
            <div className="stat">
              <span className="label">Now</span>
              <span className="stat__value">{phase}</span>
              <span className="faint">current step</span>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

/**
 * Wall clock, which is one of the two resources that genuinely runs out. A run
 * still moving is measured to now; a finished one to the last thing it did.
 */
function elapsedText(
  run: { created_at: string; updated_at: string },
  live: boolean,
  now: number,
): string {
  const started = parseTs(run.created_at);
  if (Number.isNaN(started)) return "—";
  const ended = live ? now : parseTs(run.updated_at);
  if (Number.isNaN(ended) || ended < started) return "—";
  return formatDuration(ended - started);
}

function CallRing({ used, total }: { used: number; total: number }) {
  const fraction = total > 0 ? Math.min(1, used / total) : 0;
  const percent = Math.round(fraction * 100);
  const tone: Tone = fraction >= 0.9 ? "danger" : fraction >= 0.7 ? "caution" : "accent";

  return (
    <div
      className="ring"
      data-tone={tone}
      role="img"
      aria-label={`${used} of ${total} model calls used`}
      title={METRIC_HINTS.calls}
    >
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <circle
          className="ring__track"
          cx="60"
          cy="60"
          r="52"
          fill="none"
          strokeWidth="8"
        />
        <circle
          className="ring__arc"
          cx="60"
          cy="60"
          r="52"
          fill="none"
          strokeWidth="8"
          pathLength={100}
          strokeDasharray="100"
          strokeDashoffset={100 - percent}
        />
      </svg>
      <div style={{ textAlign: "center" }}>
        <div className="ring__value">
          {used}
          <span className="faint">/{total}</span>
        </div>
        <div className="ring__caption">calls</div>
      </div>
    </div>
  );
}

/** Twelve or fewer rounds are countable; past that a sentence is clearer. */
function RoundTrack({
  round,
  target,
  active,
  completed,
}: {
  round: number;
  target: number;
  active: boolean;
  completed: number;
}) {
  if (target <= 0) {
    return (
      <p className="muted" title={METRIC_HINTS.round}>
        Round <strong className="numeral">{round}</strong>
      </p>
    );
  }

  if (target > 12) {
    return (
      <p className="muted" title={METRIC_HINTS.round}>
        Round <strong className="numeral">{round}</strong> of{" "}
        <span className="numeral">{target}</span>
      </p>
    );
  }

  return (
    <div
      className="round-track"
      role="img"
      aria-label={`Round ${round} of ${target}`}
      title={METRIC_HINTS.round}
    >
      {Array.from({ length: target }, (_, index) => {
        const number = index + 1;
        // "done" means finished, which is what `completed` counts. A round that
        // was started and never finished is `abandoned`, not silently promoted:
        // the previous rule coloured every step `<= run.round` as done on a
        // terminal run, so a run that died mid-round claimed it.
        const state =
          number <= completed
            ? "done"
            : active && number === round
              ? "current"
              : !active && number <= round
                ? "abandoned"
                : "todo";
        return (
          <span
            className="round-step"
            key={number}
            data-state={state}
            title={state === "abandoned" ? "Started but never finished" : undefined}
          >
            {number}
          </span>
        );
      })}
    </div>
  );
}

import { Link } from "react-router-dom";

import type { RunConfig, RunDetail, RunEvent, RunSummary } from "../../api/types";
import { ControlBar } from "../../components/run/ControlBar";
import { EventStream } from "../../components/run/EventStream";
import { Leaderboard } from "../../components/run/Leaderboard";
import { GraftPanel, NoteBox } from "../../components/run/Panels";
import { ProgressHeader } from "../../components/run/Progress";
import { TrustLine } from "../../components/run/TrustLine";
import { SkeletonBlock } from "../../components/States";
import { currentPhase, withoutFalseModelClaims } from "../../lib/eventText";
import { METRIC_HINTS, isActiveLifecycle } from "../../lib/status";
import { deriveTrust } from "../../lib/trust";

/**
 * What the run is doing right now.
 *
 * Everything that answers "is this alive and is it working" is above the fold:
 * progress against the plan and the call budget, the call in flight, the
 * standings. Steering and diagnostics sit beside it. Imported runs lose the
 * ring, the controls and the note box — there is no process to steer and no
 * budget left to spend, and showing dead controls was the previous UI's most
 * confusing habit.
 *
 * The event feed is passed through `withoutFalseModelClaims` first. This is the
 * one place that knows the run's harness, and a demo run's call events carry
 * the model a real run *would* have used; stripping it here means the log, the
 * trust line and the derived telemetry all tell the same story.
 */
export function LiveTab({
  run,
  detail,
  events,
  now,
  forceStopRevealed,
  onStopRequested,
}: {
  run: RunSummary;
  detail: RunDetail | null;
  events: readonly RunEvent[];
  now: number;
  forceStopRevealed: boolean;
  onStopRequested: () => void;
}) {
  const imported = run.source === "imported";
  const live = isActiveLifecycle(run.lifecycle) && !imported;
  const roundsCompleted =
    detail?.rounds.filter((round) => round.status === "completed").length ?? 0;

  const feed = withoutFalseModelClaims(events, run.harness);

  const trust = deriveTrust({
    events: feed,
    lifecycle: run.lifecycle,
    now,
    callsUsed: run.calls_used,
    budgetCalls: run.budget_calls,
    roundsTarget: run.rounds_target,
    roundsCompleted,
  });

  const phase = live ? currentPhase(feed, trust.inCall?.role ?? null) : null;

  return (
    <div className="stack">
      {detail?.config.workflow === "adaptive" ? (
        <div className="card">
          <strong>Adaptive research</strong>
          <p>
            Follow the competing approaches, evidence and complete-answer challenge in the{" "}
            <Link to="?tab=research">Research workspace</Link>. The hypothesis list
            preserves alternatives; its Elo values do not rank this workflow's final
            answer.
          </p>
        </div>
      ) : null}
      <ProgressHeader
        run={run}
        phase={phase}
        now={now}
        roundsCompleted={roundsCompleted}
      />

      {imported ? (
        <p className="muted">
          This run was imported from an earlier engine. It finished before this interface
          existed, so there is no live feed and nothing left to control — but every
          hypothesis, review and match it recorded is here.
        </p>
      ) : (
        <>
          <TrustLine
            run={run}
            trust={trust}
            lostSteps={detail?.lost_steps ?? run.lost_steps}
            failedCalls={detail?.failed_calls ?? run.failed_calls}
            activityHref={`/runs/${run.id}?tab=activity`}
          />
          <ControlBar
            run={run}
            config={(detail?.config as Partial<RunConfig> | undefined) ?? null}
            roundsCompleted={roundsCompleted}
            forceStopRevealed={forceStopRevealed}
            onStopRequested={onStopRequested}
          />
        </>
      )}

      <div className="ws-grid">
        <section className="panel">
          <header className="panel__head">
            <span className="label">
              {detail?.research ? "Research portfolio" : "Standings"}
            </span>
            <span className="spacer" />
            <span
              className="faint"
              style={{ fontSize: "var(--text-xs)" }}
              title={METRIC_HINTS.elo}
            >
              {detail?.research ? "Approach coverage and evidence" : "Ranked by Elo"}
            </span>
          </header>
          <div className="panel__body">
            {detail?.research ? (
              <ul>
                {detail.research.portfolio.map((candidate) => (
                  <li key={candidate.hid}>
                    <Link to={`/runs/${run.id}/hypotheses/${candidate.hid}`}>
                      {candidate.title}
                    </Link>{" "}
                    — {candidate.readiness.replaceAll("_", " ")}
                  </li>
                ))}
              </ul>
            ) : detail ? (
              <Leaderboard runId={run.id} rows={detail.leaderboard} limit={8} ranked />
            ) : (
              <SkeletonBlock height={160} />
            )}
          </div>
        </section>

        <div className="stack">
          {live ? (
            <section className="panel">
              <header className="panel__head">
                <span className="label">Guidance</span>
              </header>
              <div className="panel__body">
                <NoteBox run={run} />
              </div>
            </section>
          ) : null}

          {detail ? <GraftPanel run={run} events={detail.graft_events} /> : null}

          <section className="panel">
            <header className="panel__head">
              <span className="label">Recent activity</span>
              <span className="spacer" />
              <Link
                className="faint"
                style={{ fontSize: "var(--text-xs)" }}
                to={`/runs/${run.id}?tab=activity`}
              >
                Full log
              </Link>
            </header>
            <EventStream
              events={feed.slice(-40)}
              live={live}
              emptyMessage={
                imported
                  ? "No event log — this run was reconstructed from its files, not watched."
                  : "Nothing has happened yet."
              }
            />
          </section>
        </div>
      </div>
    </div>
  );
}

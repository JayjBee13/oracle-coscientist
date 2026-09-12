import type { RunEvent, RunSummary } from "../../api/types";
import { EventStream } from "../../components/run/EventStream";
import { Forensics } from "../../components/run/Panels";
import { withoutFalseModelClaims } from "../../lib/eventText";
import { describeRole, isActiveLifecycle } from "../../lib/status";
import { deriveForensics } from "../../lib/trust";

/**
 * Everything the run did, and — when it failed — why.
 *
 * The plan's requirement for this tab is blunt: a failed run must be
 * diagnosable from here alone. So the forensics panel comes first, then the
 * complete stream with its filters. Lines the client could not parse are shown
 * raw with a badge rather than dropped.
 *
 * Two things reach the screen only because this tab looks for them. A demo run
 * makes no model calls, so its call events are stripped of the model a real run
 * would have used before anything renders them. And a call the CLI served with
 * a *different* model than the one requested fails nothing and warns nobody —
 * it is only visible if somebody reads the per-call telemetry, which is what
 * the substitution panel below does.
 */
export function ActivityTab({
  run,
  events,
}: {
  run: RunSummary;
  events: readonly RunEvent[];
}) {
  const live = isActiveLifecycle(run.lifecycle) && run.source !== "imported";
  const feed = withoutFalseModelClaims(events, run.harness);

  return (
    <div className="stack">
      <Substitutions events={feed} />

      <Forensics run={run} events={feed} />

      <section className="panel">
        <header className="panel__head">
          <span className="label">Event log</span>
          <span className="spacer" />
          <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
            {feed.length} recorded
          </span>
        </header>
        <EventStream
          events={feed}
          live={live}
          filterable
          tall
          emptyMessage={
            run.source === "imported"
              ? "This run was reconstructed from the files it left behind, so it has no event log. Its hypotheses, reviews and matches are all still here."
              : "Nothing has happened yet."
          }
        />
      </section>
    </div>
  );
}

/**
 * Which calls did not run on the model they were asked to run on.
 *
 * Absent unless it happened. A substitution changes what the research is — a
 * weaker model answering a ranking call quietly rewrites the leaderboard — and
 * the Settings tab still prints the table that was *requested*, so without this
 * there is nowhere in the app that the difference shows.
 */
function Substitutions({ events }: { events: readonly RunEvent[] }) {
  const { substitutedCalls } = deriveForensics(events);
  if (substitutedCalls.length === 0) return null;

  const belowFloor = substitutedCalls.some((call) => call.belowFloor);

  return (
    <section className="panel">
      <header className="panel__head">
        <span className="label">Calls that ran on a different model</span>
        <span className="spacer" />
        <span
          className="chip"
          data-tone={belowFloor ? "caution" : "info"}
          title="The model that answered was not the model this run's table asked for."
        >
          {substitutedCalls.length} {substitutedCalls.length === 1 ? "call" : "calls"}
        </span>
      </header>
      <div className="panel__body stack">
        <p className="muted" style={{ fontSize: "var(--text-sm)" }}>
          {belowFloor
            ? "At least one call was answered by a model weaker than this app's floor. Treat those results as provisional."
            : "These calls were answered by a model other than the one requested. Nothing failed, which is exactly why it is worth reading."}
        </p>
        <div className="scroll-x">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Step</th>
                <th scope="col">Round</th>
                <th scope="col">Asked for</th>
                <th scope="col">Actually ran</th>
              </tr>
            </thead>
            <tbody>
              {substitutedCalls.map((call) => (
                <tr key={call.seq}>
                  <td>{call.role ? describeRole(call.role) : "Unknown step"}</td>
                  <td className="numeral">{call.round ?? "—"}</td>
                  <td className="mono">{call.requested ?? "not recorded"}</td>
                  <td>
                    <span className="row">
                      <span className="mono">{call.ran ?? "not recorded"}</span>
                      {call.belowFloor ? (
                        <span className="chip" data-tone="caution">
                          below the floor
                        </span>
                      ) : null}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

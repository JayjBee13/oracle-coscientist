import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { IdeaTrace } from "../../lib/ideaTrace";
import { clusterLabel, describeOperator } from "../../lib/status";

/**
 * One idea's life, laid along a lane.
 *
 * The circuit above says what the machine does; this says what it did. Same six
 * moments for every idea — drafted, reviewed, filed under a theme, fought,
 * bred, reported — so the lane is a template and the run supplies the values.
 *
 * Nothing here is written for effect. Every figure on the lane is a column of
 * `GET /api/runs/{id}/graph`, folded by `lib/ideaTrace`, and the lane's job is
 * to render them in the vocabulary the rest of the app uses: `clusterLabel` for
 * the theme, `describeOperator` for how an offspring was made. When the machine
 * has never finished a run there is nothing to fold, and the lane says *example*
 * on itself rather than passing an illustration off as a record.
 */
export function IdeaTraceLane({ trace }: { trace: IdeaTrace }) {
  const steps = traceSteps(trace);

  return (
    <div className="trace">
      <ol className="trace__lane">
        {steps.map((step) => (
          <li key={step.key} className={`trace__step trace__step--${step.tone}`}>
            <span className="trace__dot" aria-hidden="true">
              {step.dot}
            </span>
            <span className="trace__name">{step.name}</span>
            <span className="trace__what">{step.what}</span>
          </li>
        ))}
      </ol>

      <p className="trace__dynasty">
        {trace.children.length > 0 ? (
          <>
            <strong>What came out of it:</strong>{" "}
            {trace.children.slice(0, 3).map((child) => (
              <span key={child.hid} className="trace__kid-line">
                <span className="trace__kid">
                  {child.hid}
                  {child.operator ? ` · ${describeOperator(child.operator).label}` : ""}
                </span>{" "}
                {child.label}
              </span>
            ))}
            {trace.children.length > 3
              ? ` …and ${trace.children.length - 3} more.`
              : null}{" "}
            One draft became a {trace.children.length + 1}-idea family — that lineage is
            the Hypotheses tab.
          </>
        ) : (
          <>
            <strong>No dynasty:</strong> nothing was bred from this idea, so its branch of
            the genealogy ends where it started.
          </>
        )}
      </p>

      {trace.runId ? (
        <p className="trace__link">
          <Link
            className="btn btn--sm btn--ghost"
            to={`/runs/${trace.runId}?tab=hypotheses`}
          >
            Open this run’s genealogy
          </Link>
        </p>
      ) : null}
    </div>
  );
}

type TraceStep = {
  key: string;
  dot: string;
  tone: "step" | "win" | "breed";
  name: string;
  what: ReactNode;
};

function traceSteps(trace: IdeaTrace): TraceStep[] {
  const theme = trace.cluster ? clusterLabel(trace.cluster) : null;
  const operator = trace.operator ? describeOperator(trace.operator).label : null;

  return [
    {
      key: "born",
      dot: `R${trace.bornRound}`,
      tone: "step",
      name: trace.origin === "generation" ? "Born in Generation" : "Bred by Evolution",
      what:
        trace.origin === "generation" ? (
          <>
            “{trace.title}” — a round-{trace.bornRound} draft, <code>{trace.hid}</code>
          </>
        ) : (
          <>
            “{trace.title}” — <code>{trace.hid}</code>, bred from{" "}
            <code>{trace.parents.join(" × ")}</code>
            {operator ? ` · ${operator}` : ""}
          </>
        ),
    },
    {
      key: "reflection",
      dot: "✓",
      tone: "step",
      name: "Survived Reflection",
      what: <>accepted; siblings that failed review died here</>,
    },
    {
      key: "clustered",
      dot: "◉",
      tone: "step",
      name: "Clustered",
      what: theme ? (
        <>
          filed under <code>{theme}</code>;{" "}
          {trace.mergedIn > 0
            ? `${trace.mergedIn} near-duplicate${trace.mergedIn === 1 ? "" : "s"} merged into it`
            : "no duplicate found"}
        </>
      ) : (
        <>this run recorded no themes, so nothing was filed or merged</>
      ),
    },
    {
      key: "matches",
      dot: trace.matches > 0 ? `${trace.wins}–${trace.losses}` : "—",
      tone: trace.matches > 0 && trace.wins > trace.losses ? "win" : "step",
      name:
        trace.matches === 0
          ? "Never matched"
          : trace.wins > trace.losses
            ? "Won its matches"
            : "Fought its matches",
      what:
        trace.matches === 0 ? (
          <>
            no head-to-head debate reached it; Elo stayed at <code>{eloOf(trace)}</code>
          </>
        ) : (
          <>
            {trace.matches} head-to-head debate{trace.matches === 1 ? "" : "s"},{" "}
            {trace.wins} won → Elo <code>{eloOf(trace)}</code>
          </>
        ),
    },
    {
      key: "bred",
      dot: trace.children.length > 0 ? "⑂" : "·",
      tone: trace.children.length > 0 ? "breed" : "step",
      name: trace.children.length > 0 ? "Chosen to breed" : "Never bred",
      what:
        trace.children.length > 0 ? (
          <>
            Evolution used it as a parent {trace.children.length} time
            {trace.children.length === 1 ? "" : "s"}
          </>
        ) : (
          <>it never finished a round in the top three</>
        ),
    },
    {
      key: "reported",
      dot: "★",
      tone: trace.isLeader || trace.rank === 1 ? "win" : "step",
      name: "Reported",
      what:
        trace.isLeader || trace.rank === 1 ? (
          <>led the final standings; first idea in the report</>
        ) : (
          <>
            ranked {trace.rank} of {trace.survivors} survivors in the final report
          </>
        ),
    },
  ];
}

function eloOf(trace: IdeaTrace): number {
  return Math.round(trace.elo);
}

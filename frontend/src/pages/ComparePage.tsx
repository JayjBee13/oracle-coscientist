import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import * as api from "../api/client";
import { errorMessage } from "../api/client";
import type { CompareAnalytics, HypothesisRow, RunsQuery } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, SkeletonBlock } from "../components/States";
import {
  headlineVerdict,
  rankWithinRun,
  readDeltas,
  readGraft,
  readMovement,
} from "../lib/compare";
import type { RankedList } from "../lib/compare";
import { useRuns } from "../store/runs";
import { getRunsList } from "../store/runs";
import {
  Banner,
  DeltaTable,
  GraftSummary,
  RankedPanel,
  RunPicker,
  VerdictCard,
} from "./compare/parts";
import "../styles/compare.css";

/** One page of runs for both pickers; searching filters it in the browser. */
const PICKER_QUERY: RunsQuery = { page_size: 100, show_archived: true, sort: "recent" };

type Loaded = {
  analytics: CompareAnalytics;
  baselineRanking: RankedList;
  challengerRanking: RankedList;
};

/**
 * Two runs, side by side.
 *
 * The page exists because the previous one was statistically invalid: it merged
 * both runs' hypotheses into a single Elo leaderboard, which ranks numbers that
 * were never in the same tournament. Here each run keeps its own list,
 * normalised against its own best and worst, and the only figures that cross
 * the gap are counts and the Elo *spread* — how sharply a run separated its own
 * ideas, which is a property of that run alone.
 */
export function ComparePage() {
  const { a, b } = useParams<{ a?: string; b?: string }>();
  const navigate = useNavigate();

  useRuns(PICKER_QUERY);
  const runs = getRunsList();

  const [baselineId, setBaselineId] = useState<string | null>(a ?? null);
  const [challengerId, setChallengerId] = useState<string | null>(b ?? null);

  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const sameRun = Boolean(a && b && a === b);
  const shouldLoad = Boolean(a && b) && !sameRun;

  useEffect(() => {
    if (!shouldLoad || !a || !b) return;
    const abort = new AbortController();
    Promise.all([
      api.getCompare(a, b, abort.signal),
      api.listHypotheses(a, {}, abort.signal),
      api.listHypotheses(b, {}, abort.signal),
    ])
      .then(
        ([analytics, baselineRows, challengerRows]: [
          CompareAnalytics,
          HypothesisRow[],
          HypothesisRow[],
        ]) => {
          if (abort.signal.aborted) return;
          setLoaded({
            analytics,
            // Normalised separately, on purpose: there is no shared scale.
            baselineRanking: rankWithinRun(baselineRows),
            challengerRanking: rankWithinRun(challengerRows),
          });
          setError(null);
        },
      )
      .catch((cause: unknown) => {
        if (abort.signal.aborted) return;
        setLoaded(null);
        setError(errorMessage(cause));
      });
    return () => abort.abort();
  }, [a, b, shouldLoad, attempt]);

  const pick = (side: "baseline" | "challenger", runId: string): void => {
    const nextBaseline = side === "baseline" ? runId : baselineId;
    const nextChallenger = side === "challenger" ? runId : challengerId;
    if (side === "baseline") setBaselineId(runId);
    else setChallengerId(runId);
    if (nextBaseline && nextChallenger && nextBaseline !== nextChallenger) {
      navigate(`/compare/${nextBaseline}/${nextChallenger}`);
    }
  };

  const swap = (): void => {
    setBaselineId(challengerId);
    setChallengerId(baselineId);
    if (a && b) navigate(`/compare/${b}/${a}`);
  };

  return (
    <>
      <PageHeader eyebrow="Compare" title="Compare two runs">
        What changed when the prompt or the settings changed. Each run keeps its own
        ranking — strength scores are only comparable inside the tournament that produced
        them.
      </PageHeader>

      <section className="card" style={{ marginBottom: "var(--space-5)" }}>
        <div className="picker-grid">
          <RunPicker
            label="Baseline"
            hint="The run you are comparing against."
            runs={runs}
            selectedId={baselineId}
            conflictId={challengerId}
            onSelect={(runId) => pick("baseline", runId)}
          />
          <button
            type="button"
            className="btn btn--sm picker-swap"
            onClick={swap}
            disabled={!baselineId || !challengerId}
            title="Swap which run is the baseline"
          >
            Swap
          </button>
          <RunPicker
            label="Challenger"
            hint="The run you want to know about."
            runs={runs}
            selectedId={challengerId}
            conflictId={baselineId}
            onSelect={(runId) => pick("challenger", runId)}
          />
        </div>
      </section>

      {sameRun ? (
        <Banner tone="caution" title="That is the same run on both sides">
          Every difference would be zero, which would say nothing about anything. Pick a
          second run to compare it against.
        </Banner>
      ) : null}

      {!a || !b ? (
        !sameRun ? (
          <EmptyState title="Pick two runs">
            Choose a baseline and a challenger above. The comparison is the URL, so you
            can bookmark it or send it to yourself.
          </EmptyState>
        ) : null
      ) : error ? (
        <ErrorState
          title="That comparison did not load"
          message={error}
          onRetry={() => {
            setError(null);
            setAttempt((count) => count + 1);
          }}
        />
      ) : !loaded ? (
        <div className="stack" aria-busy="true" aria-label="Loading the comparison">
          <SkeletonBlock height={120} />
          <SkeletonBlock height={220} />
        </div>
      ) : (
        <Comparison loaded={loaded} />
      )}
    </>
  );
}

function Comparison({ loaded }: { loaded: Loaded }) {
  const { analytics, baselineRanking, challengerRanking } = loaded;
  const deltas = readDeltas(analytics.deltas);
  const verdict = headlineVerdict(deltas, { sharedPrompt: analytics.shared_prompt });
  const movement = readMovement(analytics);
  const baselineGraft = readGraft(analytics.graft_summary?.baseline);
  const challengerGraft = readGraft(analytics.graft_summary?.challenger);

  return (
    <div className="stack" style={{ gap: "var(--space-5)" }}>
      {analytics.shared_prompt ? (
        <Banner tone="go" title="Same prompt on both sides">
          Both runs started from identical wording, so what differs between them is the
          settings and the luck of the draw.
        </Banner>
      ) : (
        <Banner tone="caution" title="These runs did not start from the same prompt">
          They were asked different things, so a difference below may be the question
          rather than anything you changed. Compare them as two separate results, not as a
          controlled experiment.
        </Banner>
      )}

      <VerdictCard verdict={verdict} />

      <section className="panel">
        <div className="panel__head">
          <h2 className="label">What moved</h2>
        </div>
        <div className="panel__body">
          <DeltaTable
            deltas={deltas}
            baselineTitle={analytics.baseline.title}
            challengerTitle={analytics.challenger.title}
          />
        </div>
      </section>

      {/* Two charts, never one. Each is normalised inside its own run. */}
      <div className="cmp-grid">
        <RankedPanel side="Baseline" run={analytics.baseline} list={baselineRanking} />
        <RankedPanel
          side="Challenger"
          run={analytics.challenger}
          list={challengerRanking}
        />
      </div>

      {movement.length > 0 ? (
        <section className="panel">
          <div className="panel__head">
            <h2 className="label">Ideas that appear in both runs</h2>
          </div>
          <div className="panel__body">
            <p className="muted" style={{ marginBottom: "var(--space-3)" }}>
              Matched by title — two independent runs share no hypothesis ids. The rank is
              within each run's own list.
            </p>
            <div className="scroll-x">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Hypothesis</th>
                    <th scope="col">In the baseline</th>
                    <th scope="col">In the challenger</th>
                  </tr>
                </thead>
                <tbody>
                  {movement.map((row) => (
                    <tr key={`${row.hid}-${row.title}`}>
                      <td>{row.title}</td>
                      <td className="tabular">
                        {row.previousRank === null ? "—" : `#${row.previousRank}`}
                      </td>
                      <td className="tabular">
                        {row.rank === null ? "—" : `#${row.rank}`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      ) : null}

      {baselineGraft || challengerGraft ? (
        <section className="panel">
          <div className="panel__head">
            <h2 className="label">Diversity injection</h2>
          </div>
          <div className="panel__body stack">
            <p className="muted">
              When ideas converge too tightly, a fresh framing from another field is
              grafted in. Only shown for runs whose engine had it.
            </p>
            {baselineGraft ? (
              <GraftSummary side="Baseline" graft={baselineGraft} />
            ) : null}
            {challengerGraft ? (
              <GraftSummary side="Challenger" graft={challengerGraft} />
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

import { useState } from "react";
import { ArrowLeft } from "lucide-react";
import { Link, useParams } from "react-router-dom";

import { Markdown } from "../components/Markdown";
import {
  EloCurve,
  Lineage,
  MatchTable,
  ReviewList,
} from "../components/run/HypothesisParts";
import { Chip, LabelChip } from "../components/Status";
import { EmptyState, ErrorState, LoadingPage } from "../components/States";
import { formatElo, plural, stripLeadingTitle } from "../lib/format";
import { useHypothesisDetail } from "../lib/hooks";
import {
  METRIC_HINTS,
  clusterLabel,
  describeHypothesisStatus,
  describeNovelty,
  describeOperator,
} from "../lib/status";
import { archiveRunHypothesis, useRun } from "../store/runs";
import "../styles/screens.css";

/**
 * One hypothesis, on its own page.
 *
 * Addressed by the human-readable `hid` (`/runs/:id/hypotheses/h007`) rather
 * than the database uuid, so the URL means something when it is pasted into a
 * lab notebook. The row in the run's leaderboard is what turns one into the
 * other — which is also how this page knows the hypothesis belongs to the run
 * in the address.
 */
export function HypothesisPage() {
  const { id = "", hid = "" } = useParams<{ id: string; hid: string }>();
  const [archiving, setArchiving] = useState(false);

  const entry = useRun(id);
  const row =
    entry.detail?.leaderboard.find((candidate) => candidate.hid === hid) ?? null;
  const { detail, status, error } = useHypothesisDetail(row?.id ?? null);

  if (!entry.detail && entry.status === "error") {
    return (
      <ErrorState
        title="Could not open this run"
        message={entry.error ?? "The backend did not answer."}
      />
    );
  }

  if (!entry.detail) return <LoadingPage label="Loading hypothesis" />;

  if (!row) {
    return (
      <EmptyState
        title="No such hypothesis in this run"
        action={
          <Link className="btn" to={`/runs/${id}?tab=hypotheses`}>
            See every hypothesis
          </Link>
        }
      >
        This run has nothing numbered {hid}.
      </EmptyState>
    );
  }

  const run = entry.detail.run;
  const hypothesisStatus = describeHypothesisStatus(row.status);

  async function archive(): Promise<void> {
    setArchiving(true);
    await archiveRunHypothesis(id, hid);
    setArchiving(false);
  }

  return (
    <>
      <header className="ws-head">
        <Link className="ws-head__id" to={`/runs/${id}?tab=hypotheses`}>
          <ArrowLeft size={12} aria-hidden="true" /> {run.title}
        </Link>

        <div className="ws-head__top">
          <div style={{ minWidth: 0, flex: "1 1 420px" }}>
            <h1>{row.title}</h1>
            <div className="ws-head__facts" style={{ marginTop: "var(--space-3)" }}>
              <span className="mono faint">{row.hid}</span>
              <LabelChip value={hypothesisStatus} />
              {row.novelty_level ? (
                <LabelChip value={describeNovelty(row.novelty_level)} />
              ) : null}
              {row.operator ? <LabelChip value={describeOperator(row.operator)} /> : null}
              {row.cluster ? (
                <Chip title={METRIC_HINTS.cluster}>{clusterLabel(row.cluster)}</Chip>
              ) : null}
              <span className="faint">Written in round {row.created_round}</span>
            </div>
          </div>

          <div className="ws-head__actions">
            <div className="stat" style={{ alignItems: "flex-end" }}>
              <span className="label">Elo</span>
              <span className="stat__value" title={METRIC_HINTS.elo}>
                {formatElo(row.elo)}
              </span>
              <span className="faint">
                {row.matches > 0
                  ? `${row.wins} of ${plural(row.matches, "match", "matches")} won`
                  : "no matches yet"}
              </span>
            </div>
            {row.status === "active" ? (
              <button
                type="button"
                className="btn"
                disabled={archiving}
                onClick={() => void archive()}
              >
                {archiving ? "Setting aside…" : "Set aside"}
              </button>
            ) : null}
          </div>
        </div>
      </header>

      {status === "loading" ? (
        <LoadingPage label="Loading hypothesis" />
      ) : status === "error" || !detail ? (
        <ErrorState
          title="Could not load this hypothesis"
          message={error ?? "The backend did not answer."}
        />
      ) : (
        <div className="ws-grid">
          <div className="stack">
            <section className="panel">
              <header className="panel__head">
                <span className="label">The hypothesis</span>
              </header>
              <div className="panel__body">
                {/* The title is the page heading; most bodies repeat it as their
                    own `# <title>`. See `stripLeadingTitle`. */}
                <Markdown source={stripLeadingTitle(detail.body_md, row.title)} />
              </div>
            </section>

            <section className="panel">
              <header className="panel__head">
                <span className="label">What the reviewers said</span>
              </header>
              <div className="panel__body">
                <ReviewList reviews={detail.reviews} />
              </div>
            </section>

            <section className="panel">
              <header className="panel__head">
                <span className="label">Head to head</span>
              </header>
              <div className="panel__body">
                <MatchTable hid={row.hid} matches={detail.match_history} />
              </div>
            </section>
          </div>

          <div className="stack">
            <section className="panel">
              <header className="panel__head">
                <span className="label">Strength over the tournament</span>
              </header>
              <div className="panel__body">
                <EloCurve hid={row.hid} matches={detail.match_history} />
                {detail.match_history.length === 0 ? (
                  <p className="tab-note">Nothing to plot until it has fought a match.</p>
                ) : detail.match_history.every((match) => match.elo_a_before == null) ? (
                  <p className="tab-note">
                    This run recorded who won each match but not the strength either side
                    carried into it, so there is no curve to draw.
                  </p>
                ) : null}
              </div>
            </section>

            <section className="panel">
              <header className="panel__head">
                <span className="label">Where it came from</span>
              </header>
              <div className="panel__body">
                <Lineage
                  runId={id}
                  parents={detail.lineage.parents}
                  evolvedInto={detail.lineage.children}
                />
              </div>
            </section>
          </div>
        </div>
      )}
    </>
  );
}

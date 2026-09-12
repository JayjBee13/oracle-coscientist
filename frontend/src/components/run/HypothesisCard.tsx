import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Link } from "react-router-dom";

import type { HypothesisRow } from "../../api/types";
import { formatElo, stripLeadingTitle } from "../../lib/format";
import { useHypothesisDetail } from "../../lib/hooks";
import {
  METRIC_HINTS,
  clusterLabel,
  describeHypothesisStatus,
  describeNovelty,
  describeOperator,
} from "../../lib/status";
import { archiveRunHypothesis } from "../../store/runs";
import { Markdown } from "../Markdown";
import { Chip, LabelChip } from "../Status";
import { ErrorState, SkeletonText } from "../States";
import { EloCurve, Lineage, MatchTable, ReviewList } from "./HypothesisParts";

/**
 * A hypothesis as a card you can open: the claim, its critiques, its record and
 * its ancestry, in the reading register rather than the instrument one.
 *
 * The body is fetched only when the card is opened — a leaderboard of sixty
 * would otherwise ship sixty full texts to render six of them.
 */
export function HypothesisCard({
  runId,
  row,
  rank,
  open,
  onToggle,
}: {
  runId: string;
  row: HypothesisRow;
  rank: number;
  open: boolean;
  onToggle: () => void;
}) {
  const [archiving, setArchiving] = useState(false);
  const { detail, status, error } = useHypothesisDetail(open ? row.id : null);
  const hypothesisStatus = describeHypothesisStatus(row.status);

  async function archive(): Promise<void> {
    setArchiving(true);
    await archiveRunHypothesis(runId, row.hid);
    setArchiving(false);
  }

  return (
    <article className="hyp" data-open={open ? "true" : "false"}>
      <button type="button" className="hyp__head" aria-expanded={open} onClick={onToggle}>
        <span className="hyp__rank numeral" aria-hidden="true">
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </span>

        <span style={{ minWidth: 0 }}>
          <span className="hyp__title">{row.title}</span>
          <span className="hyp__meta">
            <span className="mono faint">{row.hid}</span>
            <LabelChip value={hypothesisStatus} />
            {row.novelty_level ? (
              <LabelChip value={describeNovelty(row.novelty_level)} />
            ) : null}
            {row.operator ? <LabelChip value={describeOperator(row.operator)} /> : null}
            {row.cluster ? (
              <Chip title={METRIC_HINTS.cluster}>{clusterLabel(row.cluster)}</Chip>
            ) : null}
            <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
              {row.matches > 0
                ? `${row.wins} of ${row.matches} matches won`
                : "no matches yet"}
              {" · round "}
              {row.created_round}
            </span>
          </span>
        </span>

        <span className="hyp__elo">
          <span className="hyp__elo-value" title={METRIC_HINTS.elo}>
            #{rank}
          </span>
          <span className="faint numeral">{formatElo(row.elo)}</span>
        </span>
      </button>

      {open ? (
        <div className="hyp__body">
          {status === "loading" ? (
            <div style={{ paddingTop: "var(--space-5)" }}>
              <SkeletonText lines={5} />
            </div>
          ) : status === "error" ? (
            <ErrorState
              title="Could not open this hypothesis"
              message={error ?? "The backend did not answer."}
            />
          ) : detail ? (
            <>
              <div className="hyp__section">
                {/* The title is at the top of this card, and most bodies open
                    with `# <title>` again. See `stripLeadingTitle`. */}
                <Markdown source={stripLeadingTitle(detail.body_md, row.title)} />
              </div>

              <div className="hyp__section">
                <span className="label">What the reviewers said</span>
                <ReviewList reviews={detail.reviews} />
              </div>

              {detail.match_history.length > 0 ? (
                <div className="hyp__section">
                  <span className="label">Head to head</span>
                  <EloCurve hid={row.hid} matches={detail.match_history} />
                  <MatchTable hid={row.hid} matches={detail.match_history} />
                </div>
              ) : null}

              <div className="hyp__section">
                <span className="label">Where it came from</span>
                <Lineage
                  runId={runId}
                  parents={detail.lineage.parents}
                  evolvedInto={detail.lineage.children}
                />
              </div>

              <div className="row-wrap" style={{ marginTop: "var(--space-6)" }}>
                <Link className="btn" to={`/runs/${runId}/hypotheses/${row.hid}`}>
                  Open on its own page
                </Link>
                {row.status === "active" ? (
                  <button
                    type="button"
                    className="btn btn--ghost"
                    disabled={archiving}
                    onClick={() => void archive()}
                  >
                    {archiving ? "Setting aside…" : "Set aside"}
                  </button>
                ) : null}
              </div>
            </>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

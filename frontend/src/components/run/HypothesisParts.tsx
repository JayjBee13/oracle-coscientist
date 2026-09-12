import { Link } from "react-router-dom";

import type { HypothesisRow, MatchRow, ReviewRow } from "../../api/types";
import { formatElo, relativeTime } from "../../lib/format";
import { METRIC_HINTS, describeNovelty, describeVerdict } from "../../lib/status";
import { Chip, LabelChip } from "../Status";

/* --- Reviews --------------------------------------------------------------- */

const REVIEW_FIELDS = [
  { key: "correctness", label: "Is it right?" },
  { key: "testability", label: "Can it be tested?" },
  { key: "key_risk", label: "Biggest risk" },
  { key: "note", label: "Reviewer's note" },
] as const;

/**
 * All five review fields. The engine has always produced them; the previous UI
 * parsed them and threw four away, showing only pass or reject — which is the
 * one part of a critique that tells you nothing.
 */
export function ReviewList({ reviews }: { reviews: ReviewRow[] }) {
  if (reviews.length === 0) {
    return <p className="tab-note">No review was recorded for this hypothesis.</p>;
  }

  return (
    <div>
      {reviews.map((review, index) => {
        const structured = REVIEW_FIELDS.filter((field) => review[field.key]?.trim());
        return (
          <article className="review" key={index}>
            <header className="review__head">
              <LabelChip value={describeVerdict(review.verdict)} />
              {review.novelty_level ? (
                <LabelChip
                  value={describeNovelty(review.novelty_level)}
                  title="How new the reviewer judged this idea to be."
                />
              ) : null}
              <span className="spacer" />
              {review.model ? <span className="mono faint">{review.model}</span> : null}
            </header>

            {review.novelty_note?.trim() ? (
              <p style={{ fontFamily: "var(--font-prose)" }}>{review.novelty_note}</p>
            ) : null}

            {structured.length > 0 ? (
              <div className="review__fields" style={{ marginTop: "var(--space-3)" }}>
                {structured.map((field) => (
                  <div className="review__field" key={field.key}>
                    <span className="label">{field.label}</span>
                    <p>{review[field.key]}</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="faint" style={{ marginTop: "var(--space-3)" }}>
                This review was imported from an earlier engine, which recorded only the
                verdict.
              </p>
            )}
          </article>
        );
      })}
    </div>
  );
}

/* --- Matches --------------------------------------------------------------- */

type Side = "a" | "b";

function sideOf(match: MatchRow, hid: string): Side {
  return match.a.hid === hid ? "a" : "b";
}

function eloPair(
  match: MatchRow,
  side: Side,
): { before: number | null; after: number | null } {
  return side === "a"
    ? { before: match.elo_a_before, after: match.elo_a_after }
    : { before: match.elo_b_before, after: match.elo_b_after };
}

function won(match: MatchRow, side: Side): boolean | null {
  if (match.winner == null) return null;
  return (match.winner === 1 && side === "a") || (match.winner === 2 && side === "b");
}

/**
 * Every match this hypothesis fought. Elo before and after are printed rather
 * than implied: imported runs have no per-match Elo at all, and a zero there
 * would describe a tournament that was never recorded — so those cells read
 * "—".
 */
export function MatchTable({ hid, matches }: { hid: string; matches: MatchRow[] }) {
  if (matches.length === 0) {
    return <p className="tab-note">This hypothesis has not been in a match yet.</p>;
  }

  return (
    <div className="scroll-x">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Round</th>
            <th scope="col">Against</th>
            <th scope="col">Result</th>
            <th scope="col" title={METRIC_HINTS.elo}>
              Elo
            </th>
            <th scope="col">Judged by</th>
            <th scope="col">When</th>
          </tr>
        </thead>
        <tbody>
          {matches.map((match) => {
            const side = sideOf(match, hid);
            const other = side === "a" ? match.b : match.a;
            const elo = eloPair(match, side);
            const outcome = won(match, side);
            const delta =
              elo.before != null && elo.after != null ? elo.after - elo.before : null;
            return (
              <tr key={match.id}>
                <td className="numeral">{match.round}</td>
                <td>{other.title || other.hid}</td>
                <td>
                  {outcome == null ? (
                    <span className="faint">Not decided</span>
                  ) : (
                    <Chip tone={outcome ? "go" : "neutral"}>
                      {outcome ? "Won" : "Lost"}
                    </Chip>
                  )}
                </td>
                <td className="numeral">
                  {elo.before == null && elo.after == null ? (
                    <span className="faint">—</span>
                  ) : (
                    <>
                      {formatElo(elo.before)} → {formatElo(elo.after)}{" "}
                      {delta != null ? (
                        <span
                          className="match-delta"
                          data-direction={
                            delta > 0 ? "up" : delta < 0 ? "down" : undefined
                          }
                        >
                          ({delta > 0 ? "+" : ""}
                          {Math.round(delta)})
                        </span>
                      ) : null}
                    </>
                  )}
                </td>
                <td className="mono faint">{match.judge_model ?? "—"}</td>
                <td className="faint">{match.ts ? relativeTime(match.ts) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* --- Elo curve ------------------------------------------------------------- */

const CURVE_WIDTH = 520;
const CURVE_HEIGHT = 150;
const PAD_X = 12;
const PAD_TOP = 18;
const PAD_BOTTOM = 24;
const START_ELO = 1200;

/**
 * How this hypothesis's strength moved, match by match.
 *
 * One series, so no legend — the panel title names it. The dashed line is the
 * 1,200 every hypothesis starts at, which is what makes the shape readable at
 * all. Points carry the match in a tooltip and the table underneath is the
 * accessible reading of the same data.
 *
 * Returns null when there is nothing to draw: imported runs recorded final Elo
 * but no per-match history, and an empty axis would imply otherwise.
 */
export function EloCurve({ hid, matches }: { hid: string; matches: MatchRow[] }) {
  const points: { elo: number; match: MatchRow | null; won: boolean | null }[] = [];

  const ordered = [...matches].sort((left, right) => left.round - right.round);
  for (const match of ordered) {
    const side = sideOf(match, hid);
    const { before, after } = eloPair(match, side);
    if (before == null && after == null) continue;
    if (points.length === 0 && before != null) {
      points.push({ elo: before, match: null, won: null });
    }
    if (after != null) points.push({ elo: after, match, won: won(match, side) });
  }

  if (points.length < 2) return null;

  const values = points.map((point) => point.elo);
  const low = Math.min(START_ELO, ...values) - 12;
  const high = Math.max(START_ELO, ...values) + 12;
  const span = high - low || 1;

  const x = (index: number): number =>
    PAD_X + (index / (points.length - 1)) * (CURVE_WIDTH - PAD_X * 2);
  const y = (elo: number): number =>
    PAD_TOP + (1 - (elo - low) / span) * (CURVE_HEIGHT - PAD_TOP - PAD_BOTTOM);

  const path = points
    .map(
      (point, index) =>
        `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(point.elo).toFixed(1)}`,
    )
    .join(" ");
  const last = points[points.length - 1];

  return (
    <svg
      className="elo-curve"
      viewBox={`0 0 ${CURVE_WIDTH} ${CURVE_HEIGHT}`}
      role="img"
      aria-label={`Elo over ${points.length - 1} matches, ending at ${formatElo(last.elo)}`}
    >
      <line
        className="elo-curve__baseline"
        x1={PAD_X}
        x2={CURVE_WIDTH - PAD_X}
        y1={y(START_ELO)}
        y2={y(START_ELO)}
      />
      <text className="elo-curve__label" x={PAD_X} y={y(START_ELO) - 4}>
        start {START_ELO}
      </text>

      <path className="elo-curve__line" d={path} />

      {points.map((point, index) => (
        <circle
          key={index}
          className="elo-curve__point"
          data-result={point.won === false ? "loss" : undefined}
          cx={x(index)}
          cy={y(point.elo)}
          r={4}
        >
          <title>
            {point.match
              ? `Round ${point.match.round} · ${point.won ? "beat" : "lost to"} ${
                  (sideOf(point.match, hid) === "a" ? point.match.b : point.match.a).hid
                } · ${formatElo(point.elo)}`
              : `Starting strength ${formatElo(point.elo)}`}
          </title>
        </circle>
      ))}

      <text
        className="elo-curve__value"
        x={CURVE_WIDTH - PAD_X}
        y={y(last.elo) - 10}
        textAnchor="end"
      >
        {formatElo(last.elo)}
      </text>
      <text className="elo-curve__label" x={PAD_X} y={CURVE_HEIGHT - 6}>
        first match
      </text>
      <text
        className="elo-curve__label"
        x={CURVE_WIDTH - PAD_X}
        y={CURVE_HEIGHT - 6}
        textAnchor="end"
      >
        latest
      </text>
    </svg>
  );
}

/* --- Lineage --------------------------------------------------------------- */

/** Where an idea came from and what it became — a tree, not "h007 <- h001". */
export function Lineage({
  runId,
  parents,
  evolvedInto,
}: {
  runId: string;
  parents: HypothesisRow[];
  evolvedInto: HypothesisRow[];
}) {
  if (parents.length === 0 && evolvedInto.length === 0) {
    return (
      <p className="tab-note">
        Written from scratch in generation, and nothing has been evolved from it yet.
      </p>
    );
  }

  return (
    <div className="lineage">
      {parents.length > 0 ? (
        <div className="lineage__group">
          <span className="label">Evolved from</span>
          {parents.map((row) => (
            <LineageItem key={row.hid} runId={runId} row={row} />
          ))}
        </div>
      ) : null}

      {evolvedInto.length > 0 ? (
        <div className="lineage__group">
          <span className="label">Led to</span>
          {evolvedInto.map((row) => (
            <LineageItem key={row.hid} runId={runId} row={row} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function LineageItem({ runId, row }: { runId: string; row: HypothesisRow }) {
  return (
    <Link className="lineage__item" to={`/runs/${runId}/hypotheses/${row.hid}`}>
      <span className="lineage__hid">{row.hid}</span>
      <span className="truncate">{row.title}</span>
      <span className="spacer" />
      <span className="numeral faint">{formatElo(row.elo)}</span>
    </Link>
  );
}

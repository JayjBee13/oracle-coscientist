import { Link } from "react-router-dom";

import type { HypothesisRow } from "../../api/types";
import { formatElo, plural } from "../../lib/format";
import { METRIC_HINTS, describeHypothesisStatus } from "../../lib/status";

const ROW_HEIGHT = 46;

/**
 * The standings, on **one** scale.
 *
 * The previous UI drew Elo bars against two different denominators in two
 * places (one of them a magic `/15`), which made the same hypothesis look
 * stronger in one panel than the other. Here the scale is computed once from
 * the whole list and printed at both ends, so a bar means something you can
 * check.
 *
 * Rows are absolutely positioned by rank rather than reordered in the DOM, so
 * when a match flips the standings the rows slide past each other instead of
 * teleporting — the one place in this app where animation carries information.
 * `prefers-reduced-motion` collapses the transition globally.
 */
export function Leaderboard({
  runId,
  rows,
  limit,
  ranked = false,
}: {
  runId: string;
  rows: HypothesisRow[];
  limit?: number;
  /**
   * Whether this list is being presented as *standings*. When it is, a row that
   * has not earned its position does not get one: an idea with no matches sits
   * at the untouched default of 1200, which used to print as a rank ordinal
   * above every idea that competed and lost, and a rejected or archived one kept
   * whatever rating it had on the way out. Neither had any visual signal at all.
   */
  ranked?: boolean;
}) {
  const shown = limit ? rows.slice(0, limit) : rows;
  if (shown.length === 0) {
    return (
      <p className="tab-note">
        No hypotheses yet. The first round writes them, then reviews them.
      </p>
    );
  }

  const scored = rows.filter((row) => row.elo != null);
  const tournament = rows.some((row) => row.matches > 0);
  const values = scored.map((row) => row.elo as number);
  const low = values.length > 0 ? Math.min(...values) : 0;
  const high = values.length > 0 ? Math.max(...values) : 0;
  const span = high - low;
  const allRejected = rows.length > 0 && rows.every((row) => row.status === "rejected");

  const width = (elo: number | null): string => {
    if (elo == null || !tournament) return "0%";
    if (span <= 0) return "50%";
    return `${Math.max(4, Math.round(((elo - low) / span) * 100))}%`;
  };

  return (
    <div className="lb">
      {allRejected ? (
        <p className="tab-note">
          All {rows.length} hypotheses were rejected in review — no tournament ran. Their
          critiques are still worth reading.
        </p>
      ) : tournament ? (
        <div className="lb__scale" aria-hidden="true">
          <span>Elo {formatElo(low)}</span>
          <span>{formatElo(high)}</span>
        </div>
      ) : (
        <p className="tab-note">
          No matches yet — everything starts level, so there is nothing to rank.
        </p>
      )}

      <div className="lb__rows" style={{ height: shown.length * ROW_HEIGHT }}>
        {shown.map((row, index) => {
          const status = describeHypothesisStatus(row.status);
          // A rating only means something once it has been earned in a match, and
          // only for an idea still in play. Everything else shows what it is.
          const placed = !ranked || (row.status === "active" && row.matches > 0);
          return (
            <Link
              key={row.hid}
              className="lb__row"
              data-status={row.status}
              data-tone={row.status === "active" && placed ? "accent" : "neutral"}
              style={{ transform: `translateY(${index * ROW_HEIGHT}px)` }}
              to={`/runs/${runId}/hypotheses/${row.hid}`}
              title={`${row.title} — ${status.label}, ${plural(row.matches, "match", "matches")}`}
            >
              <span className="lb__rank numeral">{placed ? index + 1 : "—"}</span>
              <span className="lb__label">
                <span className="lb__title">{row.title}</span>
                {tournament && placed ? (
                  <span className="lb__bar">
                    <span className="lb__fill" style={{ width: width(row.elo) }} />
                  </span>
                ) : (
                  <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
                    {placed
                      ? status.label
                      : row.status === "active"
                        ? "unranked — no matches yet"
                        : status.label}
                  </span>
                )}
              </span>
              <span className="lb__elo" title={METRIC_HINTS.elo}>
                {tournament && placed ? formatElo(row.elo) : "—"}
              </span>
            </Link>
          );
        })}
      </div>

      {limit && rows.length > limit ? (
        <p className="tab-note">{rows.length - limit} more in the Hypotheses tab.</p>
      ) : null}
    </div>
  );
}

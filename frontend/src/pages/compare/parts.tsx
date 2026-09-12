import { useId, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";

import type { RunSummary } from "../../api/types";
import { Chip, StatusChip } from "../../components/Status";
import type { DeltaView, GraftView, RankedList, Verdict } from "../../lib/compare";
import { formatChange, formatMetricValue } from "../../lib/compare";
import { METRIC_HINTS, describeRunStatus } from "../../lib/status";
import type { Tone } from "../../lib/status";

/* --- Picking the two runs -------------------------------------------------
   Rich rows rather than a select of opaque ids: the previous UI identified runs
   by filename and made you guess. Search is client-side over one already-loaded
   page of runs, so typing filters instantly and the two pickers never race each
   other for the store's list.
   ------------------------------------------------------------------------- */

export function RunPicker({
  label,
  hint,
  runs,
  selectedId,
  conflictId,
  onSelect,
}: {
  label: string;
  hint: string;
  runs: RunSummary[];
  selectedId: string | null;
  conflictId: string | null;
  onSelect: (runId: string) => void;
}) {
  const id = useId();
  const [search, setSearch] = useState("");

  const matches = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return runs;
    return runs.filter((run) =>
      `${run.title} ${run.question} ${run.engine_run_id}`.toLowerCase().includes(needle),
    );
  }, [runs, search]);

  return (
    <div className="stack" style={{ gap: "var(--space-2)" }}>
      <div className="field">
        <label className="field__label" htmlFor={id}>
          {label}
        </label>
        <input
          id={id}
          className="input"
          type="search"
          value={search}
          placeholder="Search by title or question"
          onChange={(event) => setSearch(event.target.value)}
        />
        <span className="field__hint">{hint}</span>
      </div>

      {matches.length === 0 ? (
        <p className="faint" style={{ padding: "var(--space-3)" }}>
          No run matches “{search}”.
        </p>
      ) : (
        <div className="picker__list" role="listbox" aria-label={label}>
          {matches.map((run) => {
            const status = describeRunStatus(run);
            const isConflict = run.id === conflictId;
            return (
              <button
                key={run.id}
                type="button"
                className="picker__row"
                aria-pressed={run.id === selectedId}
                onClick={() => onSelect(run.id)}
              >
                <span className="stack" style={{ gap: 2, minWidth: 0, flex: 1 }}>
                  <span className="picker__title">{run.title}</span>
                  <span className="picker__meta">
                    {run.round}/{run.rounds_target} rounds · {run.counts.active} active ·{" "}
                    {run.counts.matches} matches
                    {isConflict ? " · already on the other side" : ""}
                  </span>
                </span>
                <StatusChip status={status} />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* --- Banners and the verdict ---------------------------------------------- */

export function Banner({
  tone,
  title,
  children,
}: {
  tone: Tone;
  title: string;
  children: ReactNode;
}) {
  return (
    <div
      className="cmp-banner"
      data-tone={tone}
      role={tone === "caution" ? "alert" : undefined}
    >
      <div className="stack" style={{ gap: "var(--space-1)" }}>
        <span className="cmp-banner__title">{title}</span>
        <span className="muted">{children}</span>
      </div>
    </div>
  );
}

export function VerdictCard({ verdict }: { verdict: Verdict }) {
  return (
    <div className="card verdict" data-tone={verdict.tone}>
      <span className="label">Verdict</span>
      <p className="verdict__headline" style={{ marginTop: "var(--space-2)" }}>
        {verdict.headline}
      </p>
      <p className="muted" style={{ marginTop: "var(--space-3)", maxWidth: "78ch" }}>
        {verdict.detail}
      </p>
    </div>
  );
}

/* --- Deltas --------------------------------------------------------------- */

export function DeltaTable({
  deltas,
  baselineTitle,
  challengerTitle,
}: {
  deltas: DeltaView[];
  baselineTitle: string;
  challengerTitle: string;
}) {
  return (
    <div className="scroll-x">
      <table className="delta-table">
        <thead>
          <tr>
            <th scope="col">Measure</th>
            <th scope="col">{baselineTitle}</th>
            <th scope="col">{challengerTitle}</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          {deltas.map((delta) => (
            <tr key={delta.metric}>
              <td>
                {delta.metric}
                {delta.metric.toLowerCase().includes("elo") ? (
                  <span
                    className="faint"
                    style={{ display: "block", fontSize: "var(--text-xs)" }}
                  >
                    Distance from this run's best hypothesis to its median one — a
                    property of its own tournament, so it does cross runs.
                  </span>
                ) : null}
              </td>
              <td>{formatMetricValue(delta.base)}</td>
              <td>{formatMetricValue(delta.challenger)}</td>
              <td>
                <span className="delta__change" data-tone={delta.tone}>
                  <span aria-hidden="true">{delta.arrow}</span>
                  {formatChange(delta.change)}
                  <span className="delta__word">{delta.label}</span>
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* --- One side's ranked list ----------------------------------------------
   A chart of one series, so no legend: the panel heading names it. The bar is
   normalised against *this* run's best and worst and says so in the caption;
   the Elo beside it is the real number, because the bar is a comparison the
   reader makes inside this column and nowhere else.
   ------------------------------------------------------------------------- */

export function RankedPanel({
  side,
  run,
  list,
}: {
  side: "Baseline" | "Challenger";
  run: RunSummary;
  list: RankedList;
}) {
  return (
    <section className="panel" aria-label={`${side}: ${run.title}`}>
      <div className="cmp-side__head">
        <span className="label">{side}</span>
        <Link to={`/runs/${run.id}`} className="cmp-side__title">
          {run.title}
        </Link>
        <span className="row-wrap" style={{ gap: "var(--space-2)", marginTop: 4 }}>
          <StatusChip status={describeRunStatus(run)} />
          <Chip tone="neutral" className="chip--quiet" title={METRIC_HINTS.matches}>
            {run.counts.matches} matches
          </Chip>
        </span>
      </div>

      {list.rows.length === 0 ? (
        <p className="muted" style={{ padding: "var(--space-6)", textAlign: "center" }}>
          This run has no hypotheses to rank.
        </p>
      ) : (
        <div className="rank">
          {list.rows.map((row) => (
            <Link
              key={row.hid}
              className="rank__row"
              to={`/runs/${run.id}/hypotheses/${row.hid}`}
            >
              <span className="rank__place">{row.rank}</span>
              <span className="rank__title" title={row.title}>
                {row.title}
              </span>
              <span className="rank__elo" title={METRIC_HINTS.elo}>
                {row.elo === null ? "—" : Math.round(row.elo)}
              </span>
              <span className="rank__bar">
                <span
                  className="rank__fill"
                  style={{ width: `${Math.round(row.share * 100)}%` }}
                />
              </span>
            </Link>
          ))}
        </div>
      )}

      <p className="rank__caption">
        {list.basis === "all"
          ? `All ${list.total} hypotheses — none survived review, so there was no tournament to rank.`
          : `Top ${list.rows.length} of ${list.total} active. `}
        {list.basis !== "none" && list.best !== null && list.worst !== null
          ? `Bar length is position between this run's weakest (${Math.round(list.worst)}) and strongest (${Math.round(list.best)}) — Elo does not carry across runs.`
          : ""}
      </p>
    </section>
  );
}

/* --- Diversity injection --------------------------------------------------
   Rendered only for runs whose engine had the concept. Zeros for a v1 run would
   claim its injection never fired, which is a false statement rather than an
   empty one.
   ------------------------------------------------------------------------- */

export function GraftSummary({ side, graft }: { side: string; graft: GraftView }) {
  return (
    <div className="stack" style={{ gap: "var(--space-1)" }}>
      <span className="label">{side}</span>
      <span>
        {graft.enabled ? "On" : "Off"} · fired {graft.firedCount}{" "}
        {graft.firedCount === 1 ? "time" : "times"}
        {graft.collapseEvents !== null
          ? ` · ${graft.collapseEvents} collapse check${graft.collapseEvents === 1 ? "" : "s"}`
          : ""}
      </span>
    </div>
  );
}

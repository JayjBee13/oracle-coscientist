import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import * as api from "../../api/client";
import { errorMessage } from "../../api/client";
import type { HypothesisRow, RunDetail, RunEvent, RunSummary } from "../../api/types";
import { IdeaGraph } from "../../components/graph/IdeaGraph";
import type { GraphNode, RunGraph } from "../../components/graph/graphModel";
import {
  applyEvent,
  needsRefetchFor,
  withLabels,
} from "../../components/graph/graphModel";
import { Markdown } from "../../components/Markdown";
import { HypothesisCard } from "../../components/run/HypothesisCard";
import { Lineage, MatchTable, ReviewList } from "../../components/run/HypothesisParts";
import { Chip, LabelChip } from "../../components/Status";
import {
  EmptyState,
  ErrorState,
  SkeletonBlock,
  SkeletonText,
} from "../../components/States";
import { formatElo, previewText, stripLeadingTitle } from "../../lib/format";
import { useHypothesisDetail } from "../../lib/hooks";
import {
  METRIC_HINTS,
  clusterLabel,
  describeHypothesisStatus,
  describeOperator,
  isActiveLifecycle,
} from "../../lib/status";
import { useRun } from "../../store/runs";

/**
 * The ideas of one run: how they descended from each other, and how they ended.
 *
 * Three ways of looking at the same set, and the order is the argument. The
 * **organic** graph is the default because the question a researcher brings to
 * this tab is "what survived, and where did it come from" — a shape, not a
 * ranking — and the force layout is the only view where a clump of related
 * ideas reads as a clump. It is safe as a default only because it is
 * reproducible: `lib/graphLayout` seeds its PRNG from the run id, so the same
 * run always draws the same picture. **Layered** puts the rounds in bands, which
 * is what to switch to when tracing one lineage exactly. **List** is the ranked
 * document this tab used to be, unchanged, because sometimes you just want to
 * read top-down.
 *
 * The choice lives in `?view=`, so a link a scientist pastes into a message
 * opens on what they were looking at. It is a *view* preference and never
 * touches `runs.config`: nothing here changes what the run did.
 *
 * Three rules the wiring follows.
 *
 * 1. **One graph request per run, not one per event.** `GET /graph` is read
 *    once; the live stream advances the model in place through
 *    `graphModel.applyEvent`. Refetching on every event would re-solve the
 *    layout several times a round and reshuffle the canvas under the reader —
 *    which is exactly what pin-on-place exists to prevent. Only the two events
 *    that change the graph without saying how (`cluster_applied`,
 *    `graft_fired`), and the two that are cheap places to re-sync, ask again.
 * 2. **One way to load a hypothesis.** The rail uses `useHypothesisDetail`, the
 *    same hook the cards use. There is no second fetch path and no cache to go
 *    stale against a later round's evolution.
 * 3. **The ideas outlive the drawing.** If the genealogy cannot be assembled —
 *    an older backend, a bad round trip — this tab still has the whole
 *    leaderboard in hand, so it says what failed and shows the ranked list
 *    rather than replacing a run's output with an error box.
 */

const VIEWS = [
  { id: "organic", label: "Organic" },
  { id: "layered", label: "Layered" },
  { id: "list", label: "List" },
] as const;

type ViewId = (typeof VIEWS)[number]["id"];

/** Explicitly the default, and explicitly not what the recommendation was. */
const DEFAULT_VIEW: ViewId = "organic";

const STATUS_FILTERS = [
  { value: "all", label: "All" },
  { value: "active", label: "In play" },
  { value: "rejected", label: "Rejected" },
  { value: "archived", label: "Set aside" },
] as const;

const SORTS = [
  { value: "elo", label: "Strongest first" },
  { value: "recent", label: "Newest first" },
  { value: "hid", label: "Order written" },
] as const;

type SortId = (typeof SORTS)[number]["value"];

/**
 * Events after which the graph is re-read even though nothing was missed.
 *
 * The patch path can only be as complete as the payloads, and a fetch in flight
 * when an idea arrives would have missed it. Both of these happen at most once
 * per round, which makes them the cheapest possible place to re-sync — and a
 * finished run's last picture is then exactly what the database holds.
 */
const RESYNC_EVENTS: ReadonlySet<string> = new Set(["round_completed", "run_finished"]);

export function HypothesesTab({
  run,
  detail,
}: {
  run: RunSummary;
  detail: RunDetail | null;
}) {
  const [params, setParams] = useSearchParams();
  const [selectedHid, setSelectedHid] = useState<string | null>(null);

  // The events are already in the store — the workspace opened the stream for
  // any run that can still move. Reading them here is not a second transport.
  const entry = useRun(run.id);
  const { graph, error, reload } = useRunGraph(run.id, entry.events, entry.lastSeq);

  const requested = params.get("view");
  const view: ViewId =
    VIEWS.find((candidate) => candidate.id === requested)?.id ?? DEFAULT_VIEW;

  function selectView(next: ViewId): void {
    const updated = new URLSearchParams(params);
    // The default carries no parameter, so a plain link to the tab and a link
    // to its default view are the same URL.
    if (next === DEFAULT_VIEW) updated.delete("view");
    else updated.set("view", next);
    setParams(updated, { replace: true });
  }

  if (!detail) return <SkeletonBlock height={280} />;

  const rows = detail.leaderboard;
  const nodes = graph?.nodes ?? [];

  // "Nothing yet" is only true once something has answered. A run whose graph
  // is still in flight and whose leaderboard has not caught up is *loading*,
  // and saying it produced nothing would be a claim, then a correction.
  if (rows.length === 0 && nodes.length === 0) {
    if (!graph && !error) return <SkeletonBlock height={280} />;
    return (
      <EmptyState title="No hypotheses yet">
        The first round writes them, then reviews them. Nothing has been produced for this
        run.
      </EmptyState>
    );
  }

  const selectedNode = nodes.find((node) => node.hid === selectedHid) ?? null;
  const selectedRow = rows.find((row) => row.hid === selectedHid) ?? null;
  const live = isActiveLifecycle(run.lifecycle) && run.source !== "imported";
  const drawable = graph !== null;

  return (
    <div className="stack">
      {/* A switch appears with the thing it switches between. When the graph
          could not be loaded there is one view, not three, and a tab marked
          selected over a list that is not what it names would be a lie told in
          the accessibility tree.

          `role="tablist"` is also a promise about the keyboard: one tab stop
          for the set, and the arrow keys to move within it. Declared without
          being honoured, it announces "tab 1 of 3" and then leaves the arrow
          keys a reader has been told about inert. */}
      {drawable ? (
        <div
          className="ws-tabs"
          role="tablist"
          aria-label="How to look at these ideas"
          onKeyDown={(event) => {
            const at = VIEWS.findIndex((candidate) => candidate.id === view);
            const to =
              event.key === "ArrowRight"
                ? (at + 1) % VIEWS.length
                : event.key === "ArrowLeft"
                  ? (at - 1 + VIEWS.length) % VIEWS.length
                  : event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? VIEWS.length - 1
                      : -1;
            if (to < 0) return;
            event.preventDefault();
            selectView(VIEWS[to].id);
            document.getElementById(`ideas-tab-${VIEWS[to].id}`)?.focus();
          }}
        >
          {VIEWS.map((candidate) => (
            <button
              key={candidate.id}
              type="button"
              className="ws-tab"
              role="tab"
              id={`ideas-tab-${candidate.id}`}
              aria-selected={view === candidate.id}
              aria-controls="ideas-panel"
              tabIndex={view === candidate.id ? 0 : -1}
              onClick={() => selectView(candidate.id)}
            >
              {candidate.label}
            </button>
          ))}
        </div>
      ) : null}

      <div
        role={drawable ? "tabpanel" : undefined}
        id="ideas-panel"
        aria-labelledby={drawable ? `ideas-tab-${view}` : undefined}
      >
        {view === "list" || !drawable ? (
          <div className="stack">
            {!drawable && error ? (
              <p className="notice" data-tone="caution">
                <span>
                  The genealogy could not be loaded — {error} The ideas themselves are
                  below, ranked.
                </span>
                <button type="button" className="btn btn--sm" onClick={reload}>
                  Try again
                </button>
              </p>
            ) : null}
            <RankedList run={run} rows={rows} />
          </div>
        ) : (
          <div className="ws-grid">
            <div className="stack">
              {error ? (
                <p className="notice" data-tone="caution">
                  <span>
                    The graph below is the last one that loaded — the refresh failed:{" "}
                    {error}
                  </span>
                  <button type="button" className="btn btn--sm" onClick={reload}>
                    Try again
                  </button>
                </p>
              ) : null}
              <IdeaGraph
                graph={graph}
                view={view === "layered" ? "layered" : "organic"}
                selectedHid={selectedHid}
                onSelect={setSelectedHid}
                seed={run.engine_run_id}
                live={live}
              />
            </div>

            <IdeaRail runId={run.id} node={selectedNode} row={selectedRow} />
          </div>
        )}
      </div>
    </div>
  );
}

/* --- The graph, read once and then advanced -------------------------------- */

type GraphLoad = {
  graph: RunGraph | null;
  /** Present alongside a graph when a *refresh* failed over good data. */
  error: string | null;
  reload: () => void;
};

/**
 * One run's graph: fetched once, then moved forward by the events the store is
 * already receiving.
 *
 * The watermark is read when a response *lands* rather than when it was asked
 * for, because what the server sent contains everything that had happened by
 * then; replaying those events over it would count some matches twice. The
 * opposite mistake — an idea born while the request was in flight — is what
 * `RESYNC_EVENTS` closes, at the end of the round rather than immediately,
 * since a node arriving a minute late is a smaller lie than a canvas that
 * re-solves itself every few seconds.
 */
function useRunGraph(
  runId: string,
  events: readonly RunEvent[],
  lastSeq: number | null,
): GraphLoad {
  const [reloadKey, setReloadKey] = useState(0);
  const [loaded, setLoaded] = useState<{
    runId: string;
    graph: RunGraph | null;
    error: string | null;
  }>({ runId: "", graph: null, error: null });

  const applied = useRef(0);
  // The response handler below must read the newest watermark, not the one that
  // existed when the request was made, so it goes through a ref rather than a
  // closure — kept current in an effect, because a ref written during a render
  // is a render with a side effect in it.
  const streamSeq = useRef(lastSeq ?? 0);
  useEffect(() => {
    streamSeq.current = lastSeq ?? 0;
  }, [lastSeq]);

  useEffect(() => {
    const controller = new AbortController();

    api
      .getRunGraph(runId, controller.signal)
      .then((fetched) => {
        if (controller.signal.aborted) return;
        applied.current = streamSeq.current ?? 0;
        // The one place a fetched graph becomes the app's graph, so the canvas
        // label is derived once here and once in `applyEvent` — and nowhere in
        // a render.
        setLoaded({ runId, graph: withLabels(fetched), error: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded((current) => ({
          runId,
          // A failed refresh keeps the picture it already drew: a blank canvas
          // is a worse answer than a slightly old one, and the note says so.
          graph: current.runId === runId ? current.graph : null,
          error: errorMessage(error),
        }));
      });

    return () => controller.abort();
  }, [runId, reloadKey]);

  useEffect(() => {
    if (loaded.runId !== runId || !loaded.graph) return;
    const fresh = events.filter((event) => event.seq > applied.current);
    if (fresh.length === 0) return;
    // The store keeps an unparseable stream line as an event with `seq: -1`, on
    // purpose. Taking the last event's sequence blindly therefore *rewound* the
    // watermark whenever a malformed line ended a batch, and the next batch
    // replayed up to five hundred buffered events — every buffered match
    // counted a second time, so the rail read "8 of 4 matches won". Advance
    // only on real sequence numbers, and only forwards.
    for (const event of fresh) {
      if (event.seq > applied.current) applied.current = event.seq;
    }

    let next = loaded.graph;
    let resync = false;
    for (const event of fresh) {
      next = applyEvent(next, event);
      if (needsRefetchFor(event) || RESYNC_EVENTS.has(event.type)) resync = true;
    }

    if (next !== loaded.graph) setLoaded({ runId, graph: next, error: null });
    if (resync) setReloadKey((key) => key + 1);
  }, [events, loaded, runId]);

  const reload = useCallback(() => setReloadKey((key) => key + 1), []);
  const mine = loaded.runId === runId;
  return { graph: mine ? loaded.graph : null, error: mine ? loaded.error : null, reload };
}

/* --- The rail --------------------------------------------------------------
   A rail rather than a modal, so that clicking from idea to idea compares them
   against the graph instead of covering it. It holds the reading register: the
   claim, what the reviewers said, who it beat, and where it came from — the
   tournament being a fact about one idea rather than a shape of the run.
   ------------------------------------------------------------------------- */

function IdeaRail({
  runId,
  node,
  row,
}: {
  runId: string;
  node: GraphNode | null;
  row: HypothesisRow | null;
}) {
  // An idea inserted from the stream carries no hypothesis id until the graph
  // is re-read, and the leaderboard sometimes has one before the graph does.
  const hypothesisId = node?.id || row?.id || null;
  const { detail, status, error } = useHypothesisDetail(hypothesisId);

  return (
    <aside className="panel" aria-label="Selected idea">
      <header className="panel__head">
        <span className="label">Selected idea</span>
      </header>
      <div className="panel__body">
        {/* Selecting a circle changes this whole panel and moves no focus, so
            without this the change is silent: `aria-current` flipping on a
            `<g>` is not reliably spoken. */}
        <p className="visually-hidden" aria-live="polite">
          {node ? `${node.hid} selected — ${node.title}` : ""}
        </p>
        {!node ? (
          <p className="tab-note">
            Choose an idea to read it. Click a circle, or tab to the graph and use the
            arrow keys — up and down follow descent.
          </p>
        ) : (
          <>
            <div className="hyp__meta" style={{ marginTop: 0 }}>
              <span className="mono faint">{node.hid}</span>
              <LabelChip value={describeHypothesisStatus(node.status)} />
              {node.operator ? (
                <LabelChip value={describeOperator(node.operator)} />
              ) : null}
              {node.cluster ? (
                // A cluster name is a clause the model wrote, not a tag: one on
                // this canvas runs to sixty characters, and a chip does not
                // wrap, so at full length it hangs out over the graph. Clipped
                // here and whole in the tooltip, exactly as the legend does it —
                // and through the same `clusterLabel`, so the rail and the
                // legend cannot say "Theme: 0" and "Cluster 0" about one node.
                <Chip title={`${clusterLabel(node.cluster)} — ${METRIC_HINTS.cluster}`}>
                  {previewText(clusterLabel(node.cluster), 28)}
                </Chip>
              ) : null}
            </div>

            <h3 className="hyp__title" style={{ marginTop: "var(--space-3)" }}>
              {node.title}
            </h3>

            <dl className="kv" style={{ marginTop: "var(--space-4)" }}>
              <dt title={METRIC_HINTS.elo}>Strength</dt>
              <dd className="numeral">
                {formatElo(node.elo)}
                <span className="faint"> · within this run</span>
              </dd>
              <dt>Record</dt>
              <dd>
                {node.matches > 0
                  ? `${node.wins} of ${node.matches} matches won`
                  : "No matches played"}
              </dd>
              <dt>Written</dt>
              <dd>Round {node.created_round}</dd>
              {node.duplicate_of ? (
                <>
                  <dt>Merged into</dt>
                  <dd className="mono">{node.duplicate_of}</dd>
                </>
              ) : null}
            </dl>

            {hypothesisId === null ? (
              <p className="tab-note">
                This idea has just arrived, so its text has not been read back yet. It
                appears when the round finishes.
              </p>
            ) : status === "loading" ? (
              <div style={{ marginTop: "var(--space-5)" }}>
                <SkeletonText lines={5} />
              </div>
            ) : status === "error" ? (
              <ErrorState
                title="Could not open this idea"
                message={error ?? "The backend did not answer."}
              />
            ) : detail ? (
              <>
                <div className="hyp__section">
                  {/* The body opens with `# <title>` on most runs, and the
                      title is three lines up. See `stripLeadingTitle`. */}
                  <Markdown source={stripLeadingTitle(detail.body_md, node.title)} />
                </div>

                <div className="hyp__section">
                  <span className="label">What the reviewers said</span>
                  <ReviewList reviews={detail.reviews} />
                </div>

                {detail.match_history.length > 0 ? (
                  <div className="hyp__section">
                    <span className="label">Head to head</span>
                    <MatchTable hid={node.hid} matches={detail.match_history} />
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
              </>
            ) : null}

            <div className="row-wrap" style={{ marginTop: "var(--space-6)" }}>
              <Link className="btn" to={`/runs/${runId}/hypotheses/${node.hid}`}>
                Open on its own page
              </Link>
            </div>
          </>
        )}
      </div>
    </aside>
  );
}

/* --- The list, as it always was -------------------------------------------- */

/**
 * The ranked document this tab used to be.
 *
 * Sorting and filtering happen here rather than on the API because the run's
 * whole leaderboard already arrived with the detail — a round trip to reorder
 * sixty rows we are holding would be a strange thing to do.
 */
function RankedList({ run, rows }: { run: RunSummary; rows: HypothesisRow[] }) {
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [sort, setSort] = useState<SortId>("elo");
  const [openHid, setOpenHid] = useState<string | null>(null);

  const filtered = rows.filter(
    (row) => statusFilter === "all" || row.status === statusFilter,
  );
  const sorted = sortRows(filtered, sort);
  const allRejected = rows.length > 0 && rows.every((row) => row.status === "rejected");

  return (
    <div className="stack">
      {allRejected ? (
        <p className="notice" data-tone="caution">
          All {rows.length} hypotheses were rejected in review, so no tournament ran. What
          the reviewers objected to is the useful output of this run.
        </p>
      ) : null}

      <div className="runs-filters">
        {STATUS_FILTERS.map((option) => (
          <button
            key={option.value}
            type="button"
            className="chip chip--button"
            aria-pressed={statusFilter === option.value}
            onClick={() => setStatusFilter(option.value)}
          >
            {option.label}{" "}
            <span className="ws-tab__count">
              {option.value === "all"
                ? rows.length
                : rows.filter((row) => row.status === option.value).length}
            </span>
          </button>
        ))}

        <span className="spacer" />

        <label className="runs-sort">
          <span className="label">Sort</span>
          <select
            className="select"
            value={sort}
            aria-label="Sort hypotheses"
            onChange={(event) => setSort(event.target.value as SortId)}
          >
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {sorted.length === 0 ? (
        <EmptyState title="Nothing in that state">
          No hypothesis in this run is currently in that state.
        </EmptyState>
      ) : (
        <div className="hyp-list">
          {sorted.map((row, index) => (
            <HypothesisCard
              key={row.hid}
              runId={run.id}
              row={row}
              rank={index + 1}
              open={openHid === row.hid}
              onToggle={() => setOpenHid(openHid === row.hid ? null : row.hid)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function sortRows(rows: HypothesisRow[], sort: SortId): HypothesisRow[] {
  const copy = [...rows];
  if (sort === "hid")
    return copy.sort((left, right) => left.hid.localeCompare(right.hid));
  if (sort === "recent") {
    return copy.sort(
      (left, right) =>
        right.created_round - left.created_round || left.hid.localeCompare(right.hid),
    );
  }
  return copy.sort((left, right) => (right.elo ?? 0) - (left.elo ?? 0));
}

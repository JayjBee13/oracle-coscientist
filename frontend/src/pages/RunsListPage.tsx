import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Archive, ArchiveRestore, Pencil, Search, X } from "lucide-react";

import type { RunSummary, RunsQuery } from "../api/types";
import { Chip, StatusChip } from "../components/Status";
import { EmptyState, ErrorState } from "../components/States";
import { VirtualList } from "../components/VirtualList";
import { useDebounced } from "../lib/hooks";
import {
  absoluteTime,
  compactNumber,
  formatDuration,
  formatElo,
  plural,
  previewText,
  relativeTime,
} from "../lib/format";
import { isRunUnseen } from "../lib/notifications";
import { METRIC_HINTS, describeHarness, describeRunStatus } from "../lib/status";
import { fetchRuns, saveRunPatch, useRuns, useRunsSummaries } from "../store/runs";
import { useIdentity } from "../store/identity";
import "../styles/screens.css";

/** One page holds everything the API will give; the list virtualises. */
const PAGE_SIZE = 100;
const ROW_HEIGHT = 128;

const MODEL_LEVEL_LABELS = {
  low: "Low",
  med: "Med",
  high: "High",
  max: "Max",
} as const;

const LEVEL_HINT = "Overall model level recorded for this run.";
const CUSTOM_LEVEL_HINT =
  "Custom model settings. The approximate level summarizes the run; individual roles may differ.";
const ELAPSED_HINT =
  "Elapsed from the first recorded start to final finish, including pauses and gaps.";
const ELAPSED_MISSING_HINT = "Elapsed time was not recorded for this run.";

/** `status` is an exact lifecycle match on the API, so these are single states. */
const STATUS_FILTERS = [
  { value: "", label: "All" },
  { value: "running", label: "Running" },
  { value: "paused", label: "Paused" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
] as const;

const SORTS = [
  { value: "recent", label: "Most recent" },
  { value: "oldest", label: "Oldest first" },
  { value: "title", label: "Title A–Z" },
  { value: "calls", label: "Most calls used" },
] as const;

/**
 * Every run this machine has done.
 *
 * The headline of a row is the run's *title*. The previous list led with engine
 * ids like `run-20260602-064935` — a filename, not a research question — and
 * offered no way to search, sort or filter twenty-four of them. Filters live in
 * the URL, so a filtered list is a link you can send yourself.
 *
 * A row is scanned for what it *did* — rounds, the leading idea, calls used,
 * how recently it moved. Cost is not one of those things: these runs are
 * subscription CLI calls, nothing is billed per token, and a dollar column
 * would rank the list by a number that constrains nothing.
 */
export function RunsListPage() {
  const [params, setParams] = useSearchParams();
  const identity = useIdentity();
  const [searchText, setSearchText] = useState(() => params.get("q") ?? "");
  const search = useDebounced(searchText.trim(), 250);

  const status = params.get("status") ?? "";
  const sort = params.get("sort") ?? "recent";
  const showArchived = params.get("archived") === "1";
  const includeDemo = params.get("demo") === "1";
  const isAdmin = identity.data?.is_admin === true;
  const allUsers = isAdmin && params.get("scope") === "all";

  useEffect(() => {
    const current = params.get("q") ?? "";
    if (current === search) return;
    const next = new URLSearchParams(params);
    if (search) next.set("q", search);
    else next.delete("q");
    setParams(next, { replace: true });
  }, [search, params, setParams]);

  const query = useMemo<RunsQuery>(
    () => ({
      q: search || undefined,
      status: status || undefined,
      sort,
      show_archived: showArchived || undefined,
      include_demo: includeDemo || undefined,
      // Personal is the safe default for every identity. Administrators must
      // deliberately choose the cross-workspace view, which is explicit on the
      // wire as well as in the URL.
      mine: !allUsers,
      page_size: PAGE_SIZE,
    }),
    [search, status, sort, showArchived, includeDemo, allUsers],
  );

  const list = useRuns(query);
  const runs = useRunsSummaries();
  const filtered = Boolean(search || status || showArchived || includeDemo || allUsers);

  function setParam(key: string, value: string | null): void {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  }

  function clearFilters(): void {
    setSearchText("");
    setParams(new URLSearchParams(), { replace: true });
  }

  return (
    <>
      <header className="runs-head">
        <div>
          <span className="label">
            {allUsers
              ? "Administrative view"
              : isAdmin
                ? "Administrator workspace"
                : "Personal workspace"}
          </span>
          <h1>
            {allUsers
              ? "All users’ research runs"
              : isAdmin
                ? "My research runs"
                : "Your research runs"}
          </h1>
          {identity.data && !isAdmin ? (
            <p className="runs-head__context">
              Your current and historical research runs are private to your workspace.
            </p>
          ) : null}
        </div>
        <span className="spacer" />
        {list.status === "ready" ? (
          <span className="muted numeral" aria-live="polite">
            {list.total} {list.total === 1 ? "run" : "runs"}
          </span>
        ) : null}
        {/* The only other way into /how-it-works is step 5 of the wizard, which
            means the page written for someone who has not decided to launch yet
            was reachable only by someone who had. */}
        <Link className="btn btn--sm btn--ghost" to="/how-it-works">
          How it works
        </Link>
      </header>

      <div className="runs-toolbar">
        <div className="runs-search">
          <Search size={15} aria-hidden="true" />
          <input
            className="input focus-inset"
            type="search"
            value={searchText}
            placeholder="Search titles and questions"
            aria-label="Search runs"
            onChange={(event) => setSearchText(event.target.value)}
          />
          {searchText ? (
            <button
              type="button"
              className="btn btn--ghost btn--icon btn--sm"
              aria-label="Clear search"
              onClick={() => setSearchText("")}
            >
              <X size={14} aria-hidden="true" />
            </button>
          ) : null}
        </div>

        <div className="runs-filters">
          {isAdmin ? (
            <div className="row" role="group" aria-label="Run ownership">
              <button
                type="button"
                className="chip chip--button"
                aria-pressed={!allUsers}
                onClick={() => setParam("scope", null)}
              >
                My runs
              </button>
              <button
                type="button"
                className="chip chip--button"
                aria-pressed={allUsers}
                onClick={() => setParam("scope", "all")}
              >
                All users
              </button>
            </div>
          ) : null}

          {STATUS_FILTERS.map((option) => (
            <button
              key={option.value || "all"}
              type="button"
              className="chip chip--button"
              aria-pressed={status === option.value}
              onClick={() => setParam("status", option.value || null)}
            >
              {option.label}
            </button>
          ))}

          <span className="runs-filters__divider" aria-hidden="true" />

          <button
            type="button"
            className="chip chip--button"
            aria-pressed={includeDemo}
            title="Demo runs are practice runs — scripted end to end, they call no model at all"
            onClick={() => setParam("demo", includeDemo ? null : "1")}
          >
            Demo runs
          </button>
          <button
            type="button"
            className="chip chip--button"
            aria-pressed={showArchived}
            onClick={() => setParam("archived", showArchived ? null : "1")}
          >
            Archived
          </button>

          <label className="runs-sort">
            <span className="label">Sort</span>
            <select
              className="select"
              value={sort}
              aria-label="Sort runs"
              onChange={(event) => setParam("sort", event.target.value)}
            >
              {SORTS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {list.status === "ready" && list.error ? (
        <div className="notice" data-tone="caution" role="status">
          <span>Showing the last good list — {list.error}</span>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => void fetchRuns(query)}
          >
            Retry
          </button>
        </div>
      ) : null}

      {list.status === "loading" && runs.length === 0 ? (
        <RunsSkeleton />
      ) : list.status === "error" ? (
        <ErrorState
          title="Could not load your runs"
          message={list.error ?? "The backend did not answer."}
          onRetry={() => void fetchRuns(query)}
        />
      ) : runs.length === 0 ? (
        filtered ? (
          <EmptyState
            title="No runs match those filters"
            action={
              <button type="button" className="btn" onClick={clearFilters}>
                Clear filters
              </button>
            }
          >
            Nothing here fits the search and filters you have set.
          </EmptyState>
        ) : (
          <FirstUse />
        )
      ) : (
        <>
          <VirtualList
            items={runs}
            rowHeight={ROW_HEIGHT}
            label="Research runs"
            className="runs-list"
            rowKey={(run) => run.id}
            renderRow={(run) => <RunRow run={run} showOwner={allUsers} />}
          />
          {list.total > runs.length ? (
            <p className="faint runs-more">
              Showing the first {runs.length} of {list.total}. Narrow the list with search
              or a filter to see the rest.
            </p>
          ) : null}
        </>
      )}
    </>
  );
}

function RunRow({ run, showOwner }: { run: RunSummary; showOwner: boolean }) {
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(run.title);
  const [busy, setBusy] = useState(false);
  const status = describeRunStatus(run);
  const imported = run.source === "imported";
  const unseen = isRunUnseen(run);
  const top = run.top[0] ?? null;

  async function commit(): Promise<void> {
    const next = title.trim();
    if (!next || next === run.title) {
      setRenaming(false);
      setTitle(run.title);
      return;
    }
    setBusy(true);
    const saved = await saveRunPatch(run.id, { title: next });
    setBusy(false);
    if (saved) setRenaming(false);
    else setTitle(run.title);
  }

  async function toggleArchive(): Promise<void> {
    setBusy(true);
    await saveRunPatch(run.id, { archived: !run.archived });
    setBusy(false);
  }

  return (
    <article
      className="run-row"
      data-archived={run.archived ? "true" : undefined}
      data-show-owner={showOwner ? "true" : undefined}
    >
      <span
        className="run-row__unseen"
        data-unseen={unseen ? "true" : "false"}
        title={unseen ? "Changed since you last opened it" : undefined}
      >
        {unseen ? (
          <span className="visually-hidden">Changed since you last looked</span>
        ) : null}
      </span>

      <div className="run-row__main">
        {renaming ? (
          <div className="row run-row__rename">
            <input
              className="input focus-inset"
              value={title}
              autoFocus
              aria-label="Run title"
              disabled={busy}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void commit();
                if (event.key === "Escape") {
                  setRenaming(false);
                  setTitle(run.title);
                }
              }}
            />
            <button
              type="button"
              className="btn btn--sm btn--primary"
              disabled={busy}
              onClick={() => void commit()}
            >
              Save
            </button>
          </div>
        ) : (
          <h2 className="run-row__title">
            <Link className="run-row__link" to={`/runs/${run.id}`}>
              {run.title}
            </Link>
          </h2>
        )}
        <p className="run-row__question" title={run.question}>
          {questionPreview(run)}
        </p>
      </div>

      <div className="run-row__status">
        <StatusChip status={status} />
        <div className="run-row__tags">
          {run.harness !== "claude" ? (
            <Chip tone={describeHarness(run.harness).tone} className="chip--quiet">
              {describeHarness(run.harness).label}
            </Chip>
          ) : null}
          {run.archived ? <Chip className="chip--quiet">Archived</Chip> : null}
        </div>
      </div>

      <div className="run-row__mobile-meta faint">
        <span>Level {levelText(run)}</span>
        <span>Elapsed {elapsedText(run)}</span>
        <span>Calls {callsText(run)}</span>
      </div>

      {showOwner ? (
        <div className="run-row__cell run-row__cell--owner">
          <span className="label">Owner</span>
          <span className="truncate" title={run.owner_display_name ?? undefined}>
            {run.owner_display_name ?? "Unknown"}
          </span>
        </div>
      ) : null}

      <div className="run-row__cell run-row__cell--rounds">
        <span className="label">Rounds</span>
        <span className="numeral run-row__rounds">
          {run.round}
          {run.rounds_target > 0 ? (
            <span className="faint"> / {run.rounds_target}</span>
          ) : null}
        </span>
        <RoundBar round={run.round} target={run.rounds_target} tone={status.tone} />
      </div>

      <div className="run-row__cell run-row__cell--metadata">
        <span className="run-row__fact">
          <span className="label">Level</span>
          <span title={run.model_level_custom ? CUSTOM_LEVEL_HINT : LEVEL_HINT}>
            {levelText(run)}
          </span>
        </span>
        <span className="run-row__fact">
          <span className="label">Elapsed</span>
          <span
            className="numeral"
            title={run.elapsed_seconds == null ? ELAPSED_MISSING_HINT : ELAPSED_HINT}
          >
            {elapsedText(run)}
          </span>
        </span>
      </div>

      <div className="run-row__cell run-row__cell--top">
        <span className="label">Leading idea</span>
        {top ? (
          <>
            <span className="truncate" title={top.title}>
              {top.title}
            </span>
            <span className="numeral faint" title={METRIC_HINTS.elo}>
              Elo {formatElo(top.elo)}
            </span>
          </>
        ) : (
          <span className="faint">None yet</span>
        )}
      </div>

      <div
        className="run-row__cell run-row__cell--calls"
        title={
          imported ? "Model calls recorded by this historical run." : METRIC_HINTS.calls
        }
      >
        <span className="label">
          {imported ? "Calls" : run.budget_calls > 0 ? "Calls used / max" : "Calls used"}
        </span>
        {imported ? (
          <>
            <span className="muted numeral">
              {plural(run.calls_used, "call")} recorded
            </span>
            <span className="faint">historical</span>
          </>
        ) : (
          <>
            <span className="numeral">{callsText(run)}</span>
            <span className="faint numeral" title={METRIC_HINTS.tokens}>
              {run.tokens_total > 0
                ? `${compactNumber(run.tokens_total)} tokens`
                : "no tokens recorded"}
            </span>
          </>
        )}
      </div>

      <time className="run-row__when faint" title={absoluteTime(run.updated_at)}>
        {relativeTime(run.updated_at)}
      </time>

      <div className="run-row__actions">
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          aria-label={`Rename ${run.title}`}
          disabled={busy}
          onClick={() => setRenaming(true)}
        >
          <Pencil size={14} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          aria-label={run.archived ? `Unarchive ${run.title}` : `Archive ${run.title}`}
          disabled={busy}
          onClick={() => void toggleArchive()}
        >
          {run.archived ? (
            <ArchiveRestore size={14} aria-hidden="true" />
          ) : (
            <Archive size={14} aria-hidden="true" />
          )}
        </button>
      </div>
    </article>
  );
}

function levelText(run: RunSummary): string {
  const level = run.model_level ? MODEL_LEVEL_LABELS[run.model_level] : null;
  if (run.model_level_custom) return level ? `Custom · ~${level}` : "Custom";
  return level ?? "—";
}

function elapsedText(run: RunSummary): string {
  return run.elapsed_seconds == null ? "—" : formatDuration(run.elapsed_seconds * 1000);
}

function callsText(run: RunSummary): string {
  return run.budget_calls > 0
    ? `${run.calls_used} / ${run.budget_calls} max`
    : String(run.calls_used);
}

/**
 * Imported runs were titled by truncating their own question, so printing both
 * shows the same sentence twice. When the title is the head of the question,
 * the second line picks up where the title stopped.
 */
function questionPreview(run: RunSummary): string {
  const title = run.title.replace(/…$/, "").trim();
  if (title && run.question.startsWith(title)) {
    const rest = run.question.slice(title.length).trim();
    return rest ? `…${previewText(rest, 170)}` : "";
  }
  return previewText(run.question, 180);
}

function RoundBar({
  round,
  target,
  tone,
}: {
  round: number;
  target: number;
  tone: string;
}) {
  if (target <= 0) return null;
  const done = Math.max(0, Math.min(round, target));
  return (
    <span className="round-bar" data-tone={tone} aria-hidden="true">
      {Array.from({ length: Math.min(target, 12) }, (_, index) => (
        <span key={index} className="round-bar__pip" data-done={index < done} />
      ))}
    </span>
  );
}

function RunsSkeleton() {
  return (
    <div className="runs-list" aria-busy="true" aria-label="Loading runs">
      {Array.from({ length: 6 }, (_, index) => (
        <div
          className="skeleton skeleton--block"
          key={index}
          style={{ height: 76, marginBottom: "var(--space-2)" }}
        />
      ))}
    </div>
  );
}

function FirstUse() {
  return (
    <div className="first-use">
      <span className="label">Nothing here yet</span>
      <h2>An oracle that shows its work</h2>
      <p>
        Give it a research question and it runs a tournament of ideas: it writes
        hypotheses, reviews them, groups the duplicates, makes them argue head to head,
        evolves the winners, and writes you a report on what survived.
      </p>
      <p className="muted">
        A demo run does the whole loop with scripted answers — no model calls, no waiting
        — so you can see the shape of it before starting a real one.
      </p>
      <div className="first-use__actions">
        <Link className="btn btn--primary btn--lg" to="/new?runner=demo">
          Try a demo run
        </Link>
        <Link className="btn btn--lg" to="/new">
          Start a real run
        </Link>
        <Link className="btn btn--lg btn--ghost" to="/how-it-works">
          How it works
        </Link>
      </div>
    </div>
  );
}

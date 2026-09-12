import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Archive, ArchiveRestore, Pencil, Repeat, Trash2 } from "lucide-react";

import type { RunSummary } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Chip, StatusChip } from "../components/Status";
import { ErrorState, LoadingPage } from "../components/States";
import { useDelayedReveal, useNow } from "../lib/hooks";
import { absoluteTime, relativeTime } from "../lib/format";
import { describeHarness, describeRunStatus, isActiveLifecycle } from "../lib/status";
import {
  deleteRunById,
  refreshRunDetail,
  saveRunPatch,
  useRun,
  useRunEvents,
} from "../store/runs";
import { ActivityTab } from "./run/ActivityTab";
import { HypothesesTab } from "./run/HypothesesTab";
import { LiveTab } from "./run/LiveTab";
import { ReportTab } from "./run/ReportTab";
import { ResearchTab } from "./run/ResearchTab";
import { SettingsTab } from "./run/SettingsTab";
import "../styles/screens.css";

/** Stop gets twenty seconds to work before force stop is even on screen. */
const FORCE_STOP_REVEAL_MS = 20_000;

const TABS = [
  { id: "live", label: "Live" },
  { id: "research", label: "Research" },
  { id: "hypotheses", label: "Hypotheses" },
  { id: "report", label: "Report" },
  { id: "activity", label: "Activity" },
  { id: "settings", label: "Settings" },
] as const;

type TabId = (typeof TABS)[number]["id"];

/**
 * One run, as a workspace.
 *
 * The tab lives in the URL, so a reload, a bookmark or a link from a toast all
 * land where you meant — the previous UI kept the equivalent in component state
 * and lost it on every navigation.
 *
 * The event stream is opened by a child that only mounts for runs that can
 * still produce events. That keeps the "one EventSource per watched run" rule
 * without a conditional hook, and means finished and imported runs cost no
 * connection at all.
 */
export function RunDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();

  const entry = useRun(id);
  const run = entry.summary;
  const detail = entry.detail;

  const active = run
    ? isActiveLifecycle(run.lifecycle) && run.source !== "imported"
    : false;
  // A run that has just turned terminal still owes us its closing event, so the
  // stream outlives the lifecycle by the store's drain window.
  const live = active || entry.draining;
  const now = useNow(active);

  const [stopRequestedAt, setStopRequestedAt] = useState<number | null>(null);
  const forceStopRevealed = useDelayedReveal(stopRequestedAt, FORCE_STOP_REVEAL_MS);

  const requested = params.get("tab");
  const tab: TabId = TABS.find((candidate) => candidate.id === requested)?.id ?? "live";

  function selectTab(next: TabId): void {
    const updated = new URLSearchParams(params);
    if (next === "live") updated.delete("tab");
    else updated.set("tab", next);
    setParams(updated, { replace: true });
  }

  if (!run && entry.status === "error") {
    return (
      <ErrorState
        title="Could not open this run"
        message={entry.error ?? "The backend did not answer."}
        onRetry={() => void refreshRunDetail(id)}
      />
    );
  }

  if (!run) return <LoadingPage label="Loading run" />;

  const imported = run.source === "imported";

  return (
    <>
      {live ? <LiveStream runId={id} /> : null}

      <RunHeader run={run} onDeleted={() => navigate("/")} />

      <div className="ws-tabs" role="tablist" aria-label="Run workspace">
        {TABS.filter(
          (candidate) =>
            candidate.id !== "research" || detail?.config.workflow === "adaptive",
        ).map((candidate) => (
          <button
            key={candidate.id}
            type="button"
            className="ws-tab"
            role="tab"
            id={`tab-${candidate.id}`}
            aria-selected={tab === candidate.id}
            aria-controls={`panel-${candidate.id}`}
            onClick={() => selectTab(candidate.id)}
          >
            {candidate.id === "live" && imported ? "Summary" : candidate.label}{" "}
            {candidate.id === "hypotheses" && detail ? (
              <span className="ws-tab__count">{detail.leaderboard.length}</span>
            ) : null}
            {/* Pointed at lost work, not at `degraded_count` — that counts model
                substitutions and has never once fired in this engine's history. */}
            {candidate.id === "activity" &&
            (run.lost_steps > 0 || run.failed_calls > 0) ? (
              <span
                className="ws-tab__count"
                data-tone={run.lost_steps > 0 ? "danger" : "caution"}
              >
                !
              </span>
            ) : null}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "live" ? (
          <LiveTab
            run={run}
            detail={detail}
            events={entry.events}
            now={now}
            forceStopRevealed={forceStopRevealed}
            onStopRequested={() => setStopRequestedAt(Date.now())}
          />
        ) : tab === "research" ? (
          <ResearchTab detail={detail} />
        ) : tab === "hypotheses" ? (
          <HypothesesTab run={run} detail={detail} />
        ) : tab === "report" ? (
          <ReportTab run={run} detail={detail} />
        ) : tab === "activity" ? (
          <ActivityTab run={run} events={entry.events} />
        ) : (
          <SettingsTab run={run} detail={detail} />
        )}
      </div>
    </>
  );
}

/** Holds the live subscription open for as long as the run can still move. */
function LiveStream({ runId }: { runId: string }) {
  useRunEvents(runId);
  return null;
}

/** The question is usually a long prompt, so it opens clamped. */
function RunQuestion({ question }: { question: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = question.length > 260;

  return (
    <>
      <p className="ws-question" data-clamped={long && !expanded ? "true" : "false"}>
        {question}
      </p>
      {long ? (
        <button
          type="button"
          className="ws-question__more"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Show less" : "Show the whole question"}
        </button>
      ) : null}
    </>
  );
}

function RunHeader({ run, onDeleted }: { run: RunSummary; onDeleted: () => void }) {
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(run.title);
  const [busy, setBusy] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const status = describeRunStatus(run);
  const harness = describeHarness(run.harness);

  async function commitTitle(): Promise<void> {
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

  async function remove(): Promise<void> {
    setBusy(true);
    const deleted = await deleteRunById(run.id);
    setBusy(false);
    setConfirmingDelete(false);
    if (deleted) onDeleted();
  }

  return (
    <header className="ws-head">
      <div className="ws-head__top">
        <div style={{ minWidth: 0, flex: "1 1 420px" }}>
          <span className="ws-head__id mono">{run.engine_run_id}</span>
          {renaming ? (
            <div className="row" style={{ marginTop: "var(--space-2)" }}>
              <input
                className="input focus-inset"
                value={title}
                autoFocus
                aria-label="Run title"
                disabled={busy}
                onChange={(event) => setTitle(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void commitTitle();
                  if (event.key === "Escape") {
                    setRenaming(false);
                    setTitle(run.title);
                  }
                }}
              />
              <button
                type="button"
                className="btn btn--primary"
                disabled={busy}
                onClick={() => void commitTitle()}
              >
                Save
              </button>
            </div>
          ) : (
            <div className="ws-title">
              <h1>{run.title}</h1>
              <button
                type="button"
                className="btn btn--ghost btn--icon btn--sm"
                aria-label="Rename this run"
                onClick={() => setRenaming(true)}
              >
                <Pencil size={14} aria-hidden="true" />
              </button>
            </div>
          )}
        </div>

        <div className="ws-head__actions">
          {/*
            "from scratch" is load-bearing, not padding. A finished run offers
            two ways forward a few pixels apart — this one copies the question
            and settings into a new run that starts with no hypotheses, and "Run
            more rounds" in the control bar carries on with the pool and the
            ratings this one built. Picking wrong costs a full run of calls, and
            "Run again" alone does not say which is which.
          */}
          <Link
            className="btn"
            to={`/new?from=${encodeURIComponent(run.id)}`}
            title="Copies this run's question and settings into a new run. The new run starts with no hypotheses and no ratings."
          >
            <Repeat size={14} aria-hidden="true" /> Run again from scratch
          </Link>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void saveRunPatch(run.id, { archived: !run.archived })}
          >
            {run.archived ? (
              <>
                <ArchiveRestore size={14} aria-hidden="true" /> Unarchive
              </>
            ) : (
              <>
                <Archive size={14} aria-hidden="true" /> Archive
              </>
            )}
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--icon"
            aria-label="Delete this run"
            disabled={busy}
            onClick={() => setConfirmingDelete(true)}
          >
            <Trash2 size={14} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="ws-head__facts">
        <StatusChip status={status} />
        <Chip tone={harness.tone} className="chip--quiet">
          {harness.label}
        </Chip>
        {run.archived ? <Chip className="chip--quiet">Archived</Chip> : null}
        <span className="faint" title={absoluteTime(run.updated_at)}>
          Updated {relativeTime(run.updated_at)}
        </span>
        <span className="faint" title={absoluteTime(run.created_at)}>
          Started {relativeTime(run.created_at)}
        </span>
      </div>

      <RunQuestion question={run.question} />

      {confirmingDelete ? (
        <ConfirmDialog
          title="Delete this run?"
          confirmLabel="Delete it"
          busy={busy}
          onConfirm={() => void remove()}
          onCancel={() => setConfirmingDelete(false)}
        >
          <p>
            <strong>{run.title}</strong> disappears from the list along with its
            hypotheses, reviews and report.
          </p>
        </ConfirmDialog>
      ) : null}
    </header>
  );
}

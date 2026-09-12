import { useState } from "react";
import { Copy, Download, FileSpreadsheet } from "lucide-react";
import { Link } from "react-router-dom";

import { getArtifactUrl, getExportUrl } from "../../api/client";
import type { RunConfig, RunDetail, RunSummary } from "../../api/types";
import { Markdown } from "../../components/Markdown";
import { ContinueDialog } from "../../components/run/ContinueDialog";
import { EmptyState, ErrorState, SkeletonText } from "../../components/States";
import { MAX_ROUNDS } from "../../lib/estimates";
import { plural } from "../../lib/format";
import { useOverview } from "../../lib/hooks";
import { isActiveLifecycle, isControlAllowed } from "../../lib/status";
import { pushToast } from "../../lib/toast";
import { sendRunControl } from "../../store/runs";

/**
 * The report, read as a document — and the guidance that shaped it.
 *
 * The overview is the thing the twelve imported runs were actually *for*: an
 * 11–21KB piece of prose their operator read once and could never open again
 * through the old GUI. It gets the prose register, a download and a copy.
 *
 * It is also where somebody decides the run is not finished with. Reading the
 * conclusions is the moment that thought arrives, and the control bar that can
 * act on it is two tabs away — so the same action gets a second entry point
 * here. Not a second implementation of it: the dialog is the one in
 * `components/run/ContinueDialog`, and it sends through the same
 * `sendRunControl` the control bar does.
 */
export function ReportTab({
  run,
  detail,
}: {
  run: RunSummary;
  detail: RunDetail | null;
}) {
  const [reloadKey, setReloadKey] = useState(0);
  const overview = useOverview(run.id, reloadKey, run.has_overview);
  const [copied, setCopied] = useState(false);

  async function copy(): Promise<void> {
    if (!overview.text) return;
    try {
      await navigator.clipboard.writeText(overview.text);
      setCopied(true);
      pushToast({ tone: "go", title: "Report copied" });
    } catch {
      pushToast({
        tone: "danger",
        title: "Could not copy",
        message: "Your browser refused clipboard access. Use Download instead.",
      });
    }
  }

  return (
    <div className="stack">
      <section className="panel">
        <header className="panel__head">
          <span className="label">Research overview</span>
          <span className="spacer" />
          <div className="report-actions">
            <a className="btn btn--sm" href={getExportUrl(run.id)} download>
              <Download size={14} aria-hidden="true" /> Download report
            </a>
            <a className="btn btn--sm" href={getExportUrl(run.id, 5)} download>
              <Download size={14} aria-hidden="true" /> Download top 5
            </a>
            <button
              type="button"
              className="btn btn--sm"
              disabled={!overview.text}
              onClick={() => void copy()}
            >
              <Copy size={14} aria-hidden="true" /> {copied ? "Copied" : "Copy"}
            </button>
            <a
              className="btn btn--sm"
              href={getArtifactUrl(run.id, "ideas_ranked.csv")}
              target="_blank"
              rel="noreferrer"
            >
              <FileSpreadsheet size={14} aria-hidden="true" /> Ranked ideas (.csv)
            </a>
          </div>
        </header>

        <div className="panel__body">
          {overview.status === "loading" ? (
            <SkeletonText lines={8} />
          ) : overview.status === "ready" && overview.text ? (
            <Markdown source={overview.text} />
          ) : overview.code === "overview_missing" || overview.code === "http_404" ? (
            <MissingReport run={run} />
          ) : (
            <ErrorState
              title="Could not load the report"
              message={overview.error ?? "The backend did not answer."}
              onRetry={() => setReloadKey((value) => value + 1)}
            />
          )}
        </div>
      </section>

      <MoreRounds run={run} detail={detail} />

      <section className="panel">
        <header className="panel__head">
          <span className="label">How the guidance moved</span>
        </header>
        <div className="panel__body">
          {detail == null ? (
            <SkeletonText lines={3} />
          ) : detail.feedback_history.length === 0 ? (
            <p className="tab-note">
              No round has finished yet, so the run has not written itself any guidance.
            </p>
          ) : (
            <>
              <p className="muted" style={{ marginBottom: "var(--space-5)" }}>
                At the end of each round the run summarises what is working and tells the
                next round what to do differently. This is that trail.
              </p>
              <div className="guidance">
                {detail.feedback_history.map((entry) => (
                  <div className="guidance__entry" key={entry.round}>
                    <div className="guidance__round">Round {entry.round}</div>
                    <div className="guidance__text">{entry.guidance}</div>
                  </div>
                ))}
              </div>
              {run.source === "imported" && detail.feedback_history.length === 1 ? (
                <p className="faint" style={{ marginTop: "var(--space-5)" }}>
                  Imported runs kept only their final guidance — the earlier rounds were
                  not preserved.
                </p>
              ) : null}
            </>
          )}
        </div>
      </section>
    </div>
  );
}

/**
 * "Run more rounds", where the thought actually occurs.
 *
 * The same single action as the control bar's, reached from the other end of
 * the workspace: you finish reading the conclusions, you want more of them, and
 * the button is under the last paragraph instead of two tabs back. It opens the
 * control bar's own `ContinueDialog` and sends through the same store call, so
 * there is one implementation of continuing a run and one set of words for it.
 *
 * The gate is `controlActionsFor`'s, via `isControlAllowed`: completed or
 * stopped, and never imported. That last clause is the important one — an
 * imported run has no model table for a supervisor to run against, so
 * continuing it would end as `failed` written over a historical result that
 * cannot be regenerated. The button is absent rather than disabled, which is
 * this workspace's rule for anything a run cannot do.
 *
 * Like the dialog it opens, this talks about ideas and rounds. Nothing here
 * frames the decision as money: these calls run on a subscription.
 */
function MoreRounds({ run, detail }: { run: RunSummary; detail: RunDetail | null }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<unknown>(null);

  const config = (detail?.config as Partial<RunConfig> | undefined) ?? null;
  const roundsCompleted =
    detail?.rounds.filter((round) => round.status === "completed").length ?? 0;

  // The dialog sizes its increment from the rounds completed and its budget
  // from the run's settings, so it waits for the detail exactly as the control
  // bar does rather than opening a box doing arithmetic on zeroes.
  if (
    !isControlAllowed(run.lifecycle, "continue", run.source) ||
    config == null ||
    roundsCompleted >= MAX_ROUNDS
  ) {
    return null;
  }

  async function send(
    addRounds: number,
    budgetCalls: number,
    removeCostCeiling: boolean,
  ): Promise<void> {
    setBusy(true);
    setRefusal(null);
    const outcome = await sendRunControl(
      run.id,
      "continue",
      {
        add_rounds: addRounds,
        budget_calls: budgetCalls,
        // Sent only to clear it: `null` asks the server to remove the ceiling,
        // and omitting the key is what says "do not touch it".
        ...(removeCostCeiling ? { budget_usd: null } : {}),
      },
      // Refused here, answered here — the dialog stays open carrying the reason,
      // rather than a toast landing over a box that closed.
      { handled: true },
    );
    setBusy(false);
    if (!outcome.ok) {
      setRefusal(outcome.error);
      return;
    }
    setOpen(false);
    // The control bar needs no confirmation because the thing it started is
    // directly above it and visibly starts moving. Here the run is two tabs
    // away and this panel is about to remove itself — the lifecycle it gates on
    // is no longer `completed` — so without a word the click reads as nothing
    // having happened.
    pushToast({
      tone: "go",
      title: `Running ${plural(addRounds, "more round")}`,
      message: "The Live tab shows it working.",
    });
  }

  return (
    <section className="panel">
      <header className="panel__head">
        <span className="label">Not finished with it?</span>
      </header>
      <div className="panel__body">
        <p className="muted" style={{ marginBottom: "var(--space-5)", maxWidth: "78ch" }}>
          This report is written from what the run had when it stopped. More rounds carry
          on from exactly there — every hypothesis, rating and review it produced stays,
          and the next round starts from the guidance the last one ended on. To ask the
          question afresh instead, use “Run again from scratch” at the top of the page.
        </p>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => {
            setRefusal(null);
            setOpen(true);
          }}
        >
          Run more rounds
        </button>
      </div>

      {open ? (
        <ContinueDialog
          run={run}
          config={config}
          roundsCompleted={roundsCompleted}
          maxRounds={MAX_ROUNDS}
          busy={busy}
          refusal={refusal}
          onConfirm={(addRounds, budgetCalls, removeCostCeiling) =>
            void send(addRounds, budgetCalls, removeCostCeiling)
          }
          onCancel={() => {
            setRefusal(null);
            setOpen(false);
          }}
        />
      ) : null}
    </section>
  );
}

/**
 * Why the report is missing, in the backend's own words.
 *
 * Two of the twenty-nine runs this app has executed ended `completed` with no
 * deliverable at all, and the only trace was a boolean buried in an event
 * payload. This tab said "No report was written / This run has no research
 * overview stored." — no cause, no next step, and not even the "See what went
 * wrong" link, because that branch keys on a `failed` lifecycle and these runs
 * were `completed`.
 */
const SKIPPED_REASON: Record<string, { title: string; body: string }> = {
  budget_usd: {
    title: "This run hit its cost ceiling before it could write the report",
    body: "Everything it produced is in the Hypotheses tab. Raise the ceiling and run more rounds to get a report.",
  },
  budget_calls: {
    title: "This run used every model call it had before it could write the report",
    body: "Everything it produced is in the Hypotheses tab. Raise the call budget and run more rounds to get a report.",
  },
  force_stopped: {
    title: "This run was force stopped before it could write a report",
    body: "The process was killed rather than asked to wind down, so nothing composed the overview. Everything it produced is in the Hypotheses tab.",
  },
  overview_call_failed: {
    title: "The report call did not come back with a usable answer",
    body: "The run finished everything else. The Activity tab has what the call reported.",
  },
};

function MissingReport({ run }: { run: RunSummary }) {
  const explained = run.overview_skipped_reason
    ? SKIPPED_REASON[run.overview_skipped_reason]
    : undefined;

  if (explained) {
    return (
      <EmptyState
        title={explained.title}
        action={
          <Link className="btn" to={`/runs/${run.id}?tab=activity`}>
            See what went wrong
          </Link>
        }
      >
        {explained.body}
      </EmptyState>
    );
  }

  if (run.lifecycle === "failed" || run.lifecycle === "lost") {
    return (
      <EmptyState
        title="This run ended before it could write a report"
        action={
          <Link className="btn" to={`/runs/${run.id}?tab=activity`}>
            See what went wrong
          </Link>
        }
      >
        The Activity tab has the error it stopped on, and everything it managed first.
      </EmptyState>
    );
  }

  if (isActiveLifecycle(run.lifecycle)) {
    return (
      <EmptyState title="No report yet">
        The report is written when the run finishes — and also when you stop it, from
        whatever exists at that point.
      </EmptyState>
    );
  }

  return (
    <EmptyState
      title="No report was written"
      action={
        <Link className="btn" to={`/runs/${run.id}?tab=activity`}>
          See what this run did
        </Link>
      }
    >
      This run has no research overview stored.
    </EmptyState>
  );
}

import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusChip } from "../components/Status";
import {
  describeConnection,
  describeHarness,
  describeRunStatus,
  isActiveLifecycle,
} from "../lib/status";
import type { Tone } from "../lib/status";
import { haltAllRuns, useActiveRuns, useConnection, useLanes } from "../store/runs";
import { useIdentity } from "../store/identity";
import { ModelsIndicator } from "./ModelsIndicator";
import { ThemeToggle } from "./ThemeToggle";

/**
 * The instrument's chrome. Everything here answers a question you should never
 * have to navigate for: is anything running, on which harness, which models it
 * is spending on, is the feed alive, and how do I stop all of it.
 */
export function TopBar() {
  const lanes = useLanes();
  const connection = useConnection();
  const activeRuns = useActiveRuns();
  const identity = useIdentity();
  const [confirmingHalt, setConfirmingHalt] = useState(false);
  const [halting, setHalting] = useState(false);

  const master = useMemo(
    () => masterTone(lanes, connection === "offline"),
    [lanes, connection],
  );

  async function confirmHalt(): Promise<void> {
    setHalting(true);
    try {
      await haltAllRuns();
      setConfirmingHalt(false);
    } catch {
      // haltAllRuns already raised a toast with the reason.
    } finally {
      setHalting(false);
    }
  }

  return (
    <>
      <header className="topbar">
        <Link className="wordmark" to="/">
          <span className="wordmark__mark" aria-hidden="true" />
          Oracle
          <span className="wordmark__sub">Research runs</span>
        </Link>

        <nav className="lanes" aria-label="Harness lanes">
          {lanes.map((lane) => {
            const harness = describeHarness(lane.harness);
            const status = lane.run ? describeRunStatus(lane.run) : null;
            return (
              <Link
                key={lane.harness}
                className="lane"
                to={lane.run ? `/runs/${lane.run.id}` : "/new"}
                data-active={lane.run ? "true" : "false"}
                data-tone={status?.tone ?? "neutral"}
                aria-label={
                  lane.run
                    ? `${harness.label} lane: ${status?.label}, ${lane.run.title}`
                    : `${harness.label} lane: idle`
                }
                title={status?.hint}
              >
                <span
                  className="status-mark"
                  data-mark={status?.mark ?? "queued"}
                  aria-hidden="true"
                />
                <span className="lane__name">{harness.label}</span>
                <span className="lane__title">{lane.run ? lane.run.title : "Idle"}</span>
              </Link>
            );
          })}
        </nav>

        <span className="spacer" />

        <ThemeToggle />

        <ModelsIndicator />

        <StatusChip status={describeConnection(connection)} />

        {identity.data ? <WorkspaceIdentity identity={identity.data} /> : null}

        {identity.data?.is_admin ? (
          <button
            type="button"
            className="btn btn--danger btn--sm"
            onClick={() => setConfirmingHalt(true)}
            disabled={activeRuns.length === 0}
            title={
              activeRuns.length === 0
                ? "Nothing is running"
                : "Stop every run that is still in flight"
            }
          >
            Stop all
          </button>
        ) : null}

        <Link className="btn btn--primary" to="/new">
          New run
        </Link>
      </header>

      <div className="topbar__hairline" data-tone={master.tone} data-live={master.live} />

      {confirmingHalt ? (
        <ConfirmDialog
          title={`Stop ${activeRuns.length} run${activeRuns.length === 1 ? "" : "s"}?`}
          confirmLabel="Stop them"
          busy={halting}
          onConfirm={() => void confirmHalt()}
          onCancel={() => setConfirmingHalt(false)}
        >
          <p>These runs will be asked to stop as soon as their in-flight calls return:</p>
          <ul style={{ marginTop: "var(--space-3)", paddingLeft: "var(--space-5)" }}>
            {activeRuns.map((run) => (
              <li key={run.id}>
                <strong>{run.title}</strong>{" "}
                <span className="faint">
                  — {describeHarness(run.harness).label},{" "}
                  {describeRunStatus(run).label.toLowerCase()}
                </span>
              </li>
            ))}
          </ul>
          <p style={{ marginTop: "var(--space-4)" }}>
            Each one still writes a report from the hypotheses it already has.
          </p>
        </ConfirmDialog>
      ) : null}
    </>
  );
}

function WorkspaceIdentity({
  identity,
}: {
  identity: {
    display_name: string | null;
    username: string;
    email: string | null;
    is_admin: boolean;
  };
}) {
  const name = identity.display_name?.trim() || identity.username;
  return (
    <div
      className="workspace-identity"
      aria-label={`Signed in as ${name}, ${identity.is_admin ? "administrator" : "personal workspace"}`}
      title={identity.email ?? `Signed in as ${identity.username}`}
    >
      <span className="workspace-identity__scope">
        {identity.is_admin ? "Administrator" : "Personal workspace"}
      </span>
      <span className="workspace-identity__name">{name}</span>
    </div>
  );
}

function masterTone(
  lanes: { run: { lifecycle: string } | null }[],
  offline: boolean,
): { tone: Tone; live: boolean } {
  if (offline) return { tone: "danger", live: true };
  const runs = lanes.map((lane) => lane.run).filter((run) => run != null);
  if (runs.some((run) => run.lifecycle === "running" || run.lifecycle === "finishing")) {
    return { tone: "go", live: true };
  }
  if (runs.some((run) => isActiveLifecycle(run.lifecycle))) {
    return { tone: "caution", live: true };
  }
  return { tone: "neutral", live: false };
}

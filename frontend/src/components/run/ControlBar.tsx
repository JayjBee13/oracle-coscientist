import { useState } from "react";

import type {
  ControlAction,
  ControlExtension,
  RunConfig,
  RunSummary,
} from "../../api/types";
import { MAX_ROUNDS } from "../../lib/estimates";
import { askAbout } from "../../lib/format";
import { controlActionsFor, describeControlAction } from "../../lib/status";
import { sendRunControl } from "../../store/runs";
import { ConfirmDialog } from "../ConfirmDialog";
import { ContinueDialog } from "./ContinueDialog";

/**
 * Pause, resume, finish, stop, and — once a run has ended — more rounds.
 * Enabled strictly by `controlActionsFor`, which is the plan's lifecycle table
 * and the only place that rule is written down.
 *
 * Three things the previous control bar got wrong are fixed here: only the
 * button you pressed says it is working (all six used to), anything that ends a
 * run asks first and names the run and how far it has got, and force stop stays
 * hidden until a polite stop has had twenty seconds to land — it kills the
 * process and loses whatever was in flight, so it is not a first resort.
 *
 * `continue` is the one action that needs an answer before it can be sent, so it
 * gets its own dialog rather than the shared confirmation. It is also the reason
 * this bar now renders on a finished run at all: everything else here is a way
 * of stopping something, and this is the way of asking for more.
 *
 * The confirmation names calls, not dollars. This is the highest-stakes moment
 * in the product — somebody deciding whether to kill a run — and a bolded
 * dollar figure here frames the stake as money the owner is not being charged.
 * What stopping actually reclaims is wall clock and plan-window headroom; what
 * it costs is the calls already made.
 */
export function ControlBar({
  run,
  config = null,
  roundsCompleted = 0,
  forceStopRevealed,
  onStopRequested,
}: {
  run: RunSummary;
  /** The run's stored settings, for the Continue dialog's budget estimate. */
  config?: Partial<RunConfig> | null;
  /** Rounds this run actually finished — what an increment counts from. */
  roundsCompleted?: number;
  forceStopRevealed: boolean;
  onStopRequested: () => void;
}) {
  const [pending, setPending] = useState<ControlAction | null>(null);
  const [confirming, setConfirming] = useState<ControlAction | null>(null);
  const [refusal, setRefusal] = useState<unknown>(null);

  const allowed = controlActionsFor(run.lifecycle, run.source);

  // Continue sizes its increment from the rounds the run completed and its
  // budget from the run's settings, and both of those arrive with the detail.
  // Offering the button before then would open a dialog doing arithmetic on
  // zeroes — so it appears when the numbers do, which on a run you have just
  // opened is the same paint.
  const canContinue = config != null && roundsCompleted < MAX_ROUNDS;
  const visible = allowed.filter(
    (action) =>
      (action !== "force_stop" || forceStopRevealed) &&
      (action !== "continue" || canContinue),
  );

  async function send(
    action: ControlAction,
    extension?: ControlExtension,
  ): Promise<void> {
    // A refused `continue` is answered inside its own dialog rather than by a
    // toast over a dialog that closed: every way it can be refused is something
    // the scientist can do something about without leaving the box they are in.
    const handled = action === "continue";
    setPending(action);
    setRefusal(null);
    const outcome = await sendRunControl(run.id, action, extension, { handled });
    setPending(null);
    if (!outcome.ok && handled) {
      setRefusal(outcome.error);
      return; // the dialog stays open, now carrying the reason
    }
    setConfirming(null);
    if (outcome.ok && (action === "stop" || action === "force_stop")) onStopRequested();
  }

  function press(action: ControlAction): void {
    setRefusal(null);
    if (describeControlAction(action).confirm) setConfirming(action);
    else void send(action);
  }

  if (visible.length === 0) return null;

  const dialog = confirming ? describeControlAction(confirming) : null;

  return (
    <>
      <div className="controls">
        {visible.map((action) => {
          const descriptor = describeControlAction(action);
          const busy = pending === action;
          return (
            <button
              key={action}
              type="button"
              className={
                action === "stop" || action === "force_stop"
                  ? "btn btn--danger"
                  : action === "resume" || action === "continue"
                    ? "btn btn--primary"
                    : "btn"
              }
              disabled={pending !== null}
              aria-busy={busy}
              onClick={() => press(action)}
            >
              {busy ? "Sending…" : descriptor.label}
            </button>
          );
        })}

        {allowed.includes("stop") && !forceStopRevealed ? (
          <span className="controls__hint">
            Stopping still writes a report from what exists.
          </span>
        ) : null}

        {visible.includes("continue") ? (
          <span className="controls__hint">
            Carries on with this run's ideas and ratings. To ask the question afresh
            instead, use “Run again from scratch” above.
          </span>
        ) : null}
      </div>

      {confirming === "continue" ? (
        <ContinueDialog
          run={run}
          config={config}
          roundsCompleted={roundsCompleted}
          maxRounds={MAX_ROUNDS}
          busy={pending !== null}
          refusal={refusal}
          onConfirm={(addRounds, budgetCalls, removeCostCeiling) =>
            void send("continue", {
              add_rounds: addRounds,
              budget_calls: budgetCalls,
              // Sent only to clear it. `null` asks the server to remove the
              // ceiling; leaving the key out is what says "do not touch it",
              // and the two are different requests, so `undefined` here is not
              // a value but an omission.
              ...(removeCostCeiling ? { budget_usd: null } : {}),
            })
          }
          onCancel={() => {
            setRefusal(null);
            setConfirming(null);
          }}
        />
      ) : confirming && dialog ? (
        <ConfirmDialog
          title={askAbout(dialog.label, run.title)}
          confirmLabel={dialog.label}
          tone={dialog.destructive ? "danger" : "accent"}
          busy={pending !== null}
          onConfirm={() => void send(confirming)}
          onCancel={() => setConfirming(null)}
        >
          <p>{dialog.confirm}</p>
          <p style={{ marginTop: "var(--space-3)" }}>
            It has used <strong>{run.calls_used}</strong>
            {run.budget_calls > 0 ? ` of ${run.budget_calls}` : ""} model calls.
          </p>
        </ConfirmDialog>
      ) : null}
    </>
  );
}

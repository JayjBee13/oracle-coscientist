import { useState } from "react";
import { Link } from "react-router-dom";

import { errorMessage, isApiError } from "../../api/client";
import type { RunConfig, RunSummary } from "../../api/types";
import { OVERVIEW_RESERVED_CALLS, suggestedBudgetCalls } from "../../lib/estimates";
import { askAbout, formatUsd } from "../../lib/format";
import { describeControlAction } from "../../lib/status";
import { ConfirmDialog } from "../ConfirmDialog";
import { NumberField } from "../NumberField";

/**
 * "Run more rounds" — the dialog that turns a finished run back on.
 *
 * Two numbers, and the reason both are here rather than one: the increment is
 * what the scientist is actually deciding, and the call ceiling is what decides
 * whether they get it. A run that finished at 48 of its 60 calls has twelve
 * left; asking it for two more rounds without raising that ceiling buys about a
 * third of one round and then a report. So the budget is pre-filled with what
 * the added rounds actually cost — the same `estimateCalls` arithmetic the
 * wizard's presets use — and it re-computes as the round count changes, right up
 * until the moment somebody types their own number, after which it is theirs.
 *
 * The rounds figure counts from what the run *completed*, not from the target it
 * was launched with, because the backend does too: a run stopped at round 2 of 5
 * and given one more runs round 3 and finishes. Showing "takes it to 3" while
 * the server means 6 would be a lie in the one place it costs money.
 *
 * Like the stop dialog beside it, this talks in model calls and never in
 * dollars. These are subscription calls; a bolded dollar figure would frame the
 * decision as money the owner is not being charged.
 *
 * The cost ceiling is the single exception, and it is here to be *removed*
 * rather than to be set. Runs launched before dollars stopped governing carry a
 * ceiling that gates on API-equivalent telemetry, and one of them can stop a run
 * mid-way with most of its call budget untouched — so on those runs, and only
 * those, a third control appears, ticked, offering to take the obsolete limit
 * off. A run launched since has no ceiling and never sees the field, because a
 * box about a constraint that does not exist is a box that invents one.
 */
export function ContinueDialog({
  run,
  config,
  roundsCompleted,
  maxRounds,
  busy,
  refusal = null,
  onConfirm,
  onCancel,
}: {
  run: RunSummary;
  /** The run's stored settings, for the cost estimate. Absent until detail loads. */
  config: Partial<RunConfig> | null;
  roundsCompleted: number;
  maxRounds: number;
  busy: boolean;
  /** Why the last attempt was refused, if it was. The dialog stays open on one. */
  refusal?: unknown;
  onConfirm: (addRounds: number, budgetCalls: number, removeCostCeiling: boolean) => void;
  onCancel: () => void;
}) {
  const headroom = Math.max(1, maxRounds - roundsCompleted);
  const [addRounds, setAddRounds] = useState(Math.min(2, headroom));
  const [budgetCalls, setBudgetCalls] = useState(() =>
    suggestBudget(run, config, Math.min(2, headroom)),
  );
  const [budgetEdited, setBudgetEdited] = useState(false);
  // Ticked from the start: no ceiling is the system default, removing this one
  // is what the run needs to get past where it stopped, and nothing about it is
  // spend the owner is authorising.
  const [removeCostCeiling, setRemoveCostCeiling] = useState(true);
  const costCeiling = ceilingOf(config);

  function commitRounds(next: number): void {
    setAddRounds(next);
    if (!budgetEdited) setBudgetCalls(suggestBudget(run, config, next));
  }

  // The engine holds two calls back at every step so a run can always write its
  // report, and the backend refuses a ceiling with no room for a step on top of
  // that. It is the field's floor rather than an error, because a number you
  // cannot type is kinder than one you type and are then told off for — but the
  // hint has to say why, or the snap-back on blur is just the box arguing.
  const floor = run.calls_used + OVERVIEW_RESERVED_CALLS + 1;
  const needed = estimateForRounds(config, addRounds);

  return (
    <ConfirmDialog
      title={askAbout("Run more rounds of", run.title)}
      confirmLabel="Run more rounds"
      tone="accent"
      busy={busy}
      onConfirm={() =>
        onConfirm(addRounds, budgetCalls, costCeiling != null && removeCostCeiling)
      }
      onCancel={onCancel}
    >
      <p>
        This run carries on from where it stopped. Every hypothesis, rating and review it
        already produced stays, and the next round starts from the guidance the last one
        ended on.
      </p>

      <div className="stack" style={{ marginTop: "var(--space-5)" }}>
        <NumberField
          label="Rounds to add"
          value={addRounds}
          min={1}
          max={headroom}
          onCommit={commitRounds}
          hint={`It has completed ${roundsCompleted}. This takes it to ${roundsCompleted + addRounds}.`}
        />

        <NumberField
          label="Call budget"
          value={budgetCalls}
          min={floor}
          max={10000}
          onCommit={(next) => {
            setBudgetEdited(true);
            setBudgetCalls(next);
          }}
          hint={budgetHint(run, needed, floor)}
        />

        {costCeiling != null ? (
          <label className="checkbox">
            <input
              type="checkbox"
              checked={removeCostCeiling}
              onChange={(event) => setRemoveCostCeiling(event.target.checked)}
            />
            <span>
              <span style={{ display: "block", color: "var(--text-strong)" }}>
                Remove the old cost ceiling
              </span>
              <span className="faint">{ceilingNote(run, costCeiling)}</span>
            </span>
          </label>
        ) : null}

        <Refusal error={refusal} />
      </div>
    </ConfirmDialog>
  );
}

/**
 * Why the server said no, in terms of the thing to do about it.
 *
 * Four refusals can reach this dialog and each wants different words. A busy
 * lane is not the scientist's mistake at all — some other run is using the
 * harness — so it names that run and offers to open it. A run that is no longer
 * finished has usually been continued from another tab already, so it lists what
 * is legal now instead of repeating the request. A budget with no headroom is a
 * correction to the field directly above, which is why it lands here rather than
 * in a toast over a dialog that closed.
 */
function Refusal({ error }: { error: unknown }) {
  if (error == null) return null;

  const code = isApiError(error) ? error.code : "";
  const details = (isApiError(error) ? error.details : null) as Record<
    string,
    unknown
  > | null;

  if (code === "lane_busy") {
    const holder = details?.conflicting_run_id;
    return (
      <span className="field__error">
        The {String(details?.harness ?? "claude")} harness is busy — one run at a time
        uses it.{" "}
        {typeof holder === "string" && holder ? (
          <>
            <Link to={`/runs/${encodeURIComponent(holder)}`}>
              Open the run holding it
            </Link>
            , then try again once it has finished.
          </>
        ) : (
          "Try again once the run holding it has finished."
        )}
      </span>
    );
  }

  if (code === "illegal_transition") {
    const allowed = Array.isArray(details?.allowed) ? (details.allowed as string[]) : [];
    // The scientist's words for those actions, never the wire values — and
    // `describeControlAction` degrades for an action this build has not met.
    const labels = allowed.map((action) => describeControlAction(action).label);
    return (
      <span className="field__error">
        This run is no longer finished, so it cannot be extended.{" "}
        {labels.length > 0
          ? `Available here now: ${labels.join(", ")}.`
          : "Reload to see where it got to."}
      </span>
    );
  }

  return <span className="field__error">{errorMessage(error)}</span>;
}

/**
 * The run's cost ceiling, or `null` when it has none — which since dollars
 * stopped governing runs is every run launched through this app.
 *
 * Read from the stored config, the same place the Settings tab reads it, so the
 * two screens cannot disagree about whether a run is capped.
 */
function ceilingOf(config: Partial<RunConfig> | null): number | null {
  const ceiling = config?.budget_usd;
  return ceiling != null && ceiling > 0 ? ceiling : null;
}

/**
 * The sentence beside the tick box, and the one piece of copy in this dialog
 * that has to say a dollar figure out loud.
 *
 * It says what the cap *is* and, when the run is past it, that the cap is why
 * the run is sitting here — but never that it is money. It is not: these calls
 * go through a subscription and the figure is what the same work would have cost
 * on the API. A run still inside its cap gets the milder sentence, because
 * telling somebody a limit stopped their run when it did not is how a dialog
 * stops being believed.
 */
function ceilingNote(run: RunSummary, ceiling: number): string {
  const cap = formatUsd(ceiling) ?? "—";
  const spent = formatUsd(run.spend_usd);
  const reached =
    spent != null && run.spend_usd >= ceiling
      ? `, and reaching it is what stopped the run — ${spent} recorded`
      : ", which runs launched now are not";
  return (
    `This run was capped at ${cap} of API-equivalent cost${reached}. That figure is ` +
    "telemetry, not a bill: these calls run on a subscription and are not charged per token."
  );
}

/**
 * What the rounds being added will cost, or `null` when there is nothing to
 * estimate from.
 *
 * `suggestedBudgetCalls` is the wizard's own preset arithmetic — the C3 step
 * count plus 25% headroom — so the figure quoted here is the one the same rounds
 * would have been budgeted at launch.
 */
function estimateForRounds(
  config: Partial<RunConfig> | null,
  addRounds: number,
): number | null {
  if (!config || config.generation_batch == null) return null;
  return suggestedBudgetCalls({
    rounds: addRounds,
    generation_batch: config.generation_batch,
    matches_per_round: config.matches_per_round ?? 0,
    evolve_top_k: config.evolve_top_k ?? 0,
    grounding_depth: config.grounding_depth,
  });
}

/** What the run has already spent, plus a fresh budget for the rounds being added. */
function suggestBudget(
  run: RunSummary,
  config: Partial<RunConfig> | null,
  addRounds: number,
): number {
  const added = estimateForRounds(config, addRounds);
  // Never below the ceiling the run already carries — the backend refuses a
  // budget that goes down, and a run whose existing ceiling already covers the
  // extra rounds needs no raise at all.
  if (added == null) {
    return Math.max(run.budget_calls, run.calls_used + OVERVIEW_RESERVED_CALLS + 1);
  }
  return Math.max(run.budget_calls, run.calls_used + added);
}

/**
 * The sentence under the budget box.
 *
 * It quotes what the added rounds are estimated to cost, which is not the same
 * number as the headroom the ceiling leaves — a run with a generous ceiling
 * already has room for the extra rounds, and saying "these rounds need 51 calls"
 * when they need about 27 is the kind of wrong that makes somebody raise a
 * budget they did not need to.
 */
function budgetHint(run: RunSummary, needed: number | null, floor: number): string {
  const spent = `It has used ${run.calls_used} of ${run.budget_calls}.`;
  const cost = needed == null ? "" : ` The rounds above need about ${needed} calls.`;
  return `${spent}${cost} The ceiling cannot go below ${floor} — two calls are always held back for the report.`;
}

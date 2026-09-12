import { useState } from "react";

import type { Workshop, WorkshopOption } from "../../api/types";
import { Chip } from "../../components/Status";
import { ErrorState } from "../../components/States";
import { describeGroundingDepth } from "../../lib/status";
import { Section } from "./parts";

/* --- Waiting --------------------------------------------------------------
   No SSE for workshops, so this polls. What it must not do is show a bare
   spinner: a scientist who has just typed their question wants to know that
   something is happening, what it is, and how long it has been going.
   ------------------------------------------------------------------------- */

const SLOW_AFTER_SECONDS = 45;

export function WorkshopWaiting({ elapsed }: { elapsed: number }) {
  return (
    <div className="card wiz-panel">
      <div className="waiting">
        <span className="status" data-tone="accent">
          <span className="status-mark" data-mark="running" aria-hidden="true" />
          Drafting two directions
        </span>
        <span className="waiting__elapsed">{formatElapsed(elapsed)}</span>
        <p className="muted" style={{ maxWidth: "56ch" }}>
          One model call is reading your question and writing two different research
          prompts for it — one that goes deep, one that goes wide. It usually takes under
          a minute.
        </p>
        {elapsed >= SLOW_AFTER_SECONDS ? (
          <p className="faint" style={{ maxWidth: "56ch" }}>
            This one is taking longer than usual. It will either arrive or report what
            went wrong — no research run has started.
          </p>
        ) : null}
      </div>
    </div>
  );
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}m ${String(rest).padStart(2, "0")}s` : `${rest}s`;
}

/* --- The pair ------------------------------------------------------------- */

export function OptionsStep({
  workshop,
  selectedId,
  refining,
  onSelect,
  onRefine,
  onContinue,
  onBack,
}: {
  workshop: Workshop;
  selectedId: string | null;
  refining: boolean;
  onSelect: (optionId: string) => void;
  onRefine: (base: string, note: string) => void;
  onContinue: (option: WorkshopOption) => void;
  onBack: () => void;
}) {
  const [note, setNote] = useState("");

  const live = workshop.options.filter((option) => !option.rejected);
  const rejected = workshop.options.filter((option) => option.rejected);
  const selected = live.find((option) => option.id === selectedId) ?? null;

  if (workshop.state === "failed") {
    return (
      <ErrorState
        title="The workshop could not draft options"
        message={
          workshop.error?.message ??
          "The model did not return a usable pair of prompts. No research run has started."
        }
        detail={workshop.error?.detail ?? workshop.question}
        onRetry={onBack}
        retryLabel="Back to the question"
      />
    );
  }

  return (
    <div className="stack" style={{ gap: "var(--space-5)" }}>
      <div className="opt-grid">
        {live.map((option, index) => (
          <OptionCard
            key={option.id}
            option={option}
            letter={index === 0 ? "A" : "B"}
            selected={option.id === selectedId}
            onSelect={() => onSelect(option.id)}
          />
        ))}
      </div>

      <div className="card wiz-panel">
        <Section title="Not quite right?">
          <div className="field">
            <label className="field__label" htmlFor="wizard-refine-note">
              What should change
            </label>
            <textarea
              id="wizard-refine-note"
              className="textarea"
              value={note}
              placeholder="Keep the mechanism focus but drop the cost constraint; I care about feasibility, not economics."
              onChange={(event) => setNote(event.target.value)}
            />
            <span className="field__hint">
              A note is optional for a merge, and worth writing for another pass.
            </span>
          </div>
          <div className="row-wrap" style={{ marginTop: "var(--space-4)" }}>
            <button
              type="button"
              className="btn"
              disabled={refining || live.length < 2}
              onClick={() => onRefine("merge", note)}
            >
              Merge both directions
            </button>
            <button
              type="button"
              className="btn"
              disabled={refining || !selected}
              onClick={() => selected && onRefine(selected.id, note)}
            >
              {selected
                ? `Another pass on ${selected.strategy}`
                : "Another pass — pick a direction first"}
            </button>
            {refining ? <span className="muted">Working…</span> : null}
          </div>
        </Section>
      </div>

      {rejected.length > 0 ? <RejectedHistory options={rejected} /> : null}

      <div className="wiz-actions" style={{ borderTop: 0, paddingTop: 0, marginTop: 0 }}>
        <button
          type="button"
          className="btn btn--primary btn--lg"
          disabled={!selected || refining}
          onClick={() => selected && onContinue(selected)}
        >
          {selected ? "Continue with this direction" : "Pick a direction to continue"}
        </button>
        <button type="button" className="btn btn--ghost" onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
}

/**
 * One direction.
 *
 * Selection is carried four ways at once — border, spine, background wash and a
 * badge whose *text* changes — because the UX review found the previous card
 * faded when chosen, so people read the selected option as the rejected one.
 */
function OptionCard({
  option,
  letter,
  selected,
  onSelect,
}: {
  option: WorkshopOption;
  letter: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const grounding = describeGroundingDepth(option.recommended_settings.grounding_depth);
  return (
    <button
      type="button"
      className="opt"
      data-selected={selected}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="opt__head">
        <span className="opt__strategy">
          <span className="label" style={{ display: "block" }}>
            Direction {letter}
          </span>
          {option.strategy}
        </span>
        <span className="spacer" />
        <span className="opt__badge">{selected ? "✓ Selected" : "Select"}</span>
      </span>

      <span className="opt__field">
        <span className="label">Optimises for</span>
        <span>{option.optimizes_for}</span>
      </span>
      <span className="opt__field">
        <span className="label">Deliberately leaves out</span>
        <span className="muted">{option.excludes}</span>
      </span>
      <span className="opt__field">
        <span className="label">Why</span>
        <span className="muted">{option.rationale}</span>
      </span>

      <span className="opt__field">
        <span className="label">Prompt</span>
        <span className="opt__prompt">{option.prompt}</span>
      </span>

      <span className="row-wrap" style={{ gap: "var(--space-2)" }}>
        <Chip tone="neutral" className="chip--quiet">
          {option.recommended_settings.rounds} rounds
        </Chip>
        <Chip tone="neutral" className="chip--quiet">
          {option.recommended_settings.matches_per_round} matches a round
        </Chip>
        <Chip tone={grounding.tone} className="chip--quiet" title={grounding.label}>
          {option.recommended_settings.grounding_depth === "deep"
            ? "Deep grounding"
            : option.recommended_settings.grounding_depth === "shallow"
              ? "Light grounding"
              : "Standard grounding"}
        </Chip>
      </span>
    </button>
  );
}

/** Every pair that was refined away, so a good idea can be recovered. */
function RejectedHistory({ options }: { options: WorkshopOption[] }) {
  const passes: WorkshopOption[][] = [];
  for (let index = 0; index < options.length; index += 2) {
    passes.push(options.slice(index, index + 2));
  }

  return (
    <details className="history">
      <summary>
        Earlier directions ({options.length} set aside over {passes.length} pass
        {passes.length === 1 ? "" : "es"})
      </summary>
      <div className="history__body">
        {passes.map((pass, index) => (
          <div key={index} className="stack" style={{ gap: "var(--space-2)" }}>
            <span className="label">Pass {index + 1}</span>
            {pass.map((option) => (
              <div key={option.id} className="history__item">
                <div style={{ color: "var(--text-strong)" }}>{option.strategy}</div>
                <div className="muted" style={{ fontSize: "var(--text-sm)" }}>
                  {option.optimizes_for}
                </div>
                {option.note ? (
                  <div
                    className="faint"
                    style={{ marginTop: "var(--space-2)", fontSize: "var(--text-sm)" }}
                  >
                    Your note: {option.note}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        ))}
      </div>
    </details>
  );
}

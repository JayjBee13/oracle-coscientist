import { Section } from "./parts";

/**
 * The prompt, editable.
 *
 * The previous UI rendered the final prompt read-only *and* collapsed its
 * newlines, so the one artefact a scientist most wants to adjust was the one
 * thing they could not touch. Here it is a mono textarea with the whitespace
 * intact, it saves to the draft on every keystroke, and the workshop's original
 * wording stays recoverable.
 */
export function PromptStep({
  prompt,
  source,
  busy,
  onChange,
  onRevert,
  onContinue,
  onBack,
}: {
  prompt: string;
  source: string;
  busy: boolean;
  onChange: (prompt: string) => void;
  onRevert: () => void;
  onContinue: () => void;
  onBack: () => void;
}) {
  const edited = Boolean(source) && prompt !== source;
  const empty = prompt.trim().length === 0;

  return (
    <div className="card wiz-panel">
      <Section
        title="Final prompt"
        aside={
          <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
            {prompt.length.toLocaleString()} characters
            {edited ? " · edited" : ""}
          </span>
        }
      >
        <div className="field">
          <label className="field__label" htmlFor="wizard-prompt">
            This is what every generation call will be given
          </label>
          <textarea
            id="wizard-prompt"
            className="textarea textarea--mono"
            style={{ minHeight: 320 }}
            value={prompt}
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
          />
          <span className="field__hint">
            Saved as you type — closing this tab will not lose it. Your edits win over the
            workshop's wording.
          </span>
        </div>
      </Section>

      <div className="wiz-actions">
        <button
          type="button"
          className="btn btn--primary btn--lg"
          disabled={empty || busy}
          onClick={onContinue}
        >
          {busy ? "Saving…" : "Continue to settings"}
        </button>
        <button type="button" className="btn btn--ghost" onClick={onBack} disabled={busy}>
          Back
        </button>
        <span className="spacer" />
        {edited ? (
          <button type="button" className="btn btn--ghost btn--sm" onClick={onRevert}>
            Revert to the workshop's wording
          </button>
        ) : null}
      </div>
    </div>
  );
}

import { useRef, useState } from "react";

import type { ContextDocInput } from "../../api/types";
import { Chip } from "../../components/Status";
import {
  ACCEPT_ATTRIBUTE,
  ACCEPTED_EXTENSIONS,
  MAX_DOCS,
  MAX_DOC_BYTES,
  docChars,
  formatBytes,
  readContextDocs,
} from "./contextDocs";
import { Section } from "./parts";

/** Below this the workshop has nothing to work with, and we say so rather than
 *  disabling a button for reasons the previous UI never explained. */
export const MIN_QUESTION_LENGTH = 10;

export function IntentStep({
  question,
  docs,
  busy,
  onQuestionChange,
  onDocsChange,
  onStartWorkshop,
  onWritePromptMyself,
}: {
  question: string;
  docs: ContextDocInput[];
  busy: boolean;
  onQuestionChange: (question: string) => void;
  onDocsChange: (docs: ContextDocInput[]) => void;
  onStartWorkshop: () => void;
  onWritePromptMyself: () => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  const [docErrors, setDocErrors] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);

  const trimmed = question.trim();
  const tooShort = trimmed.length < MIN_QUESTION_LENGTH;

  const accept = async (files: FileList | null): Promise<void> => {
    if (!files || files.length === 0) return;
    const result = await readContextDocs([...files], docs);
    setDocErrors(result.errors);
    if (result.docs.length > 0) onDocsChange([...docs, ...result.docs]);
  };

  return (
    <div className="card wiz-panel">
      <Section title="The question">
        <div className="field">
          <label className="field__label" htmlFor="wizard-question">
            What should Oracle investigate?
          </label>
          <textarea
            id="wizard-question"
            className="textarea"
            style={{ minHeight: 140, fontSize: "var(--text-md)" }}
            value={question}
            placeholder="How could ammonia be produced photocatalytically at ambient pressure?"
            onChange={(event) => onQuestionChange(event.target.value)}
          />
          <span className="field__hint">
            {tooShort
              ? `A sentence or two works best — ${MIN_QUESTION_LENGTH - trimmed.length} more character${
                  MIN_QUESTION_LENGTH - trimmed.length === 1 ? "" : "s"
                } before this can be worked on.`
              : "Two candidate research prompts get drafted from this. You choose between them, or merge them."}
          </span>
        </div>
      </Section>

      <Section
        title="Context documents"
        aside={
          <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
            optional · up to {MAX_DOCS}
          </span>
        }
      >
        <div
          className="dropzone"
          data-over={dragOver}
          onDragOver={(event) => {
            event.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragOver(false);
            void accept(event.dataTransfer.files);
          }}
        >
          <p style={{ maxWidth: "52ch" }}>
            Drop in anything the run should already know — a brief, a data dictionary,
            earlier findings. The text travels with every prompt.
          </p>
          <button
            type="button"
            className="btn"
            onClick={() => fileInput.current?.click()}
            disabled={docs.length >= MAX_DOCS}
          >
            Choose files
          </button>
          <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
            {ACCEPTED_EXTENSIONS.join(" · ")} · up to {formatBytes(MAX_DOC_BYTES)} each
          </span>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept={ACCEPT_ATTRIBUTE}
            className="visually-hidden"
            onChange={(event) => {
              void accept(event.target.files);
              event.target.value = "";
            }}
          />
        </div>

        {docs.length > 0 ? (
          <ul className="doc-list" style={{ listStyle: "none", padding: 0 }}>
            {docs.map((doc) => (
              <li key={doc.name} className="doc-row">
                <span className="doc-row__name truncate">{doc.name}</span>
                <Chip tone="neutral" className="chip--quiet">
                  {docChars(doc).toLocaleString()} chars
                </Chip>
                <span className="spacer" />
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  onClick={() =>
                    onDocsChange(docs.filter((one) => one.name !== doc.name))
                  }
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        {docErrors.length > 0 ? (
          <ul
            className="stack"
            style={{
              gap: "var(--space-1)",
              marginTop: "var(--space-3)",
              listStyle: "none",
              padding: 0,
            }}
            role="alert"
          >
            {docErrors.map((error) => (
              <li key={error} className="field__error">
                {error}
              </li>
            ))}
          </ul>
        ) : null}
      </Section>

      <div className="wiz-actions">
        <button
          type="button"
          className="btn btn--primary btn--lg"
          onClick={onStartWorkshop}
          disabled={tooShort || busy}
        >
          {busy ? "Drafting…" : "Draft two directions"}
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={onWritePromptMyself}
          disabled={tooShort || busy}
        >
          Write the prompt myself
        </button>
        <span className="spacer" />
        <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
          Nothing runs until you press Launch.
        </span>
      </div>
    </div>
  );
}

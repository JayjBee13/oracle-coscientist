/**
 * A numeric input that tolerates being mid-edit.
 *
 * The field keeps its own text so it can be *empty while you are typing in it*,
 * and only ever commits a parsed, clamped number — so nothing downstream can
 * hold NaN and no estimate flickers to nothing between two keystrokes.
 *
 * Shared rather than wizard-local: the launch wizard sets a run's numbers and
 * the Continue dialog raises two of the same ones afterwards, and they must
 * clamp, hint and behave identically or the second one is a different control
 * wearing the first one's clothes.
 */

import { useId, useState } from "react";

export function NumberField({
  label,
  hint,
  value,
  min,
  max,
  onCommit,
  onClear,
}: {
  label: string;
  hint?: string;
  /** `null` is a ceiling that is not set: the field shows empty, not a zero. */
  value: number | null;
  min: number;
  max: number;
  onCommit: (value: number) => void;
  /**
   * Supplying this makes the field clearable. Without it an empty box is
   * mid-edit and the previous number comes back on blur; with it, emptying the
   * box means the ceiling is unset — which is what the optional ceilings default
   * to and what their hints have always claimed you could do.
   */
  onClear?: () => void;
}) {
  const id = useId();
  const clearable = onClear != null;
  const [text, setText] = useState(value == null ? "" : String(value));
  const [seen, setSeen] = useState(value);

  // React's "adjust state when a prop changes" pattern, not an effect: when a
  // preset rewrites the config the field must show the new number, but while
  // *you* are typing the text is yours — including while it is empty.
  if (value !== seen) {
    setSeen(value);
    setText(value == null ? "" : String(value));
  }

  const handleChange = (next: string): void => {
    setText(next);
    if (next.trim() === "") {
      onClear?.();
      return; // otherwise mid-edit; nothing to commit yet
    }
    const parsed = Number(next);
    if (!Number.isFinite(parsed)) return;
    if (parsed < min || parsed > max) return;
    onCommit(parsed);
  };

  const handleBlur = (): void => {
    const parsed = Number(text);
    if (text.trim() === "" || !Number.isFinite(parsed)) {
      if (clearable && text.trim() === "") {
        onClear?.();
        setText("");
        return;
      }
      setText(value == null ? "" : String(value));
      return;
    }
    const clamped = Math.min(max, Math.max(min, parsed));
    onCommit(clamped);
    setText(String(clamped));
  };

  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="input tabular"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={text}
        onChange={(event) => handleChange(event.target.value)}
        onBlur={handleBlur}
      />
      {hint ? <span className="field__hint">{hint}</span> : null}
    </div>
  );
}

import { useState } from "react";

import type { GraftEvent, RunEvent, RunSummary } from "../../api/types";
import { absoluteTime, clockTime } from "../../lib/format";
import { errorText, eventBody } from "../../lib/eventText";
import { METRIC_HINTS, describeEventType } from "../../lib/status";
import { deriveForensics, formatPayload } from "../../lib/trust";
import { addRunNote } from "../../store/runs";
import { Chip } from "../Status";

/**
 * Steering. A note is not a chat message — it is read into the next round as
 * top-priority guidance, which the box says out loud so nobody expects a reply.
 */
export function NoteBox({ run }: { run: RunSummary }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) return;
    setBusy(true);
    const sent = await addRunNote(run.id, trimmed);
    setBusy(false);
    if (sent) setText("");
  }

  return (
    <div className="note-box">
      <label className="field">
        <span className="field__label">Steer the next round</span>
        <textarea
          className="textarea"
          value={text}
          disabled={busy}
          placeholder="e.g. Focus on mechanisms that could be tested in a wet lab within six months."
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      <div className="note-box__actions">
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy || text.trim().length === 0}
          onClick={() => void send()}
        >
          {busy ? "Sending…" : "Send to the run"}
        </button>
        <span className="faint">
          Applied at the next round boundary, above the model's own guidance.
        </span>
      </div>
    </div>
  );
}

/**
 * Diversity injection — never "graft" or "cartographer" on screen. Hidden
 * entirely when the feature is off, because a panel of zeros about a setting
 * you did not enable is noise.
 */
export function GraftPanel({ run, events }: { run: RunSummary; events: GraftEvent[] }) {
  if (!run.graft.enabled) return null;

  return (
    <section className="panel">
      <header className="panel__head">
        <span className="label">Diversity injection</span>
        <span className="spacer" />
        {run.graft.pending ? (
          <Chip tone="info">Seed waiting for the next round</Chip>
        ) : (
          <Chip>{run.graft.fired_count} fired</Chip>
        )}
      </header>
      <div className="panel__body stack">
        <p className="muted" style={{ fontSize: "var(--text-sm)" }}>
          {METRIC_HINTS.graft}
        </p>

        {events.length === 0 ? (
          <p className="tab-note">
            Watching. Nothing has converged tightly enough to need a fresh framing.
          </p>
        ) : (
          <ul
            className="stack"
            style={{ listStyle: "none", padding: 0, gap: "var(--space-3)" }}
          >
            {events.map((event, index) => (
              <li
                key={`${event.round}-${index}`}
                className="inset"
                style={{ padding: "var(--space-3)" }}
              >
                <div className="row-wrap">
                  <span className="label">Round {event.round}</span>
                  <Chip tone={event.fired ? "info" : "neutral"}>
                    {event.fired ? "Injected" : "Held back"}
                  </Chip>
                  <span className="faint" style={{ fontSize: "var(--text-xs)" }}>
                    {event.votes ?? 0} of 3 signals · {event.n_clusters} themes
                    {event.hhi != null ? ` · concentration ${event.hhi.toFixed(2)}` : ""}
                  </span>
                </div>
                {event.fired ? (
                  <p style={{ marginTop: "var(--space-2)" }}>
                    <strong>{event.source_domain ?? "Another field"}</strong>
                    {event.seed_framing ? ` — ${event.seed_framing}` : null}
                  </p>
                ) : (
                  <p className="muted" style={{ marginTop: "var(--space-2)" }}>
                    {event.abstained_reason ?? "Conditions were not met."}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

/**
 * Why a run failed, in one place.
 *
 * The previous UI rendered "error failed" beside an empty section, which is why
 * the plan requires a failed run to be diagnosable from this tab alone: the
 * error payload verbatim, any permission denials (which are a configuration
 * fault, not a research one), and every non-fatal problem the run worked
 * around on its way down.
 */
export function Forensics({
  run,
  events,
}: {
  run: RunSummary;
  events: readonly RunEvent[];
}) {
  const forensics = deriveForensics(events);
  const failed = run.lifecycle === "failed" || run.lifecycle === "lost";
  if (!failed && forensics.problems.length === 0) return null;

  return (
    <section className={failed ? "panel forensics" : "panel"}>
      <header className="panel__head">
        <span className="label">
          {failed ? "Why this run ended" : "Problems it worked around"}
        </span>
      </header>
      <div className="panel__body stack">
        {failed ? (
          <>
            <p>{errorText(forensics.error)}</p>
            {forensics.error && typeof forensics.error !== "string" ? (
              <pre className="forensics__block">{formatPayload(forensics.error)}</pre>
            ) : null}
          </>
        ) : null}

        {forensics.permissionDenials.length > 0 ? (
          <>
            <p>
              <strong>The model was denied a tool it had been given.</strong> That is a
              configuration fault rather than a research one — the run could not do the
              work it was asked to do.
            </p>
            {forensics.deniedCalls.length > 0 ? (
              <ul className="stack" style={{ listStyle: "none", padding: 0 }}>
                {forensics.deniedCalls.map((call) => (
                  <li key={call.seq}>
                    <span className="label">
                      {call.role ?? "unknown role"}
                      {call.round === null ? "" : ` · round ${call.round}`}
                    </span>
                    <pre className="forensics__block">{formatPayload(call.denials)}</pre>
                  </li>
                ))}
              </ul>
            ) : (
              <pre className="forensics__block">
                {formatPayload(forensics.permissionDenials)}
              </pre>
            )}
          </>
        ) : null}

        {forensics.problems.length > 0 ? (
          <div>
            <span className="label">Everything that went wrong</span>
            <ul style={{ listStyle: "none", padding: 0, marginTop: "var(--space-2)" }}>
              {forensics.problems.map((event) => (
                <li key={event.seq} className="row" style={{ alignItems: "baseline" }}>
                  <span className="event__time" title={absoluteTime(event.ts)}>
                    {clockTime(event.ts)}
                  </span>
                  <span
                    className="event__type"
                    data-tone={describeEventType(event.type).tone}
                  >
                    {describeEventType(event.type).label}
                  </span>
                  <span className="muted">{eventBody(event)}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </section>
  );
}

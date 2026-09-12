import { useEffect, useRef, useState } from "react";

import type { RunEvent } from "../../api/types";
import { absoluteTime, clockTime } from "../../lib/format";
import { EVENT_FAMILIES, eventBody, eventMatchesFamily } from "../../lib/eventText";
import type { EventFamilyId } from "../../lib/eventText";
import { describeEventType } from "../../lib/status";
import { MALFORMED_EVENT_TYPE } from "../../store/runs";

/** How close to the bottom still counts as "at the end". */
const FOLLOW_SLACK_PX = 40;

/**
 * The event log — the thing that makes a run watchable and a failure
 * diagnosable.
 *
 * Rules it exists to keep: every line is timestamped; a line the client could
 * not parse is shown raw with a badge rather than dropped, so a broken producer
 * is visible; and following the tail is abandoned the moment the reader scrolls
 * up, because a log that yanks itself away mid-sentence is worse than none.
 */
export function EventStream({
  events,
  live,
  filterable = false,
  tall = false,
  emptyMessage = "Nothing has happened yet.",
}: {
  events: readonly RunEvent[];
  live: boolean;
  filterable?: boolean;
  tall?: boolean;
  emptyMessage?: string;
}) {
  const [family, setFamily] = useState<EventFamilyId>("all");
  const [following, setFollowing] = useState(true);
  const logRef = useRef<HTMLDivElement>(null);

  const shown = filterable
    ? events.filter((event) => eventMatchesFamily(event.type, family))
    : events;

  useEffect(() => {
    const element = logRef.current;
    if (!element || !live || !following) return;
    element.scrollTop = element.scrollHeight;
  }, [shown.length, live, following]);

  function onScroll(): void {
    const element = logRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    setFollowing(distance < FOLLOW_SLACK_PX);
  }

  function jumpToEnd(): void {
    const element = logRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
    setFollowing(true);
  }

  return (
    <>
      {filterable ? (
        <div className="log-toolbar" role="group" aria-label="Filter events">
          {EVENT_FAMILIES.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className="chip chip--button"
              aria-pressed={family === entry.id}
              onClick={() => setFamily(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </div>
      ) : null}

      <div
        className={tall ? "event-log event-log--tall" : "event-log"}
        ref={logRef}
        onScroll={onScroll}
        role="log"
        aria-label="Run events"
      >
        {shown.length === 0 ? (
          <p className="tab-note">{emptyMessage}</p>
        ) : (
          shown.map((event) => (
            <EventRow key={`${event.seq}-${event.ts}`} event={event} />
          ))
        )}
      </div>

      {live && shown.length > 0 ? (
        <div className="log-follow">
          {following ? (
            <span>Following as events arrive.</span>
          ) : (
            <>
              <span>Paused while you read.</span>
              <button type="button" className="btn btn--sm" onClick={jumpToEnd}>
                Jump to latest
              </button>
            </>
          )}
        </div>
      ) : null}
    </>
  );
}

function EventRow({ event }: { event: RunEvent }) {
  const malformed = event.type === MALFORMED_EVENT_TYPE;
  const descriptor = describeEventType(event.type);
  const body = eventBody(event);

  if (malformed) {
    const raw = typeof event.payload?.raw === "string" ? event.payload.raw : "";
    return (
      <div className="event" data-malformed="true" data-tone="caution">
        <span className="event__time" title={absoluteTime(event.ts)}>
          {clockTime(event.ts)}
        </span>
        <span className="event__type">Unreadable line</span>
        <span className="event__body">
          <span className="chip" data-tone="caution">
            Could not be parsed
          </span>
          <pre className="event__raw">{raw}</pre>
        </span>
      </div>
    );
  }

  return (
    <div className="event" data-tone={descriptor.tone}>
      <span className="event__time" title={absoluteTime(event.ts)}>
        {clockTime(event.ts)}
      </span>
      <span className="event__type">{descriptor.label}</span>
      <span className="event__body">{body}</span>
    </div>
  );
}

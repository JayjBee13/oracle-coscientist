/**
 * Telling the scientist a run finished while they were somewhere else.
 *
 * Three mechanisms, deliberately layered from least to most intrusive:
 *   1. the document title (always — costs nothing, works with the tab closed),
 *   2. an unseen marker in the runs list (survives a full reload),
 *   3. a desktop notification, only if they opted in at the Confirm step.
 *
 * Everything here is defensive: jsdom has no `Notification`, private windows
 * throw on `localStorage`, and none of that is worth an error boundary.
 */

const SEEN_KEY = "coscientist.seen.v1";
const OPT_IN_KEY = "coscientist.notify.v1";

export const BASE_TITLE = "Oracle";

/* --- localStorage, without the exceptions --------------------------------- */

function readStore(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable — the feature degrades, the app does not */
  }
}

/* --- Unseen-since-last-visit ---------------------------------------------- */

type SeenMap = Record<string, string>;

function readSeen(): SeenMap {
  const raw = readStore(SEEN_KEY);
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as SeenMap;
    }
  } catch {
    /* corrupt entry — start over */
  }
  return {};
}

/** Records that the user has looked at this run in its current state. */
export function markRunSeen(runId: string, updatedAt: string): void {
  const seen = readSeen();
  if (seen[runId] === updatedAt) return;
  seen[runId] = updatedAt;
  writeStore(SEEN_KEY, JSON.stringify(seen));
}

/** True when the run has changed since the user last opened it. */
export function isRunUnseen(run: { id: string; updated_at: string }): boolean {
  const seen = readSeen();
  const last = seen[run.id];
  if (last === undefined) return false;
  return last !== run.updated_at;
}

/** Test helper. */
export function clearSeen(): void {
  writeStore(SEEN_KEY, "{}");
}

/* --- Desktop notifications ------------------------------------------------ */

export function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function notificationPermission(): NotificationPermission | "unsupported" {
  if (!notificationsSupported()) return "unsupported";
  return Notification.permission;
}

export function isNotificationOptIn(): boolean {
  return readStore(OPT_IN_KEY) === "true";
}

export function setNotificationOptIn(value: boolean): void {
  writeStore(OPT_IN_KEY, value ? "true" : "false");
}

/**
 * Asks the browser for permission and records the opt-in. Called from the
 * wizard's Confirm step, never on page load — an unprompted permission dialog
 * is the fastest way to get permanently denied.
 */
export async function requestNotificationPermission(): Promise<
  NotificationPermission | "unsupported"
> {
  if (!notificationsSupported()) return "unsupported";
  const permission =
    Notification.permission === "default"
      ? await Notification.requestPermission()
      : Notification.permission;
  setNotificationOptIn(permission === "granted");
  return permission;
}

export function notifyRunFinished(
  run: { id: string; title: string },
  statusLabel: string,
): void {
  if (!isNotificationOptIn()) return;
  if (notificationPermission() !== "granted") return;
  try {
    new Notification(`${statusLabel} — ${run.title}`, {
      body: BASE_TITLE,
      tag: `run-${run.id}`,
    });
  } catch {
    /* some browsers throw for non-persistent notifications; nothing to do */
  }
}

/* --- Document title ------------------------------------------------------- */

let titleRestoreInstalled = false;

/**
 * Puts an alert in front of the document title until the tab is focused again.
 * The alert is what someone sees in the taskbar when the run they walked away
 * from has finished.
 */
export function setTitleAlert(text: string | null): void {
  if (typeof document === "undefined") return;
  document.title = text ? `● ${text} — ${BASE_TITLE}` : BASE_TITLE;
  if (text) installTitleRestore();
}

function installTitleRestore(): void {
  if (titleRestoreInstalled || typeof window === "undefined") return;
  titleRestoreInstalled = true;
  const restore = (): void => {
    if (!document.hidden) setTitleAlert(null);
  };
  window.addEventListener("focus", restore);
  document.addEventListener("visibilitychange", restore);
}

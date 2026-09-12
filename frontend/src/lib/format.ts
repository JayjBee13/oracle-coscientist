/**
 * Numbers and times, formatted the way the instrument reads them.
 *
 * Two rules from the review live here rather than in the screens:
 *   - **Never render "$0.00".** A run whose per-call cost the CLI did not
 *     report has `spend_usd: null`, and a zero would read as "this was free"
 *     rather than "we do not know". `formatUsd` returns `null` for both, and
 *     every caller falls back to tokens.
 *   - **Missing is "—", not 0.** Imported runs have no per-match Elo; a zero
 *     there would be a claim about a tournament that never happened.
 */

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Parses an API timestamp. Returns NaN for anything unusable. */
export function parseTs(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  const value = Date.parse(iso);
  return Number.isNaN(value) ? Number.NaN : value;
}

/**
 * "just now" · "4 min ago" · "2 h ago" · "3 d ago" · "12 Jun".
 * Deliberately coarse: the exact second is in the `title` attribute wherever
 * this is used, and a list of runs is read by shape, not by clock.
 */
export function relativeTime(
  iso: string | null | undefined,
  now: number = Date.now(),
): string {
  const ts = parseTs(iso);
  if (Number.isNaN(ts)) return "unknown";
  const delta = now - ts;
  if (delta < 0) return "just now";
  if (delta < 45 * SECOND) return "just now";
  if (delta < HOUR) return `${Math.round(delta / MINUTE)} min ago`;
  if (delta < DAY) return `${Math.round(delta / HOUR)} h ago`;
  if (delta < 7 * DAY) return `${Math.round(delta / DAY)} d ago`;
  return new Date(ts).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** Full timestamp for a `title` attribute. */
export function absoluteTime(iso: string | null | undefined): string {
  const ts = parseTs(iso);
  if (Number.isNaN(ts)) return "Time not recorded";
  return new Date(ts).toLocaleString();
}

/** Clock only — the event log's left column. */
export function clockTime(iso: string | null | undefined): string {
  const ts = parseTs(iso);
  if (Number.isNaN(ts)) return "--:--:--";
  return new Date(ts).toLocaleTimeString(undefined, { hour12: false });
}

/** "8s" · "47s" · "4m 12s" · "1h 03m". Used for elapsed and for estimates. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const totalSeconds = Math.floor(ms / SECOND);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** Coarser than `formatDuration` — for "about 12 min left". */
export function formatRoughDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < MINUTE) return "under a minute";
  const minutes = Math.round(ms / MINUTE);
  if (minutes < 90) return `${minutes} min`;
  const hours = ms / HOUR;
  return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`;
}

/** 412000 → "412k". Token counts are read as magnitudes, never as digits. */
export function compactNumber(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs < 1000) return String(Math.round(value));
  if (abs < 1_000_000) {
    const thousands = value / 1000;
    return `${abs < 10_000 ? thousands.toFixed(1) : Math.round(thousands)}k`;
  }
  const millions = value / 1_000_000;
  return `${abs < 10_000_000 ? millions.toFixed(1) : Math.round(millions)}M`;
}

export function formatInteger(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return Math.round(value).toLocaleString();
}

/**
 * Money, or nothing at all. `null` means "no cost was reported" and the caller
 * must show something else — a zero here would be a lie about a run that spent
 * real money on a subscription the CLI does not price.
 */
export function formatUsd(value: number | null | undefined): string | null {
  if (value == null || !Number.isFinite(value) || value <= 0) return null;
  if (value < 0.01) return "<$0.01";
  return `$${value.toFixed(2)}`;
}

/** Elo, or an em dash. Imported runs have no per-match Elo to show. */
export function formatElo(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(Math.round(value));
}

export function formatPercent(fraction: number): string {
  if (!Number.isFinite(fraction)) return "—";
  return `${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`;
}

/** "1 call" / "12 calls" — the plural rule this app repeats most. */
export function plural(count: number, one: string, many = `${one}s`): string {
  return `${formatInteger(count)} ${count === 1 ? one : many}`;
}

/**
 * `Stop “Photocatalytic routes”?` — a confirmation heading that names its run.
 *
 * The trailing question mark is dropped when the run's own title already ends in
 * one, which on this product is the ordinary case rather than an edge: run
 * titles are derived from research questions, so a naive `"${title}"?` renders
 * `…falling nutrient loads?”?` on most of them.
 */
export function askAbout(action: string, title: string): string {
  const name = title.trim();
  const ends = /[?!.]$/.test(name);
  return `${action} “${name}”${ends ? "" : "?"}`;
}

/**
 * A markdown body with its opening heading removed when that heading is only
 * the title the screen has already printed.
 *
 * The engine writes `# <title>` as the first line of most hypothesis bodies —
 * `body_md` is a document in its own right, and a document starts with its
 * name. Every surface that shows one prints the title itself as well, because
 * the title is what identifies the idea before its body has loaded (or ever
 * arrives: an idea inserted from the event stream has a title and nothing
 * else). Printed twice the second copy comes at heading size, which in the
 * Ideas tab's 340px detail rail pushes the actual claim below the fold.
 *
 * Matched loosely on purpose: capitalisation and trailing punctuation are the
 * model's to vary. A heading that is *not* the title (`## Claim`) is the body's
 * own structure and stays.
 */
export function stripLeadingTitle(bodyMd: string, title: string): string {
  const opening = /^\s*#{1,3}[ \t]+(.+?)[ \t]*(?:\n|$)/.exec(bodyMd);
  if (!opening || !sameWords(opening[1], title)) return bodyMd;
  return bodyMd.slice(opening[0].length).replace(/^\s+/, "");
}

function sameWords(left: string, right: string): boolean {
  const key = (text: string): string =>
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  return key(left) === key(right);
}

/** First sentence or so of a question, for a list row. */
export function previewText(text: string | null | undefined, max = 160): string {
  if (!text) return "";
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `${flat.slice(0, max - 1).trimEnd()}…`;
}

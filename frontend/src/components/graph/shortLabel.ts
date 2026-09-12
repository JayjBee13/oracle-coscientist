/**
 * The words a circle carries on the idea graph.
 *
 * The canvas used to be labelled with hids — `h001`, `h002` — which is
 * unambiguous and says nothing. A reader scanning fifty-nine circles for the
 * one about lithium had to hover every one of them. So the label is now a short
 * phrase cut from the hypothesis title, and the hid moves to the tooltip and the
 * accessible name where it is still one hover or one focus away.
 *
 * Two rules make that safe rather than merely nicer.
 *
 * * **Pure and deterministic.** No model call, no clock, no randomness. The same
 *   title always yields the same phrase, so a screenshot of a run still matches
 *   the run a week later, and the layout — which is sized from the widest label
 *   a run can produce — cannot be surprised at paint time.
 * * **Degenerate input falls back to the hid.** An untitled idea (the live model
 *   fills `title` with the hid when `hypothesis_added` carried none) gets its hid
 *   back rather than a blank circle. A label is never empty.
 *
 * The cut is deliberately dumb: strip the wrapping a model likes to add, drop a
 * leading article, then take whole words until the budget runs out. Nothing here
 * tries to find "the important words" — a heuristic that is wrong one time in ten
 * is worse than a truncation that is honest every time, because the reader can
 * see a truncation and cannot see a bad guess.
 */

/**
 * Roughly how much a wrapped label may carry, as a *string*.
 *
 * This bounds the phrase, not the drawing: how wide the drawing can get is
 * `MAX_LINE_CHARS` below, which `wrapLabel` enforces on every line it returns.
 * Keeping the two separate is deliberate — the label string is also what tests
 * and fixtures read, and clipping it to the render width would throw away words
 * that a two-line wrap can perfectly well carry.
 */
export const LABEL_BUDGET = 26;

/** Four words is a phrase; five is a sentence fragment nobody scans. */
export const LABEL_WORDS = 4;

/** Past this a label is wrapped onto two lines rather than run wide. */
export const WRAP_OVER = 14;

/**
 * The widest line the canvas will ever be asked to draw, in characters.
 *
 * This is a *guarantee*, not an estimate: `wrapLabel` ellipsizes any line longer
 * than this, so the number holds for every input, including the ones that cannot
 * be wrapped at all. It used to be an assumption — "a wrapped 26-character label
 * draws at most ~18 characters" — and it was false, because a label with no
 * space in it is never split: `Electrochemicallymediated…` drew all 26 of its
 * characters, half again as wide as three spacing constants had been sized for,
 * and a node at the left edge of the canvas had its words clipped off it.
 * Truncating a line loses nothing a reader cannot get back: the full title is in
 * the tooltip and in the accessible name, one hover or one Tab away.
 */
export const MAX_LINE_CHARS = 18;

/**
 * What one character of a label measures on the canvas, in px.
 *
 * The condensed cut of the display face at 10px (`.ig-node__label` in
 * `styles/graph.css`) averages this. It is the conversion between the two
 * constants above and every px-denominated spacing constant that depends on
 * them — `LABEL_SIDE` and `BAND_SLOT` in `IdeaGraph`, `FORCE.labelReach` in
 * `lib/graphLayout` — all of which import it from here rather than repeating it,
 * because three independent copies of a number is how three constants drift out
 * of agreement with the function they claim to measure. Change the font size or
 * the stretch in `graph.css` and this is the one place to re-measure.
 */
export const LABEL_CHAR_PX = 5.6;

/** The widest a rendered line can be, in px. */
export const MAX_LINE_PX = MAX_LINE_CHARS * LABEL_CHAR_PX;

const ELLIPSIS = "…";

/** Dropped only in first position, and only these three. A verb is content. */
const ARTICLES = new Set(["a", "an", "the"]);

/**
 * Quotes and markdown emphasis a title arrives wrapped in.
 *
 * Titles really do come back as `**Plasmonic nitrogen fixation**` or
 * `"Ambient-pressure Haber variant"`; left alone, the asterisks eat two of the
 * twenty-six characters and the label reads as a typo.
 */
const WRAPPER = "[\\s\"'“”‘’*_`#]";
const LEADING = new RegExp(`^${WRAPPER}+`);
const TRAILING = new RegExp(`${WRAPPER}+$`);

/**
 * A hypothesis title cut down to the two-to-four words a circle can hold.
 *
 * Returns `hid` unchanged when the title is empty, whitespace only, or is the
 * hid itself — the three shapes where a derived label would be a worse
 * identifier than the one the run already gave the idea.
 */
export function shortLabel(title: string, hid: string): string {
  const cleaned = clean(title);
  if (cleaned.length === 0 || cleaned === hid) return hid;
  // A title of exactly `The` is the same degenerate case as an empty one: the
  // article is dropped only when something follows it, so left alone this would
  // label a circle `The`. The hid is less pretty and strictly more useful.
  if (ARTICLES.has(cleaned.toLowerCase())) return hid;

  const words = dropArticle(cleaned.split(" "));

  const taken: string[] = [];
  let width = 0;
  for (const word of words) {
    if (taken.length === LABEL_WORDS) break;
    const next = taken.length === 0 ? word.length : width + 1 + word.length;
    // The first word is always taken: a label is never empty, even when one
    // word already spends the whole budget.
    if (taken.length > 0 && next > LABEL_BUDGET) break;
    taken.push(word);
    width = next;
  }

  if (taken.length === 1) {
    // Nothing to break on, so the truncation is shown rather than hidden.
    return ellipsize(taken[0], LABEL_BUDGET);
  }
  return taken.join(" ");
}

/**
 * One label as the lines it is drawn on: one, or two broken at a word boundary,
 * each of them **at most `MAX_LINE_CHARS` long**.
 *
 * Two lines of thirteen characters occupy about half the width of one line of
 * twenty-six, and horizontal room is what a force layout is short of — the
 * vertical cost is one 11px line, which `LABEL_ROOM` in `IdeaGraph` already
 * pays for at both ends of the canvas. A label with no space in it is never
 * split: a hyphen mid-word reads as part of the idea's name.
 *
 * Which leaves the case that made the spacing constants a fiction: a label with
 * nothing to break on — `Photoelectrocatalytically`, a first word long enough
 * that no second word fits the budget behind it, which chemistry titles produce
 * routinely. There is no wrap that helps, so the line is cut to the width the
 * canvas was built for and the cut is shown. That is the *rendering* decision,
 * which is why it lives here rather than in `shortLabel`: the label string keeps
 * its words for the places that have room for them.
 */
export function wrapLabel(label: string): string[] {
  if (label.length <= WRAP_OVER) return [fit(label)];
  const words = label.split(" ");
  if (words.length < 2) return [fit(label)];

  // The break nearest the middle, so neither line is a stub. Balanced by
  // character count — which *is* rendered width under the fixed
  // `LABEL_CHAR_PX` estimate the whole layout is spaced against — rather than
  // by word index, because "Electrochemical nitride" splits evenly by index and
  // very unevenly by width.
  let at = 1;
  let best = Infinity;
  for (let i = 1; i < words.length; i += 1) {
    const head = words.slice(0, i).join(" ").length;
    const tail = label.length - head - 1;
    const gap = Math.abs(head - tail);
    if (gap < best) {
      best = gap;
      at = i;
    }
  }
  // Either line can still be a single over-long word — "Photoelectrocatalytic
  // ammonia" breaks cleanly and the head is still 21 characters — so both go
  // through the same cut.
  return [fit(words.slice(0, at).join(" ")), fit(words.slice(at).join(" "))];
}

/** One rendered line, cut to `MAX_LINE_CHARS` with the cut left visible. */
function fit(line: string): string {
  return ellipsize(line, MAX_LINE_CHARS);
}

/**
 * `text` in at most `max` characters, ellipsis included in the count.
 *
 * The surrogate check is not theoretical: titles arrive with a leading emoji,
 * and half of a surrogate pair renders as a replacement box — a truncation the
 * reader reads as a bug rather than as a truncation.
 */
function ellipsize(text: string, max: number): string {
  if (text.length <= max) return text;
  const cut = text.slice(0, max - 1);
  const safe = /[\uD800-\uDBFF]$/.test(cut) ? cut.slice(0, -1) : cut;
  return `${safe}${ELLIPSIS}`;
}

function clean(title: string): string {
  return (typeof title === "string" ? title : "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(LEADING, "")
    .replace(TRAILING, "");
}

function dropArticle(words: string[]): string[] {
  if (words.length < 2) return words;
  return ARTICLES.has(words[0].toLowerCase()) ? words.slice(1) : words;
}

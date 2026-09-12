# Co-Scientist design system — "Observatory"

The frontend is an instrument for watching and steering long, expensive research
runs. It is not a dashboard and not a chat app. Somebody launches a run, walks
away, comes back twenty minutes later and needs to know — in one glance, without
reading — whether the machine is working, what it has found, and what it cost.

Everything below exists to serve that. `src/styles/tokens.css` holds the values,
`src/styles/base.css` holds the primitives. **Screens compose these; they do not
introduce their own colours, spacings, radii or font stacks.** If something is
missing, add a token rather than a one-off.

---

## The idea

Mission control for research. Two registers, deliberately held apart:

- **The instrument** — chrome, controls, telemetry. Dense, rectilinear, set in a
  DIN-derived grotesque, numerals tabular. Quiet until something needs you.
- **The science** — hypotheses, reviews, the final report. Set in a reading
  serif at a comfortable measure. This is a document, not a data grid.

The app background is plotting paper (a 32px hairline grid) lit by a console
glow under the top bar. The 2px rule under the top bar is the master indicator:
it takes the tone of whatever the app is currently doing — cyan when a run is
live, amber when one is pausing or stopping, red on failure, inert grey when
idle. If you learn one thing about this UI, learn that rule.

---

## Type

| Role    | Token            | Stack                                               | Used for                                                        |
| ------- | ---------------- | --------------------------------------------------- | --------------------------------------------------------------- |
| Display | `--font-display` | Bahnschrift → Segoe UI Variable Display → system-ui | headings, buttons, chips, tabs, all numerals, `.label` eyebrows |
| UI      | `--font-ui`      | Segoe UI Variable Text → Segoe UI → system-ui       | body UI text, forms, table cells                                |
| Prose   | `--font-prose`   | Constantia → Cambria → Charter → Georgia            | hypothesis bodies, reviews, overview report (`.prose`)          |
| Mono    | `--font-mono`    | Cascadia Mono → Consolas → ui-monospace             | prompts, event payloads, ids, argv, raw text                    |

No web fonts, ever — this app runs offline against a local backend. Bahnschrift
ships with Windows 10+ and is a variable DIN: technical, condensable (we use
`font-stretch: 87.5%` for micro-labels), and unmistakably instrument-panel. The
serif for research text is the single most important typographic decision here;
it is what makes a hypothesis read like a claim instead of a row.

Scale (rem, 16px root): `--text-2xs` 11 · `xs` 12 · `sm` 13 · `base` 14 ·
`md` 16 · `lg` 18 · `xl` 22 · `2xl` 28 · `3xl` 36. App default is 14; prose is 16. Leading: `tight` 1.15 (headings) · `snug` 1.35 (dense) · `normal` 1.5 (UI) ·
`prose` 1.65.

Numbers that a person compares (Elo, calls, spend, tokens) use `.numeral` or
`.tabular` so columns line up and digits do not jitter as they tick.

---

## Colour

Dark-first. Light is a real theme, not an afterthought: it comes from
`prefers-color-scheme` and can be forced either way with `:root[data-theme]`
(the attribute wins in both directions).

Semantic tokens — **use these names, never a hex value**:

| Token                                             | Dark                              | Light                             | Meaning                                 |
| ------------------------------------------------- | --------------------------------- | --------------------------------- | --------------------------------------- |
| `--surface`                                       | `#0c1116`                         | `#f3f6f8`                         | page                                    |
| `--surface-raised`                                | `#121a22`                         | `#ffffff`                         | cards, top bar                          |
| `--surface-raised-2`                              | `#18222c`                         | `#fafbfc`                         | controls, nested cards                  |
| `--surface-overlay`                               | `#1b2632`                         | `#ffffff`                         | dialogs, toasts, popovers               |
| `--surface-inset`                                 | `#090d12`                         | `#eef1f4`                         | inputs, wells, log panes                |
| `--border` / `--border-strong` / `--border-faint` | `#223040` / `#35485c` / `#161f29` | `#d6dde5` / `#b3bfcb` / `#e6ebef` | separators, control edges, hairlines    |
| `--text` / `--text-strong`                        | `#e4ebf2` / `#f6f9fc`             | `#12191f` / `#060a0e`             | body, headings                          |
| `--muted` / `--faint`                             | `#95a6b8` / `#61748a`             | `#4f5f6e` / `#78868f`             | secondary copy, labels                  |
| `--accent`                                        | `#45cfe3`                         | `#0d7787`                         | brand, links, primary action, live      |
| `--go`                                            | `#5ac983`                         | `#14764a`                         | healthy, complete, winning              |
| `--caution`                                       | `#e9a93c`                         | `#8a5606`                         | paused, slow, degraded, budget pressure |
| `--danger`                                        | `#f2685e`                         | `#bf3327`                         | failed, destructive actions             |
| `--info`                                          | `#7fa7f5`                         | `#2f52c0`                         | neutral notices                         |

Each status colour also has a `--*-wash` at ~12–14% for chip fills. The five
hues sit at roughly 188° (accent), 145° (go), 38° (caution), 5° (danger) and
220° (info) — far enough apart to survive a bad monitor, but colour is never the
only signal (see below).

Setting `data-tone="go|caution|danger|info|accent|neutral"` on any element
exposes `--tone` and `--tone-wash`; `.chip`, `.status`, `.toast`, `.lane` and
`.topbar__hairline` all read them. `lib/status.ts` is the only thing that
decides which tone a value gets.

---

## Status has its own visual language

Colour-blind users, glanceable screenshots and small chips all need status
readable without hue. Every status mark is a _shape_, and exactly one of them
moves:

| Mark           | Shape                                                       | Tone    | Lifecycle values                   |
| -------------- | ----------------------------------------------------------- | ------- | ---------------------------------- |
| `queued`       | hollow ring                                                 | neutral | `queued`                           |
| `running`      | solid dot + pulsing halo (the only animation in the system) | go      | `running`                          |
| `transitional` | dashed ring, slowly rotating                                | caution | `pausing`, `stopping`, `finishing` |
| `paused`       | pause glyph (two bars)                                      | caution | `paused`                           |
| `completed`    | solid centre inside a thin ring (target reached)            | go      | `completed`                        |
| `stopped`      | hard square                                                 | neutral | `stopped`                          |
| `failed`       | cross                                                       | danger  | `failed`                           |
| `lost`         | dashed hollow ring                                          | danger  | `lost`                             |

Imported runs are `completed` + neutral tone and read **"Completed · Imported"**
— one status vocabulary for the whole app, no second set of words for history.
Under `prefers-reduced-motion` the pulse and the rotation stop and the marks
stay distinguishable by shape alone.

---

## Space, shape, depth

Strict 4px grid: `--space-1` 4 → `--space-12` 96. Nothing uses an off-grid gap.

Radii are tight because the thing is an instrument: `xs` 2 · `sm` 4 · `md` 6
(controls, inputs) · `lg` 10 (cards) · `xl` 14 (dialogs) · `pill` (chips only).
Cards are rectangles; only the things that "float" or "toggle" are round.

Depth is mostly border + a hairline top sheen, with three shadow steps
(`--elev-1..3`) for card, popover and dialog. Dark UIs get shadow wrong by
default — prefer a border and a lighter surface over a bigger blur.

Layout: `--topbar-h` 52, `--content-max` 1440, `--gutter` 24 (16 under 900px).
Breakpoints are 480 (minimum supported), 900 (mid) and 1280 (wide). Wide content
— tables, timelines, Elo curves — scrolls inside a `.scroll-x` box. The page
body never scrolls sideways.

---

## Motion

Four durations (`--dur-1` 90ms → `--dur-4` 400ms) and two easings
(`--ease-out` for entrances and state changes, `--ease-in-out` for loops).
Motion is used for three things only: the running pulse, entrances of things
that appear unbidden (toasts, dialogs), and skeleton shimmer. Numbers changing
in place do not animate — a value that slides is a value you cannot read.

`prefers-reduced-motion: reduce` collapses every duration to 1ms and disables
the pulse, rotation and shimmer globally.

---

## Rules the foundation enforces

These come out of the review of the previous UI; the base layer is built so
that violating them takes effort:

1. **No raw enum ever reaches the screen.** `lib/status.ts` maps every enum in
   C4/C5 — lifecycle, event type, hypothesis status, match status, verdict,
   novelty, operator, workshop state, harness, role, grounding depth — to
   `{ label, tone }`. A test asserts totality. If you find yourself writing
   `{run.lifecycle}`, add the mapping instead.
2. **One status vocabulary.** Imported, demo and live runs use the same words.
3. **Never zeros beside an error.** Error and empty states (`.state`) replace
   content; they always name the problem and offer a retry.
4. **Focus is always visible** (`:focus-visible`, 2px accent ring, 2px offset).
   Never `outline: none` without a replacement.
5. **Effects are StrictMode-safe.** No `mountedRef` guards — use
   `AbortController` and cleanup-scoped flags. The old Research Notebook died on
   exactly this.
6. **Skeletons, not spinners,** for content that has a shape.
7. **Scientist language.** No "harness", "graft", "lane" or "not_tracked" on
   screen without a plain-English label and a tooltip.

---

## Primitives in `base.css`

`.card` `.panel` `.panel__head` `.panel__body` `.inset` · `.btn`
(`--primary` `--danger` `--ghost` `--sm` `--lg` `--icon`) · `.chip`
(`--quiet` `--button`) · `.status` + `.status-mark[data-mark]` · `.field`
`.input` `.select` `.textarea` (`--mono`) · `.table` · `.skeleton`
(`--text` `--title` `--block`) · `.state` (`--error`) · `.scrim` `.dialog` ·
`.toaster` `.toast` · `.topbar` `.topbar__hairline` `.wordmark` `.lanes`
`.lane` `.main` · helpers `.label` `.mono` `.numeral` `.tabular` `.muted`
`.faint` `.footnote` `.truncate` `.clamp-2` `.prose` `.stack` `.row` `.row-wrap`
`.scroll-x` `.scroll-y` `.visually-hidden`.

`.footnote` lives here rather than in `screens.css` because the launch wizard
needs the same register for the same reason: it is the only permitted home for a
dollar figure, and the wizard has one.

React components you should reuse rather than reinvent:
`<StatusLabel>` `<StatusChip>` `<Chip>` `<LabelChip>` (`components/Status.tsx`)
· `<ConfirmDialog>` (focus-trapped, Escape-closable) · `<ErrorBoundary>` ·
`<Toaster>` · `<PageHeader>` · `<EmptyState>` `<ErrorState>` `<SkeletonText>`
`<SkeletonBlock>` `<LoadingPage>` (`components/States.tsx`) · `<Markdown>`
(`components/Markdown.tsx`) · `<VirtualList>` (`components/VirtualList.tsx`) ·
`<NumberField>` (`components/NumberField.tsx`) — the numeric input that tolerates
being mid-edit and only ever commits a clamped number; every box in the app that
takes a run's numbers is this one.

Run-workspace pieces, reusable across its tabs (`components/run/`):
`<ProgressHeader>` `<TrustLine>` `<ControlBar>` `<ContinueDialog>`
`<Leaderboard>` `<EventStream>` `<NoteBox>` `<GraftPanel>` `<Forensics>`
`<HypothesisCard>` and the parts a hypothesis is made of — `<ReviewList>`
`<MatchTable>` `<EloCurve>` `<Lineage>`.

The Ideas tab's graph surface (`components/graph/`): `<IdeaGraph>` draws it,
`lib/graphLayout.ts` decides where every node sits (pure: `organicLayout`,
`layeredLayout`, `radiusFor`, `mulberry32`, `hashSeed`), and `graphModel.ts`
advances the graph one SSE event at a time (`applyEvent`, `needsRefetchFor`).
See `styles/graph.css` below.

---

## Rendering what the model wrote

`lib/markdown.ts` parses the engine's markdown into **data**, and
`<Markdown>` turns that data into React elements. No HTML string is ever built,
so a hypothesis body cannot inject markup, and a `javascript:` link renders as
text with its address visible. Raw `<script>` in the source comes out as
characters, which is the correct reading of untrusted model output.

Supporting derivations, all pure and unit-tested:
`lib/format.ts` (relative times, compact numbers, durations — and the rule that
`formatUsd` returns _null_ rather than "$0.00", so callers fall back to tokens),
`lib/eventText.ts` (an event payload as a sentence, plus the Activity filter
families and the current phase), `lib/trust.ts` (calls in flight, staleness
thresholds — amber at 90s, red at 7min — the ETA, and the failure forensics).
`lib/hooks.ts` holds `useNow` `useDebounced` `useDelayedReveal`
`useHypothesisDetail` `useOverview`.

---

## Screen layers on top of the base

Some screens carry enough of their own furniture to need a stylesheet, imported
by the page that owns it. They add classes; they never redefine base ones, and
every value is a token.

### `styles/screens.css` — the list, the workspace, the hypothesis (`/`, `/runs/:id`)

The runs list: `.runs-head` `.runs-toolbar` `.runs-search` `.runs-filters`
`.runs-sort` `.run-row` (+ `__unseen` `__title` `__link` `__question` `__status`
`__cell` `__when` `__actions`) `.round-bar` `.first-use`, and
`.virtual-list` / `__sizer` / `__row` for the windowing.

The workspace: `.ws-head` `.ws-tabs` `.ws-tab` `.ws-grid` · `.progress` `.ring`
`.round-track` / `.round-step` `.stat-strip` / `.stat` · `.trust` · `.controls` ·
`.lb` (`__scale` `__rows` `__row` `__bar` `__fill` `__elo`) · `.event-log` /
`.event` / `.log-toolbar` / `.log-follow` · `.forensics` · `.hyp` `.review`
`.elo-curve` `.lineage` `.guidance` · `.notice` (a caution that sits _beside_
good data) and `.kv` (a definition grid) are general enough to reuse. The
toneless aside a dollar figure lives in, `.footnote`, is a base primitive.

**The rules this layer exists for.** The row headline is the run's _title_ — the
old list led with `run-20260602-064935`, which is a filename. Leaderboard rows
are positioned by rank (`transform: translateY`) rather than reordered in the
DOM, so a re-rank slides instead of teleporting; it is the only animation here
that carries information, and every bar is drawn against one scale printed at
both ends. `.trust` is the line that says the machine is alive: role, model,
elapsed, quiet-time, tokens, rough ETA. Anything a run cannot do is _absent_,
not disabled — imported runs have no `.ring`, no `.controls` and no note box.

**Money is not a governor here, and must never be drawn as one.** Role calls go
through the Claude CLI on a subscription: nothing is billed per token, so the
dollar figure the engine reports is API-equivalent telemetry, not spend. What
actually runs out is _calls_, _wall-clock time_ and the plan window. So `.ring`
counts calls, `.stat-strip` leads with calls, elapsed and tokens, and the runs
list has a calls column rather than a cost one. A dollar figure appears only as
a `.footnote` — never a ring, never a ceiling, never a warning tone, and (per
`formatUsd`) never "$0.00". A run with no dollar ceiling set says so; it does
not print an empty gauge. Where there is no figure the screen says what is true
("not recorded", "no model calls") or omits the line.

**A demo run may not name a model anywhere.** It is scripted end to end and
invokes nothing, but the engine still stamps call events with the model a real
run of the same settings would have used. `lib/eventText.ts`'s
`withoutFalseModelClaims` strips that at the page boundary — the one place that
knows the run's harness — so the event log, `.trust` and the derived telemetry
all read "Demo · no model call" together, and Settings prints no resolved model
table at all.

### `styles/graph.css` — the idea genealogy canvas (Ideas tab)

Owned by `components/graph/IdeaGraph.tsx`, not by a page — the second stylesheet
imported by a component, for the same reason as `diagram.css`: one drawing serves
both of its views, and the legend swatches reuse the canvas's own node classes so
a mark in the legend cannot drift from the mark on the graph.

`.ig` (+ `__canvas` (`--empty`) `__stage` `__svg` `__plot` `__bands` `__edges`
`__nodes` `__labels` `__foot` `__hint`) · `.ig-edge` `.ig-arrow` (`--hot`) ·
`.ig-node` (+ `__disc` `__hid` `__ring` `__strike` `__focus`) · `.ig-label` ·
`.ig-band__rule` `.ig-band__label` · `.ig-legend` (+ `__item`,
`__item--cluster`, `__item--aside`, `__mark`, `__edge`).

**The encoding is a table, and it is the same in both views** — switching from
Organic to Layered must never re-teach the reader a symbol:

| Channel                              | Meaning                                            |
| ------------------------------------ | -------------------------------------------------- |
| Node size                            | Elo, scaled **within this run only** (`radiusFor`) |
| Green fill, heavier ring, outer ring | Current leader                                     |
| Faded, hollow, struck through        | Rejected in review                                 |
| Hollow, dashed                       | Merged away as a duplicate                         |
| Hollow, quiet                        | Set aside                                          |
| Filled, solid ring                   | In play                                            |
| Arrow, parent → child                | Descent; two converging arrows are a combination   |
| Subtle fill hue                      | Cluster                                            |
| Accent ring + accent arrows          | The selected idea and the descent touching it      |

**Two additions to the system, both deliberate.** `--cluster-1..6` are the only
new colours: the five semantic tones all mean something else, and a cluster
painted `--info` reads as a notice. They avoid green and red entirely, because on
this canvas green means _leading_ and red means _rejected_. And only the **six
largest** clusters are shaded — one imported run has 26 across 59 ideas, and six
recycled hues over 26 groups is a legend claiming a distinction the eye cannot
make; the rest are left unshaded and counted in the legend.

**The label says what the idea is; the hid is one hover away.** The canvas used
to be labelled `h001 … h059`, which is unambiguous and tells a reader nothing:
finding the idea about lithium meant hovering fifty-nine circles. So each node
carries a two-to-four word phrase cut from its title by `shortLabel` — pure,
deterministic, derived once in `graphModel` and never in a render, and falling
back to the hid for a title that is empty, is the hid, or is a bare article. The
full title is still nowhere near the canvas: it is in the SVG `<title>` (a
tooltip with no JavaScript), in the node's accessible name, and in the detail
rail. The **hid** is in the tooltip and the accessible name too, so the
identifier every other surface in the app uses is one hover or one Tab from any
circle.

The phrase is capped twice, and both caps are what the spacing constants are
sized against: at most 26 characters and four words as a string, and at most
`MAX_LINE_CHARS` = 18 characters per _rendered line_, wrapped onto at most two
lines and cut with a visible ellipsis when a single word cannot be broken.
`LABEL_CHAR_PX` = 5.6 converts that to px, and `LABEL_SIDE`, `BAND_SLOT` and
`FORCE.labelReach` are computed from those two constants rather than copied from
them — a label wider than the layout expects is one drawn off the edge of the
canvas or over its neighbour's.

**A label is never dimmed and never buried.** Every label is drawn in one
`.ig__labels` layer _after_ every disc, inside an `.ig-label` group that carries
the node's status so the encoding table still applies — inside its own node's
group a label is painted over by any node later in DOM order, which cost eight of
fifty-nine ideas their only on-canvas words on the largest run. A halo in the
canvas colour keeps a label readable where it crosses an arrow. And the fade that
marks a rejected or set-aside idea sits on the **disc**, never on the group:
10px semibold at 62% opacity measured 3.63:1 dark and 2.64:1 light against AA's
4.5:1, and most real runs draw more rejected ideas than surviving ones. The
strike and the hollow ring already carry the fate twice.

**The legend describes the run, not the component.** A status mark is rendered
only when a node on the canvas carries it, exactly as the cluster swatches always
were: "Leading" and "In play" beside the run that rejected all eighteen of its
ideas were claims about circles that are not there. "Set aside" is its own mark,
separate from "Merged away", because hollow and hollow-dashed are two fates.

**Rules this layer exists for.** The canvas is drawn at 1:1 inside `.ig__stage`,
which is never smaller than either the graph or the viewport — centre a canvas
directly in a scroll box and an SVG asked for less width than its `viewBox`
letterboxes itself, silently shrinking every label. The plot is inset by the
height of a label at top and bottom, so a node clamped to the floor still draws
its hid inside the canvas. The organic canvas is sized from the **run**, never
from the panel: a small run gets a small, centred canvas rather than a speck
adrift in a wall of grid (a run with one or two ideas narrows the canvas itself,
`data-scale="tiny"`), and a large one takes the area `AREA_PER_NODE` asks for and
scrolls, exactly as the layered canvas already did. Clamping it to the viewport
silently ignored that constant above about twenty ideas and drew the
fifty-nine-idea run as a knot. It also means a window resize cannot move a
settled node. Past 150 nodes the organic view hands over to layered and says so
on screen. In the layered view each band names the rounds inside it
(`.ig-band__label`), read off the placed nodes so the label cannot drift from the
band — a band holding both round 0 and round 1, which two imported runs really
do, says "Rounds 0–1" rather than picking one and contradicting the rail. The whole graph is a composite widget with **one
tab stop**: arrows move between ideas, up and down follow descent, Enter selects,
and the `.ig-node__focus` ring replaces the browser outline, which does not
follow a circle. A visually-hidden `<ul aria-label="Idea lineage">` carries every
idea, its parents, its operator and its fate — generated from the same nodes in
the same render, so there is no second copy to go stale. The simulation itself
never animates: it is solved synchronously and frozen, and the only motion is an
arriving idea fading up, which `prefers-reduced-motion` removes. Escape clears
the selection and leaves focus where it is, and the rail announces the change
through a polite live region, because selecting a circle rewrites a whole panel
and moves no focus. When a filter empties the canvas, `.ig__canvas--empty` says
what happened and the toggle beside it names what the next click does ("Show
rejected"), rather than leaving a blank grid under a pressed button.

### `styles/wizard.css` — the pre-flight panel (`/new`)

A numbered rail (`.wiz-rail`), one card of work at a time (`.wiz-panel`,
`.wiz-section`, `.wiz-actions`), and an estimate that never leaves the screen
once there are numbers (`.est-strip` / `.est` / `.est-strip__footnote`). Also
`.confirm-notes` (the engine's own escalations under the Confirm table),
`.dropzone` + `.doc-row`
(context documents), `.opt-grid` / `.opt` (workshop directions), `.history`
(rejected directions), `.preset-grid` / `.preset`, `.num-grid` + `.advanced`,
`.confirm-lines` / `.confirm-line`, `.reassurance`, `.checkbox`, `.waiting`, and
the per-role picker `.role-matrix` (+ `__head` `__tier` `__headline` `__lede`
`__dirty` `__table` `__step` `__role` `__note` `__select` `__grounded` `__reset`
`__notes` `__about` `__foot`).

**The estimate strip holds only what runs out.** Two tiles: model calls (the
governor — it is what the engine counts and stops on) and wall clock. There is
no cost tile; the API-equivalent range is the `.footnote` under the strip,
worded as the run's Settings tab words it. The Confirm step's caption names the
call ceiling and, if one is set, the wall-clock ceiling — never a dollar figure,
and never `budget_usd` interpolated without a null guard, because a cost ceiling
is off by default. The Advanced grid still offers `Cost ceiling (USD)`, empty,
and `NumberField`'s `onClear` is what lets it genuinely be emptied.

**The second rule this layer carries:** a run is a loop of model calls and
they are not the same job, so the tier is a _table_ rather than a setting — it
sits inside `.role-matrix`, fills one row per step, and any cell can then be moved
on its own. A row that has been moved says so three ways at once (accent spine,
accent select border, and a Reset button that appears), because colour alone
cannot carry it. Every value in the matrix — the rows, the models offered, the
effort vocabulary, the per-row reason and the escalations under it — comes from
`/api/capabilities`; nothing in the wizard names a model id, tier or effort of
its own.

**The rule this layer exists for:** a selected option is selected in four ways at
once — 2px accent border, accent spine, washed background, and a badge whose
_text_ changes from "Select" to "✓ Selected". The previous UI faded the chosen
card, and people read the selection as a rejection. Greyscale and colour-blind
readers must both be able to tell at a glance.

### `styles/compare.css` — the two-column comparison (`/compare/:a/:b`)

`.picker-grid` / `.picker__list` / `.picker__row` (the two run pickers),
`.cmp-banner` (shared-prompt state), `.verdict` (the headline),
`.delta-table` + `.delta__change` (a directional delta: arrow, number _and_
word), `.cmp-grid` / `.cmp-side__head` (the two runs, always in the same
left-right order) and `.rank` / `.rank__row` / `.rank__bar` / `.rank__fill`
(one run's within-run normalised ranked list).

**The rule this layer exists for:** Elo is only comparable inside one run, so
there is no merged leaderboard anywhere on the page. Each side's bars are
normalised against that side's own best and worst, both lists use the same
accent fill, and identity comes from column position and heading — never from a
second hue.

### `styles/diagram.css` — the workflow diagram (`/how-it-works`, Confirm step)

Owned by `components/WorkflowDiagram.tsx` rather than by a page, because two
screens draw it: the full variant on `/how-it-works`, the compact strip in the
wizard's Confirm step. It is the one stylesheet imported by a component.

`.wf` (+ `__phase` `__steps` `__step` `__node` `__card` `__head` `__title`
`__marks` `__model` `__what` `__flags` `__flag` `__icon` `__fan` `__fan-svg`
`__mark-line` `__mark-fill` `__note` `__aside` `__branch` `__return`
`__escalations` `__retired`) and, for the compact strip, `.wf__scroller`
`.wf__strip` `__tile` `__pip` `__tile-name` `__tile-model` `__tile-flags`
`__caption` `__legend`.

`.wf__scroller` is focusable (`tabIndex` + `role="group"` + a name), because a
scroll container with nothing focusable inside it cannot be reached by keyboard
at all. Below 1100px `.wf__strip` wraps instead of scrolling and the tile-to-tile
chevrons go — the tile that used to be cut off was the report, which is the one
reassurance the strip exists to give. `.wf__retired` is the line the diagram
shows when a run's stored tier is no longer published: it names the fallback
rather than drawing eight blank steps.

**The rules this layer exists for.** The diagram must read as a _loop with a
tail_: the seven round steps sit on one continuous spine that visibly turns back
on itself (a hand-written SVG U-turn), and the report is cut off from them by a
phase banner because it happens once. Everything is a border, a grid and two SVG
paths — no charting dependency, and colour comes from `currentColor` so light
and dark need no second set of values. The step that is _code_ rather than a
model call is dashed and sunken, so it is legible as a different kind of thing
before you read its label.

Two harder rules. **The diagram is an ordered list**: the boxes and glyphs are
decoration over `<ol>`/`<li>`, the compact tiles carry what they drop visually in
a `.visually-hidden` span, and there is no second copy of the content to go
stale. And **cost is not a dimension of it** — the axes are time and quality:
what a step is for, how hard it is thinking, and whether it happens all at once.

The step list, the order and the fan-out are structural facts about
`engine/orchestrator.py` and live in `WORKFLOW_STEPS`. Everything else — model,
effort, grounding, and the _reason_ a step is set that way — comes from the
`models` block of `GET /api/capabilities` via `store/capabilities.ts`. Nothing in
the component or its tests names a model, a tier or an effort.

---

## Estimating before spending: `lib/estimates.ts`

`estimateCalls` mirrors `engine/core.py` exactly (C3's step list), so a preset
budget is `estimate × 1.25` rather than a number somebody typed. `estimateUsd`
and `estimateMinutes` are planning heuristics and are labelled as estimates
wherever they appear; `USD_PER_CALL_ESTIMATE` is the single place a price per
call is written down, and `estimateUsd` falls back to the default tier rather
than throwing when a cloned run names one that has been retired. No preset sets
`budget_usd`: calls are the governor, and a cost ceiling is a thing the engine
accepts and nothing here suggests.

`estimates.test.ts` holds a parity fixture generated from the Python functions —
if the engine's formula moves and this file does not, that test fails first.

---

## The engine's role table: `lib/models.ts`

There is no client-side mirror of `engine/runners.py`. There was, and it went
stale the week the engine moved four roles to high effort — after which the
Confirm step printed a table contradicting the diagram three inches above it.
`GET /api/capabilities` publishes the table; `lib/models.ts` is the one place it
is folded, and four surfaces read it (the role matrix, the Confirm step, the
workflow diagram, `/how-it-works`):

- `resolveTier(models, tier)` picks the tier to describe and _reports_ when the
  one asked for is no longer published, instead of yielding an empty table;
- `runRoles(rows, { graft })` drops the roles a launched run never executes —
  the pre-run role the workshop calls before a run exists, and the collapse-only
  role while the injection that fires it is off;
- `applyOverrides(rows, config.model_overrides)` lays a run's own per-role
  choices over the tier's table, so every surface shows the same run;
- `modelLabel(id, catalog)` turns an id into the catalog's name for it, and
  shortens rather than substitutes when the catalog no longer lists it.

Nothing in the frontend types a model id, a tier name or an effort value.

---

## Reading data: `store/runs.ts`

No screen fetches for itself. Everything goes through the store, which owns the
cache, the single EventSource per run and the refetch policy.

**Hooks** — `useRuns(query)` (list state for a query; refetches when the query
changes, and joins a request for the same query rather than racing it) ·
`useRunsSummaries()` (the resolved summaries behind that list, so a rename or a
live patch reaches the list without a refetch) ·
`useRun(id)` (one run's summary + detail, fetched on demand) ·
`useRunEvents(id)` (opens and ref-counts the live stream; returns `events`,
`connection`, `lastSeq`) · `useLanes()` (top-bar harness lanes) ·
`useConnection()` (aggregate connection state) · `useActiveRuns()` (everything
still in flight).

**Actions** — `fetchRuns` · `refreshRunDetail` / `ensureRunDetail` ·
`refreshLanes` · `sendRunControl(id, action)` · `saveRunPatch(id, {title,
archived})` · `addRunNote(id, text)` · `archiveRunHypothesis(id, hid)` ·
`deleteRunById(id)` · `haltAllRuns()`. Each raises its own toast on failure and
returns a boolean, so callers handle the happy path only.

**Selectors** (non-React) — `getRunSummary` · `getRunEntry` · `getRunsList` ·
`getRunsListState` · `getLanes` · `getActiveRuns` · `getConnectionState`.

**Test support** — `resetRunStore()` (called for you in `test/setup.ts`) ·
`primeRuns(items)` to seed the cache without a request ·
`MockEventSource` in `test/mockEventSource.ts` (inject events, drop the
connection, read `.url` to assert the `after_seq` resume) · DTO builders in
`test/fixtures.ts` (runs) and `test/runFixtures.ts` (hypotheses, reviews,
matches, events, and `agoIso(seconds)` for the trust line's clock).

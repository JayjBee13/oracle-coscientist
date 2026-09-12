/**
 * The round circuit: ten stations, the wires between them, and what each one is.
 *
 * Data, not drawing. `CircuitBoard` renders every field below and decides
 * nothing; the inspector rail reads the same objects for its copy. Keeping the
 * geometry here rather than as literal SVG in the component is what lets the
 * board be one `map` per layer instead of four hundred lines of hand-placed
 * markup, and it is what lets a test ask "what does Generation emit" without
 * parsing a path.
 *
 * Two things this file deliberately does **not** contain:
 *
 * * **Any model, effort or tier.** A station names the engine `role` it runs as,
 *   and that is the whole of its claim about models. What that role resolves to
 *   is served by `/api/capabilities` and folded by `lib/models.ts` — restating a
 *   model here is how a page ends up disagreeing with the run it describes.
 * * **Any colour.** The five edge semantics are `kind` strings; `circuit.css`
 *   owns what cyan, blue, amber, red and green mean.
 *
 * The coordinate space is 1240×620, laid out once and scaled to fit; the board
 * crops its viewBox to the band these numbers actually occupy. They are a
 * layout, not a set of pixels.
 */

/** The ten stations, in reading order round the loop. */
export const STATION_IDS = [
  "question",
  "workshop",
  "generation",
  "reflection",
  "clustering",
  "tournament",
  "evolution",
  "cartographer",
  "metareview",
  "report",
] as const;

export type StationId = (typeof STATION_IDS)[number];

/** What a wire carries. The five meanings the legend names. */
export type EdgeKind =
  /** An idea itself, moving. */
  | "idea"
  /** A judgment or a signal about ideas — never an idea. */
  | "eval"
  /** Steering for the next round. Carries instructions, never a hypothesis. */
  | "guide"
  /** One of the two ways an idea dies. */
  | "death"
  /** Into the report. */
  | "exit";

/** A tone for a wire's label, matching the wire it belongs to. */
export type LabelTone = "muted" | "amber" | "red" | "green" | "blue";

export type EdgeLabel = { x: number; y: number; text: string; tone?: LabelTone };

export type CircuitEdge = {
  id: string;
  kind: EdgeKind;
  /** The stations this wire touches. Selecting either one lights it. */
  ends: StationId[];
  /** The SVG path, in viewBox units. */
  d: string;
  /** What is written along it. Highlighted and dimmed with the wire. */
  labels: EdgeLabel[];
};

export type StationText = {
  kind: "step" | "title" | "sub";
  x: number;
  y: number;
  text: string;
  /** Title text set down a size, for the two boxes too narrow for the full one. */
  tight?: boolean;
};

export type Station = {
  id: StationId;
  /**
   * The engine role this station runs as, or null when no model runs here.
   *
   * These are `api/types.ts`'s `Role` values, and they are the join between the
   * drawing and the live model table. `workshop` is null on purpose: it runs
   * before a run exists and is not part of a round's table (see `PRE_RUN_ROLES`).
   */
  role: string | null;
  /** Small boxes are the two that are not stations of the round proper. */
  small?: boolean;
  rect: { x: number; y: number; width: number; height: number };
  texts: StationText[];
  /** The rail's kicker: where this sits in the round. */
  caption: string;
  title: string;
  receives: string;
  emits: string;
  /** How it fans out, as chips. */
  fan: string[];
};

export const STATIONS: readonly Station[] = [
  {
    id: "question",
    role: null,
    small: true,
    rect: { x: 20, y: 138, width: 76, height: 44 },
    texts: [
      { kind: "title", x: 34, y: 158, text: "Your", tight: true },
      { kind: "title", x: 34, y: 172, text: "question", tight: true },
    ],
    caption: "input",
    title: "Your question",
    receives: "Whatever you type — a goal, a market, a mystery.",
    emits: "The raw research goal, plus any context documents you attach.",
    fan: [],
  },
  {
    id: "workshop",
    role: null,
    small: true,
    rect: { x: 140, y: 132, width: 112, height: 56 },
    texts: [
      { kind: "step", x: 152, y: 148, text: "PRE-RUN" },
      { kind: "title", x: 152, y: 164, text: "Workshop" },
      { kind: "sub", x: 152, y: 178, text: "sharpens the ask" },
    ],
    caption: "pre-run · optional",
    title: "Workshop",
    receives: "Your raw question.",
    emits:
      "Competing sharpened prompts — you pick one; wide vs deep framings change what the whole run explores.",
    fan: ["interactive"],
  },
  {
    id: "generation",
    role: "generation",
    rect: { x: 300, y: 112, width: 172, height: 76 },
    texts: [
      { kind: "step", x: 314, y: 130, text: "1 · FAN ×3 · WEB" },
      { kind: "title", x: 314, y: 148, text: "Generation" },
      { kind: "sub", x: 314, y: 163, text: "drafts new hypotheses," },
      { kind: "sub", x: 314, y: 176, text: "steered by all guidance" },
    ],
    caption: "station 1",
    title: "Generation",
    receives:
      "Sharpened goal · context docs · summaries of every living idea (so it never re-drafts one) · amber guidance from Meta-review · diversity directives from Cartographer.",
    emits:
      "A batch of 8 new hypotheses per round, drafted in 3 parallel shards, each grounded with live web search.",
    fan: ["×3 shards", "web search"],
  },
  {
    id: "reflection",
    role: "reflection",
    rect: { x: 544, y: 72, width: 172, height: 60 },
    texts: [
      { kind: "step", x: 558, y: 90, text: "2 · PER IDEA" },
      { kind: "title", x: 558, y: 108, text: "Reflection" },
      { kind: "sub", x: 558, y: 122, text: "accepts or rejects each" },
    ],
    caption: "station 2",
    title: "Reflection",
    receives:
      "Each new draft, one at a time — including Evolution’s offspring from last round.",
    emits:
      "A verdict per idea: accept (with a novelty grade) or reject. A reject is death — the idea is archived, visible in the genealogy as a struck-through node.",
    fan: ["parallel per idea"],
  },
  {
    id: "clustering",
    role: "proximity",
    rect: { x: 776, y: 72, width: 172, height: 60 },
    texts: [
      { kind: "step", x: 790, y: 90, text: "3 · +ARITHMETIC" },
      { kind: "title", x: 790, y: 108, text: "Clustering" },
      { kind: "sub", x: 790, y: 122, text: "groups themes, kills dupes" },
    ],
    caption: "station 3",
    title: "Clustering",
    receives: "One-line summaries of every active idea.",
    emits:
      "A theme label per idea, and merges for near-duplicates (the duplicate dies, its record kept). A collapse check — pure arithmetic, no model — raises the alarm if the field is converging on one theme.",
    fan: ["1 call + arithmetic"],
  },
  {
    id: "tournament",
    role: "ranking",
    rect: { x: 1018, y: 136, width: 172, height: 88 },
    texts: [
      { kind: "step", x: 1032, y: 154, text: "4 · 6 MATCHES" },
      { kind: "title", x: 1032, y: 172, text: "Tournament" },
      { kind: "sub", x: 1032, y: 187, text: "head-to-head debates;" },
      { kind: "sub", x: 1032, y: 200, text: "winners take Elo" },
    ],
    caption: "station 4",
    title: "Tournament",
    receives: "Pairs of active ideas, matched by current Elo so contests are close.",
    emits:
      "6 verdicts per round from head-to-head debate; winners take Elo from losers. Decisive matches get extra thinking effort. Rank here decides who breeds and who leads the report.",
    fan: ["6 matches/round"],
  },
  {
    id: "evolution",
    role: "evolution",
    rect: { x: 760, y: 300, width: 172, height: 72 },
    texts: [
      { kind: "step", x: 774, y: 318, text: "5 · BREEDS" },
      { kind: "title", x: 774, y: 336, text: "Evolution" },
      { kind: "sub", x: 774, y: 351, text: "mutates and grafts the" },
      { kind: "sub", x: 774, y: 364, text: "winners into offspring" },
    ],
    caption: "station 5",
    title: "Evolution",
    receives: "The top 3 by Elo, plus Meta-review’s breeding brief.",
    emits:
      "Offspring: sharpened variants, grounded extensions, or grafts that combine two or three parents into one new idea. Parent links are recorded — that’s the genealogy.",
    fan: ["top-3", "graft ×2–3 parents"],
  },
  {
    id: "cartographer",
    role: "cartographer",
    rect: { x: 208, y: 400, width: 150, height: 60 },
    texts: [
      { kind: "step", x: 222, y: 418, text: "WATCHDOG" },
      { kind: "title", x: 222, y: 436, text: "Cartographer" },
      { kind: "sub", x: 222, y: 450, text: "keeps the field varied" },
    ],
    caption: "watchdog",
    title: "Cartographer",
    receives: "The cluster map — how many themes, how crowded each one is.",
    emits:
      "Diversity directives for the next Generation: name the crowded ground to avoid and the empty ground to explore. Steering only; it never writes an idea itself.",
    fan: ["1 call/round"],
  },
  {
    id: "metareview",
    role: "meta_review",
    rect: { x: 474, y: 400, width: 226, height: 60 },
    texts: [
      { kind: "step", x: 488, y: 418, text: "6 · CLOSES THE ROUND" },
      { kind: "title", x: 488, y: 436, text: "Meta-review" },
      { kind: "sub", x: 488, y: 450, text: "turns results into next-round strategy" },
    ],
    caption: "station 6",
    title: "Meta-review",
    receives: "The whole round’s record: verdicts, match results, Elo moves, deaths.",
    emits:
      "Next-round strategy in amber: what patterns keep losing, what is missing from the field, what Evolution should try. This is why round 3 is smarter than round 1.",
    fan: ["1 call/round"],
  },
  {
    id: "report",
    role: "overview",
    rect: { x: 1082, y: 402, width: 140, height: 72 },
    texts: [
      { kind: "step", x: 1096, y: 420, text: "ONCE, AT END" },
      { kind: "title", x: 1096, y: 438, text: "Report" },
      { kind: "sub", x: 1096, y: 453, text: "written even if you" },
      { kind: "sub", x: 1096, y: 466, text: "stop the run early" },
    ],
    caption: "finale",
    title: "Report",
    receives:
      "The survivors ranked by Elo, the full genealogy, every death, and the run-health record (nothing lost is hidden).",
    emits:
      "The research overview — findings with the losing arguments on record. Written once at the end, and still written if you stop early.",
    fan: ["once"],
  },
];

export const EDGES: readonly CircuitEdge[] = [
  {
    id: "question-workshop",
    kind: "idea",
    ends: ["question", "workshop"],
    d: "M96 160 H136",
    labels: [],
  },
  {
    id: "workshop-generation",
    kind: "idea",
    ends: ["workshop", "generation"],
    d: "M252 160 H296",
    // Under the boxes rather than between them: the gap here is 48px and the
    // label is nearer 90, so on the wire it is painted over by Generation.
    labels: [{ x: 244, y: 206, text: "sharpened goal" }],
  },
  {
    id: "generation-reflection",
    kind: "idea",
    ends: ["generation", "reflection"],
    d: "M472 138 C 500 128, 512 124, 540 120",
    labels: [{ x: 458, y: 108, text: "new drafts ×8" }],
  },
  {
    id: "reflection-clustering",
    kind: "idea",
    ends: ["reflection", "clustering"],
    d: "M716 108 H772",
    labels: [{ x: 722, y: 100, text: "accepted", tone: "green" }],
  },
  {
    id: "clustering-tournament",
    kind: "idea",
    ends: ["clustering", "tournament"],
    d: "M948 120 C 980 128, 990 134, 1014 146",
    labels: [{ x: 952, y: 112, text: "actives, de-duped" }],
  },
  {
    id: "tournament-evolution",
    kind: "idea",
    ends: ["tournament", "evolution"],
    d: "M1064 220 C 1040 274, 980 306, 900 318",
    labels: [{ x: 985, y: 296, text: "top 3 by Elo" }],
  },
  {
    id: "evolution-reflection",
    kind: "idea",
    ends: ["evolution", "reflection"],
    d: "M756 300 C 700 240, 668 180, 636 138",
    labels: [
      { x: 596, y: 236, text: "offspring join" },
      { x: 596, y: 250, text: "next review" },
    ],
  },
  {
    id: "reflection-death",
    kind: "death",
    ends: ["reflection"],
    d: "M628 132 V 176",
    labels: [{ x: 638, y: 168, text: "rejected", tone: "red" }],
  },
  {
    id: "clustering-death",
    kind: "death",
    ends: ["clustering"],
    d: "M860 132 V 176",
    labels: [{ x: 870, y: 168, text: "merged as duplicate", tone: "red" }],
  },
  {
    id: "reflection-metareview",
    kind: "eval",
    ends: ["reflection", "metareview"],
    d: "M600 132 C 570 230, 550 330, 545 398",
    labels: [{ x: 516, y: 268, text: "verdicts", tone: "blue" }],
  },
  {
    id: "tournament-metareview",
    kind: "eval",
    ends: ["tournament", "metareview"],
    d: "M1080 224 C 1060 330, 900 396, 762 412",
    labels: [{ x: 940, y: 378, text: "match results, Elo moves", tone: "blue" }],
  },
  {
    id: "clustering-cartographer",
    kind: "eval",
    ends: ["clustering", "cartographer"],
    d: "M800 132 C 640 260, 420 330, 316 396",
    labels: [{ x: 470, y: 330, text: "cluster map", tone: "blue" }],
  },
  {
    id: "metareview-generation",
    kind: "guide",
    ends: ["metareview", "generation"],
    d: "M470 420 C 340 396, 340 260, 372 186",
    labels: [
      { x: 330, y: 252, text: "guidance: what is missing,", tone: "amber" },
      { x: 330, y: 266, text: "what to stop repeating", tone: "amber" },
    ],
  },
  {
    id: "metareview-evolution",
    kind: "guide",
    ends: ["metareview", "evolution"],
    d: "M700 398 C 740 386, 780 372, 812 356",
    labels: [{ x: 742, y: 392, text: "breeding brief", tone: "amber" }],
  },
  {
    id: "cartographer-generation",
    kind: "guide",
    ends: ["cartographer", "generation"],
    d: "M262 396 C 280 330, 320 240, 356 190",
    labels: [{ x: 240, y: 300, text: "diversity directives", tone: "amber" }],
  },
  {
    id: "tournament-report",
    kind: "exit",
    ends: ["tournament", "report"],
    d: "M1108 224 C 1140 300, 1150 360, 1152 398",
    labels: [{ x: 1128, y: 316, text: "final standings", tone: "green" }],
  },
  {
    id: "metareview-report",
    kind: "exit",
    ends: ["metareview", "report"],
    d: "M700 448 C 850 470, 980 470, 1078 462",
    labels: [
      { x: 850, y: 486, text: "round syntheses, lineage, run health", tone: "green" },
    ],
  },
];

/**
 * The one thing written on the board that is not a station or a wire.
 *
 * Both death edges point down into the same empty space, and without this the
 * reader is left to assume a rejected idea is deleted. It is not: it stays in
 * the genealogy, struck through, and the report counts it.
 */
export const GRAVEYARD_NOTE = {
  x: 600,
  y: 194,
  text: "✝ archived — losses stay on the record",
};

/** The five wire meanings, as the legend spells them. */
export const LEGEND: readonly { kind: EdgeKind; text: string }[] = [
  { kind: "idea", text: "ideas moving" },
  { kind: "eval", text: "judgments & signals" },
  { kind: "guide", text: "steering (never carries an idea)" },
  { kind: "death", text: "the two ways an idea dies" },
  { kind: "exit", text: "into the report" },
];

/**
 * The whole picture in one sentence, for a reader who cannot see it.
 *
 * The SVG's `aria-label`. It is written here beside the stations so that a wire
 * added above without a word added here is visible as an omission in one file.
 */
export const CIRCUIT_LABEL =
  "Flow diagram of one round: your question is sharpened in the Workshop and drafted into hypotheses by Generation; Reflection accepts or rejects each; Clustering groups the survivors and merges duplicates; the Tournament ranks them by head-to-head debate; Evolution breeds the top three and their offspring re-enter Reflection. Meta-review and Cartographer feed steering back into Generation and Evolution, and the Report is written once at the end from the final standings.";

export function stationById(id: StationId | null): Station | null {
  if (!id) return null;
  return STATIONS.find((station) => station.id === id) ?? null;
}

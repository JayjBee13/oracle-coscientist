/**
 * The browser smoke's journey, one phase per `run-code` invocation.
 *
 * `browser.ps1` substitutes __SMOKE_CONFIG__ for a JSON object and calls this
 * file once per phase against the same playwright-cli session. The `page`
 * object survives between invocations, so console errors collected in the first
 * phase are still there in the last — which is how "zero console errors across
 * the whole walk" is enforced without holding one enormous call open.
 *
 * Two rules this file exists to obey, both learned the hard way:
 *
 *   1. Never `waitUntil: "networkidle"`. A run with an open SSE stream never
 *      goes idle, so the wait would hang until the timeout every time. Every
 *      wait here is on a selector or on a value changing.
 *   2. Never treat `lifecycle == "completed"` as "the run is done". The
 *      supervisor writes the overview, then the lifecycle, then the
 *      `run_finished` event, then the projected artifacts. Waits key on the
 *      `run_finished` event reaching the UI, or on the artifact itself.
 */
async (page) => {
  const CONFIG = __SMOKE_CONFIG__;
  const T = CONFIG.timeoutMs;

  /* --- state that outlives a single run-code invocation ------------------ */

  if (!page.__smoke) {
    const smoke = { errors: [], screenshots: [], data: {} };
    page.__smoke = smoke;
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      const body = message.text();
      // Chromium reports a failed request as a console error too. Those are the
      // network's news, not the app's; they are counted separately so that a
      // deliberately stopped backend cannot mask a real exception.
      const kind = /^Failed to load resource/.test(body) ? "resource" : "console";
      smoke.errors.push({ kind, text: body, url: page.url() });
    });
    page.on("pageerror", (error) => {
      smoke.errors.push({ kind: "pageerror", text: error.message, url: page.url() });
    });
  }
  const S = page.__smoke;

  /* --- helpers ----------------------------------------------------------- */

  async function shot(name) {
    await page.screenshot({ path: `${CONFIG.shotDir}/${name}.png`, fullPage: true });
    if (!S.screenshots.includes(name)) S.screenshots.push(name);
  }

  async function seen(locator, label, timeout) {
    const first = locator.first();
    try {
      await first.waitFor({ state: "visible", timeout: timeout || T });
    } catch {
      await shot(`failure-${CONFIG.phase}`);
      const body = (await page.locator("body").innerText()).slice(0, 2000);
      throw new Error(`${label} never became visible at ${page.url()}: ${body}; errors: ${JSON.stringify(S.errors)}`);
    }
    return first;
  }

  async function gone(locator, label, timeout) {
    try {
      await locator.first().waitFor({ state: "detached", timeout: timeout || T });
    } catch {
      throw new Error(`${label} was still on screen`);
    }
  }

  /** Poll a predicate until it is true. Errors inside it are treated as "not yet". */
  async function until(label, predicate, timeout) {
    const deadline = Date.now() + (timeout || T);
    let last = null;
    while (Date.now() < deadline) {
      try {
        const value = await predicate();
        if (value) return value;
        last = value;
      } catch (error) {
        last = `threw ${error.message}`;
      }
      await page.waitForTimeout(200);
    }
    throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)})`);
  }

  async function apiGet(path) {
    const raw = await page.evaluate(async (url) => {
      const response = await fetch(url);
      return { ok: response.ok, status: response.status, text: await response.text() };
    }, CONFIG.apiBase + path);
    if (!raw.ok) throw new Error(`GET ${path} -> ${raw.status}: ${raw.text.slice(0, 300)}`);
    return JSON.parse(raw.text);
  }

  async function text(locator) {
    return ((await locator.first().textContent()) || "").trim();
  }

  function assert(condition, message) {
    if (!condition) throw new Error(message);
  }

  /** Placeholder text the old product shipped; its absence is the point. */
  const PLACEHOLDERS = [
    "Generated Research Hypothesis",
    "undefined",
    "null",
    "[object Object]",
    "NaN",
  ];

  function assertRealText(value, label, minLength) {
    assert(typeof value === "string" && value.trim().length >= minLength,
      `${label} was too short to be real content (${JSON.stringify((value || "").slice(0, 80))})`);
    for (const bad of PLACEHOLDERS) {
      assert(!value.includes(bad), `${label} contains the placeholder ${JSON.stringify(bad)}`);
    }
  }

  /** The connection chip in the top bar, by its five possible labels. */
  function connectionChip() {
    return page
      .locator(".topbar .chip")
      .filter({ hasText: /^(Connected|Connecting|Live|Reconnecting|Offline)$/ });
  }

  async function openTab(name) {
    await page.getByRole("tab", { name }).first().click();
    await page.waitForTimeout(150);
  }

  /* --- phases ------------------------------------------------------------ */

  /**
   * First load: the runs list shows the imported history, with real titles,
   * real questions and real leaders — not the placeholder rows the old build
   * rendered while it pretended to have data.
   */
  async function boot() {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto(`${CONFIG.baseUrl}/`, { waitUntil: "domcontentloaded" });

    await seen(page.getByRole("heading", { name: "Research runs", level: 1 }), "runs list heading");

    const rows = page.locator(".run-row");
    await until(
      `at least ${CONFIG.expectedImportedRuns} run rows`,
      async () => (await rows.count()) >= CONFIG.expectedImportedRuns,
      45000,
    );
    const rowCount = await rows.count();

    const titles = await page.locator(".run-row__title").allInnerTexts();
    assert(titles.length >= CONFIG.expectedImportedRuns,
      `expected ${CONFIG.expectedImportedRuns} titles, saw ${titles.length}`);
    titles.forEach((title, index) => assertRealText(title, `run row ${index} title`, 8));

    const questions = await page.locator(".run-row__question").allInnerTexts();
    const substantial = questions.filter((q) => q.trim().length >= 40).length;
    assert(substantial >= 10,
      `only ${substantial} rows showed a real question preview; the list is rendering stubs`);

    // Two separate claims, because the list is virtualized and only the rows in
    // the viewport exist in the DOM. Counting "Imported" rows in the DOM used to
    // stand in for both, which silently became unsatisfiable as soon as real runs
    // existed: they sort ahead of the archive and push imported rows out of the
    // window. First: the archive is all there, and the backend serves it.
    const listed = await apiGet("/runs?sort=recent&page_size=100");
    const importedTotal = listed.items.filter((r) => r.source === "imported").length;
    assert(importedTotal >= CONFIG.expectedImportedRuns,
      `backend serves ${importedTotal} imported runs, expected ${CONFIG.expectedImportedRuns}`);

    // Second: every imported row that IS rendered reads the imported vocabulary.
    // The window starts at the top of an unscrolled list, so the rendered rows are
    // the first `rowCount` of the same query, in the same order.
    const imported = await page.locator(".run-row__status", { hasText: "Imported" }).count();
    const expectedInWindow = listed.items
      .slice(0, rowCount)
      .filter((r) => r.source === "imported").length;
    assert(imported === expectedInWindow,
      `${imported} of the ${rowCount} rendered rows read as Imported, expected ${expectedInWindow}`);
    assert(imported > 0, "no rendered row read as Imported — the archive is not on screen");

    const leaders = await page.locator(".run-row__cell--top", { hasText: "Elo" }).count();
    assert(leaders >= 10, `only ${leaders} rows showed a leading idea with an Elo`);

    // The demo runs this smoke creates must not be in the default list.
    const demoChips = await page.locator(".run-row__tags .chip", { hasText: "Demo" }).count();
    assert(demoChips === 0, `${demoChips} demo runs leaked into the default list`);

    await seen(connectionChip(), "connection chip");
    await shot("01-runs-list");

    return { rowCount, importedRows: imported, leadersWithElo: leaders };
  }

  /**
   * An imported run end to end: every tab renders, the research overview is
   * prose, and a hypothesis body is actually readable.
   */
  async function imported() {
    const list = await apiGet("/runs?page_size=100");
    const candidates = list.items
      .filter((r) => r.source === "imported" && r.has_overview && r.counts.matches > 0)
      .sort((a, b) => b.counts.matches - a.counts.matches);
    assert(candidates.length > 0, "no imported run with an overview and a tournament to open");
    const run = candidates[0];

    const withoutOverview = list.items.find((r) => r.source === "imported" && !r.has_overview);
    assert(withoutOverview, "expected at least one imported run without an overview");
    S.data.run = { id: run.id, title: run.title };
    S.data.noOverviewRunId = withoutOverview.id;
    S.data.comparePair = candidates.slice(0, 2).map((r) => ({ id: r.id, title: r.title }));
    assert(S.data.comparePair.length === 2, "need two imported runs to compare");

    // Reach it the way a person would: search the list, then click the row.
    await page.goto(`${CONFIG.baseUrl}/`, { waitUntil: "domcontentloaded" });
    const search = await seen(page.getByLabel("Search runs"), "run search box");
    await search.fill(run.title.slice(0, 30));
    const link = page.locator(`a.run-row__link[href="/runs/${run.id}"]`);
    await seen(link, `row for ${run.title}`, 20000);
    await link.click();

    await until("the run workspace URL", async () => page.url().includes(`/runs/${run.id}`));
    await seen(page.getByRole("tablist", { name: "Run workspace" }), "workspace tablist");

    // Imported runs read "Summary" where a live run reads "Live".
    for (const name of ["Summary", "Hypotheses", "Report", "Activity", "Settings"]) {
      await seen(page.getByRole("tab", { name: new RegExp(`^${name}`) }), `${name} tab`);
    }
    await seen(page.locator(".lb__row"), "leaderboard rows");
    await shot("02-imported-summary");

    // Ideas: the genealogy is the default view. Every idea is a circle, the
    // legend describes the run rather than the component, and clicking one
    // opens it in the rail beside the graph.
    await openTab(/^Hypotheses/);
    const ideas = page.locator("g.ig-node[role='button']");
    const drawn = await until("the genealogy to draw its ideas",
      async () => { const n = await ideas.count(); return n > 0 ? n : false; }, 20000);
    await seen(page.getByRole("list", { name: "Legend" }), "graph legend");
    // The label is a short phrase cut from the title — never the bare hid, which
    // told a reader nothing, and never the ninety-character title, which wrecks
    // the layout. The hid is still on the node, in its accessible name.
    const firstLabel = await text(page.locator("g.ig__labels text.ig-node__label"));
    assert(firstLabel.length > 0, "a node on the genealogy carries no label at all");
    assert(!/^h\d+$/.test(firstLabel),
      `a node is labelled ${JSON.stringify(firstLabel)} — the bare hid, not a phrase from its title`);
    const firstNodeName = (await ideas.first().getAttribute("aria-label")) || "";
    assert(/h\d+/.test(firstNodeName),
      `a node's accessible name is ${JSON.stringify(firstNodeName)}, with no hid in it`);
    await shot("03-imported-ideas-graph");

    await ideas.first().click();
    const rail = page.locator("aside", { hasText: "Selected idea" });
    const railBody = await seen(rail.locator(".prose"), "the selected idea's body", 20000);
    const railText = await railBody.innerText();
    assertRealText(railText, "selected idea body", 200);
    await seen(rail.locator(".hyp__section", { hasText: "What the reviewers said" }),
      "reviews in the rail");
    await shot("03b-imported-idea-selected");

    // The ranked list is still one of the three views, and still reads.
    await page.getByRole("tab", { name: "List" }).click();
    const cards = page.locator("article.hyp");
    await until("hypothesis cards", async () => (await cards.count()) > 0, 20000);
    const cardCount = await cards.count();
    await cards.first().locator(".hyp__head").click();
    const body = await seen(cards.first().locator(".hyp__body .hyp__section").first(),
      "hypothesis body", 20000);
    const bodyText = await body.innerText();
    assertRealText(bodyText, "hypothesis body", 200);
    await seen(cards.first().locator(".hyp__body", { hasText: "What the reviewers said" }),
      "reviews section");
    await shot("03c-imported-hypotheses-list");

    // Report: the overview renders as prose.
    await openTab("Report");
    const prose = await seen(page.locator(".prose"), "research overview prose", 25000);
    const overview = await prose.innerText();
    assertRealText(overview, "research overview", 500);
    await seen(page.getByRole("link", { name: "Download report", exact: true }), "overview download link");
    await shot("04-imported-report");

    // Activity: an imported run has no event log and says so rather than lying.
    await openTab(/^Activity/);
    await seen(page.locator(".panel", { hasText: "Event log" }), "event log panel");
    await shot("05-imported-activity");

    // Settings: the immutable config.
    await openTab("Settings");
    await seen(page.locator(".panel", { hasText: "The question" }), "question panel");
    await seen(page.locator(".kv"), "config key/value list");
    await shot("06-imported-settings");

    return {
      run: run.title,
      ideasDrawn: drawn,
      hypothesisCards: cardCount,
      overviewChars: overview.length,
      hypothesisBodyChars: bodyText.length,
    };
  }

  /** A hypothesis on its own page: body, reviews and lineage. */
  async function hypothesis() {
    const run = S.data.run;
    // `?view=` carries the choice, so a pasted link opens on what its sender
    // was looking at — which is also how this phase reaches the ranked list.
    await page.goto(`${CONFIG.baseUrl}/runs/${run.id}?tab=hypotheses&view=list`, {
      waitUntil: "domcontentloaded",
    });
    const cards = page.locator("article.hyp");
    await until("hypothesis cards", async () => (await cards.count()) > 0, 20000);
    await cards.first().locator(".hyp__head").click();
    const open = await seen(page.getByRole("link", { name: "Open on its own page" }),
      "open-on-its-own-page link", 20000);
    await open.click();

    await until("the hypothesis URL", async () => /\/runs\/[^/]+\/hypotheses\/h?\w+/.test(page.url()));
    await seen(page.locator(".panel", { hasText: "The hypothesis" }), "hypothesis panel");
    const body = await text(page.locator(".panel", { hasText: "The hypothesis" }).locator(".prose"));
    assertRealText(body, "hypothesis page body", 200);
    await seen(page.locator(".panel", { hasText: "What the reviewers said" }), "reviews panel");
    await seen(page.locator(".panel", { hasText: "Where it came from" }), "lineage panel");
    await shot("07-hypothesis-page");

    return { url: page.url(), bodyChars: body.length };
  }

  /**
   * The three GUI-era runs have no research overview. The Report tab has to say
   * so honestly instead of rendering an empty document.
   */
  async function noReport() {
    await page.goto(`${CONFIG.baseUrl}/runs/${S.data.noOverviewRunId}?tab=report`, {
      waitUntil: "domcontentloaded",
    });
    const empty = await seen(
      page.locator(".state", { hasText: "No report was written" }),
      "honest missing-report empty state",
      20000,
    );
    const message = await empty.innerText();
    assert(!/\b0\b/.test(message), `missing-report state rendered a zero: ${message}`);
    await shot("08-imported-no-report");
    return { message: message.split("\n")[0] };
  }

  /** The launch wizard, all five steps, as a demo run. */
  async function wizard() {
    await page.goto(`${CONFIG.baseUrl}/new?runner=demo`, { waitUntil: "domcontentloaded" });
    await seen(page.getByRole("heading", { name: "Start a research run", level: 1 }),
      "wizard heading");

    const question =
      "SMOKE: which failure modes stop a multi-agent research loop from producing testable hypotheses?";
    await page.getByLabel("What should Oracle investigate?").fill(question);
    await shot("09-wizard-question");

    await page.getByRole("button", { name: "Draft two directions" }).click();

    // The demo harness runs the workshop through the scripted runner, so the two
    // options arrive in a second or two rather than a minute.
    const options = page.locator("button.opt");
    await until("two workshop directions", async () => (await options.count()) === 2, 60000);
    await shot("10-wizard-options");

    await options.first().click();
    await until("the first direction to read as selected", async () =>
      (await options.first().getAttribute("aria-pressed")) === "true");
    await page.getByRole("button", { name: "Continue with this direction" }).click();

    const prompt = await seen(page.locator("#wizard-prompt"), "final prompt textarea", 20000);
    const promptText = await prompt.inputValue();
    assertRealText(promptText, "workshop final prompt", 80);
    await shot("11-wizard-prompt");
    await page.getByRole("button", { name: "Continue to settings" }).click();

    // Presets: prove the mechanism moves the estimate, then settle on Standard —
    // a one-round Quick look finishes before a person could click Pause, and the
    // live/pause/resume assertions below need a run that lasts.
    const quick = page.locator("button.preset", { hasText: "Quick look" });
    const standard = page.locator("button.preset", { hasText: "Standard" });
    await seen(quick, "Quick look preset");
    await quick.click();
    await until("Quick look to read as selected", async () =>
      (await quick.getAttribute("aria-pressed")) === "true");
    const quickEstimate = await text(page.locator(".est-strip .est__value").first());
    await standard.click();
    await until("Standard to read as selected", async () =>
      (await standard.getAttribute("aria-pressed")) === "true");
    const standardEstimate = await text(page.locator(".est-strip .est__value").first());
    assert(quickEstimate !== standardEstimate,
      `preset change did not move the call estimate (${quickEstimate} both times)`);
    await shot("12-wizard-configure");

    await page.getByRole("button", { name: "Review and launch" }).click();

    const confirm = await seen(page.locator(".confirm-line", { hasText: "Runs on" }),
      "confirm summary", 20000);
    const runsOn = await confirm.innerText();
    assert(runsOn.includes("Demo"), `confirm step did not resolve to the demo runner: ${runsOn}`);
    await seen(
      page.getByText("You can pause or stop at any time — stopping still writes a report from what exists."),
      "the pause/stop reassurance sentence",
    );
    await seen(page.locator("table", { hasText: "Thinking effort" }), "resolved model table");
    await shot("13-wizard-confirm");

    await page.getByRole("button", { name: "Launch demo run" }).click();

    const runId = await until("the new run's URL", async () => {
      const match = page.url().match(/\/runs\/([0-9a-f-]{36})/);
      return match ? match[1] : false;
    }, 45000);
    S.data.demoRunIds = (S.data.demoRunIds || []).concat(runId);
    S.data.demoRunId = runId;

    return { runId, question, quickEstimate, standardEstimate };
  }

  /**
   * The assertion this whole smoke exists for: the live view is live. Events
   * arrive over SSE and the numbers move, with no reload anywhere.
   */
  async function live() {
    const runId = S.data.demoRunId;
    await seen(page.getByRole("tablist", { name: "Run workspace" }), "workspace tablist", 30000);

    // If the page reloads, this sentinel dies with it. Nothing below reloads.
    const sentinel = await page.evaluate(() => {
      window.__smokeSentinel = Math.random().toString(36).slice(2);
      return window.__smokeSentinel;
    });

    const ring = page.getByRole("img", { name: /model calls used/ });
    await seen(ring, "budget ring", 30000);
    const ringBefore = await ring.getAttribute("aria-label");
    const events = page.locator(".event-log .event");
    const eventsBefore = await events.count();
    const hypStat = page.locator(".stat", { hasText: "Hypotheses" }).locator(".stat__value");
    const hypBefore = Number(await text(hypStat));

    await until("the stream to report itself Live", async () =>
      (await text(connectionChip())) === "Live", 30000);

    const grew = await until("the event log to grow by five without a reload", async () => {
      const now = await events.count();
      return now >= eventsBefore + 5 ? now : false;
    }, 90000);

    await until("the budget ring to move", async () =>
      (await ring.getAttribute("aria-label")) !== ringBefore, 90000);

    const hypAfter = await until("hypotheses to appear", async () => {
      const value = Number(await text(hypStat));
      return value > hypBefore ? value : false;
    }, 90000);

    await until("the research portfolio to fill", async () =>
      (await page.locator("section.panel", { hasText: "Research portfolio" }).locator("li a").count()) > 0, 90000);

    const stillThere = await page.evaluate(() => window.__smokeSentinel);
    assert(stillThere === sentinel,
      "the page reloaded during the live phase — the updates were not live");

    // The Live tab shows the log in a sidebar column. Its three-column row once
    // squeezed the message down to one word per line there, which is legible
    // only in a screenshot — so it is measured.
    const messageBox = await page.locator(".event-log .event__body").first().boundingBox();
    assert(messageBox && messageBox.width >= 140,
      `the event message column collapsed to ${messageBox ? Math.round(messageBox.width) : 0}px`);

    await shot("14-live-running");

    /* --- controls: pause, resume, finish, with the confirm dialog -------- */

    const controls = page.locator(".controls");
    await seen(controls.getByRole("button", { name: "Pause" }), "Pause button", 30000);
    await controls.getByRole("button", { name: "Pause" }).click();

    await until("the run to reach Paused", async () =>
      (await controls.getByRole("button", { name: "Resume" }).count()) > 0, 60000);
    const pausedStatus = await text(page.locator(".ws-head .chip").first());
    await shot("15-paused");

    await controls.getByRole("button", { name: "Resume" }).click();
    await until("the run to be running again", async () =>
      (await controls.getByRole("button", { name: "Pause" }).count()) > 0, 60000);

    // Finish is confirmed, and the dialog names the run and its spend.
    await controls.getByRole("button", { name: "Finish after this round" }).click();
    const dialog = await seen(page.getByRole("dialog"), "confirm dialog", 15000);
    const dialogText = await dialog.innerText();
    assert(dialogText.includes("Finish after this round"),
      `dialog did not name the action: ${dialogText}`);
    assert(/model calls/.test(dialogText), `dialog did not state the spend: ${dialogText}`);
    await seen(dialog.getByRole("button", { name: "Cancel" }), "dialog cancel button");
    await shot("16-confirm-dialog");

    // Cancelling has to actually cancel.
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await gone(page.getByRole("dialog"), "confirm dialog after cancel", 10000);
    await until("the run to still be running after cancel", async () =>
      (await controls.getByRole("button", { name: "Finish after this round" }).count()) > 0, 15000);

    await controls.getByRole("button", { name: "Finish after this round" }).click();
    const again = await seen(page.getByRole("dialog"), "confirm dialog (second time)", 15000);
    await again.getByRole("button", { name: "Finish after this round" }).click();

    // Wait on the run_finished EVENT, never on the lifecycle: the supervisor
    // still has writes in flight when the lifecycle flips.
    await openTab(/^Activity/);
    await until("the run_finished event to reach the UI", async () =>
      (await page.locator(".event__type", { hasText: "Run finished" }).count()) > 0, 180000);
    await shot("17-activity-finished");

    return {
      runId,
      eventsBefore,
      eventsAfter: grew,
      hypothesesAfter: hypAfter,
      pausedStatus,
      sentinel: stillThere === sentinel,
    };
  }

  /** After run_finished: a report to read and hypotheses to rank. */
  async function finished() {
    const runId = S.data.demoRunId;

    await page.goto(`${CONFIG.baseUrl}/runs/${runId}?tab=report`, { waitUntil: "domcontentloaded" });
    const prose = await seen(page.locator(".prose"), "generated research overview", 60000);
    const overview = await prose.innerText();
    assertRealText(overview, "generated research overview", 200);
    await shot("18-report-after-finish");

    await openTab("Research");
    await seen(page.getByRole("heading", { name: "Research workspace", exact: true }), "adaptive workspace");
    await seen(page.getByRole("heading", { name: "Independent approaches", exact: true }), "independent approaches");
    await page.getByRole("button", { name: /Challenge the whole answer/ }).click();
    await seen(page.getByText(/Blocking findings feed the next checkpoint/), "challenge feedback explanation");
    await shot("18b-adaptive-research");

    await page.goto(`${CONFIG.baseUrl}/how-it-works`, { waitUntil: "domcontentloaded" });
    await seen(page.getByRole("region", { name: "Adaptive research workflow" }), "adaptive workflow diagram");
    await page.getByRole("button", { name: /Explore independently or develop/ }).click();
    await seen(page.getByText(/Exploration calls see their own framing/), "independent-context explanation");
    await shot("18c-adaptive-workflow");
    await page.goto(`${CONFIG.baseUrl}/runs/${runId}?tab=research`, { waitUntil: "domcontentloaded" });

    await openTab(/^Hypotheses/);
    const ideas = page.locator("g.ig-node[role='button']");
    const drawn = await until("the genealogy of the finished run", async () => {
      const n = await ideas.count();
      return n >= 3 ? n : false;
    }, 30000);
    await shot("19-ideas-after-finish");

    await page.getByRole("tab", { name: "List" }).click();
    const cards = page.locator("article.hyp");
    const count = await until("the generated hypotheses", async () => {
      const n = await cards.count();
      return n >= 3 ? n : false;
    }, 30000);
    const firstTitle = await text(cards.first().locator(".hyp__title"));
    assertRealText(firstTitle, "generated hypothesis title", 8);
    await shot("19b-hypotheses-after-finish");

    const status = await text(page.locator(".ws-head .chip").first());
    return { overviewChars: overview.length, ideasDrawn: drawn, hypotheses: count, status };
  }

  /**
   * Compare: two independent ranked lists. The old build merged Elo across
   * runs, which is meaningless — every row on each side must belong to that
   * side's run.
   */
  async function compare() {
    const [a, b] = S.data.comparePair;
    await page.goto(`${CONFIG.baseUrl}/compare`, { waitUntil: "domcontentloaded" });
    await seen(page.getByRole("heading", { name: "Compare two runs", level: 1 }), "compare heading");

    const pick = async (side, run) => {
      const box = page.getByRole("searchbox").nth(side === "Baseline" ? 0 : 1);
      await box.fill(run.title.slice(0, 30));
      const row = page
        .locator(`.picker__list[aria-label="${side}"] .picker__row`, { hasText: run.title.slice(0, 30) });
      await seen(row, `${side} picker row for ${run.title}`, 20000);
      await row.first().click();
    };
    await pick("Baseline", a);
    await pick("Challenger", b);

    await until("the comparison URL", async () => page.url().includes(`/compare/${a.id}/${b.id}`), 20000);

    const baseline = await seen(page.locator('section[aria-label^="Baseline: "]'), "baseline panel", 30000);
    const challenger = await seen(page.locator('section[aria-label^="Challenger: "]'), "challenger panel");

    const baselineHrefs = await baseline.locator("a.rank__row").evaluateAll((nodes) =>
      nodes.map((n) => n.getAttribute("href")));
    const challengerHrefs = await challenger.locator("a.rank__row").evaluateAll((nodes) =>
      nodes.map((n) => n.getAttribute("href")));

    assert(baselineHrefs.length > 0 && challengerHrefs.length > 0,
      `both sides must rank something (${baselineHrefs.length}/${challengerHrefs.length})`);
    assert(baselineHrefs.every((href) => href.startsWith(`/runs/${a.id}/`)),
      "the baseline list contains a hypothesis from the other run — the rankings were merged");
    assert(challengerHrefs.every((href) => href.startsWith(`/runs/${b.id}/`)),
      "the challenger list contains a hypothesis from the other run — the rankings were merged");

    const panels = await page.locator(".cmp-grid > section").count();
    assert(panels === 2, `expected exactly two ranked panels, saw ${panels}`);
    await shot("20-compare");

    return { baselineRanked: baselineHrefs.length, challengerRanked: challengerHrefs.length };
  }

  /**
   * Backend stopped by the caller. The UI has to name the problem and offer a
   * way out — and must not paint a screen of confident zeros while it does.
   */
  async function offline() {
    // A query nothing has cached, so this is the cold-start error path.
    await page.goto(`${CONFIG.baseUrl}/?q=offline-probe-${Date.now()}`, {
      waitUntil: "domcontentloaded",
    });
    const listError = await seen(
      page.locator(".state--error", { hasText: "Could not load your runs" }),
      "named runs-list error",
      45000,
    );
    const listMessage = await listError.innerText();
    assertRealText(listMessage, "runs-list error message", 30);
    await seen(listError.getByRole("button", { name: "Try again" }), "runs-list retry button");
    assert((await page.locator(".run-row").count()) === 0, "rows rendered beside the error");
    const zeros = await page.locator(".stat__value", { hasText: /^0$/ }).count();
    assert(zeros === 0, `${zeros} zero-valued stats rendered beside the error`);
    await shot("21-offline-runs-list");

    // A run nobody opened this session: nothing cached, so nothing to fake.
    await page.goto(`${CONFIG.baseUrl}/runs/${S.data.noOverviewRunId}`, {
      waitUntil: "domcontentloaded",
    });
    const detailError = await seen(
      page.locator(".state--error", { hasText: "Could not open this run" }),
      "named run-detail error",
      45000,
    );
    await seen(detailError.getByRole("button", { name: "Try again" }), "run-detail retry button");
    assert((await page.locator(".ring").count()) === 0, "a budget ring rendered beside the error");
    assert((await page.locator(".stat__value").count()) === 0, "stats rendered beside the error");
    await shot("22-offline-run-detail");

    return { listMessage: listMessage.split("\n").slice(0, 2).join(" — ") };
  }

  /** Backend restarted by the caller. Retry has to actually recover. */
  async function recover() {
    const retry = await seen(page.getByRole("button", { name: "Try again" }), "retry button", 30000);
    await retry.click();
    await seen(page.getByRole("tablist", { name: "Run workspace" }), "run workspace after retry", 45000);

    await page.goto(`${CONFIG.baseUrl}/`, { waitUntil: "domcontentloaded" });
    await until("the runs list to come back", async () =>
      (await page.locator(".run-row").count()) >= CONFIG.expectedImportedRuns, 45000);
    await shot("23-recovered");

    return { rows: await page.locator(".run-row").count() };
  }

  const PHASES = {
    boot,
    imported,
    hypothesis,
    no_report: noReport,
    wizard,
    live,
    finished,
    compare,
    offline,
    recover,
  };

  const phase = PHASES[CONFIG.phase];
  if (!phase) throw new Error(`unknown phase ${CONFIG.phase}`);

  const result = await phase();

  /* --- console errors are fatal, attributed to the phase that raised them -- */

  const describe = (list) => list.map((e) => `${e.kind} @ ${e.url} :: ${e.text}`).join("\n");

  const appErrors = S.errors.filter((error) => error.kind !== "resource");
  if (appErrors.length > 0) {
    throw new Error(
      `browser console errors (${appErrors.length}) by end of phase ${CONFIG.phase}:\n` +
        describe(appErrors),
    );
  }

  let resourceErrors = S.errors.filter((error) => error.kind === "resource");
  let tolerated = 0;
  if (CONFIG.phase === "offline") {
    // The backend was stopped on purpose; the browser reporting those requests
    // as failed is the expected consequence, and the app threw nothing.
    assert(resourceErrors.length > 0, "the backend was supposed to be down, but nothing failed");
    tolerated = resourceErrors.length;
    S.errors.splice(0, S.errors.length);
    resourceErrors = [];
  }
  if (resourceErrors.length > 0) {
    throw new Error(
      `failed requests (${resourceErrors.length}) by end of phase ${CONFIG.phase}:\n` +
        describe(resourceErrors),
    );
  }

  return {
    phase: CONFIG.phase,
    ...result,
    demoRunIds: S.data.demoRunIds || [],
    screenshots: S.screenshots,
    consoleErrors: 0,
    toleratedRequestFailures: tolerated,
  };
}

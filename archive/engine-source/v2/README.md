# AI Co-Scientist — Claude Code edition (v2.0)

A multi-agent hypothesis engine you run inside the **Claude Code CLI**. Give it any
research goal and it generates → critiques → ranks → evolves novel, testable hypotheses
in a self-improving Elo tournament. Modeled on Google's *AI co-scientist*
(arXiv:2502.18864), but domain-agnostic.

**New in v2.0 — the Cartographer Graft.** A reactive, zero-when-idle organ that detects
idea-pool *collapse* (premature convergence) from the existing cluster structure and, only
then, injects a distant-domain analogy into the next generation step to re-diversify the
search. It is byte-identical to v1 when it does not fire. This was the top-ranked design
out of an 18-architecture self-tournament on "how to restructure the co-scientist"; see
**`cartographer_graft_spec.md`** for the mechanism, calibration, and A/B protocol.

## Start here (orientation for a new Claude session)

If you are Claude and someone pointed you here: this project is the **co-scientist** above.
To operate it:
1. The user will give a **research goal**. **First restate that goal in one crisp line and
   wait for the user's explicit confirmation (or correction) before doing anything** — they
   like the rephrasing but want to approve it before any compute is spent. **Never auto-start.**
2. Once confirmed, run the **`coscientist` skill** (`.claude/skills/coscientist/SKILL.md`),
   which drives the whole generate→reflect→cluster→rank→evolve→meta-review loop and the v2
   collapse-check/Cartographer step. It owns all state via `coscientist.py`.
3. Research agents can use **Perplexity** (broad search → links → deep-read) once the MCP
   server is connected — run `/mcp` to confirm `perplexity` is up (see "Research tools").

`CLAUDE.md` auto-loads this same protocol whenever Claude is launched inside this folder, so
usually no pointing is needed — launching in `ai-coscientist_v2/` is enough.

## What's in here

```
ai-coscientist_v2/
├── CLAUDE.md                       # project memory (auto-loaded by Claude Code)
├── coscientist.py                  # deterministic state engine (stdlib only) — adds collapse-check + inject
├── co-scientist_principles.md      # reference spec of the published architecture
├── cartographer_graft_spec.md      # v2.0 design + calibration + A/B experiment plan
├── .claude/
│   ├── skills/coscientist/SKILL.md # the Supervisor loop (the brain) — adds Step 3.5
│   └── agents/                     # seven specialized subagents
│       ├── generation.md
│       ├── reflection.md
│       ├── ranking.md
│       ├── evolution.md
│       ├── proximity.md
│       ├── meta-review.md
│       └── cartographer.md         # v2.0 reactive divergence organ (fires on collapse only)
└── runs/                           # created per run; state + hypotheses (restart-safe)
```

## Requirements
- Claude Code CLI installed and authenticated.
- Python 3.9+ (no packages needed).
- For Perplexity research (optional, v2): Node.js / `npx` and a `PERPLEXITY_API_KEY` (see
  "Research tools" below). Without it, agents fall back to built-in `WebSearch`/`WebFetch`.

## Quick start
```bash
cd ai-coscientist
claude                  # start Claude Code in this directory
```
Then in the session:
```
Run the co-scientist on: "Why do some long-lived species resist cancer despite having
far more cells than humans?"  Use a budget of 60 to start.
```
The Supervisor skill takes over: it initializes a run, then loops generation → reflection →
proximity → ranking → evolution → meta-review, showing you a standings table after each
round and asking whether to continue.

## How it works
1. **Generation** proposes N grounded hypotheses (web search).
2. **Reflection** peer-reviews each → pass/reject.
3. **Proximity** clusters near-duplicates so the tournament isn't wasted on rephrasings.
   - **3.5 Collapse check (v2).** The engine's `collapse-check` reads the cluster structure;
     if the pool is converging, the **Cartographer** injects a distant-domain analogy seed
     into the next generation step. No fire ⇒ no-op ⇒ identical to v1.
4. **Ranking** runs pairwise debates; the engine updates **Elo** ratings.
5. **Evolution** improves the leaders (ground / combine / simplify / out-of-box) → new
   variants re-enter the tournament.
6. **Meta-review** distills recurring critiques into feedback for the next round, and
   writes the final `research_overview.md`.

The Supervisor (the skill) owns all state; subagents are stateless workers with strict
output contracts. Deterministic work (Elo, IDs, budget, pairing) is in `coscientist.py`.

## Controls
`python3 coscientist.py init` flags:
- `--budget N`   hard ceiling on total LLM calls (default 150). **Start at ~60.**
- `--matches N`  ranking matches per round (default 6).
- `--top-k N`    how many leaders to evolve each round (default 3).

Inspect any run directly:
```bash
python3 coscientist.py status --run-id <id>
python3 coscientist.py top    --run-id <id> --k 5
cat runs/<id>/hypotheses/h001.md
```

## v2.0: The Cartographer Graft

A single reactive organ on the unchanged v1 backbone. Each round, after clustering:

```bash
python3 coscientist.py collapse-check --run-id <id>   # deterministic, no LLM, no tick
```

It votes "collapse" on up to three signals computed from existing state — **plateau**
(distinct active-cluster count not growing over `window` rounds), **concentration**
(Herfindahl over cluster sizes ≥ threshold), and **birth-rate** (no new cluster labels
born). It fires when `votes ≥ quorum_k` (and a `cooldown` has elapsed). On fire, the
Supervisor calls the `cartographer` subagent once and stores its seed:

```bash
python3 coscientist.py inject --run-id <id> --text "<SKELETON + SEED_FRAMING>"
# consumed (and cleared) by the next generation step; `--clear` empties it
```

Config lives under `config.graft` in `state.json` (set at `init`):
- `enabled` (default **true**; set false ⇒ byte-identical to v1)
- `quorum_k` — **1 = single-signal (shipped/minimal-first)**, 2 = hardened multi-signal
  (only after calibrating thresholds on logged runs — see spec §6)
- `window`, `thresholds {hhi, birth_rate, plateau_slack}`, `cooldown`

**Budget:** `collapse-check` spends **0** LLM calls; the Cartographer is **1** call, and
only when it fires. A healthy run never fires ⇒ zero added cost. Verify with
`status` → `graft.fired_count`. The recommended rollout (ship single-signal, calibrate,
then promote to `quorum_k=2`, and never bundle other organs) is in the spec.

## Research tools: Perplexity (MCP)

**The Supervisor asks at the start of each run** whether to use Perplexity (cited, paid) or
built-in WebSearch (free), then passes that choice to the agents.

The search-using agents — **generation, reflection, evolution, cartographer** — can use
Perplexity's official MCP server (`@perplexity-ai/mcp-server`) for grounding, on top of the
built-in `WebSearch`/`WebFetch`. The intended pattern is **broad search → pull links →
deep-read**: `perplexity_search` returns ranked, *cited* results; the agent then `WebFetch`es
the interesting links. `perplexity_research` is available for a deeper multi-source pass.

**Setup (already wired in this repo):**
- `.mcp.json` registers the server and reads the key from `${PERPLEXITY_API_KEY}`.
- `.claude/settings.local.json` holds the key (in its `env` block) and pre-allows the
  `mcp__perplexity__*` tools. **This file is git-ignored — the key is never committed.**
- The four agents list the Perplexity tools in their frontmatter; `ranking`, `proximity`,
  and `meta-review` deliberately do **not** use it.

**To activate:** restart Claude Code in this directory so the MCP server loads, then run
`/mcp` to confirm `perplexity` is connected. If the key isn't picked up from settings, export
it before launching (`export PERPLEXITY_API_KEY=...`) or put it directly in `.mcp.json`'s
`env`. Agents fall back to `WebSearch`/`WebFetch` if the server is absent.

> **Security:** keep the key in `settings.local.json` (git-ignored) or an env var, never in a
> committed file. Rotate the key if it has ever been shared in plaintext (e.g., pasted into a chat).

> **Cost:** Perplexity API is paid per request and a subagent-heavy run issues many searches.
> There is no spend cap here yet — watch usage in the Perplexity dashboard. To throttle later,
> remove the Perplexity tools from `reflection` (the highest-volume caller) first, or gate them
> behind a "deep-research" path.

## Cost warning
Subagent-heavy workflows can use several times the tokens of a single chat thread (each
subagent keeps its own context). The `--budget` ceiling is your cost control — test small.

## Caveat
Elo here is a **self-evaluated** quality proxy, not ground truth (same limitation the
original paper flags). Treat outputs as leads for a human to validate, not findings.

## Stage 2 (later)
To make providers selectable (Gemini / OpenAI / local), port the six agent prompts and
the loop into a Python backend using a provider-abstraction layer (e.g. LiteLLM). The
prompts here transfer directly; only the orchestration host changes.

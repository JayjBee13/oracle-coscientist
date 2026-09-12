# AI Co-Scientist — Claude Code edition

A multi-agent hypothesis engine you run inside the **Claude Code CLI**. Give it any
research goal and it generates → critiques → ranks → evolves novel, testable hypotheses
in a self-improving Elo tournament. Modeled on Google's *AI co-scientist*
(arXiv:2502.18864), but domain-agnostic.

## What's in here

```
ai-coscientist/
├── CLAUDE.md                       # project memory (auto-loaded by Claude Code)
├── coscientist.py                  # deterministic state engine (stdlib only)
├── .claude/
│   ├── skills/coscientist/SKILL.md # the Supervisor loop (the brain)
│   └── agents/                     # six specialized subagents
│       ├── generation.md
│       ├── reflection.md
│       ├── ranking.md
│       ├── evolution.md
│       ├── proximity.md
│       └── meta-review.md
└── runs/                           # created per run; state + hypotheses (restart-safe)
```

## Requirements
- Claude Code CLI installed and authenticated.
- Python 3.9+ (no packages needed).

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

## Research tools: Perplexity (optional, asked each run)

generation / reflection / evolution can use Perplexity's official MCP server
(`@perplexity-ai/mcp-server`) for grounding — "broad search → pull cited links → deep-read"
via `WebFetch`. **The Supervisor asks at the start of each run** whether to use Perplexity
(cited, paid per search) or the built-in `WebSearch` (free), and passes that choice to the
agents. Setup: `.mcp.json` registers the server reading `${PERPLEXITY_API_KEY}`; the key sits
in the git-ignored `.claude/settings.local.json`. Needs Node/npx and a fresh Claude session
(launched in this folder) to load the server; run `/mcp` to confirm it's connected. Without
it, agents fall back to built-in search.

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

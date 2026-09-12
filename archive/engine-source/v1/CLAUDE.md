# AI Co-Scientist (Claude Code)

A general-purpose, multi-agent hypothesis engine modeled on Google's "AI co-scientist"
(arXiv:2502.18864). Given any research goal, it generates, peer-reviews, ranks, and
evolves novel testable hypotheses through a self-improving tournament loop.

## Starting a run (operating protocol — do this first)
When asked to run the co-scientist on a goal: (1) **restate the goal in one crisp line and get
the user's confirmation/correction**, and (2) **ask whether to use Perplexity grounding**
(cited, paid per search) **or built-in WebSearch** for this run. WAIT for the user to confirm
both before initializing — do **not** auto-start. Then run the `coscientist` skill, passing
the grounding choice into every search-agent prompt.

## How it runs
- The **`coscientist` skill** (`.claude/skills/coscientist/SKILL.md`) is the Supervisor.
  It drives the whole loop. Trigger it by asking to run the co-scientist on a goal.
- Six **subagents** (`.claude/agents/`) are the specialized workers: generation,
  reflection, ranking, evolution, proximity, meta-review.
- **`coscientist.py`** is the deterministic state engine: IDs, Elo, budget, pair
  selection, clustering, persistence. Run it with `python3` (stdlib only, no deps).
- State + hypotheses live under `runs/<run_id>/`. Runs are restart-safe.
- **Perplexity (optional):** generation/reflection/evolution can use the Perplexity MCP server
  (`@perplexity-ai/mcp-server`) for cited "broad search → links → deep-read" grounding. The
  Supervisor asks each run whether to use it (else built-in WebSearch). Key lives in the
  git-ignored `.claude/settings.local.json`; needs Node/npx and a fresh session to load.

## Architecture rules (important)
- The Supervisor owns ALL state. Subagents are stateless; they return text, the
  Supervisor parses it and writes state via `coscientist.py`.
- Subagents do NOT inherit skills or each other's context. Put everything they need in
  the invocation prompt.
- Call `python3 coscientist.py tick --run-id <id>` after every subagent call; stop when
  it returns `"stop": true`.
- Bias compute toward verification (reflection/ranking) over raw generation.

## Cost
Subagent-heavy: a full run can use many× the tokens of a single thread. The `--budget`
ceiling (LLM-call count) is the main control. Start small (e.g. `--budget 60`) when testing.

## Caveat
Elo is a self-evaluated proxy, not ground truth. Outputs are leads to validate, not
conclusions.

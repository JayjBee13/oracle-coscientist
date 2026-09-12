# AI Co-Scientist (Claude Code) — v2.0

A general-purpose, multi-agent hypothesis engine modeled on Google's "AI co-scientist"
(arXiv:2502.18864). Given any research goal, it generates, peer-reviews, ranks, and
evolves novel testable hypotheses through a self-improving tournament loop.

**v2.0** adds the **Cartographer Graft**: a reactive divergence organ that fires only when
the idea pool is detected to be collapsing (premature convergence). See
`cartographer_graft_spec.md`.

## Starting a run (operating protocol — do this first)
When asked to run the co-scientist on a goal, FIRST (1) **restate the goal in one crisp line
and get the user's confirmation (or correction)**, and (2) **ask whether to use Perplexity
grounding** (cited, paid per search) **or built-in WebSearch** for this run. WAIT for the user
to confirm both before initializing — do **not** auto-start. Then proceed via the `coscientist`
skill, passing the grounding choice into every search-agent prompt.

## How it runs
- The **`coscientist` skill** (`.claude/skills/coscientist/SKILL.md`) is the Supervisor.
  It drives the whole loop. Trigger it by asking to run the co-scientist on a goal.
- Seven **subagents** (`.claude/agents/`) are the specialized workers: generation,
  reflection, ranking, evolution, proximity, meta-review, and **cartographer** (v2 —
  invoked only when `collapse-check` fires).
- **`coscientist.py`** is the deterministic state engine: IDs, Elo, budget, pair
  selection, clustering, persistence, and the v2 **`collapse-check`** trigger + **`inject`**
  seed setter. Run it with `python3` (stdlib only, no deps).
- State + hypotheses live under `runs/<run_id>/`. Runs are restart-safe.

## Architecture rules (important)
- The Supervisor owns ALL state. Subagents are stateless; they return text, the
  Supervisor parses it and writes state via `coscientist.py`.
- Subagents do NOT inherit skills or each other's context. Put everything they need in
  the invocation prompt.
- Call `python3 coscientist.py tick --run-id <id>` after every subagent call; stop when
  it returns `"stop": true`.
- Bias compute toward verification (reflection/ranking) over raw generation.
- **v2 graft:** run `collapse-check` (deterministic — **no tick**) right after `cluster`
  each round; invoke the `cartographer` subagent (and `tick --n 1`) ONLY when it returns
  `"fire": true`, then it feeds the next Generate via `pending_injection`. The graft is a
  reactive, zero-when-idle organ: when it does not fire, the loop is identical to v1. Keep
  it minimal — do not add further organs without an independent A/B (see spec §9).

## Cost
Subagent-heavy: a full run can use many× the tokens of a single thread. The `--budget`
ceiling (LLM-call count) is the main control. Start small (e.g. `--budget 60`) when testing.

## Caveat
Elo is a self-evaluated proxy, not ground truth. Outputs are leads to validate, not
conclusions.

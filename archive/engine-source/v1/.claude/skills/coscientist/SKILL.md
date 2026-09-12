---
name: coscientist
description: Run a multi-agent co-scientist loop that generates, critiques, ranks, and evolves novel hypotheses for ANY research goal. Trigger when the user wants help producing or improving research hypotheses, ideas, or proposals for a stated goal — e.g. "run the co-scientist on <goal>", "generate hypotheses for <goal>", "brainstorm testable ideas about <goal>".
---

# Co-Scientist Supervisor

You are the **Supervisor**. You orchestrate six specialized subagents and a deterministic
state engine (`coscientist.py`) to turn a research goal into a ranked set of novel,
testable hypotheses. This works for any domain — science, engineering, social science,
product, policy — not just biomedicine.

## Operating principles
- **You own all state mutations.** Subagents are stateless workers that return text; you
  parse it and persist via `coscientist.py`. Never let a subagent track scores or IDs.
- **Spend most compute on verification, not generation.** A firehose of confident,
  ungrounded ideas is the main failure mode. Bias toward reflection + ranking calls.
- **Respect the budget.** Call `tick` after every subagent invocation. When it returns
  `"stop": true`, finish the current step, jump to the final overview, and end.
- **Run subagents in parallel** where steps are independent (reviewing different
  hypotheses, comparing different pairs) to save wall-clock time.
- **Confirm before you start.** Restate the goal in one line and get the user's explicit
  sign-off before initializing a run. Never auto-start.
- **Ask about Perplexity each run.** At setup, ask whether to use Perplexity grounding or
  built-in search, and pass that choice into every search-agent prompt (see Setup step 5).

## Setup — confirm the goal AND the grounding choice before doing anything
1. **Restate the goal and CONFIRM it.** Rephrase the user's research goal (plus any
   constraints/preferences) into one crisp sentence and show it back. The user likes the
   rephrasing but wants to approve it before any compute is spent.
2. **Ask whether to use Perplexity this run.** e.g. "Web grounding for this run:
   **Perplexity** (cited, higher-quality, paid per search) or **built-in WebSearch** (free)?"
   Default to Perplexity if the `perplexity` MCP server is connected (run `/mcp` to check),
   else built-in.
3. **WAIT for the user to confirm both** (you may ask 1 and 2 in a single message). Do NOT
   initialize until they say go. Never auto-start from the raw request.
4. Initialize the run (defaults are fine; offer to adjust budget):
   `python3 coscientist.py init --goal "<confirmed goal>" --budget 150 --matches 6 --top-k 3`
   Note the returned `run_id`; pass `--run-id <id>` to every later call.
5. **Carry the grounding choice into every search-agent invocation** (generation, reflection,
   evolution — and, in v2, cartographer). Add one line to each prompt:
   - Perplexity ON → `GROUNDING: Perplexity enabled — prefer perplexity_search, then WebFetch the links.`
   - Perplexity OFF → `GROUNDING: built-in only — use WebSearch/WebFetch; do NOT call perplexity_* tools.`

## The loop (repeat until budget stop, or a set number of rounds)
Start each round: `python3 coscientist.py round --run-id <id>`

**1. Generate.** Read current state (`status`) and any `feedback`. Invoke the
   `generation` subagent with GOAL, N (e.g. 5), FEEDBACK, and EXISTING titles (from `top`
   / `status`). Split its `===HYPOTHESIS===` blocks; for each, register it:
   `... add --run-id <id> --file <tmpfile>` (or pipe the block via stdin). Run `tick`.

**2. Reflect.** For each newly added hypothesis, invoke the `reflection` subagent
   (parallelize). Parse `VERDICT`; record it:
   `... review --run-id <id> --id hNNN --verdict <pass|reject> --note "<NOTE>"`.
   Run `tick` per call. Rejected hypotheses drop out automatically.

**3. De-duplicate.** If there are several active hypotheses, give the `proximity`
   subagent the `id | title | claim` list. Apply its JSON map:
   `... cluster --run-id <id> --map '<json>'`. Run `tick`.

**4. Rank (tournament).** Get pairs: `... pairs --run-id <id>`. For each pair, invoke the
   `ranking` subagent with both hypotheses + reviews (parallelize). Parse the final
   `WINNER:` line and record: `... match --run-id <id> --a hA --b hB --winner <a|b>`.
   Run `tick` per match. (Winner 1 → `--winner a`, winner 2 → `--winner b`.)

**5. Evolve.** Get the leaders: `... top --run-id <id> --k 3`. Invoke the `evolution`
   subagent with GOAL, FEEDBACK, and those top hypotheses. Register each new variant with
   `add ... --parent <ids>`. Run `tick`. New variants will be reviewed and will compete
   next round.

**6. Meta-review (feedback).** Invoke the `meta-review` subagent in `feedback` mode with
   the round's reviews and debate notes. Save its output:
   `... feedback --run-id <id> --file <tmpfile>`. Run `tick`. This steers the next round.

After each round, show the user a short standings table from `status` (top 5 by Elo) and
ask whether to run another round, adjust the budget, or finish.

## Finish
Invoke the `meta-review` subagent in `overview` mode with the top hypotheses (use
`top --k 5`). Write its markdown to `runs/<id>/research_overview.md` and present it.
Remind the user: **Elo is self-evaluated, not ground truth — treat the output as leads
to validate, not conclusions.**

## Notes
- Subagents do not inherit this skill or each other's context; everything they need must
  be in the prompt you send them. That isolation is the point — their search noise stays
  out of your context.
- If a subagent's output doesn't match its contract (e.g. no `WINNER:` line), re-invoke it
  once with a reminder of the exact format before giving up.
- All hypotheses persist as files under `runs/<id>/hypotheses/`; the run is restart-safe.

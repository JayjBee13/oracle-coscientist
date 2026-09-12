---
name: coscientist
description: Codex-native supervisor instructions for running bounded ai-coscientist engine batches from a GUI prompt package. Use when Codex is asked by Oracle to continue, finish, or validate a v1/v2 co-scientist run while preserving the artifact contract.
---

# Co-Scientist Runtime

Use the prompt package supplied by `Oracle` as the source of truth. Do not infer harness, engine version, grounding mode, budget, rounds, matches, or top-k from memory when the prompt package provides them.

## Rules

- Work only inside the supplied `engine_root`.
- Write artifacts only under `runs/<engine_run_id>/`.
- Do not mutate historical runs unless `engine_run_id` explicitly names the run to continue.
- Produce `state.json` and referenced hypothesis files before reporting success.
- Produce a machine-readable status line or `status.json` with `engine_run_id`, `phase`, `rounds_completed`, and `stop_reason`.
- For `grounding.mode = built_in_web`, do not call Perplexity tools.
- For `grounding.mode = perplexity`, use Perplexity only when the prompt package says it is available or fallback is allowed.
- Stop at the requested round, budget, or stop-after-current-round boundary.

## Success Criteria

The run is successful only when the artifacts normalize into the GUI run DTO:

- `runs/<engine_run_id>/state.json` exists and parses.
- Every hypothesis file referenced by state exists under `engine_root`.
- v1/v2-specific fields are preserved.
- `research_overview.md` or ranked exports exist only when the mode asks for finishing output.

If artifacts are partial or inconsistent, report failure and leave the latest readable files in place for recovery.

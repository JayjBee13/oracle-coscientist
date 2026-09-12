# Codex Supervisor Prompt

Use this prompt with `codex exec --json` after rendering the GUI prompt package.

1. Read the prompt package JSON.
2. Change to the supplied `engine_root`.
3. Use the supplied `engine_run_id` or create exactly that run directory.
4. Run only the bounded batch described by `batch`.
5. Emit JSONL process events when meaningful work starts, artifacts change, controls are observed, and the batch exits.
6. Write `runs/<engine_run_id>/state.json` and the artifact contract described in `prompts/shared/artifact-contract.md`.
7. Never claim success without valid artifacts.

Grounding:

- `built_in_web`: use built-in web search/fetch only.
- `perplexity`: use Perplexity for broad cited search, then fetch/read selected links, unless the prompt package allows fallback.

Resume:

- Resume only with a trusted Codex session id from the process record.
- If resume metadata is missing or untrusted, launch a fresh bounded batch using the saved prompt package and latest artifacts.

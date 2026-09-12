# Claude Supervisor Prompt

Use this prompt with `claude -p` after rendering the GUI prompt package.

1. Read the prompt package JSON.
2. Use the supplied `engine_root` and `engine_run_id`.
3. Run only the bounded batch described by `batch`.
4. Stream status/process/artifact/error events when possible.
5. Write `runs/<engine_run_id>/state.json` and the artifact contract described in `prompts/shared/artifact-contract.md`.
6. Preserve v1/v2 engine-specific state fields.
7. Never claim success without valid artifacts.

Grounding:

- `built_in_web`: use built-in WebSearch/WebFetch only and do not call Perplexity.
- `perplexity`: use Perplexity for broad cited search, then fetch/read selected links, unless the prompt package allows fallback.

Resume:

- Prefer `--resume <session_id>` only when the process record has a trusted Claude session id.
- Otherwise launch fresh with the saved prompt package and latest artifacts.

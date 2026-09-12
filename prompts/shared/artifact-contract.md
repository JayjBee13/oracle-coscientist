# Artifact Contract

Every harness must produce artifacts that normalize into the same GUI run DTO.

Required for co-scientist batches:

- `runs/<engine_run_id>/state.json`
- Hypothesis markdown files referenced by `state.json`
- Machine-readable `status_line` or `status.json` with `engine_run_id`, `phase`, `rounds_completed`, and `stop_reason`

Required for finish mode:

- `runs/<engine_run_id>/research_overview.md`
- Ranked exports when helper scripts are available

Validation rules:

- Artifact paths must stay under the supplied engine root.
- Grounding behavior must match the prompt package before artifacts are marked successful.
- Existing historical run directories are read-only unless selected for continuation.
- The process is not successful until artifacts parse and referenced files exist.
- State and process status must agree on lifecycle before the GUI marks the run complete.

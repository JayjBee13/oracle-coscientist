# Run workdirs

`runs/` is the live working directory root: the supervisor creates one directory per run,
named by `engine_run_id`, and writes that run's projected artifacts there (`state.json`,
`hypotheses/hNNN.md`, `research_overview.md`, `ideas_ranked.csv`, plus the supervisor log
and the recorded spawn argv). It is configured by `COSCIENTIST_RUNS_ROOT`.

The directory has to be named `runs` and sit under a parent directory, because the
projection resolves a run's engine root as this directory's parent.

Nothing here is a source of truth. The database holds the run state; these files are a
projection of it, kept so the artifacts stay readable and portable outside the app.

The two engine checkouts that used to live beside this directory (`v1/`, `v2/`) were
copies of the original standalone co-scientist scripts. The Python orchestrator replaced
them and they were deleted in the overhaul; their source and their runs are preserved
byte-exact under `archive/`.

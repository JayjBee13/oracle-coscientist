# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-09-12

First public release.

### Added

- **Adaptive research workflow.** Framing establishes several distinct approaches and a
  dependency map; a deterministic scheduler then chooses exploration, development, evidence
  review or reframing at each checkpoint from the recorded research state, and always
  returns to fresh exploration after targeted work.
- **Novelty injection.** Four rotating creative methods (first principles, assumption
  inversion, structural analogy, recombination); the Cartographer graft, which seeds a
  structure from an unrelated domain when the idea pool measurably collapses, under a
  quorum/window/cooldown rule calibrated against real run history.
- **The earlier tournament workflow**, selectable for comparison: generation, reflection,
  proximity clustering, an Elo tournament with written debates, evolution, and a
  meta-review that steers the next round.
- **Thirteen agent roles**, each a stateless CLI process with an enforced JSON output
  schema and either no tools or web search alone.
- **Two providers, five models, four tiers.** Anthropic (`fable` → Fable 5.1,
  `claude-opus-5`) through the Claude Code CLI and OpenAI (`gpt-6-astra`, `gpt-5.6-sol`,
  `gpt-5.6-luna`) through the Codex CLI, with per-role model and effort overrides that may
  mix providers within a single run, and a substitution guard that compares the model a
  call actually ran with the one requested.
- **Configurable depth**: rounds, generation batch, matches per round, evolve top-k,
  grounding depth, graft parameters, and three presets (Quick look, Standard, Deep).
- **Two hard ceilings** — call count and optional dollars — enforced in the store and
  re-checked at the spawn layer, plus an optional wall-clock ceiling that writes the report
  before it ends the run.
- **Live run view** over Server-Sent Events, backed by an append-only `run_events` table
  with `LISTEN`/`NOTIFY`: pause, resume, note (which becomes top-priority guidance next
  round), archive a hypothesis, extend a finished run in place, clone a run's setup, stop,
  and a global stop-all.
- **Free demo runner**: a scripted model that produces a complete run — hypotheses,
  reviews, clusters, a tournament with real Elo movement, a report — with no network calls
  and no cost.
- **Prompt workshop** that turns a rough question into two framed research goals to choose
  between, merge or edit.
- **Artifact projection** to disk (`state.json`, `hypotheses/hNNN.md`,
  `research_overview.md`, `ideas_ranked.csv`) so results stay readable outside the app.
- **Run comparison** analytics between any two runs.
- **Private workspaces** with optional gateway-supplied identity and an administrator role.
- **Containerised deployment**: `docker compose up` brings up PostgreSQL, the API serving
  the built frontend, and nginx, with the application running read-only, non-root, with
  `cap_drop: ALL` and isolated CLI credentials.
- **Three verification gates**: a read-only safety doctor, a free full-stack gate including
  a Playwright walkthrough of a demo run, and a guarded real-model smoke test.
- CI running ruff, the backend suite against a real PostgreSQL service, and the frontend
  lint, typecheck, test, build and OpenAPI no-diff gate.

### Notes

- This repository ships no run archive. A fresh install opens on an empty run list;
  `IMPORT_ON_STARTUP` defaults to off, and the demo runner is the intended first
  experience.
- `archive/engine-source/` holds the project's original v1 and v2 engines and their role
  prompts, kept byte-exact because a test diffs today's prompts against them.

[Unreleased]: https://github.com/JayjBee13/oracle-coscientist/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JayjBee13/oracle-coscientist/releases/tag/v0.1.0

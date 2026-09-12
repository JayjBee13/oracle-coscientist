# Contributing to Oracle

Thanks for taking an interest. This document is short, but the rules in it are load-bearing:
most of them exist because ignoring them broke something real.

## Before you start

For anything larger than a bug fix, **open an issue first** and describe the change. Oracle
is a research engine with a deliberate architecture, and the fastest way to have a pull
request declined is to arrive with a rewrite of the orchestrator nobody asked for.

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).

## Setting up

See [Install](README.md#install). In short:

```bash
cp .env.example .env                          # point DATABASE_URL at a local PostgreSQL 17
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
cd ../frontend && npm install
```

## The rules that are not negotiable

These are engine invariants. A pull request that weakens one will be declined regardless of
how well it is written.

1. **The model never gets a shell.** No Bash, Read, Write, Edit or Task tool reaches a role
   call. Judging roles run with `--tools ""`; grounded roles get `--tools "WebSearch"` and
   nothing else.
2. **`--allowedTools` must mirror `--tools`.** A permission denial is fixed by correcting
   `--allowedTools` — never by weakening the permission mode. `bypassPermissions` is not an
   option anywhere in this codebase.
3. **Every role call passes `--strict-mcp-config`**, so the host operator's own MCP
   connectors cannot appear inside a research call.
4. **WebFetch is never used.** It reaches loopback and the local network.
5. **The loop stays in Python.** Roles return data; the orchestrator decides what happens
   next. A change that lets a model choose the next step will be declined.
6. **One database.** The application owns `ai_coscientist_gui` as `ai_coscientist_gui_app`
   and asserts it. Tests get a throwaway schema and never fall back to `public`.
7. **`archive/` is immutable.** A test diffs today's role prompts against the archived
   originals, proving the passages that had to survive the port still say what they said.
   If your change makes that test fail, the prompt change is the thing to reconsider.
8. **Model ids are pinned, and substitution is checked.** There is deliberately no fallback
   model: a silent downgrade mid-tournament poisons Elo with verdicts from a different
   judge. Read `backend/app/engine/models.py` before touching anything about models.

## Code style

- **Read the module docstring before changing a module.** Engine modules carry long
  docstrings explaining why they are the way they are. If your change contradicts one,
  update the docstring in the same commit, with the reason.
- Python: `ruff check .` must pass (line length 100). Type annotations on public functions.
- TypeScript: `npm run lint` with zero warnings, and `npm run typecheck` must pass.
- The API contract is generated, not hand-written. After changing a route or a DTO, run
  `npm run gen:api` in `frontend/` and commit the result; CI fails on a diff.

## Tests

```bash
# backend, from ./backend
.venv/bin/python -m pytest

# frontend, from ./frontend
npm run lint && npm run typecheck && npm test && npm run build
```

New behaviour needs a test. Engine changes need one that does not depend on a model: the
`FakeRunner` and the demo runner exist so that the whole loop can be exercised for free.

Before opening a pull request that touches the engine, run the free full gate:

```powershell
powershell -File scripts/smoke/fake_full_stack.ps1
```

It runs the safety doctor, both test suites, lint, the build, the OpenAPI no-diff check and
a Playwright walkthrough of a demo run. It costs nothing.

## Commits and pull requests

- One logical change per pull request. Describe **what** changed and **why** — the why is
  the part reviewers cannot reconstruct.
- Say how you verified it. "Tests pass" is weaker than "added
  `test_low_tier_pins_luna_across_providers`; full gate green".
- If you changed a prompt asset, say which passage and what it does to a run.

## Reporting bugs

Use the issue templates. For anything involving a real run, include the run's config
(rounds, batch, tier, provider, overrides), what the resolved model table said, and the
relevant `run_events` — never paste credentials or the contents of your `.env`.

Security issues do **not** go in the issue tracker: see [`SECURITY.md`](SECURITY.md).

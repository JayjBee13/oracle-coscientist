## What and why

<!-- What changes, and the reason. The reason is the part a reviewer cannot reconstruct. -->

## How it was verified

<!-- Commands run and what they said. "Tests pass" is weaker than naming the test you added. -->

- [ ] `ruff check .` (from `backend/`)
- [ ] `python -m pytest` (from `backend/`)
- [ ] `npm run lint && npm run typecheck && npm test && npm run build` (from `frontend/`)
- [ ] `npm run gen:api` re-run and committed, if a route or DTO changed
- [ ] `scripts/smoke/fake_full_stack.ps1` — free; required for engine changes

## Engine invariants

<!-- Delete this section if the change does not touch the engine. -->

- [ ] No role gained a tool; `--allowedTools` still mirrors `--tools`
- [ ] `--strict-mcp-config` is still passed on every call
- [ ] The orchestrator still decides the next step; roles still only return data
- [ ] Nothing under `archive/` changed, and the prompt-diff test still passes
- [ ] No new model id bypasses the allowlist or the substitution guard

## Notes for the reviewer

<!-- Trade-offs, alternatives you rejected, follow-ups you deliberately left out. -->

# Security policy

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private reporting:

> **Security** → **Report a vulnerability** on this repository
> (https://github.com/JayjBee13/oracle-coscientist/security/advisories/new)

Please include what you can of: affected version or commit, the component (engine, API,
frontend, container), reproduction steps, and the impact you believe it has. If you have a
proof of concept, describe the class of problem rather than attaching a working exploit.

You can expect an acknowledgement within 7 days and an assessment within 30. Fixes are
released as a normal version bump with the advisory published at the same time. If you would
like credit, say so and how you want to be named.

## Supported versions

Oracle is pre-1.0. Only the `main` branch receives fixes.

## What is in scope

- Anything that lets a research role escape its confinement: gaining a tool it was not
  granted, reading or writing outside its run workdir, reaching the host network, or
  causing a process to be spawned with arguments it did not supply.
- Anything that defeats the run budget ceilings, which exist to stop a loop from spending
  without bound.
- Authentication and workspace isolation: reading another user's runs, reports, events or
  workshop history; forging gateway identity; escalating to administrator.
- Secret exposure: credentials in spawn arguments, logs, events, API responses or the
  projected artifact files.
- Standard web issues in the API or frontend — injection, SSRF, path traversal, CSRF on a
  state-changing route, XSS in rendered model output.

## What is out of scope

- **The content of model output.** Oracle reports what models produce, with their caveats.
  A wrong, biased or confidently-stated-but-unsupported hypothesis is a research-quality
  issue, not a vulnerability — the README says so in its first paragraphs, and the report
  format is built around saying it too.
- Vulnerabilities in the Claude Code CLI, the Codex CLI, or the model providers' services.
  Report those to the respective vendors.
- Anything requiring an operator to deliberately weaken a documented protection — running
  with `bypassPermissions`, exposing the container port to a hostile network, disabling
  `--strict-mcp-config`, or granting a role a tool it does not ship with.
- Denial of service achieved by configuring an enormous run. The ceilings are configurable
  on purpose.

## The protections you are testing against

These are asserted in code and in the smoke gates, and a bypass of any one of them is a
valid report:

| Protection | Where |
| --- | --- |
| Roles get `--tools ""` or `--tools "WebSearch"`, and `--allowedTools` mirrors it | `backend/app/engine/runners.py` |
| Every call passes `--strict-mcp-config`; WebFetch is never available | `backend/app/engine/claude_runner.py` |
| One process per role call, no session, no shell, no filesystem | `backend/app/engine/spawn.py` |
| `://` is rejected in spawn argv; no argv may contain a secret | `backend/app/engine/spawn.py` |
| Writes are confined to the run's own workdir | `backend/app/services/runs/paths.py` |
| Two budget ceilings, enforced in the store and re-checked at spawn | `backend/app/engine/store.py`, `spawn.py` |
| The model a call actually ran is compared with the one requested | `backend/app/engine/models.py` |
| A supplied but wrong gateway secret fails closed, never downgrading to local identity | `backend/app/core/identity.py` |
| Container runs read-only, non-root, `cap_drop: ALL`, `no-new-privileges` | `compose.yaml` |

`scripts/smoke/safety_doctor.ps1` re-checks several of these on demand, including that no
recorded spawn argv contains a secret.

## Operator guidance

- Keep `docker/.env` and `.env` out of version control; both are gitignored.
- Oracle ships bound to loopback. Do not publish it to a network without an authenticating
  reverse proxy in front — see `docs/reverse-proxy-sso.md`.
- Sign the CLIs in inside the container's own home volume rather than mounting your host
  credentials.
- Treat model output as untrusted input when you paste it elsewhere.

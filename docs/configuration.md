# Configuration

Two layers: **environment**, which configures the installation, and **run config**, which
configures a single piece of research.

## Environment

Loaded from `.env` in the repository root by `backend/app/core/config.py` (a
pydantic-settings model). Copy `.env.example` and edit. Every key has a working default
except the database.

| Key | Default | What it does |
| --- | --- | --- |
| `DATABASE_URL` | local placeholder | The one database this app owns. Must name `ai_coscientist_gui` and connect as `ai_coscientist_gui_app` — the migrations and the tests both assert it, so a mistyped DSN cannot point the engine at another database. |
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8787` | Where the API binds. |
| `FRONTEND_ORIGIN` | `http://127.0.0.1:5173` | CORS origin for the dev server. |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8787/api` | What the browser client calls. |
| `VITE_GATEWAY_URL` | `/` | Where "sign in" points when a reverse proxy fronts the app. |
| `WEB_DIST_DIR` | `frontend/dist` | The built frontend the API serves, when one exists. A backend with no build beside it serves the API alone and says so once in the log. |
| `COSCIENTIST_RUNS_ROOT` | `engines/runs` | Where run artifacts are written, one directory per run. **Must** be named `runs` and sit under an engine root: the projection resolves a run's engine root as this directory's parent. |
| `IMPORT_ON_STARTUP` | `true` in code, `false` in `.env.example` | Import an archived run corpus on boot. This repository ships none, so leave it off unless you have one. |
| `IMPORT_ARCHIVE_ROOT` | `archive/imported-runs` | Where that corpus lives, if you have one. |
| `FAKE_HARNESS_ENABLED` | `true` | The free scripted runner. Leave it on. |
| `REAL_HARNESS_ENABLED` | `false` | Must be `true` before a run may call a real model. Real runs are opt-in by design. |
| `COSCIENTIST_DEMO_LATENCY` | `0` | Seconds of delay per call in a demo run. `0` finishes instantly; `2` or `3` lets you watch the live view fill up, practise pausing, or take screenshots. Read straight from the environment by the supervisor. |
| `SSE_HEARTBEAT_SECONDS` | `15` | How long the event stream stays silent before a keep-alive comment. |
| `APP_AUTH_TOKEN` | unset | When set, every route except `/api/health` requires it in an `X-Coscientist-Token` or `Authorization: Bearer` header. It does **not** gate the built page or its assets — a shell that 401s is a shell nobody can open in order to supply the token. |
| `LOCAL_IDENTITY_USERNAME` | `operator` | Who every request is, when gateway identity is not required. |
| `IDENTITY_TRUST_HEADERS` | `true` | Whether upstream identity headers are read at all. |
| `IDENTITY_REQUIRE_GATEWAY` | `false` | Strict mode: identity must come from a verified gateway. Requires `GATEWAY_SECRET`; an invalid combination refuses to start. |
| `IDENTITY_ADMIN_GROUP` | `admin` | The group name that grants administrator visibility. |
| `GATEWAY_SECRET` | unset | The server-held value a trusted proxy injects as `X-Gateway-Secret`. A supplied but wrong secret always fails closed; it never falls back to local identity. |
| `MANAGE_SUPERVISORS_ON_SHUTDOWN` | `false` | Containers set this true: shutdown requests a pause, waits the grace period, then terminates verified process trees and parks their runs for Resume. |
| `SUPERVISOR_SHUTDOWN_GRACE_SECONDS` | `10` | That grace period. |

Docker deployments read `docker/.env` instead; see
[`docker-deployment.md`](docker-deployment.md).

## Run configuration

Set in the wizard, or as the `config` block of `POST /api/runs`. Validation here is
deliberately thin — bounds a person could plausibly type wrong — because the launcher
normalises everything through the engine's own `RunConfig`.

| Key | Range | Default | Effect |
| --- | --- | --- | --- |
| `workflow` | `adaptive` \| `tournament` | `adaptive` | Which loop runs. Unversioned historical runs keep their tournament behaviour on resume. |
| `rounds` | 1–25 | 5 | Checkpoints. Each is one scheduler decision and the work it implies. |
| `generation_batch` | 1–40 | 8 | Hypotheses proposed per exploration round, split across parallel calls. |
| `matches_per_round` | 0–100 | 6 | Tournament pairings judged per round. |
| `evolve_top_k` | 0–25 | 3 | How many survivors development derives from. |
| `grounding_depth` | `shallow` \| `standard` \| `deep` | `standard` | How hard grounded roles verify. `deep` instructs them to verify every major claim independently, and moves their timeout ceiling accordingly. |
| `provider` | `anthropic` \| `openai` | system default | Fills the rows you do not override. Stated explicitly it wins for this run alone and changes nothing about the default. |
| `model_tier` | `max` \| `high` \| `med` \| `low` | system default | Thinking effort, and — for `low` — the model pin. |
| `model_overrides` | per role | system default | `{role: {model?, effort?}}` for any of the thirteen roles. `{}` means *no* overrides; `null` inherits the system default's. |
| `budget_calls` | 1–10 000 | 150 | The governor. Enforced in the store and re-checked at spawn. |
| `budget_usd` | > 0 | off | Optional dollar ceiling, for a metered key. |
| `wall_clock_minutes` | > 0 | off | Optional elapsed-time ceiling. Ends the run the way `finish` does — the report is written first. |
| `graft.enabled` | bool | `false` | Novelty injection: the Cartographer graft. |
| `graft.quorum_k` | 1–3 | 3 | How many collapse signals must agree before it fires. |
| `graft.window` | 1–10 | 3 | Over how many rounds they are counted. |
| `graft.cooldown` | 0–10 | 2 | Rounds before it may fire again. |

### Presets

| Preset | Shape | Grounding |
| --- | --- | --- |
| Quick look | 1 round, 3 hypotheses, 2 matches | shallow |
| Standard | 3 rounds, 6 hypotheses, 4 matches | standard |
| Deep | 5 rounds, 8 hypotheses, 6 matches | deep |

These come from the same estimator the engine uses, so the numbers on the Confirm step are
not decorative: it shows the resolved per-role model table, the estimated call count, the
dollar-equivalent and wall-clock estimates, and both hard ceilings, before you launch.

### Context documents

Up to five files of `.md`, `.txt`, `.csv` or `.json`, 200 KB each. They are inlined into the
generation, reflection, evolution and cartographer prompts, and they count toward the
prompt's character cap.

### Changing a run after launch

A run's config is immutable after launch, with one sanctioned exception: **`continue`**
extends a `completed` or `stopped` run in place. It takes `add_rounds` and optionally a
raised `budget_calls`, keeps every hypothesis, Elo, match, cluster, note and graft state, and
opens the next round on the previous round's meta-review guidance. It may reach `rounds` and
`budget_calls` and nothing else, and it writes a `run_extended` event recording both sides of
both numbers.

Do not confuse it with **`from_run`**, which copies a run's *setup* into a new run that
starts from no hypotheses — that starts the science over.

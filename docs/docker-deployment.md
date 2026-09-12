# Docker deployment

`docker compose up` brings up four things: PostgreSQL, the application (FastAPI serving both
`/api/*` and the built frontend), and an nginx front end. The image contains the built React
frontend, the backend, and pinned versions of both CLIs — Claude Code 2.1.263 and Codex CLI
0.153.4. The Node, Python and nginx base images are pinned by digest.

```bash
cp docker/.env.example   docker/.env    # the application
cp docker/db.env.example docker/db.env  # the bundled database
docker compose up -d --build
docker compose run --rm --no-deps app alembic upgrade head
```

Both files work as shipped, so a first run needs no edits. The first build installs both
CLIs and builds the frontend, so allow several minutes.

Open **http://localhost:18000**. Health, including a real database query and the installed
CLI versions, is at **http://localhost:18001/api/health**.

Both ports bind to loopback only. Publishing Oracle to a network is a deliberate act — put
an authenticating reverse proxy in front of it first, and read
[`reverse-proxy-sso.md`](reverse-proxy-sso.md).

## Configure

`docker/.env` holds credentials and is ignored by both Git and the Docker build.

- Two files, both working as shipped: `docker/.env` for the application and `docker/db.env`
  for the bundled database. They are separate so the application's secrets are never
  injected into the database container.
- `DATABASE_URL` must name database `ai_coscientist_gui` and role `ai_coscientist_gui_app`;
  the application's own checks reject anything else. The bundled `db` service creates both
  from `docker/init-db.sh`, **once, on an empty volume** — so `APP_DB_PASSWORD` in
  `docker/db.env` and the password in `DATABASE_URL` must agree, and must be set before the
  first `up`. To start over: `docker compose down && docker volume rm oracle_db`.
- Note what does *not* work: Compose `${...}` interpolation reads the invoking shell, never a
  service's `env_file`. A variable the compose file interpolates cannot be supplied from
  `docker/.env` alone — use `docker compose --env-file docker/.env ...` for those.
- To use an existing PostgreSQL instead, point `DATABASE_URL` at it and start only the two
  services that need it: `docker compose up -d app proxy`.
- Set `REAL_HARNESS_ENABLED=true` only when real subscription-backed model calls are
  intended.
- Compose fixes every container path, so do not carry host `COSCIENTIST_RUNS_ROOT`,
  `IMPORT_ARCHIVE_ROOT` or `WEB_DIST_DIR` values into deployment configuration.

Validate the resolved configuration without printing its values:

```bash
docker compose config --quiet
```

## Migrate

The application does **not** run Alembic during startup. Applying migrations is an explicit
database write; take a backup first when deploying a new one.

```bash
docker compose run --rm --no-deps app alembic upgrade head
```

## Sign the CLIs in

The application invokes both CLIs as the unprivileged `oracle` user (uid 10001). Their
writable home is the `oracle_home` named volume, so authentication survives image rebuilds
and container replacement:

```bash
docker compose exec app claude login
docker compose exec app codex login
```

On Windows, `docker/bootstrap-credentials.ps1` can instead copy an existing host login into
that volume. It is deliberately narrow — Claude's `claudeAiOauth` object and the two
onboarding flags, Codex's `auth_mode`, `tokens` and `last_refresh`, plus a minimal Codex
config — and it omits MCP OAuth tokens, API keys, user instructions, trusted projects,
plugins and host CLI settings. It sanitises each document in memory, streams it over stdin
to a one-off container running as the unprivileged user, creates no plaintext temporary
files, and prints nothing.

Vendor documentation for the credential locations it reads:
[Claude](https://code.claude.com/docs/en/authentication#credential-management),
[Codex](https://developers.openai.com/codex/auth#login-on-headless-devices).

## What is mounted, and what is not

The image never receives `.env`, `engines/runs/`, logs, frontend output or either dependency
tree as build context. At runtime:

| Mount | Mode | Why |
| --- | --- | --- |
| `./engines` → `/app/engines` | read-write | Run artifacts stay on the host and survive the container. |
| `oracle_db` → `/var/lib/postgresql/data` | volume | Database storage. Survives `down`; removing it destroys the runs. |
| `oracle_home` → `/home/oracle` | volume | The two CLI credential stores and their token refreshes. |
| `oracle_runtime` → `/app/.dev` | volume | Workshop and demo scratch state. |
| `./docker/nginx.conf` | read-only | Proxy configuration. |

The application's root filesystem is **read-only**. Both services run with no Linux
capabilities (`cap_drop: ALL`), `no-new-privileges`, an init process, bounded JSON logs and
explicit shutdown grace periods. **No Docker socket is mounted.**

## Start, verify, stop

```bash
docker compose up -d
docker compose ps
curl http://localhost:18001/api/health
docker compose logs --follow app proxy
```

The app health check requires both HTTP readiness and a successful database query. Nginx
waits for the app to be healthy, disables proxy buffering for the SSE event stream, and
accepts bodies up to 16 MiB so five valid context documents still reach FastAPI after JSON
escaping.

```bash
docker compose down
```

A normal stop asks the backend to manage live supervisors, allows ten seconds for the
handoff, and gives the container 45 seconds to exit; uvicorn drains HTTP for at most five of
those, and browsers reconnect SSE normally. Runs that were mid-flight are parked for Resume.

Named volumes survive `down`. **Do not add `--volumes`** unless deleting the CLI
authentication and runtime state is what you intend.

## Tests in the container

The `test` target adds development dependencies and the test suite on top of the production
runtime; both are absent from the production image.

```bash
docker build --target test --tag oracle-coscientist:test .
docker run --rm --env DATABASE_URL oracle-coscientist:test python -m pytest tests
```

Integration tests need a role that can create its own throwaway `test_<hex>` schema — they
never fall back to `public`, because that is where live data lives. The prompt-diff test
needs the archive, which is deliberately absent from every image target, so mount it
read-only when running the full suite:

```bash
docker run --rm --env DATABASE_URL \
  --mount "type=bind,source=$(pwd)/archive,target=/app/archive,readonly" \
  oracle-coscientist:test python -m pytest tests
```

Neither the test target nor the image build ever makes a real model call.

## Lifecycle smokes

Two free black-box smokes exercise the container itself. Each creates its own
`smoke_<8hex>` schema through the guarded schema helper, uses a separate container on port
18002 with a disposable volume, and drops exactly what it created in `finally`. Neither
starts, stops or shares state with your running deployment, and neither calls a model.

```bash
python docker/smoke-lifecycle.py     # pause/resume, container restart with same-id resume,
                                     # force-stop process cleanup
python docker/smoke-workspaces.py    # fail-closed gateway auth, origin enforcement,
                                     # per-user isolation, admin visibility
```

`smoke-workspaces.py` generates its own gateway secret and passes it through the process
environment rather than argv, and prints no credentials.

## Platforms

The image supports Linux `amd64` and `arm64`; the npm CLI packages select their own platform
dependency at install time. It has been exercised on Docker Desktop's Linux `amd64` engine.
Build and smoke-test on the target architecture before relying on a cross-platform image.

## One backend per database

Only one Oracle backend may manage a given database. If you also run the application
directly on the host — a service, a scheduled task, or a bare `uvicorn` — stop it before
starting Compose. Two backends managing the same runs concurrently is not a supported
configuration.

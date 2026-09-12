# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:24-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/tsconfig.app.json frontend/tsconfig.node.json frontend/vite.config.ts ./
COPY frontend/public ./public
COPY frontend/src ./src
RUN npm run build

FROM ${NODE_IMAGE} AS cli-build
ARG CLAUDE_CODE_VERSION=2.1.263
ARG CODEX_VERSION=0.153.4
RUN npm install --global --omit=dev --no-audit --no-fund \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
    && test "$(claude --version | awk '{print $1}')" = "${CLAUDE_CODE_VERSION}" \
    && test "$(codex --version | awk '{print $2}')" = "${CODEX_VERSION}" \
    && npm cache clean --force

FROM ${PYTHON_IMAGE} AS runtime
ARG CLAUDE_CODE_VERSION=2.1.263
ARG CODEX_VERSION=0.153.4

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/oracle \
    APP_HOST=0.0.0.0 \
    APP_PORT=8787 \
    WEB_DIST_DIR=/app/frontend/dist \
    COSCIENTIST_RUNS_ROOT=/app/engines/runs \
    IMPORT_ARCHIVE_ROOT=/app/archive/imported-runs

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 oracle \
    && useradd --uid 10001 --gid oracle --create-home --shell /usr/sbin/nologin oracle

COPY --from=cli-build /usr/local/bin/node /usr/local/bin/node
COPY --from=cli-build /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex

WORKDIR /app/backend
COPY docker/backend-constraints.txt /app/docker/backend-constraints.txt
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN python -m pip install --constraint /app/docker/backend-constraints.txt . \
    && test "$(claude --version | awk '{print $1}')" = "${CLAUDE_CODE_VERSION}" \
    && test "$(codex --version | awk '{print $2}')" = "${CODEX_VERSION}"

COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist

RUN mkdir -p /app/engines/runs /app/archive/imported-runs /app/.dev \
      /home/oracle/.claude /home/oracle/.codex \
    && chown -R oracle:oracle /app /home/oracle

USER oracle
EXPOSE 8787
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--timeout-graceful-shutdown", "5"]

FROM runtime AS test
USER root
COPY backend/tests ./tests
COPY .agents /app/.agents
COPY prompts /app/prompts
RUN python -m pip install --constraint /app/docker/backend-constraints.txt ".[dev]"
USER oracle
CMD ["python", "-m", "pytest", "tests"]

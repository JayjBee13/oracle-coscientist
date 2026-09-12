"""A throwaway Postgres schema for the browser smoke to boot its backend against.

The smoke asserts that the archived historical runs render, so its backend has to boot
with `IMPORT_ON_STARTUP=true`. The owner's live database no longer holds those runs and
its `.env` keeps the flag off on purpose, so importing them into `public` is not an
option. This creates `smoke_<8hex>` on the same database, migrates it with Alembic, and
hands back a `DATABASE_URL` pinned to it; `drop` removes it afterwards.

This is `backend/tests/conftest.py`'s isolation, reshaped for a script that hands the URL
to another process instead of holding it in a fixture. As there, `public` is deliberately
left off the search path and there is no fallback to it.

Run it with the backend virtualenv's python:

    backend\\.venv\\Scripts\\python.exe scripts\\smoke\\_smoke_schema.py create
    backend\\.venv\\Scripts\\python.exe scripts\\smoke\\_smoke_schema.py drop smoke_1a2b3c4d

`create` prints the URL as its only stdout line; everything else goes to stderr, so the
caller can capture stdout wholesale.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import secrets
import sys
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
ENV_FILE = REPO_ROOT / ".env"
EXPECTED_DATABASE = "ai_coscientist_gui"

# The only schemas this script will ever drop. `public`, the owner's live data, cannot
# match — nor can the test suite's `test_<8hex>` schemas.
SCHEMA_NAME = re.compile(r"^smoke_[0-9a-f]{8}$")

GRANT_INSTRUCTION = """
The browser smoke needs to create a throwaway schema and the app role cannot.
Run this once as a Postgres superuser, then re-run the smoke:

    GRANT CREATE ON DATABASE ai_coscientist_gui TO ai_coscientist_gui_app;

The smoke will NOT fall back to the public schema — that would import 15 archived
runs into live data.
"""


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _database_url() -> str:
    """The admin DSN, read from `.env` rather than the environment.

    The caller exports its own `DATABASE_URL` once this script hands one back, and a
    leaked one from an interrupted run must not become the DSN the next schema is
    created on.
    """
    if not ENV_FILE.exists():
        _fail(f"{ENV_FILE} not found")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("DATABASE_URL="):
            value = stripped.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    _fail(f"DATABASE_URL is not set in {ENV_FILE}")


def _with_search_path(database_url: str, schema: str) -> str:
    """Pin every connection made through this URL to `schema` alone."""
    parts = urlparse(database_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunparse(parts._replace(query=urlencode(query)))


def _connect(database_url: str) -> Engine:
    engine = create_engine(database_url, poolclass=NullPool)
    with engine.connect() as conn:
        database = conn.execute(text("select current_database()")).scalar_one()
        if database != EXPECTED_DATABASE:
            engine.dispose()
            _fail(f"Refusing to run: connected to {database!r}, expected {EXPECTED_DATABASE!r}.")
    return engine


def _drop_schema(engine: Engine, schema: str) -> None:
    with engine.connect() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        conn.commit()


def _migrate(database_url: str) -> None:
    """Bring the new schema to head, exactly as the app would find it in production."""
    os.environ["DATABASE_URL"] = database_url
    sys.path.insert(0, str(BACKEND_ROOT))
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # Alembic logs to stderr, but a migration is free to print; stdout belongs to the URL.
    with contextlib.redirect_stdout(sys.stderr):
        command.upgrade(config, "head")


def create() -> None:
    admin_url = _database_url()
    engine = _connect(admin_url)
    try:
        with engine.connect() as conn:
            may_create = conn.execute(
                text("select has_database_privilege(current_user, current_database(), 'CREATE')")
            ).scalar_one()
        if not may_create:
            _fail(GRANT_INSTRUCTION)

        schema = f"smoke_{secrets.token_hex(4)}"
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.commit()
        print(f"created schema {schema}", file=sys.stderr)

        database_url = _with_search_path(admin_url, schema)
        try:
            _migrate(database_url)
        except Exception:
            _drop_schema(engine, schema)
            print(f"dropped schema {schema} after a failed migration", file=sys.stderr)
            raise
    finally:
        engine.dispose()

    print(database_url)


def drop(schema: str) -> None:
    if not SCHEMA_NAME.match(schema):
        _fail(f"Refusing to drop {schema!r}: only smoke_<8hex> schemas may be dropped.")
    engine = _connect(_database_url())
    try:
        _drop_schema(engine, schema)
    finally:
        engine.dispose()
    print(f"dropped schema {schema}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="create and migrate a throwaway schema; print its URL")
    dropper = commands.add_parser("drop", help="drop a throwaway schema")
    dropper.add_argument("schema", help="the smoke_<8hex> schema to drop")

    args = parser.parse_args()
    if args.command == "create":
        create()
    else:
        drop(args.schema)


if __name__ == "__main__":
    main()

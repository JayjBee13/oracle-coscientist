"""Every test session runs against its own throwaway Postgres schema.

The suite used to run against the owner's live `public` schema and delete rows from it
directly. Here we create `test_<8hex>`, migrate it with Alembic (so triggers and server
defaults exist exactly as in production), point `DATABASE_URL` at it for the whole
session, and drop it afterwards.

There is deliberately no fallback to `public`: if the app role cannot create a schema the
session aborts with the GRANT to run. Falling back would silently resume writing to live
data, which is the failure mode this file exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import Settings, get_settings
from app.db.session import reset_engine_cache

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_DATABASE = "ai_coscientist_gui"


@pytest.fixture(autouse=True)
def background_test_children(monkeypatch):
    """Test probes and helper trees must not open consoles or steal the owner's focus."""
    if os.name != "nt":
        return
    original_popen = subprocess.Popen
    original_async = asyncio.create_subprocess_exec

    def hidden_popen(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return original_popen(*args, **kwargs)

    async def hidden_async(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return await original_async(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", hidden_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", hidden_async)

GRANT_INSTRUCTION = """
The test suite needs to create a throwaway schema and the app role cannot.
Run this once as a Postgres superuser, then re-run the tests:

    GRANT CREATE ON DATABASE ai_coscientist_gui TO ai_coscientist_gui_app;

The suite will NOT fall back to the public schema — that would write to live data.
"""


def _with_search_path(database_url: str, schema: str) -> str:
    """Pin every connection made through this URL to `schema` alone.

    `public` is deliberately left off the path: a query that reaches for a live table
    should fail loudly in tests rather than quietly find one.
    """
    parts = urlparse(database_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunparse(parts._replace(query=urlencode(query)))


@pytest.fixture(scope="session", autouse=True)
def isolated_schema() -> Iterator[str]:
    admin_url = Settings().database_url
    admin_engine = create_engine(admin_url, poolclass=NullPool)

    with admin_engine.connect() as conn:
        database = conn.execute(text("select current_database()")).scalar_one()
        if database != EXPECTED_DATABASE:
            pytest.exit(
                f"Refusing to run: connected to {database!r}, expected {EXPECTED_DATABASE!r}.",
                returncode=1,
            )
        may_create = conn.execute(
            text("select has_database_privilege(current_user, current_database(), 'CREATE')")
        ).scalar_one()
        if not may_create:
            pytest.exit(GRANT_INSTRUCTION, returncode=1)

    schema = f"test_{secrets.token_hex(4)}"
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()

    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _with_search_path(admin_url, schema)
    get_settings.cache_clear()
    reset_engine_cache()

    try:
        alembic_config = Config(str(BACKEND_ROOT / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        command.upgrade(alembic_config, "head")
        yield schema
    finally:
        reset_engine_cache()
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.commit()
        admin_engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()

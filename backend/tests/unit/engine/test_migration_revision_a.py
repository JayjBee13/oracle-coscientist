"""Revision A must be reversible, additive, and refuse the wrong database.

The up/down cycle runs in a schema this module creates and drops itself, so it cannot
disturb the session's migrated schema that every other test shares.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import Settings, get_settings
from app.db.session import reset_engine_cache

BACKEND_ROOT = Path(__file__).resolve().parents[3]
REVISION_A = "20260801_0003"
PREVIOUS_REVISION = "20260603_0002"

ADDED_TABLES = {
    "runs2",
    "hypotheses",
    "reviews",
    "matches",
    "graft_events",
    "budget_ledger",
    "run_events",
    "run_context_docs",
    "workshops",
    "workshop_options",
}
LEGACY_TABLES = {
    "runs",
    "processes",
    "process_events",
    "artifact_index",
    "run_settings",
    "prompt_workshops",
    "prompt_options",
    "comparison_groups",
    "comparison_members",
}


def _with_search_path(database_url: str, schema: str) -> str:
    parts = urlparse(database_url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunparse(parts._replace(query=urlencode(query)))


def _alembic_config() -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return config


@contextmanager
def _scratch_schema() -> Iterator[str]:
    """A second throwaway schema, migrated on request, restored on the way out."""
    base_url = Settings().database_url
    admin_engine = create_engine(base_url, poolclass=NullPool)
    schema = f"test_reva_{secrets.token_hex(4)}"
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()

    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = _with_search_path(base_url, schema)
    get_settings.cache_clear()
    try:
        yield schema
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.commit()
        admin_engine.dispose()
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        get_settings.cache_clear()
        reset_engine_cache()


def _tables(schema: str) -> set[str]:
    engine = create_engine(Settings().database_url, poolclass=NullPool)
    try:
        return set(inspect(engine).get_table_names(schema=schema))
    finally:
        engine.dispose()


def _trigger_exists(schema: str) -> bool:
    engine = create_engine(Settings().database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        """
                        select 1
                        from pg_trigger t
                        join pg_class c on c.oid = t.tgrelid
                        join pg_namespace n on n.oid = c.relnamespace
                        where t.tgname = 'run_events_notify_trigger'
                          and c.relname = 'run_events'
                          and n.nspname = :schema
                        """
                    ),
                    {"schema": schema},
                ).scalar()
            )
    finally:
        engine.dispose()


@pytest.mark.usefixtures("isolated_schema")
def test_revision_a_adds_the_engine_tables_and_can_be_taken_back_out():
    with _scratch_schema() as schema:
        config = _alembic_config()

        # Pinned to Revision A, not head: Revision B is what drops the legacy tables, and
        # this test is about A leaving every one of them alone.
        command.upgrade(config, REVISION_A)
        after_upgrade = _tables(schema)
        assert ADDED_TABLES <= after_upgrade
        assert LEGACY_TABLES <= after_upgrade, "Revision A must not touch the legacy tables"
        assert _trigger_exists(schema)

        command.downgrade(config, PREVIOUS_REVISION)
        after_downgrade = _tables(schema)
        assert not (ADDED_TABLES & after_downgrade), "downgrade must remove everything it added"
        assert LEGACY_TABLES <= after_downgrade
        assert not _trigger_exists(schema)

        command.upgrade(config, REVISION_A)
        assert ADDED_TABLES <= _tables(schema)


def test_revision_a_refuses_a_database_that_is_not_the_apps_own(monkeypatch):
    """The guard is what stops a wrong DSN from reaching the owner's other databases."""
    module = _load_revision_module()

    class _Result:
        def scalar_one(self) -> str:
            return "someone_elses_db"

    class _Bind:
        def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

    monkeypatch.setattr(module.op, "get_bind", lambda: _Bind())

    with pytest.raises(RuntimeError, match="ai_coscientist_gui"):
        module._assert_database_identity()


def _load_revision_module():
    import importlib.util

    path = (
        BACKEND_ROOT / "alembic" / "versions" / f"{REVISION_A}_engine_schema_revision_a.py"
    )
    spec = importlib.util.spec_from_file_location("revision_a_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

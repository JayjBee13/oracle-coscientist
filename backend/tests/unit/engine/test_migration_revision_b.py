"""Revision B moves the owner's prompt workshops before it drops anything.

The eleven `prompt_workshops` rows and their fourteen options are the only data in this
database that exists nowhere else — the runs can be rebuilt from the archive, the owner's
prompt-design work cannot. So the assertions here are not "the tables exist afterwards":
they seed legacy rows covering every status the legacy CHECK constraint allowed, both
`scoring_criteria` shapes that occur in the live data, and the chosen/unchosen split, then
check each migrated field individually.

Everything runs in a scratch schema this module creates and drops itself.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import Settings, get_settings
from app.db.session import reset_engine_cache

BACKEND_ROOT = Path(__file__).resolve().parents[3]
REVISION_A = "20260801_0003"
REVISION_B = "20260801_0004"

# Dropped by Revision B. `runs` is absent because the name survives — the legacy table goes
# and `runs2` takes its place.
DROPPED_TABLES = {
    "processes",
    "process_events",
    "artifact_index",
    "run_settings",
    "prompt_workshops",
    "prompt_options",
    "comparison_groups",
    "comparison_members",
    "runs2",
}

ENGINE_TABLES = {
    "runs",
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
    base_url = Settings().database_url
    admin_engine = create_engine(base_url, poolclass=NullPool)
    schema = f"test_revb_{secrets.token_hex(4)}"
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


@contextmanager
def _connect() -> Iterator[object]:
    engine = create_engine(Settings().database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


def _tables(schema: str) -> set[str]:
    engine = create_engine(Settings().database_url, poolclass=NullPool)
    try:
        return set(inspect(engine).get_table_names(schema=schema))
    finally:
        engine.dispose()


# --- the fixture ------------------------------------------------------------------------
# Shapes taken from the live table: three workshops that reached a chosen prompt, four that
# only ever produced options, four that failed before producing any, and the two
# `scoring_criteria` vocabularies that actually occur.

CHOSEN_ID = UUID("11111111-1111-4111-8111-111111111111")
OPTIONS_ID = UUID("22222222-2222-4222-8222-222222222222")
FAILED_ID = UUID("33333333-3333-4333-8333-333333333333")
DRAFT_ID = UUID("44444444-4444-4444-8444-444444444444")
REFINING_ID = UUID("55555555-5555-4555-8555-555555555555")

FINAL_PROMPT = "What is the primary driver of aging? Answer as a biomedical researcher, edited."

STRUCTURED_CRITERIA = {
    "primary": "Rewards a mechanistic causal hierarchy.",
    "secondary": "Rewards explicit comparison of competing theories.",
    "rejection": "Reject if it treats aging as having one settled cause.",
}
FREEFORM_CRITERIA = {
    "novelty": "must survive a named-competitor sweep",
    "revenue": "must show bottom-up conversion math",
    "why_now": "must depend on a 2024-2026 AI capability",
}


def _seed_legacy(conn) -> None:
    workshops = [
        (CHOSEN_ID, "claude", "what is the primary driver of aging?", "prompt_ready", "B",
         FINAL_PROMPT),
        (OPTIONS_ID, "codex", "what are the primary drivers for aging", "options_ready",
         None, None),
        (FAILED_ID, "claude", "what the major driving cause of aging?", "failed", None, None),
        (DRAFT_ID, "claude", "a question never worked on", "draft", None, None),
        (REFINING_ID, "codex", "a question still being worked on", "refining", None, None),
    ]
    for wid, harness, intent, status, selected, final_prompt in workshops:
        conn.execute(
            text(
                """
                insert into prompt_workshops
                    (id, harness, intent, status, selected_option, final_prompt,
                     created_at, updated_at)
                values (:id, :harness, :intent, :status, :selected, :final_prompt,
                        timestamptz '2026-06-04 12:34:13.804166+00',
                        timestamptz '2026-06-04 12:40:00.000000+00')
                """
            ),
            {
                "id": wid,
                "harness": harness,
                "intent": intent,
                "status": status,
                "selected": selected,
                "final_prompt": final_prompt,
            },
        )

    options = [
        # The chosen workshop: option B was selected, and its stored prompt differs from
        # the scientist's edited final prompt. B must end up holding the edit; A must not.
        (CHOSEN_ID, "A", "Option A prompt as drafted.", STRUCTURED_CRITERIA,
         {"top_k": 6, "budget": 8, "rounds": 3, "grounding_depth": "deep",
          "matches_per_round": 4}),
        (CHOSEN_ID, "B", "Option B prompt as drafted.", STRUCTURED_CRITERIA,
         {"top_k": 5, "budget": 6, "rounds": 2, "grounding_depth": "deep",
          "matches_per_round": 4}),
        # The options-only workshop, carrying the older free-form criteria vocabulary and a
        # legacy `grounding_mode` key that the new four-field contract has no room for.
        (OPTIONS_ID, "A", "Optimize for novelty.", FREEFORM_CRITERIA,
         {"top_k": 3, "budget": 120, "rounds": 4, "grounding_mode": "built_in_web",
          "grounding_depth": "standard", "matches_per_round": 6}),
        (OPTIONS_ID, "B", "Optimize for fast MVP.", {}, {}),
    ]
    for wid, label, prompt, criteria, recommended in options:
        conn.execute(
            text(
                """
                insert into prompt_options
                    (id, workshop_id, label, prompt, scoring_criteria, recommended_settings,
                     created_at)
                values (:id, :workshop_id, :label, :prompt,
                        cast(:criteria as jsonb), cast(:recommended as jsonb),
                        timestamptz '2026-06-04 12:34:13.819238+00')
                """
            ),
            {
                "id": uuid4(),
                "workshop_id": wid,
                "label": label,
                "prompt": prompt,
                "criteria": json.dumps(criteria),
                "recommended": json.dumps(recommended),
            },
        )
    conn.commit()


@pytest.mark.usefixtures("isolated_schema")
def test_revision_b_migrates_every_legacy_workshop_and_option():
    with _scratch_schema() as schema:
        config = _alembic_config()
        command.upgrade(config, REVISION_A)

        with _connect() as conn:
            _seed_legacy(conn)

        command.upgrade(config, "head")

        with _connect() as conn:
            # Nothing is lost: five workshops in, five out; four options in, four out.
            assert conn.execute(text("select count(*) from workshops")).scalar_one() == 5
            assert conn.execute(text("select count(*) from workshop_options")).scalar_one() == 4

            states = dict(
                conn.execute(text("select id, state from workshops")).fetchall()
            )
            assert states[CHOSEN_ID] == "chosen", "prompt_ready is the legacy name for chosen"
            assert states[OPTIONS_ID] == "options_ready"
            assert states[FAILED_ID] == "failed"
            assert states[DRAFT_ID] == "refining", "a draft never got past refining"
            assert states[REFINING_ID] == "refining"

            chosen = conn.execute(
                text(
                    "select question, harness, error, created_at, updated_at "
                    "from workshops where id = :id"
                ),
                {"id": CHOSEN_ID},
            ).one()
            assert chosen.question == "what is the primary driver of aging?"
            assert chosen.harness == "claude", "harness carries over, codex included"
            assert chosen.error is None
            assert chosen.created_at.isoformat().startswith("2026-06-04T12:34:13.804166")
            assert chosen.updated_at.isoformat().startswith("2026-06-04T12:40:00")

            # A workshop that failed before the overhaul says so, rather than rendering as a
            # failure with no reason at all.
            failed_error = conn.execute(
                text("select error from workshops where id = :id"), {"id": FAILED_ID}
            ).scalar_one()
            assert failed_error["code"] == "legacy_failure"
            assert "message" in failed_error

            # The legacy `final_prompt` column has no counterpart: the chosen option's
            # prompt IS the final prompt, exactly as `choose()` writes it today.
            chosen_options = conn.execute(
                text(
                    "select ordinal, prompt, strategy, chosen, rejected, optimizes_for, "
                    "excludes, rationale, recommended_settings, created_at "
                    "from workshop_options where workshop_id = :id order by ordinal"
                ),
                {"id": CHOSEN_ID},
            ).fetchall()
            option_a, option_b = chosen_options
            assert option_a.ordinal == 0 and option_b.ordinal == 1
            assert option_a.strategy == "Option A"
            assert option_b.strategy == "Option B"
            assert option_b.chosen is True and option_a.chosen is False
            assert option_b.prompt == FINAL_PROMPT
            assert option_a.prompt == "Option A prompt as drafted.", (
                "the unchosen option keeps what the model drafted"
            )
            assert option_a.rejected is False
            assert option_a.created_at.isoformat().startswith("2026-06-04T12:34:13.819238")

            # `scoring_criteria` splits three ways under the new contract.
            assert option_a.optimizes_for == STRUCTURED_CRITERIA["primary"]
            assert option_a.excludes == STRUCTURED_CRITERIA["rejection"]
            assert option_a.rationale == STRUCTURED_CRITERIA["secondary"]
            # `budget` is the legacy spelling of `budget_calls`; `top_k` and
            # `grounding_mode` have no place in the frozen four-field shape.
            assert option_a.recommended_settings == {
                "rounds": 3,
                "budget_calls": 8,
                "matches_per_round": 4,
                "grounding_depth": "deep",
            }

            freeform, empty = conn.execute(
                text(
                    "select ordinal, optimizes_for, excludes, rationale, recommended_settings "
                    "from workshop_options where workshop_id = :id order by ordinal"
                ),
                {"id": OPTIONS_ID},
            ).fetchall()
            # The older vocabulary has no primary/secondary/rejection split, so the whole
            # criterion set is preserved as prose rather than thrown away.
            assert "novelty: must survive a named-competitor sweep" in freeform.optimizes_for
            assert "why_now:" in freeform.optimizes_for
            assert freeform.excludes is None and freeform.rationale is None
            assert freeform.recommended_settings["budget_calls"] == 120
            assert freeform.recommended_settings["grounding_depth"] == "standard"
            # An option with nothing recorded gets the Standard preset, not zeros.
            assert empty.recommended_settings == {
                "rounds": 3,
                "budget_calls": 60,
                "matches_per_round": 6,
                "grounding_depth": "standard",
            }
            assert empty.optimizes_for is None

            # No workshop that never produced options invents any.
            assert (
                conn.execute(
                    text("select count(*) from workshop_options where workshop_id = :id"),
                    {"id": FAILED_ID},
                ).scalar_one()
                == 0
            )

        after = _tables(schema)
        assert not (DROPPED_TABLES & after), f"still present: {sorted(DROPPED_TABLES & after)}"
        assert ENGINE_TABLES <= after

        with _connect() as conn:
            # `runs` is the engine's table now, not the legacy one.
            columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "select column_name from information_schema.columns "
                        "where table_schema = :schema and table_name = 'runs'"
                    ),
                    {"schema": schema},
                ).fetchall()
            }
            assert {"question", "lifecycle", "budget_usd", "engine_state"} <= columns
            assert "version" not in columns, "the legacy runs table is gone"


@pytest.mark.usefixtures("isolated_schema")
def test_revision_b_renames_runs2_indexes_and_constraints():
    """A table called `runs` with indexes called `ux_runs2_*` is a trap for the next reader."""
    with _scratch_schema() as schema:
        command.upgrade(_alembic_config(), "head")

        with _connect() as conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    text(
                        "select indexname from pg_indexes "
                        "where schemaname = :schema and tablename = 'runs'"
                    ),
                    {"schema": schema},
                ).fetchall()
            }
            constraints = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        select con.conname from pg_constraint con
                        join pg_class rel on rel.oid = con.conrelid
                        join pg_namespace n on n.oid = rel.relnamespace
                        where n.nspname = :schema and rel.relname = 'runs'
                        """
                    ),
                    {"schema": schema},
                ).fetchall()
            }

        assert not any("runs2" in name for name in indexes | constraints), (
            f"leftover runs2 names: {sorted(n for n in indexes | constraints if 'runs2' in n)}"
        )
        assert {
            "runs_pkey",
            "ux_runs_engine_run_id_live",
            "ux_runs_active_lane_per_harness",
            "ix_runs_deleted_lifecycle_updated",
        } <= indexes
        assert {"ck_runs_source", "ck_runs_harness", "ck_runs_lifecycle"} <= constraints


@pytest.mark.usefixtures("isolated_schema")
def test_revision_b_up_down_cycle_restores_a_migratable_schema():
    """The dropped rows do not come back — the dump is for that — but the schema must.

    Without this, a downgrade leaves a database Alembic can neither use nor re-upgrade.
    """
    with _scratch_schema() as schema:
        config = _alembic_config()

        command.upgrade(config, "head")
        assert not (DROPPED_TABLES & _tables(schema))

        command.downgrade(config, REVISION_A)
        after_downgrade = _tables(schema)
        assert DROPPED_TABLES <= after_downgrade, "downgrade must put the legacy schema back"
        assert "runs2" in after_downgrade
        assert {"workshops", "workshop_options", "hypotheses"} <= after_downgrade

        command.upgrade(config, "head")
        assert not (DROPPED_TABLES & _tables(schema))
        assert ENGINE_TABLES <= _tables(schema)


def test_revision_b_refuses_a_database_that_is_not_the_apps_own(monkeypatch):
    """The one mistake a downgrade cannot undo is running against the wrong DSN."""
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


def test_revision_b_guards_both_directions():
    """A downgrade drops and recreates tables too; it needs the same guard as the upgrade."""
    source = (
        BACKEND_ROOT
        / "alembic"
        / "versions"
        / f"{REVISION_B}_engine_schema_revision_b.py"
    ).read_text(encoding="utf-8")

    for function in ("def upgrade() -> None:", "def downgrade() -> None:"):
        body = source.split(function, 1)[1]
        first_statement = next(
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.strip().startswith(('"""', "#"))
        )
        assert first_statement == "_assert_database_identity()", (
            f"{function} must begin with the identity guard, found {first_statement!r}"
        )


def _load_revision_module():
    import importlib.util

    path = (
        BACKEND_ROOT
        / "alembic"
        / "versions"
        / f"{REVISION_B}_engine_schema_revision_b.py"
    )
    spec = importlib.util.spec_from_file_location("revision_b_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

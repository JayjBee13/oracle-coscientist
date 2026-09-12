from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings

# The whole schema, since Revision B dropped the legacy tables and renamed `runs2` to
# `runs`. If a table appears here that is not in a migration, or the other way round, this
# is where it shows up.
EXPECTED_TABLES = {
    "alembic_version",
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
    # Revision 0005: system-wide preferences, keyed by name. Today one key, `models.default`.
    "app_settings",
    "users",
}

# Gone in Revision B. Named rather than merely absent so a re-introduction is a failure
# with an explanation attached.
DROPPED_LEGACY_TABLES = {
    "artifact_index",
    "comparison_groups",
    "comparison_members",
    "process_events",
    "processes",
    "prompt_options",
    "prompt_workshops",
    "run_settings",
    "runs2",
}


def test_database_url_targets_dedicated_app_database():
    settings = get_settings()
    engine = create_engine(settings.database_url)

    with engine.connect() as conn:
        row = conn.execute(text("select current_database(), current_user")).one()

    assert row[0] == "ai_coscientist_gui"
    assert row[1] == "ai_coscientist_gui_app"


def test_migrated_schema_has_exactly_the_engine_tables():
    engine = create_engine(get_settings().database_url)
    tables = set(inspect(engine).get_table_names())

    assert tables == EXPECTED_TABLES
    assert not (tables & DROPPED_LEGACY_TABLES)


def test_users_and_nullable_ownership_are_migrated_and_backfilled():
    engine = create_engine(get_settings().database_url)
    inspector = inspect(engine)

    users = {column["name"]: column for column in inspector.get_columns("users")}
    assert set(users) == {
        "id", "username", "email", "display_name", "is_admin", "created_at", "last_seen_at"
    }
    assert users["email"]["nullable"] is True
    assert users["display_name"]["nullable"] is True
    assert {index["name"] for index in inspector.get_indexes("runs")} >= {"ix_runs_owner_id"}
    assert {index["name"] for index in inspector.get_indexes("workshops")} >= {
        "ix_workshops_owner_id"
    }

    with engine.connect() as conn:
        owner = conn.execute(
            text("select id, username, is_admin from users where username = :username"),
            {"username": get_settings().local_identity_username},
        ).one()
        assert owner.is_admin is True


def test_run_lane_index_is_unique_per_harness_over_active_lifecycles():
    engine = create_engine(get_settings().database_url)
    inspector = inspect(engine)

    lane = next(
        index
        for index in inspector.get_indexes("runs")
        if index["name"] == "ux_runs_active_lane_per_harness"
    )
    assert lane["unique"] is True
    assert lane["column_names"] == ["harness"]

    with engine.connect() as conn:
        predicate = conn.execute(
            text(
                """
                select pg_get_expr(i.indpred, i.indrelid)
                from pg_index i
                join pg_class c on c.oid = i.indexrelid
                join pg_namespace n on n.oid = c.relnamespace
                where c.relname = 'ux_runs_active_lane_per_harness'
                  and n.nspname = current_schema()
                """
            )
        ).scalar_one()

    # A paused run holds no lane: it has no work in flight, so it must not block a new run.
    for lifecycle in ("queued", "running", "pausing", "stopping", "finishing"):
        assert lifecycle in predicate
    assert "paused" not in predicate.replace("pausing", "")


def test_run_lane_lock_blocks_two_active_runs_for_the_same_harness():
    engine = create_engine(get_settings().database_url)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            _insert_run(conn, "test-lane-lock-a", "claude", "running")
            try:
                _insert_run(conn, "test-lane-lock-b", "claude", "queued")
            except IntegrityError:
                pass
            else:
                raise AssertionError("the lane lock let a second claude run start")
        finally:
            transaction.rollback()


def test_demo_and_claude_runs_hold_separate_lanes():
    """Intended: practising with a demo run must not have to wait for real work."""
    engine = create_engine(get_settings().database_url)

    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            _insert_run(conn, "test-lane-claude", "claude", "running")
            _insert_run(conn, "test-lane-demo", "demo", "running")
        finally:
            transaction.rollback()


def _insert_run(conn, engine_run_id: str, harness: str, lifecycle: str) -> None:
    conn.execute(
        text(
            """
            insert into runs (engine_run_id, title, question, prompt, harness, lifecycle)
            values (:engine_run_id, 'Schema test', 'A question', 'A prompt',
                    :harness, :lifecycle)
            """
        ),
        {"engine_run_id": engine_run_id, "harness": harness, "lifecycle": lifecycle},
    )

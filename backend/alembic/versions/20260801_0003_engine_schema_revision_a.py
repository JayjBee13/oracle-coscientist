"""engine schema (Revision A, additive)

Adds the tables the research engine writes to, plus the NOTIFY trigger that SSE tails.
Nothing existing is touched: the legacy read path stays green until Wave 3B.2 re-points it,
and Revision B does the destructive half (dropping legacy tables, renaming runs2 → runs).

The new runs table is `runs2` because `runs` is still occupied by the legacy table. Every
new foreign key already points at runs2, so Revision B's rename is a one-line change here.

Revision ID: 20260801_0003
Revises: 20260603_0002
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260801_0003"
down_revision: str | Sequence[str] | None = "20260603_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPECTED_DATABASE = "ai_coscientist_gui"

LIFECYCLE_SQL = (
    "lifecycle IN ('queued', 'running', 'pausing', 'paused', 'stopping', 'stopped', "
    "'finishing', 'completed', 'failed', 'lost')"
)
LANE_SQL = "lifecycle IN ('queued', 'running', 'pausing', 'stopping', 'finishing')"

# The channel is database-wide, so the payload names the schema: tests run against
# throwaway `test_<hex>` schemas in the same database and their events must not be
# mistaken for live ones by a listener.
NOTIFY_FUNCTION = """
CREATE FUNCTION run_events_notify() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'run_events',
        json_build_object(
            'schema', TG_TABLE_SCHEMA,
            'run_id', NEW.run_id::text,
            'seq', NEW.seq
        )::text
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

NOTIFY_TRIGGER = """
CREATE TRIGGER run_events_notify_trigger
AFTER INSERT ON run_events
FOR EACH ROW EXECUTE FUNCTION run_events_notify();
"""


def _assert_database_identity() -> None:
    """Refuse to touch anything but the app's own database.

    This server also hosts the owner's production databases. A migration pointed at the
    wrong DSN is the one mistake that cannot be undone by a downgrade.
    """
    database = op.get_bind().execute(sa.text("select current_database()")).scalar_one()
    if database != EXPECTED_DATABASE:
        raise RuntimeError(
            f"Refusing to migrate {database!r}: this revision only runs against "
            f"{EXPECTED_DATABASE!r}. Check DATABASE_URL."
        )


def upgrade() -> None:
    _assert_database_identity()

    op.create_table(
        "runs2",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("engine_run_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'app'"), nullable=False),
        sa.Column("source_version", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("base_prompt_hash", sa.Text(), nullable=True),
        sa.Column("harness", sa.Text(), nullable=False),
        sa.Column("lifecycle", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("round", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rounds_target", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("calls_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("budget_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("budget_usd", sa.Numeric(12, 4), server_default=sa.text("0"), nullable=False),
        sa.Column("spend_usd", sa.Numeric(14, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_in", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "engine_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "feedback_history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "graft_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "pending_interventions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("root_path", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("control_requested", sa.Text(), nullable=True),
        sa.Column("supervisor_pid", sa.Integer(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("source IN ('app', 'imported')", name="ck_runs2_source"),
        sa.CheckConstraint("harness IN ('claude', 'codex', 'demo')", name="ck_runs2_harness"),
        sa.CheckConstraint(LIFECYCLE_SQL, name="ck_runs2_lifecycle"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_runs2_engine_run_id_live",
        "runs2",
        ["engine_run_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # One active run per harness. `demo` is its own harness, so a demo run and a real run
    # can be live at the same time — that is intended.
    op.create_index(
        "ux_runs2_active_lane_per_harness",
        "runs2",
        ["harness"],
        unique=True,
        postgresql_where=sa.text(LANE_SQL),
    )
    op.create_index(
        "ix_runs2_deleted_lifecycle_updated",
        "runs2",
        ["deleted_at", "lifecycle", "updated_at"],
    )

    op.create_table(
        "hypotheses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hid", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_md", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("elo", sa.Float(), server_default=sa.text("1200"), nullable=False),
        sa.Column("matches", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("wins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cluster", sa.Text(), nullable=True),
        sa.Column("duplicate_of", sa.Text(), nullable=True),
        sa.Column(
            "parent_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("operator", sa.Text(), nullable=True),
        sa.Column("seed_id", sa.Text(), nullable=True),
        sa.Column("created_round", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'agent'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'rejected', 'archived')", name="ck_hypotheses_status"
        ),
        sa.CheckConstraint("source IN ('agent', 'human')", name="ck_hypotheses_source"),
        sa.ForeignKeyConstraint(["run_id"], ["runs2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "hid", name="uq_hypotheses_run_hid"),
    )
    op.create_index("ix_hypotheses_run_status_elo", "hypotheses", ["run_id", "status", "elo"])

    op.create_table(
        "reviews",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("novelty_level", sa.Text(), nullable=True),
        sa.Column("novelty_note", sa.Text(), nullable=True),
        sa.Column("correctness", sa.Text(), nullable=True),
        sa.Column("testability", sa.Text(), nullable=True),
        sa.Column("key_risk", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["hypothesis_id"], ["hypotheses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviews_hypothesis_id", "reviews", ["hypothesis_id"])

    op.create_table(
        "matches",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("hid_a", sa.Text(), nullable=False),
        sa.Column("hid_b", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'planned'"), nullable=False),
        sa.Column("winner", sa.Integer(), nullable=True),
        sa.Column("elo_a_before", sa.Float(), nullable=True),
        sa.Column("elo_a_after", sa.Float(), nullable=True),
        sa.Column("elo_b_before", sa.Float(), nullable=True),
        sa.Column("elo_b_after", sa.Float(), nullable=True),
        sa.Column("k", sa.Integer(), nullable=True),
        sa.Column("debate_md", sa.Text(), nullable=True),
        sa.Column("judge_model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'completed', 'skipped')", name="ck_matches_status"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matches_run_round", "matches", ["run_id", "round"])

    op.create_table(
        "graft_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("n_clusters", sa.Integer(), nullable=True),
        sa.Column("hhi", sa.Float(), nullable=True),
        sa.Column(
            "signals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("votes", sa.Integer(), nullable=True),
        sa.Column("fired", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("abstained_reason", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("skeleton", sa.Text(), nullable=True),
        sa.Column("seed_framing", sa.Text(), nullable=True),
        sa.Column("seed_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "budget_ledger",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("tokens_in", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_creation", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_budget_ledger_run_role", "budget_ledger", ["run_id", "role"])

    op.create_table(
        "run_events",
        sa.Column("seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("seq"),
    )
    op.create_index("ix_run_events_run_seq", "run_events", ["run_id", "seq"])

    op.create_table(
        "run_context_docs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("chars", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs2.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_context_docs_run_id", "run_context_docs", ["run_id"])

    op.create_table(
        "workshops",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'refining'"), nullable=False),
        sa.Column("harness", sa.Text(), server_default=sa.text("'claude'"), nullable=False),
        sa.Column(
            "context_docs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "workshop_options",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workshop_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("optimizes_for", sa.Text(), nullable=True),
        sa.Column("excludes", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "recommended_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("chosen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rejected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workshop_id"], ["workshops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workshop_options_workshop_id", "workshop_options", ["workshop_id"])

    op.execute(NOTIFY_FUNCTION)
    op.execute(NOTIFY_TRIGGER)


def downgrade() -> None:
    _assert_database_identity()

    op.execute("DROP TRIGGER IF EXISTS run_events_notify_trigger ON run_events")
    op.execute("DROP FUNCTION IF EXISTS run_events_notify()")

    op.drop_index("ix_workshop_options_workshop_id", table_name="workshop_options")
    op.drop_table("workshop_options")
    op.drop_table("workshops")
    op.drop_index("ix_run_context_docs_run_id", table_name="run_context_docs")
    op.drop_table("run_context_docs")
    op.drop_index("ix_run_events_run_seq", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_budget_ledger_run_role", table_name="budget_ledger")
    op.drop_table("budget_ledger")
    op.drop_table("graft_events")
    op.drop_index("ix_matches_run_round", table_name="matches")
    op.drop_table("matches")
    op.drop_index("ix_reviews_hypothesis_id", table_name="reviews")
    op.drop_table("reviews")
    op.drop_index("ix_hypotheses_run_status_elo", table_name="hypotheses")
    op.drop_table("hypotheses")
    op.drop_index("ix_runs2_deleted_lifecycle_updated", table_name="runs2")
    op.drop_index("ux_runs2_active_lane_per_harness", table_name="runs2")
    op.drop_index("ux_runs2_engine_run_id_live", table_name="runs2")
    op.drop_table("runs2")

"""initial schema

Revision ID: 20260603_0001
Revises:
Create Date: 2026-06-03 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260603_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("engine_run_id", sa.Text(), nullable=False),
        sa.Column("harness", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("lifecycle", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("final_prompt", sa.Text(), nullable=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("run_path", sa.Text(), nullable=True),
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
        sa.CheckConstraint("harness IN ('claude', 'codex')", name="ck_runs_harness"),
        sa.CheckConstraint("version IN ('v1', 'v2')", name="ck_runs_version"),
        sa.CheckConstraint(
            "lifecycle IN ('draft', 'refining', 'prompt_ready', 'queued', 'running', "
            "'pause_requested', 'paused', 'stopping', 'stopped', 'finishing', "
            "'completed', 'failed', 'abandoned', 'imported', 'unknown')",
            name="ck_runs_lifecycle",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", "engine_run_id", name="uq_runs_version_engine_run_id"),
    )
    op.create_index("ix_runs_engine_identity", "runs", ["version", "engine_run_id"])
    op.create_index("ix_runs_lifecycle_updated", "runs", ["lifecycle", "updated_at"])

    op.create_table(
        "prompt_workshops",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("harness", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("selected_option", sa.Text(), nullable=True),
        sa.Column("final_prompt", sa.Text(), nullable=True),
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
        sa.CheckConstraint("harness IN ('claude', 'codex')", name="ck_prompt_workshops_harness"),
        sa.CheckConstraint(
            "status IN ('draft', 'refining', 'options_ready', 'prompt_ready', 'failed')",
            name="ck_prompt_workshops_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "comparison_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_prompt", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        "prompt_options",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("workshop_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "scoring_criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "recommended_settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workshop_id"], ["prompt_workshops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workshop_id", "label", name="uq_prompt_options_workshop_label"),
    )

    op.create_table(
        "run_settings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grounding_mode", sa.Text(), nullable=False),
        sa.Column("grounding_depth", sa.Text(), nullable=False),
        sa.Column("allow_fallback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("rounds", sa.Integer(), nullable=True),
        sa.Column("budget", sa.Integer(), nullable=True),
        sa.Column("matches", sa.Integer(), nullable=True),
        sa.Column("top_k", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "grounding_mode IN ('built_in_web', 'perplexity')",
            name="ck_run_settings_grounding_mode",
        ),
        sa.CheckConstraint(
            "grounding_depth IN ('shallow', 'standard', 'deep')",
            name="ck_run_settings_grounding_depth",
        ),
        sa.CheckConstraint("revision > 0", name="ck_run_settings_revision_positive"),
        sa.CheckConstraint("rounds IS NULL OR rounds > 0", name="ck_run_settings_rounds_positive"),
        sa.CheckConstraint("budget IS NULL OR budget > 0", name="ck_run_settings_budget_positive"),
        sa.CheckConstraint(
            "matches IS NULL OR matches > 0", name="ck_run_settings_matches_positive"
        ),
        sa.CheckConstraint("top_k IS NULL OR top_k > 0", name="ck_run_settings_top_k_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "revision", name="uq_run_settings_run_revision"),
    )
    op.create_index(
        "ux_run_settings_one_current_per_run",
        "run_settings",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "processes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workshop_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("harness", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
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
        sa.CheckConstraint("harness IN ('claude', 'codex')", name="ck_processes_harness"),
        sa.CheckConstraint(
            "mode IN ('prompt_workshop', 'co_scientist_batch', 'finish')",
            name="ck_processes_mode",
        ),
        sa.CheckConstraint(
            "state IN ('starting', 'running', 'pause_requested', 'paused', "
            "'stopping', 'stopped', 'finishing', 'completed', 'failed', "
            "'lost', 'killed')",
            name="ck_processes_state",
        ),
        sa.CheckConstraint(
            "(run_id IS NOT NULL) OR (workshop_id IS NOT NULL)",
            name="ck_processes_has_owner",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workshop_id"], ["prompt_workshops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_one_active_process_per_harness",
        "processes",
        ["harness"],
        unique=True,
        postgresql_where=sa.text("state IN ('starting', 'running', 'pause_requested', 'stopping')"),
    )

    op.create_table(
        "artifact_index",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "kind IN ('state_json', 'research_overview', 'ideas_ranked_csv', "
            "'hypothesis_markdown', 'other')",
            name="ck_artifact_index_kind",
        ),
        sa.CheckConstraint("size IS NULL OR size >= 0", name="ck_artifact_index_size_nonnegative"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "path", name="uq_artifact_index_run_path"),
    )
    op.create_index("ix_artifacts_run_kind", "artifact_index", ["run_id", "kind"])

    op.create_table(
        "process_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("process_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stream IN ('stdout', 'stderr', 'system')", name="ck_process_events_stream"
        ),
        sa.CheckConstraint(
            "event_type IN ('status', 'process_event', 'artifact_changed', 'error', 'control')",
            name="ck_process_events_event_type",
        ),
        sa.ForeignKeyConstraint(["process_id"], ["processes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_process_events_process_id_id", "process_events", ["process_id", "id"])

    op.create_table(
        "comparison_members",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["comparison_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "run_id"),
    )


def downgrade() -> None:
    op.drop_table("comparison_members")
    op.drop_index("ix_process_events_process_id_id", table_name="process_events")
    op.drop_table("process_events")
    op.drop_index("ix_artifacts_run_kind", table_name="artifact_index")
    op.drop_table("artifact_index")
    op.drop_index("ux_one_active_process_per_harness", table_name="processes")
    op.drop_table("processes")
    op.drop_index("ux_run_settings_one_current_per_run", table_name="run_settings")
    op.drop_table("run_settings")
    op.drop_table("prompt_options")
    op.drop_table("comparison_groups")
    op.drop_table("prompt_workshops")
    op.drop_index("ix_runs_lifecycle_updated", table_name="runs")
    op.drop_index("ix_runs_engine_identity", table_name="runs")
    op.drop_table("runs")

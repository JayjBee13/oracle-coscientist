"""Tables owned by the research engine — the whole schema, since Revision B.

The legacy tables (processes, artifact_index, run_settings, prompt_workshops...) and the
`app.db.models` module that described them are gone, along with the interim `runs2` name
this table carried while the old `runs` still occupied it.

`updated_at` is maintained by SQLAlchemy (`onupdate`), not by a database trigger. Anything
that writes these rows with raw SQL must set `updated_at` itself.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# The canonical lifecycle set (plan C4). Transitions:
#   queued → running → completed | failed | lost
#   running → pausing → paused → running
#   running | paused → stopping → stopped
#   running → finishing → completed
LIFECYCLES: tuple[str, ...] = (
    "queued",
    "running",
    "pausing",
    "paused",
    "stopping",
    "stopped",
    "finishing",
    "completed",
    "failed",
    "lost",
)

# A run in one of these states holds its harness lane. `paused` deliberately does not:
# a paused run has no in-flight work, so it must not block a new one.
LANE_LIFECYCLES: tuple[str, ...] = ("queued", "running", "pausing", "stopping", "finishing")

HARNESSES: tuple[str, ...] = ("claude", "codex", "demo")
RUN_SOURCES: tuple[str, ...] = ("app", "imported")
HYPOTHESIS_STATUSES: tuple[str, ...] = ("active", "rejected", "archived")
HYPOTHESIS_SOURCES: tuple[str, ...] = ("agent", "human")
MATCH_STATUSES: tuple[str, ...] = ("planned", "completed", "skipped")

_LIFECYCLE_SQL = ", ".join(f"'{value}'" for value in LIFECYCLES)
_LANE_SQL = ", ".join(f"'{value}'" for value in LANE_LIFECYCLES)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Run(Base):
    """A research run."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("source IN ('app', 'imported')", name="ck_runs_source"),
        CheckConstraint("harness IN ('claude', 'codex', 'demo')", name="ck_runs_harness"),
        CheckConstraint(f"lifecycle IN ({_LIFECYCLE_SQL})", name="ck_runs_lifecycle"),
        Index(
            "ux_runs_engine_run_id_live",
            "engine_run_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ux_runs_active_lane_per_harness",
            "harness",
            unique=True,
            postgresql_where=text(f"lifecycle IN ({_LANE_SQL})"),
        ),
        Index("ix_runs_deleted_lifecycle_updated", "deleted_at", "lifecycle", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    engine_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'app'"))
    source_version: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    base_prompt_hash: Mapped[str | None] = mapped_column(Text)
    harness: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rounds_target: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    budget_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, server_default=text("0")
    )
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    engine_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    feedback_history: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    graft_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    pending_interventions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    root_path: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    control_requested: Mapped[str | None] = mapped_column(Text)
    supervisor_pid: Mapped[int | None] = mapped_column(Integer)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class Hypothesis(Base):
    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'rejected', 'archived')", name="ck_hypotheses_status"
        ),
        CheckConstraint("source IN ('agent', 'human')", name="ck_hypotheses_source"),
        UniqueConstraint("run_id", "hid", name="uq_hypotheses_run_hid"),
        Index("ix_hypotheses_run_status_elo", "run_id", "status", "elo"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    hid: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    elo: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("1200"))
    matches: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cluster: Mapped[str | None] = mapped_column(Text)
    duplicate_of: Mapped[str | None] = mapped_column(Text)
    parent_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    operator: Mapped[str | None] = mapped_column(Text)
    seed_id: Mapped[str | None] = mapped_column(Text)
    created_round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'agent'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_hypothesis_id", "hypothesis_id"),)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    hypothesis_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("hypotheses.id", ondelete="CASCADE"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    novelty_level: Mapped[str | None] = mapped_column(Text)
    novelty_note: Mapped[str | None] = mapped_column(Text)
    correctness: Mapped[str | None] = mapped_column(Text)
    testability: Mapped[str | None] = mapped_column(Text)
    key_risk: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'completed', 'skipped')", name="ck_matches_status"
        ),
        Index("ix_matches_run_round", "run_id", "round"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    hid_a: Mapped[str] = mapped_column(Text, nullable=False)
    hid_b: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'planned'"))
    winner: Mapped[int | None] = mapped_column(Integer)
    elo_a_before: Mapped[float | None] = mapped_column(Float)
    elo_a_after: Mapped[float | None] = mapped_column(Float)
    elo_b_before: Mapped[float | None] = mapped_column(Float)
    elo_b_after: Mapped[float | None] = mapped_column(Float)
    k: Mapped[int | None] = mapped_column(Integer)
    debate_md: Mapped[str | None] = mapped_column(Text)
    judge_model: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GraftEvent(Base):
    __tablename__ = "graft_events"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    n_clusters: Mapped[int | None] = mapped_column(Integer)
    hhi: Mapped[float | None] = mapped_column(Float)
    signals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    votes: Mapped[int | None] = mapped_column(Integer)
    fired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    abstained_reason: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str | None] = mapped_column(Text)
    skeleton: Mapped[str | None] = mapped_column(Text)
    seed_framing: Mapped[str | None] = mapped_column(Text)
    seed_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BudgetLedger(Base):
    __tablename__ = "budget_ledger"
    __table_args__ = (Index("ix_budget_ledger_run_role", "run_id", "role"),)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cache_creation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_read: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ok'"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RunEvent(Base):
    """Append-only event log. An insert trigger NOTIFYs `run_events`; SSE tails it.

    Written through `RunStore.emit` only — see that method for why the writer is single.
    """

    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_seq", "run_id", "seq"),)

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int | None] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RunContextDoc(Base):
    __tablename__ = "run_context_docs"
    __table_args__ = (Index("ix_run_context_docs_run_id", "run_id"),)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    chars: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AppSetting(Base):
    """One system-wide preference, keyed by name. Today there is exactly one key, `models`.

    JSONB rather than a column per setting: the shape of the models default is owned by the
    model policy, which validates it far more strictly than a check constraint could, and
    which learns something new every time a CLI is probed. There is no `user_id` — this is a
    deliberately system-wide preference, readable by everyone and writable only by an
    administrator.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Workshop(Base):
    __tablename__ = "workshops"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'refining'"))
    harness: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'claude'"))
    context_docs: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkshopOption(Base):
    __tablename__ = "workshop_options"
    __table_args__ = (Index("ix_workshop_options_workshop_id", "workshop_id"),)

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    workshop_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workshops.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    optimizes_for: Mapped[str | None] = mapped_column(Text)
    excludes: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    recommended_settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    chosen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

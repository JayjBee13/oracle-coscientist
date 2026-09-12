"""users and resource ownership

Revision ID: 20260904_0006
Revises: 20260811_0005
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

revision: str = "20260904_0006"
down_revision: str | Sequence[str] | None = "20260811_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.add_column("runs", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("workshops", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_runs_owner_id_users",
        "runs",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_workshops_owner_id_users",
        "workshops",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_runs_owner_id", "runs", ["owner_id"])
    op.create_index("ix_workshops_owner_id", "workshops", ["owner_id"])

    username = get_settings().local_identity_username.strip()
    if not username:
        raise RuntimeError("LOCAL_IDENTITY_USERNAME must not be blank")
    owner_id = op.get_bind().execute(
        sa.text(
            "INSERT INTO users (username, display_name, is_admin) "
            "VALUES (:username, :display_name, true) RETURNING id"
        ),
        {"username": username, "display_name": username},
    ).scalar_one()
    op.get_bind().execute(sa.text("UPDATE runs SET owner_id = :owner_id"), {"owner_id": owner_id})
    op.get_bind().execute(
        sa.text("UPDATE workshops SET owner_id = :owner_id"), {"owner_id": owner_id}
    )


def downgrade() -> None:
    op.drop_index("ix_workshops_owner_id", table_name="workshops")
    op.drop_index("ix_runs_owner_id", table_name="runs")
    op.drop_constraint("fk_workshops_owner_id_users", "workshops", type_="foreignkey")
    op.drop_constraint("fk_runs_owner_id_users", "runs", type_="foreignkey")
    op.drop_column("workshops", "owner_id")
    op.drop_column("runs", "owner_id")
    op.drop_table("users")

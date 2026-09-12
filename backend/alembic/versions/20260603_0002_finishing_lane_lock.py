"""include finishing in active lane lock

Revision ID: 20260603_0002
Revises: 20260603_0001
Create Date: 2026-06-03 19:25:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260603_0002"
down_revision: str | Sequence[str] | None = "20260603_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ux_one_active_process_per_harness", table_name="processes")
    op.create_index(
        "ux_one_active_process_per_harness",
        "processes",
        ["harness"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('starting', 'running', 'pause_requested', 'stopping', 'finishing')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ux_one_active_process_per_harness", table_name="processes")
    op.create_index(
        "ux_one_active_process_per_harness",
        "processes",
        ["harness"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('starting', 'running', 'pause_requested', 'stopping')"
        ),
    )

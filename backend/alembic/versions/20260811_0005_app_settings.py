"""app_settings: the persisted defaults the top bar edits

Additive and reversible. One table, `app_settings(key text primary key, value jsonb not
null, updated_at timestamptz not null default now())`, holding application-wide preferences
keyed by name. The first key is `models`, the provider/tier/overrides a new run and a new
workshop inherit when the request does not state its own.

Nothing existing is touched, and nothing is backfilled: an absent row means "no default has
been chosen", which the service answers with the engine's own built-in default rather than
with a row somebody has to remember to insert. That is why there is no seed here — a seeded
row would freeze today's default into the database, and the day the engine's default changes
the two would disagree with no way to tell which was intended.

`updated_at` is maintained by SQLAlchemy's `onupdate`, matching every other table in this
schema; the server default covers the insert.

Revision ID: 20260811_0005
Revises: 20260801_0004
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260811_0005"
down_revision: str | Sequence[str] | None = "20260801_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name="pk_app_settings"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")

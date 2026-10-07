"""llm exchange

Revision ID: c5d81e37b240
Revises: b7c2e1904f3d
Create Date: 2026-10-07 22:03:41.117204

The prompt and the reply for one model call, hanging off its event row so that
rotating the log rotates them too.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d81e37b240"
down_revision: str | None = "b7c2e1904f3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_exchange",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["event.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_exchange_event_id"), "llm_exchange", ["event_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_exchange_event_id"), table_name="llm_exchange")
    op.drop_table("llm_exchange")

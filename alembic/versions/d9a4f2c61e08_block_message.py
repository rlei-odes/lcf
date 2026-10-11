"""block message

Revision ID: d9a4f2c61e08
Revises: c5d81e37b240
Create Date: 2026-10-11 10:12:00.000000

`block_message` holds the open discussion about a block: the author's messages
and the assistant's replies, none of which can become content.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9a4f2c61e08"
down_revision: str | None = "c5d81e37b240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "block_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["block_id"], ["block.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_block_message_block_id"), "block_message", ["block_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_block_message_block_id"), table_name="block_message")
    op.drop_table("block_message")

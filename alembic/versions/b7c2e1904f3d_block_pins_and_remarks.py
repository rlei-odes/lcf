"""block pins and proposal remarks

Revision ID: b7c2e1904f3d
Revises: f04b89b9cdd9
Create Date: 2026-10-07 11:04:12.551903

`block_pin` holds the passages an author has settled; `proposal.remark` holds what
they asked for, which turns a proposal row into one turn of a conversation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2e1904f3d"
down_revision: str | None = "f04b89b9cdd9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "block_pin",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["block_id"], ["block.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_block_pin_block_id"), "block_pin", ["block_id"], unique=False)
    op.add_column("proposal", sa.Column("remark", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("proposal", "remark")
    op.drop_index(op.f("ix_block_pin_block_id"), table_name="block_pin")
    op.drop_table("block_pin")

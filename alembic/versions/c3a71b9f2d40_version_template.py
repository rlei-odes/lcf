"""docx template bound to a doc type version

Revision ID: c3a71b9f2d40
Revises: f6e2be5dffb1
Create Date: 2026-09-22 20:14:02.117403
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a71b9f2d40"
down_revision: str | None = "f6e2be5dffb1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable because most versions have no template and never need one —
    # `render_plain` is the path that works with no setup at all.
    op.add_column("doc_type_version", sa.Column("template_uri", sa.Text(), nullable=True))
    op.add_column(
        "doc_type_version", sa.Column("template_filename", sa.String(length=300), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("doc_type_version", "template_filename")
    op.drop_column("doc_type_version", "template_uri")

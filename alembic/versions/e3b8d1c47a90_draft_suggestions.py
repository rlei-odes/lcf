"""suggestions held on a doc type draft

Revision ID: e3b8d1c47a90
Revises: d9a4f2c61e08
Create Date: 2026-10-24 21:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3b8d1c47a90"
down_revision: str | None = "d9a4f2c61e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("doc_type_draft", sa.Column("suggestions", Json, nullable=True))


def downgrade() -> None:
    op.drop_column("doc_type_draft", "suggestions")

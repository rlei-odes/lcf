"""evidence source digest

Revision ID: f04b89b9cdd9
Revises: 8acd207f2637
Create Date: 2026-10-04 16:32:27.288839

Autogenerate also proposed dropping five indexes on `event`, `house_style`,
`evidence_candidate` and `evidence_run`. Those are hand-written in earlier
migrations with `DESC` ordering and a partial `WHERE`, which the models cannot
express and autogenerate therefore cannot see. They are deliberately kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f04b89b9cdd9"
down_revision: str | None = "8acd207f2637"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("evidence_source", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_evidence_source_sha256"), "evidence_source", ["sha256"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_evidence_source_sha256"), table_name="evidence_source")
    op.drop_column("evidence_source", "sha256")

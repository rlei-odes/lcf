"""house style

Revision ID: c411d72d040c
Revises: 57f4a1b45d55
Create Date: 2026-09-27 11:13:40.817124
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c411d72d040c'
down_revision: str | None = '57f4a1b45d55'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'house_style',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('uri', sa.Text(), nullable=False),
        sa.Column('filename', sa.String(length=300), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        # clock_timestamp(), not now(): this column decides which upload is in
        # force, and now() is constant across a transaction, so two rows written
        # together would tie.
        sa.Column(
            'uploaded_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('clock_timestamp()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # Newest first is the only way this table is ever read.
    op.create_index('ix_house_style_uploaded_at', 'house_style', [sa.text('uploaded_at DESC')])


def downgrade() -> None:
    op.drop_index('ix_house_style_uploaded_at', table_name='house_style')
    op.drop_table('house_style')

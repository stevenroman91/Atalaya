"""collect_runs.cancel_requested: anulación cooperativa de una colecta

Revision ID: b7c8d9e0f1a2
Revises: a1f2c3d4e5f6
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7c8d9e0f1a2'
down_revision = 'a1f2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('collect_runs', sa.Column('cancel_requested', sa.Boolean(),
                                            nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('collect_runs', 'cancel_requested')

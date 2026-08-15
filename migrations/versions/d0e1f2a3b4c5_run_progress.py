"""collect_runs.progress_total/done — progreso visible de una colecta

Revision ID: d0e1f2a3b4c5
Revises: c4d5e6f7a8b9
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa

revision = 'd0e1f2a3b4c5'
down_revision = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('collect_runs', sa.Column('progress_total', sa.Integer(),
                                            nullable=False, server_default='0'))
    op.add_column('collect_runs', sa.Column('progress_done', sa.Integer(),
                                            nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('collect_runs', 'progress_done')
    op.drop_column('collect_runs', 'progress_total')

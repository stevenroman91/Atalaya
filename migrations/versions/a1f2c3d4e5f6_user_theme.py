"""users.theme: preferencia de tema (system | light | dark)

Revision ID: a1f2c3d4e5f6
Revises: dd46d8fb0df1
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1f2c3d4e5f6'
down_revision = 'dd46d8fb0df1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('theme', sa.String(length=8),
                                     nullable=False, server_default='system'))


def downgrade() -> None:
    op.drop_column('users', 'theme')

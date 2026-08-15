"""Índice sobre articles.gn_url — dedupe barato antes de resolver redirecciones

Revision ID: c4d5e6f7a8b9
Revises: b7c8d9e0f1a2
Create Date: 2026-08-15
"""
from alembic import op

revision = 'c4d5e6f7a8b9'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_articles_gn_url', 'articles', ['gn_url'])


def downgrade() -> None:
    op.drop_index('ix_articles_gn_url', table_name='articles')

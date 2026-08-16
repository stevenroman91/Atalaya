"""Traza de los rechazos por criterio.

«Ne jamais écarter en silence»: hasta ahora un artículo descartado por un
filtro desaparecía sin dejar nada — solo un contador se movía. El analista
no podía contradecir lo que no ve.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a3b4c5d6e7f8'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'rejects',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.Integer(), sa.ForeignKey('collect_runs.id'), nullable=True),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('reason', sa.String(length=255), nullable=False),
        sa.Column('country', sa.String(length=2), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint('uq_rejects_url', 'rejects', ['url'])
    op.create_index('ix_rejects_domain_country', 'rejects', ['domain', 'country'])


def downgrade() -> None:
    op.drop_index('ix_rejects_domain_country', table_name='rejects')
    op.drop_constraint('uq_rejects_url', 'rejects', type_='unique')
    op.drop_table('rejects')

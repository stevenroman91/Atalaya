"""Resultado del diagnóstico de portada, por fuente.

El diagnóstico se lanza sobre todas las fuentes en fallo a la vez y tarda
minutos: su resultado debe sobrevivir a la recarga de la página.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""
import sqlalchemy as sa
from alembic import op

revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sources', sa.Column('probe_note', sa.Text(), nullable=True))
    op.add_column('sources', sa.Column('probe_at', sa.DateTime(timezone=True),
                                       nullable=True))


def downgrade() -> None:
    op.drop_column('sources', 'probe_at')
    op.drop_column('sources', 'probe_note')

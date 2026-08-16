"""Coordenadas exactas en el artículo (USGS, GDACS).

La prensa no publica coordenadas: el mapa ponía el marcador en la capital
del país a falta de zona reconocida. Las API oficiales sí las dan, y esa
precisión debe poder subir hasta el evento.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('articles', sa.Column('lat', sa.Float(), nullable=True))
    op.add_column('articles', sa.Column('lon', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('articles', 'lon')
    op.drop_column('articles', 'lat')

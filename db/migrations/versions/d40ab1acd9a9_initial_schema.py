"""initial_schema

Revision ID: d40ab1acd9a9
Revises:
Create Date: 2026-04-09 21:10:21.991345

Reescrita en la reestructuración (Fase 0): se quitan clients/departments/
tickets — esos datos viven en el sistema de tickets, no aquí. Reescrita en
el lugar (no una migración nueva que cree-y-borre) porque este repo nunca
tocó una base de datos de producción real. Cualquier DB local existente
debe recrearse (`docker compose down -v` + `alembic upgrade head`), ya que
Alembic rastrea por revision id, no por contenido del archivo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd40ab1acd9a9'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('workers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('phone_number', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('role', sa.String(length=60), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('external_user_id', sa.UUID(), nullable=True),
    sa.Column('external_department_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('phone_number')
    )
    op.create_table('raw_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('worker_id', sa.UUID(), nullable=False),
    sa.Column('wamid', sa.String(length=64), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('external_ticket_id', sa.UUID(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['worker_id'], ['workers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('wamid')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('raw_messages')
    op.drop_table('workers')

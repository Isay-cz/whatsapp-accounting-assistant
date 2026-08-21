"""ticket_creations: bitácora de tickets creados por el bot

Tabla nueva `ticket_creations` (lo que el bot mandó crear, incluidos los
intentos fallidos) y `raw_messages.ticket_creation_id` para vincular el
bloque de mensajes con el intento que generó. Ver CLAUDE.md, decisión #20.

Se retira `raw_messages.external_ticket_id`: era una referencia suelta al
otro sistema que ningún código llegó a escribir (se verificó que no tuviera
valores no nulos antes de retirarla). El ticket ahora se alcanza por el join
con `ticket_creations`.

Revision ID: c2f81a4d6e70
Revises: b1c4e7f92a03
Create Date: 2026-08-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c2f81a4d6e70'
down_revision: Union[str, Sequence[str], None] = 'b1c4e7f92a03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ticket_creations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('worker_id', sa.UUID(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('entities', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('priority', sa.String(length=10), nullable=False),
        sa.Column('client_id', sa.UUID(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('external_ticket_id', sa.UUID(), nullable=True),
        sa.Column('ticket_number', sa.Integer(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['worker_id'], ['workers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_ticket_creations_ticket_number', 'ticket_creations', ['ticket_number']
    )
    op.add_column('raw_messages', sa.Column('ticket_creation_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'fk_raw_messages_ticket_creation_id',
        'raw_messages',
        'ticket_creations',
        ['ticket_creation_id'],
        ['id'],
    )
    op.drop_column('raw_messages', 'external_ticket_id')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('raw_messages', sa.Column('external_ticket_id', sa.UUID(), nullable=True))
    op.drop_constraint('fk_raw_messages_ticket_creation_id', 'raw_messages', type_='foreignkey')
    op.drop_column('raw_messages', 'ticket_creation_id')
    op.drop_index('ix_ticket_creations_ticket_number', table_name='ticket_creations')
    op.drop_table('ticket_creations')

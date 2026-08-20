"""drop workers.external_department_id

El bot ya no manda ni guarda el departamento del ticket: se deriva server-side
en el sistema de tickets a partir de created_by (ver CLAUDE.md, decisión #8).
La columna quedó sin ningún consumidor.

Revision ID: b1c4e7f92a03
Revises: d40ab1acd9a9
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1c4e7f92a03'
down_revision: Union[str, Sequence[str], None] = 'd40ab1acd9a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('workers', 'external_department_id')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'workers',
        sa.Column('external_department_id', sa.UUID(), nullable=True),
    )

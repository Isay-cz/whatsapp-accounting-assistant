"""raw_messages.wamid: 64 -> 255 caracteres

El `wamid` real de la Cloud API mide más de lo que cabía. El primero que llegó
de Meta en la VM de pruebas (2026-09-17) medía **66 caracteres**:

    wamid.HBgNNTIxNTUxMTk3MTI2NhUCABIYFDNBNzRCNERBQ0FCMDUyMjE1NUU3AA==

y el INSERT moría con `value too long for type character varying(64)`. El
webhook contestaba 500 y Meta reintentaba el mismo mensaje una y otra vez, así
que el bot no procesaba nada.

Ninguna prueba lo detectó porque todas usaban wamids cortos e inventados
(`wamid.log.0`). El largo real depende del identificador del número y del
mensaje, así que 255 es holgura, no un número medido.

Revision ID: e7a92c5f18bd
Revises: c2f81a4d6e70
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e7a92c5f18bd'
down_revision: Union[str, Sequence[str], None] = 'c2f81a4d6e70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "raw_messages",
        "wamid",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Truncaría los wamid reales, que es justo el bug que arregla esta
    # migración: bajar de nuevo solo tiene sentido con la tabla vacía.
    op.alter_column(
        "raw_messages",
        "wamid",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
    )

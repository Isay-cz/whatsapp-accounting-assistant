from sqlalchemy import func, select

from database import AsyncSessionLocal
from models.orm import Worker
from services.ticket_system.sync import PHONE_MATCH_DIGITS, phone_match_key


def active_worker_by_phone_stmt(phone: str):
    """Consulta de whitelist: compara los últimos dígitos del número.

    Meta manda el número sin `+` y con o sin el "1" de móvil mexicano según el
    contexto, así que comparar la cadena completa daría falsos negativos. Ver
    services/ticket_system/sync.py.
    """
    return select(Worker).where(
        func.right(Worker.phone_number, PHONE_MATCH_DIGITS) == phone_match_key(phone),
        Worker.is_active == True,  # noqa: E712
    )


async def lookup_active_worker_by_phone(phone: str) -> Worker | None:
    """Usado por el flujo conversacional para resolver el `Worker` (y sus
    referencias externas) en el momento de cerrar el buffer o recuperar
    watchers tras un restart — nunca se arrastra el objeto ORM a través de
    la ventana de debounce, se re-consulta fresco cada vez."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(active_worker_by_phone_stmt(phone))
        return result.scalar_one_or_none()

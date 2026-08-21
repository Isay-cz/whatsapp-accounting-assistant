import logging
import uuid

from sqlalchemy import func, select, update

from database import AsyncSessionLocal
from models.orm import RawMessage, TicketCreation, Worker
from models.schemas import TicketCreationLog
from services.ticket_system.sync import PHONE_MATCH_DIGITS, phone_match_key

logger = logging.getLogger(__name__)


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


async def record_ticket_creation_in(db, record: TicketCreationLog) -> TicketCreation:
    """El insert en sí, sobre una sesión que recibe de fuera — así las
    pruebas de flujo completo lo pueden correr dentro de su propia
    transacción."""
    creation = TicketCreation(
        worker_id=record.worker_id,
        title=record.title,
        entities=record.entities.model_dump(mode="json") if record.entities else None,
        priority=record.priority,
        client_id=uuid.UUID(record.client_id) if record.client_id else None,
        status=record.status.value,
        external_ticket_id=(
            uuid.UUID(record.external_ticket_id) if record.external_ticket_id else None
        ),
        ticket_number=record.ticket_number,
        error=record.error,
    )
    db.add(creation)
    await db.flush()

    if record.wamids:
        await db.execute(
            update(RawMessage)
            .where(RawMessage.wamid.in_(record.wamids))
            .values(ticket_creation_id=creation.id)
        )
    return creation


async def record_ticket_creation(record: TicketCreationLog) -> None:
    """Escribe la bitácora del intento de creación y vincula los
    `raw_messages` del bloque (CLAUDE.md, decisión #20).

    Se llama igual en éxito y en fallo. El orquestador la invoca dentro de un
    try/except a propósito: cuando el ticket ya se creó, una falla al
    registrarlo no puede tumbar el flujo ni provocar un segundo intento —
    perder la bitácora es malo, crear el ticket dos veces es peor."""
    async with AsyncSessionLocal() as db:
        await record_ticket_creation_in(db, record)
        await db.commit()

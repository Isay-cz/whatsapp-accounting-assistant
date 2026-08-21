"""
Bitácora `ticket_creations` a nivel base de datos (CLAUDE.md, decisión #20).

Prueba el recorder real —el que abre su propia sesión y hace commit— no el
mock que usan las pruebas del orquestador.
"""

import uuid

import pytest_asyncio
from sqlalchemy import select

from models.orm import RawMessage, TicketCreation, Worker
from models.schemas import CreationStatus, ExtractedEntities, TicketCreationLog, Tramite
from services.conversation.deps import record_ticket_creation


@pytest_asyncio.fixture
async def worker_comiteado(db_session):
    """El recorder abre su propia sesión, así que el trabajador y los
    mensajes tienen que existir fuera de la transacción del test. Se limpian
    al final a mano."""
    worker = Worker(
        phone_number=f"52155{uuid.uuid4().int % 100000000:08d}",
        name="Trabajador de bitácora",
        is_active=True,
        external_user_id=uuid.uuid4(),
    )
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        db.add(worker)
        await db.commit()
        await db.refresh(worker)

    yield worker

    async with AsyncSessionLocal() as db:
        await db.execute(
            RawMessage.__table__.delete().where(RawMessage.worker_id == worker.id)
        )
        await db.execute(
            TicketCreation.__table__.delete().where(TicketCreation.worker_id == worker.id)
        )
        await db.execute(Worker.__table__.delete().where(Worker.id == worker.id))
        await db.commit()


async def _raw_message(worker: Worker, wamid: str, body: str) -> None:
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        db.add(
            RawMessage(
                worker_id=worker.id,
                wamid=wamid,
                body=body,
                payload={"id": wamid, "type": "text"},
            )
        )
        await db.commit()


async def _creaciones(worker: Worker) -> list[TicketCreation]:
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(TicketCreation).where(TicketCreation.worker_id == worker.id)
        )
        return list(result.scalars().all())


async def test_registra_la_creacion_y_vincula_el_bloque(worker_comiteado):
    run = uuid.uuid4().hex[:8]
    wamids = [f"wamid.log.{run}.0", f"wamid.log.{run}.1"]
    for i, wamid in enumerate(wamids):
        await _raw_message(worker_comiteado, wamid, f"mensaje {i}")

    ticket_id = str(uuid.uuid4())
    await record_ticket_creation(
        TicketCreationLog(
            worker_id=worker_comiteado.id,
            wamids=wamids,
            title="Cliente pide factura",
            entities=ExtractedEntities(monto="$1,000", tramite=Tramite.factura),
            priority="alta",
            client_id=None,
            status=CreationStatus.created,
            external_ticket_id=ticket_id,
            ticket_number=482,
        )
    )

    creaciones = await _creaciones(worker_comiteado)
    assert len(creaciones) == 1
    creacion = creaciones[0]
    assert creacion.ticket_number == 482
    assert str(creacion.external_ticket_id) == ticket_id
    assert creacion.status == "created"
    # Las entidades se guardan porque son lo único de la descripción que no
    # se puede reconstruir desde los mensajes crudos.
    assert creacion.entities == {
        "monto": "$1,000",
        "fecha": None,
        "rfc": None,
        "periodo": None,
        "tramite": "factura",
    }

    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        filas = (
            (await db.execute(select(RawMessage).where(RawMessage.wamid.in_(wamids))))
            .scalars()
            .all()
        )
    assert {f.ticket_creation_id for f in filas} == {creacion.id}


async def test_registra_el_intento_fallido(worker_comiteado):
    await record_ticket_creation(
        TicketCreationLog(
            worker_id=worker_comiteado.id,
            wamids=[],
            title="Cliente pide factura",
            priority="media",
            status=CreationStatus.failed,
            error="503 Service Unavailable",
        )
    )

    creaciones = await _creaciones(worker_comiteado)
    assert len(creaciones) == 1
    assert creaciones[0].status == "failed"
    assert creaciones[0].ticket_number is None
    assert creaciones[0].external_ticket_id is None
    assert "503" in creaciones[0].error

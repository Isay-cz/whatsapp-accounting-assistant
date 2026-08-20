import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from models.orm import Worker
from models.schemas import WorkerSync
from services.ticket_system.sync import (
    normalize_phone,
    phone_match_key,
    upsert_workers,
    worker_sync_loop,
)


def _entry(**overrides) -> WorkerSync:
    base = {
        "user_id": str(uuid.uuid4()),
        "name": "Operativo Archivo",
        "whatsapp_phone": "+5215511112222",
        "bot_enabled": True,
        "is_active": True,
    }
    base.update(overrides)
    return WorkerSync(**base)


async def _workers(db_session) -> list[Worker]:
    return list((await db_session.execute(select(Worker))).scalars().all())


# --- Normalización de teléfonos ------------------------------------------


def test_normalize_phone_quita_todo_lo_que_no_sea_digito():
    assert normalize_phone("+52 155 1111-2222") == "5215511112222"
    assert normalize_phone("") == ""


def test_phone_match_key_ignora_el_uno_de_movil_mexicano():
    """Meta no es consistente con el "1" de móvil; el número nacional sí."""
    assert phone_match_key("+5215511112222") == phone_match_key("525511112222")


# --- Upsert ---------------------------------------------------------------


async def test_da_de_alta_un_trabajador_nuevo(db_session):
    entry = _entry()
    await upsert_workers(db_session, [entry])

    workers = await _workers(db_session)
    assert len(workers) == 1
    assert workers[0].phone_number == "5215511112222"
    assert workers[0].name == "Operativo Archivo"
    assert workers[0].is_active is True
    assert str(workers[0].external_user_id) == entry.user_id


async def test_bot_apagado_desactiva_localmente(db_session):
    await upsert_workers(db_session, [_entry(bot_enabled=False)])

    workers = await _workers(db_session)
    assert workers[0].is_active is False


async def test_usuario_inactivo_desactiva_localmente(db_session):
    await upsert_workers(db_session, [_entry(is_active=False)])

    workers = await _workers(db_session)
    assert workers[0].is_active is False


async def test_actualiza_en_vez_de_duplicar(db_session):
    entry = _entry()
    await upsert_workers(db_session, [entry])
    await upsert_workers(db_session, [_entry(user_id=entry.user_id, name="Nombre Nuevo")])

    workers = await _workers(db_session)
    assert len(workers) == 1
    assert workers[0].name == "Nombre Nuevo"


async def test_desaparecer_del_roster_desactiva(db_session):
    """Autocurativo: si le borran el número del otro lado, se apaga aquí."""
    await upsert_workers(db_session, [_entry()])
    await upsert_workers(db_session, [])

    workers = await _workers(db_session)
    assert len(workers) == 1
    assert workers[0].is_active is False


async def test_un_ciclo_perdido_se_corrige_en_el_siguiente(db_session):
    entry = _entry(bot_enabled=False)
    await upsert_workers(db_session, [entry])
    assert (await _workers(db_session))[0].is_active is False

    # El siguiente poll trae el estado correcto y reconcilia sin intervención.
    await upsert_workers(db_session, [_entry(user_id=entry.user_id, bot_enabled=True)])
    assert (await _workers(db_session))[0].is_active is True


# --- Loop -----------------------------------------------------------------


async def test_el_loop_sobrevive_a_una_falla_del_otro_lado():
    """Una caída de red no debe matar el poll: el siguiente ciclo reintenta."""
    ticket_system = AsyncMock()
    ticket_system.list_workers.side_effect = RuntimeError("sistema de tickets caído")

    task = asyncio.create_task(worker_sync_loop(ticket_system, interval_seconds=0.05))
    await asyncio.sleep(0.2)
    assert not task.done()
    assert ticket_system.list_workers.await_count > 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

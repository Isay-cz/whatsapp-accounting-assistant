"""Sincronización de la whitelist de trabajadores — el bot hace *pull*.

El sistema de tickets nunca le pega a este bot: la única superficie entrante
que existe aquí es el webhook de Meta (CLAUDE.md, decisión #1). Por eso la
whitelist se mantiene con un poll periódico contra `GET /internal/workers`
(decisión #15).

El poll es autocurativo por diseño: cada ciclo reconcilia la tabla completa,
así que perder un ciclo — o varios — no deja nada desincronizado de forma
permanente. No hay reintentos ni botón de resync manual, a propósito.
"""

import asyncio
import logging
import re
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from database import AsyncSessionLocal
from models.orm import Worker
from models.schemas import WorkerSync

logger = logging.getLogger(__name__)

# Cuántos dígitos finales se comparan para decidir si dos números son el mismo.
# Los celulares mexicanos llegan como 52XXXXXXXXXX o 521XXXXXXXXXX según el
# contexto (Meta no es consistente con el "1" de móvil), así que comparar la
# cadena completa produciría falsos negativos. Los últimos 10 dígitos son el
# número nacional, que sí es estable. Vale para un despacho 100% mexicano;
# revisar si algún día entran números de otro país.
PHONE_MATCH_DIGITS = 10


def normalize_phone(raw: str) -> str:
    """Deja solo los dígitos de un teléfono.

    El sistema de tickets guarda E.164 con `+` y Meta manda el número sin él;
    normalizar de los dos lados es lo que hace que el upsert haga match.
    """
    return re.sub(r"\D", "", raw or "")


def phone_match_key(raw: str) -> str:
    """Sufijo con el que se comparan dos teléfonos (ver PHONE_MATCH_DIGITS)."""
    return normalize_phone(raw)[-PHONE_MATCH_DIGITS:]


async def upsert_workers(session, roster: Iterable[WorkerSync]) -> None:
    """Reconcilia la tabla `workers` completa contra el roster recibido."""
    seen_keys: set[str] = set()

    for entry in roster:
        phone = normalize_phone(entry.whatsapp_phone)
        if not phone:
            continue
        seen_keys.add(phone_match_key(entry.whatsapp_phone))

        # El is_active local es la conjunción: alguien puede seguir activo en
        # el sistema de tickets pero tener el bot apagado.
        is_active = entry.bot_enabled and entry.is_active

        stmt = (
            insert(Worker)
            .values(
                phone_number=phone,
                name=entry.name,
                is_active=is_active,
                external_user_id=entry.user_id,
            )
            .on_conflict_do_update(
                index_elements=["phone_number"],
                set_={
                    "name": entry.name,
                    "is_active": is_active,
                    "external_user_id": entry.user_id,
                },
            )
        )
        await session.execute(stmt)

    # Los que ya no aparecen en el roster se desactivan localmente: es el caso
    # de alguien a quien le borraron el número del otro lado. Se desactivan en
    # vez de borrarse porque `raw_messages` los referencia por FK.
    existing = (await session.execute(select(Worker))).scalars().all()
    stale_ids = [
        w.id
        for w in existing
        if w.is_active and phone_match_key(w.phone_number) not in seen_keys
    ]
    if stale_ids:
        await session.execute(
            update(Worker).where(Worker.id.in_(stale_ids)).values(is_active=False)
        )
        logger.info("Whitelist: %d trabajador(es) desactivado(s).", len(stale_ids))

    await session.commit()


async def sync_once(ticket_system) -> None:
    roster = await ticket_system.list_workers()
    async with AsyncSessionLocal() as session:
        await upsert_workers(session, roster)
    logger.debug("Whitelist sincronizada: %d trabajador(es) en el roster.", len(roster))


async def worker_sync_loop(ticket_system, interval_seconds: int) -> None:
    """Bucle de poll. Nunca muere por un error: una caída de red o un 500 del
    otro lado se loguean y el siguiente ciclo reintenta solo."""
    while True:
        try:
            await sync_once(ticket_system)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falló la sincronización de la whitelist; se reintenta.")
        await asyncio.sleep(interval_seconds)

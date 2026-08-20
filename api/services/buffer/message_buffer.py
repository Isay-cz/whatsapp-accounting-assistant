import asyncio
import logging
from typing import Awaitable, Callable

from redis.asyncio import Redis

from ._tasks import track
from .keys import buffer_key, buffer_lock_key, buffer_marker_key, phone_from_buffer_key, KEY_PREFIX

logger = logging.getLogger(__name__)

OnBufferClose = Callable[[str, list[str]], Awaitable[None]]

# Margen de seguridad sobre el TTL real de los datos: la señal de "cerró el
# debounce" la da el marcador (`buffer_marker_key`), nunca la expiración
# física de la lista de mensajes — si usáramos la misma expiración para
# ambas cosas, el watcher podría despertar justo cuando Redis ya borró el
# contenido junto con la señal, perdiendo el bloque completo.
_STORAGE_SAFETY_MARGIN_SECONDS = 30


async def push_message(redis: Redis, phone: str, message: str, ttl_seconds: int) -> None:
    """Agrega un mensaje al bloque en curso y refresca la ventana de
    debounce — cada mensaje nuevo la reinicia."""
    key = buffer_key(phone)
    await redis.rpush(key, message)
    await redis.expire(key, ttl_seconds + _STORAGE_SAFETY_MARGIN_SECONDS)
    await redis.set(buffer_marker_key(phone), "1", ex=ttl_seconds)


async def spawn_watcher_if_needed(
    redis: Redis, phone: str, ttl_seconds: int, on_close: OnBufferClose
) -> None:
    """Asegura que exactamente un watcher esté corriendo para este teléfono.
    El lock es a nivel Redis (SET NX EX), no un dict en proceso — sigue
    siendo correcto si en el futuro corre más de una réplica del bot."""
    acquired = await redis.set(buffer_lock_key(phone), "1", nx=True, ex=ttl_seconds + 5)
    if not acquired:
        return
    task = asyncio.create_task(_watch(redis, phone, ttl_seconds, on_close))
    track(task)


async def _watch(redis: Redis, phone: str, ttl_seconds: int, on_close: OnBufferClose) -> None:
    key = buffer_key(phone)
    marker = buffer_marker_key(phone)
    try:
        wait_seconds: float = ttl_seconds
        while True:
            await asyncio.sleep(wait_seconds)
            # PTTL (ms) en vez de TTL (s, redondeado al segundo más cercano)
            # — con TTL, el redondeo puede hacer que el watcher despierte
            # hasta un segundo tarde respecto al cierre real del debounce.
            remaining_ms = await redis.pttl(marker)
            if remaining_ms is None or remaining_ms <= 0:
                break
            wait_seconds = remaining_ms / 1000

        messages = await redis.lrange(key, 0, -1)
        await redis.delete(key, marker)
        if messages:
            await on_close(phone, messages)
    except Exception:
        logger.exception("Watcher de buffer falló para %s", phone)
    finally:
        await redis.delete(buffer_lock_key(phone))


async def recover_watchers(redis: Redis, ttl_seconds: int, on_close: OnBufferClose) -> int:
    """Al arrancar, re-levanta watchers para buffers que quedaron abiertos
    por un restart a medio conteo — si no, esos bloques nunca se cierran y
    el flujo se pierde en silencio."""
    recovered = 0
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}buffer:*"):
        phone = phone_from_buffer_key(key)
        await spawn_watcher_if_needed(redis, phone, ttl_seconds, on_close)
        recovered += 1
    return recovered

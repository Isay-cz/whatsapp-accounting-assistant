import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis

from ._tasks import track
from .keys import KEY_PREFIX, phone_from_session_key, session_key, session_lock_key

logger = logging.getLogger(__name__)

OnTimeout = Callable[[str, dict[str, Any]], Awaitable[None]]

# Igual que en message_buffer: la sesión debe seguir leíble un poco después
# de `ttl_seconds` para que el watcher (que despierta justo a los
# `ttl_seconds`) alcance a leerla antes de que Redis la borre sola. La
# decisión de "ya venció" la toma la app comparando `step`, no la
# expiración física de la llave.
_STORAGE_SAFETY_MARGIN_SECONDS = 30


async def start_session(redis: Redis, phone: str, state: dict[str, Any], ttl_seconds: int) -> None:
    await redis.set(
        session_key(phone), json.dumps(state), ex=ttl_seconds + _STORAGE_SAFETY_MARGIN_SECONDS
    )


async def get_session(redis: Redis, phone: str) -> dict[str, Any] | None:
    raw = await redis.get(session_key(phone))
    return json.loads(raw) if raw else None


async def clear_session(redis: Redis, phone: str) -> None:
    await redis.delete(session_key(phone))


async def spawn_timeout_watcher(
    redis: Redis,
    phone: str,
    wait_seconds: int,
    expected_step: str,
    on_timeout: OnTimeout,
) -> None:
    """Dispara `on_timeout` una sola vez transcurrido `wait_seconds`, solo
    si la sesión sigue en `expected_step` (si el trabajador ya contestó, el
    `step` habrá avanzado y este watcher no hace nada)."""
    lock_key = session_lock_key(phone, expected_step)
    acquired = await redis.set(lock_key, "1", nx=True, ex=wait_seconds + 5)
    if not acquired:
        return
    task = asyncio.create_task(
        _watch(redis, phone, wait_seconds, expected_step, on_timeout, lock_key)
    )
    track(task)


async def _watch(
    redis: Redis,
    phone: str,
    wait_seconds: int,
    expected_step: str,
    on_timeout: OnTimeout,
    lock_key: str,
) -> None:
    try:
        await asyncio.sleep(wait_seconds)
        state = await get_session(redis, phone)
        if state is not None and state.get("step") == expected_step:
            await on_timeout(phone, state)
    except Exception:
        logger.exception("Watcher de timeout falló para %s (step=%s)", phone, expected_step)
    finally:
        await redis.delete(lock_key)


async def recover_watchers(
    redis: Redis, step_timeouts: dict[str, OnTimeout]
) -> int:
    """Al arrancar, re-levanta watchers de timeout para sesiones que
    quedaron a medias por un restart. El tiempo de espera usado es el TTL
    restante real de la llave, no el timeout completo del paso, para no
    alargar una espera que ya llevaba corriendo antes del restart."""
    recovered = 0
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}session:*"):
        phone = phone_from_session_key(key)
        state = await get_session(redis, phone)
        if not state:
            continue
        step = state.get("step")
        on_timeout = step_timeouts.get(step)
        if on_timeout is None:
            continue
        remaining = await redis.ttl(key)
        if remaining is None or remaining <= 0:
            continue
        # El TTL almacenado incluye el margen de seguridad de start_session
        # — se descuenta para no alargar el timeout real más de la cuenta.
        wait_seconds = max(0, remaining - _STORAGE_SAFETY_MARGIN_SECONDS)
        if wait_seconds == 0:
            await on_timeout(phone, state)
            continue
        await spawn_timeout_watcher(redis, phone, wait_seconds, step, on_timeout)
        recovered += 1
    return recovered

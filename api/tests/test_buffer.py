import asyncio

from models.schemas import BufferedMessage
from services.buffer import message_buffer


def bodies(messages: list[BufferedMessage]) -> list[str]:
    return [m.body for m in messages]


async def test_buffer_closes_and_groups_messages(redis_client):
    closed = []

    async def on_close(phone, messages):
        closed.append((phone, messages))

    phone = "16315551181"
    await message_buffer.push_message(
        redis_client, phone, "hola", ttl_seconds=1, wamid="wamid.1"
    )
    await message_buffer.spawn_watcher_if_needed(redis_client, phone, ttl_seconds=1, on_close=on_close)
    await message_buffer.push_message(
        redis_client, phone, "necesito mi cfdi", ttl_seconds=1, wamid="wamid.2"
    )

    await asyncio.sleep(1.5)

    assert len(closed) == 1
    got_phone, messages = closed[0]
    assert got_phone == phone
    assert bodies(messages) == ["hola", "necesito mi cfdi"]
    # El wamid viaja con el mensaje para poder vincular después la fila de
    # `raw_messages` con el ticket (CLAUDE.md, decisión #20).
    assert [m.wamid for m in messages] == ["wamid.1", "wamid.2"]
    assert await redis_client.exists(f"bot:buffer:{phone}") == 0


async def test_buffer_ttl_refresh_delays_close(redis_client):
    closed = []

    async def on_close(phone, messages):
        closed.append((phone, bodies(messages)))

    phone = "16315551182"
    await message_buffer.push_message(redis_client, phone, "primero", ttl_seconds=1)
    await message_buffer.spawn_watcher_if_needed(redis_client, phone, ttl_seconds=1, on_close=on_close)

    await asyncio.sleep(0.6)
    await message_buffer.push_message(redis_client, phone, "segundo", ttl_seconds=1)  # refresca el TTL

    await asyncio.sleep(0.6)
    assert closed == []  # el refresh debió correr el cierre más adelante

    await asyncio.sleep(0.7)
    assert closed == [(phone, ["primero", "segundo"])]


async def test_spawn_watcher_is_idempotent_per_phone(redis_client):
    call_count = 0

    async def on_close(phone, messages):
        nonlocal call_count
        call_count += 1

    phone = "16315551183"
    await message_buffer.push_message(redis_client, phone, "uno", ttl_seconds=1)
    for _ in range(3):
        await message_buffer.spawn_watcher_if_needed(redis_client, phone, ttl_seconds=1, on_close=on_close)

    await asyncio.sleep(1.5)
    assert call_count == 1


async def test_entradas_en_formato_viejo_no_se_pierden(redis_client):
    """Transición: los bloques que ya estaban en Redis cuando se desplegó el
    cambio de formato traen el texto pelón, sin `wamid`. Duran menos de un
    minuto, pero perder uno es perder un ticket."""
    closed = []

    async def on_close(phone, messages):
        closed.append(messages)

    phone = "16315551184"
    # Escrito a mano tal como lo dejaba la versión anterior de push_message.
    await redis_client.rpush(f"bot:buffer:{phone}", "mensaje del formato viejo")
    await redis_client.set(f"bot:markers:buffer:{phone}", "1", ex=1)
    await message_buffer.spawn_watcher_if_needed(redis_client, phone, ttl_seconds=1, on_close=on_close)

    await asyncio.sleep(1.5)

    assert len(closed) == 1
    assert bodies(closed[0]) == ["mensaje del formato viejo"]
    assert closed[0][0].wamid is None

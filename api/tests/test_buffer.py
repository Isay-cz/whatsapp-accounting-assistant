import asyncio

from services.buffer import message_buffer


async def test_buffer_closes_and_groups_messages(redis_client):
    closed = []

    async def on_close(phone, messages):
        closed.append((phone, messages))

    phone = "16315551181"
    await message_buffer.push_message(redis_client, phone, "hola", ttl_seconds=1)
    await message_buffer.spawn_watcher_if_needed(redis_client, phone, ttl_seconds=1, on_close=on_close)
    await message_buffer.push_message(redis_client, phone, "necesito mi cfdi", ttl_seconds=1)

    await asyncio.sleep(1.5)

    assert closed == [(phone, ["hola", "necesito mi cfdi"])]
    assert await redis_client.exists(f"bot:buffer:{phone}") == 0


async def test_buffer_ttl_refresh_delays_close(redis_client):
    closed = []

    async def on_close(phone, messages):
        closed.append((phone, messages))

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

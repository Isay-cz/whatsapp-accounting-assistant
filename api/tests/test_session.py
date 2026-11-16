import asyncio

from services.buffer import session


async def test_timeout_watcher_fires_when_step_unchanged(redis_client):
    fired = []

    async def on_timeout(phone, state):
        fired.append((phone, state))

    phone = "16315551184"
    await session.start_session(redis_client, phone, {"step": "awaiting_priority"}, ttl_seconds=1)
    await session.spawn_timeout_watcher(
        redis_client, phone, wait_seconds=1, expected_step="awaiting_priority", on_timeout=on_timeout
    )

    await asyncio.sleep(1.3)
    assert fired == [(phone, {"step": "awaiting_priority"})]


async def test_timeout_watcher_noop_when_step_advanced(redis_client):
    fired = []

    async def on_timeout(phone, state):
        fired.append((phone, state))

    phone = "16315551185"
    await session.start_session(redis_client, phone, {"step": "awaiting_client"}, ttl_seconds=5)
    await session.spawn_timeout_watcher(
        redis_client, phone, wait_seconds=1, expected_step="awaiting_client", on_timeout=on_timeout
    )

    await asyncio.sleep(0.3)
    await session.start_session(redis_client, phone, {"step": "awaiting_priority"}, ttl_seconds=5)  # avanzó

    await asyncio.sleep(1.0)
    assert fired == []


async def test_get_session_returns_none_when_absent(redis_client):
    assert await session.get_session(redis_client, "no-existe") is None


async def test_clear_session(redis_client):
    phone = "16315551186"
    await session.start_session(redis_client, phone, {"step": "awaiting_client"}, ttl_seconds=5)
    await session.clear_session(redis_client, phone)
    assert await session.get_session(redis_client, phone) is None


async def test_update_session_keeps_ttl(redis_client):
    """Anotar algo en la sesión no le regala tiempo al paso en curso."""
    phone = "16315551186"
    await session.start_session(redis_client, phone, {"step": "awaiting_client"}, ttl_seconds=5)
    key = f"bot:session:{phone}"
    ttl_before = await redis_client.pttl(key)

    await asyncio.sleep(0.2)
    updated = await session.update_session(
        redis_client, phone, {"step": "awaiting_client", "client_options": ["c1"]}
    )

    assert updated is True
    assert (await session.get_session(redis_client, phone))["client_options"] == ["c1"]
    ttl_after = await redis_client.pttl(key)
    assert 0 < ttl_after < ttl_before


async def test_update_session_does_not_revive_an_expired_session(redis_client):
    """Sin `XX`, escribir con `KEEPTTL` sobre una llave que ya venció crearía
    una sesión sin vencimiento."""
    phone = "16315551187"
    updated = await session.update_session(redis_client, phone, {"step": "awaiting_client"})

    assert updated is False
    assert await session.get_session(redis_client, phone) is None

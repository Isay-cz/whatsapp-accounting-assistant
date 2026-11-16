"""`/health` dice si el bot puede atender, no solo si el proceso está vivo."""

from unittest.mock import AsyncMock

import main
from main import app


async def test_health_ok_con_postgres_y_redis(client, redis_client):
    app.state.redis = redis_client
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_503_si_redis_no_contesta(client):
    redis = AsyncMock()
    redis.ping.side_effect = ConnectionError("redis caído en 10.0.0.5:6379")
    app.state.redis = redis

    response = await client.get("/health")

    assert response.status_code == 503
    # Es pública detrás del proxy: el detalle va al log, no a la respuesta.
    assert response.json() == {"status": "unavailable"}
    assert "10.0.0.5" not in response.text


async def test_health_503_si_postgres_no_contesta(client, redis_client, monkeypatch):
    app.state.redis = redis_client

    class EngineCaido:
        def connect(self):
            raise OSError("connection refused: postgres:5432")

    monkeypatch.setattr(main, "engine", EngineCaido())

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


async def test_health_503_si_una_dependencia_se_cuelga(client, redis_client, monkeypatch):
    """Un Postgres que acepta la conexión y no contesta tiene que verse como
    caído, no dejar a la sonda esperando."""
    import asyncio

    app.state.redis = redis_client
    monkeypatch.setattr(main, "HEALTH_TIMEOUT_SECONDS", 0.1)

    async def colgado(request):
        await asyncio.sleep(5)

    monkeypatch.setattr(main, "_check_dependencies", colgado)

    response = await client.get("/health")

    assert response.status_code == 503

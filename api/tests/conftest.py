"""
Requiere un Postgres y un Redis desechables alcanzables vía DATABASE_URL /
REDIS_URL (ver docker-compose.test.yml) — nunca corre contra el stack real
de cgho-ops. Espeja el patrón de tests del repo hermano: una transacción
por test que se revierte al final (tests/conftest.py de cgho-ops).
"""

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

# Defaults de prueba — se aplican solo si el entorno no los definió ya
# (docker-compose.test.yml / CI son quienes normalmente los proveen).
os.environ.setdefault(
    "DATABASE_URL", "postgresql://bot_test:bot_test@localhost:5432/bot_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")
os.environ.setdefault("TICKET_SYSTEM_BASE_URL", "http://ticket-system.test")

# Número del trabajador de prueba — el mismo que traen los payloads
# estáticos de tests/fixtures/webhook_payloads/.
WORKER_PHONE = "16315551181"

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from config import get_settings  # noqa: E402
from database import get_db  # noqa: E402
from main import app  # noqa: E402
from models.orm import Worker  # noqa: E402


def _async_database_url() -> str:
    return get_settings().database_url.replace("postgresql://", "postgresql+asyncpg://")


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine():
    """`database.engine` se crea una vez al importar el módulo, pero cada test
    corre en su propio event loop. Una conexión que quedó en el pool de un
    loop ya cerrado revienta —o peor, se cuelga— cuando el siguiente test la
    reutiliza. Se vacía el pool al terminar cada test.

    Aplica a todo test que toque `AsyncSessionLocal` (el recorder de la
    bitácora, el `get_db` real de las pruebas live); para los demás es un
    no-op barato."""
    yield
    from database import engine

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(_async_database_url(), pool_pre_ping=True)
    async with engine.connect() as conn:
        trans = await conn.begin()
        session_factory = async_sessionmaker(conn, expire_on_commit=False)
        async with session_factory() as session:
            yield session
        await trans.rollback()
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def worker(db_session: AsyncSession) -> Worker:
    w = Worker(
        phone_number=WORKER_PHONE,
        name="Trabajador de prueba",
        is_active=True,
        external_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    )
    db_session.add(w)
    await db_session.flush()
    return w


@pytest_asyncio.fixture
async def redis_client():
    from services.buffer import get_redis

    redis = get_redis(get_settings())
    await redis.flushdb()
    yield redis
    await redis.flushdb()
    await redis.aclose()


@pytest_asyncio.fixture
async def mocked_app_state():
    """Reemplaza el estado que normalmente arma el `lifespan` de main.py con
    mocks — los tests de dispatcher del webhook solo verifican que se llame
    al método correcto según el `field`, no el comportamiento real de
    ConversationFlow (eso lo cubre test_conversation_flow.py)."""
    conversation_flow = AsyncMock()
    alert_notifier = AsyncMock()
    app.state.conversation_flow = conversation_flow
    app.state.alert_notifier = alert_notifier
    yield conversation_flow, alert_notifier


def sign_payload(body: bytes) -> str:
    secret = get_settings().meta_app_secret
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"



async def wait_for(predicate, *, timeout: float = 6.0, message: str = "") -> None:
    """Espera activa por un efecto asíncrono (cierre de buffer, timeout de
    sesión, respuesta del LLM) en vez de dormir un tiempo fijo: mantiene los
    tests rápidos y evita que un margen apretado los vuelva intermitentes."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message or "La condición esperada nunca se cumplió")

# -- Builders de payloads de webhook ---------------------------------------
#
# Los payloads estáticos de tests/fixtures/webhook_payloads/ sirven para
# probar el dispatcher (una forma fija, un caso). Un flujo completo necesita
# varios mensajes distintos en secuencia — cada uno con su `wamid`, porque el
# webhook deduplica por esa llave — así que se arman en código.


def text_message_payload(
    text: str, *, wamid: str, from_number: str = WORKER_PHONE
) -> dict:
    """Payload `messages` de un mensaje de texto entrante, con la misma forma
    que manda Cloud API (ver docs/whatsapp-webhook-reference.md)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_id_1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456123",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Trabajador de prueba"},
                                    "wa_id": from_number,
                                }
                            ],
                            "messages": [
                                {
                                    "id": wamid,
                                    "from": from_number,
                                    "timestamp": "1504902988",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def interactive_reply_payload(
    reply_id: str,
    *,
    wamid: str,
    reply_type: str = "list_reply",
    title: str = "",
    from_number: str = WORKER_PHONE,
) -> dict:
    """Respuesta a una lista (`list_reply`) o a botones (`button_reply`). El
    `id` es el que el bot puso al armar la opción — nunca texto libre."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_id_1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "123456123",
                            },
                            "messages": [
                                {
                                    "id": wamid,
                                    "from": from_number,
                                    "timestamp": "1504903100",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": reply_type,
                                        reply_type: {"id": reply_id, "title": title},
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def post_webhook(client, payload: dict):
    """POST firmado al webhook, tal como llegaría de Meta."""
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_payload(body),
        },
    )

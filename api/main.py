import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from config import get_settings
from routes.webhook import router as webhook_router
from services.alerts import get_alert_notifier
from services.buffer import get_redis, message_buffer, session
from services.conversation import (
    ConversationFlow,
    lookup_active_worker_by_phone,
    record_ticket_creation,
)
from services.llm import get_extractor
from services.meta import get_meta_client
from services.ticket_system import get_ticket_system_client, worker_sync_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = get_redis(settings)
    ticket_system = get_ticket_system_client(settings)
    conversation_flow = ConversationFlow(
        redis=redis,
        settings=settings,
        llm=get_extractor(settings),
        meta=get_meta_client(settings),
        ticket_system=ticket_system,
        worker_lookup=lookup_active_worker_by_phone,
        record_creation=record_ticket_creation,
    )
    app.state.redis = redis
    app.state.conversation_flow = conversation_flow
    app.state.alert_notifier = get_alert_notifier()

    # Re-levanta watchers de buffers/sesiones que quedaron a medias por un
    # restart durante una ventana de debounce (ver CLAUDE.md, services/buffer).
    recovered_buffers = await message_buffer.recover_watchers(
        redis, settings.buffer_ttl_seconds, conversation_flow.on_buffer_close
    )
    recovered_sessions = await session.recover_watchers(
        redis, conversation_flow.step_timeout_handlers
    )
    logger.info(
        "Watchers recuperados al iniciar: %s buffers, %s sesiones",
        recovered_buffers,
        recovered_sessions,
    )

    # Poll de la whitelist contra el sistema de tickets (CLAUDE.md, decisión
    # #15). Es la única forma en que `workers` se mantiene al día: el sistema
    # de tickets nunca le pega a este bot.
    sync_task = asyncio.create_task(
        worker_sync_loop(ticket_system, settings.worker_sync_interval_seconds)
    )

    yield

    sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass

    await redis.aclose()


app = FastAPI(
    title="WhatsApp Bot — CGHO Contadores",
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"status": "ok"}

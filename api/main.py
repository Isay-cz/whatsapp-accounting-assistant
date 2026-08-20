import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from config import get_settings
from routes.webhook import router as webhook_router
from services.alerts import get_alert_notifier
from services.buffer import get_redis, message_buffer, session
from services.conversation import ConversationFlow, lookup_active_worker_by_phone
from services.llm import get_extractor
from services.meta import get_meta_client
from services.ticket_system import get_ticket_system_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = get_redis(settings)
    conversation_flow = ConversationFlow(
        redis=redis,
        settings=settings,
        llm=get_extractor(settings),
        meta=get_meta_client(settings),
        ticket_system=get_ticket_system_client(settings),
        worker_lookup=lookup_active_worker_by_phone,
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

    yield

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

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from core.security import verify_meta_signature
from database import get_db
from models.orm import RawMessage, Worker
from models.schemas import InteractiveReply
from services.alerts import AlertNotifier
from services.conversation import ConversationFlow
from services.conversation.deps import active_worker_by_phone_stmt

logger = logging.getLogger(__name__)
router = APIRouter()

# Best-effort: orden conocido de los tiers de calidad de Meta, de peor a
# mejor. No está confirmado contra la documentación oficial más reciente —
# revisar antes de depender de esto en producción. Si un valor no está en
# la lista, se trata como "no degradación" (default seguro: no alertar de
# más) y se deja el valor crudo en el log para inspección manual.
_QUALITY_TIER_ORDER = [
    "TIER_NOT_SET",
    "TIER_50",
    "TIER_250",
    "TIER_1K",
    "TIER_10K",
    "TIER_100K",
    "TIER_UNLIMITED",
]

_INFORMATIONAL_FIELDS = {
    "account_alerts",
    "message_template_quality_update",
    "message_template_status_update",
    "phone_number_name_update",
}


@router.get("/webhook")
async def verify_webhook(request: Request, settings: Settings = Depends(get_settings)):
    """Handshake de suscripción de Meta — se llama una vez al registrar la
    URL del webhook en el dashboard de Meta for Developers."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == settings.meta_verify_token and challenge is not None:
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verificación de webhook fallida")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(settings.meta_app_secret, raw_body, signature):
        logger.warning("Firma X-Hub-Signature-256 inválida — posible request no autorizado")
        raise HTTPException(status_code=403, detail="Firma inválida")

    payload = await request.json()
    conversation_flow: ConversationFlow = request.app.state.conversation_flow
    alert_notifier: AlertNotifier = request.app.state.alert_notifier

    # Nunca asumir longitud 1 — en producción `entry` y `changes` a veces
    # traen más de un elemento (ver docs/whatsapp-webhook-reference.md).
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            field = change.get("field")
            value = change.get("value", {}) or {}
            await _dispatch_field(field, value, db, conversation_flow, alert_notifier, background_tasks)

    # Responder 200 rápido siempre — el trabajo pesado ya quedó en background tasks.
    return PlainTextResponse("", status_code=200)


async def _dispatch_field(
    field: str | None,
    value: dict,
    db: AsyncSession,
    conversation_flow: ConversationFlow,
    alert_notifier: AlertNotifier,
    background_tasks: BackgroundTasks,
) -> None:
    if field == "messages":
        await _handle_messages(value, db, conversation_flow, background_tasks)
    elif field == "account_update":
        await alert_notifier.notify("account_update", f"event={value.get('event')}")
    elif field == "phone_number_quality_update":
        await _handle_quality_update(value, alert_notifier)
    elif field == "security":
        await alert_notifier.notify("security", f"event={value.get('event')}")
    elif field in _INFORMATIONAL_FIELDS:
        logger.info("Evento informativo de Meta: field=%s value=%s", field, value)
    elif field == "calls":
        pass  # explícitamente no suscrito — ver docs/whatsapp-webhook-reference.md
    else:
        logger.info("Evento de Meta sin manejar: field=%s", field)


async def _handle_messages(
    value: dict,
    db: AsyncSession,
    conversation_flow: ConversationFlow,
    background_tasks: BackgroundTasks,
) -> None:
    for message in value.get("messages", []) or []:
        wamid = message.get("id")
        from_number = message.get("from")
        if not wamid or not from_number:
            continue  # payload incompleto — no hay nada seguro que hacer con él

        existing = await db.execute(select(RawMessage).where(RawMessage.wamid == wamid))
        if existing.scalar_one_or_none() is not None:
            continue  # ya procesado — Meta reintenta entregas hasta 7 días

        result = await db.execute(
            active_worker_by_phone_stmt(from_number)
        )
        worker = result.scalar_one_or_none()
        if worker is None:
            logger.info("Número no autorizado intentó enviar mensaje: %s", from_number)
            continue

        raw_message = RawMessage(
            worker_id=worker.id,
            wamid=wamid,
            body=_message_body(message),
            payload=message,
        )
        db.add(raw_message)
        await db.commit()

        background_tasks.add_task(_process_message, from_number, message, conversation_flow)


def _message_body(message: dict) -> str:
    msg_type = message.get("type")
    if msg_type == "text":
        return message.get("text", {}).get("body", "")
    if msg_type == "interactive":
        reply = _parse_interactive_reply(message)
        return reply.title if reply else ""
    return ""


def _parse_interactive_reply(message: dict) -> InteractiveReply | None:
    interactive = message.get("interactive", {}) or {}
    reply_type = interactive.get("type")
    reply_data = interactive.get(reply_type) if reply_type else None
    if not reply_type or not reply_data or not reply_data.get("id"):
        return None
    return InteractiveReply(type=reply_type, id=reply_data["id"], title=reply_data.get("title", ""))


async def _process_message(phone: str, message: dict, conversation_flow: ConversationFlow) -> None:
    """Trabajo pesado (LLM, llamadas al sistema de tickets) fuera del ciclo
    request/response del webhook."""
    msg_type = message.get("type")
    if msg_type == "text":
        text = message.get("text", {}).get("body", "").strip()
        if text:
            # El wamid viaja hasta el buffer para poder vincular después esta
            # fila de `raw_messages` con el ticket que genere (decisión #20).
            await conversation_flow.handle_incoming_message(
                phone, text, wamid=message.get("id")
            )
    elif msg_type == "interactive":
        reply = _parse_interactive_reply(message)
        if reply:
            await conversation_flow.handle_interactive_reply(phone, reply.id)
    else:
        logger.info("Tipo de mensaje sin manejar: %s", msg_type)


async def _handle_quality_update(value: dict, alert_notifier: AlertNotifier) -> None:
    old_limit = value.get("old_limit")
    current_limit = value.get("current_limit")
    if _is_tier_degradation(old_limit, current_limit):
        await alert_notifier.notify(
            "phone_number_quality_update", f"Límite bajó de {old_limit} a {current_limit}"
        )
    else:
        logger.info(
            "phone_number_quality_update sin degradación detectada: %s -> %s",
            old_limit,
            current_limit,
        )


def _is_tier_degradation(old_limit: str | None, current_limit: str | None) -> bool:
    if old_limit not in _QUALITY_TIER_ORDER or current_limit not in _QUALITY_TIER_ORDER:
        return False
    return _QUALITY_TIER_ORDER.index(current_limit) < _QUALITY_TIER_ORDER.index(old_limit)

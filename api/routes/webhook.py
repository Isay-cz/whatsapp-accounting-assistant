from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from twilio.request_validator import RequestValidator
from config import get_settings, Settings
from database import get_db
from models.orm import Worker, RawMessage
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


async def validate_twilio_signature(request: Request, settings: Settings) -> dict:
    """
    Twilio firma cada webhook con HMAC-SHA1.
    Si la firma no coincide, el request no viene de Twilio.
    Nunca saltear esta validación en producción.
    """
    validator = RequestValidator(settings.twilio_auth_token)
    form_data = await request.form()
    params = dict(form_data)
    url = str(request.url)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, params, signature):
        logger.warning("Firma Twilio inválida — posible request no autorizado")
        raise HTTPException(status_code=403, detail="Firma inválida")

    return params


async def get_active_worker(phone: str, db: AsyncSession) -> Worker:
    """
    Busca al trabajador por su número de teléfono.
    Si no está en la whitelist o está inactivo, rechaza.
    """
    # Twilio envía el número como "whatsapp:+521XXXXXXXXXX"
    clean_phone = phone.replace("whatsapp:", "")
    result = await db.execute(
        select(Worker).where(
            Worker.phone_number == clean_phone,
            Worker.is_active == True
        )
    )
    worker = result.scalar_one_or_none()
    if not worker:
        logger.info(f"Número no autorizado intentó enviar mensaje: {clean_phone}")
        raise HTTPException(status_code=403, detail="Número no autorizado")
    return worker


@router.post("/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # 1. Validar firma Twilio
    params = await validate_twilio_signature(request, settings)

    # 2. Extraer campos del payload
    from_number = params.get("From", "")
    body = params.get("Body", "").strip()
    message_sid = params.get("MessageSid", "")

    if not body:
        # Twilio puede enviar webhooks por mensajes multimedia sin texto
        return PlainTextResponse("", status_code=200)

    # 3. Validar whitelist
    worker = await get_active_worker(from_number, db)

    # 4. Guardar mensaje crudo — siempre, antes de cualquier procesamiento
    raw_message = RawMessage(
        worker_id=worker.id,
        twilio_sid=message_sid,
        body=body,
        forwarded_body=_extract_forwarded_body(body),
        twilio_payload=dict(params),
    )
    db.add(raw_message)
    await db.commit()
    await db.refresh(raw_message)

    logger.info(f"Mensaje guardado: {raw_message.id} | Worker: {worker.name}")

    # 5. TODO Fase 2: disparar pipeline NLP (aquí irá el call al extractor)

    # 6. Respuesta TwiML vacía — Twilio espera 200 aunque no respondamos al usuario todavía
    return PlainTextResponse("", status_code=200)


def _extract_forwarded_body(body: str) -> str | None:
    """
    WhatsApp marca los mensajes reenviados con un encabezado.
    Esta función extrae solo el cuerpo del mensaje original.
    Ejemplo de body reenviado:
        [Forwarded]
        Cliente dice que necesita su CFDI de enero por $8,500
    """
    lines = body.splitlines()
    forwarded_indicators = {"[forwarded]", "[reenviado]", "forwarded message"}
    for i, line in enumerate(lines):
        if line.strip().lower() in forwarded_indicators:
            return "\n".join(lines[i+1:]).strip()
    return body  # Si no detecta encabezado, guarda el body completo
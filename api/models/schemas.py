from enum import Enum

from pydantic import BaseModel


class Priority(str, Enum):
    alta = "alta"
    media = "media"
    baja = "baja"


class ClientSearchResult(BaseModel):
    id: str
    name: str


class InteractiveReply(BaseModel):
    """Respuesta estructurada de una lista o botones de WhatsApp — el `id`
    es el UUID real que el bot puso al armar la opción (ver
    docs/bot-diseno-flujo-cliente.md), nunca texto libre."""

    type: str  # "list_reply" | "button_reply"
    id: str
    title: str

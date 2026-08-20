from enum import Enum

from pydantic import BaseModel


class Priority(str, Enum):
    alta = "alta"
    media = "media"
    baja = "baja"


class ClientSearchResult(BaseModel):
    id: str
    name: str


class CreatedTicket(BaseModel):
    """Respuesta de POST /internal/tickets. El `ticket_number` es lo que se le
    confirma al trabajador — un UUID es incómodo de leer o repetir por
    WhatsApp (ver CLAUDE.md, decisión #16)."""

    id: str
    ticket_number: int


class WorkerSync(BaseModel):
    """Una fila del roster que devuelve GET /internal/workers.

    `bot_enabled` e `is_active` llegan por separado; el `is_active` local de
    la tabla `workers` es la conjunción de los dos."""

    user_id: str
    name: str
    whatsapp_phone: str
    bot_enabled: bool
    is_active: bool


class InteractiveReply(BaseModel):
    """Respuesta estructurada de una lista o botones de WhatsApp — el `id`
    es el UUID real que el bot puso al armar la opción (ver
    docs/bot-diseno-flujo-cliente.md), nunca texto libre."""

    type: str  # "list_reply" | "button_reply"
    id: str
    title: str

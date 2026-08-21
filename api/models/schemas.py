from enum import Enum
from uuid import UUID

from pydantic import BaseModel


class Priority(str, Enum):
    alta = "alta"
    media = "media"
    baja = "baja"


class Tramite(str, Enum):
    """Lista cerrada de trámites. Es el único campo de `ExtractedEntities`
    que el LLM *infiere* en vez de copiar, así que su defensa no puede ser la
    regla verbatim: lo que no caiga exactamente en esta lista se descarta.
    Ampliarla es agregar un miembro aquí — nada más lee estos valores fuera
    de `TRAMITE_LABELS`."""

    factura = "factura"
    nomina = "nomina"
    declaracion = "declaracion"
    contabilidad = "contabilidad"
    imss = "imss"
    constancia = "constancia"


TRAMITE_LABELS = {
    Tramite.factura: "Factura",
    Tramite.nomina: "Nómina",
    Tramite.declaracion: "Declaración",
    Tramite.contabilidad: "Contabilidad",
    Tramite.imss: "IMSS",
    Tramite.constancia: "Constancia",
}


class ExtractedEntities(BaseModel):
    """Datos que el LLM señala dentro del bloque de mensajes, para anteponer
    a la descripción del ticket. Todos opcionales: lo que no aparezca queda
    en `None` y no se renderiza.

    Los cuatro primeros son *copias literales* del texto crudo — se validan
    con esa regla en `services/llm/entities.py` (CLAUDE.md, decisión #19).
    Deliberadamente no hay campo de cliente ni de prioridad: eso lo confirma
    el trabajador, nunca el LLM (decisiones #5 y #9)."""

    monto: str | None = None
    fecha: str | None = None
    rfc: str | None = None
    periodo: str | None = None
    tramite: Tramite | None = None

    def is_empty(self) -> bool:
        return not any(self.model_dump().values())


class BufferedMessage(BaseModel):
    """Un mensaje dentro de la ventana de debounce. Lleva el `wamid` para
    poder vincular después la fila de `raw_messages` con el ticket que ese
    bloque generó (CLAUDE.md, decisión #20).

    `wamid` es opcional por la transición: los bloques que ya estaban en
    Redis cuando se desplegó este cambio traen solo el cuerpo del mensaje."""

    wamid: str | None = None
    body: str


class ClientSearchResult(BaseModel):
    id: str
    name: str


class CreatedTicket(BaseModel):
    """Respuesta de POST /internal/tickets. El `ticket_number` es lo que se le
    confirma al trabajador — un UUID es incómodo de leer o repetir por
    WhatsApp (ver CLAUDE.md, decisión #16)."""

    id: str
    ticket_number: int


class CreationStatus(str, Enum):
    created = "created"
    failed = "failed"


class TicketCreationLog(BaseModel):
    """Lo que se registra en `ticket_creations`: la bitácora de lo que este
    bot *mandó*, no un espejo del ticket (CLAUDE.md, decisión #20). Se
    escribe igual cuando la creación falla, con `status=failed` y el error.

    No guarda la descripción: se reconstruye desde los `raw_messages`
    vinculados más `title` y `entities`, que es lo único de la descripción
    que no es derivable del texto crudo."""

    worker_id: UUID
    wamids: list[str]
    title: str
    entities: ExtractedEntities | None = None
    priority: str
    client_id: str | None = None
    status: CreationStatus
    external_ticket_id: str | None = None
    ticket_number: int | None = None
    error: str | None = None


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

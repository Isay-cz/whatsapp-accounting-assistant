import logging
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis

from config import Settings
from models.orm import Worker
from models.schemas import (
    BufferedMessage,
    CreationStatus,
    ExtractedEntities,
    Priority,
    TicketCreationLog,
)
from services.buffer import message_buffer, session
from services.llm.base import LLMExtractor
from services.meta import MetaClient
from services.ticket_system import TicketSystemClient

from .description import build_description

logger = logging.getLogger(__name__)

STEP_AWAITING_CLIENT = "awaiting_client"
STEP_AWAITING_PRIORITY = "awaiting_priority"

PRIORITY_OPTIONS = [
    {"id": Priority.alta.value, "title": "Alta"},
    {"id": Priority.media.value, "title": "Media"},
    {"id": Priority.baja.value, "title": "Baja"},
]
DEFAULT_PRIORITY = Priority.media.value

# Opción fija que acompaña siempre a las coincidencias de clientes. Existe para
# que el trabajador pueda decir "es trabajo interno" de inmediato, en vez de
# tener que dejar pasar el timeout para conseguir lo mismo.
NO_CLIENT_OPTION_ID = "__sin_cliente__"
NO_CLIENT_OPTION_TITLE = "Sin cliente"
# Tope de coincidencias: 9 + "Sin cliente" = los 10 que permite la lista
# interactiva de WhatsApp.
MAX_CLIENT_MATCHES = 9

ASK_PRIORITY_TEXT = "¿Qué prioridad tiene?"
CREATION_FAILED_TEXT = (
    "No se pudo crear el ticket ahora mismo. Tus mensajes no se perdieron: "
    "vuelve a elegir la prioridad para reintentar."
)
NO_EXTERNAL_USER_TEXT = (
    "No se pudo crear el ticket: tu usuario no está vinculado al sistema de "
    "tickets todavía. Avisa a un administrador."
)

WorkerLookup = Callable[[str], Awaitable[Worker | None]]
CreationRecorder = Callable[[TicketCreationLog], Awaitable[None]]


class ConversationFlow:
    """Orquesta el flujo post-buffer (paso 3 en adelante del flujo end-to-end
    de CLAUDE.md): extracción de título y entidades, confirmación de cliente,
    prioridad y creación del ticket. Usa `services/buffer` para el estado de
    sesión (`bot:session:{phone}`) con el mismo mecanismo de TTL que el
    buffer de mensajes.

    No conoce SQLAlchemy: lo que necesita de la base entra por
    `worker_lookup` y `record_creation`, igual que el resto de sus
    dependencias."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        llm: LLMExtractor,
        meta: MetaClient,
        ticket_system: TicketSystemClient,
        worker_lookup: WorkerLookup,
        record_creation: CreationRecorder,
    ):
        self._redis = redis
        self._settings = settings
        self._llm = llm
        self._meta = meta
        self._ticket_system = ticket_system
        self._worker_lookup = worker_lookup
        self._record_creation = record_creation

    @property
    def step_timeout_handlers(self) -> dict[str, session.OnTimeout]:
        return {
            STEP_AWAITING_CLIENT: self._on_client_timeout,
            STEP_AWAITING_PRIORITY: self._on_priority_timeout,
        }

    # -- Entrada desde el webhook --------------------------------------

    async def handle_incoming_message(self, phone: str, text: str, wamid: str | None = None) -> None:
        """Punto de entrada único para un mensaje de texto ya validado
        contra la whitelist. Decide si es un mensaje nuevo a bufferizar o
        la respuesta a la pregunta de cliente en curso."""
        state = await session.get_session(self._redis, phone)
        if state is not None and state.get("step") == STEP_AWAITING_CLIENT:
            await self._handle_client_reply(phone, text)
            return

        await message_buffer.push_message(
            self._redis, phone, text, self._settings.buffer_ttl_seconds, wamid=wamid
        )
        await message_buffer.spawn_watcher_if_needed(
            self._redis, phone, self._settings.buffer_ttl_seconds, self.on_buffer_close
        )

    async def handle_interactive_reply(self, phone: str, reply_id: str) -> None:
        """Respuesta a una lista o botones — `reply_id` es el id real (UUID
        de cliente, o valor de prioridad) que el bot puso al armar la
        opción, nunca texto libre."""
        state = await session.get_session(self._redis, phone)
        if state is None:
            return
        step = state.get("step")
        if step == STEP_AWAITING_CLIENT:
            client_id = None if reply_id == NO_CLIENT_OPTION_ID else reply_id
            await self._ask_priority(phone, state, client_id=client_id)
        elif step == STEP_AWAITING_PRIORITY:
            await self._finish(phone, state, priority=reply_id)

    # -- Cierre de buffer -------------------------------------------------

    async def on_buffer_close(self, phone: str, messages: list[BufferedMessage]) -> None:
        worker = await self._worker_lookup(phone)
        if worker is None:
            logger.warning(
                "Buffer cerrado para %s pero ya no está en la whitelist activa; se descarta.",
                phone,
            )
            return

        bodies = [m.body for m in messages]
        result = await self._llm.extract_title(bodies)
        state: dict[str, Any] = {
            "step": STEP_AWAITING_CLIENT,
            "title": result.title,
            "entities": result.entities.model_dump(mode="json") if result.entities else None,
            "messages": bodies,
            # Los wamids viajan en la sesión para poder vincular después las
            # filas de `raw_messages` con el ticket (decisión #20).
            "wamids": [m.wamid for m in messages if m.wamid],
            "worker_id": str(worker.id),
            "external_user_id": str(worker.external_user_id) if worker.external_user_id else None,
        }
        await session.start_session(
            self._redis, phone, state, self._settings.client_response_timeout_seconds
        )
        await session.spawn_timeout_watcher(
            self._redis,
            phone,
            self._settings.client_response_timeout_seconds,
            STEP_AWAITING_CLIENT,
            self._on_client_timeout,
        )
        await self._meta.send_text(phone, "¿A qué cliente corresponde? Escribe el nombre.")

    # -- Confirmación de cliente -------------------------------------------

    async def _handle_client_reply(self, phone: str, text: str) -> None:
        state = await session.get_session(self._redis, phone)
        if state is None or state.get("step") != STEP_AWAITING_CLIENT:
            return

        results = await self._ticket_system.search_clients(text)
        if not results:
            # Sin coincidencias no hay nada que escoger: una lista con solo
            # "Sin cliente" no le aporta nada al trabajador.
            await self._ask_priority(phone, state, client_id=None)
            return

        # Siempre se pregunta, incluso con una sola coincidencia: escogerla
        # automáticamente sería adivinar, y un cliente mal asignado en
        # silencio es peor que un ticket sin cliente (CLAUDE.md, decisión #5).
        options = [
            {"id": r.id, "title": r.name} for r in results[:MAX_CLIENT_MATCHES]
        ]
        options.append({"id": NO_CLIENT_OPTION_ID, "title": NO_CLIENT_OPTION_TITLE})

        if len(options) <= 3:
            await self._meta.send_buttons(phone, "¿Cuál de estos clientes?", options)
        else:
            await self._meta.send_list(phone, "¿Cuál de estos clientes?", options)
        # se mantiene en STEP_AWAITING_CLIENT — la respuesta llega por
        # handle_interactive_reply con el client_id real como `id`

    async def _on_client_timeout(self, phone: str, state: dict[str, Any]) -> None:
        await self._ask_priority(phone, state, client_id=None)

    # -- Prioridad ----------------------------------------------------------

    async def _ask_priority(self, phone: str, state: dict[str, Any], client_id: str | None) -> None:
        state = {**state, "step": STEP_AWAITING_PRIORITY, "client_id": client_id}
        await session.start_session(
            self._redis, phone, state, self._settings.priority_response_timeout_seconds
        )
        await session.spawn_timeout_watcher(
            self._redis,
            phone,
            self._settings.priority_response_timeout_seconds,
            STEP_AWAITING_PRIORITY,
            self._on_priority_timeout,
        )
        await self._meta.send_buttons(phone, ASK_PRIORITY_TEXT, PRIORITY_OPTIONS)

    async def _on_priority_timeout(self, phone: str, state: dict[str, Any]) -> None:
        await self._finish(phone, state, priority=DEFAULT_PRIORITY)

    # -- Creación del ticket -------------------------------------------------

    async def _finish(self, phone: str, state: dict[str, Any], priority: str) -> None:
        entities = self._entities_from(state)
        created_by = state.get("external_user_id")

        if not created_by:
            logger.error(
                "No se puede crear el ticket para %s: el worker no tiene "
                "external_user_id. Normalmente lo llena el poll de la whitelist; "
                "revisar que ese trabajador tenga whatsapp_phone en el sistema de "
                "tickets y que el sync esté corriendo.",
                phone,
            )
            # Aquí sí se cierra la sesión: reintentar no arreglaría nada
            # mientras no cambie el roster del otro lado.
            await session.clear_session(self._redis, phone)
            await self._log(
                state,
                entities,
                priority,
                CreationStatus.failed,
                error="worker sin external_user_id",
            )
            await self._meta.send_text(phone, NO_EXTERNAL_USER_TEXT)
            return

        description = build_description(state.get("messages", []), entities)
        try:
            # El departamento no se manda: lo deriva el sistema de tickets del
            # departamento de `created_by` (CLAUDE.md, decisión #8).
            ticket = await self._ticket_system.create_ticket(
                title=state["title"],
                description=description,
                priority=priority,
                created_by=created_by,
                client_id=state.get("client_id"),
            )
        except Exception as exc:
            logger.exception("Falló POST /internal/tickets para %s", phone)
            await self._log(state, entities, priority, CreationStatus.failed, error=str(exc))
            # La sesión NO se borra: si se borrara, el bloque de mensajes se
            # perdería y el trabajador se quedaría sin saber que su ticket no
            # existe (CLAUDE.md, decisión #21). Se refresca su TTL y se
            # vuelven a mandar los botones para que un toque reintente.
            await session.start_session(
                self._redis, phone, state, self._settings.priority_response_timeout_seconds
            )
            await self._meta.send_text(phone, CREATION_FAILED_TEXT)
            await self._meta.send_buttons(phone, ASK_PRIORITY_TEXT, PRIORITY_OPTIONS)
            return

        await session.clear_session(self._redis, phone)
        await self._log(
            state,
            entities,
            priority,
            CreationStatus.created,
            external_ticket_id=ticket.id,
            ticket_number=ticket.ticket_number,
        )
        await self._meta.send_text(phone, f"Ticket #{ticket.ticket_number} creado")

    @staticmethod
    def _entities_from(state: dict[str, Any]) -> ExtractedEntities | None:
        raw = state.get("entities")
        if not raw:
            return None
        try:
            return ExtractedEntities(**raw)
        except Exception:
            # Estado de sesión escrito por una versión anterior del bot, o
            # corrupto: el ticket se crea igual, sin encabezado de entidades.
            logger.warning("Entidades ilegibles en la sesión de un ticket; se ignoran")
            return None

    async def _log(
        self,
        state: dict[str, Any],
        entities: ExtractedEntities | None,
        priority: str,
        status: CreationStatus,
        external_ticket_id: str | None = None,
        ticket_number: int | None = None,
        error: str | None = None,
    ) -> None:
        """La bitácora nunca puede tumbar el flujo: cuando el ticket ya se
        creó, una falla al registrarlo no debe propagarse hacia arriba y
        provocar un segundo intento."""
        worker_id = state.get("worker_id")
        if not worker_id:
            logger.warning("Sesión sin worker_id; no se registra en ticket_creations")
            return
        try:
            await self._record_creation(
                TicketCreationLog(
                    worker_id=worker_id,
                    wamids=state.get("wamids", []),
                    title=state.get("title", ""),
                    entities=entities,
                    priority=priority,
                    client_id=state.get("client_id"),
                    status=status,
                    external_ticket_id=external_ticket_id,
                    ticket_number=ticket_number,
                    error=error,
                )
            )
        except Exception:
            logger.exception("No se pudo registrar el intento en ticket_creations")

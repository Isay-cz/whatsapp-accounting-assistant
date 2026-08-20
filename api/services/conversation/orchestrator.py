import logging
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis

from config import Settings
from models.orm import Worker
from models.schemas import Priority
from services.buffer import message_buffer, session
from services.llm.base import LLMExtractor
from services.meta import MetaClient
from services.ticket_system import TicketSystemClient

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

WorkerLookup = Callable[[str], Awaitable[Worker | None]]


class ConversationFlow:
    """Orquesta el flujo post-buffer (paso 3 en adelante del flujo end-to-end
    de CLAUDE.md): extracción de título, confirmación de cliente, prioridad
    y creación del ticket. Usa `services/buffer` para el estado de sesión
    (`bot:session:{phone}`) con el mismo mecanismo de TTL que el buffer de
    mensajes."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        llm: LLMExtractor,
        meta: MetaClient,
        ticket_system: TicketSystemClient,
        worker_lookup: WorkerLookup,
    ):
        self._redis = redis
        self._settings = settings
        self._llm = llm
        self._meta = meta
        self._ticket_system = ticket_system
        self._worker_lookup = worker_lookup

    @property
    def step_timeout_handlers(self) -> dict[str, session.OnTimeout]:
        return {
            STEP_AWAITING_CLIENT: self._on_client_timeout,
            STEP_AWAITING_PRIORITY: self._on_priority_timeout,
        }

    # -- Entrada desde el webhook --------------------------------------

    async def handle_incoming_message(self, phone: str, text: str) -> None:
        """Punto de entrada único para un mensaje de texto ya validado
        contra la whitelist. Decide si es un mensaje nuevo a bufferizar o
        la respuesta a la pregunta de cliente en curso."""
        state = await session.get_session(self._redis, phone)
        if state is not None and state.get("step") == STEP_AWAITING_CLIENT:
            await self._handle_client_reply(phone, text)
            return

        await message_buffer.push_message(
            self._redis, phone, text, self._settings.buffer_ttl_seconds
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

    async def on_buffer_close(self, phone: str, messages: list[str]) -> None:
        worker = await self._worker_lookup(phone)
        if worker is None:
            logger.warning(
                "Buffer cerrado para %s pero ya no está en la whitelist activa; se descarta.",
                phone,
            )
            return

        result = await self._llm.extract_title(messages)
        state: dict[str, Any] = {
            "step": STEP_AWAITING_CLIENT,
            "title": result.title,
            "messages": messages,
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
        await self._meta.send_buttons(phone, "¿Qué prioridad tiene?", PRIORITY_OPTIONS)

    async def _on_priority_timeout(self, phone: str, state: dict[str, Any]) -> None:
        await self._finish(phone, state, priority=DEFAULT_PRIORITY)

    # -- Creación del ticket -------------------------------------------------

    async def _finish(self, phone: str, state: dict[str, Any], priority: str) -> None:
        await session.clear_session(self._redis, phone)

        created_by = state.get("external_user_id")
        if not created_by:
            logger.error(
                "No se puede crear el ticket para %s: el worker no tiene "
                "external_user_id. Normalmente lo llena el poll de la whitelist; "
                "revisar que ese trabajador tenga whatsapp_phone en el sistema de "
                "tickets y que el sync esté corriendo.",
                phone,
            )
            await self._meta.send_text(
                phone,
                "No se pudo crear el ticket: tu usuario no está vinculado al sistema de "
                "tickets todavía. Avisa a un administrador.",
            )
            return

        description = "\n".join(state.get("messages", []))
        # El departamento no se manda: lo deriva el sistema de tickets del
        # departamento de `created_by` (CLAUDE.md, decisión #8).
        ticket = await self._ticket_system.create_ticket(
            title=state["title"],
            description=description,
            priority=priority,
            created_by=created_by,
            client_id=state.get("client_id"),
        )

        await self._meta.send_text(phone, f"Ticket #{ticket.ticket_number} creado")

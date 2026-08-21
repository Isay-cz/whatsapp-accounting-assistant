import asyncio
import uuid
from unittest.mock import ANY, AsyncMock

from models.orm import Worker
from models.schemas import (
    BufferedMessage,
    ClientSearchResult,
    CreatedTicket,
    CreationStatus,
    ExtractedEntities,
    Tramite,
)
from services.buffer import session as session_module
from services.conversation.description import SEPARATOR
from services.conversation.orchestrator import (
    ASK_PRIORITY_TEXT,
    CREATION_FAILED_TEXT,
    ConversationFlow,
)
from services.llm.base import ExtractionResult


class FakeSettings:
    buffer_ttl_seconds = 1
    client_response_timeout_seconds = 1
    priority_response_timeout_seconds = 1


def make_worker(
    external_user_id="11111111-1111-1111-1111-111111111111",
) -> Worker:
    return Worker(
        id=uuid.uuid4(),
        phone_number="16315551181",
        name="Trabajador",
        is_active=True,
        external_user_id=uuid.UUID(external_user_id) if external_user_id else None,
    )


def block(*bodies: str) -> list[BufferedMessage]:
    """Bloque de buffer con wamids sintéticos — lo que recibe on_buffer_close."""
    return [BufferedMessage(wamid=f"wamid.{i}", body=b) for i, b in enumerate(bodies)]


def make_flow(redis_client, worker, search_results=None):
    llm = AsyncMock()
    llm.extract_title.return_value = ExtractionResult(title="Cliente pide CFDI", source="llm")

    meta = AsyncMock()

    ticket_system = AsyncMock()
    ticket_system.search_clients.return_value = search_results or []
    ticket_system.create_ticket.return_value = CreatedTicket(id="ticket-123", ticket_number=482)

    async def worker_lookup(phone):
        return worker

    recorder = AsyncMock()

    flow = ConversationFlow(
        redis=redis_client,
        settings=FakeSettings(),
        llm=llm,
        meta=meta,
        ticket_system=ticket_system,
        worker_lookup=worker_lookup,
        record_creation=recorder,
    )
    return flow, llm, meta, ticket_system, recorder


async def test_full_happy_path_single_client_match(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="cliente-1", name="Juan Pérez")]
    )

    await flow.on_buffer_close("16315551181", block("necesito mi cfdi de enero"))
    llm.extract_title.assert_awaited_once_with(["necesito mi cfdi de enero"])
    meta.send_text.assert_awaited_with(
        "16315551181", "¿A qué cliente corresponde? Escribe el nombre."
    )

    await flow.handle_incoming_message("16315551181", "Juan Perez")
    ticket_system.search_clients.assert_awaited_once_with("Juan Perez")
    # Una sola coincidencia igual se pregunta: escogerla sola sería adivinar.
    meta.send_buttons.assert_awaited_with(
        "16315551181",
        "¿Cuál de estos clientes?",
        [
            {"id": "cliente-1", "title": "Juan Pérez"},
            {"id": "__sin_cliente__", "title": "Sin cliente"},
        ],
    )

    await flow.handle_interactive_reply("16315551181", "cliente-1")
    await flow.handle_interactive_reply("16315551181", "alta")
    ticket_system.create_ticket.assert_awaited_once_with(
        title="Cliente pide CFDI",
        description="necesito mi cfdi de enero",
        priority="alta",
        created_by="11111111-1111-1111-1111-111111111111",
        client_id="cliente-1",
    )
    meta.send_text.assert_awaited_with("16315551181", "Ticket #482 creado")


async def test_sin_cliente_option_creates_ticket_without_client(redis_client):
    """El trabajador puede decir "trabajo interno" sin esperar al timeout."""
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="c1", name="Cliente Uno")]
    )

    await flow.on_buffer_close("16315551195", block("mensaje interno"))
    await flow.handle_incoming_message("16315551195", "Cliente")
    await flow.handle_interactive_reply("16315551195", "__sin_cliente__")
    await flow.handle_interactive_reply("16315551195", "baja")

    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["client_id"] is None


async def test_client_matches_are_capped_at_nine_plus_sin_cliente(redis_client):
    """9 + "Sin cliente" = los 10 que permite la lista interactiva de WhatsApp."""
    worker = make_worker()
    results = [ClientSearchResult(id=f"c{i}", name=f"Cliente {i}") for i in range(9)]
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker, search_results=results)

    await flow.on_buffer_close("16315551196", block("mensaje"))
    await flow.handle_incoming_message("16315551196", "Cliente")

    _, args, _ = meta.send_list.mock_calls[0]
    options = args[2]
    assert len(options) == 10
    assert options[-1]["id"] == "__sin_cliente__"


async def test_zero_client_matches_proceeds_with_null_client(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker, search_results=[])

    await flow.on_buffer_close("16315551190", block("mensaje sin cliente claro"))
    await flow.handle_incoming_message("16315551190", "Cliente Fantasma SA")
    meta.send_buttons.assert_awaited_with("16315551190", "¿Qué prioridad tiene?", ANY)

    await flow.handle_interactive_reply("16315551190", "media")
    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["client_id"] is None


async def test_multiple_client_matches_sends_disambiguation_and_keeps_waiting(redis_client):
    worker = make_worker()
    results = [ClientSearchResult(id=f"c{i}", name=f"Cliente {i}") for i in range(4)]
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker, search_results=results)

    await flow.on_buffer_close("16315551191", block("mensaje ambiguo"))
    await flow.handle_incoming_message("16315551191", "Cliente")

    meta.send_list.assert_awaited_once()
    state = await session_module.get_session(redis_client, "16315551191")
    assert state["step"] == "awaiting_client"

    await flow.handle_interactive_reply("16315551191", "c2")
    state = await session_module.get_session(redis_client, "16315551191")
    assert state["step"] == "awaiting_priority"
    assert state["client_id"] == "c2"


async def test_priority_timeout_defaults_to_media(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="c1", name="Cliente Uno")]
    )

    await flow.on_buffer_close("16315551192", block("mensaje"))
    await flow.handle_incoming_message("16315551192", "Cliente Uno")  # -> lista de clientes
    await flow.handle_interactive_reply("16315551192", "c1")  # -> pregunta prioridad, timeout 1s

    await asyncio.sleep(1.5)

    ticket_system.create_ticket.assert_awaited_once()
    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["priority"] == "media"


async def test_client_timeout_proceeds_with_null_client(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)

    await flow.on_buffer_close("16315551193", block("mensaje"))
    await asyncio.sleep(1.3)  # timeout de la pregunta de cliente

    state = await session_module.get_session(redis_client, "16315551193")
    assert state["step"] == "awaiting_priority"
    assert state["client_id"] is None


async def test_missing_external_user_id_blocks_ticket_creation(redis_client):
    worker = make_worker(external_user_id=None)
    flow, llm, meta, ticket_system, recorder = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="c1", name="Cliente Uno")]
    )

    await flow.on_buffer_close("16315551194", block("mensaje"))
    await flow.handle_incoming_message("16315551194", "Cliente Uno")
    await flow.handle_interactive_reply("16315551194", "c1")
    await flow.handle_interactive_reply("16315551194", "alta")

    ticket_system.create_ticket.assert_not_awaited()
    meta.send_text.assert_awaited_with(
        "16315551194",
        "No se pudo crear el ticket: tu usuario no está vinculado al sistema de "
        "tickets todavía. Avisa a un administrador.",
    )


async def test_buffer_close_discards_when_worker_no_longer_active(redis_client):
    llm = AsyncMock()
    meta = AsyncMock()
    ticket_system = AsyncMock()

    async def worker_lookup(phone):
        return None

    flow = ConversationFlow(
        redis=redis_client,
        settings=FakeSettings(),
        llm=llm,
        meta=meta,
        ticket_system=ticket_system,
        worker_lookup=worker_lookup,
        record_creation=AsyncMock(),
    )

    await flow.on_buffer_close("16315551195", block("mensaje"))
    llm.extract_title.assert_not_awaited()
    meta.send_text.assert_not_awaited()


def test_client_has_no_add_department_method():
    """El bot ya no asocia departamentos: los deriva el sistema de tickets a
    partir de created_by (CLAUDE.md, decisión #8)."""
    from services.ticket_system import TicketSystemClient

    assert not hasattr(TicketSystemClient, "add_department")


# -- Bitácora y manejo de fallos (decisiones #20 y #21) --------------------


async def test_creacion_exitosa_queda_registrada_en_la_bitacora(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)

    await flow.on_buffer_close("16315551200", block("primero", "segundo"))
    await flow.handle_incoming_message("16315551200", "Cliente Fantasma")  # 0 coincidencias
    await flow.handle_interactive_reply("16315551200", "alta")

    recorder.assert_awaited_once()
    registro = recorder.call_args.args[0]
    assert registro.status == CreationStatus.created
    assert registro.ticket_number == 482
    assert registro.external_ticket_id == "ticket-123"
    assert registro.worker_id == worker.id
    assert registro.wamids == ["wamid.0", "wamid.1"]  # el bloque completo
    assert registro.priority == "alta"


async def test_fallo_de_creacion_avisa_al_trabajador_y_conserva_la_sesion(redis_client):
    """Decisión #21: la sesión no se borra hasta que el ticket existe. Si se
    borrara, el bloque se perdería y el trabajador no sabría que su ticket
    nunca se creó."""
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)
    ticket_system.create_ticket.side_effect = RuntimeError("503 del sistema de tickets")

    await flow.on_buffer_close("16315551201", block("mensaje que no se debe perder"))
    await flow.handle_incoming_message("16315551201", "Cliente Fantasma")
    await flow.handle_interactive_reply("16315551201", "alta")

    # Se avisó, y se volvieron a mandar los botones para reintentar de un toque.
    meta.send_text.assert_awaited_with("16315551201", CREATION_FAILED_TEXT)
    meta.send_buttons.assert_awaited_with("16315551201", ASK_PRIORITY_TEXT, ANY)

    # La sesión sigue viva y con el bloque intacto.
    state = await session_module.get_session(redis_client, "16315551201")
    assert state is not None
    assert state["step"] == "awaiting_priority"
    assert state["messages"] == ["mensaje que no se debe perder"]

    # Y el intento fallido quedó registrado, que es el caso que si no no deja rastro.
    registro = recorder.call_args.args[0]
    assert registro.status == CreationStatus.failed
    assert "503" in registro.error
    assert registro.ticket_number is None


async def test_reintento_despues_de_un_fallo_crea_el_ticket(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)
    ticket_system.create_ticket.side_effect = [
        RuntimeError("503 del sistema de tickets"),
        CreatedTicket(id="ticket-999", ticket_number=500),
    ]

    await flow.on_buffer_close("16315551202", block("mensaje"))
    await flow.handle_incoming_message("16315551202", "Cliente Fantasma")
    await flow.handle_interactive_reply("16315551202", "alta")  # falla
    await flow.handle_interactive_reply("16315551202", "alta")  # el trabajador reintenta

    meta.send_text.assert_awaited_with("16315551202", "Ticket #500 creado")
    assert await session_module.get_session(redis_client, "16315551202") is None
    assert [c.args[0].status for c in recorder.call_args_list] == [
        CreationStatus.failed,
        CreationStatus.created,
    ]


async def test_worker_sin_external_user_id_tambien_se_registra(redis_client):
    worker = make_worker(external_user_id=None)
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)

    await flow.on_buffer_close("16315551203", block("mensaje"))
    await flow.handle_incoming_message("16315551203", "Cliente Fantasma")
    await flow.handle_interactive_reply("16315551203", "media")

    registro = recorder.call_args.args[0]
    assert registro.status == CreationStatus.failed
    assert registro.error == "worker sin external_user_id"
    # Aquí sí se cierra la sesión: reintentar no arregla nada hasta que
    # cambie el roster del otro lado.
    assert await session_module.get_session(redis_client, "16315551203") is None


async def test_un_fallo_de_la_bitacora_no_tumba_la_creacion(redis_client):
    """El ticket ya existe: perder la bitácora es malo, pero propagar el
    error haría que el trabajador creyera que falló (y reintentara, creando
    un segundo ticket)."""
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)
    recorder.side_effect = RuntimeError("la base de la bitácora está caída")

    await flow.on_buffer_close("16315551204", block("mensaje"))
    await flow.handle_incoming_message("16315551204", "Cliente Fantasma")
    await flow.handle_interactive_reply("16315551204", "media")

    meta.send_text.assert_awaited_with("16315551204", "Ticket #482 creado")


async def test_las_entidades_se_anteponen_a_la_descripcion(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system, recorder = make_flow(redis_client, worker)
    llm.extract_title.return_value = ExtractionResult(
        title="Cliente pide factura",
        source="llm",
        entities=ExtractedEntities(monto="$12,500", tramite=Tramite.factura),
    )

    await flow.on_buffer_close("16315551205", block("necesita su factura por $12,500"))
    await flow.handle_incoming_message("16315551205", "Cliente Fantasma")
    await flow.handle_interactive_reply("16315551205", "media")

    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["description"] == (
        "Datos detectados:\n"
        "• Monto: $12,500\n"
        "• Trámite: Factura\n"
        f"{SEPARATOR}\n"
        "necesita su factura por $12,500"
    )
    # Y la bitácora guarda las entidades, que es lo único de la descripción
    # que no se puede reconstruir desde `raw_messages`.
    assert recorder.call_args.args[0].entities.monto == "$12,500"

import asyncio
import uuid
from unittest.mock import ANY, AsyncMock

from models.orm import Worker
from models.schemas import ClientSearchResult, CreatedTicket
from services.buffer import session as session_module
from services.conversation.orchestrator import ConversationFlow
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


def make_flow(redis_client, worker, search_results=None):
    llm = AsyncMock()
    llm.extract_title.return_value = ExtractionResult(title="Cliente pide CFDI", source="llm")

    meta = AsyncMock()

    ticket_system = AsyncMock()
    ticket_system.search_clients.return_value = search_results or []
    ticket_system.create_ticket.return_value = CreatedTicket(id="ticket-123", ticket_number=482)

    async def worker_lookup(phone):
        return worker

    flow = ConversationFlow(
        redis=redis_client,
        settings=FakeSettings(),
        llm=llm,
        meta=meta,
        ticket_system=ticket_system,
        worker_lookup=worker_lookup,
    )
    return flow, llm, meta, ticket_system


async def test_full_happy_path_single_client_match(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="cliente-1", name="Juan Pérez")]
    )

    await flow.on_buffer_close("16315551181", ["necesito mi cfdi de enero"])
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
    flow, llm, meta, ticket_system = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="c1", name="Cliente Uno")]
    )

    await flow.on_buffer_close("16315551195", ["mensaje interno"])
    await flow.handle_incoming_message("16315551195", "Cliente")
    await flow.handle_interactive_reply("16315551195", "__sin_cliente__")
    await flow.handle_interactive_reply("16315551195", "baja")

    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["client_id"] is None


async def test_client_matches_are_capped_at_nine_plus_sin_cliente(redis_client):
    """9 + "Sin cliente" = los 10 que permite la lista interactiva de WhatsApp."""
    worker = make_worker()
    results = [ClientSearchResult(id=f"c{i}", name=f"Cliente {i}") for i in range(9)]
    flow, llm, meta, ticket_system = make_flow(redis_client, worker, search_results=results)

    await flow.on_buffer_close("16315551196", ["mensaje"])
    await flow.handle_incoming_message("16315551196", "Cliente")

    _, args, _ = meta.send_list.mock_calls[0]
    options = args[2]
    assert len(options) == 10
    assert options[-1]["id"] == "__sin_cliente__"


async def test_zero_client_matches_proceeds_with_null_client(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system = make_flow(redis_client, worker, search_results=[])

    await flow.on_buffer_close("16315551190", ["mensaje sin cliente claro"])
    await flow.handle_incoming_message("16315551190", "Cliente Fantasma SA")
    meta.send_buttons.assert_awaited_with("16315551190", "¿Qué prioridad tiene?", ANY)

    await flow.handle_interactive_reply("16315551190", "media")
    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["client_id"] is None


async def test_multiple_client_matches_sends_disambiguation_and_keeps_waiting(redis_client):
    worker = make_worker()
    results = [ClientSearchResult(id=f"c{i}", name=f"Cliente {i}") for i in range(4)]
    flow, llm, meta, ticket_system = make_flow(redis_client, worker, search_results=results)

    await flow.on_buffer_close("16315551191", ["mensaje ambiguo"])
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
    flow, llm, meta, ticket_system = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="c1", name="Cliente Uno")]
    )

    await flow.on_buffer_close("16315551192", ["mensaje"])
    await flow.handle_incoming_message("16315551192", "Cliente Uno")  # -> lista de clientes
    await flow.handle_interactive_reply("16315551192", "c1")  # -> pregunta prioridad, timeout 1s

    await asyncio.sleep(1.5)

    ticket_system.create_ticket.assert_awaited_once()
    _, kwargs = ticket_system.create_ticket.call_args
    assert kwargs["priority"] == "media"


async def test_client_timeout_proceeds_with_null_client(redis_client):
    worker = make_worker()
    flow, llm, meta, ticket_system = make_flow(redis_client, worker)

    await flow.on_buffer_close("16315551193", ["mensaje"])
    await asyncio.sleep(1.3)  # timeout de la pregunta de cliente

    state = await session_module.get_session(redis_client, "16315551193")
    assert state["step"] == "awaiting_priority"
    assert state["client_id"] is None


async def test_missing_external_user_id_blocks_ticket_creation(redis_client):
    worker = make_worker(external_user_id=None)
    flow, llm, meta, ticket_system = make_flow(
        redis_client, worker, search_results=[ClientSearchResult(id="c1", name="Cliente Uno")]
    )

    await flow.on_buffer_close("16315551194", ["mensaje"])
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
    )

    await flow.on_buffer_close("16315551195", ["mensaje"])
    llm.extract_title.assert_not_awaited()
    meta.send_text.assert_not_awaited()


def test_client_has_no_add_department_method():
    """El bot ya no asocia departamentos: los deriva el sistema de tickets a
    partir de created_by (CLAUDE.md, decisión #8)."""
    from services.ticket_system import TicketSystemClient

    assert not hasattr(TicketSystemClient, "add_department")

"""
Ciclo de vida completo de un ticket, de punta a punta *dentro* de este repo:
webhook firmado -> whitelist -> raw_messages -> buffer/debounce en Redis ->
extracción LLM -> búsqueda de cliente -> prioridad -> POST /internal/tickets
-> confirmación al trabajador.

A diferencia de test_conversation_flow.py (que mockea el `ConversationFlow`
completo con AsyncMock), aquí el flujo, el buffer, el extractor y los
clientes HTTP son los reales: lo único falso son las respuestas *de la red*
— Meta, DeepSeek y el sistema de tickets se interceptan a nivel transporte
con `httpx_mock`. Así se ejercita el pipeline entero sin estar conectado a
Meta todavía, y se verifican las formas reales de los requests salientes.

Los timeouts se reducen a 1-2 segundos vía `Settings.model_copy` para que el
debounce sea observable sin alargar la suite.
"""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest_asyncio
from sqlalchemy import select

from config import get_settings
from main import app
from models.orm import RawMessage, TicketCreation
from models.schemas import ExtractedEntities, Tramite
from services.buffer import _tasks
from services.buffer import session as session_module
from services.conversation import ConversationFlow
from services.conversation.deps import active_worker_by_phone_stmt, record_ticket_creation_in
from services.conversation.description import HEADER, SEPARATOR
from services.llm import get_extractor
from services.llm.fallback import fallback_title
from services.meta import get_meta_client
from services.ticket_system import get_ticket_system_client

from .conftest import (
    WORKER_PHONE,
    interactive_reply_payload,
    post_webhook,
    text_message_payload,
    wait_for,
)

DEEPSEEK_URL = "https://api.deepseek.test/chat/completions"
META_URL = "https://graph.facebook.com/v21.0/123456123/messages"
TICKET_SYSTEM_URL = "http://ticket-system.test"
CREATE_TICKET_URL = f"{TICKET_SYSTEM_URL}/internal/tickets"

EXTERNAL_USER_ID = "11111111-1111-1111-1111-111111111111"  # el del fixture `worker`
ASK_CLIENT_TEXT = "¿A qué cliente corresponde? Escribe el nombre."
PICK_CLIENT_TEXT = "¿Cuál de estos clientes?"
ASK_PRIORITY_TEXT = "¿Qué prioridad tiene?"


def search_url(query: str) -> httpx.URL:
    return httpx.URL(f"{TICKET_SYSTEM_URL}/internal/clients/search", params={"q": query})


# -- Ensamblado del flujo real ---------------------------------------------


@pytest_asyncio.fixture
async def build_flow(db_session, redis_client, worker):
    """Arma el `ConversationFlow` real (buffer y sesiones sobre el Redis de
    prueba, extractor y clientes HTTP reales) y lo deja en `app.state`, que
    es de donde lo toma el webhook.

    `worker_lookup` se ata a la sesión del test en vez de usar
    `lookup_active_worker_by_phone`: ese abre su propia sesión y no vería al
    trabajador, que vive dentro de la transacción que el test revierte al
    final."""

    def build(*, buffer_ttl: int = 1, client_timeout: int = 1, priority_timeout: int = 1):
        settings = get_settings().model_copy(
            update={
                "buffer_ttl_seconds": buffer_ttl,
                "client_response_timeout_seconds": client_timeout,
                "priority_response_timeout_seconds": priority_timeout,
                "deepseek_api_key": "test-key",
                "deepseek_base_url": "https://api.deepseek.test",
                "meta_access_token": "test-token",
                "meta_phone_number_id": "123456123",
            }
        )

        async def worker_lookup(phone: str):
            result = await db_session.execute(active_worker_by_phone_stmt(phone))
            return result.scalar_one_or_none()

        async def record_creation(record):
            # El recorder real, pero sobre la sesión del test: así la fila de
            # `ticket_creations` es visible aquí y se revierte al terminar.
            await record_ticket_creation_in(db_session, record)

        flow = ConversationFlow(
            redis=redis_client,
            settings=settings,
            llm=get_extractor(settings),
            meta=get_meta_client(settings),
            ticket_system=get_ticket_system_client(settings),
            worker_lookup=worker_lookup,
            record_creation=record_creation,
        )
        app.state.conversation_flow = flow
        app.state.alert_notifier = AsyncMock()
        return flow

    yield build

    # Los watchers de buffer y de timeout viven en tasks sueltas: si alguna
    # sigue dormida cuando el test cierra su sesión, despierta sobre una
    # conexión muerta y cuelga la corrida.
    _tasks.cancel_all()
    await asyncio.sleep(0)  # deja que las cancelaciones corran su `finally`


# -- Stubs de red -----------------------------------------------------------


def stub_llm(
    httpx_mock, title: str = "Cliente pide CFDI de enero", entities: dict | None = None
) -> None:
    payload = {"title": title, "entities": entities or {}}
    httpx_mock.add_response(
        url=DEEPSEEK_URL,
        json={"choices": [{"message": {"content": json.dumps(payload)}}]},
    )


def stub_llm_failure(httpx_mock) -> None:
    httpx_mock.add_response(url=DEEPSEEK_URL, status_code=500)


def stub_meta(httpx_mock) -> None:
    """Un solo stub cubre todos los envíos salientes: texto, lista y botones
    van al mismo endpoint de Graph API."""
    httpx_mock.add_response(url=META_URL, json={"messages": [{"id": "wamid.saliente"}]})


def stub_client_search(httpx_mock, query: str, matches: list[dict]) -> None:
    httpx_mock.add_response(url=search_url(query), json={"matches": matches})


def stub_create_ticket(httpx_mock, ticket_number: int = 482) -> str:
    ticket_id = str(uuid.uuid4())
    httpx_mock.add_response(
        url=CREATE_TICKET_URL,
        json={"id": ticket_id, "ticket_number": ticket_number},
    )
    return ticket_id


# -- Lectura de lo que salió ------------------------------------------------


def _bodies(httpx_mock, url: str) -> list[dict]:
    return [
        json.loads(request.content)
        for request in httpx_mock.get_requests()
        if str(request.url) == url
    ]


def meta_messages(httpx_mock) -> list[dict]:
    return _bodies(httpx_mock, META_URL)


def texts_sent(httpx_mock) -> list[str]:
    return [m["text"]["body"] for m in meta_messages(httpx_mock) if m["type"] == "text"]


def interactive_sent(httpx_mock) -> list[dict]:
    return [m["interactive"] for m in meta_messages(httpx_mock) if m["type"] == "interactive"]


def interactive_with_body(httpx_mock, body_text: str) -> list[dict]:
    """Localiza el mensaje interactivo por su texto: la confirmación de
    cliente y la de prioridad pueden llegar ambas como botones."""
    return [i for i in interactive_sent(httpx_mock) if i["body"]["text"] == body_text]


def option_ids(interactive: dict) -> list[str]:
    """Ids de las opciones, sea el mensaje de botones o de lista."""
    if interactive["type"] == "button":
        return [b["reply"]["id"] for b in interactive["action"]["buttons"]]
    return [row["id"] for row in interactive["action"]["sections"][0]["rows"]]


def llm_prompts(httpx_mock) -> list[str]:
    return [b["messages"][-1]["content"] for b in _bodies(httpx_mock, DEEPSEEK_URL)]


def created_tickets(httpx_mock) -> list[dict]:
    return _bodies(httpx_mock, CREATE_TICKET_URL)


async def wait_for_text(httpx_mock, text: str) -> None:
    await wait_for(
        lambda: text in texts_sent(httpx_mock),
        message=f"El bot nunca mandó {text!r}; mandó {texts_sent(httpx_mock)}",
    )


async def wait_for_interactive(httpx_mock, body_text: str) -> dict:
    await wait_for(
        lambda: bool(interactive_with_body(httpx_mock, body_text)),
        message=f"El bot nunca mandó el mensaje interactivo {body_text!r}",
    )
    return interactive_with_body(httpx_mock, body_text)[0]


async def wait_for_ticket(httpx_mock) -> None:
    await wait_for(
        lambda: bool(created_tickets(httpx_mock)),
        message="Nunca se llamó a POST /internal/tickets",
    )


# -- Mensaje único ----------------------------------------------------------


async def test_mensaje_unico_recorre_el_ciclo_completo(
    client, build_flow, worker, db_session, httpx_mock, redis_client
):
    """Camino feliz completo: un mensaje reenviado termina en un ticket con
    cliente y prioridad escogidos por el trabajador."""
    build_flow()
    stub_llm(httpx_mock, title="Cliente pide CFDI de enero")
    stub_meta(httpx_mock)
    client_id = str(uuid.uuid4())
    stub_client_search(httpx_mock, "Juan Perez", [{"id": client_id, "name": "Juan Pérez López"}])
    stub_create_ticket(httpx_mock, ticket_number=482)

    # 1. Llega el mensaje reenviado.
    response = await post_webhook(
        client,
        text_message_payload("necesito mi cfdi de enero", wamid="wamid.ciclo.1"),
    )
    assert response.status_code == 200

    raw = (
        await db_session.execute(select(RawMessage).where(RawMessage.wamid == "wamid.ciclo.1"))
    ).scalar_one()
    assert raw.worker_id == worker.id
    assert raw.body == "necesito mi cfdi de enero"

    # 2-3. Cierra el buffer, se extrae el título y se pregunta por el cliente.
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)
    assert llm_prompts(httpx_mock) == ["necesito mi cfdi de enero"]

    # 4. El trabajador escribe el nombre del cliente -> búsqueda -> confirmación.
    await post_webhook(client, text_message_payload("Juan Perez", wamid="wamid.ciclo.2"))
    pregunta_cliente = await wait_for_interactive(httpx_mock, PICK_CLIENT_TEXT)
    # Una sola coincidencia igual se pregunta (decisión #5), y siempre
    # acompañada de "Sin cliente". Con 2 opciones caben en botones.
    assert pregunta_cliente["type"] == "button"
    assert pregunta_cliente["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": client_id, "title": "Juan Pérez López"}},
        {"type": "reply", "reply": {"id": "__sin_cliente__", "title": "Sin cliente"}},
    ]

    # 5. Escoge el cliente -> se pregunta prioridad por botones.
    await post_webhook(
        client,
        interactive_reply_payload(
            client_id, wamid="wamid.ciclo.3", reply_type="button_reply", title="Juan Pérez López"
        ),
    )
    pregunta_prioridad = await wait_for_interactive(httpx_mock, ASK_PRIORITY_TEXT)
    assert option_ids(pregunta_prioridad) == ["alta", "media", "baja"]

    # 6. Escoge prioridad -> se crea el ticket.
    await post_webhook(
        client,
        interactive_reply_payload(
            "alta", wamid="wamid.ciclo.4", reply_type="button_reply", title="Alta"
        ),
    )
    await wait_for_ticket(httpx_mock)

    body = created_tickets(httpx_mock)[0]
    assert body == {
        "title": "Cliente pide CFDI de enero",
        "description": "necesito mi cfdi de enero",
        "priority": "alta",
        "created_by": EXTERNAL_USER_ID,
        "client_id": client_id,
    }
    # El departamento lo deriva el sistema de tickets a partir de created_by
    # (decisión #8) — el bot no lo manda nunca.
    assert "department_id" not in body

    # El token interno es solo saliente (decisión #1).
    create_request = [
        r for r in httpx_mock.get_requests() if str(r.url) == CREATE_TICKET_URL
    ][0]
    assert create_request.headers["Authorization"] == "Bearer test-internal-token"

    # 7. Confirmación con ticket_number, no con el UUID (decisión #16).
    await wait_for_text(httpx_mock, "Ticket #482 creado")
    assert await session_module.get_session(redis_client, WORKER_PHONE) is None

    # 8. Y quedó en la bitácora, con el bloque de mensajes vinculado
    # (decisión #20).
    creacion = (
        await db_session.execute(select(TicketCreation))
    ).scalar_one()
    assert creacion.ticket_number == 482
    assert creacion.status == "created"
    assert creacion.worker_id == worker.id
    assert str(creacion.client_id) == client_id
    vinculados = (
        (
            await db_session.execute(
                select(RawMessage).where(RawMessage.ticket_creation_id == creacion.id)
            )
        )
        .scalars()
        .all()
    )
    assert {f.wamid for f in vinculados} == {"wamid.ciclo.1"}


async def test_falla_del_llm_no_bloquea_la_creacion_del_ticket(
    client, build_flow, worker, httpx_mock
):
    """Decisión #7: si DeepSeek falla, el título cae al fallback determinista
    y el ticket se crea igual."""
    build_flow()
    stub_llm_failure(httpx_mock)
    stub_meta(httpx_mock)
    stub_client_search(httpx_mock, "Interno", [])
    stub_create_ticket(httpx_mock, ticket_number=77)

    mensaje = "el cliente pregunta por la declaración anual y adjunta comprobantes de enero a marzo"
    await post_webhook(client, text_message_payload(mensaje, wamid="wamid.fallback.1"))
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)

    await post_webhook(client, text_message_payload("Interno", wamid="wamid.fallback.2"))
    await post_webhook(
        client,
        interactive_reply_payload(
            "media", wamid="wamid.fallback.3", reply_type="button_reply", title="Media"
        ),
    )
    await wait_for_ticket(httpx_mock)

    body = created_tickets(httpx_mock)[0]
    assert body["title"] == fallback_title(mensaje)
    assert body["description"] == mensaje  # la descripción nunca depende del LLM
    assert body["client_id"] is None  # cero coincidencias -> se pasa directo a prioridad


# -- Varios mensajes en la misma ventana (refresh del buffer) ---------------


async def test_varios_mensajes_se_agrupan_en_un_solo_ticket(
    client, build_flow, worker, httpx_mock
):
    """Cada mensaje nuevo refresca la ventana de debounce: los tres terminan
    en una sola llamada al LLM y en un solo ticket."""
    build_flow(buffer_ttl=2)
    stub_llm(httpx_mock, title="Cliente manda documentos de nómina")
    stub_meta(httpx_mock)
    client_id = str(uuid.uuid4())
    stub_client_search(httpx_mock, "Constructora", [{"id": client_id, "name": "Constructora ABC"}])
    stub_create_ticket(httpx_mock, ticket_number=901)

    mensajes = [
        "buenas, te reenvío lo del cliente",
        "manda los recibos de nómina de marzo",
        "dice que urge para el viernes",
    ]
    for i, texto in enumerate(mensajes):
        await post_webhook(client, text_message_payload(texto, wamid=f"wamid.bloque.{i}"))
        await asyncio.sleep(0.4)  # dentro de la ventana: cada uno la reinicia

    # A 1.2s del primer mensaje, con TTL de 2s, el bloque sigue abierto
    # justamente porque los mensajes 2 y 3 lo refrescaron.
    assert llm_prompts(httpx_mock) == []

    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)
    assert llm_prompts(httpx_mock) == ["\n".join(mensajes)]  # una sola llamada, con todo el bloque
    assert texts_sent(httpx_mock).count(ASK_CLIENT_TEXT) == 1  # una sola pregunta, no tres

    await post_webhook(client, text_message_payload("Constructora", wamid="wamid.bloque.cliente"))
    await post_webhook(
        client,
        interactive_reply_payload(client_id, wamid="wamid.bloque.pick", title="Constructora ABC"),
    )
    await post_webhook(
        client,
        interactive_reply_payload(
            "alta", wamid="wamid.bloque.prio", reply_type="button_reply", title="Alta"
        ),
    )
    await wait_for_ticket(httpx_mock)

    assert len(created_tickets(httpx_mock)) == 1
    body = created_tickets(httpx_mock)[0]
    assert body["description"] == "\n".join(mensajes)
    assert body["title"] == "Cliente manda documentos de nómina"
    assert body["client_id"] == client_id


async def test_reintento_de_meta_no_duplica_el_mensaje_del_bloque(
    client, build_flow, worker, db_session, httpx_mock
):
    """Decisión #10: Meta reintenta entregas hasta 7 días. El mismo `wamid`
    entregado dos veces no puede aparecer dos veces en la descripción."""
    build_flow(buffer_ttl=2)
    stub_llm(httpx_mock)
    stub_meta(httpx_mock)
    stub_client_search(httpx_mock, "Interno", [])
    stub_create_ticket(httpx_mock, ticket_number=12)

    payload = text_message_payload("mensaje que Meta reintenta", wamid="wamid.dup.1")
    assert (await post_webhook(client, payload)).status_code == 200
    assert (await post_webhook(client, payload)).status_code == 200  # reintento
    await post_webhook(client, text_message_payload("segundo mensaje real", wamid="wamid.dup.2"))

    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)
    assert llm_prompts(httpx_mock) == ["mensaje que Meta reintenta\nsegundo mensaje real"]

    filas = (
        await db_session.execute(select(RawMessage).where(RawMessage.wamid == "wamid.dup.1"))
    ).scalars().all()
    assert len(filas) == 1

    await post_webhook(client, text_message_payload("Interno", wamid="wamid.dup.3"))
    await post_webhook(
        client,
        interactive_reply_payload(
            "baja", wamid="wamid.dup.4", reply_type="button_reply", title="Baja"
        ),
    )
    await wait_for_ticket(httpx_mock)
    assert created_tickets(httpx_mock)[0]["description"] == (
        "mensaje que Meta reintenta\nsegundo mensaje real"
    )


# -- Variantes de la confirmación de cliente --------------------------------


async def test_sin_cliente_en_una_lista_llena_crea_ticket_sin_cliente(
    client, build_flow, worker, httpx_mock
):
    """Con muchas coincidencias la lista se corta en 9 + "Sin cliente" (el
    tope de 10 de WhatsApp), y escoger esa opción crea el ticket sin cliente
    sin tener que esperar al timeout."""
    build_flow()
    stub_llm(httpx_mock)
    stub_meta(httpx_mock)
    stub_client_search(
        httpx_mock,
        "Servicios",
        [{"id": str(uuid.uuid4()), "name": f"Servicios {i}"} for i in range(12)],
    )
    stub_create_ticket(httpx_mock, ticket_number=333)

    await post_webhook(client, text_message_payload("trabajo interno", wamid="wamid.sin.1"))
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)

    await post_webhook(client, text_message_payload("Servicios", wamid="wamid.sin.2"))
    lista = await wait_for_interactive(httpx_mock, PICK_CLIENT_TEXT)
    assert lista["type"] == "list"  # más de 3 opciones ya no caben en botones
    rows = lista["action"]["sections"][0]["rows"]
    assert len(rows) == 10  # 9 coincidencias + "Sin cliente", el tope de WhatsApp
    assert rows[-1] == {"id": "__sin_cliente__", "title": "Sin cliente"}

    await post_webhook(
        client,
        interactive_reply_payload("__sin_cliente__", wamid="wamid.sin.3", title="Sin cliente"),
    )
    await post_webhook(
        client,
        interactive_reply_payload(
            "media", wamid="wamid.sin.4", reply_type="button_reply", title="Media"
        ),
    )
    await wait_for_ticket(httpx_mock)
    assert created_tickets(httpx_mock)[0]["client_id"] is None


async def test_sin_respuesta_del_trabajador_los_timeouts_crean_el_ticket(
    client, build_flow, worker, httpx_mock, redis_client
):
    """Decisiones #5 y #9: si el trabajador nunca contesta, el ticket se crea
    igual — cliente nulo y prioridad "media" por default."""
    build_flow(client_timeout=1, priority_timeout=1)
    stub_llm(httpx_mock, title="Cliente pide constancia de situación fiscal")
    stub_meta(httpx_mock)
    stub_create_ticket(httpx_mock, ticket_number=555)

    await post_webhook(
        client, text_message_payload("necesita su constancia fiscal", wamid="wamid.timeout.1")
    )
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)

    # No se contesta nada: el timeout de cliente pasa a prioridad...
    await wait_for_interactive(httpx_mock, ASK_PRIORITY_TEXT)
    # ...y el de prioridad crea el ticket.
    await wait_for_ticket(httpx_mock)

    body = created_tickets(httpx_mock)[0]
    assert body["client_id"] is None
    assert body["priority"] == "media"
    assert body["title"] == "Cliente pide constancia de situación fiscal"
    await wait_for_text(httpx_mock, "Ticket #555 creado")
    assert await session_module.get_session(redis_client, WORKER_PHONE) is None


async def test_timeout_de_cliente_solo_dispara_prioridad_sin_cliente(
    client, build_flow, worker, httpx_mock
):
    """Dejar caducar la pregunta de cliente no debe arrastrar también la
    prioridad: avanza a preguntarla, el trabajador la contesta, y el ticket
    sale sin cliente pero con la prioridad que él escogió."""
    build_flow(client_timeout=1, priority_timeout=30)
    stub_llm(httpx_mock, title="Cliente pide su opinión de cumplimiento")
    stub_meta(httpx_mock)
    stub_create_ticket(httpx_mock, ticket_number=610)

    await post_webhook(
        client, text_message_payload("pide su opinión de cumplimiento", wamid="wamid.tocli.1")
    )
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)

    # No se contesta nada: al caducar, el flujo pasa solo a prioridad.
    await wait_for_interactive(httpx_mock, ASK_PRIORITY_TEXT)
    # Y nunca se llamó a la búsqueda de clientes: no hubo texto que buscar.
    assert not any("clients/search" in str(r.url) for r in httpx_mock.get_requests())

    await post_webhook(
        client,
        interactive_reply_payload(
            "alta", wamid="wamid.tocli.2", reply_type="button_reply", title="Alta"
        ),
    )
    await wait_for_ticket(httpx_mock)

    body = created_tickets(httpx_mock)[0]
    assert body["client_id"] is None  # caducó, no se adivina un cliente (decisión #5)
    assert body["priority"] == "alta"  # esta sí la contestó


async def test_timeout_de_prioridad_usa_media_con_el_cliente_ya_escogido(
    client, build_flow, worker, httpx_mock, db_session
):
    """El espejo del anterior: el trabajador sí escogió cliente, pero deja
    caducar la prioridad. El ticket se crea con ese cliente y con "media"
    por default (decisión #9: la columna es NOT NULL del otro lado)."""
    build_flow(client_timeout=30, priority_timeout=1)
    stub_llm(httpx_mock, title="Cliente pide su balanza de comprobación")
    stub_meta(httpx_mock)
    client_id = str(uuid.uuid4())
    stub_client_search(httpx_mock, "Herrera", [{"id": client_id, "name": "Herrera y Asociados"}])
    stub_create_ticket(httpx_mock, ticket_number=611)

    await post_webhook(
        client, text_message_payload("pide su balanza de comprobación", wamid="wamid.toprio.1")
    )
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)

    await post_webhook(client, text_message_payload("Herrera", wamid="wamid.toprio.2"))
    await wait_for_interactive(httpx_mock, PICK_CLIENT_TEXT)
    await post_webhook(
        client,
        interactive_reply_payload(
            client_id,
            wamid="wamid.toprio.3",
            reply_type="button_reply",
            title="Herrera y Asociados",
        ),
    )
    await wait_for_interactive(httpx_mock, ASK_PRIORITY_TEXT)

    # A partir de aquí no se contesta nada: el timeout crea el ticket.
    await wait_for_text(httpx_mock, "Ticket #611 creado")

    body = created_tickets(httpx_mock)[0]
    assert body["priority"] == "media"  # el default de la decisión #9
    assert body["client_id"] == client_id  # el que sí alcanzó a escoger

    creacion = (await db_session.execute(select(TicketCreation))).scalar_one()
    assert creacion.ticket_number == 611
    assert creacion.priority == "media"


async def test_entidades_detectadas_encabezan_la_descripcion(
    client, build_flow, worker, httpx_mock, db_session
):
    """Decisión #19, de punta a punta: lo que el LLM señala se antepone al
    texto crudo, que se manda íntegro y sin tocar."""
    build_flow()
    mensaje = "el cliente pide su factura por $12,500.00 del periodo enero 2026"
    stub_llm(
        httpx_mock,
        title="Cliente pide factura de enero",
        entities={
            "monto": "$12,500.00",
            "periodo": "enero 2026",
            "tramite": "factura",
            # Un RFC que el modelo se inventó: nadie lo escribió en el mensaje.
            "rfc": "GODE561231GR8",
        },
    )
    stub_meta(httpx_mock)
    stub_client_search(httpx_mock, "Interno", [])
    stub_create_ticket(httpx_mock, ticket_number=612)

    await post_webhook(client, text_message_payload(mensaje, wamid="wamid.ent.1"))
    await wait_for_text(httpx_mock, ASK_CLIENT_TEXT)
    await post_webhook(client, text_message_payload("Interno", wamid="wamid.ent.2"))
    await post_webhook(
        client,
        interactive_reply_payload(
            "baja", wamid="wamid.ent.3", reply_type="button_reply", title="Baja"
        ),
    )
    await wait_for_text(httpx_mock, "Ticket #612 creado")

    descripcion = created_tickets(httpx_mock)[0]["description"]
    assert descripcion == (
        f"{HEADER}\n"
        "• Monto: $12,500.00\n"
        "• Periodo: enero 2026\n"
        "• Trámite: Factura\n"
        f"{SEPARATOR}\n"
        f"{mensaje}"
    )
    # El RFC inventado no llegó al ticket: no aparecía literal en el mensaje.
    assert "GODE561231GR8" not in descripcion

    creacion = (await db_session.execute(select(TicketCreation))).scalar_one()
    assert creacion.entities["monto"] == "$12,500.00"
    assert creacion.entities["rfc"] is None


# -- Cortes antes de tocar la red -------------------------------------------


async def test_numero_fuera_de_whitelist_no_dispara_llm_ni_ticket(
    client, build_flow, worker, db_session, httpx_mock
):
    """Un número que no está en `workers` no debe generar ni una sola llamada
    saliente — ni al LLM, ni a Meta, ni al sistema de tickets."""
    build_flow()

    response = await post_webhook(
        client,
        text_message_payload("hola, quiero un servicio", wamid="wamid.ajeno.1", from_number="19998887777"),
    )
    assert response.status_code == 200

    await asyncio.sleep(1.3)  # más que el TTL del buffer: nada debió cerrarse
    assert httpx_mock.get_requests() == []
    filas = (
        await db_session.execute(select(RawMessage).where(RawMessage.wamid == "wamid.ajeno.1"))
    ).scalars().all()
    assert filas == []

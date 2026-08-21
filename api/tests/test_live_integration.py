"""
Pruebas contra servicios REALES — se excluyen de la corrida normal (marcador
`live`, ver pytest.ini) y solo corren cuando se piden a propósito:

    docker compose run --rm --no-deps api \\
        sh -lc "pip install -q -r requirements-dev.txt && pytest -m live -s"

Se corren desde un contenedor efímero del stack normal (docker-compose.yml)
porque `postgres`, `redis` y `tickets-api` solo resuelven dentro de
`cgho_net`, y porque así toman el `.env` actual sin reiniciar el bot que ya
está corriendo.

Qué es real aquí y qué no:

- **DeepSeek**: real. Es el punto de estas pruebas — verificar que el prompt
  de `services/llm/deepseek.py` sigue devolviendo el JSON que el resto del
  código asume, algo que un mock por definición no puede detectar.
- **Sistema de tickets**: real. Crea un ticket de verdad, visible en la
  interfaz de CGHO Sistema de Tickets, a nombre de un trabajador real.
- **Base del bot**: real. Escribe en `raw_messages` con commit, y esas filas
  se quedan: son el registro de auditoría de la corrida.
- **Meta**: mockeado siempre. Todavía no hay número de Cloud API verificado,
  y además el `phone_number_id` se sobrescribe con un valor falso para que
  sea imposible mandarle un WhatsApp a alguien desde una prueba.
"""

import asyncio
import json
import re
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import select

from config import get_settings
from database import AsyncSessionLocal
from main import app
from models.orm import RawMessage, TicketCreation, Worker
from services.buffer import session as session_module
from services.buffer.keys import buffer_key, session_key
from services.conversation import (
    ConversationFlow,
    lookup_active_worker_by_phone,
    record_ticket_creation,
)
from services.llm import get_extractor
from services.llm.fallback import fallback_title
from services.meta import get_meta_client
from services.ticket_system import get_ticket_system_client

from .conftest import interactive_reply_payload, post_webhook, text_message_payload, wait_for

pytestmark = pytest.mark.live

# Marca que queda en la descripción del ticket y en `raw_messages` para poder
# identificar (y depurar) lo que dejó una corrida de prueba.
TEST_MARKER = "PRUEBA AUTOMATIZADA DEL BOT — ignorar, se puede cerrar"

ASK_CLIENT_TEXT = "¿A qué cliente corresponde? Escribe el nombre."
PICK_CLIENT_TEXT = "¿Cuál de estos clientes?"
ASK_PRIORITY_TEXT = "¿Qué prioridad tiene?"


@pytest.fixture
def live_settings():
    settings = get_settings()
    if not settings.deepseek_api_key:
        pytest.skip("DEEPSEEK_API_KEY vacía — correr desde el stack de docker-compose.yml")
    return settings


# -- 1. Contrato real del prompt de extracción ------------------------------


@pytest.mark.parametrize(
    "caso, mensajes",
    [
        (
            "mensaje único",
            ["te reenvío lo del cliente: pide su constancia de situación fiscal actualizada"],
        ),
        (
            "bloque de varios mensajes",
            [
                "buenas, te paso lo del cliente",
                "manda los recibos de nómina de marzo y abril",
                "pregunta si ya se timbraron, dice que urge para el viernes",
            ],
        ),
    ],
)
async def test_deepseek_responde_en_el_formato_esperado(live_settings, caso, mensajes):
    """Llama a DeepSeek de verdad y verifica que el prompt siga produciendo el
    contrato que el resto del código asume: JSON `{"title": "..."}` con un
    título usable.

    `source == "llm"` es la aserción de fondo: cualquier desviación del
    formato (JSON malformado, campo `title` ausente, modelo retirado, key
    inválida) degrada a `source == "fallback"` sin lanzar excepción
    (decisión #7). Esa degradación silenciosa es justo lo que un mock no
    puede detectar — solo esta prueba."""
    extractor = get_extractor(live_settings)

    result = await extractor.extract_title(mensajes)
    print(f"\n[{caso}] modelo={live_settings.deepseek_model} título={result.title!r}")

    assert result.source == "llm", (
        f"DeepSeek degradó a fallback ({result.error}) — el prompt, el modelo "
        f"({live_settings.deepseek_model}) o la API key dejaron de servir"
    )
    assert result.error is None
    assert result.title and result.title.strip() == result.title
    assert "\n" not in result.title  # una sola línea: va como título del ticket
    assert not result.title.startswith(('"', "'"))  # el prompt lo pide sin comillas
    # El prompt pide máximo 80 caracteres; se deja margen para no volver la
    # prueba frágil si el modelo se pasa por poco.
    assert len(result.title) <= 120, f"Título demasiado largo: {len(result.title)}"
    # Y no es el fallback disfrazado (los primeros ~60 caracteres del crudo).
    assert result.title != fallback_title("\n".join(mensajes))


async def test_deepseek_resume_en_vez_de_recortar(live_settings):
    """Un mensaje largo y desordenado, como los que llegan reenviados de
    verdad: el título debe ser un resumen corto, no el texto crudo."""
    mensajes = [
        "oye buenas tardes disculpa que te moleste a esta hora pero el cliente acaba de "
        "escribir que el SAT le rechazó la declaración anual del ejercicio pasado por un "
        "problema con las deducciones personales y quiere saber si se puede presentar una "
        "complementaria antes de que le apliquen multa"
    ]
    result = await get_extractor(live_settings).extract_title(mensajes)
    print(f"\n[mensaje largo] título={result.title!r}")

    assert result.source == "llm"
    assert len(result.title) < len(mensajes[0]) / 2  # resumió, no recortó


async def test_deepseek_extrae_entidades_y_no_inventa(live_settings):
    """El otro contrato del prompt: que el modelo devuelva el bloque
    `entities` copiando del mensaje. La regla verbatim de
    `services/llm/entities.py` descarta lo que no aparezca literal, así que
    lo que llegue aquí ya pasó ese filtro — lo que se verifica es que el
    modelo sí esté señalando datos y no devolviendo todo en null."""
    mensaje = (
        "el cliente pide su factura por $12,500.00 del periodo enero 2026, "
        "su RFC es XAXX010101000 y la necesita el 15/03/2026"
    )
    result = await get_extractor(live_settings).extract_title([mensaje])
    entidades = result.entities
    print(f"\n[entidades] {entidades.model_dump() if entidades else None}")

    assert result.source == "llm"
    assert entidades is not None
    assert not entidades.is_empty(), (
        "El modelo no señaló ninguna entidad en un mensaje que trae monto, "
        "RFC, fecha y periodo explícitos — revisar el prompt"
    )
    # Todo lo que sobrevivió está literal en el mensaje: es la garantía que
    # da el filtro, comprobada de punta a punta contra el modelo real.
    for campo in ("monto", "fecha", "rfc", "periodo"):
        valor = getattr(entidades, campo)
        if valor:
            assert valor in mensaje


# -- 2. Ciclo completo contra el sistema de tickets real --------------------


@pytest_asyncio.fixture
async def live_worker() -> Worker:
    """Un trabajador real de la whitelist, tal como lo dejó el poll de
    `GET /internal/workers`. El ticket queda a nombre suyo: el bot nunca
    inventa un actor (decisión #14)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Worker)
            .where(Worker.is_active.is_(True), Worker.external_user_id.is_not(None))
            .order_by(Worker.created_at)
            .limit(1)
        )
        worker = result.scalar_one_or_none()
    if worker is None:
        pytest.skip(
            "No hay trabajador activo con external_user_id en `workers` — revisar "
            "que el poll contra /internal/workers esté corriendo"
        )
    return worker


@pytest_asyncio.fixture
async def live_redis(live_settings):
    """Redis real, el mismo que usa el bot en marcha. **Nunca** `flushdb`
    aquí: borraría los buffers en curso del bot."""
    redis = Redis.from_url(live_settings.redis_url, decode_responses=True)
    yield redis
    await redis.aclose()


@pytest_asyncio.fixture
async def live_app_client():
    """Cliente ASGI *sin* el override de `get_db`: el webhook usa la sesión
    real y hace commit de verdad en `raw_messages`."""
    app.dependency_overrides.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.fixture
def non_mocked_hosts(live_settings) -> list[str]:
    """DeepSeek y el sistema de tickets salen de verdad; lo demás (Meta) lo
    intercepta httpx_mock."""
    return [
        httpx.URL(live_settings.deepseek_base_url).host,
        httpx.URL(live_settings.ticket_system_base_url).host,
    ]


class _MetaSpy:
    """Lo que el bot *habría* mandado por WhatsApp, leído de los requests que
    httpx_mock interceptó."""

    def __init__(self, httpx_mock, url: str):
        self._httpx_mock = httpx_mock
        self._url = url

    def _bodies(self) -> list[dict]:
        return [
            json.loads(r.content)
            for r in self._httpx_mock.get_requests()
            if str(r.url) == self._url
        ]

    def texts(self) -> list[str]:
        return [b["text"]["body"] for b in self._bodies() if b["type"] == "text"]

    def interactive(self, body_text: str) -> dict | None:
        for body in self._bodies():
            if body["type"] == "interactive" and body["interactive"]["body"]["text"] == body_text:
                return body["interactive"]
        return None


async def test_ciclo_completo_crea_ticket_real_y_deja_raw_messages(
    live_settings, live_worker, live_redis, live_app_client, httpx_mock
):
    """El ciclo entero contra la infraestructura real: webhook -> whitelist ->
    `raw_messages` (commit real) -> buffer -> DeepSeek -> búsqueda de clientes
    -> prioridad -> ticket real en el sistema de tickets.

    Deja rastro a propósito: las filas de `raw_messages` y el ticket son la
    evidencia de la corrida."""
    settings = live_settings.model_copy(
        update={
            "buffer_ttl_seconds": 3,
            # Amplios: aquí se contesta rápido, pero el LLM real tarda y no
            # queremos que un timeout dispare el camino de default a media
            # mitad de la prueba.
            "client_response_timeout_seconds": 60,
            "priority_response_timeout_seconds": 60,
            # Credenciales falsas de Meta a propósito: aunque alguien quitara
            # el mock, no hay forma de que esta prueba mande un WhatsApp real.
            "meta_phone_number_id": "mock-live-test",
            "meta_access_token": "mock-live-test",
        }
    )
    meta_url = f"https://graph.facebook.com/{settings.meta_api_version}/mock-live-test/messages"
    httpx_mock.add_response(url=meta_url, json={"messages": [{"id": "wamid.mock"}]})
    meta = _MetaSpy(httpx_mock, meta_url)

    ticket_system = get_ticket_system_client(settings)

    # Contrato de la búsqueda de clientes contra el endpoint real: nunca más
    # de 9, porque el décimo lugar de la lista es "Sin cliente" (decisión #5).
    coincidencias = await ticket_system.search_clients("a")
    assert len(coincidencias) <= 9
    print(f"\n[clientes] GET /internal/clients/search?q=a -> {len(coincidencias)} coincidencias")

    app.state.conversation_flow = ConversationFlow(
        redis=live_redis,
        settings=settings,
        llm=get_extractor(settings),
        meta=get_meta_client(settings),
        ticket_system=ticket_system,
        worker_lookup=lookup_active_worker_by_phone,
        record_creation=record_ticket_creation,
    )
    app.state.alert_notifier = AsyncMock()

    phone = live_worker.phone_number
    run_id = uuid.uuid4().hex[:8]

    def wamid(n: int) -> str:
        return f"wamid.live-test.{run_id}.{n}"

    mensajes = [
        TEST_MARKER,
        "te reenvío lo del cliente: pide su constancia de situación fiscal actualizada",
        "dice que la necesita antes del viernes",
    ]

    # 1. Tres mensajes dentro de la ventana de debounce.
    for i, texto in enumerate(mensajes):
        response = await post_webhook(
            live_app_client, text_message_payload(texto, wamid=wamid(i), from_number=phone)
        )
        assert response.status_code == 200
        await asyncio.sleep(0.5)

    # 2. Quedaron en `raw_messages` de la base real, ya comiteados.
    wamids = [wamid(i) for i in range(len(mensajes))]
    async with AsyncSessionLocal() as db:
        filas = (
            (await db.execute(select(RawMessage).where(RawMessage.wamid.in_(wamids))))
            .scalars()
            .all()
        )
    assert len(filas) == len(mensajes)
    assert {f.worker_id for f in filas} == {live_worker.id}
    assert TEST_MARKER in {f.body for f in filas}

    # 3. Cierra el buffer y DeepSeek real genera el título.
    await wait_for(
        lambda: ASK_CLIENT_TEXT in meta.texts(),
        timeout=45,
        message="Nunca se preguntó por el cliente (¿falló el cierre del buffer o DeepSeek?)",
    )
    estado = await session_module.get_session(live_redis, phone)
    titulo = estado["title"]
    assert estado["messages"] == mensajes
    assert estado["external_user_id"] == str(live_worker.external_user_id)

    # 4. El trabajador contesta con un nombre de cliente -> búsqueda real.
    # Se usa la primera palabra de un cliente que existe de verdad, no una
    # palabra cualquiera: si la búsqueda no devuelve nada, la rama de escoger
    # cliente no se ejercita y la prueba pasaría cubriendo menos de lo que
    # parece.
    consulta = coincidencias[0].name.split()[0] if coincidencias else "Interno"
    await post_webhook(
        live_app_client,
        text_message_payload(
            consulta, wamid=f"wamid.live-test.{run_id}.cliente", from_number=phone
        ),
    )
    await wait_for(
        lambda: meta.interactive(PICK_CLIENT_TEXT) or meta.interactive(ASK_PRIORITY_TEXT),
        timeout=30,
        message="La búsqueda de clientes no derivó ni en lista ni en la pregunta de prioridad",
    )
    if coincidencias:
        assert meta.interactive(PICK_CLIENT_TEXT) is not None, (
            f"La búsqueda de {consulta!r} no ofreció clientes pese a que existen"
        )

    # 5. Si hubo coincidencias, se escoge "Sin cliente" a propósito: un ticket
    # de prueba no debe quedar colgado del historial de un cliente real.
    pregunta_cliente = meta.interactive(PICK_CLIENT_TEXT)
    if pregunta_cliente is not None:
        await post_webhook(
            live_app_client,
            interactive_reply_payload(
                "__sin_cliente__",
                wamid=f"wamid.live-test.{run_id}.sincliente",
                reply_type="list_reply" if pregunta_cliente["type"] == "list" else "button_reply",
                title="Sin cliente",
                from_number=phone,
            ),
        )
        await wait_for(
            lambda: meta.interactive(ASK_PRIORITY_TEXT) is not None,
            timeout=30,
            message="Nunca se preguntó la prioridad",
        )

    # 6. Prioridad baja: es un ticket de prueba, no debe encabezar la cola de nadie.
    await post_webhook(
        live_app_client,
        interactive_reply_payload(
            "baja",
            wamid=f"wamid.live-test.{run_id}.prioridad",
            reply_type="button_reply",
            title="Baja",
            from_number=phone,
        ),
    )

    # 7. El ticket real quedó creado y el bot confirmó con su número.
    await wait_for(
        lambda: any(t.startswith("Ticket #") for t in meta.texts()),
        timeout=45,
        message=f"El sistema de tickets nunca confirmó la creación. Mandado: {meta.texts()}",
    )
    confirmacion = next(t for t in meta.texts() if t.startswith("Ticket #"))
    numero = int(re.search(r"#(\d+)", confirmacion).group(1))
    assert numero > 0
    assert await session_module.get_session(live_redis, phone) is None

    # 8. La bitácora quedó escrita en la base real, con el bloque vinculado
    # (decisión #20).
    async with AsyncSessionLocal() as db:
        creacion = (
            await db.execute(
                select(TicketCreation).where(TicketCreation.ticket_number == numero)
            )
        ).scalar_one()
        vinculados = (
            (
                await db.execute(
                    select(RawMessage).where(RawMessage.ticket_creation_id == creacion.id)
                )
            )
            .scalars()
            .all()
        )
    assert creacion.status == "created"
    assert creacion.worker_id == live_worker.id
    assert creacion.priority == "baja"
    assert {f.wamid for f in vinculados} == set(wamids)

    print("\n=== Corrida live ===")
    print(f"Trabajador (created_by): {live_worker.name} / {live_worker.external_user_id}")
    print(f"Ticket creado en el sistema de tickets: #{numero} (prioridad baja, sin cliente)")
    print(f"Título generado por DeepSeek: {titulo!r}")
    print(f"Entidades detectadas: {estado.get('entities')}")
    print(f"raw_messages: {len(filas)} filas con wamid `wamid.live-test.{run_id}.*`")
    print(f"ticket_creations: 1 fila (status=created) con {len(vinculados)} mensajes vinculados")

    # Limpieza de Redis únicamente: las filas de `raw_messages` y el ticket se
    # quedan como evidencia.
    await live_redis.delete(session_key(phone), buffer_key(phone))

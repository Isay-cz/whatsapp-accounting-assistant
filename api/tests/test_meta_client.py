import json

import pytest

from services.meta.client import (
    BUTTON_TITLE_MAX,
    LIST_ROW_DESCRIPTION_MAX,
    LIST_ROW_TITLE_MAX,
    MetaClient,
    truncate,
)

PHONE_NUMBER_ID = "123456123"
API_VERSION = "v26.0"
EXPECTED_URL = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"


def _client() -> MetaClient:
    return MetaClient(access_token="test-token", phone_number_id=PHONE_NUMBER_ID, api_version=API_VERSION)


async def test_send_text_payload_shape(httpx_mock):
    httpx_mock.add_response(url=EXPECTED_URL, json={"messages": [{"id": "wamid.1"}]})
    await _client().send_text("16315551181", "Hola, tu ticket fue creado")

    request = httpx_mock.get_requests()[0]
    assert request.headers["Authorization"] == "Bearer test-token"
    body = json.loads(request.content)
    assert body == {
        "messaging_product": "whatsapp",
        "to": "16315551181",
        "type": "text",
        "text": {"body": "Hola, tu ticket fue creado"},
    }


async def test_send_buttons_payload_shape(httpx_mock):
    httpx_mock.add_response(url=EXPECTED_URL, json={"messages": [{"id": "wamid.2"}]})
    options = [{"id": "alta", "title": "Alta"}, {"id": "media", "title": "Media"}]
    await _client().send_buttons("16315551181", "¿Qué prioridad?", options)

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "button"
    assert body["interactive"]["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "alta", "title": "Alta"}},
        {"type": "reply", "reply": {"id": "media", "title": "Media"}},
    ]


async def test_base_url_configurable_para_pruebas_de_despliegue(httpx_mock):
    """`base_url` es lo que permite apuntar la salida a `scripts/meta_sink.py`
    en una VM de prueba sin número de Cloud API verificado. El default sigue
    siendo Graph API — lo comprueban las demás pruebas de este archivo."""
    sink_url = f"http://meta-sink:8099/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    httpx_mock.add_response(url=sink_url, json={"messages": [{"id": "wamid.sink.1"}]})

    client = MetaClient(
        access_token="test-token",
        phone_number_id=PHONE_NUMBER_ID,
        api_version=API_VERSION,
        base_url="http://meta-sink:8099/",  # la diagonal final no debe duplicarse
    )
    await client.send_text("16315551181", "Ticket #1 creado")

    assert str(httpx_mock.get_requests()[0].url) == sink_url


async def test_send_buttons_rejects_more_than_three_options():
    options = [{"id": str(i), "title": str(i)} for i in range(4)]
    with pytest.raises(ValueError):
        await _client().send_buttons("16315551181", "body", options)


async def test_send_list_payload_shape(httpx_mock):
    httpx_mock.add_response(url=EXPECTED_URL, json={"messages": [{"id": "wamid.3"}]})
    options = [{"id": "cliente_1", "title": "Juan Pérez"}, {"id": "cliente_2", "title": "Juan Ramírez"}]
    await _client().send_list("16315551181", "¿Cuál cliente?", options)

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["interactive"]["type"] == "list"
    assert body["interactive"]["action"]["sections"][0]["rows"] == [
        {"id": "cliente_1", "title": "Juan Pérez"},
        {"id": "cliente_2", "title": "Juan Ramírez"},
    ]


async def test_send_list_rejects_more_than_ten_options():
    options = [{"id": str(i), "title": str(i)} for i in range(11)]
    with pytest.raises(ValueError):
        await _client().send_list("16315551181", "body", options)


async def test_send_list_row_description(httpx_mock):
    """La descripción es opcional y solo viaja en la fila que la trae."""
    httpx_mock.add_response(url=EXPECTED_URL, json={"messages": [{"id": "wamid.4"}]})
    options = [
        {"id": "c1", "title": "Comercializadora del Ba…", "description": "Comercializadora del Bajío, S.A. de C.V."},
        {"id": "c2", "title": "Juan Pérez"},
    ]
    await _client().send_list("16315551181", "¿Cuál cliente?", options)

    body = json.loads(httpx_mock.get_requests()[0].content)
    assert body["interactive"]["action"]["sections"][0]["rows"] == [
        {"id": "c1", "title": "Comercializadora del Ba…", "description": "Comercializadora del Bajío, S.A. de C.V."},
        {"id": "c2", "title": "Juan Pérez"},
    ]


async def test_send_list_rejects_title_over_limit():
    """Que falle aquí y no contra Meta, que rechaza el mensaje completo."""
    options = [{"id": "c1", "title": "x" * (LIST_ROW_TITLE_MAX + 1)}]
    with pytest.raises(ValueError):
        await _client().send_list("16315551181", "body", options)


async def test_send_list_rejects_description_over_limit():
    options = [{"id": "c1", "title": "ok", "description": "x" * (LIST_ROW_DESCRIPTION_MAX + 1)}]
    with pytest.raises(ValueError):
        await _client().send_list("16315551181", "body", options)


async def test_send_buttons_rejects_title_over_limit():
    options = [{"id": "c1", "title": "x" * (BUTTON_TITLE_MAX + 1)}]
    with pytest.raises(ValueError):
        await _client().send_buttons("16315551181", "body", options)


def test_truncate():
    assert truncate("Juan Pérez", 24) == "Juan Pérez"
    assert truncate("x" * 24, 24) == "x" * 24
    recortado = truncate("Comercializadora del Bajío SA", 24)
    assert len(recortado) == 24 and recortado.endswith("…")
    # Sin espacio colgando antes de los puntos suspensivos.
    assert truncate("Despacho Contable Hermanos García", 18) == "Despacho Contable…"

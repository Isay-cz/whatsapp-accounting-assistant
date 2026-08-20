import json

import pytest

from services.meta.client import MetaClient

PHONE_NUMBER_ID = "123456123"
EXPECTED_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"


def _client() -> MetaClient:
    return MetaClient(access_token="test-token", phone_number_id=PHONE_NUMBER_ID, api_version="v21.0")


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

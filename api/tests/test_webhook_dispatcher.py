import json
from pathlib import Path

from sqlalchemy import select

from config import get_settings
from models.orm import RawMessage

from .conftest import sign_payload

FIXTURES = Path(__file__).parent / "fixtures" / "webhook_payloads"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def _post(client, body: bytes):
    return await client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_payload(body),
        },
    )


# -- Verificación de firma -------------------------------------------------


async def test_post_rejects_invalid_signature(client, mocked_app_state):
    body = _load("account_update.json")
    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bogus"},
    )
    assert response.status_code == 403


async def test_post_rejects_missing_signature(client, mocked_app_state):
    body = _load("account_update.json")
    response = await client.post("/webhook", content=body)
    assert response.status_code == 403


# -- Handshake de verificación (GET) ---------------------------------------


async def test_get_verify_handshake_success(client):
    settings = get_settings()
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.meta_verify_token,
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


async def test_get_verify_handshake_wrong_token(client):
    response = await client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert response.status_code == 403


# -- Dispatcher por field ----------------------------------------------------


async def test_messages_field_creates_raw_message_and_schedules_processing(
    client, mocked_app_state, worker, db_session
):
    conversation_flow, _ = mocked_app_state
    response = await _post(client, _load("messages_text.json"))
    assert response.status_code == 200

    result = await db_session.execute(select(RawMessage).where(RawMessage.wamid == "ABGGFlA5Fpa"))
    raw_message = result.scalar_one()
    assert raw_message.worker_id == worker.id
    assert raw_message.body == "this is a text message"

    conversation_flow.handle_incoming_message.assert_awaited_once_with(
        "16315551181", "this is a text message", wamid="ABGGFlA5Fpa"
    )


async def test_duplicate_wamid_is_not_reprocessed(client, mocked_app_state, worker, db_session):
    conversation_flow, _ = mocked_app_state
    body = _load("messages_text.json")

    first = await _post(client, body)
    second = await _post(client, body)
    assert first.status_code == 200
    assert second.status_code == 200

    result = await db_session.execute(select(RawMessage).where(RawMessage.wamid == "ABGGFlA5Fpa"))
    assert len(result.scalars().all()) == 1
    assert conversation_flow.handle_incoming_message.await_count == 1


async def test_message_from_unknown_number_is_ignored(client, mocked_app_state, db_session):
    # sin fixture `worker` — el número del payload no está en la whitelist
    conversation_flow, _ = mocked_app_state
    response = await _post(client, _load("messages_text.json"))
    assert response.status_code == 200

    result = await db_session.execute(select(RawMessage))
    assert result.scalars().all() == []
    conversation_flow.handle_incoming_message.assert_not_awaited()


async def test_message_defensive_parsing_without_optional_fields(
    client, mocked_app_state, worker, db_session
):
    """El payload no trae `contacts` ni `from_user_id` — campos que, según
    docs/whatsapp-webhook-reference.md, no siempre están presentes."""
    conversation_flow, _ = mocked_app_state
    response = await _post(client, _load("messages_text_minimal.json"))
    assert response.status_code == 200
    conversation_flow.handle_incoming_message.assert_awaited_once()


async def test_multi_entry_multi_change_payload_dispatches_each(
    client, mocked_app_state, worker, db_session
):
    conversation_flow, alert_notifier = mocked_app_state
    response = await _post(client, _load("multi_entry_multi_change.json"))
    assert response.status_code == 200

    conversation_flow.handle_incoming_message.assert_awaited_once()  # el mensaje del primer entry
    alert_notifier.notify.assert_awaited_once()  # solo "security" es alerta; account_alerts es informativo


async def test_account_update_notifies_alerts_channel(client, mocked_app_state):
    _, alert_notifier = mocked_app_state
    response = await _post(client, _load("account_update.json"))
    assert response.status_code == 200
    alert_notifier.notify.assert_awaited_once_with("account_update", "event=VERIFIED_ACCOUNT")


async def test_quality_update_onboarding_does_not_alert(client, mocked_app_state):
    _, alert_notifier = mocked_app_state
    response = await _post(client, _load("quality_update_onboarding.json"))
    assert response.status_code == 200
    alert_notifier.notify.assert_not_awaited()


async def test_quality_update_degradation_alerts(client, mocked_app_state):
    _, alert_notifier = mocked_app_state
    response = await _post(client, _load("quality_update_degradation.json"))
    assert response.status_code == 200
    alert_notifier.notify.assert_awaited_once()


async def test_calls_field_is_ignored(client, mocked_app_state):
    conversation_flow, alert_notifier = mocked_app_state
    response = await _post(client, _load("calls_ignored.json"))
    assert response.status_code == 200
    conversation_flow.handle_incoming_message.assert_not_awaited()
    alert_notifier.notify.assert_not_awaited()


async def test_interactive_list_reply_dispatches_to_conversation_flow(
    client, mocked_app_state, worker, db_session
):
    conversation_flow, _ = mocked_app_state
    response = await _post(client, _load("interactive_list_reply.json"))
    assert response.status_code == 200
    conversation_flow.handle_interactive_reply.assert_awaited_once_with(
        "16315551181", "cliente_482"
    )


async def test_interactive_button_reply_dispatches_to_conversation_flow(
    client, mocked_app_state, worker, db_session
):
    conversation_flow, _ = mocked_app_state
    response = await _post(client, _load("interactive_button_reply.json"))
    assert response.status_code == 200
    conversation_flow.handle_interactive_reply.assert_awaited_once_with("16315551181", "alta")

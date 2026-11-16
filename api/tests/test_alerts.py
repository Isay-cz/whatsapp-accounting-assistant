"""Canal de alertas de Meta (CLAUDE.md, decisión #22)."""

import logging

import httpx

from config import get_settings
from services.alerts import (
    HealthchecksAlertNotifier,
    LoggingAlertNotifier,
    get_alert_notifier,
)

CHECK_URL = "https://hc-ping.test/11111111-2222-3333-4444-555555555555"


def _settings(url: str):
    return get_settings().model_copy(update={"alerts_healthcheck_url": url})


def test_sin_url_las_alertas_van_solo_al_log():
    assert isinstance(get_alert_notifier(_settings("")), LoggingAlertNotifier)
    assert isinstance(get_alert_notifier(_settings("   ")), LoggingAlertNotifier)


def test_con_url_se_usa_healthchecks():
    assert isinstance(get_alert_notifier(_settings(CHECK_URL)), HealthchecksAlertNotifier)


async def test_manda_la_alerta_a_fail_con_el_aviso_en_el_cuerpo(httpx_mock):
    httpx_mock.add_response(url=f"{CHECK_URL}/fail", text="OK")

    # Con diagonal final en la URL configurada: no debe duplicarse.
    await HealthchecksAlertNotifier(CHECK_URL + "/").notify("account_update", "event=DISABLED")

    request = httpx_mock.get_requests()[0]
    assert request.method == "POST"
    assert str(request.url) == f"{CHECK_URL}/fail"
    assert request.content.decode() == "[account_update] event=DISABLED"


async def test_un_error_de_healthchecks_no_se_propaga(httpx_mock, caplog):
    httpx_mock.add_response(url=f"{CHECK_URL}/fail", status_code=500)

    with caplog.at_level(logging.WARNING, logger="bot.alerts"):
        await HealthchecksAlertNotifier(CHECK_URL).notify("security", "event=X")

    # La alerta queda en el log aunque no haya llegado…
    assert "ALERTA [security]: event=X" in caplog.text
    assert "No se pudo mandar la alerta" in caplog.text
    # …pero no la URL del check, que funciona como su credencial.
    assert CHECK_URL not in caplog.text


async def test_healthchecks_inalcanzable_no_se_propaga(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("sin red"), url=f"{CHECK_URL}/fail")

    await HealthchecksAlertNotifier(CHECK_URL).notify("security", "event=X")


async def test_el_cuerpo_se_acota(httpx_mock):
    httpx_mock.add_response(url=f"{CHECK_URL}/fail", text="OK")

    await HealthchecksAlertNotifier(CHECK_URL).notify("security", "x" * 10_000)

    assert len(httpx_mock.get_requests()[0].content) == 2000

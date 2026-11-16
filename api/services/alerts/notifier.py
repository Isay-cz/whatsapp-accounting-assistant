import logging
from typing import Protocol

import httpx

from config import Settings

logger = logging.getLogger("bot.alerts")

# healthchecks.io guarda el principio del cuerpo de cada ping; un aviso de
# Meta cabe de sobra, esto solo evita mandar un payload entero por accidente.
_MAX_BODY_CHARS = 2000


class AlertNotifier(Protocol):
    async def notify(self, subject: str, detail: str) -> None: ...


class LoggingAlertNotifier:
    """Implementación de respaldo: solo escribe a log. Es la que se usa cuando
    `ALERTS_HEALTHCHECK_URL` no está definida (desarrollo, pruebas)."""

    async def notify(self, subject: str, detail: str) -> None:
        logger.warning("ALERTA [%s]: %s", subject, detail)


class HealthchecksAlertNotifier:
    """Manda la alerta a un check de healthchecks.io (CLAUDE.md, decisión #22).

    Un `POST` a `<URL>/fail` con el aviso en el cuerpo pone el check en rojo, y
    healthchecks.io avisa por los canales que ya tenga configurados. La alerta
    no puede viajar por WhatsApp: lo que avisa es justo que el número o la
    cuenta tienen un problema.

    `notify` nunca lanza: un fallo al avisar se registra y nada más. El
    webhook tiene que contestar 200 de todos modos (decisión #11)."""

    def __init__(self, url: str, timeout_seconds: float = 5.0):
        self._fail_url = url.rstrip("/") + "/fail"
        self._timeout_seconds = timeout_seconds

    async def notify(self, subject: str, detail: str) -> None:
        # Al log también: es el rastro local si healthchecks.io no contesta.
        logger.warning("ALERTA [%s]: %s", subject, detail)
        body = f"[{subject}] {detail}"[:_MAX_BODY_CHARS]
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(self._fail_url, content=body.encode())
                response.raise_for_status()
        except Exception as exc:
            # Sin el mensaje de la excepción: httpx escribe ahí la URL del
            # check, que funciona como su credencial.
            logger.error(
                "No se pudo mandar la alerta [%s] a healthchecks.io (%s)",
                subject,
                type(exc).__name__,
            )


def get_alert_notifier(settings: Settings) -> AlertNotifier:
    url = settings.alerts_healthcheck_url.strip()
    if url:
        return HealthchecksAlertNotifier(url)
    return LoggingAlertNotifier()

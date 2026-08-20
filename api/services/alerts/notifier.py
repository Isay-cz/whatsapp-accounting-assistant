import logging
from typing import Protocol

logger = logging.getLogger("bot.alerts")


class AlertNotifier(Protocol):
    async def notify(self, subject: str, detail: str) -> None: ...


class LoggingAlertNotifier:
    """Implementación de respaldo: solo escribe a log. El canal real
    (Telegram, correo) todavía no está decidido — ver CLAUDE.md,
    Pendientes. Cuando se decida, se agrega una implementación nueva detrás
    del mismo protocolo `AlertNotifier`, sin tocar quien lo llama."""

    async def notify(self, subject: str, detail: str) -> None:
        logger.warning("ALERTA [%s]: %s", subject, detail)


def get_alert_notifier() -> AlertNotifier:
    return LoggingAlertNotifier()

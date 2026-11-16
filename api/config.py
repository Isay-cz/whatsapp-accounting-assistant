import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Sin cualquiera de estos el bot arranca, pero no funciona o no es seguro: con
# el App Secret vacío, el HMAC del webhook lo calcula cualquiera. Por eso el
# arranque falla en vez de avisar.
REQUIRED_SETTINGS = (
    "meta_app_secret",
    "meta_verify_token",
    "meta_access_token",
    "meta_phone_number_id",
    "internal_api_token",
    "ticket_system_base_url",
)
# Valores de relleno que alguien deja "mientras tanto" y se quedan.
PLACEHOLDER_VALUES = frozenset({"changeme", "placeholder", "xxx"})


def _is_unset(value: str) -> bool:
    cleaned = value.strip().lower()
    return not cleaned or cleaned in PLACEHOLDER_VALUES


class Settings(BaseSettings):
    """
    Fuente única de configuración. Todas las variables llegan por entorno
    (Docker Compose `environment:` en runtime, o el shell en local/tests) —
    nunca `env_file` aquí ni `load_dotenv()` en otro lado (ver CLAUDE.md,
    decisión #12: la versión Twilio tenía tres mecanismos de env vars que no
    se comunicaban entre sí y perdían variables silenciosamente).
    """

    # Base de datos
    database_url: str

    # Redis — buffer de mensajes y timeouts de confirmación (namespaced con
    # el prefijo de llave `bot:`, ver services/buffer)
    redis_url: str
    buffer_ttl_seconds: int = 45
    client_response_timeout_seconds: int = 60
    priority_response_timeout_seconds: int = 60

    # LLM — DeepSeek
    llm_provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout_seconds: float = 15.0

    # WhatsApp Cloud API (Meta)
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    meta_api_version: str = "v26.0"
    # Solo se cambia en entornos de prueba, para apuntar la salida a un
    # receptor local (`scripts/meta_sink.py`) en vez de a Meta. En producción
    # se deja el default: si esto apuntara a otro lado, el bot dejaría de
    # mandar WhatsApp sin que nada fallara visiblemente.
    meta_graph_base_url: str = "https://graph.facebook.com"

    # Sistema de tickets (CGHO Sistema de Tickets)
    internal_api_token: str = ""
    ticket_system_base_url: str = ""
    # Cada cuánto se re-consulta el roster de trabajadores. La ventana de
    # retraso es aceptable: son ~22 personas y las altas/bajas son raras.
    worker_sync_interval_seconds: int = 300

    # App
    debug: bool = False
    # Eco de cada sentencia SQL con sus parámetros, es decir, con el texto de
    # los mensajes de los clientes. Va aparte de `debug` para que prender uno
    # no vuelque conversaciones al log sin querer. Solo desarrollo.
    sql_echo: bool = False

    @model_validator(mode="after")
    def _require_secrets(self) -> "Settings":
        """Falla el arranque antes que atender con un secreto vacío.

        Se evalúa al construir `Settings`, que `main.py` hace al importarse:
        un despliegue mal configurado muere con un mensaje claro en vez de
        aceptar webhooks que cualquiera puede firmar."""
        missing = [
            name.upper() for name in REQUIRED_SETTINGS if _is_unset(getattr(self, name))
        ]
        if missing:
            raise ValueError(
                "Faltan variables obligatorias (vacías o con un valor de relleno): "
                + ", ".join(missing)
                + ". El bot no arranca sin ellas; ver .env.example."
            )
        if _is_unset(self.deepseek_api_key):
            # No detiene el arranque: sin llave la extracción degrada al título
            # de respaldo y el ticket se crea igual (CLAUDE.md, decisión #7).
            logger.warning(
                "DEEPSEEK_API_KEY vacía: los tickets saldrán con el título de "
                "respaldo y sin entidades."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

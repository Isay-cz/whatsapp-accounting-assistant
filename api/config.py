from pydantic_settings import BaseSettings
from functools import lru_cache


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
    meta_api_version: str = "v21.0"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()

from config import Settings

from .base import LLMExtractor
from .deepseek import DeepSeekExtractor


def get_extractor(settings: Settings) -> LLMExtractor:
    """Punto único de swap de proveedor — hoy solo DeepSeek, pero todo el
    resto del código depende únicamente del protocolo `LLMExtractor`."""
    if settings.llm_provider == "deepseek":
        return DeepSeekExtractor(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.deepseek_timeout_seconds,
        )
    raise ValueError(f"Proveedor de LLM desconocido: {settings.llm_provider!r}")

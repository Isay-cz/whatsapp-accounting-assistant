import asyncio
import json
import logging

import httpx

from .base import ExtractionResult
from .fallback import fallback_title

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Eres un asistente que resume mensajes de clientes de un despacho "
    "contable en un título corto y claro para un ticket interno. "
    'Responde únicamente JSON con la forma {"title": "..."}. '
    "El título debe tener como máximo 80 caracteres, en español, sin "
    "comillas ni explicación adicional."
)


class DeepSeekExtractor:
    """Extracción vía DeepSeek (deepseek-v4-flash, endpoint compatible con
    OpenAI). `extract_title` nunca lanza: cualquier excepción (timeout,
    error HTTP, JSON malformado) se captura y degrada a `fallback_title`."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout_seconds: float):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def extract_title(self, messages: list[str]) -> ExtractionResult:
        raw_text = "\n".join(messages)
        try:
            title = await asyncio.wait_for(
                self._call(raw_text), timeout=self._timeout_seconds
            )
            return ExtractionResult(title=title, source="llm")
        except Exception as exc:  # cualquier falla degrada a fallback, nunca se propaga
            logger.warning("Extracción DeepSeek falló, usando fallback: %s", exc)
            return ExtractionResult(
                title=fallback_title(raw_text), source="fallback", error=str(exc)
            )

    async def _call(self, raw_text: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": raw_text},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            title = json.loads(content)["title"]
            if not isinstance(title, str) or not title.strip():
                raise ValueError("DeepSeek devolvió un título vacío")
            return title.strip()

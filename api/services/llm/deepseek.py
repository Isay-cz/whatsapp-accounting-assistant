import asyncio
import json
import logging

import httpx

from .base import ExtractionResult
from .entities import validate_entities
from .fallback import fallback_title

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Eres un asistente que procesa mensajes de clientes de un despacho "
    "contable para abrir un ticket interno.\n"
    "Responde únicamente JSON con la forma "
    '{"title": "...", "entities": {"monto": null, "fecha": null, '
    '"rfc": null, "periodo": null, "tramite": null}}.\n'
    "\n"
    "title: máximo 80 caracteres, en español, sin comillas ni explicación.\n"
    "\n"
    "entities: datos que YA aparecen en el mensaje. Copia cada valor "
    "literalmente, tal como está escrito; no lo reformatees, no lo "
    "completes y no lo deduzcas. Si un dato no aparece, déjalo en null. "
    "Es preferible null a un valor aproximado.\n"
    "- monto: la cantidad de dinero mencionada.\n"
    "- fecha: la fecha o el plazo mencionado.\n"
    "- rfc: el RFC mencionado.\n"
    "- periodo: el periodo fiscal mencionado.\n"
    "- tramite: uno exacto de "
    "[factura, nomina, declaracion, contabilidad, imss, constancia], "
    "o null si ninguno aplica claramente.\n"
    "\n"
    "Nunca infieras el nombre del cliente ni la urgencia: eso lo confirma "
    "una persona."
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
            title, entities = await asyncio.wait_for(
                self._call(raw_text), timeout=self._timeout_seconds
            )
            return ExtractionResult(title=title, source="llm", entities=entities)
        except Exception as exc:  # cualquier falla degrada a fallback, nunca se propaga
            logger.warning("Extracción DeepSeek falló, usando fallback: %s", exc)
            return ExtractionResult(
                title=fallback_title(raw_text), source="fallback", error=str(exc)
            )

    async def _call(self, raw_text: str):
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
            payload = json.loads(content)
            title = payload["title"]
            if not isinstance(title, str) or not title.strip():
                raise ValueError("DeepSeek devolvió un título vacío")
            # Degradación parcial: las entidades son un extra. Si vienen
            # malformadas se pierden ellas, no el título — que es lo único
            # sin lo cual no se puede abrir el ticket.
            entities = validate_entities(payload.get("entities"), raw_text)
            return title.strip(), entities

from typing import Literal, Protocol

from pydantic import BaseModel


class ExtractionResult(BaseModel):
    title: str
    source: Literal["llm", "fallback"]
    error: str | None = None


class LLMExtractor(Protocol):
    async def extract_title(self, messages: list[str]) -> ExtractionResult:
        """Genera un título a partir de los mensajes crudos de un bloque de
        buffer. Nunca lanza — cualquier falla se resuelve internamente en
        un ExtractionResult con source="fallback" (ver CLAUDE.md, decisión
        #7: la creación del ticket nunca se bloquea por una falla del LLM)."""
        ...

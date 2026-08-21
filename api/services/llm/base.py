from typing import Literal, Protocol

from pydantic import BaseModel

from models.schemas import ExtractedEntities


class ExtractionResult(BaseModel):
    title: str
    source: Literal["llm", "fallback"]
    entities: ExtractedEntities | None = None
    error: str | None = None


class LLMExtractor(Protocol):
    async def extract_title(self, messages: list[str]) -> ExtractionResult:
        """Genera un título —y, si las hay, las entidades— a partir de los
        mensajes crudos de un bloque de buffer. Nunca lanza: cualquier falla
        se resuelve internamente en un ExtractionResult con
        source="fallback" (ver CLAUDE.md, decisión #7: la creación del
        ticket nunca se bloquea por una falla del LLM)."""
        ...

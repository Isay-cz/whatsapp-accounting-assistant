"""Validación de las entidades que devuelve el LLM.

El seguro central es la **regla verbatim**: un monto, una fecha, un RFC o un
periodo solo sobreviven si la cadena aparece literalmente en el bloque de
mensajes. El LLM señala, no interpreta — y menos inventa. La razón está en
CLAUDE.md, decisión #19: la descripción se escribe una sola vez en un
sistema que este bot no puede corregir después, así que un RFC alucinado se
ve autoritativo y alguien lo copia a un trámite real.

`tramite` es la excepción: es una clasificación, no una copia, así que su
defensa es la lista cerrada de `Tramite`. Lo que no caiga exacto se descarta.

Nada aquí lanza: entradas malformadas producen entidades vacías, nunca un
error que tumbe la extracción del título (decisión #7).
"""

import logging
import re
import unicodedata
from typing import Any

from models.schemas import ExtractedEntities, Tramite

logger = logging.getLogger(__name__)

_VERBATIM_FIELDS = ("monto", "fecha", "rfc", "periodo")
# Solo para la comparación verbatim: el LLM suele devolver el mismo dato con
# otros espacios o sin acentos. Se compara normalizado, pero se guarda el
# valor tal como lo devolvió el modelo.
_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(c for c in lowered if not unicodedata.combining(c))
    return _WHITESPACE.sub(" ", stripped).strip()


def validate_entities(raw: Any, source_text: str) -> ExtractedEntities:
    """Filtra lo que devolvió el LLM contra el texto crudo del que salió.

    Devuelve entidades vacías —nunca lanza— si `raw` no es un dict, si los
    valores no son cadenas, o si nada pasa las reglas."""
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("El LLM devolvió `entities` con forma inesperada: %r", type(raw))
        return ExtractedEntities()

    haystack = _normalize(source_text)
    kept: dict[str, Any] = {}

    for field in _VERBATIM_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if _normalize(value) not in haystack:
            # El caso que justifica todo este módulo: el modelo "completó" un
            # dato que nadie escribió.
            logger.warning(
                "Entidad descartada por no aparecer literal en el mensaje: %s=%r", field, value
            )
            continue
        kept[field] = value

    tramite = raw.get("tramite")
    if isinstance(tramite, str):
        try:
            kept["tramite"] = Tramite(tramite.strip().casefold())
        except ValueError:
            logger.info("Trámite fuera de la lista cerrada, se descarta: %r", tramite)

    return ExtractedEntities(**kept)

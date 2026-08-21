"""Armado de la descripción del ticket.

Función pura a propósito: es lo único de la descripción que decide *forma*,
y así se prueba sin LLM, sin Redis y sin red.

Regla que no cambia: el texto crudo del cliente va completo y sin tocar. Las
entidades se **anteponen** como un bloque separado y claramente atribuible al
bot — nunca reemplazan ni reescriben lo que la persona escribió.
"""

from models.schemas import TRAMITE_LABELS, ExtractedEntities

HEADER = "Datos detectados:"
SEPARATOR = "—" * 20

_FIELD_LABELS = {
    "monto": "Monto",
    "fecha": "Fecha",
    "rfc": "RFC",
    "periodo": "Periodo",
}


def build_description(messages: list[str], entities: ExtractedEntities | None) -> str:
    """Descripción final: bloque de entidades (si hay) + mensajes crudos.

    Sin entidades no se antepone nada — un encabezado que dice "no se detectó
    nada" es ruido en cada ticket."""
    raw = "\n".join(messages)
    if entities is None or entities.is_empty():
        return raw

    lines = [HEADER]
    for field, label in _FIELD_LABELS.items():
        value = getattr(entities, field)
        if value:
            lines.append(f"• {label}: {value}")
    if entities.tramite:
        lines.append(f"• Trámite: {TRAMITE_LABELS[entities.tramite]}")
    lines.append(SEPARATOR)

    return "\n".join(lines) + "\n" + raw

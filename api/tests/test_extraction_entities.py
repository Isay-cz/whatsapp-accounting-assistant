"""
Extracción de entidades: la regla verbatim, la lista cerrada de trámites y
el armado de la descripción (CLAUDE.md, decisión #19).

Lo que se prueba aquí es sobre todo lo que *no* debe pasar: que un dato que
el modelo no copió del mensaje jamás llegue a la descripción de un ticket.
"""

import json

from models.schemas import ExtractedEntities, Tramite
from services.conversation.description import HEADER, SEPARATOR, build_description
from services.llm.deepseek import DeepSeekExtractor
from services.llm.entities import validate_entities

BASE_URL = "https://api.deepseek.test"

MENSAJE = (
    "el cliente pide su factura por $12,500.00 del periodo enero 2026, "
    "su RFC es XAXX010101000 y la necesita el 15/03/2026"
)


def _extractor() -> DeepSeekExtractor:
    return DeepSeekExtractor(
        api_key="test-key", model="deepseek-v4-flash", base_url=BASE_URL, timeout_seconds=5.0
    )


def _respuesta(title: str, entities) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({"title": title, "entities": entities})}}]
    }


# -- Regla verbatim ---------------------------------------------------------


def test_conserva_las_entidades_que_aparecen_literales():
    entities = validate_entities(
        {
            "monto": "$12,500.00",
            "fecha": "15/03/2026",
            "rfc": "XAXX010101000",
            "periodo": "enero 2026",
            "tramite": "factura",
        },
        MENSAJE,
    )
    assert entities.monto == "$12,500.00"
    assert entities.fecha == "15/03/2026"
    assert entities.rfc == "XAXX010101000"
    assert entities.periodo == "enero 2026"
    assert entities.tramite is Tramite.factura


def test_descarta_la_entidad_que_el_modelo_invento():
    """El caso que justifica todo el módulo: un RFC con formato válido que
    nadie escribió. Una vez en la descripción se ve autoritativo y alguien
    lo copia a un trámite real."""
    entities = validate_entities(
        {"rfc": "GODE561231GR8", "monto": "$12,500.00"}, MENSAJE
    )
    assert entities.rfc is None
    assert entities.monto == "$12,500.00"  # esta sí estaba


def test_descarta_el_dato_reformateado():
    """`12500` no aparece así en el mensaje: el modelo lo normalizó. Se
    descarta — preferimos un hueco a un valor que no es cita textual."""
    assert validate_entities({"monto": "12500"}, MENSAJE).monto is None


def test_la_comparacion_ignora_acentos_y_espacios_de_mas():
    mensaje = "necesita la constancia del periodo  Enero  2026"
    assert validate_entities({"periodo": "enero 2026"}, mensaje).periodo == "enero 2026"


def test_tramite_fuera_de_la_lista_cerrada_se_descarta():
    """`tramite` es el único campo inferido, así que su defensa no es la
    regla verbatim sino el enum."""
    assert validate_entities({"tramite": "auditoria"}, MENSAJE).tramite is None
    assert validate_entities({"tramite": "FACTURA"}, MENSAJE).tramite is Tramite.factura


def test_entidades_malformadas_no_lanzan():
    assert validate_entities(None, MENSAJE).is_empty()
    assert validate_entities("no soy un dict", MENSAJE).is_empty()
    assert validate_entities({"monto": 12500}, MENSAJE).is_empty()  # número, no cadena
    assert validate_entities({"desconocido": "x"}, MENSAJE).is_empty()


# -- Integración con el extractor ------------------------------------------


async def test_extraccion_devuelve_titulo_y_entidades(httpx_mock):
    httpx_mock.add_response(
        url=f"{BASE_URL}/chat/completions",
        json=_respuesta(
            "Cliente pide factura de enero",
            {"monto": "$12,500.00", "periodo": "enero 2026", "tramite": "factura"},
        ),
    )
    result = await _extractor().extract_title([MENSAJE])
    assert result.source == "llm"
    assert result.entities.monto == "$12,500.00"
    assert result.entities.tramite is Tramite.factura


async def test_entidades_malformadas_no_pierden_el_titulo(httpx_mock):
    """Degradación parcial: más campos en el JSON es más superficie de falla,
    y el título es lo único sin lo cual no se puede abrir el ticket."""
    httpx_mock.add_response(
        url=f"{BASE_URL}/chat/completions",
        json=_respuesta("Cliente pide factura de enero", "esto no es un objeto"),
    )
    result = await _extractor().extract_title([MENSAJE])
    assert result.source == "llm"
    assert result.title == "Cliente pide factura de enero"
    assert result.entities.is_empty()


async def test_el_fallback_no_trae_entidades(httpx_mock):
    httpx_mock.add_response(url=f"{BASE_URL}/chat/completions", status_code=500)
    result = await _extractor().extract_title([MENSAJE])
    assert result.source == "fallback"
    assert result.entities is None


# -- Armado de la descripción ----------------------------------------------


def test_descripcion_antepone_las_entidades_sin_tocar_el_crudo():
    descripcion = build_description(
        ["necesita su factura", "urge"],
        ExtractedEntities(monto="$12,500.00", tramite=Tramite.nomina),
    )
    assert descripcion == (
        f"{HEADER}\n"
        "• Monto: $12,500.00\n"
        "• Trámite: Nómina\n"
        f"{SEPARATOR}\n"
        "necesita su factura\nurge"
    )


def test_sin_entidades_la_descripcion_es_solo_el_crudo():
    """Un encabezado que dice "no se detectó nada" sería ruido en cada ticket."""
    assert build_description(["hola", "adiós"], None) == "hola\nadiós"
    assert build_description(["hola"], ExtractedEntities()) == "hola"

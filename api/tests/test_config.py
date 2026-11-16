"""El arranque falla sin los secretos que hacen al bot seguro o funcional.

Se construye `Settings()` directo y no `get_settings()`, que está en caché con
los valores de `conftest.py`.
"""

import logging

import pytest
from pydantic import ValidationError

from config import PLACEHOLDER_VALUES, REQUIRED_SETTINGS, Settings


def test_con_todo_configurado_arranca():
    settings = Settings()
    for name in REQUIRED_SETTINGS:
        assert getattr(settings, name)


@pytest.mark.parametrize("name", REQUIRED_SETTINGS)
def test_una_variable_obligatoria_vacia_no_arranca(monkeypatch, name):
    monkeypatch.setenv(name.upper(), "")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert name.upper() in str(excinfo.value)


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_VALUES))
def test_un_valor_de_relleno_no_arranca(monkeypatch, placeholder):
    # Con mayúsculas y espacios: así es como queda escrito a mano en un .env.
    monkeypatch.setenv("META_APP_SECRET", f" {placeholder.upper()} ")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert "META_APP_SECRET" in str(excinfo.value)


def test_el_error_lista_todo_lo_que_falta(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    message = str(excinfo.value)
    assert "META_APP_SECRET" in message and "INTERNAL_API_TOKEN" in message


def test_los_valores_de_prueba_no_son_de_relleno():
    """Si un default de `conftest.py` cayera en la lista de marcadores, la
    suite entera dejaría de importar `main`."""
    settings = Settings()
    for name in REQUIRED_SETTINGS:
        assert getattr(settings, name).strip().lower() not in PLACEHOLDER_VALUES


def test_sin_llave_de_deepseek_arranca_con_aviso(monkeypatch, caplog):
    """La extracción degrada al título de respaldo (decisión #7): no es motivo
    para no arrancar, pero tiene que quedar dicho en el log."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    with caplog.at_level(logging.WARNING, logger="config"):
        Settings()
    assert "DEEPSEEK_API_KEY" in caplog.text

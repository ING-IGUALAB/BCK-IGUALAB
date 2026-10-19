"""Tests de la ruta del Asistente IA (Swagger) — registro y guardas."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.modules.asistente.router import (
    esquema_vectorial_listo,
    get_proveedor_llm,
    obtener_ambiente,
    router,
)


def _request_con_estado(ingesta, ingesta_error=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ingesta=ingesta, ingesta_error=ingesta_error)))


def test_ruta_registrada_en_swagger():
    rutas = [r.path for r in router.routes]
    assert "/asistente/consultas" in rutas


def test_tag_del_router():
    assert router.tags == ["Asistente IA (RAG)"]


def test_llm_sin_configurar_responde_503(monkeypatch):
    monkeypatch.setattr("app.modules.asistente.router.llm_configurado", lambda: False)
    with pytest.raises(ServiceUnavailableError) as e:
        get_proveedor_llm()
    assert e.value.code == "LLM_NOT_CONFIGURED"


def test_ambiente_desde_app_env(monkeypatch):
    monkeypatch.setattr("app.modules.asistente.router.settings", type("S", (), {"APP_ENV": "development"})())
    assert obtener_ambiente() == "development"


def test_esquema_vectorial_sin_ingesta_responde_503_con_el_motivo_real():
    error = ("INGESTION_SCHEMA_NOT_READY", "no se pudo preparar el esquema", {"base": "vectorial"})
    with pytest.raises(ServiceUnavailableError) as e:
        esquema_vectorial_listo(_request_con_estado(None, error))
    assert e.value.code == "INGESTION_SCHEMA_NOT_READY"
    assert e.value.details == {"base": "vectorial"}


def test_esquema_vectorial_con_ingesta_preparada_no_interviene():
    esquema_vectorial_listo(_request_con_estado(object()))

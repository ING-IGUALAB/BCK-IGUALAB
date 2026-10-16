"""Tests de la ruta del Asistente IA (Swagger) — registro y guardas."""
from __future__ import annotations

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.modules.asistente.router import get_proveedor_llm, obtener_ambiente, router


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

"""Tests de la ruta de Auditoría (Swagger) — registro y guardas."""
from __future__ import annotations

from app.modules.auditoria.router import router


def test_ruta_registrada_en_swagger():
    rutas = [r.path for r in router.routes]
    assert "/auditoria" in rutas


def test_tag_del_router():
    assert router.tags == ["Auditoría"]


def test_es_solo_lectura():
    metodos = {m for r in router.routes for m in r.methods}
    assert metodos == {"GET"}

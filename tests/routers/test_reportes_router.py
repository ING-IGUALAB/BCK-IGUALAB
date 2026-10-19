"""Tests de las rutas de Reportes (Swagger) — registro."""
from __future__ import annotations

from app.modules.reportes.router import router


def test_rutas_registradas_en_swagger():
    rutas = sorted(r.path for r in router.routes)
    assert "/reportes" in rutas
    assert "/reportes/{reporte_id}/archivo" in rutas


def test_tag_del_router():
    assert router.tags == ["Reportes de Prospección"]

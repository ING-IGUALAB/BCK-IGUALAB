"""Tests del servicio de Auditoría (Módulo 8): consulta con filtros y paginación (RF-029/030)."""
from __future__ import annotations

import uuid

from app.models import TipoEventoAuditoria
from app.modules.auditoria.service import listar_eventos


class FakeResult:
    def __init__(self, count, rows):
        self._count = count
        self._rows = rows

    def scalar_one(self):
        return self._count

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, count, rows):
        self._count = count
        self._rows = rows

    async def execute(self, _q):
        return FakeResult(self._count, self._rows)


def _fila(i):
    return type("F", (), {
        "id": uuid.uuid4(), "usuario_id": uuid.uuid4(),
        "tipo_evento": TipoEventoAuditoria.INICIO_SESION,
        "detalle": f"evento {i}", "fecha_hora_utc": None, "resultado": "EXITOSO" if i % 2 else None,
    })()


async def test_listar_sin_filtros_devuelve_pagina():
    db = FakeDB(2, [_fila(1), _fila(2)])
    r = await listar_eventos(db)
    assert r["total"] == 2
    assert r["pagina"] == 1
    assert r["tamano"] == 20
    assert len(r["items"]) == 2


async def test_listar_con_filtros_y_paginacion():
    db = FakeDB(50, [_fila(1)])
    r = await listar_eventos(db, tipo_evento=TipoEventoAuditoria.INGESTA_DATOS,
                             usuario_id=uuid.uuid4(), pagina=3, tamano=10)
    assert r["total"] == 50
    assert r["pagina"] == 3
    assert r["tamano"] == 10
    assert len(r["items"]) == 1


async def test_listar_vacio():
    db = FakeDB(0, [])
    r = await listar_eventos(db)
    assert r["total"] == 0
    assert r["items"] == []

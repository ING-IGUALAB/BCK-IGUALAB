"""Sector «Otros» (`OTROS`) en el almacenamiento y el filtro de la base vectorial, SIN pgvector: una sesión de captura registra lo que
se enviaría a la base. Comprueban que `OTROS` se guarda como texto en `fragmentos_documento.sector` (VARCHAR(64) sin lista de
valores) y que el filtro `sector = :sector` lo recibe igual que a cualquier otro sector. La ejecución real contra pgvector está en
`test_fragmentos_vectoriales.py` (necesita Docker) y no se sustituye por estas pruebas."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import SectorEmpresa
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as fv
from tests.services.test_fragmentos_vectoriales import EMPRESA, IDENTIDAD, args, eje, lote


class SesionDeCaptura:
    """Sesión vectorial mínima: registra cada consulta y sus parámetros, y no devuelve filas."""

    def __init__(self):
        self.consultas: list[tuple[str, object]] = []

    def in_transaction(self) -> bool:
        return False

    async def execute(self, consulta, parametros=None):
        self.consultas.append((str(consulta), parametros))
        return SimpleNamespace(all=lambda: [], first=lambda: None)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.parametrize("sector", list(SectorEmpresa), ids=lambda s: s.name)
async def test_los_fragmentos_se_guardan_con_el_valor_del_sector_incluido_otros(sector):
    db = SesionDeCaptura()
    paquete = lote()

    insertados = await fv.guardar_lote(db, lote=paquete, **args(sector=sector))

    filas = db.consultas[-1][1]
    assert insertados == len(paquete.elementos)
    assert {fila["sector"] for fila in filas} == {sector.value}


@pytest.mark.parametrize("sector", list(SectorEmpresa), ids=lambda s: s.name)
async def test_la_busqueda_filtra_por_el_valor_del_sector_incluido_otros(sector):
    db = SesionDeCaptura()

    resultado = await fv.buscar_similares(
        db, ambiente="development", vector_consulta=eje(0), identidad=IDENTIDAD, empresa_ids=[EMPRESA], limite=5, anio=2025, sector=sector,
    )

    sql, parametros = db.consultas[-1]
    assert resultado == []
    assert "sector = :sector" in sql
    assert parametros["sector"] == sector.value
    assert parametros["empresas"] == [EMPRESA]
    assert parametros["anio"] == 2025
    assert parametros["ambiente"] == "development"

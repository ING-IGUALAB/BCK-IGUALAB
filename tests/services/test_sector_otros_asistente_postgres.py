"""Asistente IA (Módulo 6) con una empresa del sector «Otros» (`OTROS`) por HTTP sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

REAL: autenticación y roles, validación de la petición, comprobación de empresa/sector/estado en la base y visibilidad transaccional de
los documentos. CONTROLADOS (dobles, sin red, sin OCI ni DeepSeek): embeddings, modelo de chat y la búsqueda vectorial
`buscar_similares` (aquí no hay pgvector): se comprueba QUÉ filtros recibe, no que pgvector los aplique. Reutiliza los dobles y el
armado de `test_asistente_postgres.py`. Sin PostgreSQL se OMITEN (`pytest -rs`)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio

from app.models import SectorEmpresa
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    comprobar_error,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
)
from tests.services.test_asistente import afirmacion, respuesta
from tests.services.test_asistente_postgres import (
    IDENTIDAD,
    EmbeddingsControlado,
    LlmControlado,
    consultar,
    fragmento_de,
    servicio,
    sin_proveedores_ni_busqueda,
)


@pytest_asyncio.fixture
async def m6(fabrica_pg, monkeypatch):
    """Una empresa OTROS con su documento, otra OTROS (rival) y una de minería, todas con documentos completados."""
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Servicios Varios", sector=SectorEmpresa.OTROS)
        otra = await crear_empresa(db, "Consultora Rival", sector=SectorEmpresa.OTROS)
        minera = await crear_empresa(db, "Minera Andina", sector=SectorEmpresa.MINERIA)
        doc = await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, nombre="memoria-otros-2025.md")
        doc_otra = await sembrar_documento(db, otra, admin, texto=MD_GRI_204_305, nombre="rival.md")
    busquedas: list[dict] = []
    entorno = SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, superadmin=superadmin, empresa=empresa, otra=otra, minera=minera,
        doc=doc, doc_otra=doc_otra, busquedas=busquedas, devolver=[],
        embeddings=EmbeddingsControlado(), llm=LlmControlado(),
    )

    async def buscar_controlado(db_vectorial, **kwargs):
        busquedas.append(kwargs)
        return list(entorno.devolver)

    monkeypatch.setattr(servicio, "buscar_similares", buscar_controlado)
    return entorno


async def test_el_asistente_acepta_el_sector_otros_y_limita_la_recuperacion_a_su_empresa_anio_ambiente_y_sector(m6):
    m6.devolver = [fragmento_de(m6), fragmento_de(m6, m6.doc_otra, m6.otra, texto="Contenido de OTRA empresa que no debe verse.")]
    m6.llm.contenido = respuesta(afirmacion())

    r = await consultar(m6, sector="OTROS")

    assert r.status_code == 200, r.text
    (busqueda,) = m6.busquedas
    assert busqueda["sector"] is SectorEmpresa.OTROS
    assert busqueda["empresa_ids"] == [m6.empresa.id]
    assert busqueda["anio"] == 2025
    assert busqueda["ambiente"] == "development"
    assert busqueda["identidad"] == IDENTIDAD
    datos = r.json()
    assert datos["sin_evidencia"] is False
    assert [c["documento_id"] for c in datos["citas"]] == [str(m6.doc.id)]  # solo el documento de la empresa elegida
    assert "Empresa: Servicios Varios | Documento: memoria-otros-2025.md | Año: 2025" in m6.llm.usuario
    assert "OTRA empresa" not in m6.llm.usuario


async def test_el_sector_se_sigue_contrastando_con_la_empresa_tambien_para_otros(m6):
    de_otros_como_mineria = await consultar(m6, sector="MINERIA")
    comprobar_error(de_otros_como_mineria, 400, "COMPANY_SECTOR_MISMATCH")
    sin_proveedores_ni_busqueda(m6)

    de_mineria_como_otros = await consultar(m6, sector="OTROS", empresa_id=str(m6.minera.id))
    comprobar_error(de_mineria_como_otros, 400, "COMPANY_SECTOR_MISMATCH")
    sin_proveedores_ni_busqueda(m6)

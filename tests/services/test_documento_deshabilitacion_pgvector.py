"""Deshabilitación de documentos (ACT-02) sobre PostgreSQL + pgvector REALES: el filtro por documento se aplica en SQL ANTES de ordenar por
similitud y limitar, de modo que los fragmentos de un documento retirado (que conserva sus vectores) no ocupan los primeros lugares.

REAL: la base transaccional aislada (deshabilitación con su protección del origen), pgvector en un contenedor desechable (esquema de
`app/migraciones/vectorial`, vista `fragmentos_consultables`, distancia coseno) y `responder_consulta` de punta a punta. CONTROLADOS: los
proveedores de embeddings y de chat (sin red, sin OCI ni DeepSeek).

Los vectores se eligen para que el retirado sea SIEMPRE el más parecido a la consulta (coseno 1,0); el reemplazo y el reporte vigente tienen
menor similitud. Con `limite = 4` y 6 fragmentos del retirado, una búsqueda sin el filtro previo devolvería solo fragmentos del retirado.
Si Docker o la imagen local de pgvector no están disponibles estas pruebas se OMITEN (`pytest -rs`) y NO cuentan como ejecutadas.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio

from app.models import TipoDocumento
from app.modules.asistente import service as servicio_chat
from app.modules.ingesta.application import documento_service as servicio
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
)
from tests.services.test_asistente import afirmacion, respuesta
from tests.services.test_asistente_postgres import LlmControlado
from tests.services.test_asistente_pgvector import EmbeddingsControlado
from tests.services.test_fragmentos_vectoriales import IDENTIDAD, eje, guardar, lote, publicar, sintetico

AMBIENTE = "development"
MEMORIA, REPORTE = TipoDocumento.MEMORIA_ANUAL, TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
CONSULTA = eje(0)
TEXTO_RETIRADO = "Documento retirado: la empresa redujo el consumo de agua en 12 % durante el periodo"
TEXTO_REEMPLAZO = "Memoria corregida: la empresa redujo el consumo de agua en 12 % tras la corrección"
TEXTO_REPORTE = "Reporte vigente: la empresa gestionó el agua con indicadores propios"


async def indexar(fabrica_vectorial, documento, empresa, vector, texto: str, indice: int = 0) -> None:
    await guardar(
        fabrica_vectorial, lote([sintetico(indice, texto)], [vector]), ambiente=AMBIENTE, documento_id=documento.id, empresa_id=empresa.id,
        anio=documento.anio, tipo=documento.tipo, sector=empresa.sector,
    )
    assert await publicar(fabrica_vectorial, ambiente=AMBIENTE, documento=documento.id) == 1


@pytest_asyncio.fixture
async def mundo(fabrica_pg, fabrica_vectorial, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        rival = await crear_empresa(db, "Minera Rival")
        retirado = await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, tipo=MEMORIA, nombre="memoria-equivocada.md")
        reporte = await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, tipo=REPORTE, nombre="reporte-2025.md")
    for n in range(6):  # el retirado: coseno 1,0 con la consulta en los seis fragmentos
        await indexar(fabrica_vectorial, retirado, empresa, CONSULTA, f"{TEXTO_RETIRADO} ({n})", indice=n)
    async with fabrica_pg() as db:
        await servicio.deshabilitar_documento(db, retirado.id, ambiente=AMBIENTE, usuario_id=superadmin.id, motivo="Se cargó equivocada.")
    async with fabrica_pg() as db:
        reemplazo = await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, tipo=MEMORIA, nombre="memoria-corregida.md")
        ajeno = await sembrar_documento(db, rival, admin, texto=MD_GRI_204_305, tipo=MEMORIA, nombre="rival.md")
    await indexar(fabrica_vectorial, reemplazo, empresa, eje(0, {1: 1.0}), TEXTO_REEMPLAZO)  # coseno ≈ 0,71
    await indexar(fabrica_vectorial, reporte, empresa, eje(0, {2: 2.0}), TEXTO_REPORTE)  # coseno ≈ 0,45
    await indexar(fabrica_vectorial, ajeno, rival, CONSULTA, "Otra empresa: redujo el consumo de agua en 12 %")  # coseno 1,0, ajeno
    return SimpleNamespace(
        fabrica_pg=fabrica_pg, fabrica_vectorial=fabrica_vectorial, empresa=empresa, rival=rival, retirado=retirado, reemplazo=reemplazo,
        reporte=reporte, ajeno=ajeno, embeddings=EmbeddingsControlado(), llm=LlmControlado(),
    )


async def buscar(mundo, **extra):
    async with mundo.fabrica_vectorial() as db:
        return await vectores.buscar_similares(
            db, ambiente=AMBIENTE, vector_consulta=CONSULTA, identidad=IDENTIDAD, empresa_ids=[mundo.empresa.id], limite=4, anio=2025, **extra
        )


async def test_sin_el_filtro_por_documento_los_cuatro_primeros_son_del_retirado(mundo):
    """Control: así era antes. Los 4 primeros lugares son del retirado y el reemplazo queda fuera del límite."""
    encontrados = await buscar(mundo)

    assert {f.documento_id for f in encontrados} == {mundo.retirado.id}
    assert len(encontrados) == 4


async def test_con_documento_ids_el_filtro_se_aplica_antes_de_ordenar_y_limitar(mundo):
    encontrados = await buscar(mundo, documento_ids=[mundo.reemplazo.id, mundo.reporte.id])

    assert [f.documento_id for f in encontrados] == [mundo.reemplazo.id, mundo.reporte.id]  # por similitud: el reemplazo primero
    assert encontrados[0].texto_literal == TEXTO_REEMPLAZO
    assert encontrados[0].similitud > encontrados[1].similitud
    assert all(f.empresa_id == mundo.empresa.id for f in encontrados)  # el filtro por documento se SUMA al de empresa, no lo reemplaza
    # Un documento de otra empresa listado en `documento_ids` tampoco entra: el filtro por empresa sigue vigente.
    assert {f.documento_id for f in await buscar(mundo, documento_ids=[mundo.ajeno.id, mundo.reemplazo.id])} == {mundo.reemplazo.id}
    assert await buscar(mundo, documento_ids=[]) == []


async def test_el_chat_recupera_la_evidencia_del_reemplazo_con_pgvector_real(mundo):
    mundo.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    async with mundo.fabrica_pg() as db, mundo.fabrica_vectorial() as db_vectorial:
        resultado = await servicio_chat.responder_consulta(
            db, db_vectorial, pregunta="¿Cómo evolucionó el consumo de agua?", ambiente=AMBIENTE, proveedor_embeddings=mundo.embeddings,
            proveedor_llm=mundo.llm, sector=mundo.empresa.sector, empresa_id=mundo.empresa.id, anio=2025,
        )

    assert resultado.sin_evidencia is False
    assert [c.documento_id for c in resultado.citas] == [mundo.reemplazo.id]
    assert TEXTO_REEMPLAZO in mundo.llm.usuario
    assert TEXTO_REPORTE in mundo.llm.usuario
    assert TEXTO_RETIRADO not in mundo.llm.usuario
    assert "Otra empresa" not in mundo.llm.usuario

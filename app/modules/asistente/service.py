"""Casos de uso del Asistente IA — RAG (Módulo 6, CU004). Orquesta la recuperación y la respuesta.

Flujo (RF-023/024 · RN-033..037):
  pregunta → embedding de CONSULTA (SEARCH_QUERY) → `buscar_similares` (pgvector, k)
  → filtrar por `documentos_recuperables` (solo COMPLETADO con análisis y empresa activa)
  → prompt con contexto numerado → LLM → post-verificación de citas → respuesta con citas.

Si el corpus no tiene evidencia, se declara explícitamente (RN-035); nunca se inventa (RN-033).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documento_ingesta import Documento
from app.models import Empresa
from app.modules.asistente.domain.citas import procesar_citas
from app.modules.asistente.domain.prompt import SISTEMA, construir_prompt_usuario, validar_pregunta
from app.modules.asistente.infrastructure.llm import ProveedorLlm
from app.modules.asistente.schemas import Cita, ConsultaResponse
from app.modules.ingesta.application.documento_service import documentos_recuperables
from app.modules.ingesta.domain.embeddings import ProveedorEmbeddingsConsulta, embeber_consulta
from app.modules.ingesta.domain.parametros import PARAMETROS_INGESTA
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import (
    FragmentoRecuperado,
    buscar_similares,
)

MENSAJE_SIN_EVIDENCIA = (
    "No tengo evidencia en el corpus indexado para responder esa pregunta. "
    "Puedes reformularla o verificar que la empresa y el año tengan documentos analizados."
)
LIMITES_POR_DEFECTO = 4  # k de recuperación (RAG simple)


async def _empresa_ids(db: AsyncSession, empresa_id: uuid.UUID | None) -> list[uuid.UUID]:
    """Empresas donde buscar: la indicada, o todas las ACTIVAS si no se filtra."""
    if empresa_id is not None:
        return [empresa_id]
    filas = (await db.execute(select(Empresa.id).where(Empresa.activa.is_(True)))).scalars().all()
    return list(filas)


async def responder_consulta(
    db: AsyncSession,
    db_vectorial: AsyncSession,
    *,
    pregunta: str,
    ambiente: str,
    proveedor_embeddings: ProveedorEmbeddingsConsulta,
    proveedor_llm: ProveedorLlm,
    empresa_id: uuid.UUID | None = None,
    anio: int | None = None,
    limite: int = LIMITES_POR_DEFECTO,
) -> ConsultaResponse:
    """Responde una consulta con citas verificadas contra el corpus."""
    validar_pregunta(pregunta)

    empresas = await _empresa_ids(db, empresa_id)
    if not empresas:
        return ConsultaResponse(respuesta="", citas=[], sin_evidencia=True, mensaje=MENSAJE_SIN_EVIDENCIA)

    # 1) embedding de la consulta (asimétrico: SEARCH_QUERY)
    vector = await embeber_consulta(
        pregunta,
        proveedor_embeddings,
        timeout_segundos=PARAMETROS_INGESTA.timeout_embeddings_segundos,
    )

    # 2) recuperación vectorial (exacta por coseno)
    candidatos = await buscar_similares(
        db_vectorial,
        ambiente=ambiente,
        vector_consulta=vector,
        identidad=proveedor_embeddings.identidad,
        empresa_ids=empresas,
        limite=limite,
        anio=anio,
    )

    # 3) visibilidad transaccional: solo documentos COMPLETADOS con análisis y empresa activa
    ids = {f.documento_id for f in candidatos}
    recuperables = await documentos_recuperables(db, ambiente, ids) if ids else set()
    fragmentos = [f for f in candidatos if f.documento_id in recuperables]

    if not fragmentos:
        return ConsultaResponse(respuesta="", citas=[], sin_evidencia=True, mensaje=MENSAJE_SIN_EVIDENCIA)

    # 4) prompt + LLM
    prompt_usuario = construir_prompt_usuario(pregunta, fragmentos)
    desde_llm = await proveedor_llm.completar(SISTEMA, prompt_usuario)

    # 5) post-verificación de citas (RN-034)
    texto_limpio, citados = procesar_citas(desde_llm, fragmentos)
    nombres = await _nombres_documentos(db, {f.documento_id for f in citados})
    citas = [_a_cita(f, nombres.get(f.documento_id, "")) for f in citados]
    return ConsultaResponse(respuesta=texto_limpio, citas=citas, sin_evidencia=False)


async def _nombres_documentos(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    filas = (await db.execute(select(Documento.id, Documento.nombre_archivo).where(Documento.id.in_(ids)))).all()
    return {fila.id: fila.nombre_archivo for fila in filas}


def _a_cita(f: FragmentoRecuperado, documento_nombre: str) -> Cita:
    extracto = f.texto_literal.strip()
    if len(extracto) > 300:
        extracto = extracto[:297] + "…"
    return Cita(
        documento_id=f.documento_id,
        documento_nombre=documento_nombre,
        seccion=f.seccion or "(sin sección)",
        anio=f.anio,
        empresa_id=f.empresa_id,
        extracto=extracto,
    )

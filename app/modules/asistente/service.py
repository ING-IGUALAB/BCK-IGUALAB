"""Casos de uso del Asistente IA — RAG (Módulo 6, CU004). Orquesta la recuperación y la respuesta.

Flujo (RF-023/024 · RN-033..037 · RNF-010):
  sector + empresa + año + pregunta
  → la empresa existe, está activa y es del sector (en la base; no se confía en el frontend)
  → embedding de CONSULTA (SEARCH_QUERY) → `buscar_similares` (pgvector) limitado a ambiente, empresa, año y sector
  → filtrar por `documentos_recuperables` (solo COMPLETADO con análisis y empresa activa)
  → prompt con procedencia (empresa, documento, año, sección) y contenido como DATO → LLM
  → verificación afirmación por afirmación (`domain/citas.py`): referencia válida y cita literal del fragmento citado.

Si ninguna afirmación se puede respaldar, se declara explícitamente que no hay evidencia (RN-035) y se omite todo texto del modelo;
nunca se borra un marcador para conservar una afirmación sin fuente (RN-033/034). Ver los límites en `domain/citas.py`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessValidationError
from app.models import Empresa, SectorEmpresa
from app.models.documento_ingesta import Documento
from app.modules.asistente.domain.citas import (
    MOTIVO_AFIRMACIONES_NO_VERIFICABLES,
    MOTIVO_FUERA_DE_DOMINIO,
    MOTIVO_MODELO_SIN_EVIDENCIA,
    MOTIVO_RESPUESTA_NO_INTERPRETABLE,
    MOTIVO_SIN_FRAGMENTOS,
    verificar_respuesta,
)
from app.modules.asistente.domain.prompt import SISTEMA, construir_prompt_usuario, nuevo_codigo_de_marcador, validar_pregunta
from app.modules.asistente.infrastructure.llm import ProveedorLlm
from app.modules.asistente.schemas import Afirmacion, Cita, ConsultaResponse
from app.modules.empresas import service as empresa_service
from app.modules.ingesta.application.documento_service import documentos_recuperables
from app.modules.ingesta.domain.embeddings import ProveedorEmbeddingsConsulta, embeber_consulta
from app.modules.ingesta.domain.parametros import PARAMETROS_INGESTA
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import (
    FragmentoRecuperado,
    buscar_similares,
)

LIMITES_POR_DEFECTO = 4  # k de recuperación (RAG simple)

_MENSAJES = {
    MOTIVO_SIN_FRAGMENTOS: (
        "No hay documentos publicados de esa empresa, sector y año que permitan responder. "
        "Verifique la selección o que el documento esté ingerido y publicado."
    ),
    MOTIVO_MODELO_SIN_EVIDENCIA: (
        "No se encontró evidencia en los documentos de la empresa y el año seleccionados para responder esa pregunta. "
        "Puede reformularla."
    ),
    MOTIVO_AFIRMACIONES_NO_VERIFICABLES: (
        "El asistente no pudo respaldar ninguna afirmación con una cita verificable de los documentos, así que no se muestra "
        "ninguna respuesta. Puede reformular la pregunta."
    ),
    MOTIVO_RESPUESTA_NO_INTERPRETABLE: (
        "La respuesta del asistente no tuvo el formato verificable esperado, así que no se muestra. Intente de nuevo."
    ),
    MOTIVO_FUERA_DE_DOMINIO: (
        "La consulta está fuera del dominio del asistente (sostenibilidad empresarial, indicadores GRI, sanciones o el contenido "
        "de los documentos ingeridos)."
    ),
}


def _sin_evidencia(motivo: str, descartadas: int = 0) -> ConsultaResponse:
    return ConsultaResponse(
        respuesta="", sin_evidencia=True, motivo=motivo, mensaje=_MENSAJES[motivo], afirmaciones_descartadas=descartadas
    )


async def _empresa_consultable(db: AsyncSession, empresa_id: uuid.UUID, sector: SectorEmpresa) -> Empresa:
    """La empresa existe (404), está habilitada (400) y pertenece al sector seleccionado (400): se comprueba en la base."""
    empresa = await empresa_service.obtener_empresa(db, empresa_id)
    if not empresa.activa:
        raise BusinessValidationError("COMPANY_INACTIVE", "La empresa está inactiva; no se puede consultar sobre ella.")
    if empresa.sector != sector:
        raise BusinessValidationError(
            "COMPANY_SECTOR_MISMATCH", "El sector indicado no corresponde a la empresa seleccionada."
        )
    return empresa


async def responder_consulta(
    db: AsyncSession,
    db_vectorial: AsyncSession,
    *,
    pregunta: str,
    ambiente: str,
    proveedor_embeddings: ProveedorEmbeddingsConsulta,
    proveedor_llm: ProveedorLlm,
    sector: SectorEmpresa,
    empresa_id: uuid.UUID,
    anio: int,
    limite: int = LIMITES_POR_DEFECTO,
) -> ConsultaResponse:
    """Responde una consulta limitada a un ambiente, sector, empresa y año, con afirmaciones respaldadas por fuentes verificables."""
    try:
        validar_pregunta(pregunta)
    except ValueError as exc:
        # El dominio lanza ValueError genérico; en la frontera HTTP eso sería un 500.
        raise BusinessValidationError("EMPTY_QUESTION", str(exc)) from None

    empresa = await _empresa_consultable(db, empresa_id, sector)
    empresa_nombre, empresa_sector = empresa.nombre, empresa.sector
    await db.rollback()  # fin de la lectura: no se mantiene una transacción abierta durante las llamadas a proveedores

    # 1) embedding de la consulta (asimétrico: SEARCH_QUERY)
    vector = await embeber_consulta(
        pregunta,
        proveedor_embeddings,
        timeout_segundos=PARAMETROS_INGESTA.timeout_embeddings_segundos,
    )

    # 2) recuperación vectorial (exacta por coseno), SIEMPRE limitada a ambiente, empresa, año y sector
    candidatos = await buscar_similares(
        db_vectorial,
        ambiente=ambiente,
        vector_consulta=vector,
        identidad=proveedor_embeddings.identidad,
        empresa_ids=[empresa_id],
        limite=limite,
        anio=anio,
        sector=empresa_sector,
    )

    # 3) visibilidad transaccional: solo documentos COMPLETADOS con análisis y empresa activa. Defensa en profundidad: aunque la
    #    búsqueda se equivocara, un fragmento de otra empresa o año no entra al contexto.
    ids = {f.documento_id for f in candidatos}
    recuperables = await documentos_recuperables(db, ambiente=ambiente, documento_ids=ids) if ids else set()
    fragmentos = [
        f for f in candidatos if f.documento_id in recuperables and f.empresa_id == empresa_id and f.anio == anio
    ]
    if not fragmentos:
        return _sin_evidencia(MOTIVO_SIN_FRAGMENTOS)

    nombres = await _nombres_documentos(db, {f.documento_id for f in fragmentos})
    await db.rollback()

    # 4) prompt (con procedencia) + LLM
    prompt_usuario = construir_prompt_usuario(
        pregunta, fragmentos, empresa_nombre=empresa_nombre, nombres_documentos=nombres, codigo=nuevo_codigo_de_marcador()
    )
    desde_llm = await proveedor_llm.completar(SISTEMA, prompt_usuario)

    # 5) verificación de las afirmaciones (RN-033/034/035)
    resultado = verificar_respuesta(desde_llm, fragmentos)
    if resultado.motivo is not None:
        return _sin_evidencia(resultado.motivo, resultado.descartadas)

    usadas = sorted({n for a in resultado.afirmaciones for n in a.referencias})
    citas = [
        _a_cita(n, fragmentos[n - 1], nombres.get(fragmentos[n - 1].documento_id, ""), empresa_nombre) for n in usadas
    ]
    afirmaciones = [Afirmacion(texto=a.texto, referencias=list(a.referencias), cita_textual=a.cita_textual) for a in resultado.afirmaciones]
    texto = "\n".join(f"{a.texto} " + "".join(f"[{n}]" for n in a.referencias) for a in resultado.afirmaciones)
    return ConsultaResponse(
        respuesta=texto,
        afirmaciones=afirmaciones,
        citas=citas,
        sin_evidencia=False,
        afirmaciones_descartadas=resultado.descartadas,
    )


async def _nombres_documentos(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    filas = (await db.execute(select(Documento.id, Documento.nombre_archivo).where(Documento.id.in_(ids)))).all()
    return {fila.id: fila.nombre_archivo for fila in filas}


def _a_cita(referencia: int, f: FragmentoRecuperado, documento_nombre: str, empresa_nombre: str) -> Cita:
    extracto = f.texto_literal.strip()
    if len(extracto) > 300:
        extracto = extracto[:297] + "…"
    return Cita(
        referencia=referencia,
        documento_id=f.documento_id,
        documento_nombre=documento_nombre,
        empresa_id=f.empresa_id,
        empresa_nombre=empresa_nombre,
        seccion=f.seccion or "(sin sección)",
        anio=f.anio,
        extracto=extracto,
    )

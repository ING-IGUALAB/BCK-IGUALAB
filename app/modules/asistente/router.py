"""Asistente IA — RAG (Módulo 6, CU004) por HTTP. EXCLUSIVO del rol Administrador
(`AdministradorUser`: sin sesión 401, SuperAdmin 403).

El router solo coordina: la lógica está en `app.modules.asistente.service`. Reutiliza la
recuperación vectorial (`buscar_similares`) y la visibilidad transaccional (`documentos_recuperables`).
Requiere embeddings (OCI) y chat (DeepSeek) configurados; si falta alguno responde 503.

CONTRATO (LTX:RN-037): la consulta exige `sector`, `empresa_id` y `anio`; el backend comprueba en la base que la empresa existe, está
activa y es del sector, y limita la recuperación a ese ambiente, empresa, año y sector. No hay búsqueda global.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database_vectorial import get_vector_db
from app.core.dependencies import AdministradorUser, DatabaseSession
from app.core.exceptions import ServiceUnavailableError
from app.modules.asistente.infrastructure.llm import ProveedorLlm, construir_llm_desde_config, llm_configurado
from app.modules.asistente.schemas import ConsultaRequest, ConsultaResponse
from app.modules.asistente.service import responder_consulta
from app.modules.ingesta.domain.embeddings import ProveedorEmbeddingsConsulta
from app.modules.ingesta.domain.parametros import PARAMETROS_INGESTA, ambiente_desde_app_env

router = APIRouter(prefix="/asistente", tags=["Asistente IA (RAG)"])

VectorDatabaseSession = Annotated[AsyncSession, Depends(get_vector_db)]


def esquema_vectorial_listo(request: Request) -> None:
    """El asistente responde SOBRE el corpus indexado: sin el esquema vectorial preparado
    (migraciones fallaron o nunca corrieron) la consulta fallaría con un 500 opaco. Responde
    503 con el MISMO motivo que la ingesta (`app.state.ingesta_error`, fijado en el lifespan)."""
    if getattr(request.app.state, "ingesta", None) is not None:
        return
    codigo, mensaje, detalles = getattr(request.app.state, "ingesta_error", None) or (
        "INGESTION_NOT_CONFIGURED",
        "El asistente no está disponible: el esquema de la base vectorial no está preparado.",
        None,
    )
    raise ServiceUnavailableError(codigo, mensaje, details=detalles)


def obtener_ambiente() -> str:
    """Ambiente de esta aplicación (`APP_ENV`); toda recuperación se limita a él."""
    try:
        return ambiente_desde_app_env(settings.APP_ENV)
    except ValueError:
        raise ServiceUnavailableError(
            "ANALYSIS_ENVIRONMENT_INVALID",
            "El asistente no está disponible: APP_ENV no es un ambiente válido.",
        ) from None


def get_proveedor_llm() -> ProveedorLlm:
    if not llm_configurado():
        raise ServiceUnavailableError(
            "LLM_NOT_CONFIGURED",
            "El asistente no está disponible: falta configurar el proveedor de IA (DEEPSEEK_API_KEY).",
        )
    return construir_llm_desde_config()


def get_proveedor_embeddings() -> ProveedorEmbeddingsConsulta:
    try:
        from app.modules.ingesta.infrastructure.proveedor_oci import ProveedorEmbeddingsOCI

        return ProveedorEmbeddingsOCI(PARAMETROS_INGESTA)
    except Exception as exc:  # OCI sin configurar / credenciales inválidas
        raise ServiceUnavailableError(
            "EMBEDDINGS_NOT_CONFIGURED",
            "El asistente no está disponible: falta configurar el proveedor de embeddings (OCI).",
        ) from exc


@router.post(
    "/consultas",
    response_model=ConsultaResponse,
    summary="Consultar al asistente IA sobre el corpus indexado (RAG con afirmaciones respaldadas)",
    description=(
        "Exige sector, empresa y año. Solo devuelve afirmaciones con referencia válida a un fragmento entregado al modelo y con una "
        "cita literal presente en ese fragmento; las que no se pueden respaldar se omiten y, si no queda ninguna, `sin_evidencia` "
        "es verdadero con su `motivo`. Verificar la cita literal no prueba que el pasaje implique la afirmación."
    ),
    responses={
        400: {"description": "Pregunta vacía, empresa inactiva (`COMPANY_INACTIVE`) o de otro sector (`COMPANY_SECTOR_MISMATCH`)."},
        401: {"description": "No autenticado o sesión inválida."},
        403: {"description": "Requiere rol Administrador."},
        404: {"description": "La empresa no existe (`COMPANY_NOT_FOUND`)."},
        422: {"description": "Falta o es inválido `sector`, `empresa_id` o `anio` (`REQUEST_VALIDATION_ERROR`)."},
        502: {"description": "Falló el proveedor de embeddings o de chat."},
        503: {"description": "Servicio no disponible por configuración."},
        504: {"description": "El proveedor de embeddings no respondió a tiempo."},
    },
)
async def consultar(
    payload: ConsultaRequest,
    # 401/403 ANTES que 502/503 (esquema/OCI/LLM): la autenticación tiene prioridad.
    _usuario: AdministradorUser,
    db: DatabaseSession,
    _esquema: Annotated[None, Depends(esquema_vectorial_listo)],
    db_vectorial: VectorDatabaseSession,
    ambiente: Annotated[str, Depends(obtener_ambiente)],
    proveedor_embeddings: Annotated[ProveedorEmbeddingsConsulta, Depends(get_proveedor_embeddings)],
    proveedor_llm: Annotated[ProveedorLlm, Depends(get_proveedor_llm)],
) -> ConsultaResponse:
    return await responder_consulta(
        db,
        db_vectorial,
        pregunta=payload.pregunta,
        ambiente=ambiente,
        proveedor_embeddings=proveedor_embeddings,
        proveedor_llm=proveedor_llm,
        sector=payload.sector,
        empresa_id=payload.empresa_id,
        anio=payload.anio,
    )

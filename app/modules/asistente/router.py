"""Asistente IA — RAG (Módulo 6, CU004) por HTTP. EXCLUSIVO del rol Administrador
(`AdministradorUser`: sin sesión 401, SuperAdmin 403).

El router solo coordina: la lógica está en `app.modules.asistente.service`. Reutiliza la
recuperación vectorial (`buscar_similares`) y la visibilidad transaccional (`documentos_recuperables`).
Requiere embeddings (OCI) y chat (DeepSeek) configurados; si falta alguno responde 503.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
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
    summary="Consultar al asistente IA sobre el corpus indexado (RAG con citas)",
    responses={
        401: {"description": "No autenticado o sesión inválida."},
        403: {"description": "Requiere rol Administrador."},
        503: {"description": "Servicio no disponible por configuración."},
    },
)
async def consultar(
    payload: ConsultaRequest,
    db: DatabaseSession,
    db_vectorial: VectorDatabaseSession,
    ambiente: Annotated[str, Depends(obtener_ambiente)],
    _usuario: AdministradorUser,
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
        empresa_id=payload.empresa_id,
        anio=payload.anio,
    )

"""Asistente IA — RAG (Módulo 6, CU004) por HTTP. EXCLUSIVO del rol Administrador
(`AdministradorUser`: sin sesión 401, SuperAdmin 403).

El router solo coordina: la lógica está en `app.modules.asistente.service`. Reutiliza la
recuperación vectorial (`buscar_similares`) y la visibilidad transaccional (`documentos_recuperables`).
La búsqueda documental requiere embeddings (OCI), chat (DeepSeek) y el esquema vectorial; si falta alguno responde 503/502, EXCEPTO
una pregunta inequívoca de solo estado GRI, que se responde desde Análisis GRI sin ellos (se preparan solo si hacen falta).

CONTRATO (LTX:RN-037): la consulta exige `sector`, `empresa_id` y `anio`; el backend comprueba en la base que la empresa existe, está
activa y es del sector, y limita la recuperación a ese ambiente, empresa, año y sector. No hay búsqueda global.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.config import settings
from app.core.dependencies import AdministradorUser, DatabaseSession
from app.core.exceptions import ServiceUnavailableError
from app.modules.asistente.continuacion_service import RecursosRag
from app.modules.asistente.infrastructure.llm import ProveedorLlm, construir_llm_desde_config, llm_configurado
from app.modules.asistente.schemas import ConsultaRequest, ConsultaResponse
from app.modules.asistente.service import consultar_directa
from app.modules.ingesta.domain.embeddings import ProveedorEmbeddingsConsulta
from app.modules.ingesta.domain.parametros import PARAMETROS_INGESTA, ambiente_desde_app_env

router = APIRouter(prefix="/asistente", tags=["Asistente IA (RAG)"])


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


def get_recursos_consulta(request: Request) -> RecursosRag:
    """Recursos DIFERIDOS del RAG para `POST /asistente/consultas`: son los MISMOS de la continuación (`continuacion_router.get_recursos_rag`).
    Crearlos no valida, construye, conecta ni abre nada; el servicio los prepara solo si la pregunta necesita la búsqueda documental. Una
    pregunta de solo estado GRI se responde sin ellos. (Importación perezosa: `continuacion_router` importa este módulo.)"""
    from app.modules.asistente.continuacion_router import get_recursos_rag

    return get_recursos_rag(request)


@router.post(
    "/consultas",
    summary="Consultar al asistente IA sobre el corpus indexado (RAG con afirmaciones respaldadas)",
    description=(
        "Exige sector, empresa y año. Solo devuelve afirmaciones con referencia válida a un fragmento entregado al modelo y con una "
        "cita literal presente en ese fragmento; las que no se pueden respaldar se omiten y, si no queda ninguna, `sin_evidencia` "
        "es verdadero con su `motivo`. Verificar la cita literal no prueba que el pasaje implique la afirmación. Si la pregunta nombra un código "
        "GRI explícito y pide su estado, `estado_registrado` trae el estado registrado en Análisis GRI (nunca una cita). Una pregunta "
        "INEQUÍVOCA de solo estado («¿Qué estado tiene GRI 1?») se responde únicamente desde Análisis GRI, sin búsqueda documental ni "
        "modelo: `busqueda_documental` es NO_REALIZADA, `sin_evidencia` false y `citas` vacías."
    ),
    responses={
        400: {"description": "Pregunta vacía, empresa inactiva (`COMPANY_INACTIVE`) o de otro sector (`COMPANY_SECTOR_MISMATCH`)."},
        401: {"description": "No autenticado o sesión inválida."},
        403: {"description": "Requiere rol Administrador."},
        404: {"description": "La empresa no existe (`COMPANY_NOT_FOUND`)."},
        409: {
            "description": "`ANALYSIS_CHANGING`: solo en una pregunta de SOLO ESTADO GRI, el Análisis GRI está cambiando mientras se lee; vuelva a "
            "preguntar. (En una pregunta mixta no se devuelve este error: la respuesta documental se conserva.)"
        },
        422: {"description": "Falta o es inválido `sector`, `empresa_id` o `anio` (`REQUEST_VALIDATION_ERROR`)."},
        502: {"description": "Falló el proveedor de embeddings o de chat."},
        503: {"description": "Servicio no disponible por configuración."},
        504: {"description": "El proveedor de embeddings no respondió a tiempo."},
    },
)
async def consultar(
    payload: ConsultaRequest,
    # 401/403 ANTES que cualquier 502/503: la autenticación tiene prioridad. NADA vectorial ni de IA se declara aquí: se entregan como
    # recursos DIFERIDOS que el servicio prepara solo si la pregunta necesita la búsqueda documental (como en la continuación).
    _usuario: AdministradorUser,
    db: DatabaseSession,
    ambiente: Annotated[str, Depends(obtener_ambiente)],
    recursos: Annotated[RecursosRag, Depends(get_recursos_consulta)],
) -> ConsultaResponse:
    return await consultar_directa(
        db,
        recursos=recursos,
        pregunta=payload.pregunta,
        ambiente=ambiente,
        sector=payload.sector,
        empresa_id=payload.empresa_id,
        anio=payload.anio,
    )

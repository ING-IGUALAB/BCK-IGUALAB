"""Análisis y validación de brechas GRI (Módulo 5, CU005) por HTTP. EXCLUSIVO del rol Administrador
(`AdministradorUser`: sin sesión 401, SuperAdmin 403). El autor sale siempre de la sesión validada, nunca del payload.

Los routers solo coordinan HTTP; la lógica está en `app.modules.analisis_gri`. Trabaja solo con datos almacenados: no depende
del gestor de ingesta, ni de OCI, MinIO o la base vectorial. Contrato y ejemplos: `docs/ingesta/11-modulo5-analisis-gri.md`.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.config import settings
from app.core.dependencies import AdministradorUser, DatabaseSession
from app.core.exceptions import ServiceUnavailableError
from app.modules.analisis_gri.schemas import (
    PATRON_IDENTIDAD,
    AnalisisResponse,
    AsignarEstadoRequest,
    AsignarEstadoResponse,
    PaginaHistorial,
    ValidarAnalisisRequest,
    ValidarAnalisisResponse,
)
from app.modules.analisis_gri.application import servicio
from app.modules.ingesta.domain.parametros import ambiente_desde_app_env

router = APIRouter(prefix="/analisis-gri", tags=["Análisis y Validación GRI"])


def obtener_ambiente() -> str:
    """Ambiente de esta aplicación (`APP_ENV`): toda consulta y escritura se limita a él. No lo elige el cliente."""
    try:
        return ambiente_desde_app_env(settings.APP_ENV)
    except ValueError:
        raise ServiceUnavailableError(
            "ANALYSIS_ENVIRONMENT_INVALID", "El análisis GRI no está disponible: APP_ENV no es un ambiente válido."
        ) from None


def esquema_analisis_listo(request: Request) -> None:
    """503 si el arranque no pudo preparar el esquema del Módulo 5 (se declara DESPUÉS de la autenticación)."""
    error = getattr(request.app.state, "analisis_error", None)
    if error is not None:
        codigo, mensaje, detalles = error
        raise ServiceUnavailableError(codigo, mensaje, details=detalles)


Ambiente = Annotated[str, Depends(obtener_ambiente)]
EsquemaListo = Depends(esquema_analisis_listo)

_ERRORES = {
    401: {"description": "Sin sesión válida."},
    403: {"description": "Rol sin permiso (solo Administrador)."},
    503: {"description": "Esquema del módulo no preparado o ambiente inválido."},
}
EmpresaId = Annotated[uuid.UUID, Path(description="Empresa existente.")]
Anio = Annotated[int, Path(ge=2000, le=9999, description="Año del análisis.")]
Identidad = Annotated[
    str,
    Path(
        pattern=PATRON_IDENTIDAD,
        description="Identidad de la fila (campo `identidad`): `305:2016`, `102:ambigua` o `102:no-catalogada-2030`.",
    ),
]
_BASE = "/empresas/{empresa_id}/anios/{anio}"


@router.get(_BASE, response_model=AnalisisResponse, responses={**_ERRORES, 404: {"description": "Empresa inexistente."}})
async def consultar_analisis(
    empresa_id: EmpresaId, anio: Anio, actor: AdministradorUser, db: DatabaseSession, ambiente: Ambiente, _: None = EsquemaListo
):
    """Filas GRI (una por código y edición) con su evidencia, sanciones, advertencias, puntaje ESG, pendientes, versión y estado
    de la validación. Solo documentos elegibles del ambiente: COMPLETADOS, con análisis y publicación vectorial confirmada, de una
    empresa activa."""
    return await servicio.consultar_analisis(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)


@router.put(
    _BASE + "/codigos/{identidad}/estado",
    response_model=AsignarEstadoResponse,
    responses={
        **_ERRORES,
        404: {"description": "Empresa o identidad GRI inexistente en el análisis."},
        409: {"description": "La fila cambió desde que se consultó (`ANALYSIS_ROW_VERSION_CONFLICT`)."},
        422: {"description": "Estado fuera de OK / BAJA_SUSTANCIA / SUB_REPORTADO, o cuerpo inválido."},
    },
)
async def asignar_estado(
    empresa_id: EmpresaId,
    anio: Anio,
    identidad: Identidad,
    cuerpo: AsignarEstadoRequest,
    actor: AdministradorUser,
    db: DatabaseSession,
    ambiente: Ambiente,
    _: None = EsquemaListo,
):
    """Asigna o cambia el estado manual de una fila y registra el historial (autor = sesión, fecha UTC, estado anterior → nuevo)
    en la misma transacción. Un cambio deja sin vigencia la validación anterior."""
    return await servicio.asignar_estado(
        db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, identidad=identidad, estado=cuerpo.estado,
        version_fila_esperada=cuerpo.version_fila_esperada, actor=actor,
    )


@router.get(_BASE + "/historial", response_model=PaginaHistorial, responses={**_ERRORES, 404: {}})
async def consultar_historial(
    empresa_id: EmpresaId,
    anio: Anio,
    actor: AdministradorUser,
    db: DatabaseSession,
    ambiente: Ambiente,
    _: None = EsquemaListo,
    identidad: Annotated[str | None, Query(pattern=PATRON_IDENTIDAD, description="Solo los cambios de esta identidad.")] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=servicio.TAMANO_MAXIMO_PAGINA)] = 20,
):
    """Cambios de estado (solo inserción), recientes primero."""
    return await servicio.consultar_historial(
        db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, identidad=identidad, pagina=pagina, tamano=tamano
    )


@router.post(
    _BASE + "/validacion",
    response_model=ValidarAnalisisResponse,
    status_code=201,
    responses={
        **_ERRORES,
        404: {"description": "Empresa inexistente."},
        409: {
            "description": "No procede: `ANALYSIS_VERSION_CONFLICT`, `ANALYSIS_NO_ELIGIBLE_DOCUMENTS`, "
            "`ANALYSIS_NO_GRI_CODES`, `ANALYSIS_PENDING_STATES` o `ANALYSIS_ALREADY_VALIDATED`."
        },
    },
)
async def validar_analisis(
    empresa_id: EmpresaId,
    anio: Anio,
    cuerpo: ValidarAnalisisRequest,
    actor: AdministradorUser,
    db: DatabaseSession,
    ambiente: Ambiente,
    _: None = EsquemaListo,
):
    """«Validar análisis»: confirmación explícita. Solo procede con documentos elegibles, filas GRI y TODAS las filas con estado, y
    sobre la `version` y la `huella_documentos` consultadas. Registra quién, cuándo y sobre qué versión y documentos. No genera el
    reporte de prospección."""
    return await servicio.validar_analisis(
        db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, version_esperada=cuerpo.version,
        huella_esperada=cuerpo.huella_documentos, actor=actor,
    )

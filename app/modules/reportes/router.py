"""Reportes de Prospección (Módulo 7, CU006) por HTTP. EXCLUSIVO del rol Administrador
(`AdministradorUser`). El router solo coordina; la lógica está en `app.modules.reportes.service`.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.core.config import settings
from app.core.dependencies import AdministradorUser, DatabaseSession
from app.core.exceptions import ServiceUnavailableError

from app.modules.ingesta.domain.parametros import ambiente_desde_app_env
from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf
from app.modules.reportes.schemas import DisponibilidadReportesResponse, GenerarReporteRequest, ReporteResumen
from app.modules.reportes.service import (
    disponibilidad_de_reportes,
    generar_reporte,
    listar_reportes,
    obtener_pdf,
    vista_previa,
)

router = APIRouter(prefix="/reportes", tags=["Reportes de Prospección"])


_RESPUESTAS_COMUNES = {
    401: {"description": "No autenticado o sesión inválida."},
    403: {"description": "Requiere rol Administrador."},
    503: {"description": "Servicio no disponible por configuración."},
}


def obtener_ambiente() -> str:
    try:
        return ambiente_desde_app_env(settings.APP_ENV)
    except ValueError:
        raise ServiceUnavailableError(
            "ANALYSIS_ENVIRONMENT_INVALID",
            "Los reportes no están disponibles: APP_ENV no es un ambiente válido.",
        ) from None


Ambiente = Annotated[str, Depends(obtener_ambiente)]


def obtener_compilador_pdf() -> Callable[[str], bytes]:
    """Motor HTML→PDF. Dependencia para poder sustituirlo en las pruebas HTTP sin WeasyPrint."""
    return compilar_html_a_pdf


CompiladorPdf = Annotated[Callable[[str], bytes], Depends(obtener_compilador_pdf)]

_ERRORES_DE_ANALISIS = {
    404: {"description": "Empresa inexistente (`COMPANY_NOT_FOUND`)."},
    409: {"description": "El análisis GRI no tiene una validación vigente (`ANALYSIS_NOT_VALIDATED`)."},
    502: {"description": "No se pudo compilar el PDF (`PDF_GENERATION_ERROR`)."},
}


@router.post(
    "/vista-previa",
    summary="Vista previa del reporte (PDF, sin guardar)",
    description="No crea, modifica ni sustituye ningún reporte, exista o no uno para la empresa y el año.",
    responses={
        **_RESPUESTAS_COMUNES,
        **_ERRORES_DE_ANALISIS,
        200: {"content": {"application/pdf": {}}, "description": "PDF del reporte generado."},
    },
)
async def generar_vista_previa(
    payload: GenerarReporteRequest,
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
    compilar: CompiladorPdf,
) -> Response:
    pdf = await vista_previa(db, ambiente=ambiente, empresa_id=payload.empresa_id, anio=payload.anio, compilar_pdf=compilar)
    return Response(content=pdf, media_type="application/pdf")


@router.post(
    "",
    status_code=201,
    summary="Generar reporte de prospección (PDF)",
    description=(
        "Genera el reporte de la empresa y el año a partir del análisis GRI validado. El reporte es INMUTABLE (LTX:RN-041): si ya "
        "existe uno para la misma empresa y año responde 409 `REPORT_ALREADY_EXISTS` (con `details.reporte_id`) y no cambia nada; "
        "no hay versiones, sustitución, eliminación ni regeneración."
    ),
    responses={
        **_RESPUESTAS_COMUNES,
        **_ERRORES_DE_ANALISIS,
        409: {"description": "`REPORT_ALREADY_EXISTS` (ya hay un reporte de la empresa y el año) o `ANALYSIS_NOT_VALIDATED`."},
    },
)
async def generar(
    payload: GenerarReporteRequest,
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
    compilar: CompiladorPdf,
) -> ReporteResumen:
    return await generar_reporte(
        db,
        ambiente=ambiente,
        empresa_id=payload.empresa_id,
        anio=payload.anio,
        usuario_id=usuario.id,
        compilar_pdf=compilar,
    )


@router.get(
    "",
    summary="Historial de reportes",
    responses=_RESPUESTAS_COMUNES,
)
async def listar(db: DatabaseSession, usuario: AdministradorUser) -> list[ReporteResumen]:
    return await listar_reportes(db)


@router.get(
    "/disponibilidad",
    summary="Años disponibles para generar el reporte de una empresa",
    description=(
        "Años con documentos elegibles (o con reporte ya generado) y su punto del flujo: `PENDIENTE_REVISION`, "
        "`PENDIENTE_VALIDACION`, `VALIDACION_VIGENTE`, `REPORTE_GENERADO` o `SIN_CODIGOS_GRI`. Con `anio` siempre responde ese año "
        "(`SIN_DOCUMENTACION_ELEGIBLE` si no hay documentos elegibles). Solo lectura e informativo: la generación vuelve a comprobar "
        "la validación vigente y responde 409 `REPORT_ALREADY_EXISTS` si ya hay un reporte."
    ),
    responses={**_RESPUESTAS_COMUNES, 404: {"description": "Empresa inexistente (`COMPANY_NOT_FOUND`)."}},
)
async def disponibilidad(
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
    empresa_id: uuid.UUID,
    anio: Annotated[int | None, Query(ge=2000, le=9999, description="Limita la respuesta a un año.")] = None,
) -> DisponibilidadReportesResponse:
    return await disponibilidad_de_reportes(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)


@router.get(
    "/{reporte_id}/archivo",
    summary="Descargar el PDF del reporte",
    description="Entrega el PDF almacenado al generarse (sin recompilar); su SHA-256 es el de `ReporteResumen.sha256`.",
    responses={
        **_RESPUESTAS_COMUNES,
        404: {"description": "El reporte no existe (`REPORT_NOT_FOUND`)."},
        200: {"content": {"application/pdf": {}}, "description": "PDF del reporte."},
    },
)
async def descargar(
    reporte_id: uuid.UUID,
    db: DatabaseSession,
    usuario: AdministradorUser,
) -> Response:
    pdf = await obtener_pdf(db, reporte_id, usuario_id=usuario.id)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="reporte_{reporte_id}.pdf"'},
    )

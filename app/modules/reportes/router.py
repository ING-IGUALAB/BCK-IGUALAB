"""Reportes de Prospección (Módulo 7, CU006) por HTTP. EXCLUSIVO del rol Administrador
(`AdministradorUser`). El router solo coordina; la lógica está en `app.modules.reportes.service`.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.core.config import settings
from app.core.dependencies import AdministradorUser, DatabaseSession
from app.core.exceptions import ServiceUnavailableError
from app.modules.ingesta.domain.parametros import ambiente_desde_app_env
from app.modules.reportes.schemas import GenerarReporteRequest, ReporteResumen
from app.modules.reportes.service import generar_reporte, listar_reportes, obtener_pdf, vista_previa

router = APIRouter(prefix="/reportes", tags=["Reportes de Prospección"])


def obtener_ambiente() -> str:
    try:
        return ambiente_desde_app_env(settings.APP_ENV)
    except ValueError:
        raise ServiceUnavailableError(
            "ANALYSIS_ENVIRONMENT_INVALID",
            "Los reportes no están disponibles: APP_ENV no es un ambiente válido.",
        ) from None


Ambiente = Annotated[str, Depends(obtener_ambiente)]


@router.post("/vista-previa", summary="Vista previa del reporte (PDF, sin guardar)")
async def generar_vista_previa(
    payload: GenerarReporteRequest,
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
) -> Response:
    pdf = await vista_previa(db, ambiente=ambiente, empresa_id=payload.empresa_id, anio=payload.anio)
    return Response(content=pdf, media_type="application/pdf")


@router.post("", response_model=ReporteResumen, status_code=201, summary="Generar reporte de prospección (PDF)")
async def generar(
    payload: GenerarReporteRequest,
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
) -> ReporteResumen:
    return await generar_reporte(
        db,
        ambiente=ambiente,
        empresa_id=payload.empresa_id,
        anio=payload.anio,
        usuario_id=usuario.id,
    )


@router.get("", response_model=list[ReporteResumen], summary="Historial de reportes")
async def listar(db: DatabaseSession, usuario: AdministradorUser) -> list[ReporteResumen]:
    return await listar_reportes(db)


@router.get("/{reporte_id}/archivo", summary="Descargar el PDF del reporte")
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

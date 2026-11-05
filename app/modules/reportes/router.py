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
from app.modules.reportes.schemas import (
    DisponibilidadReportesResponse,
    GenerarReporteRequest,
    ReporteEmision,
    ReporteResumen,
    ReporteVigente,
)
from app.modules.reportes.service import (
    disponibilidad_de_reportes,
    generar_reporte,
    listar_reportes,
    obtener_pdf,
    obtener_pdf_vigente,
    obtener_reporte_vigente,
    vista_previa,
)

router = APIRouter(prefix="/reportes", tags=["Reportes de Prospección"])
MEDIA_PDF = "application/pdf"


def _respuesta_pdf(descripcion: str) -> dict:
    """Respuesta 200 de OpenAPI para las rutas que entregan un PDF."""
    return {"content": {MEDIA_PDF: {}}, "description": descripcion}


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

_PARAMETRO_EMPRESA = Annotated[uuid.UUID, Query(description="Empresa del reporte.")]
_PARAMETRO_ANIO = Annotated[int, Query(ge=2000, le=9999, description="Año del reporte.")]


@router.post(
    "/vista-previa",
    summary="Vista previa del reporte (PDF, sin guardar)",
    description=(
        "Exige una validación vigente (409 `ANALYSIS_NOT_VALIDATED` si no la hay). **Si esa validación ya está respaldada por una "
        "emisión** (por su identificador o, en un histórico, por el número y la huella guardados en su snapshot) devuelve EXACTAMENTE "
        "el PDF almacenado de esa emisión, sin compilar ni reconstruirlo con datos actuales (nombre de empresa, usuarios, etc.). "
        "**Si todavía no la tiene** devuelve una vista previa CANDIDATA con el mismo constructor que la emisión: no se guarda, no "
        "reserva número de versión y no promete que una emisión futura conserve la versión o la fecha que muestra. Un histórico de "
        "origen no verificable no se presume equivalente a la validación. Nunca crea, modifica ni sustituye un reporte ni registra "
        "un evento de generación."
    ),
    responses={
        **_RESPUESTAS_COMUNES,
        **_ERRORES_DE_ANALISIS,
        200: _respuesta_pdf("PDF almacenado de la emisión existente, o vista previa candidata."),
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
    return Response(content=pdf, media_type=MEDIA_PDF)


@router.post(
    "",
    status_code=201,
    summary="Emitir una versión del reporte de prospección (PDF)",
    description=(
        "Emite la versión del reporte que respalda la ÚLTIMA validación vigente del análisis GRI (LTX:RN-041 reformulada). Cada "
        "versión es INMUTABLE y una empresa/año admite varias. Es idempotente por validación: **201** y `reutilizado=false` cuando "
        "crea una versión nueva; **200** y `reutilizado=true` cuando ya existía la versión de esa misma validación (no se crea "
        "otra, también ante solicitudes simultáneas). Sin validación vigente (incluye estados cambiados sin volver a validar) "
        "responde 409 `ANALYSIS_NOT_VALIDATED`. Si el análisis o su validación cambian mientras se genera, 409 "
        "`REPORT_SOURCE_CHANGED` y no se publica nada. Las versiones anteriores nunca se modifican."
    ),
    responses={
        **_RESPUESTAS_COMUNES,
        **_ERRORES_DE_ANALISIS,
        200: {"model": ReporteEmision, "description": "Ya existía la versión de esta validación (`reutilizado=true`)."},
        409: {
            "description": (
                "`ANALYSIS_NOT_VALIDATED`, `REPORT_SOURCE_CHANGED` (el origen cambió durante la generación) o "
                "`REPORT_VERSION_CONFLICT` (no se pudo asignar la versión; reintente)."
            )
        },
    },
)
async def generar(
    payload: GenerarReporteRequest,
    response: Response,
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
    compilar: CompiladorPdf,
) -> ReporteEmision:
    emision = await generar_reporte(
        db,
        ambiente=ambiente,
        empresa_id=payload.empresa_id,
        anio=payload.anio,
        usuario_id=usuario.id,
        compilar_pdf=compilar,
    )
    if emision.reutilizado:
        response.status_code = 200
    return emision


@router.get(
    "",
    summary="Historial de reportes (todas las versiones)",
    description=(
        "TODAS las versiones emitidas, la más reciente primero; no agrupa ni oculta versiones anteriores. Cada elemento trae `version`, "
        "`es_ultima_version` y la validación de origen. Filtros opcionales `empresa_id` y `anio`; sin `limite` devuelve todo."
    ),
    responses=_RESPUESTAS_COMUNES,
)
async def listar(
    db: DatabaseSession,
    usuario: AdministradorUser,
    empresa_id: Annotated[uuid.UUID | None, Query(description="Solo esta empresa.")] = None,
    anio: Annotated[int | None, Query(ge=2000, le=9999, description="Solo este año.")] = None,
    limite: Annotated[int | None, Query(ge=1, le=500, description="Máximo de elementos (sin valor: todos).")] = None,
    desplazamiento: Annotated[int, Query(ge=0, description="Elementos a omitir.")] = 0,
) -> list[ReporteResumen]:
    return await listar_reportes(db, empresa_id=empresa_id, anio=anio, limite=limite, desplazamiento=desplazamiento)


@router.get(
    "/disponibilidad",
    summary="Años disponibles para generar el reporte de una empresa",
    description=(
        "Años con documentos elegibles (o con reportes emitidos), todos evaluados en el MISMO instante (una instantánea de lectura). "
        "El estado refleja SIEMPRE el análisis actual; un reporte anterior no lo oculta y queda como referencia (`reporte_id`, "
        "`reporte_version`): `PENDIENTE_REVISION` (filas GRI sin estado), `PENDIENTE_VALIDACION` (revisado, falta validar o la validación "
        "venció), `VALIDACION_VIGENTE` (validación vigente sin emisión verificablemente asociada), `REPORTE_GENERADO` (la última "
        "versión demostrablemente respalda la última validación y esta sigue vigente: `reporte_actualizado=true`), "
        "`ACTUALIZACION_PENDIENTE` (validación vigente posterior sin emitir), `SIN_CODIGOS_GRI` o `SIN_DOCUMENTACION_ELEGIBLE`. "
        "`puede_generar` es true exactamente en `VALIDACION_VIGENTE` y `ACTUALIZACION_PENDIENTE`; con `REPORTE_GENERADO` es false "
        "porque no hay nada nuevo que emitir (repetir `POST /reportes` devuelve la versión existente). Con `anio` siempre responde "
        "ese año. Solo lectura e informativo: la emisión vuelve a comprobar la validación vigente."
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
    "/vigente",
    summary="Última versión emitida de una empresa y año, y si está actualizada",
    description=(
        "Metadatos de la última versión emitida y su relación con la última validación y con el análisis actual, evaluados en el MISMO "
        "instante (una instantánea de lectura; no se promete que siga siendo lo último después de él). `reporte_actualizado` es "
        "true SOLO si se demuestra que la versión respalda la última validación Y esa validación sigue vigente; con cambios por "
        "revisar o validar (`validacion_anterior_no_vigente`) es false aunque la versión corresponda a la última validación "
        "registrada. `actualizacion_pendiente` indica una validación posterior sin emitir (no cualquier cambio). "
        "`origen_verificable=false` marca un histórico sin datos para saber qué validación lo respalda: se describe sin fallar, nunca "
        "se anuncia actualizado y `puede_generar` indica si procede emitir una versión nueva. Solo lectura: no genera nada."
    ),
    responses={**_RESPUESTAS_COMUNES, 404: {"description": "Sin reportes para ese año (`REPORT_NOT_FOUND`)."}},
)
async def vigente(
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
    empresa_id: _PARAMETRO_EMPRESA,
    anio: _PARAMETRO_ANIO,
) -> ReporteVigente:
    return await obtener_reporte_vigente(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)


@router.get(
    "/vigente/archivo",
    summary="Descargar el PDF de la última versión emitida (solo si está actualizada)",
    description=(
        "Entrega los bytes almacenados de la última versión emitida SOLO si puede demostrarse que está actualizada (respalda la "
        "última validación y esta sigue vigente). Selección, evaluación, lectura del PDF y auditoría ocurren en una sola instantánea. "
        "Si no, 409 con un código específico y `details` (`reporte_id`, `version`, `estado`, `puede_generar`, `motivo_no_generar`, "
        "`accion_sugerida` = REVISAR | VALIDAR | EMITIR | DESCARGAR_VERSION_HISTORICA): `REPORT_UPDATE_PENDING` (existe una "
        "validación posterior sin emitir), `REPORT_ORIGIN_UNVERIFIABLE` (histórico cuyo origen no puede verificarse) o "
        "`REPORT_ANALYSIS_NOT_CURRENT` (cambios por revisar o validar, o sin documentación elegible). Un rechazo no registra "
        "auditoría de éxito. La versión del `details.reporte_id` se descarga explícitamente con `/reportes/{reporte_id}/archivo`."
    ),
    responses={
        **_RESPUESTAS_COMUNES,
        404: {"description": "Sin reportes para ese año (`REPORT_NOT_FOUND`)."},
        409: {
            "description": (
                "`REPORT_UPDATE_PENDING`, `REPORT_ORIGIN_UNVERIFIABLE` o `REPORT_ANALYSIS_NOT_CURRENT`: el reporte no puede "
                "presentarse como actualizado."
            )
        },
        200: _respuesta_pdf("PDF de la última versión, actualizada."),
    },
)
async def descargar_vigente(
    db: DatabaseSession,
    ambiente: Ambiente,
    usuario: AdministradorUser,
    empresa_id: _PARAMETRO_EMPRESA,
    anio: _PARAMETRO_ANIO,
) -> Response:
    pdf, reporte = await obtener_pdf_vigente(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, usuario_id=usuario.id)
    return Response(
        content=pdf,
        media_type=MEDIA_PDF,
        headers={"Content-Disposition": f'attachment; filename="reporte_{reporte.id}.pdf"', "X-Reporte-Version": str(reporte.version)},
    )


@router.get(
    "/{reporte_id}/archivo",
    summary="Descargar el PDF del reporte",
    description=(
        "Entrega EXACTAMENTE el PDF almacenado de esa versión (sin recompilar), sea la última o una anterior; su SHA-256 es el de "
        "`ReporteResumen.sha256`. Una versión histórica nunca cambia de archivo."
    ),
    responses={
        **_RESPUESTAS_COMUNES,
        404: {"description": "El reporte no existe (`REPORT_NOT_FOUND`)."},
        200: _respuesta_pdf("PDF del reporte."),
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
        media_type=MEDIA_PDF,
        headers={"Content-Disposition": f'attachment; filename="reporte_{reporte_id}.pdf"'},
    )

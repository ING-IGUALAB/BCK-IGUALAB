"""Casos de uso de los Reportes de Prospección (Módulo 7, CU006). RF-025..027 · RN-038..041.

El reporte es un **render determinista** del análisis GRI ya validado (RN-040): se obtienen los datos
con `obtener_analisis_validado` (que exige validación vigente), se rellena la plantilla LaTeX y se
compila a PDF. Se guarda un único reporte por empresa/año con su snapshot e integridad (sha256).

INMUTABILIDAD (LTX:RN-041, CU006). Un reporte generado no se modifica, no se elimina y no se regenera: una segunda generación para la
misma empresa/año se rechaza con 409 `REPORT_ALREADY_EXISTS` y deja intacto el registro anterior (ID, PDF, SHA-256, snapshot,
autor y fecha). La descarga entrega el PDF ALMACENADO sin recompilarlo. La restricción única `uq_reporte_empresa_anio` y el
disparador de solo inserción (migración 0005) lo garantizan en la base; la comprobación previa solo evita compilar en vano.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models import Empresa, ReporteProspeccion, ResultadoAuditoria, TipoEventoAuditoria, Usuario
from app.modules.analisis_gri.application.servicio import obtener_analisis_validado
from app.modules.auditoria.service import registrar_evento
from app.modules.reportes.domain.plantilla import datos_desde_analisis, renderizar
from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf
from app.modules.reportes.schemas import ReporteResumen


async def _nombres(db: AsyncSession, empresa_id: uuid.UUID, usuario_id: uuid.UUID) -> tuple[str, str]:
    """Devuelve (nombre de la empresa, nombre del usuario) para enriquecer el resumen."""
    empresa = (await db.execute(select(Empresa.nombre).where(Empresa.id == empresa_id))).scalar_one_or_none()
    usuario = (await db.execute(select(Usuario.nombre).where(Usuario.id == usuario_id))).scalar_one_or_none()
    return empresa or "", usuario or ""


async def _resumen(db: AsyncSession, r: ReporteProspeccion) -> ReporteResumen:
    empresa_nombre, generado_por_nombre = await _nombres(db, r.empresa_id, r.generado_por)
    return ReporteResumen(
        id=r.id,
        empresa_id=r.empresa_id,
        empresa_nombre=empresa_nombre,
        anio=r.anio,
        generado_por=r.generado_por,
        generado_por_nombre=generado_por_nombre,
        sha256=r.sha256,
        creado_en=r.creado_en,
    )


async def _reporte_existente(db: AsyncSession, empresa_id: uuid.UUID, anio: int):
    """Identificador y fecha del reporte de la empresa/año, sin cargar el PDF ni el snapshot."""
    return (
        await db.execute(
            select(ReporteProspeccion.id, ReporteProspeccion.creado_en).where(
                ReporteProspeccion.empresa_id == empresa_id, ReporteProspeccion.anio == anio
            )
        )
    ).first()


def _ya_existe(existente, empresa_id: uuid.UUID, anio: int) -> ConflictError:
    return ConflictError(
        "REPORT_ALREADY_EXISTS",
        "Ya existe un reporte de prospección para esta empresa y año. Es inmutable: no se genera otro ni se sustituye.",
        details={
            "reporte_id": str(existente.id),
            "empresa_id": str(empresa_id),
            "anio": anio,
            "creado_en": existente.creado_en.isoformat(),
        },
    )


async def generar_reporte(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    usuario_id: uuid.UUID,
    compilar_pdf=compilar_html_a_pdf,
) -> ReporteResumen:
    """Genera el reporte de la empresa/año a partir del análisis validado. NUNCA sustituye uno existente.

    Lanza `ConflictError("REPORT_ALREADY_EXISTS")` si ya hay un reporte de la empresa/año (también si otra generación simultánea
    lo creó primero), `ConflictError("ANALYSIS_NOT_VALIDATED")` si no hay validación vigente y `NotFoundError` si la empresa no
    existe (ambos provienen de `obtener_analisis_validado`). Un rechazo no escribe nada ni registra el evento de generación.
    """
    existente = await _reporte_existente(db, empresa_id, anio)
    if existente is not None:
        await db.rollback()
        raise _ya_existe(existente, empresa_id, anio)

    analisis = await obtener_analisis_validado(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)

    fecha = datetime.now(timezone.utc).date().isoformat()
    html = renderizar(datos_desde_analisis(analisis, fecha))
    pdf = compilar_pdf(html)
    sha256 = hashlib.sha256(pdf).hexdigest()
    snapshot = analisis.model_dump(mode="json")

    reporte = ReporteProspeccion(
        empresa_id=empresa_id,
        anio=anio,
        generado_por=usuario_id,
        pdf=pdf,
        sha256=sha256,
        snapshot=snapshot,
    )
    db.add(reporte)
    registrar_evento(
        db,
        TipoEventoAuditoria.GENERACION_REPORTE,
        f"Reporte de prospección generado (empresa={empresa_id}, anio={anio})",
        usuario_id=usuario_id,
        resultado=ResultadoAuditoria.EXITOSO,
    )
    try:
        await db.commit()
    except IntegrityError:
        # Carrera: otra generación confirmó primero. La restricción única de la base decide; el reporte y el evento de ESTA
        # solicitud se deshacen y el resultado es el conflicto de negocio (no un 500). Si el reporte de la empresa/año no existe,
        # la violación fue de otra restricción y se propaga tal cual.
        await db.rollback()
        existente = await _reporte_existente(db, empresa_id, anio)
        if existente is None:
            raise
        await db.rollback()
        raise _ya_existe(existente, empresa_id, anio) from None
    await db.refresh(reporte)
    return await _resumen(db, reporte)


async def vista_previa(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    compilar_pdf=compilar_html_a_pdf,
) -> bytes:
    """Genera el PDF del reporte SIN guardarlo (para la vista previa del frontend).

    Mismos datos y plantilla que `generar_reporte`, pero no persiste nada: no crea, no modifica y no sustituye ningún reporte
    (tampoco uno existente). Usa `obtener_analisis_validado` (exige validación vigente)."""
    analisis = await obtener_analisis_validado(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
    fecha = datetime.now(timezone.utc).date().isoformat()
    html = renderizar(datos_desde_analisis(analisis, fecha))
    return compilar_pdf(html)


async def listar_reportes(db: AsyncSession) -> list[ReporteResumen]:
    filas = (await db.execute(select(ReporteProspeccion).order_by(ReporteProspeccion.creado_en.desc()))).scalars().all()
    return [await _resumen(db, r) for r in filas]


async def obtener_pdf(db: AsyncSession, reporte_id: uuid.UUID, *, usuario_id: uuid.UUID) -> bytes:
    """Los bytes ALMACENADOS del reporte, sin recompilar (RN-041)."""
    reporte = await db.get(ReporteProspeccion, reporte_id)
    if reporte is None:
        raise NotFoundError("REPORT_NOT_FOUND", "El reporte solicitado no existe.")
    registrar_evento(
        db,
        TipoEventoAuditoria.DESCARGA,
        f"Descarga de reporte (reporte={reporte_id}, empresa={reporte.empresa_id}, anio={reporte.anio})",
        usuario_id=usuario_id,
        resultado=ResultadoAuditoria.EXITOSO,
    )
    await db.commit()
    return reporte.pdf

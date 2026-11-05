"""Reportes de prospección (Módulo 7, CU006). Una empresa/año admite VARIAS versiones inmutables (RN-041, BUG-15), cada una con el
snapshot del análisis validado que la originó (RN-040: el PDF es un render de datos ya validados, no invoca IA).

Tres números que no deben confundirse:
- `version` (esta tabla): ordinal de la emisión del reporte dentro de la empresa/año (1, 2, ...).
- `analisis_gri_validaciones.numero`: ordinal de la validación humana de esa empresa/año; `validacion_id` apunta a la fila exacta.
- `analisis_gri_control.version`: contador de cambios de estado del análisis (también guardado en `analisis_gri_validaciones`).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# JSONB en PostgreSQL, JSON genérico en SQLite (pruebas). Mismo patrón que `documentos.analisis`.
_JSON_SNAPSHOT = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class ReporteProspeccion(Base):

    __tablename__ = "reportes_prospeccion"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False, index=True)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ordinal de la emisión dentro de la empresa/año. Los reportes anteriores al versionado son la versión 1.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Validación GRI que respalda este reporte. NULL SOLO en los reportes anteriores al versionado: no se dedujo ni se inventó
    # (la tabla es de solo inserción). Un reporte nuevo siempre la registra; una validación respalda a lo sumo un reporte.
    # SIN `ForeignKey` en el modelo a propósito: `analisis_gri_validaciones` la crea la migración 0004 y no `create_all`
    # (`TABLAS_SIN_DDL_AUTOMATICO`), de modo que declararla aquí rompería la creación de tablas del arranque. Las claves foráneas
    # `fk_reporte_validacion` (0006) y `fk_reporte_validacion_empresa_anio` (compuesta con empresa y año, 0007), el disparador
    # `tr_reportes_exigir_validacion` (0007: un reporte nuevo no se inserta sin validación) y su definición exacta las exige
    # `app.migraciones.compatibilidad` en cada arranque.
    validacion_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    generado_por: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False)
    # PDF binario + su huella (integridad) y el snapshot congelado del análisis validado.
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    snapshot: Mapped[dict] = mapped_column(_JSON_SNAPSHOT, nullable=False)
    # Fecha de emisión de la versión. La fija el servicio (es la misma que figura en el PDF); `now()` solo cubre inserciones directas.
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("empresa_id", "anio", "version", name="uq_reporte_empresa_anio_version"),
        UniqueConstraint("validacion_id", name="uq_reporte_validacion"),
        CheckConstraint("version >= 1", name="ck_reporte_version_positiva"),
    )

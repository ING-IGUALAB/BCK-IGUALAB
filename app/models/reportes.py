"""Reportes de prospección (Módulo 7, CU006). Un reporte por empresa/año (RN-041), con el
snapshot del análisis validado (RN-040: el PDF es un render de datos ya validados, no invoca IA)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func
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
    generado_por: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False)
    # PDF binario + su huella (integridad) y el snapshot congelado del análisis validado.
    pdf: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    snapshot: Mapped[dict] = mapped_column(_JSON_SNAPSHOT, nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("empresa_id", "anio", name="uq_reporte_empresa_anio"),
    )

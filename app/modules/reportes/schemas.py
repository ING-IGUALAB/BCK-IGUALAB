"""Contratos HTTP de Reportes de Prospección (Módulo 7, CU006). RF-025..027."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GenerarReporteRequest(BaseModel):
    model_config = {"extra": "forbid"}

    empresa_id: uuid.UUID
    anio: int = Field(ge=2000)


class ReporteResumen(BaseModel):
    id: uuid.UUID
    empresa_id: uuid.UUID
    empresa_nombre: str = Field(description="Nombre de la empresa del reporte.")
    anio: int
    generado_por: uuid.UUID
    generado_por_nombre: str = Field(description="Nombre del usuario que generó el reporte.")
    sha256: str = Field(description="Huella de integridad del PDF generado.")
    creado_en: datetime

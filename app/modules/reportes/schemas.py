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
    anio: int
    generado_por: uuid.UUID
    sha256: str = Field(description="Huella de integridad del PDF generado.")
    creado_en: datetime

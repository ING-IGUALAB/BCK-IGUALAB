"""Contratos HTTP de Auditoría (Módulo 8, CU007). RF-029/030."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ResultadoAuditoria, TipoEventoAuditoria


class EventoAuditoriaResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID | None = None
    tipo_evento: TipoEventoAuditoria
    detalle: str
    fecha_hora_utc: datetime
    resultado: ResultadoAuditoria | None = Field(
        default=None,
        description="EXITOSO, FALLIDO o DENEGADO (LTX:RN-043). null solo en eventos anteriores a la migración 0005.",
    )


class PaginaEventos(BaseModel):
    items: list[EventoAuditoriaResponse]
    pagina: int = Field(description="Página devuelta (base 1).")
    tamano: int = Field(description="Tamaño de página.")
    total: int = Field(description="Total de eventos que cumplen el filtro.")

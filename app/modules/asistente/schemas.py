"""Contratos HTTP del Asistente IA — RAG (Módulo 6, CU004). RF-023 / RF-024."""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ConsultaRequest(BaseModel):
    pregunta: str = Field(min_length=1, max_length=2000, description="Pregunta en lenguaje natural sobre el corpus indexado.")
    empresa_id: uuid.UUID | None = Field(default=None, description="Restringe la búsqueda a una empresa (opcional).")
    anio: int | None = Field(default=None, ge=2000, description="Restringe la búsqueda a un año (opcional).")


class Cita(BaseModel):
    """Fuente verificada de la respuesta (RN-034). Solo cita fragmentos que existen en el contexto."""

    documento_id: uuid.UUID
    seccion: str = Field(description="Ruta de encabezados del fragmento citado.")
    anio: int
    empresa_id: uuid.UUID
    extracto: str = Field(description="Fragmento de texto literal usado como respaldo.")


class ConsultaResponse(BaseModel):
    respuesta: str = Field(description="Respuesta del asistente, fundamentada en el corpus.")
    citas: list[Cita] = Field(default_factory=list, description="Fuentes verificadas; vacía si no hay evidencia.")
    sin_evidencia: bool = Field(default=False, description="True si el corpus no contiene evidencia para responder (RN-035).")
    mensaje: str | None = Field(default=None, description="Aclaración cuando no hay evidencia.")

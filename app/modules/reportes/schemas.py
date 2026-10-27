"""Contratos HTTP de Reportes de Prospección (Módulo 7, CU006). RF-025..027."""
from __future__ import annotations

import enum
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


class EstadoAnioReporte(str, enum.Enum):
    """Dónde está un año de la empresa respecto del reporte de prospección. Informativo: la generación lo vuelve a comprobar."""

    PENDIENTE_REVISION = "PENDIENTE_REVISION"  # hay filas GRI sin estado asignado
    PENDIENTE_VALIDACION = "PENDIENTE_VALIDACION"  # todas las filas tienen estado; falta «Validar análisis» (o la validación venció)
    VALIDACION_VIGENTE = "VALIDACION_VIGENTE"  # se puede generar el reporte
    REPORTE_GENERADO = "REPORTE_GENERADO"  # ya existe el reporte (inmutable); una segunda generación se rechaza con 409
    SIN_CODIGOS_GRI = "SIN_CODIGOS_GRI"  # hay documentos elegibles, pero ningún código GRI evaluable: no habilita el reporte
    SIN_DOCUMENTACION_ELEGIBLE = "SIN_DOCUMENTACION_ELEGIBLE"  # solo al preguntar por un año concreto sin documentos elegibles


class AnioDisponible(BaseModel):
    anio: int
    estado: EstadoAnioReporte
    puede_generar: bool = Field(description="True solo con VALIDACION_VIGENTE y sin reporte generado.")
    documentos_elegibles: int | None = Field(default=None, description="null si el año ya tiene reporte (no se recalcula).")
    filas_gri: int | None = None
    filas_sin_estado: int | None = None
    validacion_anterior_no_vigente: bool = Field(
        default=False, description="Hubo una validación, pero cambiaron los estados o los documentos: hay que validar de nuevo."
    )
    reporte_id: uuid.UUID | None = None
    reporte_creado_en: datetime | None = None


class DisponibilidadReportesResponse(BaseModel):
    ambiente: str
    empresa_id: uuid.UUID
    empresa_nombre: str
    empresa_activa: bool
    anios: list[AnioDisponible] = Field(description="Años con documentos elegibles o con reporte, del más reciente al más antiguo.")
    aviso: str = Field(
        default="Consulta informativa: la generación vuelve a comprobar la validación vigente y que no exista un reporte.",
    )

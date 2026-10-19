"""Contratos HTTP del Asistente IA — RAG (Módulo 6, CU004). RF-023 / RF-024.

CAMBIO DE CONTRATO respecto de la versión anterior (el frontend debe adaptarse):
- Petición: `sector`, `empresa_id` y `anio` son OBLIGATORIOS (LTX:RN-037). Antes `empresa_id` y `anio` eran opcionales y no había sector.
- Respuesta: además de `respuesta`, `citas`, `sin_evidencia` y `mensaje`, trae `afirmaciones` (cada una con sus `referencias` y su
  `cita_textual`), `motivo` y `afirmaciones_descartadas`. Cada `Cita` trae `referencia`, el número `[n]` con el que las afirmaciones
  y el texto de `respuesta` la nombran, y `empresa_nombre`.
"""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.models import SectorEmpresa


class ConsultaRequest(BaseModel):
    pregunta: str = Field(min_length=1, max_length=2000, description="Pregunta en lenguaje natural sobre el corpus indexado.")
    sector: SectorEmpresa = Field(description="Sector de la empresa seleccionada (obligatorio, LTX:RN-037).")
    empresa_id: uuid.UUID = Field(description="Empresa sobre la que se consulta (obligatoria, LTX:RN-037).")
    anio: int = Field(ge=2000, le=9999, description="Año de los documentos (obligatorio, LTX:RN-037).")


class Cita(BaseModel):
    """Fuente de una o más afirmaciones (LTX:RN-034: documento, empresa y año). Solo cita fragmentos entregados al modelo."""

    referencia: int = Field(description="Número [n] con que las afirmaciones y `respuesta` nombran esta fuente.")
    documento_id: uuid.UUID
    documento_nombre: str = Field(description="Nombre del archivo del documento citado.")
    empresa_id: uuid.UUID
    empresa_nombre: str = Field(description="Nombre de la empresa del documento citado.")
    anio: int
    seccion: str = Field(description="Ruta de encabezados del fragmento citado.")
    extracto: str = Field(description="Inicio del fragmento citado (hasta 300 caracteres).")


class Afirmacion(BaseModel):
    """Una afirmación de la respuesta con su respaldo. SE VERIFICA que `cita_textual` figure literalmente en cada fragmento de
    `referencias` y que las cifras del `texto` consten en esa cita o en su sección. NO se verifica que el pasaje implique la
    afirmación (puede ser irrelevante o contradecirla): eso lo revisa quien lee. No es una garantía semántica."""

    texto: str
    referencias: list[int] = Field(
        description="Números de `citas[].referencia` cuyo fragmento CONTIENE la `cita_textual` (una referencia válida sin la cita no se muestra)."
    )
    cita_textual: str = Field(description="Pasaje que aparece literalmente en los fragmentos de `referencias` (mín. 20 caracteres y 3 palabras).")


class ConsultaResponse(BaseModel):
    respuesta: str = Field(description="Afirmaciones verificadas con sus marcadores [n]; vacía si `sin_evidencia`.")
    afirmaciones: list[Afirmacion] = Field(default_factory=list)
    citas: list[Cita] = Field(default_factory=list, description="Fuentes de las afirmaciones; vacía si no hay evidencia.")
    sin_evidencia: bool = Field(
        default=False, description="True si no hay ninguna afirmación respaldada (LTX:RN-035) o la consulta está fuera de dominio."
    )
    motivo: str | None = Field(
        default=None,
        description="Solo con `sin_evidencia`: SIN_FRAGMENTOS, MODELO_SIN_EVIDENCIA, AFIRMACIONES_NO_VERIFICABLES, "
        "RESPUESTA_NO_INTERPRETABLE o FUERA_DE_DOMINIO.",
    )
    mensaje: str | None = Field(default=None, description="Aclaración legible cuando `sin_evidencia`.")
    afirmaciones_descartadas: int = Field(
        default=0, description="Afirmaciones del modelo omitidas por no poder respaldarse (referencia inexistente o cita no literal)."
    )

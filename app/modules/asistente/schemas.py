"""Contratos HTTP del Asistente IA — RAG (Módulo 6, CU004). RF-023 / RF-024.

CAMBIO DE CONTRATO respecto de la versión anterior (el frontend debe adaptarse):
- Petición: `sector`, `empresa_id` y `anio` son OBLIGATORIOS (LTX:RN-037). Antes `empresa_id` y `anio` eran opcionales y no había sector.
- Respuesta: además de `respuesta`, `citas`, `sin_evidencia` y `mensaje`, trae `afirmaciones` (cada una con sus `referencias` y su
  `cita_textual`), `motivo` y `afirmaciones_descartadas`. Cada `Cita` trae `referencia`, el número `[n]` con el que las afirmaciones
  y el texto de `respuesta` la nombran, y `empresa_nombre`.
- Estado registrado en Análisis GRI (aditivo, opcional): `estado_registrado` (el estado manual de un código GRI, nunca una cita) y
  `busqueda_documental` (REALIZADA por omisión; NO_REALIZADA en una pregunta de solo estado, que es válida sin citas). Las respuestas y
  conversaciones guardadas antes de este cambio no traen ninguno de los dos y se leen con sus valores por omisión.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Literal

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


class BusquedaDocumental(str, enum.Enum):
    """Si se buscó en los documentos para esta respuesta. NO_REALIZADA = no hubo búsqueda (la pregunta era solo por el estado registrado)."""

    REALIZADA = "REALIZADA"
    NO_REALIZADA = "NO_REALIZADA"


class FilaEstadoRegistrado(BaseModel):
    """Una identidad GRI (código + edición) del Análisis GRI con su estado MANUAL actual."""

    identidad: str = Field(description="Clave de la fila en Análisis GRI, p. ej. «1:2021» o «102:ambigua».")
    codigo: str
    edicion: str | None = Field(description="Null si la edición no está resuelta (identidad ambigua).")
    estandar: str = Field(description="Etiqueta legible, p. ej. «GRI 1 (2021)».")
    estado: str | None = Field(description="OK, BAJA_SUSTANCIA o SUB_REPORTADO; null si nadie le asignó un estado.")
    estado_etiqueta: str | None = Field(description="Etiqueta exacta para mostrar: «OK», «Baja sustancia» o «Sub-reportado».")


class ValidacionRegistrada(BaseModel):
    """La última validación del análisis (vigente o no). Misma regla de vigencia que Análisis GRI."""

    numero: int
    validado_en: datetime
    motivos_no_vigente: list[str] = Field(description="Vacía si sigue vigente; si no, ESTADOS_CAMBIARON y/o DOCUMENTOS_CAMBIARON.")


class EstadoRegistrado(BaseModel):
    """Estado REGISTRADO en Análisis GRI de los códigos que pidió la pregunta, compuesto por el servidor (no por el modelo). NO es una cita del
    documento ni una conclusión automática de que existe o no una brecha: es el estado que una persona asignó en Análisis GRI."""

    fuente: Literal["ANALISIS_GRI"] = "ANALISIS_GRI"
    texto: str = Field(description="Texto legible listo para mostrar (incluye la vigencia). Nunca lleva marcadores [n].")
    codigos_solicitados: list[str] = Field(description="Los códigos tal como los escribió el usuario, p. ej. «GRI 1».")
    filas: list[FilaEstadoRegistrado] = Field(description="Filas encontradas en el análisis. Un código solicitado que no figura no aparece aquí.")
    validacion_vigente: bool = Field(description="True solo si el análisis tiene una validación vigente (flujo VALIDADO).")
    validacion: ValidacionRegistrada | None = Field(description="Última validación; null si el análisis nunca se validó.")


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
    estado_registrado: EstadoRegistrado | None = Field(
        default=None,
        description="Solo si la pregunta pidió el estado de un código GRI: el estado registrado en Análisis GRI (fuente distinta del documento; "
        "nunca una cita). Puede acompañar a `citas` o ir solo. Las respuestas anteriores no lo traen (null).",
    )
    busqueda_documental: BusquedaDocumental = Field(
        default=BusquedaDocumental.REALIZADA,
        description="NO_REALIZADA: pregunta de solo estado, sin búsqueda en los documentos; `sin_evidencia` es false, `motivo` null, `citas` "
        "vacías y `respuesta` repite `estado_registrado.texto`. REALIZADA: la búsqueda se hizo (con o sin resultados, ver `sin_evidencia`).",
    )

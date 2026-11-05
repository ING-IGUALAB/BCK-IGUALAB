"""Contratos HTTP de Reportes de Prospección (Módulo 7, CU006). RF-025..027.

Vocabulario del versionado (BUG-15). No confundir:
- «versión del reporte»: ordinal de la emisión de la empresa/año (1, 2, ...). Cada una es inmutable.
- «validación»: confirmación humana del análisis GRI (`validacion_numero`); respalda como mucho UN reporte.
- «última versión emitida» (`es_ultima_version`): la de mayor versión de la empresa/año. NO implica que refleje la última validación.
- «actualizado» (`reporte_actualizado`): True SOLO si puede demostrarse que la última versión emitida respalda la última validación
  Y esa validación sigue vigente respecto del análisis actual. Con cambios por revisar o validar es False aunque el reporte siga
  correspondiendo a la última validación registrada.
- «actualización pendiente» (`actualizacion_pendiente`): existe una validación posterior a la que respalda el reporte y aún no
  se emitió. NO es sinónimo de «hay cambios»: los cambios sin validar se ven en `estado`/`motivo_no_generar` y en
  `validacion_anterior_no_vigente`.
- «origen verificable» (`origen_verificable`): se sabe qué validación respalda el reporte (guarda `validacion_id` o, siendo
  histórico, su snapshot conservó el número y la huella). Un histórico sin esos datos sigue consultable y descargable por
  identificador, pero nunca se presenta como actualizado ni como respaldo de una validación.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GenerarReporteRequest(BaseModel):
    model_config = {"extra": "forbid"}

    empresa_id: uuid.UUID
    anio: int = Field(ge=2000)


class MotivoNoGenerar(str, enum.Enum):
    """Por qué NO se puede emitir una versión nueva ahora (informativo: la emisión lo vuelve a comprobar)."""

    REPORTE_ACTUALIZADO = "REPORTE_ACTUALIZADO"  # la última versión ya respalda la validación vigente: no hay nada nuevo que emitir
    PENDIENTE_REVISION = "PENDIENTE_REVISION"  # hay filas GRI sin estado
    PENDIENTE_VALIDACION = "PENDIENTE_VALIDACION"  # revisado, pero falta «Validar análisis» (o la validación dejó de ser vigente)
    SIN_CODIGOS_GRI = "SIN_CODIGOS_GRI"
    SIN_DOCUMENTACION_ELEGIBLE = "SIN_DOCUMENTACION_ELEGIBLE"


class ReporteResumen(BaseModel):
    id: uuid.UUID
    empresa_id: uuid.UUID
    empresa_nombre: str = Field(description="Nombre de la empresa del reporte.")
    anio: int
    version: int = Field(description="Versión del reporte dentro de la empresa/año (1 = primera emisión). Cada versión es inmutable.")
    es_ultima_version: bool = Field(
        description="True si es la versión más alta emitida para la empresa/año. No implica que refleje la última validación."
    )
    validacion_id: uuid.UUID | None = Field(
        default=None,
        description="Validación GRI que respalda este reporte. null SOLO en reportes anteriores al versionado (origen histórico).",
    )
    validacion_numero: int | None = Field(
        default=None,
        description="Número de esa validación. En un reporte histórico sale del snapshot guardado (null si el snapshot no lo trae).",
    )
    generado_por: uuid.UUID
    generado_por_nombre: str = Field(description="Nombre del usuario que generó el reporte.")
    sha256: str = Field(description="Huella de integridad del PDF generado.")
    creado_en: datetime = Field(description="Fecha de emisión de esta versión (la misma que figura en el PDF).")


class ReporteEmision(ReporteResumen):
    """Respuesta de `POST /reportes`: la versión emitida, o la ya existente para la misma validación."""

    reutilizado: bool = Field(
        default=False,
        description="True si NO se creó una versión nueva: ya existía la emitida para esta misma validación (HTTP 200).",
    )


class ReporteVigente(ReporteResumen):
    """Última versión emitida de una empresa/año y su relación con la última validación."""

    ultima_validacion_id: uuid.UUID | None = None
    ultima_validacion_numero: int | None = None
    origen_verificable: bool = Field(
        description="False si es un reporte histórico cuyo origen no puede verificarse (sin `validacion_id` ni número y huella en el snapshot)."
    )
    validacion_anterior_no_vigente: bool = Field(
        description="Existe una validación, pero el análisis actual cambió (estados, documentos o elegibilidad): hay que revisar o validar de nuevo."
    )
    reporte_actualizado: bool = Field(
        description="True solo si se demuestra que esta versión respalda la última validación Y esa validación sigue vigente."
    )
    actualizacion_pendiente: bool = Field(
        description="True si existe una validación posterior a la que respalda esta versión y aún no se emitió (no aplica a un origen no verificable)."
    )
    puede_generar: bool = Field(description="True si `POST /reportes` emitiría ahora una versión nueva.")
    motivo_no_generar: MotivoNoGenerar | None = None


class EstadoAnioReporte(str, enum.Enum):
    """Dónde está un año de la empresa respecto del reporte de prospección. Informativo: la generación lo vuelve a comprobar."""

    PENDIENTE_REVISION = "PENDIENTE_REVISION"  # hay filas GRI sin estado asignado (con o sin reportes anteriores)
    PENDIENTE_VALIDACION = "PENDIENTE_VALIDACION"  # todas las filas tienen estado; falta «Validar análisis» (o la validación venció)
    VALIDACION_VIGENTE = "VALIDACION_VIGENTE"  # validación vigente sin emisión verificablemente asociada: se puede generar una versión
    REPORTE_GENERADO = "REPORTE_GENERADO"  # la última versión demostrablemente respalda la última validación, que sigue vigente
    ACTUALIZACION_PENDIENTE = "ACTUALIZACION_PENDIENTE"  # hay una validación vigente posterior a la del último reporte: se puede emitir otra versión
    SIN_CODIGOS_GRI = "SIN_CODIGOS_GRI"  # hay documentos elegibles, pero ningún código GRI evaluable: no habilita el reporte
    SIN_DOCUMENTACION_ELEGIBLE = "SIN_DOCUMENTACION_ELEGIBLE"  # el año no tiene documentos elegibles (el año consultado, o uno con reportes históricos)


class AnioDisponible(BaseModel):
    anio: int
    estado: EstadoAnioReporte
    puede_generar: bool = Field(
        description=(
            "True si `POST /reportes` emitiría ahora una versión nueva: exactamente cuando el estado es VALIDACION_VIGENTE o "
            "ACTUALIZACION_PENDIENTE. Con REPORTE_GENERADO es false porque no hay nada nuevo que emitir (repetir la solicitud devuelve la "
            "versión existente)."
        )
    )
    motivo_no_generar: MotivoNoGenerar | None = Field(default=None, description="Por qué no se puede emitir; null si `puede_generar`.")
    documentos_elegibles: int | None = Field(default=None, description="null si el año ya no tiene documentos elegibles.")
    filas_gri: int | None = None
    filas_sin_estado: int | None = None
    validacion_anterior_no_vigente: bool = Field(
        default=False,
        description="Existe una validación, pero el análisis actual cambió (estados, documentos o elegibilidad): hay que revisar o validar de nuevo.",
    )
    reporte_id: uuid.UUID | None = Field(
        default=None, description="Última versión emitida: referencia histórica, no oculta el estado actual del análisis (null sin reportes)."
    )
    reporte_version: int | None = None
    reporte_creado_en: datetime | None = None
    reporte_validacion_numero: int | None = None
    versiones_emitidas: int = 0
    ultima_validacion_numero: int | None = None
    origen_verificable: bool | None = Field(
        default=None,
        description="False si la última versión es un histórico cuyo origen no puede verificarse; null sin reporte.",
    )
    reporte_actualizado: bool | None = Field(
        default=None,
        description=(
            "True solo si se demuestra que la última versión respalda la última validación Y esa validación sigue vigente. "
            "False con cambios sin validar, con una validación posterior o con un origen no verificable. null sin reporte."
        ),
    )
    actualizacion_pendiente: bool = Field(
        default=False, description="Existe una validación posterior a la que respalda la última versión y aún no se emitió."
    )


class DisponibilidadReportesResponse(BaseModel):
    ambiente: str
    empresa_id: uuid.UUID
    empresa_nombre: str
    empresa_activa: bool
    anios: list[AnioDisponible] = Field(description="Años con documentos elegibles o con reporte, del más reciente al más antiguo.")
    aviso: str = Field(
        default=(
            "Consulta informativa: la emisión vuelve a comprobar la validación vigente y, si ya existe una versión para esa "
            "validación, la reutiliza en vez de crear otra."
        ),
    )

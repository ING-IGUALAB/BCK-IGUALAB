"""Contrato HTTP de la ingesta de documentos (respuestas). Sin lógica de negocio.

`EstadoOperacionPublico` es el estado que ve el frontend: se DERIVA del estado propio de la operación y del
documento enlazado. Solo `COMPLETADO` es éxito, y exige los fragmentos PUBLICADOS en la base vectorial: un
documento transaccionalmente completado cuya publicación vectorial sigue pendiente es `PUBLICACION_PENDIENTE`.
"""
import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import ResultadoAnalisis
from app.services.ingesta.documento_service import EstadoProgreso


class EstadoOperacionPublico(str, enum.Enum):
    CREADA = "CREADA"  # identificador entregado; todavía no llegó ningún archivo
    VALIDANDO = "VALIDANDO"  # el archivo llegó y se valida/reserva (aún no hay documento)
    INTERRUMPIDA = "INTERRUMPIDA"  # la carga no reservó documento en el plazo esperado; ver nota en el contrato
    RECHAZADA = "RECHAZADA"  # terminó SIN crear documento (validación, duplicado, empresa, tamaño…)
    EN_PROCESO = "EN_PROCESO"  # documento reservado; el progreso dice la etapa real
    PUBLICACION_PENDIENTE = "PUBLICACION_PENDIENTE"  # completado, pero NO publicado: no es éxito; reintentable
    COMPLETADO = "COMPLETADO"  # éxito: completado Y publicado en la base vectorial
    FALLIDO = "FALLIDO"  # el intento falló y la limpieza terminó
    FALLIDO_LIMPIEZA_PENDIENTE = "FALLIDO_LIMPIEZA_PENDIENTE"  # falló; la recuperación sigue limpiando


ESTADOS_TERMINALES = frozenset(
    {EstadoOperacionPublico.COMPLETADO, EstadoOperacionPublico.RECHAZADA, EstadoOperacionPublico.FALLIDO}
)


class ErrorOperacion(BaseModel):
    """Código estable y mensaje seguro (constantes del backend). Nunca contenido del documento ni errores
    crudos de proveedores."""

    code: str
    message: str


class OperacionCreadaResponse(BaseModel):
    operacion_id: uuid.UUID
    estado: EstadoOperacionPublico
    creada_en: datetime
    ingesta_url: str = Field(description="Ruta (POST multipart) donde se envía el archivo.")
    progreso_url: str = Field(description="Ruta (GET) para consultar el progreso.")


class OperacionResponse(BaseModel):
    operacion_id: uuid.UUID
    estado: EstadoOperacionPublico
    terminal: bool = Field(description="True si el estado ya no cambiará (COMPLETADO, RECHAZADA o FALLIDO).")
    exitosa: bool = Field(description="True SOLO con COMPLETADO (completado y publicado).")
    etapa: str | None = Field(default=None, description="Etapa REAL del documento; null antes de reservarlo.")
    documento_id: uuid.UUID | None = None
    fragmentos_procesados: int = 0
    fragmentos_total: int | None = Field(default=None, description="null hasta que se conoce; no hay porcentajes.")
    advertencias: list[dict[str, Any]] = Field(default_factory=list)
    resultado_analisis: ResultadoAnalisis | None = Field(
        default=None, description="Solo con COMPLETADO. OBSERVADO es una ingesta válida (indexada, disponible para RAG)."
    )
    publicacion_reintentable: bool = False
    error: ErrorOperacion | None = None
    creada_en: datetime
    actualizada_en: datetime


class ResultadoIngestaResponse(OperacionResponse):
    """Respuesta de `POST …/ingesta` (201): operación COMPLETADA y publicada."""

    motivos: list[str] = Field(default_factory=list)
    fragmentos: int = 0


class DocumentoResumen(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    id: uuid.UUID
    operacion_id: uuid.UUID | None = None
    empresa_id: uuid.UUID
    empresa_nombre: str
    sector: SectorEmpresa
    anio: int
    tipo: TipoDocumento
    nombre_archivo: str
    sha256: str
    tamano_bytes: int
    estado: EstadoProgreso
    resultado_analisis: ResultadoAnalisis | None = Field(
        default=None, description="null salvo COMPLETADO (publicado)."
    )
    disponible_para_rag: bool
    fragmentos_total: int | None = None
    cantidad_advertencias: int = 0
    cargado_por: uuid.UUID
    creado_en: datetime
    completado_en: datetime | None = None


class DocumentoDetalle(DocumentoResumen):
    etapa: str
    fragmentos_procesados: int
    advertencias: list[dict[str, Any]] = Field(default_factory=list)
    publicacion_reintentable: bool = False
    error: ErrorOperacion | None = None
    analisis: dict[str, Any] | None = Field(
        default=None,
        description="Resultado completo del análisis (grupos GRI, sanciones, motivos, advertencias, versión del "
        "catálogo). Con citas reales del documento. null si no hay análisis persistido.",
    )
    actualizado_en: datetime


class PaginaDocumentos(BaseModel):
    items: list[DocumentoResumen]
    total: int
    pagina: int
    tamano: int
    paginas: int

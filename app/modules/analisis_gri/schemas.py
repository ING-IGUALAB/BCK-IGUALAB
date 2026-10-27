"""Contrato HTTP del Módulo 5 (análisis y validación de brechas GRI). Sin lógica de negocio.

Los mismos modelos los entrega la función reutilizable para el Módulo 7 (`obtener_analisis_validado`).

CONVENCIONES
- Una FILA (`FilaGri`) es una identidad GRI evaluable: código + edición. Aunque el código aparezca decenas de veces o en varios
  documentos de la misma empresa/año, hay UNA fila, UN estado y UNA contribución al puntaje. Las ediciones distintas son filas
  distintas; una identidad ambigua o con edición no catalogada NO se fusiona con una resuelta.
- `estado = null` significa «sin estado asignado». No es un cuarto estado de cumplimiento.
- Posiciones (`inicio`, `fin`, `cita_inicio`, `cita_fin`): caracteres del texto interpretado del documento, inicio inclusivo y fin
  exclusivo. No hay páginas: el detector no las conoce y no se inventan.
- Un monto ausente es `monto = null` y `cuantificada = false`; nunca cero. No se suman monedas ni se convierte la UIT.
- SANCIONES: `sanciones` son MENCIONES (una por aparición en el texto, con su cita y posiciones). `agrupaciones_sanciones` las
  reúne SOLO dentro de un mismo documento y SOLO con evidencia explícita (autoridad + identificador completo + una distinción
  coincidente); lo que no se puede asegurar queda separado con una advertencia. Un documento procesado antes de existir la
  agrupación no trae `agrupacion_id` en sus menciones ni aparece en `agrupaciones_sanciones`: no se presenta como revisado.
"""
import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import SectorEmpresa, TipoDocumento
from app.models.analisis_gri import EstadoGri, TipoIdentidadGri
from app.models.documento_ingesta import ResultadoAnalisis

# Patrón de la identidad de una fila: «305:2016», «102:ambigua» o «102:no-catalogada-2030».
PATRON_IDENTIDAD = r"^[1-9][0-9]{0,2}:(?:(?:19|20)[0-9]{2}|ambigua|no-catalogada-(?:19|20)[0-9]{2})$"

ETIQUETAS_ESTADO = {
    EstadoGri.OK: "OK",
    EstadoGri.BAJA_SUSTANCIA: "Baja sustancia",
    EstadoGri.SUB_REPORTADO: "Sub-reportado",
}


class EstadoFlujo(str, enum.Enum):
    """Dónde está el análisis de una empresa/año (no es un estado de cumplimiento)."""

    DOCUMENTACION_INSUFICIENTE = "DOCUMENTACION_INSUFICIENTE"  # ningún documento elegible
    SIN_CODIGOS_GRI = "SIN_CODIGOS_GRI"  # hay documentos elegibles pero ninguna fila GRI evaluable
    PENDIENTE_ESTADOS = "PENDIENTE_ESTADOS"  # falta asignar estado a alguna fila
    LISTO_PARA_VALIDAR = "LISTO_PARA_VALIDAR"  # todas las filas tienen estado; falta «Validar análisis»
    VALIDADO = "VALIDADO"  # validación vigente: habilita el reporte de prospección


class EstadoPuntaje(str, enum.Enum):
    NO_DISPONIBLE = "NO_DISPONIBLE"  # sin filas evaluables: nunca cero
    PENDIENTE_ESTADOS = "PENDIENTE_ESTADOS"  # faltan estados: no se presenta ningún puntaje
    CALCULADO_SIN_VALIDAR = "CALCULADO_SIN_VALIDAR"  # todas las filas tienen estado; no es definitivo
    VALIDADO = "VALIDADO"  # definitivo: calculado sobre una validación vigente


class MotivoNoVigente(str, enum.Enum):
    ESTADOS_CAMBIARON = "ESTADOS_CAMBIARON"
    DOCUMENTOS_CAMBIARON = "DOCUMENTOS_CAMBIARON"


class _Modelo(BaseModel):
    model_config = ConfigDict(use_enum_values=False)


class ActorResumen(_Modelo):
    id: uuid.UUID
    nombre: str


class EmpresaResumen(_Modelo):
    id: uuid.UUID
    nombre: str
    sector: SectorEmpresa
    activa: bool


class DocumentoElegible(_Modelo):
    documento_id: uuid.UUID
    nombre_archivo: str
    tipo: TipoDocumento
    resultado_analisis: ResultadoAnalisis
    observado: bool = Field(description="OBSERVADO: indexado para RAG, pero sin aportar filas GRI ni sanciones.")
    completado_en: datetime
    version_catalogo: str
    total_referencias_gri: int = Field(description="Menciones GRI detectadas (catalogadas o no) en el documento.")
    total_sanciones: int


class Evidencia(_Modelo):
    """Una mención tal como la detectó la ingesta. No se reescribe ni se amplía con párrafos vecinos."""

    documento_id: uuid.UUID
    documento_nombre: str
    seccion: str
    rol: str = Field(description="`indice` (mención en un índice GRI) o `cuerpo`.")
    referencia_original: str
    referencia_normalizada: str
    cita: str = Field(description="Cita literal (la línea o fila que contiene la referencia).")
    inicio: int
    fin: int
    cita_inicio: int
    cita_fin: int
    en_codigo: bool = Field(description="La mención está dentro de un bloque de código del Markdown.")
    edicion_mencionada: str | None
    motivos_revision: list[str]
    cita_legible: str = Field(
        default="",
        description="Presentación de `cita` sin marcas de formato (`###`, `**`, `<br>`, barras de tabla), con saltos de línea y "
        "celdas conservados. NO es literal ni sus posiciones sirven de índice: la evidencia es `cita` con `cita_inicio`/`cita_fin`.",
    )
    seccion_legible: str = Field(default="", description="Presentación de `seccion` sin marcas de formato.")


class AparicionPasaje(_Modelo):
    """Una referencia GRI que respalda un pasaje. Remite a su evidencia original (cita, sección y demás datos) por posición en
    `FilaGri.evidencias`; aquí solo viaja lo necesario para ubicarla."""

    indice_evidencia: int = Field(description="Posición de la evidencia original en `FilaGri.evidencias`.")
    referencia_original: str
    inicio: int = Field(description="Posición de la referencia en el texto original del documento (inicio inclusivo).")
    fin: int = Field(description="Fin exclusivo de la referencia.")


class PasajeEvidencia(_Modelo):
    """Presentación agrupada: referencias de la MISMA identidad GRI y del MISMO documento cuyas citas son el mismo pasaje o
    ventanas solapadas de él. No sustituye ni elimina `evidencias`: cada aparición sigue existiendo y se cuenta una vez."""

    documento_id: uuid.UUID
    documento_nombre: str
    seccion_legible: str
    rol: str
    cita_legible: str = Field(
        description="Presentación del pasaje: la cita compartida o la unión de las ventanas solapadas, sin marcas de formato. "
        "El texto original de cada aparición está en su evidencia."
    )
    cita_inicio: int = Field(description="Inicio del pasaje en el texto original del documento (unión de las ventanas).")
    cita_fin: int = Field(description="Fin exclusivo del pasaje en el texto original.")
    total_apariciones: int
    apariciones: list[AparicionPasaje]


class AdvertenciaFila(_Modelo):
    codigo: str
    mensaje: str
    total_menciones: int
    referencias: list[str] = Field(description="Hasta 20 referencias originales afectadas.")


class FilaGri(_Modelo):
    identidad: str = Field(description="Clave estable de la fila; es el `{identidad}` de los endpoints.", pattern=PATRON_IDENTIDAD)
    codigo: str
    edicion: str | None
    identidad_tipo: TipoIdentidadGri
    estandar: str = Field(description="Etiqueta legible, p. ej. «GRI 305 (2016)» o «GRI 102 (edición sin resolver)».")
    nombre: str | None = Field(description="Nombre del catálogo; null si la edición no está resuelta.")
    candidatos: list[str] = Field(description="Ediciones catalogadas del código (para revisar una identidad ambigua).")
    requiere_revision: bool
    estado: EstadoGri | None = None
    estado_etiqueta: str | None = None
    version_fila: int = Field(description="0 si no tiene estado; se envía como `version_fila_esperada` al asignar.")
    asignado_por: ActorResumen | None = None
    asignado_en: datetime | None = None
    total_menciones: int
    menciones_en_indice: int
    menciones_en_cuerpo: int
    documentos: list[uuid.UUID]
    advertencias: list[AdvertenciaFila]
    evidencias: list[Evidencia]
    pasajes: list[PasajeEvidencia] = Field(
        default_factory=list,
        description="Las mismas evidencias agrupadas por pasaje para presentarlas sin repeticiones artificiales. "
        "`total_menciones` y `evidencias` no cambian.",
    )


class AdvertenciaAnalisis(_Modelo):
    """Advertencia que NO es una fila evaluable: referencia desconocida o aviso de calidad del documento."""

    codigo: str
    categoria: str
    mensaje: str
    documento_id: uuid.UUID | None = None
    documento_nombre: str | None = None
    detalles: dict[str, Any] = Field(default_factory=dict)
    evidencias: list[Evidencia] = Field(default_factory=list)


class DatoTexto(_Modelo):
    texto: str
    inicio: int
    fin: int


class MontoVista(_Modelo):
    texto: str = Field(description="Texto original del documento («59.078 UIT», «S/ 5,000»). No se convierte.")
    unidad: str
    numero_texto: str
    separador_ambiguo: bool = Field(description="El separador decimal/de miles es ambiguo; NO se interpreta.")
    vinculo: str = Field(
        description="Cómo se unió el monto a la sanción (p. ej. `termino_y_conector`, `fila_de_tabla`, `encabezado_de_columna`)."
    )
    inicio: int
    fin: int
    unidad_inicio: int | None = Field(
        default=None,
        description="Si la moneda no está dentro de `texto` (la nombra el encabezado de la columna o una celda «Moneda»), "
        "posición inicial donde está escrita en el documento original; null si la moneda forma parte de `texto`.",
    )
    unidad_fin: int | None = Field(default=None, description="Posición final (exclusiva) de la moneda; ver `unidad_inicio`.")


class ReferenciaFormalVista(_Modelo):
    """Resolución o expediente que el texto une a la sanción."""

    tipo: str = Field(description="`resolucion` o `expediente`.")
    texto: str = Field(description="Frase literal («Resolución Directoral N° 0123-2023-OEFA/DFAI»).")
    identificador: str = Field(description="Solo el identificador literal.")
    completo: bool = Field(description="Tiene al menos dos componentes; solo un identificador completo sirve para agrupar.")
    inicio: int
    fin: int


class CalificadorVista(_Modelo):
    codigo: str
    texto: str
    inicio: int
    fin: int


class SancionVista(_Modelo):
    documento_id: uuid.UUID
    documento_nombre: str
    tipo_termino: str
    referencia_original: str
    cita: str
    inicio: int
    fin: int
    cita_inicio: int
    cita_fin: int
    seccion: str
    cita_legible: str = Field(default="", description="Presentación de `cita` sin marcas de formato (no literal).")
    seccion_legible: str = Field(default="", description="Presentación de `seccion` sin marcas de formato.")
    en_tabla: bool
    entidad: DatoTexto | None = Field(description="null si la evidencia no identifica una entidad; no se inventa.")
    monto: MontoVista | None = Field(description="null = no cuantificada (nunca cero).")
    cuantificada: bool
    periodo: DatoTexto | None
    calificadores: list[CalificadorVista]
    motivos_revision: list[str]
    agrupacion_id: str | None = Field(
        default=None,
        description="Id de la agrupación (o mención individual) de `agrupaciones_sanciones` a la que pertenece. null = el "
        "documento se procesó sin agrupación: la mención NO fue evaluada.",
    )
    referencias: list[ReferenciaFormalVista] = Field(default_factory=list)
    numeral: DatoTexto | None = None
    infraccion: DatoTexto | None = None


class EstadoImporte(str, enum.Enum):
    CUANTIFICADO = "CUANTIFICADO"  # un importe explícito y no ambiguo
    REQUIERE_REVISION = "REQUIERE_REVISION"  # «Importe que requiere revisión»: separador ambiguo o cifras literales distintas
    NO_ESPECIFICADO = "NO_ESPECIFICADO"  # el documento dice expresamente que el importe no consta o está por determinar
    NO_CONSTA = "NO_CONSTA"  # ningún importe unido a la sanción (no es cero)


class EvaluacionAgrupacion(str, enum.Enum):
    NO_APLICA = "NO_APLICA"  # no hay menciones de sanciones
    NO_EVALUADA = "NO_EVALUADA"  # ningún documento con sanciones trae agrupación
    PARCIAL = "PARCIAL"  # algún documento no la trae o no se compararon todas sus menciones
    COMPLETA = "COMPLETA"  # todos los documentos con sanciones se agruparon (no implica «sin ambigüedades»)


class CitaSancion(_Modelo):
    """Una evidencia de una agrupación, con su procedencia. El mismo texto en dos posiciones son dos evidencias."""

    documento_id: uuid.UUID
    documento_nombre: str
    seccion: str
    en_tabla: bool
    referencia_original: str
    inicio: int
    fin: int
    cita: str
    cita_inicio: int
    cita_fin: int
    cita_legible: str = Field(default="", description="Presentación de `cita` sin marcas de formato (no literal).")
    seccion_legible: str = Field(default="", description="Presentación de `seccion` sin marcas de formato.")


class AdvertenciaSancion(_Modelo):
    codigo: str = Field(description="`POSIBLE_SANCION_DUPLICADA`, `DATOS_EN_CONFLICTO` o `IMPORTE_A_REVISAR`.")
    mensaje: str = Field(description="«Posible sanción duplicada», «Importe que requiere revisión»…")
    con: list[str] = Field(default_factory=list, description="Ids de las otras agrupaciones implicadas.")
    campo: str | None = None


class AgrupacionSancion(_Modelo):
    """Una agrupación de menciones con identidad explícita (`menciones` ≥ 2) o una mención individual. NO es un total
    definitivo de sanciones únicas: mientras haya advertencias, el conteo puede incluir duplicados o separar de más."""

    id: str
    documento_id: uuid.UUID
    documento_nombre: str
    agrupada: bool = Field(description="True si reúne dos o más menciones.")
    criterio: str = Field(description="`identidad_explicita` o `mencion_individual`.")
    coincidencias: list[str] = Field(description="Datos que justificaron agrupar: autoridad, identificador, numeral, infraccion, importe.")
    menciones: int
    autoridad: DatoTexto | None = Field(description="Autoridad que sanciona (no la empresa); null si el texto no la identifica.")
    identificadores: list[ReferenciaFormalVista]
    numeral: DatoTexto | None
    infraccion: DatoTexto | None
    estado_importe: EstadoImporte
    importes: list[MontoVista] = Field(description="Importes originales de sus menciones (sin convertir ni sumar).")
    citas: list[CitaSancion]
    advertencias: list[AdvertenciaSancion]


class ResumenSanciones(_Modelo):
    """Solo conteos de MENCIONES. No se suman montos ni monedas distintas; las sanciones no forman parte del puntaje ESG.

    Una mención no es un suceso: el mismo suceso suele aparecer en un párrafo y en una tabla y se cuenta dos veces, y no se
    fusionan menciones aunque coincidan entidad y monto (dos sanciones distintas pueden coincidir)."""

    total: int = Field(description="Menciones de sanciones económicas detectadas (no sucesos independientes).")
    cuantificadas: int = Field(description="Menciones con un monto explícito unido a la sanción.")
    no_cuantificadas: int = Field(
        description="Menciones sin monto: el documento no lo da o dice expresamente que no se especifica (nunca es cero)."
    )
    por_unidad: dict[str, int] = Field(description="Cantidad de sanciones por unidad/moneda escrita en el documento.")
    evaluacion_agrupacion: EvaluacionAgrupacion = EvaluacionAgrupacion.NO_APLICA
    unidades: int | None = Field(
        default=None,
        description="Agrupaciones + menciones individuales de los documentos evaluados. NO es el total de sanciones únicas: "
        "mientras haya advertencias puede incluir duplicados. null si ningún documento fue evaluado.",
    )
    agrupaciones_identificadas: int = Field(default=0, description="Agrupaciones de dos o más menciones con identidad explícita.")
    menciones_agrupadas: int = 0
    menciones_sin_evaluar: int = Field(default=0, description="Menciones de documentos procesados sin agrupación (formato anterior).")
    posibles_duplicados: int = Field(default=0, description="Pares de unidades con «Posible sanción duplicada».")
    unidades_en_conflicto: int = Field(default=0, description="Unidades con «Datos en conflicto».")
    importes_a_revisar: int = Field(default=0, description="Unidades con «Importe que requiere revisión».")


class PuntajeEsg(_Modelo):
    estado: EstadoPuntaje
    definitivo: bool = Field(description="True solo si está calculado sobre una validación vigente.")
    valor: float | None = Field(description="Promedio simple exacto (sin redondear); null si no se presenta.")
    valor_presentacion: float | None = Field(
        description="`valor` redondeado a `decimales_presentacion` decimales, mitad hacia arriba (cálculo exacto con enteros)."
    )
    decimales_presentacion: int
    suma_puntos: int | None
    filas_contadas: int
    filas_evaluables: int
    filas_sin_estado: int
    motivo: str | None = Field(description="Código del motivo si no se presenta valor (`NO_DISPONIBLE`, `PENDIENTE_ESTADOS`).")
    mensaje: str | None = None


class Pendiente(_Modelo):
    codigo: str
    mensaje: str
    cantidad: int | None = None
    identidades: list[str] = Field(default_factory=list)


class ValidacionVista(_Modelo):
    numero: int
    vigente: bool
    motivos_no_vigente: list[MotivoNoVigente]
    validado_por: ActorResumen
    validado_en: datetime
    version_estados: int
    huella_documentos: str
    documentos_validados: int
    filas_validadas: int
    puntaje_suma: int
    puntaje_filas: int


class AnalisisResponse(_Modelo):
    ambiente: str
    empresa: EmpresaResumen
    anio: int
    version: int = Field(description="Versión de los estados (cambia con cada asignación). Se envía al validar.")
    huella_documentos: str = Field(description="Huella del conjunto de documentos elegibles. Se envía al validar.")
    estado_flujo: EstadoFlujo
    habilita_reporte: bool = Field(description="True solo con validación vigente y filas GRI. Informativo: el Módulo 7 lo recalcula.")
    version_catalogo: list[str] = Field(description="Versiones del catálogo GRI con que se analizaron los documentos.")
    documentos: list[DocumentoElegible]
    filas: list[FilaGri]
    advertencias: list[AdvertenciaAnalisis]
    sanciones: list[SancionVista]
    agrupaciones_sanciones: list[AgrupacionSancion] = Field(
        default_factory=list,
        description="Agrupaciones por documento (ver `resumen_sanciones.evaluacion_agrupacion`). Vacía no significa «sin "
        "sanciones» si hay menciones sin evaluar.",
    )
    resumen_sanciones: ResumenSanciones
    puntaje: PuntajeEsg
    pendientes: list[Pendiente]
    validacion: ValidacionVista | None = Field(description="Última validación (vigente o no), o null si nunca se validó.")


# --- Escritura -----------------------------------------------------------------------------------------------------

class AsignarEstadoRequest(BaseModel):
    """El autor NO viaja aquí: sale de la sesión."""

    model_config = ConfigDict(extra="forbid")

    estado: EstadoGri
    version_fila_esperada: int = Field(
        ge=0, description="`version_fila` que el cliente vio (0 si la fila no tenía estado). Si cambió, 409."
    )


class AsignarEstadoResponse(_Modelo):
    identidad: str
    cambio: bool = Field(description="False si el estado pedido ya era el vigente (no se registra historial).")
    estado_anterior: EstadoGri | None
    estado: EstadoGri
    version_fila: int
    version_analisis: int
    asignado_por: ActorResumen
    asignado_en: datetime
    validacion_previa_invalidada: bool = Field(description="True si había una validación vigente que este cambio dejó sin vigencia.")


class ValidarAnalisisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0, description="`version` del análisis que el Administrador consultó.")
    huella_documentos: str = Field(min_length=64, max_length=64, description="`huella_documentos` consultada.")


class ValidarAnalisisResponse(_Modelo):
    validacion: ValidacionVista
    puntaje: PuntajeEsg
    estado_flujo: EstadoFlujo
    habilita_reporte: bool


class HistorialItem(_Modelo):
    id: uuid.UUID
    identidad: str
    codigo: str
    edicion: str | None
    identidad_tipo: TipoIdentidadGri
    estado_anterior: EstadoGri | None
    estado_nuevo: EstadoGri
    usuario: ActorResumen
    cambiado_en: datetime
    version_analisis: int


class PaginaHistorial(_Modelo):
    items: list[HistorialItem]
    total: int
    pagina: int
    tamano: int

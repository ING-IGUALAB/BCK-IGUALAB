"""Modelos de datos del detector de sanciones económicas.

Dataclasses inmutables del dominio (datos literales, montos, calificadores,
hallazgos y resultado) y estructuras internas del recorrido y del análisis.
No contienen lógica: solo datos, sin LLM, OCI ni servicios externos.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Dato:
    """Texto literal del documento con su posición."""

    texto: str
    inicio: int
    fin: int


@dataclass(frozen=True, slots=True)
class MontoSancion:
    # Texto original tal como aparece («59.078 UIT», «S/ 5,000»). No se convierte.
    texto: str
    inicio: int
    fin: int
    # Unidad o moneda escrita en el documento («UIT», «S/», «soles»).
    unidad: str
    numero_texto: str
    separador_ambiguo: bool
    vinculo: str


@dataclass(frozen=True, slots=True)
class Calificador:
    codigo: str
    texto: str
    inicio: int
    fin: int


@dataclass(frozen=True, slots=True)
class SancionDetectada:
    tipo_termino: str
    referencia_original: str
    inicio: int
    fin: int
    cita: str
    cita_inicio: int
    cita_fin: int
    seccion: str
    en_tabla: bool
    base_deteccion: tuple[str, ...]
    entidad: Dato | None
    monto: MontoSancion | None
    periodo: Dato | None
    fechas_en_contexto: tuple[Dato, ...]
    calificadores: tuple[Calificador, ...]
    motivos_revision: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultadoSanciones:
    sanciones: tuple[SancionDetectada, ...]
    # (motivo, cantidad) de menciones descartadas, en orden alfabético.
    descartes: tuple[tuple[str, int], ...] = ()

    @property
    def total(self) -> int:
        return len(self.sanciones)

    @property
    def descartes_por_motivo(self) -> dict[str, int]:
        return dict(self.descartes)


@dataclass(frozen=True, slots=True)
class _Unidad:
    inicio: int
    fin: int
    seccion: str
    tipo: str  # "parrafo" | "celda" | "fila"
    celdas: tuple[tuple[int, int], ...] = ()
    # Texto de los encabezados de la tabla (solo en filas con el número de columnas del encabezado).
    encabezados: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _NumerosDeFila:
    """Cifras de las celdas de una fila distintas de la que contiene el término."""

    montos: list[MontoSancion]  # con unidad y distintos de cero
    sin_unidad: int  # cifras distintas de cero sin moneda ni UIT
    ceros_con_unidad: int  # «S/ 0»
    ceros_sin_unidad: int  # «0»


@dataclass(slots=True)
class _Termino:
    tipo: str
    inicio: int
    fin: int
    anaforico: bool = False


@dataclass(slots=True)
class _Borrador:
    unidad: _Unidad
    termino: _Termino
    clausula: tuple[int, int]
    oraciones: list[tuple[int, int]]
    base: list[str]
    monto: MontoSancion | None
    en_tabla: bool
    motivos: list[str] = field(default_factory=list)
    adicionales: list[_Termino] = field(default_factory=list)

"""Validaciones de admisión de documentos Markdown.

LTX:RF-012, RF-013, RN-018, RN-021, RN-022, RN-024; RNF-007, RNF-011, RNF-015.
Decisiones aplicadas: D07 (tamaño), D08 (sector), D09 (tablas opcionales y
tolerantes: una tabla con columnas inconsistentes NO rechaza el documento, se
conserva como texto literal con una advertencia de calidad; decisión de
2026-10-07), D18 (vacío), D19 (UTF-8/BOM/hash).

No persiste contenido, no llama al proveedor de embeddings y no registra
auditoría: eso pertenece al coordinador de etapas posteriores.
"""
import hashlib
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import BusinessValidationError, PayloadTooLargeError
from app.models import Empresa, SectorEmpresa
from app.services import empresa_service
from app.services.ingesta import markdown
from app.services.ingesta.reglas import (
    EXTENSION_PERMITIDA,
    LONGITUD_MAXIMA_NOMBRE_ARCHIVO,
    TAMANO_MAXIMO_BYTES,
)

# Lector asíncrono compatible con `UploadFile.read(n)`.
LectorBytes = Callable[[int], Awaitable[bytes]]

TAMANO_BLOQUE_LECTURA = 64 * 1024
MAX_ERRORES_TABLA_REPORTADOS = 20

_BOM_UTF8 = b"\xef\xbb\xbf"
# Controles C0 y DEL, salvo tabulación, salto de línea, retorno de carro y
# salto de página (frecuente en Markdown convertido desde PDF).
_CONTROL_NO_PERMITIDO = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")

# Formatos claramente incompatibles que pueden estar compuestos solo por ASCII
# y por tanto superar la decodificación UTF-8 y la búsqueda de controles.
# Alcance acotado: solo firmas en el offset 0 del archivo; no es un detector
# general de formatos (un PDF con bytes previos a la cabecera, que la
# especificación tolera hasta 1024, no se reconoce). Markdown no tiene firma
# propia y nada de esto se exige: solo se rechaza lo inequívocamente ajeno.
_CABECERA_PDF = re.compile(rb"%PDF-\d\.\d")
# Acepta lo mismo que `\d+\s+\d+\s+obj\b` sin imponer límites propios a dígitos ni blancos: ese patrón
# era cuadrático al buscar en 64 KiB de dígitos. El inicio solo en el comienzo de una racha de dígitos y
# los cuantificadores posesivos (el siguiente símbolo nunca puede pertenecer a la racha) evitan el retroceso.
_OBJETO_PDF = re.compile(rb"(?<!\d)\d++\s++\d++\s++obj\b")
_FIN_PDF = b"%%EOF"
_FIRMAS_SIN_CORROBORACION = (
    (b"%!PS-Adobe-", "PostScript"),
    (b"{\\rtf", "RTF"),
)
# Ventanas donde se busca la estructura que corrobora un PDF.
_VENTANA_OBJETOS_PDF = 64 * 1024
_VENTANA_FIN_PDF = 1024


@dataclass(frozen=True)
class ArchivoLeido:
    contenido: bytes
    sha256: str


@dataclass(frozen=True)
class InconsistenciaTabla:
    """Fila de una tabla pipes cuyo número de columnas difiere del encabezado.

    Solo localiza: no corrige ni completa nada. `inicio` es la posición en
    caracteres del inicio de la fila dentro del texto interpretado (sin BOM), la
    misma base que los offsets de los fragmentos; `linea` y `linea_tabla` (línea
    del encabezado de la tabla) empiezan en 1."""

    linea: int
    inicio: int
    linea_tabla: int
    columnas_esperadas: int
    columnas_encontradas: int


@dataclass(frozen=True)
class DiagnosticoTablas:
    """Resultado de revisar las tablas pipes. `total_inconsistencias` es el
    recuento real; `detalles` conserva solo las primeras
    `MAX_ERRORES_TABLA_REPORTADOS`, en orden de documento."""

    tablas: int = 0
    tablas_inconsistentes: int = 0
    total_inconsistencias: int = 0
    detalles: tuple[InconsistenciaTabla, ...] = ()

    @property
    def tiene_inconsistencias(self) -> bool:
        return self.total_inconsistencias > 0


ADVERTENCIA_TABLAS_INCONSISTENTES = "MARKDOWN_TABLE_INCONSISTENT"


@dataclass(frozen=True)
class AdvertenciaCalidad:
    """Aviso que no impide la admisión. Es independiente de OBSERVADO: ese
    resultado depende de los hallazgos del detector, no del formato."""

    codigo: str
    mensaje: str
    detalles: dict


@dataclass(frozen=True)
class DocumentoValidado:
    nombre_archivo: str
    # Bytes originales sin modificar, BOM incluido (D19).
    contenido: bytes
    # SHA-256 de `contenido` (RNF-011).
    sha256: str
    # Texto interpretado, sin el BOM inicial.
    texto: str
    tiene_bom: bool
    diagnostico_tablas: DiagnosticoTablas = DiagnosticoTablas()

    @property
    def tamano_bytes(self) -> int:
        return len(self.contenido)

    @property
    def tablas(self) -> int:
        return self.diagnostico_tablas.tablas

    @property
    def advertencias(self) -> tuple[AdvertenciaCalidad, ...]:
        diagnostico = self.diagnostico_tablas
        advertencias: list[AdvertenciaCalidad] = []
        if diagnostico.tiene_inconsistencias:
            advertencias.append(
                AdvertenciaCalidad(
                    codigo=ADVERTENCIA_TABLAS_INCONSISTENTES,
                    mensaje=(
                        "El documento contiene tablas Markdown con un número de columnas "
                        "inconsistente. Se conservan como texto literal, sin completar "
                        "celdas ni reconstruir datos; sus valores no deben leerse como "
                        "una estructura fila/columna fiable."
                    ),
                    detalles={
                        "tablas": diagnostico.tablas,
                        "tablas_inconsistentes": diagnostico.tablas_inconsistentes,
                        "total_inconsistencias": diagnostico.total_inconsistencias,
                        "inconsistencias": [asdict(d) for d in diagnostico.detalles],
                    },
                )
            )
        return tuple(advertencias)


async def obtener_empresa_activa(
    db: AsyncSession,
    empresa_id: uuid.UUID,
    sector_declarado: SectorEmpresa | None = None,
) -> Empresa:
    """El sector se toma de la empresa; el del formulario solo se contrasta."""
    empresa = await empresa_service.obtener_empresa(db, empresa_id)
    if not empresa.activa:
        raise BusinessValidationError(
            "COMPANY_INACTIVE",
            "La empresa está inactiva; no se pueden ingerir documentos.",
        )
    if sector_declarado is not None and sector_declarado != empresa.sector:
        raise BusinessValidationError(
            "COMPANY_SECTOR_MISMATCH",
            "El sector indicado no corresponde a la empresa seleccionada.",
        )
    return empresa


_SEPARADORES_DE_RUTA = ("/", "\\")
# Cc: controles C0, DEL y C1 (incluye \t, \n, \r, \x85); Zl/Zp: separadores de
# línea y de párrafo Unicode (U+2028, U+2029).
_CATEGORIAS_NO_PERMITIDAS_EN_NOMBRE = ("Cc", "Zl", "Zp")


def _caracter_no_permitido_en_nombre(caracter: str) -> bool:
    return (
        caracter in _SEPARADORES_DE_RUTA
        or unicodedata.category(caracter) in _CATEGORIAS_NO_PERMITIDAS_EN_NOMBRE
    )


def validar_nombre_archivo(nombre_archivo: str | None) -> str:
    """El nombre es solo metadata: nunca una ruta de almacenamiento (el original
    se guardará con un identificador generado por el servidor). Se admiten
    espacios, tildes, ñ y demás Unicode imprimible; se rechazan controles,
    saltos de línea y los separadores `/` y `\\`. La extensión es un requisito,
    no una prueba del contenido (RNF-007)."""
    if not nombre_archivo or not nombre_archivo.strip():
        raise BusinessValidationError(
            "INVALID_FILE_NAME", "Debe adjuntar un archivo con nombre."
        )
    if len(nombre_archivo) > LONGITUD_MAXIMA_NOMBRE_ARCHIVO:
        raise BusinessValidationError(
            "INVALID_FILE_NAME",
            f"El nombre del archivo no puede superar {LONGITUD_MAXIMA_NOMBRE_ARCHIVO} caracteres.",
        )
    if any(_caracter_no_permitido_en_nombre(c) for c in nombre_archivo):
        # No se repite el nombre recibido en el mensaje.
        raise BusinessValidationError(
            "INVALID_FILE_NAME",
            "El nombre del archivo no puede contener caracteres de control, "
            "saltos de línea ni los separadores de ruta «/» y «\\».",
        )
    if not nombre_archivo.lower().endswith(EXTENSION_PERMITIDA):
        raise BusinessValidationError(
            "INVALID_FILE_TYPE", "Solo se admiten archivos Markdown (.md)."
        )
    return nombre_archivo


async def leer_archivo_limitado(
    leer: LectorBytes,
    limite_bytes: int = TAMANO_MAXIMO_BYTES,
    tamano_bloque: int = TAMANO_BLOQUE_LECTURA,
) -> ArchivoLeido:
    """Cuenta los bytes realmente leídos; no usa Content-Length ni `size` declarados."""
    huella = hashlib.sha256()
    contenido = bytearray()
    while True:
        # Nunca se pide más de lo necesario para detectar el primer byte excedente.
        bloque = await leer(min(tamano_bloque, limite_bytes + 1 - len(contenido)))
        if not bloque:
            break
        contenido.extend(bloque)
        if len(contenido) > limite_bytes:
            raise PayloadTooLargeError(
                "FILE_TOO_LARGE",
                "El archivo supera el tamaño máximo permitido de "
                f"{format(limite_bytes, '_').replace('_', ' ')} bytes.",
                details={"tamano_maximo_bytes": limite_bytes},
            )
        huella.update(bloque)
    return ArchivoLeido(contenido=bytes(contenido), sha256=huella.hexdigest())


def decodificar_utf8(contenido: bytes) -> tuple[str, bool]:
    """Decodificación estricta; omite solo el BOM inicial al interpretar (D19)."""
    try:
        texto = contenido.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BusinessValidationError(
            "INVALID_ENCODING",
            "El archivo no está codificado en UTF-8 válido.",
            details={"posicion_byte": exc.start},
        ) from None
    tiene_bom = contenido.startswith(_BOM_UTF8)
    if tiene_bom:
        texto = texto[1:]
    return texto, tiene_bom


def detectar_formato_incompatible(contenido: bytes) -> str | None:
    """Nombre del formato ajeno a Markdown, o None si no se reconoce ninguno.

    PDF: cabecera `%PDF-n.n` en el offset 0 MÁS estructura propia de un PDF
    (un objeto `n n obj` en los primeros 64 KiB o `%%EOF` en el último KiB). La
    cabecera sola no basta, de modo que un Markdown que empiece mencionándola no
    se rechaza; una mención en cualquier otra posición ni siquiera se mira.
    PostScript y RTF se reconocen por su firma inequívoca en el offset 0.
    No modifica `contenido`; las búsquedas no copian los bytes.
    """
    if _CABECERA_PDF.match(contenido):
        longitud = len(contenido)
        if (
            _OBJETO_PDF.search(contenido, 0, _VENTANA_OBJETOS_PDF)
            or contenido.find(_FIN_PDF, max(0, longitud - _VENTANA_FIN_PDF)) != -1
        ):
            return "PDF"
    for firma, nombre in _FIRMAS_SIN_CORROBORACION:
        if contenido.startswith(firma):
            return nombre
    return None


def validar_formato(contenido: bytes) -> None:
    formato = detectar_formato_incompatible(contenido)
    if formato is not None:
        raise BusinessValidationError(
            "INVALID_FILE_CONTENT",
            f"El contenido del archivo corresponde a un documento {formato}, no a Markdown. "
            "Conviértalo a Markdown antes de cargarlo.",
            details={"formato_detectado": formato},
        )


def _numero_linea(texto: str, posicion: int) -> int:
    return texto.count("\n", 0, posicion) + 1


def validar_texto(texto: str) -> None:
    control = _CONTROL_NO_PERMITIDO.search(texto)
    if control is not None:
        raise BusinessValidationError(
            "INVALID_FILE_CONTENT",
            "El archivo contiene caracteres de control propios de un archivo binario; no es texto Markdown.",
            details={"linea": _numero_linea(texto, control.start())},
        )
    # D18: vacío o solo espacios, tabulaciones o saltos de línea (BOM ya omitido).
    if not texto.strip():
        raise BusinessValidationError(
            "EMPTY_DOCUMENT",
            "El documento está vacío o solo contiene espacios en blanco.",
        )


def _inicia_tabla(lineas: list[str], i: int) -> bool:
    return i + 1 < len(lineas) and markdown.es_inicio_tabla(lineas[i], lineas[i + 1])


class _ErroresDeTabla:
    """Cuenta todas las inconsistencias pero conserva solo las primeras
    `MAX_ERRORES_TABLA_REPORTADOS` del documento, entre todas las tablas.
    Guarda números de línea; los offsets se calculan después solo para esas."""

    def __init__(self) -> None:
        self.total = 0
        # (línea, línea del encabezado de la tabla, esperadas, encontradas)
        self.detalles: list[tuple[int, int, int, int]] = []

    def registrar(self, linea: int, esperadas: int, encontradas: int, linea_tabla: int = 0) -> None:
        self.total += 1
        if len(self.detalles) < MAX_ERRORES_TABLA_REPORTADOS:
            self.detalles.append((linea, linea_tabla, esperadas, encontradas))


def _revisar_tabla(lineas: list[str], inicio: int, errores: _ErroresDeTabla) -> tuple[int, bool]:
    """Compara cada fila con el encabezado en `inicio`, registra las diferencias
    en `errores` y devuelve (índice de la primera línea posterior a la tabla, si
    la tabla tuvo alguna inconsistencia)."""
    esperadas = len(markdown.celdas(lineas[inicio]))
    antes = errores.total
    j = inicio + 1
    while j < len(lineas) and markdown.puede_ser_fila(lineas[j]):
        encontradas = len(markdown.celdas(lineas[j]))
        if encontradas != esperadas:
            errores.registrar(j + 1, esperadas, encontradas, inicio + 1)
        j += 1
    return j, errores.total > antes


def _offsets_de_linea(texto: str, lineas: set[int]) -> dict[int, int]:
    """Posición en caracteres del inicio de cada línea pedida (base 1), con los
    mismos terminadores que `markdown.dividir_lineas`. Un solo recorrido, que se
    detiene en la última línea necesaria."""
    if not lineas:
        return {}
    ultima = max(lineas)
    offsets = {1: 0}
    for numero, terminador in enumerate(markdown.FIN_DE_LINEA.finditer(texto), start=2):
        if numero > ultima:
            break
        offsets[numero] = terminador.end()
    return {linea: offsets[linea] for linea in lineas}


def diagnosticar_tablas(texto: str) -> DiagnosticoTablas:
    """D09: tablas pipes opcionales y tolerantes. Si una fila tiene un número de
    columnas distinto del encabezado se registra como inconsistencia; el
    documento NO se rechaza y el texto no se modifica.

    Simplificación respecto de GFM (ver `markdown`): la tabla termina en la
    primera línea que no puede ser fila, y se ignoran las tablas dentro de
    bloques de código cercados. Las líneas se separan solo en LF, CRLF y CR.

    Se recorre todo el documento aunque haya muchas inconsistencias: el total es
    real, pero los detalles guardados nunca pasan de `MAX_ERRORES_TABLA_REPORTADOS`.
    """
    lineas = markdown.dividir_lineas(texto)
    errores = _ErroresDeTabla()
    tablas = 0
    tablas_inconsistentes = 0
    cerca_abierta: str | None = None
    i = 0
    while i < len(lineas):
        cerca_abierta, es_codigo = markdown.actualizar_cerca(lineas[i], cerca_abierta)
        if es_codigo or not _inicia_tabla(lineas, i):
            i += 1
            continue
        tablas += 1
        i, inconsistente = _revisar_tabla(lineas, i, errores)
        tablas_inconsistentes += inconsistente

    offsets = _offsets_de_linea(texto, {d[0] for d in errores.detalles})
    detalles = tuple(
        InconsistenciaTabla(
            linea=linea,
            inicio=offsets[linea],
            linea_tabla=linea_tabla,
            columnas_esperadas=esperadas,
            columnas_encontradas=encontradas,
        )
        for linea, linea_tabla, esperadas, encontradas in errores.detalles
    )
    return DiagnosticoTablas(
        tablas=tablas,
        tablas_inconsistentes=tablas_inconsistentes,
        total_inconsistencias=errores.total,
        detalles=detalles,
    )


async def validar_archivo(
    nombre_archivo: str | None,
    leer: LectorBytes,
    limite_bytes: int = TAMANO_MAXIMO_BYTES,
) -> DocumentoValidado:
    """Valida el archivo en orden: nombre, tamaño real, UTF-8, formato ajeno y
    contenido. Diagnostica las tablas, pero no rechaza el documento por ellas."""
    nombre = validar_nombre_archivo(nombre_archivo)
    archivo = await leer_archivo_limitado(leer, limite_bytes)
    texto, tiene_bom = decodificar_utf8(archivo.contenido)
    validar_formato(archivo.contenido)
    validar_texto(texto)
    diagnostico_tablas = diagnosticar_tablas(texto)
    return DocumentoValidado(
        nombre_archivo=nombre,
        contenido=archivo.contenido,
        sha256=archivo.sha256,
        texto=texto,
        tiene_bom=tiene_bom,
        diagnostico_tablas=diagnostico_tablas,
    )

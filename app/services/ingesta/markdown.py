"""Reconocimiento mínimo de Markdown compartido por validación y fragmentación.

Un único criterio de «línea», «bloque de código cercado», «encabezado ATX» y
«tabla pipes» evita que el validador (Etapa 1) y el fragmentador (Etapa 2)
discrepen sobre qué es una tabla o un bloque de código.

No es un intérprete completo de CommonMark/GFM. Simplificaciones conocidas:
- Solo se reconocen encabezados ATX (`# Título`); los setext (`Título\\n====`)
  se tratan como texto corriente.
- Una tabla termina en la primera línea que no puede ser fila: sin pipe sin
  escapar, con sangría de 4 o más, o que inicia otro bloque (encabezado ATX,
  cerca de código o cita).
- No se interpretan listas, citas ni front matter como estructuras propias.

Las funciones que reciben `(texto, inicio, fin)` trabajan sobre el rango
`texto[inicio:fin]` sin copiarlo; con los valores por defecto se aplican a
todo `texto`, de modo que también sirven para una línea suelta.
"""
import re

# Terminadores de línea de Markdown. NO se usa `str.splitlines()`: también
# parte en U+2028, U+2029, U+0085, \x0b, \x0c y \x1c-\x1e, que en Markdown no
# separan líneas y pueden aparecer dentro de una celda.
FIN_DE_LINEA = re.compile(r"\r\n|\r|\n")

_BLANCA = re.compile(r"\s*")
_SANGRIA = re.compile(r"[ \t]*")
_CELDA_DELIMITADORA = re.compile(r":?-+:?")
_PIPE_NO_ESCAPADO = re.compile(r"(?<!\\)\|")
_ENCABEZADO_ATX = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*")
# CommonMark: la información de una cerca de acentos graves no puede contener
# acentos graves (``` código ``` en línea no abre un bloque).
_APERTURA_CERCA = re.compile(r" {0,3}(?:(`{3,})[^`]*|(~{3,}).*)")
# La cerca de cierre no admite información después de la marca.
_CIERRE_CERCA = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
_CITA = re.compile(r" {0,3}>")


def _hasta(texto: str, fin: int | None) -> int:
    return len(texto) if fin is None else fin


def limites_de_linea(texto: str, inicio: int) -> tuple[int, int]:
    """Devuelve (fin del contenido, fin incluido el terminador) de la línea que
    empieza en `inicio`. La última línea puede carecer de terminador."""
    encontrado = FIN_DE_LINEA.search(texto, inicio)
    if encontrado is None:
        return len(texto), len(texto)
    return encontrado.start(), encontrado.end()


def dividir_lineas(texto: str) -> list[str]:
    """Líneas sin terminador, como en `splitlines()` para \\n, \\r\\n y \\r."""
    lineas = FIN_DE_LINEA.split(texto)
    if lineas and lineas[-1] == "":
        lineas.pop()
    return lineas


def es_blanca(texto: str, inicio: int = 0, fin: int | None = None) -> bool:
    return _BLANCA.fullmatch(texto, inicio, _hasta(texto, fin)) is not None


# --- Bloques de código cercados ----------------------------------------------

def marca_apertura_cerca(texto: str, inicio: int = 0, fin: int | None = None) -> str | None:
    """Marca (```... o ~~~...) si la línea abre un bloque de código cercado."""
    encontrado = _APERTURA_CERCA.fullmatch(texto, inicio, _hasta(texto, fin))
    if encontrado is None:
        return None
    return encontrado.group(1) or encontrado.group(2)


def cierra_cerca(texto: str, marca: str, inicio: int = 0, fin: int | None = None) -> bool:
    """Solo cierra una marca del mismo carácter, al menos igual de larga y sin
    nada más en la línea."""
    encontrado = _CIERRE_CERCA.fullmatch(texto, inicio, _hasta(texto, fin))
    if encontrado is None:
        return False
    cierre = encontrado.group(1)
    return cierre[0] == marca[0] and len(cierre) >= len(marca)


def actualizar_cerca(
    texto: str,
    cerca_abierta: str | None,
    inicio: int = 0,
    fin: int | None = None,
) -> tuple[str | None, bool]:
    """Devuelve el nuevo estado del bloque de código cercado y si la línea
    pertenece a él (incluidas sus marcas de apertura y cierre)."""
    if cerca_abierta is None:
        marca = marca_apertura_cerca(texto, inicio, fin)
        return (marca, True) if marca else (None, False)
    if cierra_cerca(texto, cerca_abierta, inicio, fin):
        return None, True
    return cerca_abierta, True


# --- Encabezados -------------------------------------------------------------

def titulo_atx(texto: str, inicio: int = 0, fin: int | None = None) -> tuple[int, str] | None:
    """(nivel, título sin marcas) si la línea es un encabezado ATX."""
    encontrado = _ENCABEZADO_ATX.fullmatch(texto, inicio, _hasta(texto, fin))
    if encontrado is None:
        return None
    titulo = (encontrado.group(2) or "").strip()
    # «## ###»: lo único que sigue es la secuencia de cierre; el título está vacío.
    return len(encontrado.group(1)), "" if not titulo.strip("#") else titulo


def inicia_otro_bloque(texto: str, inicio: int = 0, fin: int | None = None) -> bool:
    """Encabezado ATX, cerca de código o cita: interrumpen una tabla."""
    limite = _hasta(texto, fin)
    return (
        _ENCABEZADO_ATX.fullmatch(texto, inicio, limite) is not None
        or _APERTURA_CERCA.fullmatch(texto, inicio, limite) is not None
        or _CITA.match(texto, inicio, limite) is not None
    )


# --- Tablas pipes ------------------------------------------------------------

def celdas(linea: str) -> list[str]:
    contenido = linea.strip()
    if contenido.startswith("|"):
        contenido = contenido[1:]
    if contenido.endswith("|") and not contenido.endswith("\\|"):
        contenido = contenido[:-1]
    return [celda.strip() for celda in _PIPE_NO_ESCAPADO.split(contenido)]


def rangos_de_celdas(texto: str, inicio: int, fin: int) -> list[tuple[int, int]]:
    """Rangos (inicio, fin) de cada celda de la fila `texto[inicio:fin]`, sin
    espacios en los extremos. Mismo criterio que `celdas`:
    `[texto[a:b] for a, b in rangos_de_celdas(...)] == celdas(texto[inicio:fin])`."""
    linea = texto[inicio:fin]
    izquierda = len(linea) - len(linea.lstrip())
    derecha = len(linea.rstrip())
    desde, hasta = izquierda, derecha
    if linea[desde:hasta].startswith("|"):
        desde += 1
    if linea[desde:hasta].endswith("|") and not linea[desde:hasta].endswith("\\|"):
        hasta -= 1
    rangos = []
    cursor = desde
    for separador in _PIPE_NO_ESCAPADO.finditer(linea, desde, hasta):
        rangos.append(_recortar(linea, cursor, separador.start(), inicio))
        cursor = separador.end()
    rangos.append(_recortar(linea, cursor, hasta, inicio))
    return rangos


def _recortar(linea: str, desde: int, hasta: int, base: int) -> tuple[int, int]:
    segmento = linea[desde:hasta]
    izquierda = len(segmento) - len(segmento.lstrip())
    inicio_util = desde + izquierda
    fin_util = max(inicio_util, desde + len(segmento.rstrip()))
    return base + inicio_util, base + fin_util


def _sangria(texto: str, inicio: int, fin: int) -> int:
    return len(_SANGRIA.match(texto, inicio, fin).group().expandtabs(4))


def tiene_pipe(texto: str, inicio: int = 0, fin: int | None = None) -> bool:
    return _PIPE_NO_ESCAPADO.search(texto, inicio, _hasta(texto, fin)) is not None


def puede_ser_fila(texto: str, inicio: int = 0, fin: int | None = None) -> bool:
    """Línea con pipe sin escapar, sin sangría de código y que no inicia otro
    bloque. Una prosa con pipes sueltas solo es fila si ya está dentro de una
    tabla (encabezado + delimitador previos)."""
    limite = _hasta(texto, fin)
    return (
        _sangria(texto, inicio, limite) < 4
        and tiene_pipe(texto, inicio, limite)
        and not inicia_otro_bloque(texto, inicio, limite)
    )


def es_delimitador(linea: str) -> bool:
    return puede_ser_fila(linea) and all(
        _CELDA_DELIMITADORA.fullmatch(celda) for celda in celdas(linea)
    )


def es_inicio_tabla(linea: str, siguiente: str) -> bool:
    """Encabezado con pipes seguido de una fila delimitadora."""
    return (
        puede_ser_fila(linea)
        and not es_delimitador(linea)
        and es_delimitador(siguiente)
    )

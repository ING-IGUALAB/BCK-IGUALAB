"""Recorrido estructural y utilidades de texto del detector de sanciones.

Recorre el Markdown en párrafos, filas de tabla y celdas, y segmenta oraciones
respetando abreviaturas, iniciales («S.A.») y «S/.».
"""
import re
from collections.abc import Iterator

from app.modules.ingesta.domain import markdown

from .modelos import _Unidad

SEPARADOR_RUTA = " > "


class _UnionDePatrones:
    """Varios patrones de palabras completas con raíces distintas (sus coincidencias nunca se solapan) usados como
    uno solo: `finditer` entrega las coincidencias de todos por orden de posición."""

    def __init__(self, *patrones: re.Pattern[str]) -> None:
        self._patrones = patrones

    def finditer(self, texto: str, pos: int = 0, endpos: int | None = None):
        fin = len(texto) if endpos is None else endpos
        coincidencias = [m for patron in self._patrones for m in patron.finditer(texto, pos, fin)]
        return iter(sorted(coincidencias, key=lambda m: m.start()))


# --- Estructura ----------------------------------------------------------------------------------------
_FIN_ORACION = re.compile(r"[.!?]+[\"”’)\]]*\s+")
_ABREVIATURAS = frozenset(
    "sr sra srta ing dr dra lic art arts inc cía cia ltda av núm num nro no n s sa sac etc vs pág pp ej aprox "
    "tel dpto mg prof gral cap".split()
)
_MARCA_DE_LISTA = re.compile(r"[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+")


class _RecorridoDeUnidades:
    """Estado del recorrido línea a línea de `_unidades`."""

    def __init__(self, texto: str) -> None:
        self.texto = texto
        self.pila: list[tuple[int, str]] = []
        self.cerca: str | None = None
        self.parrafo: list[int] | None = None  # [inicio, fin]
        self.n_columnas: int | None = None
        self.encabezados: tuple[str, ...] = ()
        self.delimitador_pendiente = False

    def _seccion(self) -> str:
        return SEPARADOR_RUTA.join(t for _, t in self.pila if t)

    def _cerrar_parrafo(self) -> Iterator[_Unidad]:
        if self.parrafo is not None:
            unidad = _Unidad(self.parrafo[0], self.parrafo[1], self._seccion(), "parrafo")
            self.parrafo = None
            yield unidad

    def recorrer(self) -> Iterator[_Unidad]:
        texto = self.texto
        longitud = len(texto)
        posicion = 0
        while posicion < longitud:
            fin_linea, siguiente = markdown.limites_de_linea(texto, posicion)
            yield from self._linea(posicion, fin_linea, siguiente)
            posicion = siguiente
        yield from self._cerrar_parrafo()

    def _linea(self, posicion: int, fin_linea: int, siguiente: int) -> Iterator[_Unidad]:
        texto = self.texto
        self.cerca, es_codigo = markdown.actualizar_cerca(texto, self.cerca, posicion, fin_linea)
        if es_codigo:
            self.n_columnas = None
            yield from self._cerrar_parrafo()
            return
        titulo = markdown.titulo_atx(texto, posicion, fin_linea)
        if titulo is not None:
            self.n_columnas = None
            yield from self._cerrar_parrafo()  # con la sección anterior: el encabezado aún no la cambia
            nivel, texto_titulo = titulo
            while self.pila and self.pila[-1][0] >= nivel:
                self.pila.pop()
            self.pila.append((nivel, texto_titulo))
            return
        if self.delimitador_pendiente:
            self.delimitador_pendiente = False
            return
        if self.n_columnas is not None:
            if markdown.puede_ser_fila(texto, posicion, fin_linea):
                yield from self._fila(posicion, fin_linea)
                return
            self.n_columnas = None
        if markdown.es_blanca(texto, posicion, fin_linea):
            yield from self._cerrar_parrafo()
            return
        if self._inicia_tabla(posicion, fin_linea, siguiente):
            yield from self._cerrar_parrafo()
            rangos = markdown.rangos_de_celdas(texto, posicion, fin_linea)
            self.n_columnas = len(rangos)
            self.encabezados = tuple(texto[a:b] for a, b in rangos)
            self.delimitador_pendiente = True
            return
        yield from self._parrafo(posicion, fin_linea)

    def _inicia_tabla(self, posicion: int, fin_linea: int, siguiente: int) -> bool:
        return markdown.inicia_tabla(self.texto, posicion, fin_linea, siguiente)

    def _fila(self, posicion: int, fin_linea: int) -> Iterator[_Unidad]:
        celdas = markdown.rangos_de_celdas(self.texto, posicion, fin_linea)
        if len(celdas) == self.n_columnas:
            yield _Unidad(posicion, fin_linea, self._seccion(), "fila", tuple(celdas), self.encabezados)
            return
        for a, b in celdas:
            if b > a:
                yield _Unidad(a, b, self._seccion(), "celda")

    def _parrafo(self, posicion: int, fin_linea: int) -> Iterator[_Unidad]:
        texto = self.texto
        inicio_contenido = posicion + len(texto[posicion:fin_linea]) - len(texto[posicion:fin_linea].lstrip())
        if self.parrafo is not None and _MARCA_DE_LISTA.match(texto, posicion, fin_linea):
            yield from self._cerrar_parrafo()
        if self.parrafo is None:
            self.parrafo = [inicio_contenido, fin_linea]
        else:
            self.parrafo[1] = fin_linea


def _unidades(texto: str) -> Iterator[_Unidad]:
    """Párrafos, filas de tabla (si tienen el número de columnas del encabezado) y
    celdas sueltas (si no). Omite encabezados, bloques de código y la cabecera de
    las tablas."""
    return _RecorridoDeUnidades(texto).recorrer()


def _es_letra(caracter: str) -> bool:
    """Letra en sentido Unicode: alfanumérico que no es dígito decimal (ni «_»)."""
    return caracter.isalnum() and not caracter.isdecimal()


def _ultima_palabra(ventana: str) -> str:
    """Última racha de letras al final de `ventana`; si termina en otro símbolo no blanco, ese símbolo; si termina en
    blanco (o está vacía), cadena vacía."""
    if ventana.endswith("\n"):  # `$` de la expresión equivalente admite un salto de línea final
        ventana = ventana[:-1]
    if not ventana or ventana[-1].isspace():
        return ""
    inicio = len(ventana)
    while inicio > 0 and _es_letra(ventana[inicio - 1]):
        inicio -= 1
    return ventana[inicio:] if inicio < len(ventana) else ventana[-1]


def _punto_cierra_oracion(texto: str, inicio: int, punto: re.Match[str]) -> bool:
    """False si el «.» es de una abreviatura, una inicial («S.A.»), «S/.» o va seguido de minúscula;
    un dígito o «)» antes del punto sí cierran la oración."""
    token = _ultima_palabra(texto[max(inicio, punto.start() - 12):punto.start()])
    if token and (token.lower() in _ABREVIATURAS or (len(token) == 1 and (token.isalpha() or token == "/"))):
        return False
    return not texto[punto.end()].islower()


def _oraciones(texto: str, inicio: int, fin: int) -> list[tuple[int, int]]:
    """Oraciones de [inicio, fin): corta tras . ! ? y espacio, salvo abreviaturas,
    iniciales, «S/.» y cuando lo que sigue empieza en minúscula."""
    cortes = [inicio]
    for encontrado in _FIN_ORACION.finditer(texto, inicio, fin):
        siguiente = encontrado.end()
        if siguiente >= fin:
            continue
        puntuacion = texto[encontrado.start():encontrado.end()].strip()
        if puntuacion.startswith(".") and not _punto_cierra_oracion(texto, inicio, encontrado):
            continue
        cortes.append(siguiente)
    cortes.append(fin)
    oraciones = []
    for a, b in zip(cortes, cortes[1:]):
        while a < b and texto[a].isspace():
            a += 1
        while b > a and texto[b - 1].isspace():
            b -= 1
        if b > a:
            oraciones.append((a, b))
    return oraciones

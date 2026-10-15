"""Vista auxiliar de lectura del Markdown con vínculo exacto al texto original.

Los detectores léxicos buscan relaciones entre palabras («sanción … por un monto de …»). El formato en línea del
Markdown las interrumpe aunque no cambie el significado: `**sanción** administrativa`, `*multa*`, `[sanción](url)`,
`sanción[^1]`, una barra invertida de salto duro, `<br>`, el prefijo `>` de una cita. Esta vista es una COPIA DE LECTURA
sin esas marcas; el documento original no se toca y su SHA-256 tampoco.

Contrato (lo que permite citar el original):
- La vista solo BORRA caracteres o los SUSTITUYE de uno en uno (p. ej. `<br>` → un espacio); nunca reordena ni inserta.
  Cada posición de la vista corresponde a EXACTAMENTE una posición del original (`inicio_en_original`).
- Un tramo `plano[i:j]` equivale al tramo contiguo `original[inicio_en_original(i):fin_en_original(j)]`, que incluye las
  marcas que quedaron DENTRO del tramo («sanción **económica**») y deja fuera las de los extremos. `fin_con_marcas`
  y `inicio_con_marcas` amplían un extremo a las marcas adyacentes (para citas con el formato equilibrado).
- No se tocan los bloques de código cercados ni los fragmentos `código en línea`, las marcas de lista (`-`, `*`, `1.`),
  los encabezados `#`, las tablas (`|`, `---`) ni los caracteres escapados (`\\*`). Un `*` o `_` solitario (rodeado de
  espacios), el asterisco entre dos cifras («3*4») y el guion bajo dentro de una palabra («un_nombre») se conservan.

Límites: no es un intérprete de CommonMark. Se decide cada marca por su contexto inmediato, sin emparejar aperturas y
cierres; una marca sin pareja se retira igual (no cambia el sentido de las palabras). No se interpretan tablas HTML.
"""
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from functools import lru_cache

from app.services.ingesta import markdown

# Cualquier carácter que pueda formar parte de una marca que se retire; si la línea no tiene ninguno se copia tal cual.
_RASGOS = re.compile(r"[*_~\[<&\\>]")
_CITA_INICIAL = re.compile(r" {0,3}(?:>[ \t]?)+")
_RACHA_DE_ACENTOS_GRAVES = re.compile(r"`+")
_DELIMITADOR = re.compile(r"(?<!\\)(?:\*{1,3}|_{1,3}|~~)")
_ENLACE = re.compile(r"(?<![!\\])\[(?P<texto>[^\[\]\n]*)\](?:\((?P<destino>[^()\n]*)\)|\[(?P<referencia>[^\[\]\n]*)\])")
_NOTA_AL_PIE = re.compile(r"(?<!\\)\[\^[^\]\s]+\]")
_SALTO_HTML = re.compile(r"<br\s*/?>", re.IGNORECASE)
_ETIQUETA_EN_LINEA = re.compile(
    r"</?(?:b|i|u|em|strong|small|sup|sub|mark|span|font)(?:\s[^<>\n]*)?>", re.IGNORECASE
)
_ENTIDAD_ESPACIO = re.compile(r"&(?:nbsp|#160|#xa0);", re.IGNORECASE)
_BARRA_FINAL = re.compile(r"(?<!\\)\\$")


@dataclass(frozen=True, slots=True)
class VistaPlana:
    original: str
    plano: str
    _cortes: tuple[int, ...] = ()
    _deltas: tuple[int, ...] = ()
    # índice en `plano` de un carácter SUSTITUIDO → fin (exclusivo) de la construcción que sustituyó en el original
    _fines_de_sustitucion: dict[int, int] = field(default_factory=dict, compare=False)

    @property
    def es_identidad(self) -> bool:
        return not self._cortes

    def _delta(self, i: int) -> int:
        k = bisect_right(self._cortes, i) - 1
        return self._deltas[k] if k >= 0 else 0

    def inicio_en_original(self, i: int) -> int:
        """Posición del original que corresponde al carácter `plano[i]` (también `i == len(plano)` → fin del original)."""
        if i >= len(self.plano):
            return len(self.original)
        return i + self._delta(i)

    def fin_en_original(self, j: int) -> int:
        """Fin exclusivo, en el original, del tramo que termina en `plano[j-1]` (sin las marcas posteriores)."""
        if j <= 0:
            return 0
        return self.inicio_en_original(j - 1) + 1

    def inicio_con_marcas(self, i: int) -> int:
        """Como `inicio_en_original`, pero incluye las marcas retiradas inmediatamente antes de `plano[i]`."""
        if i <= 0:
            return 0
        if i - 1 in self._fines_de_sustitucion:  # carácter sustituido (p. ej. «<br>» → espacio): no se parte
            return self._fines_de_sustitucion[i - 1]
        return self.inicio_en_original(i - 1) + 1

    def fin_con_marcas(self, j: int) -> int:
        """Como `fin_en_original`, pero incluye las marcas retiradas inmediatamente después de `plano[j-1]`."""
        return self.inicio_en_original(j)


def _es_signo(caracter: str) -> bool:
    return unicodedata.category(caracter)[0] in "PS"


def _fragmentos_de_codigo(linea: str, desde: int) -> list[tuple[int, int]]:
    """Rangos de los fragmentos `código en línea` de la línea (CommonMark): una racha de N acentos graves abre un fragmento que
    termina en la SIGUIENTE racha de exactamente N; si no existe, la racha es texto. Recorrido lineal: un regex con `.*?` y
    retrovinculación se vuelve super-lineal con muchas rachas sin cierre (una línea hostil podía bloquear la ingesta)."""
    rachas = [m.span() for m in _RACHA_DE_ACENTOS_GRAVES.finditer(linea, desde)]
    por_longitud: dict[int, list[int]] = {}
    for indice, (a, b) in enumerate(rachas):
        por_longitud.setdefault(b - a, []).append(indice)
    fragmentos: list[tuple[int, int]] = []
    i = 0
    while i < len(rachas):
        a, b = rachas[i]
        mismas = por_longitud[b - a]
        k = bisect_right(mismas, i)
        if k < len(mismas):
            cierre = mismas[k]
            fragmentos.append((a, rachas[cierre][1]))
            i = cierre + 1
        else:
            i += 1
    return fragmentos


def _interseca(protegidos: list[tuple[int, int]], a: int, b: int) -> bool:
    """¿[a, b) toca algún fragmento protegido? `protegidos` está ordenado y sin solapes (búsqueda binaria)."""
    k = bisect_right(protegidos, (b - 1, float("inf"))) - 1
    return k >= 0 and protegidos[k][1] > a


def _es_blanco(caracter: str) -> bool:
    """Sin carácter (límite de línea) o un espacio."""
    return not caracter or caracter.isspace()


def _flanqueo(antes: str, despues: str) -> tuple[bool, bool]:
    """(flanquea por la izquierda, flanquea por la derecha): una marca abre si lo que sigue es texto y cierra si lo que la
    precede lo es; un signo de puntuación pegado a la marca solo cuenta si el otro lado es un blanco o un signo."""
    izquierda = not _es_blanco(despues) and (
        not _es_signo(despues) or _es_blanco(antes) or _es_signo(antes)
    )
    derecha = not _es_blanco(antes) and (
        not _es_signo(antes) or _es_blanco(despues) or _es_signo(despues)
    )
    return izquierda, derecha


@lru_cache(maxsize=4096)
def _es_enfasis(marca: str, antes: str, despues: str) -> bool:
    """¿Esta racha de `*`, `_` o `~~` es formato (y se retira) y no parte del texto?

    Función pura de tres cadenas de a lo sumo tres caracteres: en un documento con formato abundante se evalúa cientos de miles de
    veces con unas pocas combinaciones distintas, por eso se memoiza (acotada: no crece sin límite con caracteres raros)."""
    if _es_blanco(antes) and _es_blanco(despues):
        return False  # solitaria: marca de lista, multiplicación o separador
    if marca[0] == "_" and antes.isalnum() and despues.isalnum():
        return False  # el guion bajo dentro de una palabra («un_nombre») no es énfasis
    if marca[0] == "*" and antes.isdigit() and despues.isdigit():
        return False  # «3*4»
    izquierda, derecha = _flanqueo(antes, despues)
    return izquierda or derecha


def _marcas_de_enfasis(linea: str, protegidos: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Ediciones (inicio, fin, sustituto) de las marcas de énfasis `*`, `_` y `~~` de la línea."""
    ediciones = []
    for m in _DELIMITADOR.finditer(linea):
        a, b = m.span()
        if _interseca(protegidos, a, a + 1):
            continue
        antes = linea[a - 1] if a > 0 else ""
        despues = linea[b] if b < len(linea) else ""
        if _es_enfasis(m.group(), antes, despues):
            ediciones.append((a, b, ""))
    return ediciones


def _ediciones_de_linea(linea: str) -> list[tuple[int, int, str]]:
    """Ediciones (inicio, fin, sustituto) de una línea que no es de código, ordenadas y sin solaparse."""
    ediciones: list[tuple[int, int, str]] = []
    cita = _CITA_INICIAL.match(linea)
    arranque = 0
    if cita is not None and cita.end() > 0:
        ediciones.append((0, cita.end(), ""))
        arranque = cita.end()
    protegidos = _fragmentos_de_codigo(linea, arranque)

    def libre(a: int, b: int) -> bool:
        return not _interseca(protegidos, a, b)

    for m in _ENLACE.finditer(linea, arranque):
        if not libre(*m.span()):
            continue
        ediciones.append((m.start(), m.start("texto"), ""))  # «[»
        ediciones.append((m.end("texto"), m.end(), ""))  # «](destino)» o «][ref]»
    for patron, sustituto in (
        (_NOTA_AL_PIE, ""),
        (_SALTO_HTML, " "),
        (_ETIQUETA_EN_LINEA, ""),
        (_ENTIDAD_ESPACIO, " "),
        (_BARRA_FINAL, ""),
    ):
        for m in patron.finditer(linea, arranque):
            if libre(*m.span()):
                ediciones.append((m.start(), m.end(), sustituto))
    ediciones += [e for e in _marcas_de_enfasis(linea, protegidos) if e[0] >= arranque]
    ediciones.sort()
    # Descarta solapes (p. ej. una marca de énfasis dentro del destino de un enlace ya retirado).
    resultado: list[tuple[int, int, str]] = []
    fin_previo = 0
    for a, b, s in ediciones:
        if a < fin_previo:
            continue
        resultado.append((a, b, s))
        fin_previo = b
    return resultado


def construir_vista(texto: str) -> VistaPlana:
    """Vista de lectura de `texto` y su mapa de posiciones. Sin marcas que retirar, `plano is original`."""
    partes: list[str] = []
    cortes: list[int] = []
    deltas: list[int] = []
    sustituciones: dict[int, int] = {}
    longitud_plano = 0
    delta = 0
    copiado = 0  # índice del original hasta donde ya se volcó a `partes`
    cerca: str | None = None
    posicion = 0
    longitud = len(texto)
    while posicion < longitud:
        fin_linea, siguiente = markdown.limites_de_linea(texto, posicion)
        cerca, es_codigo = markdown.actualizar_cerca(texto, cerca, posicion, fin_linea)
        linea = texto[posicion:fin_linea]
        if not es_codigo and _RASGOS.search(linea):
            for a, b, sustituto in _ediciones_de_linea(linea):
                inicio, fin = posicion + a, posicion + b
                tramo = texto[copiado:inicio]
                partes.append(tramo)
                longitud_plano += len(tramo)
                partes.append(sustituto)
                longitud_plano += len(sustituto)
                if sustituto:
                    sustituciones[longitud_plano - 1] = fin
                delta += (fin - inicio) - len(sustituto)
                cortes.append(longitud_plano)
                deltas.append(delta)
                copiado = fin
        posicion = siguiente
    if not cortes:
        return VistaPlana(texto, texto)
    partes.append(texto[copiado:])
    return VistaPlana(texto, "".join(partes), tuple(cortes), tuple(deltas), sustituciones)

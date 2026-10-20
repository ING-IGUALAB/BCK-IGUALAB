"""Datos explícitos que DISTINGUEN una sanción de otra (decisión 2026-10-19): identificador de resolución o expediente,
numeral e infracción codificada. Regla pura y léxica: sin BD, sin LLM, sin nombres de empresas ni de documentos.

POR QUÉ EXISTE. Una resolución puede imponer varias sanciones, así que su número, por sí solo, no identifica una
sanción. Estos datos se extraen para que la agrupación (`agrupacion_sanciones`) pueda exigir, además de la autoridad,
un identificador COMPLETO y una distinción explícita. Aquí solo se extraen; nunca se infieren ni se completan.

QUÉ SE RECONOCE
- Identificador de resolución («Resolución Directoral N° 0123-2023-OEFA/DFAI», «RD 045-2022-SUNAT», «Res. 12-2024/X»)
  y de expediente («Expediente N° 2022-0345-OEFA», «Exp. 99-2021/PAS»). Es COMPLETO si tiene al menos dos componentes
  separados por «-» o «/» y alguna cifra; «Resolución N° 123» sin más se conserva como INCOMPLETO y no sirve para agrupar.
  Una fecha («2023-10-05», «05/10/2023») no es un identificador.
- Numeral («numeral 2.1», «inciso 3», «literal 4», «ítem 5») e infracción codificada («infracción N° 3», «cargo N° 2»).
  Una infracción solo descrita con palabras («incumplimiento del LMP») NO se usa: dos sanciones distintas pueden
  describirse igual, así que el texto libre no distingue. Tampoco una cifra suelta bajo la columna «Infracción»
  (tipo genérico o correlativo): exige el marcador «N°». Solo la infracción codificada IGUAL identifica la sanción concreta
  para agrupar; el numeral únicamente puede distinguir (si difiere), nunca identificar.

LÍMITES
- Español. Los números se comparan por su forma literal normalizada (mayúsculas, sin espacios): «0123-2023» y «123-2023»
  no se consideran el mismo identificador (no se asumen ceros a la izquierda).
- Un identificador en una oración distinta de la sanción no se le atribuye.
"""
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

TIPO_RESOLUCION = "resolucion"
TIPO_EXPEDIENTE = "expediente"

_I = re.IGNORECASE

_MARCADOR_NUMERO = r"(?:(?:n[°º˚]\.?|n[úu]m(?:ero|\.)?|nro?\.?)\s*)"
_SEGMENTO = r"\w+(?:\.\w+)*"
# Al menos dos componentes unidos por «-», «–» o «/».
_TOKEN_COMPLETO = rf"(?P<id>{_SEGMENTO}(?:[-–/]{_SEGMENTO})+)"
_TOKEN_SIMPLE = r"(?P<id>\d+[A-Za-z]?)"

_ADJETIVO_RESOLUCION = (
    r"(?:directoral|subdirectoral|sub-directoral|gerencial|ministerial|suprema|administrativa|jefatural|presidencial|"
    r"ejecutiva|regional|municipal|coactiva|final|firme|"
    r"de\s+(?:superintendencia|gerencia|intendencia|presidencia|sanci[óo]n|multa|alcald[íi]a|concejo))"
)
_PALABRA_RESOLUCION = (
    rf"(?:resoluci[óo]n(?:es)?(?:\s+{_ADJETIVO_RESOLUCION}){{0,2}}|res\.|(?-i:\bR\.?[DSGMAJ]\.?))"
)
_PALABRA_EXPEDIENTE = r"(?:expedientes?(?:\s+(?:administrativo|sancionador))?|exp\.)"

_REFERENCIA_COMPLETA = re.compile(
    rf"(?P<tipo>{_PALABRA_RESOLUCION}|{_PALABRA_EXPEDIENTE})\s*{_MARCADOR_NUMERO}?{_TOKEN_COMPLETO}", _I
)
_REFERENCIA_SIMPLE = re.compile(
    rf"(?P<tipo>{_PALABRA_RESOLUCION}|{_PALABRA_EXPEDIENTE})\s*{_MARCADOR_NUMERO}{_TOKEN_SIMPLE}(?![\w.-])", _I
)
_REFERENCIA_COORDINADA = re.compile(rf"\s*(?:[,;]|\b[ye]\b)\s*(?:{_MARCADOR_NUMERO})?{_TOKEN_COMPLETO}", _I)
_TOKEN_SUELTO = re.compile(_TOKEN_COMPLETO)
_NUMERO_SUELTO = re.compile(r"(?:n[°º˚]\.?\s*)?(?P<id>\d+[A-Za-z]?)", _I)
_FECHA_NUMERICA = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}")

_CODIGO = r"(?P<c>\d+(?:\.\d+)*[A-Za-z]?)(?![\w])"
_NUMERAL = re.compile(rf"\b(?:numeral(?:es)?|incisos?|literal(?:es)?|[ií]tem)\s+(?:{_MARCADOR_NUMERO})?{_CODIGO}", _I)
_INFRACCION = re.compile(rf"\b(?:infracci[óo]n(?:es)?|cargo)\s+{_MARCADOR_NUMERO}{_CODIGO}", _I)
_CODIGO_SUELTO = re.compile(rf"(?:{_MARCADOR_NUMERO})?{_CODIGO}", _I)
_CODIGO_SUELTO_MARCADO = re.compile(rf"{_MARCADOR_NUMERO}{_CODIGO}", _I)

_ENCABEZADO_RESOLUCION = re.compile(r"resoluci[óo]n|\bres\b\.?|acto\s+administrativo", _I)
_ENCABEZADO_EXPEDIENTE = re.compile(r"expediente|\bexp\b\.?", _I)
_ENCABEZADO_NUMERAL = re.compile(r"numeral|inciso|literal|tipificaci[óo]n|[ií]tem", _I)
_ENCABEZADO_INFRACCION = re.compile(r"infracci[óo]n|cargo", _I)

DISTINCION_NUMERAL = "numeral"
DISTINCION_INFRACCION = "infraccion"


@dataclass(frozen=True, slots=True)
class ReferenciaFormal:
    """Resolución o expediente que el texto une a la sanción, con su posición en el texto analizado."""

    tipo: str  # TIPO_RESOLUCION | TIPO_EXPEDIENTE
    texto: str  # frase literal («Resolución Directoral N° 0123-2023-OEFA/DFAI»)
    inicio: int
    fin: int
    identificador: str  # solo el identificador literal («0123-2023-OEFA/DFAI»)
    identificador_inicio: int
    identificador_fin: int
    completo: bool  # al menos dos componentes: solo un identificador completo puede servir para agrupar


@dataclass(frozen=True, slots=True)
class CodigoDistintivo:
    """Numeral o infracción codificada (`texto` es el literal del código, p. ej. «2.1»)."""

    texto: str
    inicio: int
    fin: int


@dataclass(frozen=True, slots=True)
class Distinciones:
    referencias: tuple[ReferenciaFormal, ...] = ()
    numerales: tuple[CodigoDistintivo, ...] = ()
    infracciones: tuple[CodigoDistintivo, ...] = ()


def clave_identificador(identificador: str) -> str:
    """Forma comparable: NFKC, mayúsculas, sin espacios y con un solo tipo de guion. No quita ceros a la izquierda."""
    limpio = unicodedata.normalize("NFKC", identificador).upper().replace("–", "-")
    return re.sub(r"\s+", "", limpio).rstrip(".")


def clave_codigo(codigo: str) -> str:
    return clave_identificador(codigo)


def _tipo_de(palabra: str) -> str:
    return TIPO_EXPEDIENTE if palabra.strip().lower().startswith("exp") else TIPO_RESOLUCION


def _no_es_identificador(identificador: str) -> bool:
    """Una fecha numérica o un token sin ninguna cifra no identifica una resolución ni un expediente."""
    return _FECHA_NUMERICA.fullmatch(identificador) is not None or not any(c.isdigit() for c in identificador)


def _coordinadas(texto: str, desde: int, fin: int, tipo: str) -> list[ReferenciaFormal]:
    """Identificadores coordinados tras el primero («Resoluciones N° 10-2022 y N° 11-2022»): también son del mismo tipo."""
    encontradas: list[ReferenciaFormal] = []
    while (m := _REFERENCIA_COORDINADA.match(texto, desde, fin)) is not None:
        if not _no_es_identificador(m.group("id")):
            encontradas.append(
                ReferenciaFormal(tipo, m.group().strip(" ,;"), m.start("id"), m.end("id"), m.group("id"), m.start("id"), m.end("id"), True)
            )
        desde = m.end()
    return encontradas


def _referencias_en(texto: str, inicio: int, fin: int) -> list[ReferenciaFormal]:
    encontradas: list[ReferenciaFormal] = []
    ocupadas: list[tuple[int, int]] = []
    for m in _REFERENCIA_COMPLETA.finditer(texto, inicio, fin):
        if _no_es_identificador(m.group("id")):
            continue
        tipo = _tipo_de(m.group("tipo"))
        encontradas.append(
            ReferenciaFormal(tipo, m.group(), m.start(), m.end(), m.group("id"), m.start("id"), m.end("id"), True)
        )
        coordinadas = _coordinadas(texto, m.end(), fin, tipo)
        encontradas += coordinadas
        ocupadas.append((m.start(), coordinadas[-1].fin if coordinadas else m.end()))
    for m in _REFERENCIA_SIMPLE.finditer(texto, inicio, fin):
        if any(a <= m.start() < b for a, b in ocupadas):
            continue
        encontradas.append(
            ReferenciaFormal(
                _tipo_de(m.group("tipo")), m.group(), m.start(), m.end(), m.group("id"), m.start("id"), m.end("id"), False
            )
        )
    return encontradas


def _codigos_en(patron: re.Pattern[str], texto: str, inicio: int, fin: int) -> list[CodigoDistintivo]:
    return [CodigoDistintivo(m.group("c"), m.start("c"), m.end("c")) for m in patron.finditer(texto, inicio, fin)]


def _sin_repetidos(codigos: Iterable[CodigoDistintivo]) -> list[CodigoDistintivo]:
    vistos: set[str] = set()
    unicos = []
    for c in sorted(codigos, key=lambda x: x.inicio):
        if clave_codigo(c.texto) not in vistos:
            vistos.add(clave_codigo(c.texto))
            unicos.append(c)
    return unicos


def buscar_en_rangos(texto: str, rangos: Iterable[tuple[int, int]]) -> tuple[list[ReferenciaFormal], list[CodigoDistintivo], list[CodigoDistintivo]]:
    """Referencias, numerales e infracciones escritos dentro de `rangos` (oraciones del hallazgo o celdas de su fila)."""
    referencias: list[ReferenciaFormal] = []
    numerales: list[CodigoDistintivo] = []
    infracciones: list[CodigoDistintivo] = []
    for a, b in rangos:
        referencias += _referencias_en(texto, a, b)
        numerales += _codigos_en(_NUMERAL, texto, a, b)
        infracciones += _codigos_en(_INFRACCION, texto, a, b)
    return referencias, numerales, infracciones


def rol_de_encabezado(encabezado: str) -> str | None:
    """`TIPO_RESOLUCION`, `TIPO_EXPEDIENTE`, `DISTINCION_NUMERAL`, `DISTINCION_INFRACCION` o None según el encabezado de columna."""
    if _ENCABEZADO_EXPEDIENTE.search(encabezado):
        return TIPO_EXPEDIENTE
    if _ENCABEZADO_RESOLUCION.search(encabezado):
        return TIPO_RESOLUCION
    if _ENCABEZADO_INFRACCION.search(encabezado):
        return DISTINCION_INFRACCION
    if _ENCABEZADO_NUMERAL.search(encabezado):
        return DISTINCION_NUMERAL
    return None


def referencias_de_celda(texto: str, inicio: int, fin: int, tipo: str) -> list[ReferenciaFormal]:
    """Referencias de una celda bajo una columna de resolución/expediente. Además de la frase completa, acepta que la celda
    sea solo el identificador («0123-2023-OEFA/DFAI»); el tipo lo da el encabezado."""
    internas = _referencias_en(texto, inicio, fin)
    if internas:
        return internas
    cuerpo = texto[inicio:fin]
    recorte_a = inicio + (len(cuerpo) - len(cuerpo.lstrip()))
    recorte_b = fin - (len(cuerpo) - len(cuerpo.rstrip()))
    if recorte_b <= recorte_a:
        return []
    literal = texto[recorte_a:recorte_b]
    completo = _TOKEN_SUELTO.fullmatch(literal)
    if completo is not None and not _no_es_identificador(literal):
        return [ReferenciaFormal(tipo, literal, recorte_a, recorte_b, literal, recorte_a, recorte_b, True)]
    simple = _NUMERO_SUELTO.fullmatch(literal)
    if simple is not None:
        inicio_id, fin_id = recorte_a + simple.start("id"), recorte_a + simple.end("id")
        return [ReferenciaFormal(tipo, literal, recorte_a, recorte_b, simple.group("id"), inicio_id, fin_id, False)]
    return []


def codigos_de_celda(texto: str, inicio: int, fin: int, rol: str) -> list[CodigoDistintivo]:
    """Código de una celda bajo una columna de numeral o infracción. Una descripción en palabras no se usa. Una cifra suelta
    bajo «Infracción» («1», «3.2») puede ser el tipo genérico o un correlativo, no la sanción concreta: solo se acepta con su
    marcador («N° 3»), igual que en una oración."""
    es_infraccion = rol == DISTINCION_INFRACCION
    internos = _codigos_en(_INFRACCION if es_infraccion else _NUMERAL, texto, inicio, fin)
    if internos:
        return internos
    cuerpo = texto[inicio:fin]
    recorte_a = inicio + (len(cuerpo) - len(cuerpo.lstrip()))
    recorte_b = fin - (len(cuerpo) - len(cuerpo.rstrip()))
    coincidencia = (_CODIGO_SUELTO_MARCADO if es_infraccion else _CODIGO_SUELTO).fullmatch(texto, recorte_a, recorte_b)
    if coincidencia is None:
        return []
    return [CodigoDistintivo(coincidencia.group("c"), coincidencia.start("c"), coincidencia.end("c"))]


def resolver(
    referencias: Iterable[ReferenciaFormal],
    numerales: Iterable[CodigoDistintivo],
    infracciones: Iterable[CodigoDistintivo],
) -> tuple[Distinciones, list[str]]:
    """Reduce lo encontrado a lo que se puede atribuir con seguridad y devuelve las claves que quedaron ambiguas.

    - Por tipo (resolución/expediente): un solo identificador completo distinto ⇒ se conserva; varios distintos ⇒ ninguno
      de ese tipo y `identificador`. Un identificador incompleto solo se conserva si el tipo no tiene uno completo.
    - Numerales e infracciones: un solo código distinto ⇒ se conserva; varios ⇒ ninguno y `numeral`/`infraccion`.
    No se elige silenciosamente uno de varios."""
    ambiguos: list[str] = []
    conservadas: list[ReferenciaFormal] = []
    todas = sorted(referencias, key=lambda r: r.inicio)
    for tipo in (TIPO_RESOLUCION, TIPO_EXPEDIENTE):
        del_tipo = [r for r in todas if r.tipo == tipo]
        completas = {clave_identificador(r.identificador): r for r in reversed(del_tipo) if r.completo}
        if len(completas) > 1:
            ambiguos.append("identificador")
            continue
        if completas:
            conservadas.append(next(iter(completas.values())))
            continue
        incompletas = {clave_identificador(r.identificador): r for r in reversed(del_tipo)}
        if len(incompletas) > 1:
            ambiguos.append("identificador")
        elif incompletas:
            conservadas.append(next(iter(incompletas.values())))
    resultado_numeral = _sin_repetidos(numerales)
    resultado_infraccion = _sin_repetidos(infracciones)
    if len(resultado_numeral) > 1:
        ambiguos.append("numeral")
        resultado_numeral = []
    if len(resultado_infraccion) > 1:
        ambiguos.append("infraccion")
        resultado_infraccion = []
    distinciones = Distinciones(
        referencias=tuple(sorted(conservadas, key=lambda r: r.inicio)),
        numerales=tuple(resultado_numeral),
        infracciones=tuple(resultado_infraccion),
    )
    return distinciones, ambiguos

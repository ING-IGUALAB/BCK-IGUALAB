"""Agrupación de menciones de sanciones DENTRO DE UN DOCUMENTO (decisión 2026-10-19). Regla pura: sin BD, sin LLM, sin red.

OBJETIVO. El mismo hecho suele aparecer en un párrafo y en una tabla. Cuando hay EVIDENCIA EXPLÍCITA de que dos menciones son la
misma sanción se agrupan (conservando todas las citas); cuando no la hay se dejan separadas y, si procede, se advierte.
`entidad` es la AUTORIDAD que sanciona (OEFA, SUNAFIL…), no la empresa sancionada.

CRITERIO PARA AGRUPAR (todas deben cumplirse entre dos menciones)
1. Misma autoridad (comparación literal normalizada del nombre o de su sigla entre paréntesis).
2. Mismo identificador COMPLETO de resolución o expediente (ver `distinciones_sanciones`). Un número de resolución por sí solo
   no basta: una resolución puede contener varias sanciones.
3. Ninguna distinción explícita contradictoria: ni numeral ni infracción codificada distintos, e importe no distinto.
4. La MISMA INFRACCIÓN CODIFICADA («infracción N° 3», «cargo N° 2», o la columna de infracción/cargo de la fila). Es la única
   distinción que identifica la sanción concreta. Ni el importe ni el numeral bastan: dos multas distintas de una misma
   resolución pueden tener el mismo importe, y el numeral suele ser la tipificación de la norma, compartida por varias
   sanciones. Un dato ausente no contradice, pero tampoco prueba identidad.
Si se cumplen y el conjunto resultante no tiene contradicciones internas, se agrupan (transitivamente). Si el conjunto sí las
tiene, NO se agrupa nada de él y se advierte el conflicto. Autoridad + resolución + importe (sin infracción codificada) NO
agrupa: se conservan separadas con «Posible sanción duplicada».

IMPORTES. Se comparan por su texto literal, sin convertir: misma moneda escrita (sin distinguir mayúsculas ni espacios), misma
cifra literal y misma escala («mil», «millones»). Un importe con separador ambiguo («59.078», «1,500») NUNCA establece igualdad ni
diferencia: no se normaliza ni se interpreta. Dos cifras enteras sin separadores y de la misma escala son distintas si difieren.
Monedas escritas distinto («S/» y «soles») no se comparan: no hay política de conversión ni de equivalencias.

POSIBLE DUPLICADO. Si coinciden autoridad y un importe NO ambiguo en la misma moneda (con o sin el mismo identificador de
resolución) y ningún dato explícito las distingue (identificador, numeral o infracción distintos), las menciones se conservan
separadas con la advertencia «Posible sanción duplicada». No se agrupa por cercanía de fechas, mes/año ni ventanas de días.
Una posible duplicidad NO es un conflicto.

CONFLICTO DE DATOS. Misma autoridad, mismo identificador completo y misma infracción codificada, pero importes distintos
(o identificadores del mismo tipo que se contradicen): se conservan separadas y se advierte. No se elige un valor.

IMPORTE QUE REQUIERE REVISIÓN. Una unidad con un importe de separador ambiguo (o con cifras literales distintas dentro de la
misma agrupación) conserva el texto original, no se suma ni se convierte en cero y lleva «Importe que requiere revisión».
Un importe NO ESPECIFICADO por el documento es otra cosa (`NO_ESPECIFICADO`), igual que la ausencia de importe (`NO_CONSTA`).

LÍMITES
- Solo dentro de un documento: nunca entre documentos ni años.
- Una sanción mencionada sin infracción codificada (solo resolución e importe) no se agrupa aunque sea la misma: se advierte
  como posible duplicado. Es el precio de exigir evidencia explícita de la sanción concreta.
- No hay revisión humana: confirmar, unir o separar agrupaciones queda como mejora futura (ver 05-avance.md).
- El conteo de unidades NO es un total definitivo de sanciones únicas mientras existan advertencias.
"""
import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.modules.ingesta.domain.deteccion_sanciones import (
    MOTIVO_MONTO_NO_ESPECIFICADO,
    Dato,
    MontoSancion,
    SancionDetectada,
)
from app.modules.ingesta.domain.distinciones_sanciones import clave_codigo, clave_identificador

VERSION_AGRUPACION_SANCIONES = "2026-10-19.1"

CRITERIO_IDENTIDAD_EXPLICITA = "identidad_explicita"
CRITERIO_MENCION_INDIVIDUAL = "mencion_individual"

EVALUACION_COMPLETA = "COMPLETA"
EVALUACION_PARCIAL = "PARCIAL"

ADV_POSIBLE_DUPLICADA = "POSIBLE_SANCION_DUPLICADA"
MENSAJE_POSIBLE_DUPLICADA = "Posible sanción duplicada"
ADV_DATOS_EN_CONFLICTO = "DATOS_EN_CONFLICTO"
MENSAJE_DATOS_EN_CONFLICTO = "Las menciones tienen datos que se contradicen; se conservan separadas"
ADV_IMPORTE_A_REVISAR = "IMPORTE_A_REVISAR"
MENSAJE_IMPORTE_A_REVISAR = "Importe que requiere revisión"

IMPORTE_CUANTIFICADO = "CUANTIFICADO"
IMPORTE_REQUIERE_REVISION = "REQUIERE_REVISION"
IMPORTE_NO_ESPECIFICADO = "NO_ESPECIFICADO"
IMPORTE_NO_CONSTA = "NO_CONSTA"

# Con más menciones que esto bajo una misma autoridad no se comparan pares (coste cuadrático): la evaluación queda PARCIAL.
LIMITE_MENCIONES_POR_AUTORIDAD = 500

_IGUAL = "igual"
_DISTINTO = "distinto"
_DESCONOCIDO = "desconocido"
_CONTRADICTORIO = "contradictorio"

_REL_MISMA = "misma"
_REL_POSIBLE_DUPLICADA = "posible_duplicada"
_REL_CONFLICTO = "conflicto"
_REL_NINGUNA = "ninguna"

_SIGLA_ENTRE_PARENTESIS = re.compile(r"\(\s*([A-Za-zÁÉÍÓÚÑ]{2,12})\s*\)")
_ENTERO = re.compile(r"\d+")
_ESCALA = re.compile(r"(mil|millones?|mm)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AdvertenciaUnidad:
    codigo: str
    mensaje: str
    con: tuple[int, ...] = ()  # posiciones (en `unidades`) de las otras unidades implicadas
    campo: str | None = None


@dataclass(frozen=True, slots=True)
class UnidadSancion:
    """Una agrupación (≥ 2 menciones con identidad explícita) o una mención individual. Los índices son posiciones en
    la lista de sanciones del documento, en el orden en que el detector las devolvió."""

    indices: tuple[int, ...]
    criterio: str
    coincidencias: tuple[str, ...]
    estado_importe: str
    advertencias: tuple[AdvertenciaUnidad, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultadoAgrupacion:
    version: str
    evaluacion: str
    unidades: tuple[UnidadSancion, ...]
    menciones: int

    def a_dict(self) -> dict:
        todas = [a for u in self.unidades for a in u.advertencias]
        pares = {
            frozenset((k, otra))
            for k, u in enumerate(self.unidades) for a in u.advertencias if a.codigo == ADV_POSIBLE_DUPLICADA for otra in a.con
        }
        return {
            "version": self.version,
            "evaluacion": self.evaluacion,
            "menciones": self.menciones,
            "unidades": [
                {
                    "indices": list(u.indices),
                    "criterio": u.criterio,
                    "coincidencias": list(u.coincidencias),
                    "estado_importe": u.estado_importe,
                    "advertencias": [
                        {"codigo": a.codigo, "mensaje": a.mensaje, "con": list(a.con), "campo": a.campo}
                        for a in u.advertencias
                    ],
                }
                for u in self.unidades
            ],
            "resumen": {
                "unidades": len(self.unidades),
                "agrupaciones": sum(len(u.indices) > 1 for u in self.unidades),
                "menciones_agrupadas": sum(len(u.indices) for u in self.unidades if len(u.indices) > 1),
                "posibles_duplicados": len(pares),  # pares de unidades
                "unidades_en_conflicto": sum(a.codigo == ADV_DATOS_EN_CONFLICTO for a in todas),
                "importes_a_revisar": sum(a.codigo == ADV_IMPORTE_A_REVISAR for a in todas),
            },
        }


# --- Rasgos comparables de cada mención ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Importe:
    unidad: str
    numero: str
    escala: str


@dataclass(frozen=True, slots=True)
class _Rasgos:
    autoridad: frozenset[str]
    identificadores: dict[str, str]  # tipo → clave del identificador COMPLETO
    numeral: str | None
    infraccion: str | None
    importe: _Importe | None  # None si no hay importe o es ambiguo: no sirve para comparar
    # Dos menciones solo se relacionan si comparten autoridad; `autoridad` vacía = sin autoridad identificada.


def _texto_normalizado(texto: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", texto).casefold().split())


def claves_de_autoridad(entidad: Dato | None) -> frozenset[str]:
    """Nombre normalizado y, si lo hay, su sigla entre paréntesis. Un texto de una sola palabra es él mismo una sigla."""
    if entidad is None:
        return frozenset()
    claves = {_texto_normalizado(entidad.texto)}
    claves.update(_texto_normalizado(s) for s in _SIGLA_ENTRE_PARENTESIS.findall(entidad.texto))
    claves.discard("")
    return frozenset(claves)


def _importe_comparable(monto: MontoSancion | None) -> _Importe | None:
    if monto is None or monto.separador_ambiguo:
        return None
    numero = monto.numero_texto.strip()
    posicion = monto.texto.find(numero)
    if not numero or posicion < 0:
        return None  # la cifra lleva marcas internas: no se puede leer la escala con seguridad
    escala = _ESCALA.match(monto.texto, _saltar_espacios(monto.texto, posicion + len(numero)))
    unidad = re.sub(r"\s+", "", unicodedata.normalize("NFKC", monto.unidad)).casefold()
    return _Importe(unidad, numero, escala.group(1).casefold() if escala else "")


def _saltar_espacios(texto: str, posicion: int) -> int:
    """Primera posición desde `posicion` que no es un espacio. Se hace aquí y no con `\\s*` en el patrón de la escala: un
    `\\s*` inicial, usado con `search`, tiene coste cuadrático sobre una tira larga de espacios."""
    while posicion < len(texto) and texto[posicion].isspace():
        posicion += 1
    return posicion


def _rasgos(s: SancionDetectada) -> _Rasgos:
    identificadores = {r.tipo: clave_identificador(r.identificador) for r in s.referencias if r.completo}
    return _Rasgos(
        autoridad=claves_de_autoridad(s.entidad),
        identificadores=identificadores,
        numeral=clave_codigo(s.numeral.texto) if s.numeral is not None else None,
        infraccion=clave_codigo(s.infraccion.texto) if s.infraccion is not None else None,
        importe=_importe_comparable(s.monto),
    )


# --- Comparación de dos menciones ---------------------------------------------------------------------------------

def _comparar_opcional(a: str | None, b: str | None) -> str:
    if a is None or b is None:
        return _DESCONOCIDO
    return _IGUAL if a == b else _DISTINTO


def _comparar_identificadores(a: dict[str, str], b: dict[str, str]) -> str:
    resultados = {_comparar_opcional(a.get(tipo), b.get(tipo)) for tipo in a.keys() & b.keys()}
    if {_IGUAL, _DISTINTO} <= resultados:
        return _CONTRADICTORIO
    if _IGUAL in resultados:
        return _IGUAL
    return _DISTINTO if _DISTINTO in resultados else _DESCONOCIDO


def _comparar_importes(a: _Importe | None, b: _Importe | None) -> str:
    if a is None or b is None or a.unidad != b.unidad or a.escala != b.escala:
        return _DESCONOCIDO
    if a.numero == b.numero:
        return _IGUAL
    if _ENTERO.fullmatch(a.numero) and _ENTERO.fullmatch(b.numero) and int(a.numero) != int(b.numero):
        return _DISTINTO
    return _DESCONOCIDO


@dataclass(frozen=True, slots=True)
class _Comparacion:
    identificador: str
    numeral: str
    infraccion: str
    importe: str

    @property
    def contradice(self) -> bool:
        return _DISTINTO in (self.identificador, self.numeral, self.infraccion, self.importe) or (
            self.identificador == _CONTRADICTORIO
        )


def _comparar(a: _Rasgos, b: _Rasgos) -> _Comparacion:
    return _Comparacion(
        identificador=_comparar_identificadores(a.identificadores, b.identificadores),
        numeral=_comparar_opcional(a.numeral, b.numeral),
        infraccion=_comparar_opcional(a.infraccion, b.infraccion),
        importe=_comparar_importes(a.importe, b.importe),
    )


def _con_el_mismo_identificador(c: _Comparacion) -> str:
    """Relación de dos menciones de la misma autoridad que comparten un identificador completo.

    Solo una infracción codificada IGUAL identifica la sanción concreta dentro de la resolución. Ni el importe ni el
    numeral lo hacen: dos multas distintas de una resolución pueden costar lo mismo y compartir numeral (el numeral suele
    ser la tipificación de la norma, no la sanción)."""
    if c.numeral == _DISTINTO or c.infraccion == _DISTINTO:
        return _REL_NINGUNA  # la propia resolución las distingue: son sanciones diferentes
    if c.infraccion == _IGUAL:
        return _REL_CONFLICTO if c.importe == _DISTINTO else _REL_MISMA
    if c.importe == _IGUAL:
        return _REL_POSIBLE_DUPLICADA  # misma resolución e importe, sin nada que identifique la sanción: se advierte
    return _REL_NINGUNA


def _relacion(a: _Rasgos, b: _Rasgos) -> tuple[str, _Comparacion]:
    """Relación entre dos menciones de la MISMA autoridad (el llamador ya lo garantiza)."""
    c = _comparar(a, b)
    if c.identificador == _CONTRADICTORIO:
        return _REL_CONFLICTO, c
    if c.identificador == _IGUAL:
        return _con_el_mismo_identificador(c), c
    if c.importe == _IGUAL and not c.contradice:
        return _REL_POSIBLE_DUPLICADA, c
    return _REL_NINGUNA, c


# --- Agrupación -----------------------------------------------------------------------------------------------------

class _Conjuntos:
    def __init__(self, n: int) -> None:
        self.padre = list(range(n))

    def raiz(self, i: int) -> int:
        while self.padre[i] != i:
            self.padre[i] = self.padre[self.padre[i]]
            i = self.padre[i]
        return i

    def unir(self, a: int, b: int) -> None:
        ra, rb = self.raiz(a), self.raiz(b)
        if ra != rb:
            self.padre[max(ra, rb)] = min(ra, rb)


@dataclass(slots=True)
class _Relaciones:
    misma: list[tuple[int, int, _Comparacion]] = field(default_factory=list)
    posible: list[tuple[int, int]] = field(default_factory=list)
    conflicto: list[tuple[int, int]] = field(default_factory=list)
    contradicciones: set[tuple[int, int]] = field(default_factory=set)
    parcial: bool = False


def _por_autoridad(rasgos: Sequence[_Rasgos]) -> dict[str, list[int]]:
    cubetas: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rasgos):
        for clave in r.autoridad:
            cubetas[clave].append(i)
    return cubetas


def _relacionar(rasgos: Sequence[_Rasgos]) -> _Relaciones:
    resultado = _Relaciones()
    vistos: set[tuple[int, int]] = set()
    for indices in _por_autoridad(rasgos).values():
        if len(indices) > LIMITE_MENCIONES_POR_AUTORIDAD:
            resultado.parcial = True
            continue
        for k, i in enumerate(indices):
            for j in indices[k + 1:]:
                if (i, j) in vistos:
                    continue
                vistos.add((i, j))
                _registrar(resultado, i, j, rasgos)
    return resultado


def _registrar(resultado: _Relaciones, i: int, j: int, rasgos: Sequence[_Rasgos]) -> None:
    relacion, comparacion = _relacion(rasgos[i], rasgos[j])
    if comparacion.contradice:
        resultado.contradicciones.add((i, j))
    if relacion == _REL_MISMA:
        resultado.misma.append((i, j, comparacion))
    elif relacion == _REL_POSIBLE_DUPLICADA:
        resultado.posible.append((i, j))
    elif relacion == _REL_CONFLICTO:
        resultado.conflicto.append((i, j))


def _componentes(n: int, relaciones: _Relaciones) -> tuple[list[list[int]], list[list[int]]]:
    """(componentes consistentes de ≥ 2 menciones, componentes con contradicciones internas)."""
    conjuntos = _Conjuntos(n)
    for i, j, _ in relaciones.misma:
        conjuntos.unir(i, j)
    miembros: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        miembros[conjuntos.raiz(i)].append(i)
    consistentes, inconsistentes = [], []
    for grupo in miembros.values():
        if len(grupo) < 2:
            continue
        pertenece = set(grupo)
        contradice = any(i in pertenece and j in pertenece for i, j in relaciones.contradicciones)
        (inconsistentes if contradice else consistentes).append(sorted(grupo))
    return consistentes, inconsistentes


def _literal(monto: MontoSancion) -> tuple[str, str, str]:
    """(cifra, moneda, escala) tal como están escritas; dos importes con el mismo literal son el mismo texto, no solo la misma cifra."""
    escala = _ESCALA.search(monto.texto)
    unidad = re.sub(r"\s+", "", unicodedata.normalize("NFKC", monto.unidad)).casefold()
    return monto.numero_texto.strip(), unidad, escala.group(1).casefold() if escala else ""


def _estado_importe(sanciones: Sequence[SancionDetectada], indices: Sequence[int]) -> str:
    montos = [sanciones[i].monto for i in indices if sanciones[i].monto is not None]
    if montos:
        if any(m.separador_ambiguo for m in montos):
            return IMPORTE_REQUIERE_REVISION
        literales = {_literal(m) for m in montos}
        return IMPORTE_CUANTIFICADO if len(literales) == 1 else IMPORTE_REQUIERE_REVISION
    if any(MOTIVO_MONTO_NO_ESPECIFICADO in sanciones[i].motivos_revision for i in indices):
        return IMPORTE_NO_ESPECIFICADO
    return IMPORTE_NO_CONSTA


def _coincidencias(indices: Sequence[int], relaciones: _Relaciones) -> tuple[str, ...]:
    pertenece = set(indices)
    campos = ["autoridad", "identificador"]
    comparaciones = [c for i, j, c in relaciones.misma if i in pertenece and j in pertenece]
    for nombre in ("numeral", "infraccion", "importe"):
        if any(getattr(c, nombre) == _IGUAL for c in comparaciones):
            campos.append(nombre)
    return tuple(campos)


def _armar_unidades(
    sanciones: Sequence[SancionDetectada], consistentes: list[list[int]], inconsistentes: list[list[int]]
) -> tuple[list[list[int]], set[int]]:
    """Listas de índices por unidad, en orden de primera aparición, y los índices de componentes inconsistentes."""
    agrupados = {i for grupo in consistentes for i in grupo}
    en_conflicto = {i for grupo in inconsistentes for i in grupo}
    unidades = [list(grupo) for grupo in consistentes]
    unidades += [[i] for i in range(len(sanciones)) if i not in agrupados]
    unidades.sort(key=lambda u: u[0])
    return unidades, en_conflicto


def _unidades_vecinas(
    pares: Sequence[tuple[int, int]], unidad_de: dict[int, int], excluidas: set[frozenset[int]] = frozenset()
) -> dict[int, set[int]]:
    """Para cada unidad, las otras unidades con las que algún par de menciones la relaciona (salvo `excluidas`)."""
    vecinas: dict[int, set[int]] = defaultdict(set)
    for i, j in pares:
        a, b = unidad_de[i], unidad_de[j]
        if a != b and frozenset((a, b)) not in excluidas:
            vecinas[a].add(b)
            vecinas[b].add(a)
    return vecinas


def _advertencias_por_unidad(
    unidades: list[list[int]],
    sanciones: Sequence[SancionDetectada],
    relaciones: _Relaciones,
    en_conflicto: set[int],
) -> list[list[AdvertenciaUnidad]]:
    unidad_de = {i: k for k, u in enumerate(unidades) for i in u}
    # Un dato explícito que distingue a dos unidades descarta la duplicidad entre ellas.
    contradichas = {frozenset((unidad_de[i], unidad_de[j])) for i, j in relaciones.contradicciones}
    posibles = _unidades_vecinas(relaciones.posible, unidad_de, contradichas)
    conflictos = _unidades_vecinas(relaciones.conflicto, unidad_de)
    resultado: list[list[AdvertenciaUnidad]] = []
    for k, indices in enumerate(unidades):
        avisos: list[AdvertenciaUnidad] = []
        if _estado_importe(sanciones, indices) == IMPORTE_REQUIERE_REVISION:
            avisos.append(AdvertenciaUnidad(ADV_IMPORTE_A_REVISAR, MENSAJE_IMPORTE_A_REVISAR, campo="importe"))
        if posibles[k] - conflictos[k]:
            avisos.append(AdvertenciaUnidad(ADV_POSIBLE_DUPLICADA, MENSAJE_POSIBLE_DUPLICADA, tuple(sorted(posibles[k]))))
        if conflictos[k] or any(i in en_conflicto for i in indices):
            avisos.append(
                AdvertenciaUnidad(ADV_DATOS_EN_CONFLICTO, MENSAJE_DATOS_EN_CONFLICTO, tuple(sorted(conflictos[k])))
            )
        resultado.append(avisos)
    return resultado


def agrupar_sanciones(sanciones: Sequence[SancionDetectada]) -> ResultadoAgrupacion:
    """Agrupa las menciones de UN documento. Cada mención pertenece a exactamente una unidad."""
    rasgos = [_rasgos(s) for s in sanciones]
    relaciones = _relacionar(rasgos)
    consistentes, inconsistentes = _componentes(len(sanciones), relaciones)
    unidades, en_conflicto = _armar_unidades(sanciones, consistentes, inconsistentes)
    avisos = _advertencias_por_unidad(unidades, sanciones, relaciones, en_conflicto)
    return ResultadoAgrupacion(
        version=VERSION_AGRUPACION_SANCIONES,
        evaluacion=EVALUACION_PARCIAL if relaciones.parcial else EVALUACION_COMPLETA,
        menciones=len(sanciones),
        unidades=tuple(
            UnidadSancion(
                indices=tuple(indices),
                criterio=CRITERIO_IDENTIDAD_EXPLICITA if len(indices) > 1 else CRITERIO_MENCION_INDIVIDUAL,
                coincidencias=_coincidencias(indices, relaciones) if len(indices) > 1 else (),
                estado_importe=_estado_importe(sanciones, indices),
                advertencias=tuple(avisos[k]),
            )
            for k, indices in enumerate(unidades)
        ),
    )

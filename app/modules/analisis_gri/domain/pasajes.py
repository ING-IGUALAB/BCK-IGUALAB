"""Agrupa, SOLO para presentarlas, las evidencias de una fila GRI que corresponden al mismo pasaje del documento.

Por qué existe. La cita de una mención GRI es la línea (o fila de tabla) que la contiene. Si una línea nombra varios códigos de la
misma identidad («GRI 305-1, 305-2 y 305-3») hay una evidencia por referencia y las tres traen la MISMA cita; en una línea larga
las ventanas de dos referencias cercanas se solapan. Mostrarlas sueltas repite el pasaje sin aportar información nueva.

Qué hace. Reúne evidencias de la MISMA fila (identidad GRI) y del MISMO documento y sección cuyas citas ocupan posiciones que se
solapan en el texto original (`cita_inicio`/`cita_fin`) y cuyo texto coincide en la zona común. El pasaje conserva TODAS las
apariciones (cada una con su referencia y sus posiciones) y remite a su evidencia original por índice.

Qué NO hace.
- No elimina ni modifica `evidencias`: los conteos (`total_menciones`, menciones en índice/cuerpo) y la contribución única de
  la identidad al puntaje ESG no dependen de esta agrupación.
- No fusiona por parecido de texto: dos citas iguales en posiciones distintas (otra sección, otro documento) son pasajes
  distintos. El criterio es la posición en el documento, no la similitud.
- No mezcla documentos, secciones ni identidades GRI.
- No reconstruye contenido: la unión de ventanas solapadas se arma solo con texto ya guardado, y solo si las citas coinciden
  exactamente en la zona común (si no, quedan separadas). Una cadena de ventanas que superaría `MAX_LONGITUD_PASAJE` se corta en
  un nuevo pasaje para que la presentación siga siendo legible.
"""
import uuid
from dataclasses import dataclass, field

from app.modules.analisis_gri.schemas import AparicionPasaje, Evidencia, PasajeEvidencia
from app.modules.ingesta.domain.legibilidad import seccion_legible, texto_legible

MAX_LONGITUD_PASAJE = 1200


@dataclass
class _Grupo:
    primera: Evidencia
    inicio: int
    fin: int
    texto: str
    consistente: bool  # la cita guardada mide lo que dicen sus posiciones: solo así se puede unir con otras
    indices: list[int] = field(default_factory=list)

    @classmethod
    def nuevo(cls, indice: int, ev: Evidencia) -> "_Grupo":
        consistente = len(ev.cita) == ev.cita_fin - ev.cita_inicio
        return cls(ev, ev.cita_inicio, ev.cita_fin, ev.cita, consistente, [indice])

    def acepta(self, ev: Evidencia) -> bool:
        if ev.seccion != self.primera.seccion:
            return False
        if self._mismo_rango(ev):
            return ev.cita == self.texto  # la misma cita en la misma posición (varias referencias de una línea)
        if ev.cita_inicio >= self.fin or not self.consistente or len(ev.cita) != ev.cita_fin - ev.cita_inicio:
            return False  # sin solape, o datos que no permiten unir sin suponer
        solape = min(self.fin, ev.cita_fin) - ev.cita_inicio
        if solape <= 0 or self.texto[ev.cita_inicio - self.inicio:ev.cita_inicio - self.inicio + solape] != ev.cita[:solape]:
            return False
        return max(self.fin, ev.cita_fin) - self.inicio <= MAX_LONGITUD_PASAJE

    def _mismo_rango(self, ev: Evidencia) -> bool:
        return (ev.cita_inicio, ev.cita_fin) == (self.inicio, self.fin)

    def agregar(self, indice: int, ev: Evidencia) -> None:
        if ev.cita_fin > self.fin and not self._mismo_rango(ev):
            self.texto += ev.cita[self.fin - ev.cita_inicio:]
            self.fin = ev.cita_fin
        self.indices.append(indice)


def _pasaje(grupo: _Grupo, evidencias: list[Evidencia]) -> PasajeEvidencia:
    ev = grupo.primera
    apariciones = sorted(
        (
            AparicionPasaje(
                indice_evidencia=i,
                referencia_original=evidencias[i].referencia_original,
                inicio=evidencias[i].inicio,
                fin=evidencias[i].fin,
            )
            for i in grupo.indices
        ),
        key=lambda a: (a.inicio, a.fin, a.indice_evidencia),
    )
    return PasajeEvidencia(
        documento_id=ev.documento_id,
        documento_nombre=ev.documento_nombre,
        seccion_legible=ev.seccion_legible or seccion_legible(ev.seccion),
        rol=ev.rol,
        cita_legible=texto_legible(grupo.texto),
        cita_inicio=grupo.inicio,
        cita_fin=grupo.fin,
        total_apariciones=len(apariciones),
        apariciones=apariciones,
    )


def agrupar_pasajes(evidencias: list[Evidencia]) -> list[PasajeEvidencia]:
    """Pasajes de las evidencias de UNA fila: por documento (en orden de primera aparición) y, dentro de él, por posición."""
    por_documento: dict[uuid.UUID, list[int]] = {}
    for i, ev in enumerate(evidencias):
        por_documento.setdefault(ev.documento_id, []).append(i)
    pasajes: list[PasajeEvidencia] = []
    for indices in por_documento.values():
        ordenados = sorted(indices, key=lambda i: (evidencias[i].cita_inicio, evidencias[i].cita_fin, evidencias[i].inicio, i))
        actual: _Grupo | None = None
        for i in ordenados:
            ev = evidencias[i]
            if actual is not None and actual.acepta(ev):
                actual.agregar(i, ev)
                continue
            if actual is not None:
                pasajes.append(_pasaje(actual, evidencias))
            actual = _Grupo.nuevo(i, ev)
        if actual is not None:
            pasajes.append(_pasaje(actual, evidencias))
    return pasajes

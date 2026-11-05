"""Citas agrupadas para presentar: cada texto idéntico se muestra una sola vez con sus menciones y todas sus ubicaciones.

Vocabulario.
- Mención: aparición detectada de una referencia GRI (una `Evidencia`; es lo que cuenta `total_menciones`).
- Pasaje (`pasajes.py`): texto que reúne varias menciones del MISMO documento y sección cuando comparten línea o sus ventanas se
  solapan. Es la unidad posicional; el mismo texto en otra posición o sección es otro pasaje.
- Cita agrupada (este módulo): pasajes de la MISMA fila GRI y del MISMO documento (`documento_id`) cuyo texto de presentación
  (`PasajeEvidencia.cita_legible`) es EXACTAMENTE igual. Se presenta una vez con la suma de sus menciones y la lista de secciones.

Qué NO hace.
- No reemplaza ni cambia la agrupación de `pasajes.py`: parte de sus pasajes tal como salen y no altera sus reglas ni el
  tratamiento de solapamientos.
- No compara por parecido: sin minúsculas, sin quitar puntuación, cifras, acentos ni códigos, sin embeddings ni modelos. La única
  limpieza es la de presentación que ya trae `cita_legible`. El texto comparado es el completo del pasaje (sin truncar).
- No mezcla documentos (aunque se llamen igual: la clave usa `documento_id`) ni identidades o ediciones GRI (se aplica a la
  evidencia de UNA fila, así que las filas distintas nunca comparten grupo).
- No elimina menciones: dos menciones con el mismo texto siguen existiendo (en `evidencias` y en los índices del grupo); solo se
  presentan juntas. `total_menciones`, los conteos de índice y cuerpo y el puntaje ESG no dependen de esta agrupación.

Coste. Una pasada con un diccionario por (documento, texto), sin comparar cada cita con todas las demás. Tiempo esperado
O(P + L + A): P pasajes, A apariciones (menciones) que se recorren una vez para contarlas y guardar sus índices, y L la longitud
total de los textos de los pasajes (cada uno se hashea una vez al usarlo como clave; la igualdad solo recorre el texto si coinciden
los hashes). Memoria adicional: los grupos (G <= P) con sus referencias distintas, las secciones y los índices de evidencia y
de pasaje que guardan (O(G + A + P) enteros y referencias) más el conjunto de menciones ya contadas (A); los textos son los mismos
objetos de los pasajes, no copias.
"""
import uuid
from dataclasses import dataclass, field

from app.modules.analisis_gri.schemas import CitaAgrupada, PasajeEvidencia, UbicacionCita


@dataclass
class _Grupo:
    documento_id: uuid.UUID
    documento_nombre: str
    texto: str
    referencias: dict[str, None] = field(default_factory=dict)  # sin repetidos, en orden de primera aparición
    ubicaciones: dict[str, list[int]] = field(default_factory=dict)  # sección legible ("" = sin sección) -> evidencias
    indices_evidencia: list[int] = field(default_factory=list)
    indices_pasaje: list[int] = field(default_factory=list)


def agrupar_citas(pasajes: list[PasajeEvidencia]) -> list[CitaAgrupada]:
    """Citas agrupadas de UNA fila, a partir de sus pasajes, en orden de primera aparición (el de `pasajes`).

    Cada evidencia se cuenta una sola vez: si un índice ya se contó (en este grupo o en otro) no se vuelve a sumar."""
    grupos: dict[tuple[uuid.UUID, str], _Grupo] = {}
    contadas: set[int] = set()
    for n, pasaje in enumerate(pasajes):
        nuevas = [a for a in pasaje.apariciones if a.indice_evidencia not in contadas]
        if not nuevas:
            continue
        clave = (pasaje.documento_id, pasaje.cita_legible)
        grupo = grupos.get(clave)
        if grupo is None:
            grupo = grupos[clave] = _Grupo(pasaje.documento_id, pasaje.documento_nombre, pasaje.cita_legible)
        grupo.indices_pasaje.append(n)
        ubicacion = grupo.ubicaciones.setdefault(pasaje.seccion_legible, [])
        for aparicion in nuevas:
            contadas.add(aparicion.indice_evidencia)
            grupo.referencias.setdefault(aparicion.referencia_original)
            grupo.indices_evidencia.append(aparicion.indice_evidencia)
            ubicacion.append(aparicion.indice_evidencia)
    return [_citar(g) for g in grupos.values()]


def _citar(grupo: _Grupo) -> CitaAgrupada:
    return CitaAgrupada(
        documento_id=grupo.documento_id,
        documento_nombre=grupo.documento_nombre,
        cita_legible=grupo.texto,
        total_menciones=len(grupo.indices_evidencia),
        referencias=list(grupo.referencias),
        ubicaciones=[
            UbicacionCita(seccion_legible=seccion, total_menciones=len(indices), indices_evidencia=indices)
            for seccion, indices in grupo.ubicaciones.items()
        ],
        indices_evidencia=grupo.indices_evidencia,
        indices_pasaje=grupo.indices_pasaje,
    )

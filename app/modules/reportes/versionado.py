"""Reglas puras del versionado de reportes (BUG-15, LTX:RN-041 reformulada). Sin base de datos ni HTTP.

ÚNICA definición del estado de emisión de una empresa/año: la disponibilidad, la consulta de la última emisión y la descarga del
reporte actualizado llaman a `evaluar`, de modo que estado, `puede_generar`, `motivo_no_generar`, `reporte_actualizado`,
`actualizacion_pendiente` y `validacion_anterior_no_vigente` no pueden contradecirse.

Hechos que se distinguen (no son sinónimos):
a. El reporte RESPALDA la última validación conocida (`respalda_ultima`): solo si puede demostrarse, por el identificador de la
   validación o, en un reporte histórico, por el número Y la huella de documentos que su propio snapshot guardó.
b. Esa validación sigue VIGENTE respecto del análisis actual (`flujo` es `VALIDADO`).
c. Hay cambios pendientes de REVISAR o VALIDAR (el flujo es `PENDIENTE_ESTADOS` o `LISTO_PARA_VALIDAR`).
d. Existe una validación POSTERIOR sin emitir (`actualizacion_pendiente`): hay reporte de origen verificable y la última
   validación conocida no es la que lo respalda.
e. El origen del reporte NO es verificable (`origen_verificable` falso): histórico sin `validacion_id` cuyo snapshot no guardó el
   número o la huella de la validación. No se presume equivalente a ninguna validación.

`reporte_actualizado` es verdadero únicamente si (a) y (b): nunca se deduce de la ausencia de una actualización pendiente.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from app.modules.analisis_gri.schemas import EstadoFlujo
from app.modules.reportes.schemas import EstadoAnioReporte, MotivoNoGenerar


@dataclass(frozen=True)
class ValidacionRef:
    """La última validación de una empresa/año (vigente o no)."""

    id: uuid.UUID
    numero: int
    huella: str


def origen_verificable(*, validacion_id: uuid.UUID | None, numero_snapshot: int | None, huella_snapshot: str | None) -> bool:
    """¿Puede saberse qué validación respalda el reporte? Sí si guarda su `validacion_id` o, siendo histórico, si su snapshot
    conservó el número Y la huella. Con datos insuficientes el vínculo no se reconstruye ni se inventa."""
    return validacion_id is not None or (numero_snapshot is not None and huella_snapshot is not None)


def respalda(
    *, validacion_id: uuid.UUID | None, numero_snapshot: int | None, huella_snapshot: str | None, ultima: ValidacionRef | None
) -> bool:
    """¿El reporte (de esta validación o, si es histórico, de la que su snapshot declara) respalda la validación `ultima`?

    Reporte nuevo: se compara el identificador. Reporte histórico (`validacion_id` nulo, anterior al versionado): el vínculo no se
    reconstruyó en la base; solo se acepta si el número Y la huella de documentos que el propio snapshot guardó coinciden con los de
    la validación. Sin datos suficientes en el snapshot, NO se supone que lo respalda."""
    if ultima is None:
        return False
    if validacion_id is not None:
        return validacion_id == ultima.id
    return numero_snapshot is not None and numero_snapshot == ultima.numero and huella_snapshot == ultima.huella


_MOTIVO_POR_FLUJO = {
    EstadoFlujo.PENDIENTE_ESTADOS: MotivoNoGenerar.PENDIENTE_REVISION,
    EstadoFlujo.LISTO_PARA_VALIDAR: MotivoNoGenerar.PENDIENTE_VALIDACION,
    EstadoFlujo.SIN_CODIGOS_GRI: MotivoNoGenerar.SIN_CODIGOS_GRI,
    EstadoFlujo.DOCUMENTACION_INSUFICIENTE: MotivoNoGenerar.SIN_DOCUMENTACION_ELEGIBLE,
}
_ESTADO_POR_FLUJO = {
    EstadoFlujo.PENDIENTE_ESTADOS: EstadoAnioReporte.PENDIENTE_REVISION,
    EstadoFlujo.LISTO_PARA_VALIDAR: EstadoAnioReporte.PENDIENTE_VALIDACION,
    EstadoFlujo.SIN_CODIGOS_GRI: EstadoAnioReporte.SIN_CODIGOS_GRI,
    EstadoFlujo.DOCUMENTACION_INSUFICIENTE: EstadoAnioReporte.SIN_DOCUMENTACION_ELEGIBLE,
}


@dataclass(frozen=True)
class Evaluacion:
    estado: EstadoAnioReporte
    puede_generar: bool
    motivo_no_generar: MotivoNoGenerar | None
    reporte_actualizado: bool | None  # None si no hay reporte
    actualizacion_pendiente: bool
    validacion_anterior_no_vigente: bool


def _estado(*, hay_reporte: bool, respalda_ultima: bool, origen_verificable: bool, flujo: EstadoFlujo | None) -> EstadoAnioReporte:
    if flujo is not EstadoFlujo.VALIDADO:
        return _ESTADO_POR_FLUJO.get(flujo, EstadoAnioReporte.SIN_DOCUMENTACION_ELEGIBLE)
    if not hay_reporte or not origen_verificable:
        return EstadoAnioReporte.VALIDACION_VIGENTE  # validación vigente sin emisión verificablemente asociada
    return EstadoAnioReporte.REPORTE_GENERADO if respalda_ultima else EstadoAnioReporte.ACTUALIZACION_PENDIENTE


def evaluar(
    *, hay_reporte: bool, origen_verificable: bool, respalda_ultima: bool, hay_validacion: bool, flujo: EstadoFlujo | None
) -> Evaluacion:
    """Estado de emisión de una empresa/año a partir de los hechos (a)–(e).

    `flujo` es el estado del análisis actual (`None` si el año ya no tiene documentos elegibles). `respalda_ultima` y
    `origen_verificable` solo importan si hay reporte. La presencia de un reporte NO oculta el estado actual del análisis:
    solo con la última validación vigente el estado puede ser `REPORTE_GENERADO`, `ACTUALIZACION_PENDIENTE` o
    `VALIDACION_VIGENTE`."""
    vigente = flujo is EstadoFlujo.VALIDADO
    respalda = hay_reporte and respalda_ultima
    pendiente = hay_reporte and origen_verificable and hay_validacion and not respalda_ultima
    puede = vigente and not respalda
    if puede:
        motivo = None
    elif vigente:
        motivo = MotivoNoGenerar.REPORTE_ACTUALIZADO  # respalda la validación vigente: no hay nada nuevo que emitir
    else:
        motivo = _MOTIVO_POR_FLUJO.get(flujo, MotivoNoGenerar.SIN_DOCUMENTACION_ELEGIBLE)
    return Evaluacion(
        estado=_estado(hay_reporte=hay_reporte, respalda_ultima=respalda_ultima, origen_verificable=origen_verificable, flujo=flujo),
        puede_generar=puede,
        motivo_no_generar=motivo,
        reporte_actualizado=(respalda and vigente) if hay_reporte else None,
        actualizacion_pendiente=pendiente,
        validacion_anterior_no_vigente=hay_validacion and not vigente,
    )


class RechazoDeDescarga(str, enum.Enum):
    """Por qué la descarga del reporte ACTUALIZADO se rechaza (cada una es un 409 distinto y documentado)."""

    ACTUALIZACION_PENDIENTE = "REPORT_UPDATE_PENDING"  # hay una validación posterior sin emitir
    ORIGEN_NO_VERIFICABLE = "REPORT_ORIGIN_UNVERIFIABLE"  # histórico sin datos para saber qué validación lo respalda
    ANALISIS_NO_VIGENTE = "REPORT_ANALYSIS_NOT_CURRENT"  # cambios por revisar o validar, o sin documentación/códigos GRI elegibles


class AccionSugerida(str, enum.Enum):
    REVISAR = "REVISAR"
    VALIDAR = "VALIDAR"
    EMITIR = "EMITIR"
    DESCARGAR_VERSION_HISTORICA = "DESCARGAR_VERSION_HISTORICA"


def rechazo_de_descarga(evaluacion: Evaluacion, *, origen_verificable: bool) -> RechazoDeDescarga | None:
    """None si el reporte puede entregarse como actualizado. Prioridad: validación posterior sin emitir; origen no verificable;
    cualquier otra razón por la que no se demuestra que esté actualizado."""
    if evaluacion.actualizacion_pendiente:
        return RechazoDeDescarga.ACTUALIZACION_PENDIENTE
    if not origen_verificable:
        return RechazoDeDescarga.ORIGEN_NO_VERIFICABLE
    if evaluacion.reporte_actualizado is not True:
        return RechazoDeDescarga.ANALISIS_NO_VIGENTE
    return None


def accion_sugerida(evaluacion: Evaluacion) -> AccionSugerida:
    """Qué debe hacer el consumidor para salir del rechazo, sin datos sensibles."""
    if evaluacion.puede_generar:
        return AccionSugerida.EMITIR
    if evaluacion.motivo_no_generar is MotivoNoGenerar.PENDIENTE_REVISION:
        return AccionSugerida.REVISAR
    if evaluacion.motivo_no_generar is MotivoNoGenerar.PENDIENTE_VALIDACION:
        return AccionSugerida.VALIDAR
    return AccionSugerida.DESCARGAR_VERSION_HISTORICA

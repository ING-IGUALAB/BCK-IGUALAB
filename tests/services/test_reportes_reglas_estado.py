"""Reglas puras del estado de emisión de los reportes (BUG-15, entrega 2): estado, `puede_generar`, `motivo_no_generar`,
`reporte_actualizado`, `actualizacion_pendiente` y `validacion_anterior_no_vigente` salen de UNA evaluación y no pueden
contradecirse. Sin base de datos: corren también en CI sin PostgreSQL."""
from __future__ import annotations

import itertools
import uuid

import pytest

from app.modules.analisis_gri.schemas import EstadoFlujo
from app.modules.reportes.schemas import EstadoAnioReporte as E
from app.modules.reportes.schemas import MotivoNoGenerar as M
from app.modules.reportes.versionado import (
    AccionSugerida,
    RechazoDeDescarga,
    ValidacionRef,
    accion_sugerida,
    evaluar,
    origen_verificable,
    rechazo_de_descarga,
    respalda,
)

F = EstadoFlujo
ULTIMA = ValidacionRef(uuid.uuid4(), 3, "h" * 64)
CAMPOS = ("estado", "puede_generar", "motivo_no_generar", "reporte_actualizado", "actualizacion_pendiente", "validacion_anterior_no_vigente")


def _ev(*, reporte: bool, verificable: bool = True, respalda_ultima: bool = False, validacion: bool = True, flujo=F.VALIDADO):
    return evaluar(
        hay_reporte=reporte, origen_verificable=verificable, respalda_ultima=respalda_ultima, hay_validacion=validacion, flujo=flujo
    )


def _tupla(ev) -> tuple:
    return tuple(getattr(ev, c) for c in CAMPOS)


# --- vínculo y verificabilidad -----------------------------------------------------------------------------------------

def test_un_reporte_nuevo_respalda_la_ultima_validacion_solo_por_identificador():
    assert respalda(validacion_id=ULTIMA.id, numero_snapshot=None, huella_snapshot=None, ultima=ULTIMA)
    assert not respalda(validacion_id=uuid.uuid4(), numero_snapshot=3, huella_snapshot=ULTIMA.huella, ultima=ULTIMA)
    assert not respalda(validacion_id=ULTIMA.id, numero_snapshot=None, huella_snapshot=None, ultima=None)


@pytest.mark.parametrize(
    ("numero", "huella", "esperado"),
    [(3, "h" * 64, True), (2, "h" * 64, False), (3, "otra", False), (None, "h" * 64, False), (3, None, False), (None, None, False)],
)
def test_un_historico_solo_respalda_si_el_snapshot_declara_el_mismo_numero_y_huella(numero, huella, esperado):
    assert respalda(validacion_id=None, numero_snapshot=numero, huella_snapshot=huella, ultima=ULTIMA) is esperado


@pytest.mark.parametrize(
    ("validacion_id", "numero", "huella", "esperado"),
    [
        (uuid.uuid4(), None, None, True),  # reporte nuevo: guarda su validación
        (None, 3, "h" * 64, True),  # histórico con número y huella
        (None, 3, None, False),
        (None, None, "h" * 64, False),
        (None, None, None, False),  # histórico sin datos: el origen no puede verificarse
    ],
)
def test_el_origen_es_verificable_con_validacion_o_con_numero_y_huella_del_snapshot(validacion_id, numero, huella, esperado):
    assert origen_verificable(validacion_id=validacion_id, numero_snapshot=numero, huella_snapshot=huella) is esperado


# --- combinaciones completas ---------------------------------------------------------------------------------------------

# (reporte, verificable, respalda, validación conocida, flujo) → (estado, puede, motivo, actualizado, pendiente, validación anterior no vigente)
CASOS = {
    "sin reporte y sin validación (pendiente de revisión)": (
        dict(reporte=False, validacion=False, flujo=F.PENDIENTE_ESTADOS), (E.PENDIENTE_REVISION, False, M.PENDIENTE_REVISION, None, False, False)),
    "sin reporte, revisado, sin validar": (
        dict(reporte=False, validacion=False, flujo=F.LISTO_PARA_VALIDAR), (E.PENDIENTE_VALIDACION, False, M.PENDIENTE_VALIDACION, None, False, False)),
    "sin reporte y con validación vigente": (
        dict(reporte=False, flujo=F.VALIDADO), (E.VALIDACION_VIGENTE, True, None, None, False, False)),
    "sin reporte y con validación que dejó de ser vigente": (
        dict(reporte=False, flujo=F.LISTO_PARA_VALIDAR), (E.PENDIENTE_VALIDACION, False, M.PENDIENTE_VALIDACION, None, False, True)),
    "reporte que respalda la última validación vigente": (
        dict(reporte=True, respalda_ultima=True, flujo=F.VALIDADO), (E.REPORTE_GENERADO, False, M.REPORTE_ACTUALIZADO, True, False, False)),
    "reporte actualizado con estados modificados sin validar": (
        dict(reporte=True, respalda_ultima=True, flujo=F.LISTO_PARA_VALIDAR), (E.PENDIENTE_VALIDACION, False, M.PENDIENTE_VALIDACION, False, False, True)),
    "reporte actualizado con filas pendientes de revisión": (
        dict(reporte=True, respalda_ultima=True, flujo=F.PENDIENTE_ESTADOS), (E.PENDIENTE_REVISION, False, M.PENDIENTE_REVISION, False, False, True)),
    "validación vigente posterior sin emitir": (
        dict(reporte=True, flujo=F.VALIDADO), (E.ACTUALIZACION_PENDIENTE, True, None, False, True, False)),
    "validación posterior que después deja de estar vigente (revisión)": (
        dict(reporte=True, flujo=F.PENDIENTE_ESTADOS), (E.PENDIENTE_REVISION, False, M.PENDIENTE_REVISION, False, True, True)),
    "validación posterior que después deja de estar vigente (validar)": (
        dict(reporte=True, flujo=F.LISTO_PARA_VALIDAR), (E.PENDIENTE_VALIDACION, False, M.PENDIENTE_VALIDACION, False, True, True)),
    "reporte que respalda la última validación y ya no hay códigos GRI": (
        dict(reporte=True, respalda_ultima=True, flujo=F.SIN_CODIGOS_GRI), (E.SIN_CODIGOS_GRI, False, M.SIN_CODIGOS_GRI, False, False, True)),
    "reporte que respalda la última validación y ya no hay documentos elegibles": (
        dict(reporte=True, respalda_ultima=True, flujo=None), (E.SIN_DOCUMENTACION_ELEGIBLE, False, M.SIN_DOCUMENTACION_ELEGIBLE, False, False, True)),
    "año sin reporte ni documentos (consultado explícitamente)": (
        dict(reporte=False, validacion=False, flujo=None), (E.SIN_DOCUMENTACION_ELEGIBLE, False, M.SIN_DOCUMENTACION_ELEGIBLE, None, False, False)),
    "sin reporte y solo sanciones u observados": (
        dict(reporte=False, validacion=False, flujo=F.SIN_CODIGOS_GRI), (E.SIN_CODIGOS_GRI, False, M.SIN_CODIGOS_GRI, None, False, False)),
    "histórico verificable que respalda la última validación vigente": (
        dict(reporte=True, verificable=True, respalda_ultima=True, flujo=F.VALIDADO), (E.REPORTE_GENERADO, False, M.REPORTE_ACTUALIZADO, True, False, False)),
    "histórico verificable de una validación anterior": (
        dict(reporte=True, verificable=True, flujo=F.VALIDADO), (E.ACTUALIZACION_PENDIENTE, True, None, False, True, False)),
    "histórico de origen no verificable con validación vigente": (
        dict(reporte=True, verificable=False, flujo=F.VALIDADO), (E.VALIDACION_VIGENTE, True, None, False, False, False)),
    "histórico de origen no verificable con cambios sin validar": (
        dict(reporte=True, verificable=False, flujo=F.LISTO_PARA_VALIDAR), (E.PENDIENTE_VALIDACION, False, M.PENDIENTE_VALIDACION, False, False, True)),
    "histórico de origen no verificable sin validación conocida": (
        dict(reporte=True, verificable=False, validacion=False, flujo=F.PENDIENTE_ESTADOS), (E.PENDIENTE_REVISION, False, M.PENDIENTE_REVISION, False, False, False)),
    "histórico verificable sin validación conocida": (
        dict(reporte=True, verificable=True, validacion=False, flujo=F.PENDIENTE_ESTADOS), (E.PENDIENTE_REVISION, False, M.PENDIENTE_REVISION, False, False, False)),
}


@pytest.mark.parametrize("caso", list(CASOS))
def test_cada_combinacion_produce_todos_los_campos_esperados(caso):
    entrada, esperado = CASOS[caso]
    assert _tupla(_ev(**entrada)) == esperado


# --- coherencia sobre TODAS las combinaciones posibles ------------------------------------------------------------------

def _combinaciones():
    for reporte, verificable, respalda_ultima, validacion, flujo in itertools.product(
        (False, True), (False, True), (False, True), (False, True), (None, *EstadoFlujo)
    ):
        if respalda_ultima and not (reporte and verificable and validacion):
            continue  # respaldar exige reporte de origen verificable y una validación conocida
        if flujo is F.VALIDADO and not validacion:
            continue  # una validación vigente es una validación conocida
        yield reporte, verificable, respalda_ultima, validacion, flujo


COMBINACIONES = list(_combinaciones())


def test_hay_combinaciones_suficientes_para_el_barrido():
    assert len(COMBINACIONES) > 40  # el barrido no queda vacío por un filtro mal escrito


@pytest.mark.parametrize(("reporte", "verificable", "respalda_ultima", "validacion", "flujo"), COMBINACIONES)
def test_los_campos_nunca_se_contradicen(reporte, verificable, respalda_ultima, validacion, flujo):
    ev = _ev(reporte=reporte, verificable=verificable, respalda_ultima=respalda_ultima, validacion=validacion, flujo=flujo)
    # Un reporte solo se anuncia actualizado si se demuestra que respalda la última validación Y esta sigue vigente.
    assert (ev.reporte_actualizado is True) == (reporte and respalda_ultima and flujo is F.VALIDADO)
    assert (ev.reporte_actualizado is None) == (not reporte)
    # REPORTE_GENERADO y el motivo REPORTE_ACTUALIZADO describen exactamente lo mismo.
    assert (ev.estado is E.REPORTE_GENERADO) == (ev.reporte_actualizado is True)
    assert (ev.motivo_no_generar is M.REPORTE_ACTUALIZADO) == (ev.estado is E.REPORTE_GENERADO)
    # Se puede generar exactamente en VALIDACION_VIGENTE y ACTUALIZACION_PENDIENTE, y entonces no hay motivo.
    assert ev.puede_generar == (ev.estado in (E.VALIDACION_VIGENTE, E.ACTUALIZACION_PENDIENTE))
    assert (ev.motivo_no_generar is None) == ev.puede_generar
    # Con la última validación vigente se puede generar salvo que un reporte demostrablemente la respalde.
    assert ev.puede_generar == (flujo is F.VALIDADO and not (reporte and respalda_ultima))
    # Un reporte anterior no oculta el estado actual del análisis.
    if flujo is not F.VALIDADO:
        assert ev.estado is {
            F.PENDIENTE_ESTADOS: E.PENDIENTE_REVISION, F.LISTO_PARA_VALIDAR: E.PENDIENTE_VALIDACION,
            F.SIN_CODIGOS_GRI: E.SIN_CODIGOS_GRI, F.DOCUMENTACION_INSUFICIENTE: E.SIN_DOCUMENTACION_ELEGIBLE,
            None: E.SIN_DOCUMENTACION_ELEGIBLE,
        }[flujo]
    # La actualización pendiente solo existe con un reporte de origen verificable que no respalda la última validación conocida.
    assert ev.actualizacion_pendiente == (reporte and verificable and validacion and not respalda_ultima)
    assert not (ev.actualizacion_pendiente and ev.reporte_actualizado)
    # Una validación conocida que no está vigente se informa como tal; sin validación conocida no hay nada que informar.
    assert ev.validacion_anterior_no_vigente == (validacion and flujo is not F.VALIDADO)


# --- descarga del reporte actualizado ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("caso", "rechazo"),
    [
        ("reporte que respalda la última validación vigente", None),
        ("histórico verificable que respalda la última validación vigente", None),
        ("validación vigente posterior sin emitir", RechazoDeDescarga.ACTUALIZACION_PENDIENTE),
        ("validación posterior que después deja de estar vigente (validar)", RechazoDeDescarga.ACTUALIZACION_PENDIENTE),
        ("histórico verificable de una validación anterior", RechazoDeDescarga.ACTUALIZACION_PENDIENTE),
        ("histórico de origen no verificable con validación vigente", RechazoDeDescarga.ORIGEN_NO_VERIFICABLE),
        ("histórico de origen no verificable con cambios sin validar", RechazoDeDescarga.ORIGEN_NO_VERIFICABLE),
        ("reporte actualizado con estados modificados sin validar", RechazoDeDescarga.ANALISIS_NO_VIGENTE),
        ("reporte actualizado con filas pendientes de revisión", RechazoDeDescarga.ANALISIS_NO_VIGENTE),
        ("reporte que respalda la última validación y ya no hay códigos GRI", RechazoDeDescarga.ANALISIS_NO_VIGENTE),
        ("reporte que respalda la última validación y ya no hay documentos elegibles", RechazoDeDescarga.ANALISIS_NO_VIGENTE),
    ],
)
def test_la_descarga_actualizada_se_rechaza_con_la_razon_especifica(caso, rechazo):
    entrada, _ = CASOS[caso]
    assert rechazo_de_descarga(_ev(**entrada), origen_verificable=entrada.get("verificable", True)) is rechazo


def test_un_origen_no_verificable_nunca_se_rechaza_como_actualizacion_pendiente():
    for flujo in (None, *EstadoFlujo):
        ev = _ev(reporte=True, verificable=False, flujo=flujo, validacion=flujo is not None)
        assert rechazo_de_descarga(ev, origen_verificable=False) is RechazoDeDescarga.ORIGEN_NO_VERIFICABLE


def test_los_codigos_de_error_son_los_documentados():
    assert {r.value for r in RechazoDeDescarga} == {"REPORT_UPDATE_PENDING", "REPORT_ORIGIN_UNVERIFIABLE", "REPORT_ANALYSIS_NOT_CURRENT"}


@pytest.mark.parametrize(
    ("caso", "accion"),
    [
        ("validación vigente posterior sin emitir", AccionSugerida.EMITIR),
        ("histórico de origen no verificable con validación vigente", AccionSugerida.EMITIR),
        ("reporte actualizado con filas pendientes de revisión", AccionSugerida.REVISAR),
        ("reporte actualizado con estados modificados sin validar", AccionSugerida.VALIDAR),
        ("validación posterior que después deja de estar vigente (validar)", AccionSugerida.VALIDAR),
        ("reporte que respalda la última validación y ya no hay códigos GRI", AccionSugerida.DESCARGAR_VERSION_HISTORICA),
        ("reporte que respalda la última validación y ya no hay documentos elegibles", AccionSugerida.DESCARGAR_VERSION_HISTORICA),
    ],
)
def test_la_accion_sugerida_orienta_al_consumidor(caso, accion):
    entrada, _ = CASOS[caso]
    assert accion_sugerida(_ev(**entrada)) is accion

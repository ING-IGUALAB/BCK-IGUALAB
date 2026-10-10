"""Resultado combinado del análisis de ingesta: GRI + sanciones (decisión 2026-10-07).

Nivel: unidad, sin BD, proveedor ni red. No prueba COMPLETADO, publicación de
fragmentos ni persistencia: el coordinador de ingesta aún no existe. Textos sintéticos.
"""
import json
import os
from pathlib import Path

import pytest

from app.exceptions import AppException
from app.models.documento_ingesta import ResultadoAnalisis
from app.services.ingesta import analisis_ingesta, deteccion_gri, validacion
from app.services.ingesta.analisis_ingesta import (
    AnalisisIngestaError,
    analizar_documento,
    analizar_texto,
)

SOLO_GRI = "# Informe\n\nReportamos emisiones según GRI 305-1 y GRI 305-2.\n"
SOLO_SANCION = "# Informe\n\nEn 2023 la SUNAT impuso una multa de S/ 12,500 a la empresa.\n"
AMBOS = SOLO_GRI + "\nEn 2023 la SUNAT impuso una multa de S/ 12,500 a la empresa.\n"
NINGUNO = "# Informe\n\nTexto narrativo sin referencias a estándares ni sanciones.\n"
TABLA_DETERIORADA = "| A | B | C |\n|---|---|\n| Datos | solo dos |\n| x | y | z | extra |\n"


def codigos(resultado):
    return [a.codigo for a in resultado.advertencias]


# --- Clasificación ----------------------------------------------------------------------------------

def test_solo_gri_es_con_hallazgos():
    r = analizar_texto(SOLO_GRI)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("referencias_gri_catalogadas",)
    assert r.hay_referencias_gri_catalogadas
    assert not r.hay_sanciones
    assert [g.codigo for g in r.gri.grupos] == ["305"]
    assert r.sanciones.total == 0


def test_solo_sanciones_es_con_hallazgos():
    r = analizar_texto(SOLO_SANCION)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("sanciones_economicas",)
    assert r.hay_sanciones
    assert not r.hay_referencias_gri_catalogadas
    assert r.gri.grupos == ()
    assert r.sanciones.total == 1


def test_gri_y_sanciones_es_con_hallazgos_con_ambos_motivos():
    r = analizar_texto(AMBOS)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("referencias_gri_catalogadas", "sanciones_economicas")


def test_ninguno_es_observado_con_su_motivo():
    r = analizar_texto(NINGUNO)
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert r.motivos == ("sin_referencias_gri_catalogadas_ni_sanciones_economicas",)
    assert r.gri.grupos == ()
    assert r.sanciones.total == 0
    assert r.advertencias == ()
    assert not r.hay_referencias_gri_catalogadas
    assert not r.hay_sanciones


def test_el_resultado_conserva_la_version_del_catalogo():
    r = analizar_texto(NINGUNO)
    assert r.version_catalogo == "2026-10-07.2" == r.gri.version_catalogo


def test_solo_hay_dos_resultados_posibles_y_no_se_asignan_estados_ni_puntajes():
    assert {r.name for r in ResultadoAnalisis} == {"CON_HALLAZGOS", "OBSERVADO"}
    r = analizar_texto(AMBOS)
    atributos = {a.lower() for a in dir(r)} | {a.lower() for g in r.gri.grupos for m in g.menciones for a in m.__slots__}
    for palabra in ("puntaje", "esg", "cumpl", "completado", "publicad", "estado_gri"):
        assert not any(palabra in a for a in atributos)


# --- GRI ambiguo y desconocido ---------------------------------------------------------------------------

def test_estandar_conocido_con_edicion_ambigua_cuenta_como_gri_y_queda_para_revision():
    r = analizar_texto("Índice: GRI 102-55 y GRI 306-3.\n")
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS  # no es ausencia de GRI
    assert r.motivos == ("referencias_gri_catalogadas",)
    assert codigos(r) == ["GRI_EDICION_AMBIGUA", "GRI_EDICION_AMBIGUA"]
    primero = r.advertencias[0]
    assert primero.categoria == "gri"
    assert primero.detalles["codigo_estandar"] == "102"
    assert primero.detalles["candidatos"] == ["102:2016", "102:2025"]
    assert primero.detalles["referencias"] == ["GRI 102-55"]
    # Las menciones siguen sin resolver: no se inventa una edición.
    assert all(g.edicion is None for g in r.gri.grupos)


def test_edicion_no_catalogada_de_un_estandar_conocido_cuenta_y_se_advierte():
    r = analizar_texto("Ver GRI 305: Emisiones 2030.\n")
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert codigos(r) == ["GRI_EDICION_NO_CATALOGADA"]


def test_referencia_completamente_desconocida_es_advertencia_y_no_hallazgo():
    r = analizar_texto("Ver GRI 999-1 y GRI 4.17.7 del informe.\n")
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert r.motivos == ("sin_referencias_gri_catalogadas_ni_sanciones_economicas",)
    assert codigos(r) == ["GRI_REFERENCIA_DESCONOCIDA", "GRI_REFERENCIA_DESCONOCIDA"]
    assert [a.detalles["codigo_estandar"] for a in r.advertencias] == ["4", "999"]
    # Se conserva en el detalle del detector sin atribuirle pertenencia al catálogo.
    assert all(not g.catalogado for g in r.gri.grupos)
    assert r.gri.total_menciones == 2


def test_referencia_desconocida_junto_a_una_conocida_no_cambia_el_motivo():
    r = analizar_texto("Ver GRI 999-1 y GRI 305-1.\n")
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert codigos(r) == ["GRI_REFERENCIA_DESCONOCIDA"]


def test_referencia_desconocida_con_sancion_es_con_hallazgos_por_la_sancion():
    r = analizar_texto("Ver GRI 999-1. Se impuso una multa de S/ 900.\n")
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("sanciones_economicas",)


def test_formato_a_revisar_se_advierte_sin_corregirse():
    r = analizar_texto("Ver GRI 305-1.5 y GRI 14-1.\n")
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert codigos(r) == ["GRI_FORMATO_A_REVISAR", "GRI_FORMATO_A_REVISAR"]
    por_codigo = {a.detalles["codigo_estandar"]: a.detalles["referencias"] for a in r.advertencias}
    assert por_codigo == {"14": ["GRI 14-1"], "305": ["GRI 305-1.5"]}


def test_sancion_con_dato_a_revisar_genera_advertencia():
    r = analizar_texto("Se aplicó una multa equivalente a 59.078 UIT.\n")
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    (a,) = r.advertencias
    assert (a.codigo, a.categoria, a.detalles["motivo"]) == ("SANCION_A_REVISAR", "sanciones", "monto_con_separador_ambiguo")
    assert r.sanciones.sanciones[0].monto.texto == "59.078 UIT"


# --- Fallos de los detectores -----------------------------------------------------------------------------------

def _falla(*_args):
    raise RuntimeError("detalle interno con contenido secreto del documento")


def test_si_falla_el_detector_gri_no_hay_observado_ni_exito_parcial():
    with pytest.raises(AnalisisIngestaError) as capturado:
        analizar_texto(NINGUNO, detector_gri=_falla)
    error = capturado.value
    assert isinstance(error, AppException)
    assert error.code == "INGESTION_ANALYSIS_FAILED"
    assert error.details == {"detector": "gri", "tipo_error": "RuntimeError"}
    assert "secreto" not in error.message
    assert "secreto" not in str(error.details)


def test_si_falla_el_detector_de_sanciones_no_hay_observado_ni_exito_parcial():
    # El detector GRI sí encontraría algo: aun así, el resultado no se entrega a medias.
    with pytest.raises(AnalisisIngestaError) as capturado:
        analizar_texto(SOLO_GRI, detector_sanciones=_falla)
    assert capturado.value.details == {"detector": "sanciones", "tipo_error": "RuntimeError"}


def test_el_error_no_filtra_el_texto_del_documento_aunque_la_causa_lo_incluya():
    def roto(texto, catalogo=None):
        raise ValueError(texto)

    with pytest.raises(AnalisisIngestaError) as capturado:
        analizar_texto("contenido sensible del cliente", detector_gri=roto)
    error = capturado.value
    assert "sensible" not in error.message
    assert "sensible" not in str(error.details)
    assert error.__cause__ is None
    assert error.__suppress_context__


def test_los_dos_detectores_fallan_se_informa_el_primero_y_no_se_ejecuta_el_otro():
    llamadas = []

    def gri(texto, catalogo):
        llamadas.append("gri")
        raise RuntimeError

    def sanciones(texto):
        llamadas.append("sanciones")
        raise RuntimeError

    with pytest.raises(AnalisisIngestaError) as capturado:
        analizar_texto(NINGUNO, detector_gri=gri, detector_sanciones=sanciones)
    assert capturado.value.details["detector"] == "gri"
    assert llamadas == ["gri"]


def test_los_dos_detectores_reciben_el_mismo_texto():
    vistos = []

    def gri(texto, catalogo):
        vistos.append(texto)
        return deteccion_gri.detectar_referencias_gri(texto, catalogo)

    def sanciones(texto):
        vistos.append(texto)
        return analisis_ingesta.detectar_sanciones(texto)

    analizar_texto(AMBOS, detector_gri=gri, detector_sanciones=sanciones)
    assert vistos == [AMBOS, AMBOS]
    assert vistos[0] is vistos[1]


def test_texto_no_str_se_rechaza():
    with pytest.raises(TypeError):
        analizar_texto(b"GRI 305-1")  # type: ignore[arg-type]


# --- Tablas deterioradas: advertencia de calidad, no clasificación ----------------------------------------------------------

async def _validar(texto: str):
    datos = texto.encode("utf-8")

    async def leer(n, _b=[datos]):
        bloque, _b[0] = _b[0][:n], _b[0][n:]
        return bloque

    return await validacion.validar_archivo("informe.md", leer)


async def test_tabla_deteriorada_sin_hallazgos_es_observado_con_advertencia_de_calidad():
    documento = await _validar(NINGUNO + "\n" + TABLA_DETERIORADA)
    assert documento.advertencias
    r = analizar_documento(documento)
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert codigos(r) == ["MARKDOWN_TABLE_INCONSISTENT"]
    assert r.advertencias[0].categoria == "calidad_documento"
    assert r.advertencias[0].detalles["total_inconsistencias"] == 3


async def test_tabla_deteriorada_con_hallazgos_es_con_hallazgos_y_conserva_la_advertencia():
    texto = AMBOS + "\n" + TABLA_DETERIORADA
    documento = await _validar(texto)
    r = analizar_documento(documento)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("referencias_gri_catalogadas", "sanciones_economicas")
    assert "MARKDOWN_TABLE_INCONSISTENT" in codigos(r)


async def test_el_mismo_texto_con_y_sin_tabla_deteriorada_se_clasifica_igual():
    sin = analizar_documento(await _validar(AMBOS))
    con = analizar_documento(await _validar(AMBOS + "\n" + TABLA_DETERIORADA))
    assert (sin.resultado, sin.motivos) == (con.resultado, con.motivos)
    calidad = lambda r: [a.codigo for a in r.advertencias if a.categoria == "calidad_documento"]  # noqa: E731
    assert calidad(sin) == []
    assert calidad(con) == ["MARKDOWN_TABLE_INCONSISTENT"]


async def test_tabla_bien_formada_sin_hallazgos_es_observado_sin_advertencias():
    r = analizar_documento(await _validar(NINGUNO + "\n| A | B |\n|---|---|\n| 1 | 2 |\n"))
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert r.advertencias == ()


async def test_las_citas_y_offsets_usan_la_base_del_texto_sin_bom():
    datos = b"\xef\xbb\xbf" + SOLO_SANCION.encode("utf-8")

    async def leer(n, _b=[datos]):
        bloque, _b[0] = _b[0][:n], _b[0][n:]
        return bloque

    documento = await validacion.validar_archivo("informe.md", leer)
    r = analizar_documento(documento)
    s = r.sanciones.sanciones[0]
    assert documento.texto[s.inicio:s.fin] == "multa"
    assert documento.texto[s.cita_inicio:s.cita_fin] == s.cita


# --- Preparado para persistencia ---------------------------------------------------------------------------------------------------

def test_a_dict_es_serializable_en_json_y_conserva_todo():
    r = analizar_texto(AMBOS + "\nVer GRI 999-1 y GRI 102-55.\n")
    datos = r.a_dict()
    texto = json.dumps(datos, ensure_ascii=False)
    recuperado = json.loads(texto)
    assert recuperado["resultado"] == "CON_HALLAZGOS"
    assert recuperado["version_catalogo"] == "2026-10-07.2"
    assert {g["codigo"] for g in recuperado["gri"]} == {"102", "305", "999"}
    g305 = next(g for g in recuperado["gri"] if g["codigo"] == "305")
    assert [m["referencia_original"] for m in g305["menciones"]] == ["GRI 305-1", "GRI 305-2"]
    assert {"cita", "inicio", "fin", "seccion", "rol"} <= set(g305["menciones"][0])
    (sancion,) = recuperado["sanciones"]
    assert sancion["monto"]["texto"] == "S/ 12,500"
    assert sancion["entidad"]["texto"] == "SUNAT"
    assert sancion["periodo"]["texto"] == "2023"
    assert {a["codigo"] for a in recuperado["advertencias"]} >= {"GRI_REFERENCIA_DESCONOCIDA", "GRI_EDICION_AMBIGUA", "SANCION_A_REVISAR"}


def test_los_datos_ausentes_de_una_sancion_son_nulos_en_la_forma_serializada():
    datos = analizar_texto("Recibimos una multa.\n").a_dict()
    (sancion,) = datos["sanciones"]
    assert sancion["entidad"] is None
    assert sancion["monto"] is None
    assert sancion["periodo"] is None
    assert sancion["calificadores"] == []
    assert sancion["fechas_en_contexto"] == []


def test_las_advertencias_tienen_detalles_acotados():
    texto = "".join(f"Ver GRI 9{i:02d}-1.\n" for i in range(30)) + "Ver GRI 900-1 y " + "GRI 900-1, " * 50 + "\n"
    r = analizar_texto(texto)
    for advertencia in r.advertencias:
        assert len(advertencia.detalles.get("referencias", [])) <= analisis_ingesta.MAX_DETALLES_ADVERTENCIA


def test_el_analisis_es_determinista():
    primero, segundo = analizar_texto(AMBOS), analizar_texto(AMBOS)
    assert primero == segundo


# --- Correcciones: negación con alcance, evidencia vinculada y cero en tablas --------------------------------------------------

def _citas_exactas(texto, r):
    for s in r.sanciones.sanciones:
        assert texto[s.inicio:s.fin] == s.referencia_original
        assert texto[s.cita_inicio:s.cita_fin] == s.cita


@pytest.mark.parametrize(
    "texto",
    [
        "La empresa no pagó la multa impuesta por OEFA de S/ 1000.",
        "La empresa no ha pagado la multa impuesta por OEFA de S/ 1000.",
        "La empresa no impugnó la multa impuesta por OEFA de S/ 1000.",
    ],
)
def test_negar_el_pago_o_el_recurso_conserva_la_multa_y_da_con_hallazgos(texto):
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("sanciones_economicas",)
    assert r.hay_sanciones
    _citas_exactas(texto, r)
    (s,) = r.sanciones.sanciones
    assert s.entidad.texto == "OEFA"
    assert s.monto.texto == "S/ 1000"
    assert not {"pagada", "impugnada"} & {c.codigo for c in s.calificadores}
    assert r.a_dict()["sanciones"][0]["monto"]["texto"] == "S/ 1000"


@pytest.mark.parametrize(
    "texto",
    [
        "La empresa no recibió multas.",
        "No se impusieron multas.",
        "La empresa podría recibir una multa por incumplimiento.",
        "En caso de no pagar, se aplicarán multas.",
    ],
)
def test_las_negaciones_de_existencia_y_las_hipotesis_siguen_siendo_observado(texto):
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert r.sanciones.total == 0
    assert r.sanciones.descartes


@pytest.mark.parametrize(
    "texto",
    [
        "Los impuestos tienen incidencia en la multa asociada.",
        "La empresa pagó los impuestos asociados a la multa.",
        "El pago de impuestos y proveedores no confirma el pago de una multa.",
        "Se pagaron impuestos y proveedores; la multa asociada sigue en revisión.",
    ],
)
def test_evidencia_no_vinculada_no_crea_sancion_y_es_observado(texto):
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert r.sanciones.total == 0
    assert r.motivos == ("sin_referencias_gri_catalogadas_ni_sanciones_economicas",)


def test_evidencia_no_vinculada_junto_a_una_multa_real_solo_cuenta_la_real():
    texto = "Los impuestos tienen incidencia en la multa asociada. El OEFA impuso una multa de S/ 700."
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    (s,) = r.sanciones.sanciones
    assert s.monto.texto == "S/ 700"
    assert s.entidad.texto == "OEFA"
    _citas_exactas(texto, r)


def test_tabla_con_saldo_cero_conserva_la_multa_y_advierte():
    texto = (
        "## Cumplimiento\n\n"
        "| Concepto | Monto impuesto | Saldo |\n"
        "| --- | --- | --- |\n"
        "| Multa impuesta por OEFA | 1000 | 0 |\n"
    )
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.motivos == ("sanciones_economicas",)
    (s,) = r.sanciones.sanciones
    assert s.monto is None
    assert s.entidad.texto == "OEFA"
    assert s.seccion == "Cumplimiento"
    _citas_exactas(texto, r)
    (adv,) = r.advertencias
    assert (adv.codigo, adv.categoria) == ("SANCION_A_REVISAR", "sanciones")
    assert adv.detalles["motivo"] == "monto_no_asociado_con_seguridad"
    assert adv.detalles["total"] == 1
    assert r.a_dict()["sanciones"][0]["monto"] is None


def test_total_agregado_en_cero_no_cambia_el_resultado_a_con_hallazgos():
    texto = "| Indicador | Unidad | Valor |\n|---|---|---|\n| Monto total por multas | S/ | 0 |\n"
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.OBSERVADO
    assert r.sanciones.descartes_por_motivo == {"monto_cero": 1}


async def test_tabla_deteriorada_con_cero_con_y_sin_hallazgos():
    sin = "| A | B | C |\n|---|---|\n| Monto total por multas | S/ | 0 | extra |\n"
    con = "| Concepto | Monto impuesto | Saldo |\n|---|---|\n| Multa impuesta por OEFA | 1000 |\n"
    r_sin = analizar_documento(await _validar(sin))
    assert r_sin.resultado is ResultadoAnalisis.OBSERVADO
    assert codigos(r_sin) == ["MARKDOWN_TABLE_INCONSISTENT"]
    r_con = analizar_documento(await _validar(con))
    assert r_con.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert "MARKDOWN_TABLE_INCONSISTENT" in codigos(r_con)
    _citas_exactas(con, r_con)
    (s,) = r_con.sanciones.sanciones
    assert s.cita == "Multa impuesta por OEFA"
    assert s.monto is None


# --- Muestra local del cliente (opcional; no se versiona ni se envía a servicios) -----------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("IGUALAB_MUESTRA_REPORTE_MD"),
    reason="Defina IGUALAB_MUESTRA_REPORTE_MD con la ruta del reporte local para ejecutar esta comprobación.",
)
def test_muestra_local_se_clasifica_con_hallazgos_y_conserva_el_contexto_de_oefa():
    texto = Path(os.environ["IGUALAB_MUESTRA_REPORTE_MD"]).read_text(encoding="utf-8").lstrip("﻿")
    r = analizar_texto(texto)
    assert r.resultado is ResultadoAnalisis.CON_HALLAZGOS
    assert r.hay_referencias_gri_catalogadas
    assert r.hay_sanciones
    oefa = [s for s in r.sanciones.sanciones if s.monto and s.monto.texto == "59.078 UIT"]
    assert len(oefa) == 1
    assert {"pagada", "impugnada"} <= {c.codigo for c in oefa[0].calificadores}
    json.dumps(r.a_dict(), ensure_ascii=False)

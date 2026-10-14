"""Cálculo PURO del Módulo 5 (sin base de datos): agrupación en filas evaluables, ediciones, evidencia, puntaje, huella y vigencia.

Los análisis se generan con los detectores REALES de la ingesta sobre Markdown de prueba (no hay IA ni red): se comprueba que el
módulo solo reorganiza lo que la ingesta ya persistió.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.exceptions import InternalProcessingError
from app.models.analisis_gri import EstadoGri, TipoIdentidadGri
from app.models.documento_ingesta import ResultadoAnalisis
from app.models.documentos import TipoDocumento
from app.schemas_analisis import ActorResumen, EstadoFlujo, EstadoPuntaje, MotivoNoVigente
from app.services.analisis_gri import calculo
from app.services.analisis_gri.calculo import DocumentoBase, EstadoAsignado
from app.services.ingesta.analisis_ingesta import analizar_texto
from tests.ayudantes_analisis_gri import MD_GRI_204_305, MD_GRI_204_Y_302, MD_OBSERVADO, MD_SOLO_SANCIONES

ACTOR = ActorResumen(id=uuid.uuid4(), nombre="Admin")


def doc(texto: str, nombre: str = "informe.md", tipo=TipoDocumento.MEMORIA_ANUAL) -> DocumentoBase:
    analisis = analizar_texto(texto).a_dict()
    return DocumentoBase(
        id=uuid.uuid4(), nombre_archivo=nombre, sha256=uuid.uuid4().hex * 2, tipo=tipo,
        resultado_analisis=ResultadoAnalisis(analisis["resultado"]),
        completado_en=datetime(2026, 10, 1, tzinfo=timezone.utc), analisis=analisis,
    )


def estados(**por_identidad: EstadoGri) -> dict[str, EstadoAsignado]:
    return {
        i.replace("_", ":", 1): EstadoAsignado(i.replace("_", ":", 1), e, 1, ACTOR, datetime(2026, 10, 2, tzinfo=timezone.utc))
        for i, e in por_identidad.items()
    }


def con_estados(filas, valores: list[EstadoGri | None]):
    asignados = {f.identidad: EstadoAsignado(f.identidad, v, 1, ACTOR, datetime(2026, 10, 2, tzinfo=timezone.utc))
                 for f, v in zip(filas, valores) if v is not None}
    return calculo.aplicar_estados(filas, asignados)


# ============================================ Agrupación y evidencia ============================================

def test_veinte_menciones_del_mismo_codigo_son_una_fila_y_una_contribucion():
    texto = "# Informe\n\n" + "".join(f"Párrafo {n}: cumplimos GRI 204-1 en el periodo.\n\n" for n in range(20))
    d = doc(texto)
    filas, advertencias = calculo.construir_filas([d])
    assert len(filas) == 1
    assert advertencias == []
    fila = filas[0]
    assert (fila.identidad, fila.codigo, fila.edicion) == ("204:2016", "204", "2016")
    assert fila.total_menciones == len(fila.evidencias) == 20
    puntaje = calculo.calcular_puntaje(con_estados(filas, [EstadoGri.OK]), vigente=False, hay_documentos=True)
    assert (puntaje.filas_contadas, puntaje.suma_puntos, puntaje.valor) == (1, 100, 100.0)


def test_referencias_204_1_y_gri_204_se_reunen_bajo_gri_204_y_conservan_todas_las_evidencias():
    d = doc(MD_GRI_204_Y_302)
    filas, _ = calculo.construir_filas([d])
    f204 = next(f for f in filas if f.codigo == "204")
    assert [e.referencia_original for e in f204.evidencias] == ["GRI 204", "GRI 204-1"]
    assert {e.documento_id for e in f204.evidencias} == {d.id}
    assert [f.codigo for f in filas] == ["204", "302"]


def test_la_evidencia_conserva_cita_literal_offsets_seccion_y_rol_sin_inventar_paginas():
    texto = MD_GRI_204_305
    d = doc(texto, nombre="reporte.md")
    filas, _ = calculo.construir_filas([d])
    for fila in filas:
        for e in fila.evidencias:
            assert texto[e.inicio:e.fin] == e.referencia_original
            assert texto[e.cita_inicio:e.cita_fin] == e.cita
            assert e.documento_nombre == "reporte.md"
            assert e.seccion
            assert e.rol in ("indice", "cuerpo")
            assert "pagina" not in type(e).model_fields


def test_distingue_indice_y_cuerpo():
    texto = (
        "# Informe\n\n## Índice de contenidos GRI\n\n| Contenido GRI | Página |\n|---|---|\n| 204-1 | 10 |\n\n"
        "## Proveedores\nSeguimos GRI 204-1 en el cuerpo.\n"
    )
    (fila,), _ = calculo.construir_filas([doc(texto)])
    assert (fila.menciones_en_indice, fila.menciones_en_cuerpo, fila.total_menciones) == (1, 1, 2)
    assert {e.rol for e in fila.evidencias} == {"indice", "cuerpo"}


def test_agrupa_entre_documentos_de_la_misma_empresa_y_anio():
    a = doc(MD_GRI_204_305, nombre="reporte.md", tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    b = doc(MD_GRI_204_Y_302, nombre="memoria.md")
    filas, _ = calculo.construir_filas([a, b])
    assert [f.identidad for f in filas] == ["204:2016", "302:2016", "305:2016"]
    f204 = filas[0]
    assert f204.documentos == [a.id, b.id]
    assert {e.documento_nombre for e in f204.evidencias} == {"reporte.md", "memoria.md"}
    assert f204.total_menciones == 3


def test_ediciones_distintas_son_filas_distintas_y_la_ambigua_no_se_fusiona_con_la_resuelta():
    texto = "# X\n\nGRI 102 (2025) y GRI 102: General Disclosures 2016 y GRI 102 y GRI 102 (2030).\n"
    filas, _ = calculo.construir_filas([doc(texto)])
    por_id = {f.identidad: f for f in filas}
    assert set(por_id) == {"102:2016", "102:2025", "102:ambigua", "102:no-catalogada-2030"}
    assert por_id["102:ambigua"].identidad_tipo is TipoIdentidadGri.EDICION_AMBIGUA
    assert por_id["102:ambigua"].edicion is None
    assert por_id["102:ambigua"].nombre is None
    assert por_id["102:ambigua"].candidatos == ["2016: General Disclosures", "2025: Climate Change"]
    assert por_id["102:ambigua"].requiere_revision
    assert [a.codigo for a in por_id["102:ambigua"].advertencias] == ["GRI_EDICION_AMBIGUA"]
    assert por_id["102:no-catalogada-2030"].identidad_tipo is TipoIdentidadGri.EDICION_NO_CATALOGADA
    assert por_id["102:no-catalogada-2030"].edicion == "2030"
    assert [a.codigo for a in por_id["102:no-catalogada-2030"].advertencias] == ["GRI_EDICION_NO_CATALOGADA"]
    assert por_id["102:2016"].identidad_tipo is TipoIdentidadGri.EDICION_RESUELTA
    assert not por_id["102:2016"].advertencias
    assert all(f.total_menciones == 1 for f in filas)


def test_la_misma_identidad_ambigua_en_dos_documentos_es_una_sola_fila():
    a, b = doc("# A\nVer GRI 102 aquí.\n"), doc("# B\nOtra vez GRI 102 allá.\n")
    filas, _ = calculo.construir_filas([a, b])
    assert [f.identidad for f in filas] == ["102:ambigua"]
    assert filas[0].total_menciones == 2
    assert filas[0].documentos == [a.id, b.id]


def test_las_referencias_desconocidas_son_advertencias_con_evidencia_y_no_puntuan():
    d = doc("# X\n\nSegún GRI 999 y también GRI 204-1.\n")
    filas, advertencias = calculo.construir_filas([d])
    assert [f.identidad for f in filas] == ["204:2016"]
    (adv,) = advertencias
    assert adv.codigo == "GRI_REFERENCIA_DESCONOCIDA"
    assert adv.documento_id == d.id
    assert adv.detalles["codigo_estandar"] == "999"
    assert adv.evidencias[0].referencia_original == "GRI 999"
    # Un documento con SOLO referencias desconocidas no aporta ninguna fila.
    solo, advertencias = calculo.construir_filas([doc("# X\nVer GRI 999.\n")])
    assert solo == []
    assert len(advertencias) == 1


def test_los_avisos_de_calidad_de_la_ingesta_se_conservan_por_documento():
    d = doc("# X\n\n| a | b |\n|---|---|\n| 1 | 2 | 3 |\n\nGRI 204-1\n")
    d.analisis["advertencias"].append(
        {"codigo": "TABLA_COLUMNAS_INCONSISTENTES", "categoria": "calidad_documento", "mensaje": "Tabla.", "detalles": {"n": 1}}
    )
    _, advertencias = calculo.construir_filas([d])
    calidad = [a for a in advertencias if a.categoria == "calidad_documento"]
    assert [(a.codigo, a.documento_id) for a in calidad] == [("TABLA_COLUMNAS_INCONSISTENTES", d.id)]


def test_el_orden_de_las_filas_es_estable():
    filas, _ = calculo.construir_filas([doc("# X\n\nGRI 305-1 (2016), GRI 102 y GRI 2-27 y GRI 204-1, GRI 102 (2025)\n")])
    ordenadas = [f.identidad for f in filas]
    assert ordenadas == sorted(ordenadas, key=lambda i: (int(i.split(":")[0]), i))
    assert calculo.construir_filas([doc("# X\n\nGRI 305-1 (2016), GRI 102 y GRI 2-27 y GRI 204-1, GRI 102 (2025)\n")])[0][0].identidad == ordenadas[0]


def test_un_analisis_almacenado_con_estructura_invalida_es_un_error_interno_no_un_resultado_vacio():
    d = doc(MD_GRI_204_305)
    roto = DocumentoBase(**{**d.__dict__, "analisis": {"gri": "no es una lista", "sanciones": [], "advertencias": []}})
    with pytest.raises(InternalProcessingError) as error:
        calculo.construir_filas([roto])
    assert error.value.code == "ANALYSIS_STORED_DATA_INVALID"
    sin_cita = doc(MD_GRI_204_305)
    del sin_cita.analisis["gri"][0]["menciones"][0]["cita"]
    with pytest.raises(InternalProcessingError):
        calculo.construir_filas([sin_cita])


# ============================================ Puntaje ============================================

def test_promedio_ok_baja_sustancia_y_sub_reportado_es_cincuenta():
    filas, _ = calculo.construir_filas([doc(MD_GRI_204_305 + "\nTambién GRI 302-1.\n")])
    assert len(filas) == 3
    filas = con_estados(filas, [EstadoGri.OK, EstadoGri.BAJA_SUSTANCIA, EstadoGri.SUB_REPORTADO])
    puntaje = calculo.calcular_puntaje(filas, vigente=False, hay_documentos=True)
    assert (puntaje.estado, puntaje.definitivo) == (EstadoPuntaje.CALCULADO_SIN_VALIDAR, False)
    assert (puntaje.valor, puntaje.valor_presentacion, puntaje.suma_puntos, puntaje.filas_contadas) == (50.0, 50.0, 150, 3)
    validado = calculo.calcular_puntaje(filas, vigente=True, hay_documentos=True)
    assert (validado.estado, validado.definitivo, validado.valor) == (EstadoPuntaje.VALIDADO, True, 50.0)


def test_sin_filas_evaluables_el_puntaje_no_esta_disponible_y_nunca_es_cero():
    for documentos, motivo in (([doc(MD_SOLO_SANCIONES)], "SIN_CODIGOS_GRI_EVALUABLES"), ([doc(MD_OBSERVADO)], "SIN_CODIGOS_GRI_EVALUABLES")):
        filas, _ = calculo.construir_filas(documentos)
        puntaje = calculo.calcular_puntaje(filas, vigente=False, hay_documentos=True)
        assert puntaje.estado is EstadoPuntaje.NO_DISPONIBLE
        assert puntaje.valor is None
        assert puntaje.valor_presentacion is None
        assert puntaje.suma_puntos is None
        assert puntaje.motivo == motivo
        assert "no disponible" in puntaje.mensaje
    sin_docs = calculo.calcular_puntaje([], vigente=False, hay_documentos=False)
    assert sin_docs.valor is None
    assert sin_docs.motivo == "DOCUMENTACION_INSUFICIENTE"


def test_todos_sub_reportados_dan_cero_valido_distinto_de_no_disponible():
    filas, _ = calculo.construir_filas([doc(MD_GRI_204_305)])
    filas = con_estados(filas, [EstadoGri.SUB_REPORTADO, EstadoGri.SUB_REPORTADO])
    puntaje = calculo.calcular_puntaje(filas, vigente=False, hay_documentos=True)
    assert puntaje.estado is EstadoPuntaje.CALCULADO_SIN_VALIDAR
    assert puntaje.valor == 0.0
    assert puntaje.valor is not None
    assert puntaje.suma_puntos == 0


def test_con_estados_pendientes_no_se_presenta_puntaje():
    filas, _ = calculo.construir_filas([doc(MD_GRI_204_305)])
    filas = con_estados(filas, [EstadoGri.OK, None])
    puntaje = calculo.calcular_puntaje(filas, vigente=False, hay_documentos=True)
    assert puntaje.estado is EstadoPuntaje.PENDIENTE_ESTADOS
    assert puntaje.definitivo is False
    assert puntaje.valor is None
    assert puntaje.valor_presentacion is None
    assert puntaje.suma_puntos is None
    assert (puntaje.filas_evaluables, puntaje.filas_sin_estado, puntaje.filas_contadas) == (2, 1, 1)


@pytest.mark.parametrize(
    "suma,filas,esperado",
    [(200, 3, 66.7), (100, 3, 33.3), (100, 16, 6.3), (50, 8, 6.3), (150, 3, 50.0), (0, 5, 0.0), (350, 4, 87.5), (50, 7, 7.1)],
)
def test_redondeo_de_presentacion_mitad_hacia_arriba_con_enteros(suma, filas, esperado):
    assert calculo.redondear_presentacion(suma, filas) == esperado


def test_el_redondeo_no_altera_el_valor_exacto():
    filas, _ = calculo.construir_filas([doc(MD_GRI_204_305 + "\nTambién GRI 302-1.\n")])
    filas = con_estados(filas, [EstadoGri.OK, EstadoGri.OK, EstadoGri.BAJA_SUSTANCIA])
    puntaje = calculo.calcular_puntaje(filas, vigente=False, hay_documentos=True)
    assert puntaje.valor == 250 / 3
    assert puntaje.valor_presentacion == 83.3
    assert puntaje.decimales_presentacion == 1


# ============================================ Huella, vigencia y flujo ============================================

def test_la_huella_depende_del_conjunto_de_documentos_y_no_del_orden():
    a, b = doc(MD_GRI_204_305), doc(MD_GRI_204_Y_302)
    assert calculo.huella_de_documentos([a, b]) == calculo.huella_de_documentos([b, a])
    assert calculo.huella_de_documentos([a]) != calculo.huella_de_documentos([a, b])
    assert len(calculo.huella_de_documentos([])) == 64


def test_vigencia_de_una_validacion():
    guardada = calculo.ValidacionGuardada(1, 3, "h" * 64, [], [], 100, 1, ACTOR, datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert calculo.motivos_no_vigente(None, 3, "h" * 64) == []
    assert calculo.motivos_no_vigente(guardada, 3, "h" * 64) == []
    assert calculo.motivos_no_vigente(guardada, 4, "h" * 64) == [MotivoNoVigente.ESTADOS_CAMBIARON]
    assert calculo.motivos_no_vigente(guardada, 3, "x" * 64) == [MotivoNoVigente.DOCUMENTOS_CAMBIARON]
    assert calculo.motivos_no_vigente(guardada, 5, "x" * 64) == [
        MotivoNoVigente.ESTADOS_CAMBIARON, MotivoNoVigente.DOCUMENTOS_CAMBIARON]


def test_estado_de_flujo():
    filas, _ = calculo.construir_filas([doc(MD_GRI_204_305)])
    assert calculo.determinar_flujo(hay_documentos=False, filas=[], vigente=False) is EstadoFlujo.DOCUMENTACION_INSUFICIENTE
    assert calculo.determinar_flujo(hay_documentos=True, filas=[], vigente=False) is EstadoFlujo.SIN_CODIGOS_GRI
    assert calculo.determinar_flujo(hay_documentos=True, filas=filas, vigente=False) is EstadoFlujo.PENDIENTE_ESTADOS
    completas = con_estados(filas, [EstadoGri.OK, EstadoGri.OK])
    assert calculo.determinar_flujo(hay_documentos=True, filas=completas, vigente=False) is EstadoFlujo.LISTO_PARA_VALIDAR
    assert calculo.determinar_flujo(hay_documentos=True, filas=completas, vigente=True) is EstadoFlujo.VALIDADO


def test_los_pendientes_son_explicitos():
    filas, _ = calculo.construir_filas([doc(MD_GRI_204_305)])
    pend = calculo.construir_pendientes(
        empresa_activa=True, hay_documentos=True, filas=filas, validacion=None, motivos=[], flujo=EstadoFlujo.PENDIENTE_ESTADOS)
    assert [(p.codigo, p.cantidad, p.identidades) for p in pend] == [("ESTADOS_PENDIENTES", 2, ["204:2016", "305:2016"])]
    inactiva = calculo.construir_pendientes(
        empresa_activa=False, hay_documentos=False, filas=[], validacion=None, motivos=[], flujo=EstadoFlujo.DOCUMENTACION_INSUFICIENTE)
    assert [p.codigo for p in inactiva] == ["EMPRESA_INACTIVA", "DOCUMENTACION_INSUFICIENTE"]
    solo_sanciones = calculo.construir_pendientes(
        empresa_activa=True, hay_documentos=True, filas=[], validacion=None, motivos=[], flujo=EstadoFlujo.SIN_CODIGOS_GRI)
    assert [p.codigo for p in solo_sanciones] == ["SIN_CODIGOS_GRI"]


# ============================================ Sanciones y documentos ============================================

def test_las_sanciones_se_muestran_con_evidencia_y_el_monto_ausente_no_es_cero():
    d = doc(MD_SOLO_SANCIONES, nombre="sanciones.md")
    sanciones, resumen = calculo.construir_sanciones([d])
    assert len(sanciones) == 2
    con_monto, sin_monto = sorted(sanciones, key=lambda s: s.monto is None)
    assert con_monto.cuantificada
    assert con_monto.monto.texto == "S/ 120,000"
    assert con_monto.monto.unidad == "S/"
    assert con_monto.monto.separador_ambiguo is True  # no se interpreta el separador
    assert con_monto.entidad.texto == "OSINERGMIN"
    assert sin_monto.monto is None
    assert sin_monto.cuantificada is False
    assert sin_monto.documento_nombre == "sanciones.md"
    assert sin_monto.cita
    assert sin_monto.seccion
    assert (resumen.total, resumen.cuantificadas, resumen.no_cuantificadas) == (2, 1, 1)


def test_no_se_suman_monedas_distintas_ni_se_convierte_uit():
    texto = (
        "# Sanciones\nOSINERGMIN impuso una multa de S/ 5,000.50 en 2024. El OEFA impuso una multa de 59 UIT en 2023. "
        "La SUNAT impuso una multa de US$ 1,200.00 en 2022.\n"
    )
    sanciones, resumen = calculo.construir_sanciones([doc(texto)])
    assert resumen.total == 3
    assert resumen.cuantificadas == 3
    assert sum(resumen.por_unidad.values()) == 3  # contadas por unidad, jamás sumadas
    assert len(resumen.por_unidad) == 3
    campos = set(type(resumen).model_fields)
    assert not {"total_monto", "monto_total", "suma"} & campos
    assert {s.monto.texto for s in sanciones} == {"S/ 5,000.50", "59 UIT", "US$ 1,200.00"}


def test_un_documento_observado_se_lista_pero_no_aporta_filas_ni_sanciones():
    observado = doc(MD_OBSERVADO)
    assert observado.resultado_analisis is ResultadoAnalisis.OBSERVADO
    (resumen,) = calculo.resumir_documentos([observado])
    assert resumen.observado is True
    assert resumen.total_referencias_gri == 0
    assert resumen.total_sanciones == 0
    assert calculo.construir_filas([observado]) == ([], [])
    assert calculo.construir_sanciones([observado])[0] == []


def test_la_foto_de_estados_y_documentos_para_la_validacion():
    d = doc(MD_GRI_204_305)
    filas, _ = calculo.construir_filas([d])
    filas = con_estados(filas, [EstadoGri.OK, EstadoGri.SUB_REPORTADO])
    assert calculo.estados_en_foto(filas) == [
        {"identidad": "204:2016", "codigo": "204", "edicion": "2016", "estado": "OK"},
        {"identidad": "305:2016", "codigo": "305", "edicion": "2016", "estado": "SUB_REPORTADO"},
    ]
    assert calculo.documentos_en_foto([d]) == [{"documento_id": str(d.id), "nombre_archivo": d.nombre_archivo, "sha256": d.sha256}]

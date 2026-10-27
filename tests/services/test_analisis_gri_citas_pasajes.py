"""Citas legibles y presentación agrupada por pasaje en el análisis del Módulo 5. Sin base de datos, red ni proveedores.

Los análisis salen de los detectores REALES de la ingesta. Se comprueba que (1) los campos legibles se AÑADEN sin tocar el
original, (2) los pasajes reúnen solo apariciones del mismo pasaje, identidad GRI y documento, y (3) conteos, evidencias y puntaje
ESG no cambian por la presentación.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models.analisis_gri import EstadoGri
from app.models.documento_ingesta import ResultadoAnalisis
from app.models.documentos import TipoDocumento
from app.modules.analisis_gri.domain import calculo
from app.modules.analisis_gri.domain.calculo import DocumentoBase, EstadoAsignado
from app.modules.analisis_gri.domain.pasajes import MAX_LONGITUD_PASAJE
from app.modules.analisis_gri.schemas import ActorResumen
from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from app.modules.ingesta.domain.legibilidad import texto_legible

ACTOR = ActorResumen(id=uuid.uuid4(), nombre="Admin")


def doc_de(texto: str, nombre: str = "informe.md") -> tuple[DocumentoBase, str]:
    analisis = analizar_texto(texto).a_dict()
    return (
        DocumentoBase(
            id=uuid.uuid4(), nombre_archivo=nombre, sha256=uuid.uuid4().hex * 2, tipo=TipoDocumento.MEMORIA_ANUAL,
            resultado_analisis=ResultadoAnalisis(analisis["resultado"]),
            completado_en=datetime(2026, 10, 1, tzinfo=timezone.utc), analisis=analisis,
        ),
        texto,
    )


def fila(filas, identidad: str):
    return next(f for f in filas if f.identidad == identidad)


TRES_REFERENCIAS_EN_UNA_LINEA = (
    "# **Gestión** ambiental<br>2025\n\n### Emisiones\n\n"
    "La empresa<br>redujo emisiones según GRI 305-1, GRI 305-2 y GRI 305-3 durante el periodo.\n"
)


# ============================================ Campos legibles ============================================

def test_los_campos_legibles_se_anaden_y_el_original_queda_intacto():
    d, texto = doc_de(TRES_REFERENCIAS_EN_UNA_LINEA)
    mencion = d.analisis["gri"][0]["menciones"][0]
    original = {k: mencion[k] for k in ("cita", "seccion", "inicio", "fin", "cita_inicio", "cita_fin", "referencia_original")}
    filas, _ = calculo.construir_filas([d])
    ev = filas[0].evidencias[0]
    assert (ev.cita, ev.seccion, ev.inicio, ev.fin, ev.cita_inicio, ev.cita_fin, ev.referencia_original) == tuple(original.values())
    assert "<br>" in ev.cita
    assert "**" in ev.seccion  # el original conserva sus marcas
    assert ev.cita_legible == "La empresa\nredujo emisiones según GRI 305-1, GRI 305-2 y GRI 305-3 durante el periodo."
    assert ev.seccion_legible == "Gestión ambiental 2025 > Emisiones"
    assert texto[ev.cita_inicio:ev.cita_fin] == ev.cita  # el vínculo con el original sigue siendo exacto
    assert mencion["cita"] == original["cita"]  # el análisis almacenado no se mutó


def test_las_sanciones_y_sus_agrupaciones_tambien_traen_la_version_legible():
    texto = (
        "# **Cumplimiento**<br>2025\n\nEl OEFA impuso una multa de S/ 5000 en 2023 por la **Resolución** N° 31-2023-OEFA/DFAI.\n\n"
        "| **Autoridad** | Multa |\n|---|---|\n| OEFA | S/ 800 de multa |\n"
    )
    d, _ = doc_de(texto)
    sanciones, _, agrupaciones = calculo.construir_sanciones_y_agrupaciones([d])
    assert len(sanciones) == 2
    parrafo, fila_de_tabla = sanciones
    assert "**" in parrafo.cita
    assert (parrafo.cita_legible == (
        "El OEFA impuso una multa de S/ 5000 en 2023 por la Resolución N° 31-2023-OEFA/DFAI."
    ))
    assert fila_de_tabla.cita == "| OEFA | S/ 800 de multa |"
    assert fila_de_tabla.cita_legible == "OEFA | S/ 800 de multa"
    assert parrafo.seccion_legible == "Cumplimiento 2025"  # la sección de las sanciones ya viene sin marcas; se conserva igual
    citas = [c for a in agrupaciones for c in a.citas]
    assert sorted(c.cita_legible for c in citas) == sorted(s.cita_legible for s in sanciones)
    assert all(c.seccion_legible == "Cumplimiento 2025" for c in citas)


def test_un_analisis_antiguo_se_presenta_sin_escrituras_ni_reanalisis():
    """Un análisis guardado cuya cita empieza a mitad de palabra (límites anteriores) sigue siendo legible: no se completa."""
    d, _ = doc_de("# Informe\n\nVer GRI 305-1 y más.\n")
    mencion = d.analisis["gri"][0]["menciones"][0]
    mencion["cita"] = "ado **ambiental** y GRI 305-1<br>cierre"  # cita recortada a mitad de palabra, como en análisis antiguos
    antes = repr(d.analisis)
    filas, _ = calculo.construir_filas([d])
    assert filas[0].evidencias[0].cita_legible == "ado ambiental y GRI 305-1\ncierre"  # no se reconstruye «certificado»
    assert repr(d.analisis) == antes


# ============================================ Pasajes ============================================

def test_tres_referencias_de_una_linea_son_un_pasaje_con_tres_apariciones_y_nada_se_pierde():
    d, texto = doc_de(TRES_REFERENCIAS_EN_UNA_LINEA)
    filas, _ = calculo.construir_filas([d])
    assert [f.identidad for f in filas] == ["305:2016"]  # 305-1, 305-2 y 305-3 son referencias de UNA identidad GRI
    f = filas[0]
    assert f.total_menciones == len(f.evidencias) == 3  # las tres apariciones se conservan y se cuentan
    assert len({e.cita for e in f.evidencias}) == 1  # con la misma cita repetida tres veces: la repetición artificial
    assert len(f.pasajes) == 1
    pasaje = f.pasajes[0]
    assert pasaje.total_apariciones == 3
    assert [a.referencia_original for a in pasaje.apariciones] == ["GRI 305-1", "GRI 305-2", "GRI 305-3"]
    assert [texto[a.inicio:a.fin] for a in pasaje.apariciones] == ["GRI 305-1", "GRI 305-2", "GRI 305-3"]
    assert pasaje.cita_legible == f.evidencias[0].cita_legible


def test_varias_referencias_de_la_misma_identidad_en_una_linea_comparten_pasaje():
    texto = "# Informe\n\nReportamos GRI 204-1, la política GRI 204-1 y otra vez 204-1 en el mismo párrafo.\n"
    d, _ = doc_de(texto)
    filas, _ = calculo.construir_filas([d])
    f = fila(filas, "204:2016")
    assert f.total_menciones == len(f.evidencias) >= 2
    assert len(f.pasajes) == 1
    pasaje = f.pasajes[0]
    assert pasaje.total_apariciones == len(f.evidencias) == len(pasaje.apariciones)
    assert sorted(a.indice_evidencia for a in pasaje.apariciones) == list(range(len(f.evidencias)))
    for a in pasaje.apariciones:  # cada aparición conserva su posición original y remite a su evidencia
        ev = f.evidencias[a.indice_evidencia]
        assert (a.inicio, a.fin) == (ev.inicio, ev.fin)
        assert texto[a.inicio:a.fin] == a.referencia_original == ev.referencia_original
    assert pasaje.cita_legible == texto_legible(texto[pasaje.cita_inicio:pasaje.cita_fin])


def test_ventanas_solapadas_de_un_mismo_pasaje_se_unen_con_el_texto_exacto_del_original():
    relleno = "Gestión de proveedores locales con criterios ambientales y sociales verificados cada año. "
    texto = "# Informe\n\n" + relleno * 6 + "Aplicamos GRI 204-1 en compras. " + relleno * 2 + "También GRI 204-1 en contratos. " + relleno * 6 + "\n"
    d, _ = doc_de(texto)
    filas, _ = calculo.construir_filas([d])
    f = fila(filas, "204:2016")
    assert f.total_menciones == 2
    ventanas = [(e.cita_inicio, e.cita_fin) for e in f.evidencias]
    assert ventanas[0] != ventanas[1]
    assert ventanas[0][1] > ventanas[1][0]  # ventanas distintas que se solapan
    assert len(f.pasajes) == 1
    pasaje = f.pasajes[0]
    assert (pasaje.cita_inicio, pasaje.cita_fin) == (min(v[0] for v in ventanas), max(v[1] for v in ventanas))
    assert pasaje.cita_legible == texto_legible(texto[pasaje.cita_inicio:pasaje.cita_fin])  # unión exacta, sin reconstruir nada
    assert pasaje.total_apariciones == 2
    assert all(e.cita_legible in pasaje.cita_legible for e in f.evidencias)


def test_el_mismo_texto_en_pasajes_distintos_no_se_fusiona_por_parecido():
    frase = "Cumplimos GRI 204-1 con proveedores locales."
    texto = f"# Informe\n\n## Compras\n\n{frase}\n\n## Contratos\n\n{frase}\n\n## Anexo\n\n{frase}\n"
    d, _ = doc_de(texto)
    filas, _ = calculo.construir_filas([d])
    f = fila(filas, "204:2016")
    assert f.total_menciones == 3
    assert [p.total_apariciones for p in f.pasajes] == [1, 1, 1]  # idénticos en texto, distintos en posición y sección
    assert [p.seccion_legible for p in f.pasajes] == ["Informe > Compras", "Informe > Contratos", "Informe > Anexo"]


def test_el_mismo_pasaje_en_dos_documentos_no_se_mezcla():
    texto = "# Informe\n\nCumplimos GRI 204-1 con proveedores locales.\n"
    primero, _ = doc_de(texto, "memoria.md")
    segundo, _ = doc_de(texto, "reporte.md")
    filas, _ = calculo.construir_filas([primero, segundo])
    f = fila(filas, "204:2016")
    assert f.total_menciones == 2
    assert [(p.documento_nombre, p.total_apariciones) for p in f.pasajes] == [("memoria.md", 1), ("reporte.md", 1)]


def test_identidades_distintas_en_una_misma_cita_no_comparten_pasaje():
    d, _ = doc_de("# Informe\n\nCumplimos GRI 204-1 y GRI 302-1 en el año.\n")
    filas, _ = calculo.construir_filas([d])
    assert {f.identidad for f in filas} == {"204:2016", "302:2016"}
    for f in filas:
        assert len(f.pasajes) == 1
        assert f.pasajes[0].total_apariciones == 1
        assert [a.referencia_original for a in f.pasajes[0].apariciones] != []


def test_una_cadena_de_ventanas_que_supera_el_limite_se_corta_en_pasajes_sucesivos():
    relleno = "Compras locales con trazabilidad completa de proveedores y contratos del periodo. "
    linea = "".join(f"{relleno * 3}GRI 204-1 aplicado. " for _ in range(14))
    d, texto = doc_de("# Informe\n\n" + linea + "\n")
    filas, _ = calculo.construir_filas([d])
    f = fila(filas, "204:2016")
    assert f.total_menciones == 14
    assert len(f.pasajes) > 1
    assert sum(p.total_apariciones for p in f.pasajes) == 14  # ninguna aparición se pierde ni se cuenta dos veces
    for p in f.pasajes:
        assert p.cita_legible == texto_legible(texto[p.cita_inicio:p.cita_fin])
        assert p.cita_fin - p.cita_inicio <= MAX_LONGITUD_PASAJE


def test_citas_con_posiciones_incoherentes_no_se_unen_por_suposicion():
    d, _ = doc_de("# Informe\n\nCumplimos GRI 204-1 en compras. Y también GRI 204-1 en contratos.\n")
    menciones = d.analisis["gri"][0]["menciones"]
    menciones[1]["cita_inicio"] += 1  # ya no solapa con la misma cita: el texto de la zona común no coincide
    filas, _ = calculo.construir_filas([d])
    assert len(fila(filas, "204:2016").pasajes) == 2


# ============================================ Conteos y puntaje ============================================

def test_la_presentacion_no_cambia_conteos_evidencias_ni_puntaje():
    d, _ = doc_de(
        TRES_REFERENCIAS_EN_UNA_LINEA + "\nGRI 305-1 se repite: GRI 305-1, GRI 305-1.\n\nAdemás GRI 204-1 y GRI 302-1.\n"
    )
    con, _ = calculo.construir_filas([d])
    sin, _ = calculo.construir_filas([d], presentacion=False)
    assert [(f.identidad, f.total_menciones, f.menciones_en_indice, f.menciones_en_cuerpo, f.documentos) for f in con] == [
        (f.identidad, f.total_menciones, f.menciones_en_indice, f.menciones_en_cuerpo, f.documentos) for f in sin
    ]
    assert [[e.model_dump(exclude={"cita_legible", "seccion_legible"}) for e in f.evidencias] for f in con] == [
        [e.model_dump(exclude={"cita_legible", "seccion_legible"}) for e in f.evidencias] for f in sin
    ]
    assert all(f.pasajes == [] and e.cita_legible == "" for f in sin for e in f.evidencias)  # las escrituras no pagan la presentación

    def puntaje(filas):
        asignados = {f.identidad: EstadoAsignado(f.identidad, EstadoGri.OK, 1, ACTOR, datetime(2026, 10, 2, tzinfo=timezone.utc)) for f in filas}
        return calculo.calcular_puntaje(calculo.aplicar_estados(filas, asignados), vigente=False, hay_documentos=True)

    assert puntaje(con) == puntaje(sin)
    assert puntaje(con).filas_contadas == len(con) == 3  # una contribución por identidad aunque 305-1 aparezca varias veces


@pytest.mark.parametrize("repeticiones", [1, 5, 40])
def test_la_contribucion_por_identidad_no_depende_de_cuantos_pasajes_o_apariciones_haya(repeticiones):
    texto = "# Informe\n\n" + "".join(f"## Capítulo {n}\n\nCumplimos GRI 204-1 en el periodo {n}.\n\n" for n in range(repeticiones))
    d, _ = doc_de(texto)
    filas, _ = calculo.construir_filas([d])
    assert len(filas) == 1
    assert filas[0].total_menciones == repeticiones == len(filas[0].pasajes)

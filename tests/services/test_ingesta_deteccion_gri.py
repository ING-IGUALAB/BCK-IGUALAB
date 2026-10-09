"""Detección inicial de referencias GRI agrupadas por estándar (decisión 2026-10-07).

Nivel: unidad, sin BD ni proveedor. NO prueba la clasificación OBSERVADO, sanciones
ni la integración con la ingesta (no existen aún). Una mención no es cumplimiento.
"""
import os
from pathlib import Path

import pytest

from app.services.ingesta import deteccion_gri, validacion
from app.services.ingesta.deteccion_gri import detectar_referencias_gri


def grupos_por_clave(resultado):
    return {(g.codigo, g.edicion): g for g in resultado.grupos}


def referencias(resultado):
    return [(m.referencia_original, m.referencia_normalizada) for g in resultado.grupos for m in g.menciones]


def verificar_citas(texto, resultado):
    """Propiedades que debe cumplir CUALQUIER resultado."""
    for grupo in resultado.grupos:
        for m in grupo.menciones:
            assert texto[m.inicio:m.fin] == m.referencia_original
            assert texto[m.cita_inicio:m.cita_fin] == m.cita
            assert m.cita_inicio <= m.inicio < m.fin <= m.cita_fin
            assert "\n" not in m.cita and "\r" not in m.cita
            assert len(m.cita) <= deteccion_gri.MAX_LONGITUD_CITA + 2 * deteccion_gri.MARGEN_CITA


# --- Agrupación por estándar -----------------------------------------------------------

def test_305_1_y_305_2_se_agrupan_bajo_gri_305_con_todas_sus_evidencias():
    texto = (
        "# Emisiones\n\n"
        "Reportamos el alcance 1 (GRI 305-1) y el alcance 2 (GRI 305-2).\n\n"
        "Más adelante, otra vez GRI 305-1 con datos del año.\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert [(g.codigo, g.edicion, g.nombre) for g in resultado.grupos] == [("305", "2016", "Emissions")]
    (grupo,) = resultado.grupos
    assert grupo.referencias_especificas == ("305-1", "305-2")
    assert [m.referencia_original for m in grupo.menciones] == ["GRI 305-1", "GRI 305-2", "GRI 305-1"]
    assert [m.inicio for m in grupo.menciones] == sorted(m.inicio for m in grupo.menciones)
    assert {m.seccion for m in grupo.menciones} == {"Emisiones"}
    assert resultado.total_menciones == 3
    assert resultado.version_catalogo == "2026-10-07.2"


def test_referencia_al_estandar_completo_y_a_sus_revelaciones_quedan_en_el_mismo_grupo():
    texto = "Seguimos GRI 305 y, en concreto, GRI 305-1.\n"
    (grupo,) = detectar_referencias_gri(texto).grupos
    assert [m.forma for m in grupo.menciones] == ["estandar", "revelacion"]
    assert grupo.referencias_especificas == ("305-1",)


@pytest.mark.parametrize(
    ("texto", "original", "normalizada", "forma"),
    [
        ("Ver GRI 305.", "GRI 305", "305", "estandar"),
        ("Ver GRI 305-1.", "GRI 305-1", "305-1", "revelacion"),
        ("Ver GRI 305-1-a.", "GRI 305-1-a", "305-1-a", "revelacion_con_sufijo"),
        ("Ver GRI 305-1a,", "GRI 305-1a", "305-1-a", "revelacion_con_sufijo"),
        ("Ver GRI 305-1(b).", "GRI 305-1(b)", "305-1-b", "revelacion_con_sufijo"),
        ("Ver GRI 305-1.c;", "GRI 305-1.c", "305-1-c", "revelacion_con_sufijo"),
        ("Ver GRI-305-2.", "GRI-305-2", "305-2", "revelacion"),
        ("Ver GRI: 302-1", "GRI: 302-1", "302-1", "revelacion"),
        ("Ver GRI 2-27 del universal.", "GRI 2-27", "2-27", "revelacion"),
        ("Ver GRI 14.1.5 de minería.", "GRI 14.1.5", "14.1.5", "sectorial"),
        ("Ver GRI 14.1 de minería.", "GRI 14.1", "14.1", "sectorial"),
        ("Ver GRI 305-1.", "GRI 305-1", "305-1", "revelacion"),
        ("Ver GRI Standards 305-1.", "GRI Standards 305-1", "305-1", "revelacion"),
    ],
)
def test_formas_de_referencia_con_puntos_guiones_y_sufijos(texto, original, normalizada, forma):
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    (mencion,) = [m for g in resultado.grupos for m in g.menciones]
    assert (mencion.referencia_original, mencion.referencia_normalizada, mencion.forma) == (original, normalizada, forma)
    assert mencion.prefijada is True


def test_referencia_sectorial_se_asigna_al_estandar_sectorial():
    resultado = detectar_referencias_gri("Minería: GRI 14.1.5 y GRI 14.1.\n")
    (grupo,) = resultado.grupos
    assert (grupo.codigo, grupo.edicion, grupo.nombre) == ("14", "2024", "Mining Sector")
    assert grupo.entrada.tipo == "sectorial"
    assert not grupo.requiere_revision


def test_lista_con_prefijo_se_lee_completa():
    texto = "Cubre GRI 305-1, 305-2 y 305-3; también GRI 302-1 / 302-4, GRI 303.\n"
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert referencias(resultado) == [
        ("GRI 302-1", "302-1"), ("302-4", "302-4"),
        ("GRI 303", "303"),
        ("GRI 305-1", "305-1"), ("305-2", "305-2"), ("305-3", "305-3"),
    ]


def test_lista_no_encadena_numeros_ordinarios_ni_anios():
    texto = "Aplicamos GRI 305-1, 2 sitios y 3 plantas; GRI 305, 2020 fue el año base y GRI 305 y 12 equipos.\n"
    resultado = detectar_referencias_gri(texto)
    assert referencias(resultado) == [("GRI 305-1", "305-1"), ("GRI 305", "305"), ("GRI 305", "305")]


# --- Exclusiones --------------------------------------------------------------------------

def test_numeros_ordinarios_y_codigos_sasb_no_son_referencias_gri():
    texto = (
        "En 2025 se emitieron 305 toneladas y el 305-1 por ciento del total.\n"
        "Página 305-1. Versión 3.5, tabla 14.1.5 y capítulo 102-55.\n"
        "Métrica SASB EM-MM-110a.1 y EM-MM-110a.2; código IFRS S2.\n"
        "Informe GRI Standards 2021 y GRI 2021, publicado en GRI 2020.\n"
    )
    resultado = detectar_referencias_gri(texto)
    assert resultado.grupos == ()
    assert resultado.total_menciones == 0


def test_sasb_junto_a_una_referencia_gri_no_se_atribuye_a_gri():
    texto = "Ver GRI 305-1 (SASB EM-MM-110a.1, 110a.2).\n"
    assert referencias(detectar_referencias_gri(texto)) == [("GRI 305-1", "305-1")]


def test_sin_referencias_el_resultado_esta_vacio_y_no_asigna_estados():
    resultado = detectar_referencias_gri("Texto sin ninguna referencia a estándares.\n")
    assert resultado.grupos == () and resultado.total_menciones == 0 and resultado.para_revision == ()
    # La detección no emite estados GRI, puntaje ESG ni OBSERVADO.
    atributos = {a.lower() for a in dir(resultado)}
    assert not any(palabra in a for a in atributos for palabra in ("observad", "puntaje", "esg", "estado", "sancion"))
    mencion_dir = {a.lower() for a in deteccion_gri.MencionGri.__slots__}
    assert not any(palabra in a for a in mencion_dir for palabra in ("estado", "puntaje", "cumpl"))


# --- Índice y cuerpo --------------------------------------------------------------------------

INDICE = (
    "# Informe 2025\n\n"
    "## Emisiones\n\n"
    "Medimos las emisiones según GRI 305-1 y GRI 305-2.\n\n"
    "## Índice de contenidos GRI\n\n"
    "| Estándar GRI | Descripción | Página | SASB |\n"
    "|---|---|---|---|\n"
    "| 305-1 | Emisiones directas | 45 | EM-MM-110a.1 |\n"
    "| 305-2 | Emisiones indirectas | 46 | 110a.2 |\n"
    "| 306 | Residuos | 12 | 150 |\n"
    "| 14.1.5 | Sector minería | 99 | |\n"
    "| GRI 303-3 | Agua | 77 | |\n"
)


def test_distingue_la_mencion_en_el_indice_de_la_del_cuerpo():
    resultado = detectar_referencias_gri(INDICE)
    verificar_citas(INDICE, resultado)
    g305 = grupos_por_clave(resultado)[("305", "2016")]
    assert [(m.referencia_original, m.rol, m.prefijada) for m in g305.menciones] == [
        ("GRI 305-1", "cuerpo", True),
        ("GRI 305-2", "cuerpo", True),
        ("305-1", "indice", False),
        ("305-2", "indice", False),
    ]
    assert (g305.menciones_en_cuerpo, g305.menciones_en_indice) == (2, 2)
    assert [m.seccion for m in g305.menciones] == [
        "Informe 2025 > Emisiones", "Informe 2025 > Emisiones",
        "Informe 2025 > Índice de contenidos GRI", "Informe 2025 > Índice de contenidos GRI",
    ]


def test_en_el_indice_solo_se_leen_las_columnas_gri_y_no_paginas_ni_sasb():
    resultado = detectar_referencias_gri(INDICE)
    todas = {m.referencia_original for g in resultado.grupos for m in g.menciones}
    assert {"306", "14.1.5", "GRI 303-3"} <= todas
    # Las páginas (45, 12, 99…) y la columna SASB no aportan referencias.
    assert not todas & {"45", "46", "12", "77", "99", "150", "110a.1", "110a.2", "EM-MM-110a.1"}
    assert grupos_por_clave(resultado)[("14", "2024")].menciones[0].rol == "indice"
    # El código solo (306) es del índice y su edición queda sin resolver porque hay dos.
    g306 = grupos_por_clave(resultado)[("306", None)]
    assert g306.menciones[0].rol == "indice" and g306.identidad == "ambigua"


def test_la_cita_de_una_fila_de_indice_es_la_fila_y_no_los_parrafos_vecinos():
    resultado = detectar_referencias_gri(INDICE)
    (indice_305_1,) = [m for g in resultado.grupos for m in g.menciones if m.referencia_original == "305-1"]
    assert indice_305_1.cita == "| 305-1 | Emisiones directas | 45 | EM-MM-110a.1 |"


def test_referencias_sin_prefijo_en_seccion_de_indice_sin_tabla():
    texto = (
        "## GRI Content Index\n\n"
        "- 305-1 Direct GHG emissions\n"
        "- **302-1** Energy consumption within the organization\n"
        "- 45 páginas en total y 2025 como año base\n"
        "- 10-15 años de vida útil\n"
        "305-2, 305-3 Indirect\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert [m.referencia_original for g in resultado.grupos for m in g.menciones] == [
        "302-1", "305-1", "305-2", "305-3",
    ]
    assert all(m.rol == "indice" and not m.prefijada for g in resultado.grupos for m in g.menciones)


def test_fuera_de_un_contexto_gri_el_codigo_sin_prefijo_no_se_reconoce():
    texto = "## Resultados\n\n305-1 es el valor reportado.\n\n| Código | Valor |\n|---|---|\n| 305-1 | 20 |\n| 306 | 3 |\n"
    assert detectar_referencias_gri(texto).grupos == ()


def test_titulo_de_tabla_con_referencia_gri_no_convierte_sus_celdas_en_codigos():
    # Hallazgo con la muestra real: el título («…2025 (GRI 203-1)») no es una columna GRI.
    texto = (
        "| **Grado de avance 2025 (GRI 203-1)** | Detalle |\n"
        "|---|---|\n"
        "| 30% de avance de obra | 57 |\n"
        "| 40 | 2 |\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert referencias(resultado) == [("GRI 203-1", "203-1")]


def test_fila_con_distinto_numero_de_columnas_no_se_interpreta_con_el_encabezado():
    texto = (
        "| Tema | GRI |\n"
        "|---|---|\n"
        "| Agua | 303 |\n"
        "| Energía | 302 | extra desplazada |\n"
        "| 305 |\n"
    )
    resultado = detectar_referencias_gri(texto)
    # Solo la fila bien formada se lee por columnas; las deterioradas no aportan códigos sueltos.
    assert referencias(resultado) == [("303", "303")]


def test_un_grupo_resuelto_por_edicion_explicita_y_por_unicidad_es_explicito():
    texto = "Ver GRI 14.1 y también GRI 14: Mining Sector 2024.\n"
    (grupo,) = detectar_referencias_gri(texto).grupos
    assert (grupo.edicion, grupo.identidad) == ("2024", "edicion_explicita")
    assert {m.identidad for m in grupo.menciones} == {"unica_edicion_catalogada", "edicion_explicita"}


def test_tabla_con_otro_marco_como_encabezado_no_es_columna_gri():
    texto = "| Marco SASB | GRI |\n|---|---|\n| 110a.1 | 305-1 |\n"
    resultado = detectar_referencias_gri(texto)
    assert referencias(resultado) == [("305-1", "305-1")]


# --- No atribuir párrafos a códigos ------------------------------------------------------------

def test_un_parrafo_cercano_a_una_lista_de_codigos_no_se_atribuye_a_ellos():
    texto = (
        "## Cobertura GRI\n\n"
        "GRI 305-1, GRI 305-2\n\n"
        "La empresa redujo sus emisiones un 12 % gracias a un nuevo sistema de captura.\n\n"
        "Además, mejoró su gestión del agua.\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    (grupo,) = resultado.grupos
    assert len(grupo.menciones) == 2
    for mencion in grupo.menciones:
        assert mencion.cita == "GRI 305-1, GRI 305-2"
        assert "redujo" not in mencion.cita and "agua" not in mencion.cita
    # El detector tampoco crea evidencia a partir de texto sin referencia.
    assert "redujo" not in "".join(m.cita for m in grupo.menciones)


# --- Ediciones, ambigüedad y códigos históricos ---------------------------------------------------

def test_gri_102_sin_edicion_es_ambiguo_y_no_se_convierte_en_ninguno_de_los_dos():
    texto = "Índice: GRI 102-55 y GRI 102-1.\n"
    resultado = detectar_referencias_gri(texto)
    (grupo,) = resultado.grupos
    assert (grupo.codigo, grupo.edicion, grupo.identidad) == ("102", None, "ambigua")
    assert grupo.entrada is None and grupo.nombre is None
    assert {e.nombre for e in grupo.candidatos} == {"General Disclosures", "Climate Change"}
    assert grupo.motivos_revision == ("edicion_ambigua",)
    assert grupo.requiere_revision and resultado.para_revision == (grupo,)


def test_versiones_distintas_del_mismo_codigo_producen_grupos_distintos():
    texto = (
        "Base: GRI 102: General Disclosures 2016 y GRI 102-55.\n"
        "Clima: GRI 102: Climate Change 2025.\n"
        "Residuos: GRI 306: Waste 2020; antes GRI 306 (2016); sin edición GRI 306-3.\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    claves = grupos_por_clave(resultado)
    assert set(claves) == {("102", "2016"), ("102", "2025"), ("102", None), ("306", "2016"), ("306", "2020"), ("306", None)}
    assert claves[("102", "2016")].nombre == "General Disclosures" and claves[("102", "2016")].identidad == "edicion_explicita"
    assert claves[("102", "2025")].nombre == "Climate Change"
    assert claves[("306", "2020")].nombre == "Waste" and claves[("306", "2016")].nombre == "Effluents and Waste"
    # La mención sin edición NO hereda la de otra parte del documento.
    assert [m.referencia_original for m in claves[("102", None)].menciones] == ["GRI 102-55"]
    assert claves[("102", None)].identidad == "ambigua"
    assert claves[("306", None)].motivos_revision == ("edicion_ambigua",)
    assert claves[("102", "2016")].menciones[0].referencia_original == "GRI 102: General Disclosures 2016"
    assert claves[("102", "2016")].menciones[0].edicion_mencionada == "2016"


def test_edicion_no_catalogada_se_conserva_para_revision():
    texto = "Ver GRI 305: Emisiones 2030 y GRI 305 (1999).\n"
    resultado = detectar_referencias_gri(texto)
    (grupo,) = resultado.grupos
    assert (grupo.codigo, grupo.edicion, grupo.identidad) == ("305", None, "edicion_no_catalogada")
    assert [m.edicion_mencionada for m in grupo.menciones] == ["2030", "1999"]
    assert grupo.motivos_revision == ("edicion_no_catalogada",)


def test_codigos_historicos_se_conservan_sin_convertirse_en_actuales():
    texto = "Ver GRI 307-1 (cumplimiento ambiental), GRI 419-1, GRI 412-1 y GRI 304-2.\n"
    resultado = detectar_referencias_gri(texto)
    claves = grupos_por_clave(resultado)
    assert set(claves) == {("307", "2016"), ("419", "2016"), ("412", "2016"), ("304", "2016")}
    assert claves[("307", "2016")].entrada.vigencia == "retirado"
    assert claves[("304", "2016")].entrada.vigencia == "historico"
    # No aparece GRI 2 (2-27) ni GRI 101 (biodiversidad) como resultado.
    assert not {"2", "101"} & {g.codigo for g in resultado.grupos}


def test_codigos_desconocidos_se_conservan_sin_corregirse():
    texto = "Ver GRI 999-1, GRI 10, GRI 200 y GRI 315-2.\n"
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert [(g.codigo, g.identidad, g.catalogado) for g in resultado.grupos] == [
        ("10", "no_catalogada", False),
        ("200", "no_catalogada", False),
        ("315", "no_catalogada", False),
        ("999", "no_catalogada", False),
    ]
    assert all(g.motivos_revision == ("estandar_no_catalogado",) for g in resultado.grupos)
    assert [m.referencia_original for g in resultado.grupos for m in g.menciones] == ["GRI 10", "GRI 200", "GRI 315-2", "GRI 999-1"]


@pytest.mark.parametrize("fragmento", ["GRI 305-1.5", "GRI 305-2016", "GRI 305(a)", "GRI 305-1-2"])
def test_formato_no_reconocido_se_conserva_para_revision(fragmento):
    texto = f"Ver {fragmento} del informe.\n"
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    (grupo,) = resultado.grupos
    assert grupo.codigo == "305"
    (mencion,) = grupo.menciones
    assert mencion.forma == "no_reconocida" and mencion.revelacion is None
    assert mencion.referencia_original == fragmento
    assert "formato_no_reconocido" in mencion.motivos_revision


@pytest.mark.parametrize("fragmento", ["GRI 14-1", "GRI 11-2-a", "GRI 305.1", "GRI 305.1.5"])
def test_forma_incongruente_con_el_tipo_de_estandar_se_marca(fragmento):
    resultado = detectar_referencias_gri(f"Ver {fragmento}.\n")
    (mencion,) = [m for g in resultado.grupos for m in g.menciones]
    assert mencion.referencia_original == fragmento
    assert "formato_incongruente" in mencion.motivos_revision


# --- Texto de origen, posiciones y bloques ----------------------------------------------------------------

def test_las_citas_coinciden_exactamente_con_el_texto_de_origen_con_crlf_y_unicode():
    texto = (
        "# Ñandú — Informe\r\n\r\n"
        "Emisiones «GRI 305-1» según el año…  y también GRI 305-2.\r\n"
        "   \tGRI 302-1 con sangría y espacios al final   \r\n"
        "| Estándar GRI | Detalle |\r\n|---|---|\r\n| 303-3 | Ágüita |\r\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert resultado.total_menciones == 4
    cita_302 = [m.cita for g in resultado.grupos for m in g.menciones if m.codigo == "302"]
    assert cita_302 == ["GRI 302-1 con sangría y espacios al final"]


def test_cita_de_una_linea_muy_larga_es_una_ventana_exacta():
    relleno = "palabra " * 200
    texto = f"{relleno}GRI 305-1 {relleno}\n"
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    (mencion,) = [m for g in resultado.grupos for m in g.menciones]
    assert len(mencion.cita) < len(texto) and "GRI 305-1" in mencion.cita


def test_referencias_en_bloques_de_codigo_se_detectan_marcadas():
    texto = "```\nGRI 305-1 en un bloque\n```\n\nFuera: GRI 305-2.\n"
    resultado = detectar_referencias_gri(texto)
    (grupo,) = resultado.grupos
    assert [(m.referencia_original, m.en_codigo) for m in grupo.menciones] == [("GRI 305-1", True), ("GRI 305-2", False)]


def test_tabla_con_columnas_inconsistentes_tambien_se_analiza_y_conserva_offsets():
    texto = (
        "## Índice GRI\n\n"
        "| GRI | Tema | Página |\n"
        "|---|---|\n"
        "| 305-1 | Emisiones |\n"
        "| 305-2 | Emisiones | 2 | extra |\n"
    )
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    (grupo,) = resultado.grupos
    assert [m.referencia_original for m in grupo.menciones] == ["305-1", "305-2"]
    diagnostico = validacion.diagnosticar_tablas(texto)
    assert diagnostico.total_inconsistencias == 3
    # Los offsets de ambos módulos usan la misma base: la fila marcada contiene la mención.
    for detalle in diagnostico.detalles[1:]:
        mencion = next(m for m in grupo.menciones if m.cita_inicio <= detalle.inicio + 2 <= m.cita_fin)
        assert detalle.inicio <= mencion.inicio


def test_texto_no_str_se_rechaza():
    with pytest.raises(TypeError):
        detectar_referencias_gri(b"GRI 305-1")  # type: ignore[arg-type]


def test_el_detector_es_determinista_y_no_modifica_el_texto():
    texto = INDICE
    copia = str(texto)
    primero, segundo = detectar_referencias_gri(texto), detectar_referencias_gri(texto)
    assert primero == segundo
    assert texto == copia


async def test_integracion_con_documento_validado_y_bom():
    fuente = "﻿# Informe\n\nVer GRI 305-1.\n".encode("utf-8")

    async def leer(n, _datos=[fuente]):
        bloque, _datos[0] = _datos[0][:n], _datos[0][n:]
        return bloque

    documento = await validacion.validar_archivo("informe.md", leer)
    assert documento.tiene_bom
    resultado = detectar_referencias_gri(documento.texto)
    (mencion,) = [m for g in resultado.grupos for m in g.menciones]
    # Offsets sobre el texto interpretado (sin BOM), igual que los fragmentos.
    assert documento.texto[mencion.inicio:mencion.fin] == "GRI 305-1"


# --- Muestra local del cliente (opcional; no se versiona) -------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("IGUALAB_MUESTRA_REPORTE_MD"),
    reason="Defina IGUALAB_MUESTRA_REPORTE_MD con la ruta del reporte local para ejecutar esta comprobación.",
)
def test_muestra_local_cumple_las_propiedades_generales():
    texto = Path(os.environ["IGUALAB_MUESTRA_REPORTE_MD"]).read_text(encoding="utf-8").lstrip("﻿")
    resultado = detectar_referencias_gri(texto)
    verificar_citas(texto, resultado)
    assert resultado.total_menciones == sum(len(g.menciones) for g in resultado.grupos)

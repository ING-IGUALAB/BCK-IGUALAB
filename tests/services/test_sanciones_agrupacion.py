"""Agrupación de menciones de sanciones dentro de un documento (decisión 2026-10-19). Unidad: sin BD, proveedor ni red.

Los textos son SINTÉTICOS y variados (autoridades, formatos de identificador, importes y disposición en párrafo o tabla): ninguna
regla depende de un archivo, empresa o cifra concretos. Es evidencia léxica, no una precisión medida sobre documentos reales.
"""
import json

import pytest

from app.core.exceptions import InternalProcessingError
from app.modules.ingesta.application import analisis_ingesta
from app.modules.ingesta.application.analisis_ingesta import AnalisisIngestaError, analizar_texto
from app.modules.ingesta.domain import agrupacion_sanciones as ag
from app.modules.ingesta.domain.agrupacion_sanciones import agrupar_sanciones
from app.modules.ingesta.domain.deteccion_sanciones import (
    MOTIVO_IDENTIFICADOR_AMBIGUO,
    MOTIVO_SEPARADOR_AMBIGUO,
    Dato,
    MontoSancion,
    SancionDetectada,
    detectar_sanciones,
)
from app.modules.ingesta.domain.distinciones_sanciones import ReferenciaFormal


def agrupar(texto: str):
    resultado = detectar_sanciones(texto)
    return resultado, agrupar_sanciones(resultado.sanciones)


def codigos(unidad) -> list[str]:
    return [a.codigo for a in unidad.advertencias]


# ------------------------------------------------------------------ misma sanción: párrafo y tabla

PARRAFO_Y_TABLA = [
    pytest.param(
        "# Cumplimiento\n\nMediante la Resolución Directoral N° 0456-2024-SUNAFIL/IR, la SUNAFIL impuso una multa de S/ 12500.75 por la infracción N° 3.\n\n"
        "| Autoridad | Resolución | Infracción | Tipo | Monto (S/) |\n|---|---|---|---|---|\n"
        "| SUNAFIL | 0456-2024-SUNAFIL/IR | N° 3 | Multa impuesta | 12500.75 |\n",
        id="sunafil-resolucion-directoral",
    ),
    pytest.param(
        "# Gestión ambiental\n\nEl OEFA impuso una multa de S/ 8400 en el Expediente N° 2022-0345-OEFA/DFAI/PAS por el cargo N° 2.\n\n"
        "| Expediente | Entidad | Cargo | Concepto | Importe (S/) |\n|---|---|---|---|---|\n"
        "| 2022-0345-OEFA/DFAI/PAS | OEFA | N° 2 | Multa impuesta | 8400 |\n",
        id="oefa-expediente-columna-primera",
    ),
    pytest.param(
        "# Riesgos\n\nSegún la Resolución de Superintendencia N° 045-2022/SUNAT la SUNAT impuso una multa de US$ 15000 por la infracción N° 1.\n\n"
        "| Tipo | Autoridad | Res. | Infracción | Importe (US$) |\n|---|---|---|---|---|\n"
        "| Multa impuesta | SUNAT | 045-2022/SUNAT | N° 1 | 15000 |\n",
        id="sunat-dolares-orden-de-columnas-distinto",
    ),
]


@pytest.mark.parametrize("texto", PARRAFO_Y_TABLA)
def test_la_misma_sancion_en_parrafo_y_tabla_es_una_agrupacion_con_todas_sus_citas(texto):
    resultado, agrupacion = agrupar(texto)
    assert resultado.total == 2  # las menciones siguen siendo dos: el conteo de menciones no cambia
    assert len(agrupacion.unidades) == 1
    (unidad,) = agrupacion.unidades
    assert unidad.indices == (0, 1)
    assert unidad.criterio == ag.CRITERIO_IDENTIDAD_EXPLICITA
    assert {"autoridad", "identificador", "infraccion", "importe"} <= set(unidad.coincidencias)
    assert unidad.estado_importe == ag.IMPORTE_CUANTIFICADO
    assert unidad.advertencias == ()
    assert [s.en_tabla for s in resultado.sanciones] == [False, True]
    for s in resultado.sanciones:  # cada cita conserva su texto y posición originales
        assert texto[s.cita_inicio:s.cita_fin] == s.cita
        assert texto[s.inicio:s.fin] == s.referencia_original
    assert len({(s.cita_inicio, s.cita_fin) for s in resultado.sanciones}) == 2


def test_la_infraccion_codificada_coincidente_agrupa_aunque_una_mencion_no_traiga_importe():
    texto = (
        "# Informe\n\nEl OEFA impuso una multa de S/ 3000 por la Resolución N° 31-2023-OEFA/DFAI, infracción N° 4.\n\n"
        "La Resolución N° 31-2023-OEFA/DFAI, infracción N° 4, es una multa impuesta por el OEFA; no se especifica el monto.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert resultado.total == 2
    (unidad,) = agrupacion.unidades  # un dato ausente (importe) no contradice al presente
    assert unidad.indices == (0, 1)
    assert "infraccion" in unidad.coincidencias
    assert unidad.estado_importe == ag.IMPORTE_CUANTIFICADO


# ------------------------------------------------------------------ lo que NO se fusiona

def test_dos_multas_distintas_de_igual_importe_en_una_resolucion_permanecen_separadas_y_se_advierte():
    texto = (
        "# Informe\n\nMediante Resolución N° 301-2024-OEFA/DFAI el OEFA impuso una multa de S/ 4500 por no presentar el reporte.\n\n"
        "Mediante Resolución N° 301-2024-OEFA/DFAI el OEFA impuso una multa de S/ 4500 por no implementar el monitoreo.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert [s.referencias[0].identificador for s in resultado.sanciones] == ["301-2024-OEFA/DFAI"] * 2
    primera, segunda = agrupacion.unidades
    assert (primera.indices, segunda.indices) == ((0,), (1,))  # autoridad + resolución + importe NO identifican la sanción
    assert codigos(primera) == codigos(segunda) == [ag.ADV_POSIBLE_DUPLICADA]


def test_el_mismo_numeral_no_identifica_la_sancion_ni_siquiera_con_el_mismo_importe():
    # El numeral suele ser la tipificación de la norma («numeral 2.1 del Reglamento»): varias sanciones lo comparten.
    texto = (
        "# Informe\n\nEn la Resolución N° 88-2023-OEFA/DFAI, numeral 2.1 del cuadro, el OEFA impuso una multa de S/ 3000.\n\n"
        "En la Resolución N° 88-2023-OEFA/DFAI, numeral 2.1 del cuadro, el OEFA impuso una multa de S/ 3000.\n\n"
        "En la Resolución N° 88-2023-OEFA/DFAI, numeral 2.1 del cuadro, el OEFA impuso una multa de S/ 8000.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert [s.numeral.texto for s in resultado.sanciones] == ["2.1"] * 3
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,), (2,)]
    assert codigos(agrupacion.unidades[0]) == codigos(agrupacion.unidades[1]) == [ag.ADV_POSIBLE_DUPLICADA]
    assert agrupacion.unidades[2].advertencias == ()  # otro importe: sin aviso ni conflicto demostrado


def test_una_cifra_suelta_bajo_la_columna_infraccion_es_generica_y_no_agrupa():
    texto = (
        "# Informe\n\n| Autoridad | Resolución | Infracción | Tipo | Monto (S/) |\n|---|---|---|---|---|\n"
        "| OEFA | 41-2024-OEFA/DFAI | 1 | Multa impuesta | 2600 |\n| OEFA | 41-2024-OEFA/DFAI | 1 | Multa impuesta | 2600 |\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert [s.infraccion for s in resultado.sanciones] == [None, None]  # sin marcador «N°» no es la infracción concreta
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]
    assert codigos(agrupacion.unidades[0]) == [ag.ADV_POSIBLE_DUPLICADA]


def test_una_infraccion_solo_descrita_con_palabras_no_agrupa():
    texto = (
        "# Informe\n\nMediante Resolución N° 52-2024-OEFA/DFAI el OEFA impuso una multa de S/ 1800 por incumplimiento del LMP.\n\n"
        "Mediante Resolución N° 52-2024-OEFA/DFAI el OEFA impuso una multa de S/ 1800 por incumplimiento del LMP.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert [s.infraccion for s in resultado.sanciones] == [None, None]
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]


def test_dos_infracciones_codificadas_distintas_con_el_mismo_importe_no_se_fusionan_ni_se_advierten():
    texto = (
        "# Informe\n\nEn la Resolución N° 63-2024-OEFA/DFAI, infracción N° 1, el OEFA impuso una multa de S/ 2000.\n\n"
        "En la Resolución N° 63-2024-OEFA/DFAI, infracción N° 2, el OEFA impuso una multa de S/ 2000.\n"
    )
    _, agrupacion = agrupar(texto)
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]
    assert all(u.advertencias == () for u in agrupacion.unidades)  # la propia resolución las distingue


def test_una_misma_resolucion_con_dos_sanciones_de_distinto_numeral_no_se_fusiona():
    texto = (
        "# Informe\n\nEn la Resolución N° 88-2023-OEFA/DFAI, numeral 2.1, el OEFA impuso una multa de S/ 3000.50.\n\n"
        "En la Resolución N° 88-2023-OEFA/DFAI, numeral 3.4, el OEFA impuso una multa de S/ 3000.50.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert [s.numeral.texto for s in resultado.sanciones] == ["2.1", "3.4"]
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]
    assert all(u.advertencias == () for u in agrupacion.unidades)  # la propia resolución las distingue


def test_una_misma_resolucion_con_importes_distintos_y_sin_numeral_no_se_fusiona_ni_se_infiere():
    texto = (
        "# Informe\n\nMediante Resolución N° 88-2023-OEFA/DFAI el OEFA impuso una multa de S/ 3000.\n\n"
        "Mediante Resolución N° 88-2023-OEFA/DFAI el OEFA impuso una multa de S/ 7000.\n"
    )
    _, agrupacion = agrupar(texto)
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]
    assert all(u.advertencias == () for u in agrupacion.unidades)  # pueden ser dos sanciones: no es un conflicto demostrado


def test_la_misma_resolucion_sin_importe_ni_numeral_ni_infraccion_no_se_agrupa():
    texto = (
        "# Informe\n\nMediante Resolución N° 90-2023-OEFA/DFAI el OEFA impuso una multa a la empresa.\n\n"
        "Mediante Resolución N° 90-2023-OEFA/DFAI el OEFA impuso una multa a la empresa.\n"
    )
    _, agrupacion = agrupar(texto)
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]  # solo el número de resolución: no basta


def test_el_mismo_numero_de_resolucion_de_autoridades_distintas_no_se_fusiona():
    texto = (
        "# Informe\n\nMediante Resolución N° 55-2022 el OEFA impuso una multa de S/ 4000.\n\n"
        "Mediante Resolución N° 55-2022 la SUNAFIL impuso una multa de S/ 4000.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert {s.entidad.texto for s in resultado.sanciones} == {"OEFA", "SUNAFIL"}
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]
    assert all(u.advertencias == () for u in agrupacion.unidades)


def test_un_identificador_incompleto_no_sirve_para_agrupar():
    texto = (
        "# Informe\n\nMediante Resolución N° 120 el OEFA impuso una multa de S/ 6000.\n\n"
        "Mediante Resolución N° 120 el OEFA impuso una multa de S/ 6000.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert all(not r.completo for s in resultado.sanciones for r in s.referencias)
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,)]


def test_una_fecha_numerica_no_es_un_identificador_de_resolucion():
    resultado = detectar_sanciones("# Informe\n\nMediante resolución de fecha 2023-10-05 el OEFA impuso una multa de S/ 5000.\n")
    assert resultado.sanciones[0].referencias == ()


def test_varios_identificadores_en_la_misma_oracion_no_se_atribuyen_a_ninguno():
    texto = "# Informe\n\nMediante las Resoluciones N° 10-2022-OEFA y N° 11-2022-OEFA el OEFA impuso una multa de S/ 5000.\n"
    (sancion,) = detectar_sanciones(texto).sanciones
    assert sancion.referencias == ()
    assert MOTIVO_IDENTIFICADOR_AMBIGUO in sancion.motivos_revision


# ------------------------------------------------------------------ posible duplicado y conflicto

def test_si_solo_coinciden_autoridad_moneda_e_importe_se_advierte_sin_fusionar():
    texto = (
        "# Informe\n\nEl OEFA impuso una multa de S/ 9000 en 2023.\n\n"
        "Durante el ejercicio, la empresa recibió una multa del OEFA por S/ 9000.\n"
    )
    _, agrupacion = agrupar(texto)
    primera, segunda = agrupacion.unidades
    assert (primera.indices, segunda.indices) == ((0,), (1,))
    assert codigos(primera) == codigos(segunda) == [ag.ADV_POSIBLE_DUPLICADA]
    assert primera.advertencias[0].mensaje == "Posible sanción duplicada"
    assert primera.advertencias[0].con == (1,)
    assert segunda.advertencias[0].con == (0,)
    assert agrupacion.a_dict()["resumen"]["posibles_duplicados"] == 1  # un par, no dos
    assert agrupacion.a_dict()["resumen"]["unidades_en_conflicto"] == 0  # una posible duplicidad no es un conflicto


def test_no_hay_posible_duplicado_con_monedas_distintas_ni_con_importes_distintos():
    monedas = "# I\n\nEl OEFA impuso una multa de S/ 9000 en 2023.\n\nEl OEFA impuso una multa de US$ 9000 en 2022.\n"
    importes = "# I\n\nEl OEFA impuso una multa de S/ 9000 en 2023.\n\nEl OEFA impuso una multa de S/ 9100 en 2022.\n"
    for texto in (monedas, importes):
        _, agrupacion = agrupar(texto)
        assert len(agrupacion.unidades) == 2
        assert all(u.advertencias == () for u in agrupacion.unidades)


def test_la_cercania_de_fechas_o_el_mismo_ano_no_agrupa_ni_advierte():
    texto = (
        "# I\n\nEl OEFA impuso una multa de S/ 4100 en marzo de 2023.\n\nLa SUNAFIL impuso una multa de S/ 4100 en abril de 2023.\n\n"
        "El OEFA impuso una multa de S/ 5200 en abril de 2023.\n"
    )
    _, agrupacion = agrupar(texto)
    assert [u.indices for u in agrupacion.unidades] == [(0,), (1,), (2,)]
    assert all(u.advertencias == () for u in agrupacion.unidades)


def test_un_importe_con_separador_ambiguo_nunca_establece_igualdad():
    texto = "# I\n\nEl OEFA impuso una multa de S/ 59.078 en 2023.\n\nEl OEFA impuso una multa de S/ 59.078 en 2022.\n"
    resultado, agrupacion = agrupar(texto)
    assert all(s.monto.separador_ambiguo for s in resultado.sanciones)
    assert all(codigos(u) == [ag.ADV_IMPORTE_A_REVISAR] for u in agrupacion.unidades)  # sin «posible duplicado»
    assert all(u.estado_importe == ag.IMPORTE_REQUIERE_REVISION for u in agrupacion.unidades)


def test_datos_contradictorios_de_la_misma_sancion_generan_conflicto_y_no_se_elige_un_valor():
    texto = (
        "# I\n\nEn la Resolución N° 70-2023-OEFA/DFAI, infracción N° 2, el OEFA impuso una multa de S/ 3000.\n\n"
        "En la Resolución N° 70-2023-OEFA/DFAI, infracción N° 2, el OEFA impuso una multa de S/ 3500.\n"
    )
    _, agrupacion = agrupar(texto)
    primera, segunda = agrupacion.unidades
    assert (primera.indices, segunda.indices) == ((0,), (1,))
    assert codigos(primera) == codigos(segunda) == [ag.ADV_DATOS_EN_CONFLICTO]
    assert ag.ADV_POSIBLE_DUPLICADA not in codigos(primera)
    assert agrupacion.a_dict()["resumen"]["unidades_en_conflicto"] == 2


# ------------------------------------------------------------------ construcción directa (casos límite)

def mencion(autoridad, *, ident=None, numeral=None, infraccion=None, importe=None, motivos=(), ambiguo=False, escala=""):
    monto = None
    if importe is not None:
        texto = f"S/ {importe}{escala}"
        monto = MontoSancion(texto, 0, len(texto), "S/", importe, ambiguo, "termino_y_conector")
    return SancionDetectada(
        tipo_termino="multa", referencia_original="multa", inicio=0, fin=5, cita="multa", cita_inicio=0, cita_fin=5,
        seccion="s", en_tabla=False, base_deteccion=(), entidad=Dato(autoridad, 0, len(autoridad)) if autoridad else None,
        monto=monto, periodo=None, fechas_en_contexto=(), calificadores=(), motivos_revision=tuple(motivos),
        referencias=(ReferenciaFormal("resolucion", ident, 0, 1, ident, 0, 1, True),) if ident else (),
        numeral=Dato(numeral, 0, 1) if numeral else None,
        infraccion=Dato(infraccion, 0, 1) if infraccion else None,
    )


def test_un_conjunto_con_contradicciones_internas_no_se_agrupa_y_se_advierte():
    # A~B y B~C por la misma infracción codificada (B no trae numeral), pero A (numeral 2.1) y C (numeral 9.9) se contradicen.
    a = mencion("OEFA", ident="1-2023-X", infraccion="1", numeral="2.1", importe="500")
    b = mencion("OEFA", ident="1-2023-X", infraccion="1", importe="500")
    c = mencion("OEFA", ident="1-2023-X", infraccion="1", numeral="9.9", importe="500")
    resultado = agrupar_sanciones([a, b, c])
    assert [u.indices for u in resultado.unidades] == [(0,), (1,), (2,)]
    assert all(ag.ADV_DATOS_EN_CONFLICTO in codigos(u) for u in resultado.unidades)


def test_la_autoridad_ausente_o_distinta_impide_toda_relacion():
    sin_autoridad = [mencion(None, ident="1-2023-X", importe="500"), mencion(None, ident="1-2023-X", importe="500")]
    distinta = [mencion("OEFA", ident="1-2023-X", importe="500"), mencion("SUNAFIL", ident="1-2023-X", importe="500")]
    for menciones in (sin_autoridad, distinta):
        resultado = agrupar_sanciones(menciones)
        assert len(resultado.unidades) == 2
        assert all(u.advertencias == () for u in resultado.unidades)


def test_la_misma_autoridad_escrita_como_nombre_y_sigla_se_reconoce_por_la_sigla_entre_parentesis():
    nombre = "Organismo de Evaluación y Fiscalización Ambiental (OEFA)"
    resultado = agrupar_sanciones(
        [mencion(nombre, ident="7-2024-OEFA", infraccion="3"), mencion("OEFA", ident="7-2024-OEFA", infraccion="3")]
    )
    assert [u.indices for u in resultado.unidades] == [(0, 1)]


def test_importes_con_escala_distinta_o_cifras_literales_distintas_no_son_comparables_ni_se_normalizan():
    mil = mencion("OEFA", ident="1-2023-X", infraccion="2", importe="5", escala=" mil")
    millones = mencion("OEFA", ident="1-2023-X", infraccion="2", importe="5", escala=" millones")
    resultado = agrupar_sanciones([mil, millones])
    assert [u.indices for u in resultado.unidades] == [(0, 1)]  # la infracción los une; el importe no se contradice
    assert resultado.unidades[0].estado_importe == ag.IMPORTE_REQUIERE_REVISION  # dos cifras literales distintas: no se elige
    assert codigos(resultado.unidades[0]) == [ag.ADV_IMPORTE_A_REVISAR]


def test_el_limite_de_menciones_por_autoridad_deja_la_evaluacion_parcial_sin_perder_menciones(monkeypatch):
    monkeypatch.setattr(ag, "LIMITE_MENCIONES_POR_AUTORIDAD", 2)
    sancionadas = [mencion("OEFA", ident="1-2023-X", infraccion="1", importe="500") for _ in range(3)]
    libres = [mencion("SUNAFIL", ident="2-2023-Y", infraccion="1", importe="700") for _ in range(2)]
    resultado = agrupar_sanciones(sancionadas + libres)
    assert resultado.evaluacion == ag.EVALUACION_PARCIAL
    # Las 3 menciones de la autoridad que superó el límite no se compararon: siguen todas presentes, cada una en su unidad.
    # Las 2 de la otra autoridad sí se compararon.
    assert sorted(i for u in resultado.unidades for i in u.indices) == [0, 1, 2, 3, 4]
    assert [u.indices for u in resultado.unidades] == [(0,), (1,), (2,), (3, 4)]
    assert resultado.a_dict()["evaluacion"] == "PARCIAL"


# ------------------------------------------------------------------ importes: ambiguo, no especificado, cero explícito

def test_importe_ambiguo_no_especificado_y_cero_explicito_dan_resultados_diferentes():
    texto = (
        "# I\n\nEl OEFA impuso una multa de S/ 59.078 en 2023.\n\nLa SUNAFIL impuso una multa a la empresa; no se especifica el monto.\n\n"
        "La SUNAT impuso una multa de S/ 0 a la empresa.\n"
    )
    resultado, agrupacion = agrupar(texto)
    assert dict(resultado.descartes) == {"monto_cero": 1}  # el cero explícito no es una sanción detectada
    ambiguo, no_especificado = agrupacion.unidades
    assert ambiguo.estado_importe == ag.IMPORTE_REQUIERE_REVISION
    assert codigos(ambiguo) == [ag.ADV_IMPORTE_A_REVISAR]
    assert no_especificado.estado_importe == ag.IMPORTE_NO_ESPECIFICADO
    assert no_especificado.advertencias == ()
    sancion = resultado.sanciones[0]
    assert sancion.monto.texto == "S/ 59.078"  # se conserva el texto original
    assert MOTIVO_SEPARADOR_AMBIGUO in sancion.motivos_revision


def test_una_sancion_sin_ningun_importe_unido_no_consta_y_no_es_cero():
    resultado = agrupar_sanciones([mencion("OEFA", ident="1-2023-X")])
    assert resultado.unidades[0].estado_importe == ag.IMPORTE_NO_CONSTA


# ------------------------------------------------------------------ posiciones y Markdown

def test_las_posiciones_de_identificador_y_numeral_son_del_texto_original_aunque_tenga_marcas_markdown():
    texto = "# I\n\nMediante la **Resolución N° 77-2021-OEFA/DFAI**, el OEFA impuso una multa de S/ 5000, numeral **2.1**.\n"
    (sancion,) = detectar_sanciones(texto).sanciones
    (ref,) = sancion.referencias
    assert texto[ref.inicio:ref.fin] == ref.texto
    assert texto[ref.identificador_inicio:ref.identificador_fin] == ref.identificador == "77-2021-OEFA/DFAI"
    assert texto[sancion.numeral.inicio:sancion.numeral.fin] == sancion.numeral.texto == "2.1"
    assert texto[sancion.cita_inicio:sancion.cita_fin] == sancion.cita


@pytest.mark.parametrize("texto", [p.values[0] for p in PARRAFO_Y_TABLA])
def test_cada_mencion_pertenece_a_exactamente_una_unidad_y_el_resultado_es_json(texto):
    resultado, agrupacion = agrupar(texto)
    indices = sorted(i for u in agrupacion.unidades for i in u.indices)
    assert indices == list(range(resultado.total))
    datos = agrupacion.a_dict()
    assert json.loads(json.dumps(datos)) == datos
    assert datos["menciones"] == resultado.total
    assert datos["version"] == ag.VERSION_AGRUPACION_SANCIONES


def test_sin_menciones_no_hay_unidades_y_la_evaluacion_es_completa():
    agrupacion = agrupar_sanciones([])
    assert agrupacion.unidades == ()
    assert agrupacion.evaluacion == ag.EVALUACION_COMPLETA


# ------------------------------------------------------------------ resultado de la ingesta

def test_el_analisis_de_ingesta_incluye_la_agrupacion_y_conserva_las_menciones():
    texto = PARRAFO_Y_TABLA[0].values[0]
    analisis = analizar_texto(texto).a_dict()
    assert analisis["version_agrupacion_sanciones"] == ag.VERSION_AGRUPACION_SANCIONES
    assert len(analisis["sanciones"]) == 2  # `total` sigue siendo de menciones
    assert [u["indices"] for u in analisis["agrupacion_sanciones"]["unidades"]] == [[0, 1]]
    assert analisis["sanciones"][0]["referencias"][0]["identificador"] == "0456-2024-SUNAFIL/IR"
    assert analisis["resultado"] == "CON_HALLAZGOS"


def test_un_fallo_de_la_agrupacion_no_se_convierte_en_observado(monkeypatch):
    def falla(_):
        raise RuntimeError("no se revela")

    monkeypatch.setattr(analisis_ingesta, "agrupar_sanciones", falla)
    with pytest.raises(AnalisisIngestaError) as capturado:
        analizar_texto(PARRAFO_Y_TABLA[0].values[0])
    assert capturado.value.details == {"detector": "agrupacion_sanciones", "tipo_error": "RuntimeError"}
    assert isinstance(capturado.value, InternalProcessingError)


def test_un_documento_sin_sanciones_tambien_trae_su_agrupacion_vacia():
    analisis = analizar_texto("# Informe\n\nTexto sin sanciones ni códigos.\n").a_dict()
    assert analisis["agrupacion_sanciones"]["unidades"] == []
    assert analisis["resultado"] == "OBSERVADO"

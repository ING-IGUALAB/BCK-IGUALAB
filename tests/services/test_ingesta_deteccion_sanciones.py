"""Detección determinista de sanciones económicas (decisión 2026-10-07).

Nivel: unidad, sin BD, proveedor ni red. Los textos son SINTÉTICOS: no se versiona
contenido del reporte del cliente. La comprobación con el reporte local es opcional
(`IGUALAB_MUESTRA_REPORTE_MD`). Es una heurística léxica: estas pruebas fijan el
comportamiento acordado, no una precisión medida sobre documentos reales.
"""
import os
import time
from pathlib import Path

import pytest

from app.services.ingesta import deteccion_sanciones
from app.services.ingesta.deteccion_sanciones import detectar_sanciones


def verificar(texto, resultado):
    """Propiedades que debe cumplir CUALQUIER resultado."""
    for s in resultado.sanciones:
        assert texto[s.inicio:s.fin] == s.referencia_original
        assert texto[s.cita_inicio:s.cita_fin] == s.cita
        assert s.cita_inicio <= s.inicio < s.fin <= s.cita_fin
        for dato in (s.entidad, s.periodo, *s.fechas_en_contexto):
            if dato is not None:
                assert texto[dato.inicio:dato.fin] == dato.texto
        if s.monto is not None:
            assert texto[s.monto.inicio:s.monto.fin] == s.monto.texto
        for c in s.calificadores:
            assert texto[c.inicio:c.fin] == c.texto
        # Nunca se conserva un estado ni una conversión.
        assert not hasattr(s, "estado") and not hasattr(s, "monto_soles")


def una(texto):
    resultado = detectar_sanciones(texto)
    verificar(texto, resultado)
    assert resultado.total == 1, resultado
    return resultado.sanciones[0]


def ninguna(texto):
    resultado = detectar_sanciones(texto)
    verificar(texto, resultado)
    assert resultado.total == 0, resultado
    return resultado


# --- Multas con datos explícitos ---------------------------------------------------------------

def test_multa_con_monto_entidad_y_periodo_explicitos():
    texto = "En 2023, la SUNAT impuso una multa de S/ 12,500 a la empresa."
    s = una(texto)
    assert (s.referencia_original, s.tipo_termino) == ("multa", "multa")
    assert s.entidad.texto == "SUNAT"
    assert (s.monto.texto, s.monto.unidad, s.monto.numero_texto) == ("S/ 12,500", "S/", "12,500")
    assert s.periodo.texto == "2023"
    assert s.cita == texto
    assert set(s.base_deteccion) == {"monto_vinculado", "verbo_de_imposicion_o_pago"}


def test_multa_con_el_monto_unido_por_verbo_activo():
    s = una("El OEFA multó a la Compañía con S/ 3,000 por no presentar el informe.")
    assert s.referencia_original == "multó"
    assert (s.entidad.texto, s.monto.texto, s.monto.vinculo) == ("OEFA", "S/ 3,000", "verbo")


def test_multa_con_el_monto_antes_del_termino():
    s = una("Pagamos S/ 700 de multa en 2022.")
    assert (s.monto.texto, s.monto.vinculo, s.periodo.texto) == ("S/ 700", "monto_antes_del_termino", "2022")


def test_multa_con_adjetivos_entre_el_termino_y_el_monto():
    assert una("Se registró una multa administrativa de S/ 900.").monto.texto == "S/ 900"


def test_multa_sin_monto_deja_los_datos_ausentes_en_none():
    s = una("Durante el 2024 recibimos una multa por infracciones ambientales.")
    assert s.monto is None and s.entidad is None
    assert s.periodo.texto == "2024"
    assert s.calificadores == ()


def test_multa_sin_ningun_dato_adicional():
    s = una("Recibimos una multa.")
    assert (s.entidad, s.monto, s.periodo) == (None, None, None)
    assert s.fechas_en_contexto == () and s.motivos_revision == ()


def test_sancion_economica_con_adjetivo_explicito_es_hallazgo():
    s = una("La sanción económica ascendió a 2.5 millones de soles según la SUNAT.")
    assert s.tipo_termino == "sancion_economica"
    assert s.monto.texto == "2.5 millones de soles"
    assert s.monto.separador_ambiguo is False
    assert s.entidad is None  # «según la SUNAT» no la une a la sanción
    # «impuesta por la SUNAT» forma parte de la misma frase nominal: el monto sigue unido al término.
    s = una("La sanción económica impuesta por la SUNAT ascendió a 2.5 millones de soles.")
    assert s.entidad.texto == "SUNAT" and s.monto.texto == "2.5 millones de soles"


def test_sancion_generica_solo_cuenta_con_un_monto_vinculado():
    assert una("La empresa fue sancionada con S/ 4,000 por la SUNAFIL.").monto.texto == "S/ 4,000"
    assert una("Fuimos sancionados con 5 UIT.").monto.texto == "5 UIT"
    ninguna("La empresa fue sancionada por la autoridad ambiental.")
    ninguna("Las sanciones aplicables están en el anexo.")


# --- Calificadores: pagada, histórica, impugnada --------------------------------------------------

def test_multa_historica_pagada_e_impugnada_sigue_siendo_un_hallazgo_con_calificadores():
    texto = "La empresa pagó una multa histórica de S/ 8,000 impuesta por la SUNAFIL, que fue impugnada."
    s = una(texto)
    assert [c.codigo for c in s.calificadores] == ["pagada", "historica", "impugnada"]
    assert [c.texto for c in s.calificadores] == ["pagó", "histórica", "impugnada"]
    assert s.entidad.texto == "SUNAFIL" and s.monto.texto == "S/ 8,000"
    # Ningún calificador la presenta como deuda pendiente ni como sanción nueva.
    assert "pendiente_de_pago" not in {c.codigo for c in s.calificadores}


@pytest.mark.parametrize(
    ("texto", "esperados"),
    [
        ("Se aplicó una multa de S/ 700 que fue pagada íntegramente.", ["pagada"]),
        ("La multa impuesta por el OEFA fue impugnada y sigue en trámite.", ["impugnada"]),
        ("Se trata de una multa histórica pagada en ejercicios anteriores.", ["historica", "pagada"]),
        ("El tribunal revocó la multa impuesta por la SUNAT.", ["revocada_o_anulada"]),
        ("El tribunal confirmó la multa impuesta por el INDECOPI.", ["confirmada"]),
        ("La multa de S/ 5,000 está pendiente de pago.", ["pendiente_de_pago"]),
    ],
)
def test_calificadores_literales(texto, esperados):
    assert [c.codigo for c in una(texto).calificadores] == esperados


def test_los_calificadores_negados_o_hipoteticos_no_se_registran():
    s = una("La multa de S/ 5,000 no fue pagada y está pendiente de pago.")
    assert [c.codigo for c in s.calificadores] == ["pendiente_de_pago"]
    s = una("La multa impuesta por el OEFA podría ser impugnada, pero no mantenemos obligaciones pendientes de pago.")
    assert s.calificadores == ()


def test_el_pago_como_sustantivo_no_es_un_calificador():
    s = una("Se impuso una multa a la empresa; el pago de proveedores continúa.")
    assert s.calificadores == ()


def test_una_oracion_anaforica_aporta_calificadores_a_la_sancion_anterior():
    texto = (
        "El OEFA impuso una multa equivalente a 15 UIT. "
        "Esta multa fue pagada íntegramente. Dicha multa fue luego impugnada."
    )
    s = una(texto)
    assert [c.codigo for c in s.calificadores] == ["pagada", "impugnada"]
    assert s.monto.texto == "15 UIT"
    assert s.cita == texto  # la cita abarca las oraciones relacionadas


def test_la_anafora_sin_antecedente_se_descarta():
    resultado = ninguna("Esta multa fue pagada íntegramente.")
    assert resultado.descartes_por_motivo == {"anaforica_sin_antecedente": 1}


def test_un_termino_repetido_sin_monto_propio_no_duplica_el_hallazgo():
    s = una("Se impuso una multa de S/ 1,000 y la multa fue pagada.")
    assert [c.codigo for c in s.calificadores] == ["pagada"]


def test_dos_multas_con_montos_propios_son_dos_hallazgos():
    resultado = detectar_sanciones("Fuimos multados por 5 UIT y sancionados con S/ 4,000.")
    verificar("Fuimos multados por 5 UIT y sancionados con S/ 4,000.", resultado)
    assert [s.monto.texto for s in resultado.sanciones] == ["5 UIT", "S/ 4,000"]


# --- Lo que NO es una sanción económica ------------------------------------------------------------------

@pytest.mark.parametrize(
    "texto",
    [
        "No recibimos multas durante el periodo.",
        "En 2025 no se impusieron multas ni sanciones económicas a la empresa.",
        "Sin multas ni sanciones significativas.",
        "Ninguna multa fue impuesta a la empresa.",
        "Hubo cero multas en el ejercicio.",
        "Multas: 0",
        "Nunca hemos sido multados por la autoridad.",
        "La compañía está libre de multas ambientales.",
    ],
)
def test_negaciones_no_son_hallazgos(texto):
    assert ninguna(texto).descartes_por_motivo.get("negacion", 0) >= 1


@pytest.mark.parametrize(
    "texto",
    [
        "Podrían imponerse multas de hasta 100 UIT por incumplimiento.",
        "Existe el riesgo de multas por parte de las autoridades ambientales.",
        "En caso de incumplimiento, se aplicarán multas a la empresa.",
        "Si se incumple la norma, la empresa puede ser multada por el OEFA.",
        "La ley establece multas de entre 1 y 50 UIT.",
        "Las multas potenciales no son materiales.",
        "Capacitamos al personal para evitar multas.",
        "La empresa está expuesta a multas y sanciones económicas.",
    ],
)
def test_riesgos_hipoteticos_y_normativos_no_son_hallazgos(texto):
    resultado = ninguna(texto)
    assert resultado.descartes_por_motivo.get("hipotetica_o_normativa", 0) >= 1


@pytest.mark.parametrize(
    "texto",
    [
        "El Reglamento Interno de Trabajo prevé sanciones y multas disciplinarias para los trabajadores.",
        "Aplicamos medidas disciplinarias, como amonestación o multa interna, ante faltas graves.",
        "El Canal de Ética recibe denuncias y se aplican multas internas conforme a las normas del trabajo.",
        "La política de hostigamiento sexual define la sanción y la multa para los infractores.",
    ],
)
def test_sanciones_disciplinarias_o_laborales_internas_no_son_hallazgos(texto):
    resultado = ninguna(texto)
    assert resultado.descartes_por_motivo.get("disciplinaria_o_laboral_interna", 0) >= 1


def test_multa_de_un_organismo_externo_por_infraccion_laboral_si_cuenta():
    s = una("En 2022 la SUNAFIL impuso una multa de S/ 6,000 por infracciones laborales.")
    assert s.entidad.texto == "SUNAFIL"


def test_la_negacion_de_otra_parte_de_la_oracion_no_anula_la_multa():
    # La coma separa el «no» del término: aquí el «no» pertenece a otra afirmación.
    s = una("Aunque no hubo observaciones, recibimos una multa de S/ 5,000.")
    assert s.monto.texto == "S/ 5,000"
    s = una("No obstante, la SUNAT impuso una multa de S/ 5,000.")
    assert s.entidad.texto == "SUNAT"


def test_un_termino_sin_evidencia_de_imposicion_no_es_hallazgo():
    resultado = ninguna("Multas de SUNAFIL y deterioro reputacional por incumplimiento de normas.")
    assert resultado.descartes_por_motivo == {"sin_evidencia_de_imposicion": 1}


def test_penalidades_contractuales_no_son_sanciones_economicas():
    ninguna("La penalidad contractual fue de S/ 2,000 según el contrato con el proveedor.")


def test_evidencia_solo_por_verbo_de_tramite_se_marca_para_revision():
    s = una("El Tribunal Fiscal revocó la multa.")
    assert s.base_deteccion == ("verbo_de_tramite",)
    assert s.motivos_revision == ("evidencia_solo_por_verbo_de_tramite",)
    assert [c.codigo for c in s.calificadores] == ["revocada_o_anulada"]
    # «revocó la resolución … y su incidencia en la multa»: el verbo no se refiere a la multa.
    ninguna("El Tribunal Fiscal revocó la resolución y su incidencia en la multa asociada.")


# --- Montos cercanos sin relación demostrada ------------------------------------------------------------

@pytest.mark.parametrize(
    "texto",
    [
        "Recibimos una multa por infracciones. El presupuesto de capacitación fue de S/ 9,500.",
        "Se pagó S/ 8,000 en servicios y la organización fue multada por el MINAM.",
        "Con ingresos de S/ 5,000,000, la compañía recibió una multa de la SUNAT.",
        "La empresa fue multada por incumplimiento ambiental; invirtió US$ 2 000 000 en mitigación.",
        "Recibimos una multa por incumplir el plan de manejo, para lo cual presupuestamos 3,000 soles.",
    ],
)
def test_un_monto_proximo_sin_relacion_expresa_no_se_vincula(texto):
    s = una(texto)
    assert s.monto is None


def test_con_dos_montos_solo_se_vincula_el_que_el_texto_une_a_la_multa():
    s = una("Obtuvimos ingresos por S/ 40,000 y pagamos una multa de S/ 1,200.")
    assert s.monto.texto == "S/ 1,200"


# --- Montos: texto original, unidad y separadores -------------------------------------------------------------

def test_el_monto_conserva_el_texto_original_sin_convertir_uit():
    s = una("Se aplicó una multa equivalente a 59.078 UIT.")
    assert s.monto.texto == "59.078 UIT"
    assert s.monto.unidad == "UIT" and s.monto.numero_texto == "59.078"
    assert not any(isinstance(v, float) for v in (s.monto.texto, s.monto.numero_texto, s.monto.unidad))


@pytest.mark.parametrize(
    ("numero", "ambiguo"),
    [
        ("59.078", True), ("1,500", True), ("8’044,241", True), ("12,500", True),
        ("1.234.567", False), ("1,234,567", False), ("1.234,56", False), ("700", False), ("2.5", False),
    ],
)
def test_los_separadores_ambiguos_no_se_interpretan_solo_se_senalan(numero, ambiguo):
    s = una(f"Se aplicó una multa de S/ {numero}.")
    assert s.monto.numero_texto == numero
    assert s.monto.separador_ambiguo is ambiguo
    assert ("monto_con_separador_ambiguo" in s.motivos_revision) is ambiguo


def test_montos_en_distintas_monedas_y_unidades_se_conservan_tal_cual():
    for monto in ("US$ 5,000", "USD 300", "1,500 soles", "20 UIT", "3 mil soles", "S/. 750"):
        assert una(f"Se impuso una multa de {monto}.").monto.texto == monto


def test_un_monto_cero_no_es_una_sancion():
    resultado = ninguna("Se aplicó una multa de S/ 0 en el periodo.")
    assert resultado.descartes_por_motivo == {"monto_cero": 1}


def test_cantidades_en_palabras_no_se_reconocen_como_monto():
    s = una("Se impuso una multa de dos millones de soles.")
    assert s.monto is None


# --- Entidad y periodo ------------------------------------------------------------------------------------------

def test_entidad_ausente_o_ambigua_queda_nula():
    assert una("Se impuso una multa de S/ 5,000.").entidad is None
    s = una("Recibimos multas del OEFA y de la SUNAFIL por S/ 1,000.")
    assert s.entidad is None and "entidad_ambigua" in s.motivos_revision
    assert s.monto is None  # el monto no está unido al término


def test_una_entidad_mencionada_sin_unirla_a_la_sancion_no_se_asigna():
    s = una("El OEFA realizó una supervisión. Se impuso una multa de S/ 5,000.")
    assert s.entidad is None
    s = una("Hubo reuniones con la SUNAFIL y recibimos una multa de S/ 2,000.")
    assert s.entidad is None


def test_entidades_por_nombre_completo_y_siglas_entre_parentesis():
    s = una("La multa impuesta por el Organismo de Evaluación y Fiscalización Ambiental (OEFA) fue pagada.")
    assert s.entidad.texto == "Organismo de Evaluación y Fiscalización Ambiental (OEFA)"
    s = una("La multa del INDECOPI fue pagada.")
    assert s.entidad.texto == "INDECOPI"


def test_las_fechas_de_la_cita_no_se_atribuyen_a_la_sancion_sin_relacion():
    texto = (
        "La multa de S/ 5,000 se originó en la supervisión de marzo de 2020; la Compañía la pagó."
    )
    s = una(texto)
    assert s.periodo is None
    assert [f.texto for f in s.fechas_en_contexto] == ["marzo de 2020"]


def test_periodo_explicito_tras_el_termino():
    assert una("Se impuso una multa de S/ 900 en marzo de 2021.").periodo.texto == "marzo de 2021"
    assert una("Se impuso una multa del año 2019.").periodo.texto == "2019"


# --- El caso OEFA (estructura sintética) --------------------------------------------------------------------------

OEFA = (
    "## Cumplimiento legal\n\n"
    "### Multas\n\n"
    "Durante el 2025, no se realizaron supervisiones por parte de OEFA. No obstante, continúa en trámite ante el "
    "Poder Judicial la impugnación de las sanciones impuestas por el OEFA derivadas de la supervisión regular de "
    "marzo de 2020, correspondientes a una multa equivalente a 59.078 UIT. Es importante señalar que esta multa fue "
    "pagada íntegramente y, al cierre del ejercicio, no mantenemos obligaciones de pago pendientes.\n\n"
    "Durante el 2025, todas las supervisiones realizadas por el OEFA concluyeron sin hallazgos.\n"
)


def test_estructura_del_caso_oefa_conserva_pago_e_impugnacion():
    s = una(OEFA)
    assert s.monto.texto == "59.078 UIT" and s.monto.separador_ambiguo is True
    assert s.entidad.texto == "OEFA"
    assert [c.codigo for c in s.calificadores] == ["impugnada", "pagada"]
    # Las obligaciones «pendientes» están negadas: no se presenta como deuda pendiente.
    assert "pendiente_de_pago" not in {c.codigo for c in s.calificadores}
    assert s.periodo is None  # «marzo de 2020» es la supervisión, no la fecha de la multa
    assert [f.texto for f in s.fechas_en_contexto] == ["marzo de 2020"]
    assert s.seccion == "Cumplimiento legal > Multas"
    assert s.cita.startswith("No obstante, continúa en trámite")
    assert s.cita.endswith("no mantenemos obligaciones de pago pendientes.")
    assert "59.078 UIT" in s.cita and "pagada íntegramente" in s.cita and "impugnación" in s.cita
    assert "supervisiones por parte de OEFA" not in s.cita
    assert s.motivos_revision == ("monto_con_separador_ambiguo",)


# --- Tablas ----------------------------------------------------------------------------------------------------------

def test_fila_de_tabla_bien_formada_une_la_etiqueta_con_su_unico_monto():
    texto = (
        "## Cumplimiento\n\n"
        "| Concepto | Monto | Año |\n|---|---|---|\n"
        "| Monto total de multas ambientales | S/ 15,000 | 2024 |\n"
    )
    s = una(texto)
    assert s.en_tabla and s.base_deteccion == ("fila_de_tabla",)
    assert (s.monto.texto, s.monto.vinculo) == ("S/ 15,000", "fila_de_tabla")
    assert s.cita == "| Monto total de multas ambientales | S/ 15,000 | 2024 |"
    assert s.periodo is None  # la celda del año no se atribuye


def test_fila_de_tabla_con_cero_explicito_no_es_hallazgo():
    texto = (
        "| Indicador | Unidad | Valor |\n|---|---|---|\n"
        "| Monto total por multas o sanciones | S/ | 0 |\n"
        "| Multas | S/ 0 | |\n"
    )
    assert ninguna(texto).descartes_por_motivo == {"monto_cero": 2}


def test_fila_con_varios_montos_no_elige_uno_y_lo_senala():
    texto = "| Concepto | A | B |\n|---|---|---|\n| Multas pagadas | S/ 1,000 | S/ 2,000 |\n"
    s = una(texto)
    assert s.monto is None and "montos_multiples_en_fila" in s.motivos_revision


def test_matriz_de_riesgos_no_es_un_hallazgo():
    texto = (
        "| Tema | Tipo | Descripción |\n|---|---|---|\n"
        "| Capital humano | Riesgo | Multas de SUNAFIL y deterioro reputacional por incumplimiento |\n"
        "| Relaves | Riesgo | Sanciones regulatorias y deterioro reputacional |\n"
    )
    ninguna(texto)


def test_tabla_deteriorada_sin_hallazgos_y_con_hallazgos():
    sin = "| A | B | C |\n|---|---|\n| Tema | solo dos |\n| Sin multas | ni sanciones | x | extra |\n"
    ninguna(sin)
    con = (
        "| A | B | C |\n|---|---|\n"
        "| Concepto | solo dos |\n"
        "| La SUNAT impuso una multa de S/ 2,500 | x | y | extra |\n"
    )
    s = una(con)
    # Fila con distinto número de columnas: se lee la celda sola, sin apoyarse en el encabezado.
    assert s.en_tabla and s.monto.texto == "S/ 2,500" and s.entidad.texto == "SUNAT"
    assert s.cita == "La SUNAT impuso una multa de S/ 2,500"


def test_los_montos_de_otras_celdas_de_una_fila_deteriorada_no_se_vinculan():
    texto = "| A | B | C |\n|---|---|---|\n| Multas pagadas | S/ 700 |\n"
    s = una(texto)  # el hallazgo viene de la celda; el monto de otra celda no se toma
    assert s.monto is None and s.cita == "Multas pagadas"


# --- Estructura y posiciones ------------------------------------------------------------------------------------------

def test_citas_y_offsets_exactos_con_crlf_acentos_y_parrafos_multilinea():
    texto = (
        "# Informe ñandú\r\n\r\n"
        "Párrafo previo sin sanciones.\r\n\r\n"
        "En 2023, la SUNAT impuso\r\nuna multa de S/ 12,500 «por infracciones».\r\n"
        "Otra línea del mismo párrafo.\r\n"
    )
    resultado = detectar_sanciones(texto)
    verificar(texto, resultado)
    (s,) = resultado.sanciones
    assert s.seccion == "Informe ñandú"
    assert s.cita.startswith("En 2023, la SUNAT impuso\r\nuna multa")
    assert s.cita.endswith("«por infracciones».")
    assert "Párrafo previo" not in s.cita and "Otra línea" not in s.cita


def test_cita_muy_larga_es_una_ventana_exacta():
    relleno = "Texto de relleno sin relación. " * 80
    texto = f"{relleno}Se impuso una multa de S/ 900 al contratista. {relleno}"
    s = una(texto)
    assert len(s.cita) <= deteccion_sanciones.MAX_LONGITUD_CITA
    assert "multa de S/ 900" in s.cita


def test_encabezados_y_bloques_de_codigo_no_se_analizan():
    texto = "## Multas impuestas por el OEFA de S/ 5,000\n\n```\nSe impuso una multa de S/ 5,000.\n```\n"
    ninguna(texto)


def test_las_abreviaturas_y_numeros_no_parten_oraciones():
    texto = "El Sr. Pérez informó que la multa N.° 0150 de S/. 5,000 fue pagada. Otra oración distinta."
    s = una(texto)
    assert s.monto is None  # el número de resolución se interpone: no hay vínculo directo
    assert [c.codigo for c in s.calificadores] == ["pagada"]
    assert s.cita == "El Sr. Pérez informó que la multa N.° 0150 de S/. 5,000 fue pagada."
    assert una("La multa de S/. 5,000 fue pagada. Otra oración distinta.").monto.texto == "S/. 5,000"


def test_listas_cada_elemento_es_un_parrafo_independiente():
    texto = "- Se impuso una multa de S/ 1,000\n- El gasto en capacitación fue de S/ 2,000\n"
    s = una(texto)
    assert s.monto.texto == "S/ 1,000" and "capacitación" not in s.cita


def test_resultado_vacio_sin_menciones():
    resultado = detectar_sanciones("Texto sin ningún tema de cumplimiento.")
    assert resultado.sanciones == () and resultado.descartes == ()


def test_muchos_blancos_tras_el_termino_no_vuelven_cuadratica_la_deteccion():
    """Antes `_NEGACION_POSTERIOR` (dos `\\s*` contiguos) tardaba ~5 s con 20 000 blancos tras «multa»."""
    texto = "Recibimos una multa" + " " * 40_000 + "x.\n"
    inicio = time.perf_counter()
    resultado = detectar_sanciones(texto)
    assert time.perf_counter() - inicio < 2
    verificar(texto, resultado)
    assert len(resultado.sanciones) == 1 and resultado.sanciones[0].referencia_original == "multa"


def test_determinismo_y_tipo_de_entrada():
    primero, segundo = detectar_sanciones(OEFA), detectar_sanciones(OEFA)
    assert primero == segundo
    with pytest.raises(TypeError):
        detectar_sanciones(b"multa")  # type: ignore[arg-type]


def test_el_modulo_es_local_sin_llm_ni_red():
    fuente = Path(deteccion_sanciones.__file__).read_text(encoding="utf-8")
    for prohibido in ("import oci", "import requests", "import httpx", "import socket", "urllib", "openai", "anthropic", "cohere"):
        assert prohibido not in fuente


# --- Defecto 1: la negación se aplica al verbo correcto ---------------------------------------------------------

@pytest.mark.parametrize(
    "texto",
    [
        "La empresa no pagó la multa impuesta por OEFA de S/ 1000.",
        "La empresa no ha pagado la multa impuesta por OEFA de S/ 1000.",
        "La empresa no han pagado la multa impuesta por OEFA de S/ 1000.".replace("no han", "aún no ha"),
        "Todavía no se pagó la multa impuesta por OEFA de S/ 1000.",
        "Las empresas no pagaron la multa impuesta por OEFA de S/ 1000.",
        "La empresa no impugnó la multa impuesta por OEFA de S/ 1000.",
        "La empresa no apeló la multa impuesta por OEFA de S/ 1000.",
        "La empresa no impugnó ni pagó la multa impuesta por OEFA de S/ 1000.",
    ],
)
def test_negar_el_pago_o_el_recurso_no_niega_la_multa(texto):
    s = una(texto)
    assert s.entidad.texto == "OEFA"
    assert s.monto.texto == "S/ 1000" and s.monto.vinculo == "termino_y_conector"
    assert s.cita == texto
    # Lo negado no se registra como calificador: ni pagada ni impugnada.
    assert {c.codigo for c in s.calificadores}.isdisjoint({"pagada", "impugnada"})
    assert "pendiente_de_pago" not in {c.codigo for c in s.calificadores}  # tampoco se infiere deuda


def test_sin_pagar_se_registra_literalmente_como_pendiente_de_pago():
    # «sin pagar» es una expresión explícita de impago; no se infiere nada que el texto no diga.
    s = una("La empresa dejó vencer el plazo sin pagar la multa impuesta por OEFA de S/ 1000.")
    assert [(c.codigo, c.texto) for c in s.calificadores] == [("pendiente_de_pago", "sin pagar")]
    assert s.monto.texto == "S/ 1000"


def test_negar_el_pago_conserva_otros_calificadores_afirmados():
    s = una("La empresa no pagó la multa impuesta por OEFA de S/ 1000, que fue impugnada.")
    assert [c.codigo for c in s.calificadores] == ["impugnada"]
    s = una("La empresa pagó la multa impuesta por OEFA de S/ 1000 pero no la impugnó.")
    assert [c.codigo for c in s.calificadores] == ["pagada"]


@pytest.mark.parametrize(
    "texto",
    [
        "La empresa no recibió multas.",
        "La empresa no recibió ninguna multa de OEFA.",
        "No se impusieron multas.",
        "No se impuso ninguna multa a la empresa en 2025.",
        "No hubo multas durante el periodo.",
        "No se registraron multas ni sanciones económicas.",
        "La empresa nunca fue multada por la SUNAT.",
        "No recibió observaciones ni multas.",
        "Sin multas durante el ejercicio.",
        "Tampoco tuvimos multas en 2024.",
    ],
)
def test_las_negaciones_de_la_existencia_siguen_excluidas(texto):
    resultado = ninguna(texto)
    assert resultado.descartes_por_motivo.get("negacion", 0) >= 1


def test_una_negacion_de_otra_afirmacion_no_anula_la_multa():
    s = una("La empresa no pagó impuestos y recibió una multa de S/ 500.")
    assert s.monto.texto == "S/ 500"
    s = una("No hubo observaciones y recibimos una multa de la SUNAT.")
    assert s.entidad.texto == "SUNAT"


@pytest.mark.parametrize(
    "texto",
    [
        "La empresa podría no pagar la multa impuesta por OEFA.",
        "No pagar la multa podría llevar a nuevas multas por S/ 5,000.",
        "En caso de no pagar la multa, se aplicarán multas mayores.",
        "Si la empresa no paga, el OEFA podría imponer una multa.",
    ],
)
def test_las_hipotesis_siguen_excluidas_aunque_hablen_de_pagar(texto):
    resultado = ninguna(texto)
    assert resultado.descartes_por_motivo.get("hipotetica_o_normativa", 0) >= 1


def test_la_multa_real_se_conserva_y_la_hipotetica_de_la_misma_oracion_no():
    texto = "La empresa no pagó la multa, por lo que podría recibir nuevas multas."
    s = una(texto)
    assert texto[s.inicio - 3:s.fin] == "la multa" and s.inicio < texto.index(",")
    assert detectar_sanciones(texto).descartes_por_motivo == {"hipotetica_o_normativa": 1}


# --- Defecto 2: la evidencia debe estar vinculada al término ---------------------------------------------------------

@pytest.mark.parametrize(
    "texto",
    [
        "Los impuestos tienen incidencia en la multa asociada.",
        "Los impuestos a la renta afectan la multa asociada.",
        "Se pagaron impuestos y proveedores; la multa asociada sigue en revisión.",
        "La empresa pagó los impuestos asociados a la multa.",
        "La empresa pagó a sus proveedores. La multa asociada está en revisión.",
        "Registramos impuestos, planilla y la multa asociada en el anexo.",
    ],
)
def test_la_palabra_en_otra_parte_de_la_clausula_no_es_evidencia(texto):
    resultado = ninguna(texto)
    assert resultado.descartes_por_motivo == {"sin_evidencia_de_imposicion": 1}


def test_un_pago_de_impuestos_o_proveedores_no_confirma_el_pago_de_una_multa():
    ninguna("El pago de impuestos y proveedores no confirma el pago de una multa.")
    ninguna("El pago de impuestos y proveedores confirma la multa asociada.")


def test_impuestos_no_es_un_verbo_de_imposicion_pero_impuesta_si():
    ninguna("Los impuestos y la multa asociada fueron revisados por el área legal.")
    assert una("La multa impuesta por la SUNAT fue revisada.").entidad.texto == "SUNAT"
    assert una("Las multas impuestas fueron revisadas.").base_deteccion == ("verbo_de_imposicion_o_pago",)
    assert una("La empresa ha impuesto una multa a su proveedor.").base_deteccion == ("verbo_de_imposicion_o_pago",)


def test_el_pago_de_otra_cosa_no_califica_a_la_multa():
    s = una("La empresa pagó los impuestos y la multa impuesta por OEFA fue impugnada.")
    assert [c.codigo for c in s.calificadores] == ["impugnada"]
    s = una("La empresa pagó proveedores. La multa impuesta por OEFA sigue vigente.")
    assert s.calificadores == ()
    s = una("La empresa pagó los impuestos asociados a la multa impuesta por OEFA.")
    assert "pagada" not in {c.codigo for c in s.calificadores}


@pytest.mark.parametrize(
    ("texto", "calificadores"),
    [
        ("Se impuso una multa de S/ 900 a la empresa.", []),
        ("La empresa pagó la multa impuesta por la SUNAT.", ["pagada"]),
        ("La multa impuesta por la SUNAT fue pagada en 2022.", ["pagada"]),
        ("Se trata de una multa histórica impuesta por el OEFA.", ["historica"]),
        ("La multa impuesta por el OEFA fue impugnada ante el Poder Judicial.", ["impugnada"]),
        ("La multa impuesta por el INDECOPI fue revocada.", ["revocada_o_anulada"]),
        ("El tribunal revocó la multa impuesta por el INDECOPI.", ["revocada_o_anulada"]),
        ("La multa impuesta por el OEFA fue pagada y luego impugnada.", ["pagada", "impugnada"]),
    ],
)
def test_los_casos_positivos_reales_se_conservan(texto, calificadores):
    s = una(texto)
    assert [c.codigo for c in s.calificadores] == calificadores
    assert "verbo_de_imposicion_o_pago" in s.base_deteccion or "monto_vinculado" in s.base_deteccion


def test_calificador_en_oracion_relativa_y_predicado_coordinado():
    s = una("La empresa pagó una multa histórica de S/ 8,000 impuesta por la SUNAFIL, que fue impugnada.")
    assert [c.codigo for c in s.calificadores] == ["pagada", "historica", "impugnada"]
    s = una("La multa de S/ 5,000 no fue pagada y está pendiente de pago.")
    assert [c.codigo for c in s.calificadores] == ["pendiente_de_pago"]


# --- Defecto 3: el cero en tablas no descarta la fila entera -----------------------------------------------------------------

TABLA_SALDO = (
    "| Concepto | Monto impuesto | Saldo |\n"
    "| --- | --- | --- |\n"
    "| Multa impuesta por OEFA | 1000 | 0 |\n"
)


def test_saldo_cero_no_descarta_la_multa_explicitamente_impuesta():
    s = una(TABLA_SALDO)
    assert s.en_tabla and s.referencia_original == "Multa"
    assert s.entidad.texto == "OEFA"
    assert s.base_deteccion == ("verbo_de_imposicion_o_pago",)
    # No se puede asociar con seguridad el monto ni su unidad: monto nulo y advertencia.
    assert s.monto is None
    assert s.motivos_revision == ("monto_no_asociado_con_seguridad",)
    assert s.cita == "| Multa impuesta por OEFA | 1000 | 0 |"


def test_un_total_agregado_en_cero_no_genera_hallazgo_por_si_solo():
    texto = "| Indicador | Unidad | Valor |\n|---|---|---|\n| Monto total por multas | S/ | 0 |\n"
    assert ninguna(texto).descartes_por_motivo == {"monto_cero": 1}
    ninguna("| Indicador | Valor |\n|---|---|\n| Monto total por multas | S/ 0 |\n")
    ninguna("| Indicador | Valor |\n|---|---|\n| Monto total por multas | 0 |\n")


def test_un_cero_de_otra_columna_no_descarta_la_fila_si_hay_un_monto_con_unidad():
    texto = "| Concepto | Monto | Saldo |\n|---|---|---|\n| Multa impuesta por OEFA | S/ 1,000 | S/ 0 |\n"
    s = una(texto)
    assert s.monto.texto == "S/ 1,000" and s.monto.vinculo == "fila_de_tabla"
    assert s.entidad.texto == "OEFA"
    assert "monto_no_asociado_con_seguridad" not in s.motivos_revision


def test_un_cero_sin_unidad_no_descarta_una_multa_con_evidencia_propia():
    texto = "| Concepto | Valor | Año |\n|---|---|---|\n| Multa pagada | 0 | 2024 |\n"
    s = una(texto)
    assert s.monto is None and s.motivos_revision == ("monto_no_asociado_con_seguridad",)
    assert [c.codigo for c in s.calificadores] == ["pagada"]


def test_cero_monetario_explicito_y_ninguna_otra_cifra_si_es_ausencia_de_multa():
    # «S/ 0» dice expresamente que el monto es cero; no hay otra cifra que lo contradiga.
    texto = "| Concepto | Monto |\n|---|---|\n| Multa impuesta por OEFA | S/ 0 |\n"
    assert ninguna(texto).descartes_por_motivo == {"monto_cero": 1}


def test_cifras_sueltas_sin_evidencia_de_imposicion_no_son_hallazgo():
    ninguna("| Concepto | Monto |\n|---|---|\n| Multa | 1000 |\n")
    ninguna("| Concepto | Monto |\n|---|---|\n| Multas | 1000 | \n".replace("| \n", "|\n"))


def test_los_anios_se_ignoran_y_las_cifras_en_columnas_de_monto_cuentan():
    texto = "| Concepto | Año | Monto impuesto |\n|---|---|---|\n| Multa impuesta por OEFA | 2024 | 2000 |\n"
    s = una(texto)
    assert s.monto is None and s.motivos_revision == ("monto_no_asociado_con_seguridad",)


def test_varios_montos_con_unidad_en_la_fila_no_se_eligen():
    texto = "| Concepto | Monto | Saldo |\n|---|---|---|\n| Multa impuesta por OEFA | S/ 1,000 | S/ 400 |\n"
    s = una(texto)
    assert s.monto is None and "montos_multiples_en_fila" in s.motivos_revision


def test_fila_deteriorada_con_cero_conserva_la_multa_de_su_celda():
    texto = (
        "| Concepto | Monto impuesto | Saldo |\n|---|---|---|\n"
        "| Multa impuesta por OEFA | 1000 |\n"  # una columna menos: no se interpreta por columnas
    )
    s = una(texto)
    assert s.monto is None and s.entidad.texto == "OEFA"
    assert s.cita == "Multa impuesta por OEFA"


# --- Muestra local del cliente (opcional; no se versiona) --------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("IGUALAB_MUESTRA_REPORTE_MD"),
    reason="Defina IGUALAB_MUESTRA_REPORTE_MD con la ruta del reporte local para ejecutar esta comprobación.",
)
def test_muestra_local_parrafo_oefa_conserva_pago_e_impugnacion():
    texto = Path(os.environ["IGUALAB_MUESTRA_REPORTE_MD"]).read_text(encoding="utf-8").lstrip("﻿")
    resultado = detectar_sanciones(texto)
    verificar(texto, resultado)
    oefa = [s for s in resultado.sanciones if s.monto is not None and "59.078" in s.monto.texto]
    assert len(oefa) == 1
    s = oefa[0]
    assert s.monto.texto == "59.078 UIT" and s.monto.separador_ambiguo
    assert s.entidad.texto == "OEFA"
    assert {"pagada", "impugnada"} <= {c.codigo for c in s.calificadores}
    assert "pendiente_de_pago" not in {c.codigo for c in s.calificadores}
    assert "pagada íntegramente" in s.cita and "impugnación" in s.cita
    # Las menciones disciplinarias, de riesgo y de monto cero del reporte no son hallazgos.
    assert all(s.referencia_original.lower() not in ("sanciones",) for s in resultado.sanciones)

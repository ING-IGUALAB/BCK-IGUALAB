"""Autoridad que sanciona escrita tras el monto («multa de S/ 5,000 por el OEFA»), versión del detector 2026-10-26.2.

Regla general: justo después del monto (o del término) vale «por [parte de] [el|la] X» sin participio, pero solo si X es una
sigla conocida o un nombre institucional completo. «por el incumplimiento…», «por la norma» o una sigla cualquiera no son una
autoridad. Sin red, BD ni proveedores.
"""
import pytest

from app.modules.ingesta.domain.deteccion_sanciones import detectar_sanciones


def _unica(texto: str):
    resultado = detectar_sanciones(texto)
    assert resultado.total == 1
    return resultado.sanciones[0]


@pytest.mark.parametrize(
    ("texto", "autoridad", "monto"),
    [
        pytest.param("La compañía recibió una multa de S/ 5,000 por el OEFA.", "OEFA", "S/ 5,000", id="sigla-con-articulo"),
        pytest.param("Se aplicó una multa de US$ 12000 por la SUNAT en 2023.", "SUNAT", "US$ 12000", id="otra-moneda-y-periodo"),
        pytest.param("Se pagó S/ 800 de multa por la SUNAFIL.", "SUNAFIL", "S/ 800", id="monto-antes-del-termino"),
        pytest.param(
            "Se aplicó una multa de S/ 8000 por el Organismo de Evaluación y Fiscalización Ambiental (OEFA).",
            "Organismo de Evaluación y Fiscalización Ambiental (OEFA)",
            "S/ 8000",
            id="nombre-completo-con-sigla",
        ),
        pytest.param(
            "Se registró una multa de S/ 3000 por el Ministerio de Energía y Minas.",
            "Ministerio de Energía y Minas",
            "S/ 3000",
            id="nombre-institucional-sin-sigla",
        ),
        pytest.param("Hubo una multa de 2500 soles por parte de INDECOPI.", "INDECOPI", "2500 soles", id="por-parte-de"),
    ],
)
def test_la_autoridad_escrita_tras_el_monto_se_une_a_la_sancion(texto, autoridad, monto):
    sancion = _unica(texto)
    assert sancion.entidad is not None
    assert sancion.entidad.texto == autoridad
    assert texto[sancion.entidad.inicio:sancion.entidad.fin] == autoridad  # posiciones del texto original
    assert sancion.monto is not None
    assert sancion.monto.texto == monto  # el monto sigue vinculado, sin interpretar


@pytest.mark.parametrize(
    "texto",
    [
        pytest.param("Se impuso una multa de S/ 5,000 por el incumplimiento de la norma del sector.", id="causa-no-autoridad"),
        pytest.param("Se impuso una multa de S/ 5,000 por la norma.", id="sustantivo-comun"),
        pytest.param("Se impuso una multa de S/ 5,000 por el retraso en el reporte al OEFA.", id="autoridad-como-destinatario-del-reporte"),
        pytest.param("Se impuso una multa de S/ 5,000 por el Servicio de agua.", id="nombre-institucional-incompleto"),
        pytest.param("Se impuso una multa de S/ 5,000 por el XYZ.", id="sigla-generica-desconocida"),
        pytest.param("Se pagó una multa de S/ 5,000. Por el OEFA se emitió un informe distinto.", id="otra-oracion"),
    ],
)
def test_lo_que_no_nombra_una_autoridad_no_se_toma_como_tal(texto):
    sancion = _unica(texto)
    assert sancion.entidad is None
    assert sancion.monto is not None


def test_dos_autoridades_tras_el_monto_no_se_resuelven():
    sancion = _unica("Se registró una multa de S/ 5,000 por el OEFA y la SUNAFIL.")
    assert sancion.entidad is None
    assert "entidad_ambigua" in sancion.motivos_revision


@pytest.mark.parametrize(
    ("texto", "autoridad", "monto"),
    [
        pytest.param("Se impuso una multa de S/ 5,000 por parte del OEFA.", "OEFA", "S/ 5,000", id="tras-monto-del"),
        pytest.param("Se impuso una multa de S/ 5,000 por parte de la SUNAT.", "SUNAT", "S/ 5,000", id="tras-monto-de-la"),
        pytest.param("Se impuso una multa de S/ 750 por parte de OSINERGMIN.", "OSINERGMIN", "S/ 750", id="tras-monto-de-sin-articulo"),
        pytest.param(
            "Se impuso una multa de US$ 9000 por parte del Ministerio de Energía y Minas.",
            "Ministerio de Energía y Minas",
            "US$ 9000",
            id="tras-monto-nombre-institucional",
        ),
        pytest.param("La empresa recibió una multa por parte del OEFA de S/ 5,000.", "OEFA", "S/ 5,000", id="entre-termino-y-monto"),
        pytest.param("La empresa recibió una multa impuesta por parte del OEFA de S/ 5,000.", "OEFA", "S/ 5,000", id="con-participio"),
        pytest.param("Por parte del OEFA se impuso una multa de S/ 5,000.", "OEFA", "S/ 5,000", id="al-inicio-de-la-oracion"),
    ],
)
def test_por_parte_del_o_de_la_autoridad_se_une_a_la_sancion(texto, autoridad, monto):
    sancion = _unica(texto)
    assert sancion.entidad is not None
    assert sancion.entidad.texto == autoridad
    assert texto[sancion.entidad.inicio:sancion.entidad.fin] == autoridad
    assert sancion.monto is not None
    assert sancion.monto.texto == monto


@pytest.mark.parametrize(
    "texto",
    [
        pytest.param("Se impuso una multa de S/ 5,000 por parte del incumplimiento de la norma.", id="causa-con-del"),
        pytest.param("Se impuso una multa de S/ 5,000 por parte del Servicio de agua.", id="nombre-institucional-incompleto-con-del"),
        pytest.param("Se impuso una multa de S/ 5,000 por parte del XYZ.", id="sigla-generica-con-del"),
        pytest.param("Se impuso una multa de S/ 5,000 por parte del área de cumplimiento.", id="area-interna-con-del"),
    ],
)
def test_por_parte_del_algo_que_no_es_una_autoridad_no_se_toma_como_tal(texto):
    sancion = _unica(texto)
    assert sancion.entidad is None
    assert sancion.monto is not None


def test_dos_autoridades_con_por_parte_del_no_se_resuelven():
    sancion = _unica("Se registró una multa de S/ 5,000 por parte del OEFA y de la SUNAFIL.")
    assert sancion.entidad is None
    assert "entidad_ambigua" in sancion.motivos_revision


def test_la_autoridad_no_pasa_a_la_oracion_siguiente_que_no_la_nombra():
    primera, segunda = detectar_sanciones(
        "La empresa recibió una multa de S/ 5,000 por el OEFA. La multa fue pagada en el mismo año."
    ).sanciones
    assert primera.entidad is not None
    assert primera.entidad.texto == "OEFA"
    assert primera.monto is not None
    assert primera.monto.separador_ambiguo is True  # «5,000» sigue sin interpretarse
    assert segunda.entidad is None
    assert segunda.monto is None  # sin monto: nunca cero


def test_las_formas_que_ya_funcionaban_siguen_dando_la_misma_autoridad():
    for texto in (
        "El OEFA impuso una multa de S/ 5,000 a la empresa.",
        "La empresa recibió una multa de S/ 5,000 impuesta por el OEFA.",
        "La empresa recibió una multa del OEFA por S/ 5,000.",
    ):
        assert _unica(texto).entidad.texto == "OEFA", texto

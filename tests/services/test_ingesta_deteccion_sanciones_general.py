"""Detector de sanciones económicas: causas generales del defecto «cero sanciones» (RF-020 / RN-031).

Nivel: unidad, sin BD, proveedor ni red. Los dos documentos de `datos/` (`energia_andina_gri_2024.md`,
`sanciones_mixtas_gri_2024.md`, usado en CP37) son la EVIDENCIA del defecto y casos de regresión; NO son plantillas: el resto
del archivo comprueba FAMILIAS de ejemplos (con y sin formato, otras empresas, entidades, cantidades, orden de columnas,
moneda en celda o encabezado, importe conocido y no determinado, negaciones, hipótesis) para que ninguna regla dependa de un
nombre de archivo, empresa, año, cifra o disposición concreta.

Es una heurística léxica: estas pruebas fijan el comportamiento acordado, no una precisión medida sobre documentos reales.
`total` cuenta MENCIONES, no sucesos (ver `test_el_conteo_es_de_menciones_*`).
"""
import gc
import hashlib
import re
import time
from pathlib import Path

import pytest

from app.modules.ingesta.domain import vista_markdown
from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from app.modules.ingesta.domain.deteccion_sanciones import MAX_LONGITUD_CITA, VERSION_DETECTOR_SANCIONES, detectar_sanciones

DATOS = Path(__file__).parent / "datos"
NL = "\n"


def leer(nombre: str) -> str:
    return (DATOS / nombre).read_bytes().decode("utf-8")


def verificar(texto, resultado):
    """Propiedades que debe cumplir CUALQUIER resultado, también con formato Markdown: todo dato es un trozo exacto del original."""
    for s in resultado.sanciones:
        assert texto[s.inicio:s.fin] == s.referencia_original
        assert texto[s.cita_inicio:s.cita_fin] == s.cita
        assert s.cita_inicio <= s.inicio < s.fin <= s.cita_fin
        for dato in (s.entidad, s.periodo, *s.fechas_en_contexto):
            if dato is not None:
                assert texto[dato.inicio:dato.fin] == dato.texto
        if s.monto is not None:
            assert texto[s.monto.inicio:s.monto.fin] == s.monto.texto
            assert (s.monto.unidad_inicio is None) == (s.monto.unidad_fin is None)
            if s.monto.unidad_inicio is not None:
                assert texto[s.monto.unidad_inicio:s.monto.unidad_fin] == s.monto.unidad
        for c in s.calificadores:
            assert texto[c.inicio:c.fin] == c.texto
        assert not hasattr(s, "estado")


def detectar(texto):
    resultado = detectar_sanciones(texto)
    verificar(texto, resultado)
    return resultado


def una(texto):
    resultado = detectar(texto)
    assert resultado.total == 1, resultado
    return resultado.sanciones[0]


def ninguna(texto):
    resultado = detectar(texto)
    assert resultado.total == 0, resultado
    return resultado


def sin_formato(texto: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_]", "", texto)).strip()


# ============================================ Documentos que evidenciaron el defecto ============================================

def test_energia_andina_detecta_la_sancion_del_parrafo_y_la_de_la_tabla():
    texto = leer("energia_andina_gri_2024.md")
    parrafo, fila = detectar(texto).sanciones
    assert (parrafo.en_tabla, parrafo.referencia_original) == (False, "sanción")
    assert sin_formato(parrafo.monto.texto) == "32,000 soles"
    assert parrafo.entidad.texto == "OEFA"
    assert parrafo.periodo.texto == "2024"
    assert "**sanción**" in parrafo.cita  # la cita conserva el formato del original
    assert set(parrafo.base_deteccion) == {"monto_vinculado", "verbo_de_imposicion_o_pago"}

    assert fila.en_tabla
    assert fila.cita == "| Sanción administrativa | OEFA | 32,000 | Subsanada |"
    assert (fila.monto.texto, fila.monto.numero_texto) == ("32,000", "32,000")
    assert (fila.monto.unidad, fila.monto.vinculo) == ("S/", "encabezado_de_columna")
    assert fila.entidad.texto == "OEFA"
    assert fila.base_deteccion == ("fila_de_tabla",)
    # La moneda no está en la fila: consta en su encabezado, y el vínculo apunta a él.
    linea = texto[texto.rfind(NL, 0, fila.monto.unidad_inicio) + 1:texto.find(NL, fila.monto.unidad_inicio)]
    assert linea == "| Tipo | Entidad | Monto (S/) | Estado |"
    assert [c.codigo for c in fila.calificadores] == []  # «Subsanada» no es un calificador del vocabulario: no se inventa


def test_cp37_sanciones_con_monto_y_sin_monto():
    texto = leer("sanciones_mixtas_gri_2024.md")
    resultado = detectar(texto)
    con_monto_parrafo, sin_monto_parrafo, con_monto_fila, sin_monto_fila = resultado.sanciones

    assert sin_formato(con_monto_parrafo.monto.texto) == "500,000 soles"
    assert con_monto_parrafo.entidad.texto == "OEFA"
    assert con_monto_fila.monto.texto == "500,000"
    assert (con_monto_fila.monto.unidad, con_monto_fila.entidad.texto) == ("S/", "OEFA")

    for sin_monto in (sin_monto_parrafo, sin_monto_fila):
        assert sin_monto.monto is None  # no cuantificada: nunca cero
        assert "monto_no_especificado_en_el_documento" in sin_monto.motivos_revision
        assert "caracter_economico_en_contexto" in sin_monto.base_deteccion
    # La oración que dice que el importe no consta forma parte de la evidencia citada.
    assert "no especifica un monto económico" in sin_monto_parrafo.cita
    assert "pendiente\nde determinación" in sin_monto_parrafo.cita
    assert sin_monto_parrafo.entidad is None  # «el organismo regulador» no es una entidad nombrada: no se toma de la tabla
    assert sin_monto_fila.entidad.texto == "OEFA"
    assert sin_monto_fila.cita == "| Sanción administrativa | OEFA | No especificado | Pendiente de determinación |"
    # «Pendiente de determinación» (del importe) no se convierte en «pendiente de pago».
    for s in resultado.sanciones:
        assert "pendiente_de_pago" not in [c.codigo for c in s.calificadores]
        if s.monto is not None:
            assert s.monto.numero_texto != "0"


def test_los_documentos_de_evidencia_no_se_alteran_ni_cambia_su_sha256():
    for nombre in ("energia_andina_gri_2024.md", "sanciones_mixtas_gri_2024.md"):
        crudo = (DATOS / nombre).read_bytes()
        antes = hashlib.sha256(crudo).hexdigest()
        texto = crudo.decode("utf-8")
        detectar(texto)
        analizar_texto(texto)
        assert hashlib.sha256(texto.encode("utf-8")).hexdigest() == antes
        assert vista_markdown.construir_vista(texto).original is texto


def test_el_conteo_es_de_menciones_no_de_sucesos_y_no_se_fusionan():
    """El mismo suceso aparece en un párrafo y en una tabla: son DOS menciones. Dos sanciones distintas con la misma entidad
    y el mismo monto tampoco se fusionan."""
    texto = leer("sanciones_mixtas_gri_2024.md")
    assert detectar(texto).total == 4  # dos sucesos descritos cada uno en un párrafo y en una fila de tabla
    mismo_monto = (
        "| Tipo | Entidad | Monto (S/) |" + NL + "|---|---|---|" + NL
        + "| Multa | OEFA | 500 |" + NL + "| Multa | OEFA | 500 |" + NL
    )
    assert detectar(mismo_monto).total == 2


# ============================================ 1. Formato Markdown ============================================

VARIANTES = {
    "plano": "La empresa recibió una sanción administrativa del OEFA por un monto de 32,000 soles.",
    "negrita": "La empresa recibió una **sanción** administrativa del OEFA por un monto de **32,000 soles**.",
    "cursiva*": "La empresa recibió una *sanción* administrativa del *OEFA* por un monto de *32,000 soles*.",
    "cursiva_": "La empresa recibió una _sanción_ administrativa del OEFA por un monto de _32,000 soles_.",
    "negrita_y_cursiva": "La empresa recibió una ***sanción*** administrativa del **OEFA** por un monto de ***32,000*** soles.",
    "enlace": "La empresa recibió una [sanción](https://x.pe/a_b) administrativa del OEFA por un monto de 32,000 soles.",
    "nota_al_pie": "La empresa recibió una sanción[^1] administrativa del OEFA por un monto de 32,000 soles.",
    "saltos_de_linea": "La empresa recibió una" + NL + "sanción administrativa del OEFA" + NL + "por un monto" + NL + "de 32,000 soles.",
    "saltos_y_negrita": "La empresa recibió una **sanción**" + NL + "administrativa del **OEFA** por un" + NL + "**monto** de **32,000" + NL + "soles**.",
    "crlf": "La empresa recibió una **sanción**\r\nadministrativa del OEFA por un monto\r\nde 32,000 soles.",
    "cita": "> La empresa recibió una **sanción**" + NL + "> administrativa del OEFA por un monto" + NL + "> de 32,000 soles.",
    "lista": "- La empresa recibió una **sanción** administrativa del OEFA" + NL + "  por un monto de 32,000 soles.",
    "salto_html": "La empresa recibió una sanción administrativa<br>del OEFA por un monto de 32,000 soles.",
    "salto_duro": "La empresa recibió una sanción administrativa\\" + NL + "del OEFA por un monto de 32,000 soles.",
    "espacio_duro": "La empresa recibió una sanción administrativa del OEFA por un monto de&nbsp;32,000 soles.",
}


@pytest.mark.parametrize("etiqueta", sorted(VARIANTES))
def test_el_formato_markdown_no_cambia_el_significado(etiqueta):
    texto = VARIANTES[etiqueta]
    s = una(texto)
    plano = una(VARIANTES["plano"])
    assert s.referencia_original == "sanción"
    assert (s.monto.numero_texto, s.monto.unidad, s.monto.vinculo) == ("32,000", "soles", plano.monto.vinculo)
    assert sin_formato(s.monto.texto) == "32,000 soles"
    assert s.entidad.texto == "OEFA"
    assert s.base_deteccion == plano.base_deteccion
    assert s.motivos_revision == plano.motivos_revision
    assert s.tipo_termino == plano.tipo_termino


def test_el_formato_en_una_tabla_no_cambia_el_significado():
    plano = "| Tipo | Entidad | Monto (S/) |" + NL + "|---|---|---|" + NL + "| Sanción económica | OEFA | 32,000 |" + NL
    con_formato = (
        "| **Tipo** | *Entidad* | **Monto (S/)** |" + NL + "|---|---|---|" + NL + "| **Sanción** económica | *OEFA* | **32,000** |" + NL
    )
    a, b = una(plano), una(con_formato)
    assert (a.monto.texto, a.monto.unidad, a.entidad.texto) == (b.monto.texto, b.monto.unidad, b.entidad.texto)
    assert b.monto.unidad == "S/"  # el literal del encabezado, sin las marcas de énfasis que lo rodean
    assert (a.base_deteccion, a.motivos_revision) == (b.base_deteccion, b.motivos_revision)


def test_las_marcas_dentro_de_un_dato_se_conservan_en_su_texto_literal_y_fuera_no():
    texto = "Recibimos una **sanción** *económica* de **S/ 5,000**."
    s = una(texto)
    assert s.referencia_original == "sanción** *económica"  # un trozo contiguo del original, con la marca interna
    assert s.monto.texto == "S/ 5,000"  # las marcas de los extremos no son parte del dato
    assert s.cita == texto  # la cita conserva el formato equilibrado


def test_el_formato_no_se_confunde_con_cifras_ni_nombres():
    s = una("Se aplicó una multa de S/ 3*4 mil y un_nombre_con_guiones no cambia.")
    assert s.monto.texto == "S/ 3"
    assert "un_nombre_con_guiones" in s.cita
    assert "3*4" in s.cita


def test_un_bloque_de_codigo_no_se_analiza_aunque_tenga_formato():
    ninguna("```" + NL + "La empresa recibió una **multa** de S/ 5,000." + NL + "```")


# ============================================ 2. Vínculo entre sanción, verbo, entidad e importe ============================================

@pytest.mark.parametrize(
    ("texto", "monto", "unidad", "entidad"),
    [
        ("La SUNAFIL impuso a Minera del Sur una multa grave por un monto de S/ 45,300.", "S/ 45,300", "S/", "SUNAFIL"),
        ("Textiles Pacífico recibió una sanción económica de la SUNAT por US$ 12,000 durante 2023.", "US$ 12,000", "US$", "SUNAT"),
        ("El INDECOPI aplicó una sanción administrativa de carácter económico por un total de S/ 150,000.", "S/ 150,000", "S/", "INDECOPI"),
        ("Se notificó una multa ambiental equivalente a 59.078 UIT.", "59.078 UIT", "UIT", None),
        ("La sanción de S/ 1,200 impuesta por la SMV fue confirmada.", "S/ 1,200", "S/", "SMV"),
        ("El OEFA impuso a la empresa una multa administrativa ambiental del OEFA por un monto de S/ 8,500.", "S/ 8,500", "S/", "OEFA"),
        ("La SUNAFIL emitió una sanción económica por incumplimiento de normas laborales, por un monto de S/ 12,000.", "S/ 12,000", "S/", "SUNAFIL"),
        ("La SUNAFIL notificó una multa grave a la empresa, equivalente a 12 UIT.", "12 UIT", "UIT", None),
        ("Se aplicó una multa impuesta el 12 de marzo de 2024 por S/ 5,000.", "S/ 5,000", "S/", None),
        ("La empresa fue objeto de una sanción económica impuesta por el INDECOPI por un total de US$ 20,000.", "US$ 20,000", "US$", "INDECOPI"),
        ("La multa impuesta por el OSIPTEL ascendió a S/ 3,000.", "S/ 3,000", "S/", "OSIPTEL"),
        ("La multa impuesta por la AUTORIDADX fue de 3,000 soles.", "3,000 soles", "soles", "AUTORIDADX"),
        ("Se registró una multa (S/ 900) de la SUNAT.", "S/ 900", "S/", "SUNAT"),
        ("Multa: S/ 700 impuesta por el OEFA.", "S/ 700", "S/", "OEFA"),
        ("Pagamos S/ 700 por concepto de multa al OEFA.", "S/ 700", "S/", None),
        ("La multa impuesta por OEFA, cuyo monto es de S/ 5,000, fue apelada.", "S/ 5,000", "S/", "OEFA"),
        ("La filial Andes Gas S.A. recibió una multa de S/ 2,000 de parte de la SBS.", "S/ 2,000", "S/", "SBS"),
        ("Se impuso una sanción firme de carácter pecuniario a la empresa, por 3 UIT.", "3 UIT", "UIT", None),
        ("Se impuso una multa de dos soles con 50 UIT.", None, None, None),
    ],
)
def test_el_importe_se_une_a_la_sancion_pese_a_calificativos_entidad_o_expresiones_intermedias(texto, monto, unidad, entidad):
    s = una(texto)
    assert (s.monto.texto if s.monto else None) == monto
    assert (s.monto.unidad if s.monto else None) == unidad
    assert (s.entidad.texto if s.entidad else None) == entidad


def test_un_termino_verbal_sin_importe_y_un_importe_que_pertenece_a_otro_termino_no_se_unen_de_mas():
    assert una("El OEFA multó a la empresa.").monto is None
    s = una("Pagamos S/ 700 de multa y recibimos otra multa.")  # el segundo término reitera al primero y no hereda el importe
    assert s.monto.texto == "S/ 700"
    assert s.monto.vinculo == "monto_antes_del_termino"


def test_un_verbo_de_autoridad_cuenta_como_imposicion_aunque_no_sea_impuso():
    for verbo in ("emitió", "dictó", "ordenó", "notificó", "aplicó"):
        s = una(f"La autoridad {verbo} una sanción económica a la empresa.")
        assert "verbo_de_imposicion_o_pago" in s.base_deteccion, verbo
    assert "verbo_de_imposicion_o_pago" in una("La empresa fue objeto de una sanción económica por S/ 800.").base_deteccion


@pytest.mark.parametrize(
    "texto",
    [
        "La sanción administrativa fue impuesta por la SUNAT. La empresa invirtió S/ 500,000 en mitigación.",
        "La empresa recibió una multa económica y registró ingresos por S/ 40,000.",
        "Recibimos una multa económica relacionada con las ventas por un monto de S/ 9,000.",
        "La multa impuesta por el OEFA representa el 2% de los ingresos de S/ 5,000,000.",
        "Se impuso una multa a la empresa; el presupuesto de mitigación es de S/ 2,000.",
    ],
)
def test_un_importe_de_otra_cosa_no_se_une_a_la_sancion(texto):
    resultado = detectar(texto)
    assert all(s.monto is None for s in resultado.sanciones)


def test_dos_sanciones_en_una_oracion_conservan_cada_una_su_importe():
    resultado = detectar("La SUNAT impuso una multa de S/ 1,000 y el OEFA impuso una sanción económica por un monto de S/ 2,000.")
    assert [s.monto.texto for s in resultado.sanciones] == ["S/ 1,000", "S/ 2,000"]
    # Límite conocido (anterior a este cambio): con dos entidades en la misma cláusula no se adivina cuál es de cuál.
    assert [s.entidad for s in resultado.sanciones] == [None, None]
    assert all("entidad_ambigua" in s.motivos_revision for s in resultado.sanciones)


# ============================================ 3. Tablas: monto, moneda y entidad en columnas distintas ============================================

def test_la_moneda_del_encabezado_se_conserva_literal_y_con_su_posicion():
    texto = "| Tipo | Entidad | Monto (S/) |" + NL + "|---|---|---|" + NL + "| Sanción administrativa | OEFA | 32,000 |" + NL
    s = una(texto)
    assert (s.monto.texto, s.monto.unidad, s.monto.vinculo) == ("32,000", "S/", "encabezado_de_columna")
    assert texto[s.monto.unidad_inicio:s.monto.unidad_fin] == "S/"
    assert s.monto.unidad_inicio < s.cita_inicio  # el encabezado queda FUERA de la cita de la fila: por eso se ubica aparte
    assert s.monto.separador_ambiguo is True  # «32,000» sigue señalándose, no se interpreta
    assert "monto_con_separador_ambiguo" in s.motivos_revision


@pytest.mark.parametrize(
    ("tabla", "monto", "unidad", "entidad", "vinculo", "calificadores"),
    [
        # columnas reordenadas: la entidad y la moneda salen del encabezado, no de la posición
        (
            "| Monto (US$) | Estado | Autoridad | Tipo ||---|---|---|---|| 15,000 | Pagada | SUNAFIL | Multa por incumplimiento |",
            "15,000", "US$", "SUNAFIL", "encabezado_de_columna", ["pagada"],
        ),
        (
            "| Entidad sancionadora | Concepto | Importe en soles |||---|---|---||| INDECOPI | Multa ambiental | 750 |",
            "750", "soles", "INDECOPI", "encabezado_de_columna", [],
        ),
        # moneda en la celda
        (
            "| Tipo | Entidad | Monto |||---|---|---||| Sanción económica | SUNAT | S/ 4,500 |",
            "S/ 4,500", "S/", "SUNAT", "fila_de_tabla", [],
        ),
        # moneda en otra columna de la misma fila
        (
            "| Tipo | Entidad | Monto | Moneda |||---|---|---|---||| Multa | OEFA | 12,000 | Soles |",
            "12,000", "Soles", "OEFA", "columna_de_moneda", [],
        ),
        # escala explícita en el encabezado: se conserva, no se multiplica
        (
            "| Concepto | Monto (miles de S/) |||---|---||| Multa impuesta por OEFA | 32 |",
            "32", "miles de S/", "OEFA", "encabezado_de_columna", [],
        ),
        # solo la moneda como encabezado
        ("| Multa | Entidad | S/ |||---|---|---||| Multa ambiental | SERNANP | 1,800 |", "1,800", "S/", "SERNANP", "encabezado_de_columna", []),
        # el importe total y el saldo pendiente son columnas distintas: solo cuenta el total
        (
            "| Tipo | Entidad | Monto total (S/) | Saldo pendiente (S/) |||---|---|---|---||| Multa | OEFA | 10,000 | 2,500 |",
            "10,000", "S/", "OEFA", "encabezado_de_columna", [],
        ),
        (
            "| Tipo | Entidad | Saldo pendiente (S/) | Monto total (S/) |||---|---|---|---||| Multa | OEFA | 2,500 | 10,000 |",
            "10,000", "S/", "OEFA", "encabezado_de_columna", [],
        ),
    ],
)
def test_tablas_con_monto_moneda_y_entidad_en_columnas_distintas(tabla, monto, unidad, entidad, vinculo, calificadores):
    s = una(_tabla_en_lineas(tabla))
    assert s.en_tabla
    assert (s.monto.texto, s.monto.unidad, s.monto.vinculo) == (monto, unidad, vinculo)
    assert s.entidad.texto == entidad
    assert [c.codigo for c in s.calificadores] == calificadores


def _tabla_en_lineas(compacta: str) -> str:
    """Las tablas de los casos vienen en una sola línea y cada límite entre filas es `||` o `|||` (cierre de una fila, apertura
    de la siguiente): se expanden a líneas reales."""
    return re.sub(r"\|\|\|?", "|" + NL + "|", compacta) + NL


def test_un_saldo_pendiente_no_es_el_monto_total_de_la_sancion():
    s = una(_tabla_en_lineas("| Concepto | Saldo pendiente (S/) |||---|---||| Multa impuesta por OEFA | 2,500 |"))
    assert s.monto is None
    assert s.motivos_revision == ("solo_se_menciona_un_saldo_o_pago_parcial",)
    s = una("La multa impuesta por OEFA mantiene un saldo pendiente de S/ 2,000.")
    assert s.monto is None
    assert "solo_se_menciona_un_saldo_o_pago_parcial" in s.motivos_revision
    assert una("Se impuso una multa por un monto de S/ 10,000, con un saldo pendiente de S/ 2,000.").monto.texto == "S/ 10,000"


@pytest.mark.parametrize(
    ("tabla", "motivo"),
    [
        # sin moneda en ninguna parte: no se inventa
        ("| Concepto | Monto |||---|---||| Multa impuesta por OEFA | 2500 |", "monto_no_asociado_con_seguridad"),
        # dos monedas en el encabezado: no se sabe cuál corresponde
        ("| Concepto | Monto (S/ o US$) |||---|---||| Multa impuesta por OEFA | 2,500 |", "monto_no_asociado_con_seguridad"),
        # otra magnitud
        ("| Concepto | Ingresos (S/) |||---|---||| Multa impuesta por OEFA | 5,000,000 |", None),
        ("| Concepto | Inversión (US$) |||---|---||| Multa impuesta por OEFA | 90,000 |", None),
        # dos columnas de importe con moneda propia (celda con moneda): no se elige
        ("| Concepto | Monto | Saldo |||---|---|---||| Multa impuesta por OEFA | S/ 1,000 | S/ 400 |", "montos_multiples_en_fila"),
    ],
)
def test_no_se_inventa_la_moneda_ni_se_toma_otra_magnitud(tabla, motivo):
    s = una(_tabla_en_lineas(tabla))
    assert s.monto is None
    if motivo:
        assert motivo in s.motivos_revision


def test_una_tabla_o_fila_hipotetica_no_usa_los_encabezados():
    ninguna(_tabla_en_lineas("| Riesgo | Sanción potencial | Monto (S/) |||---|---|---||| Ambiental | Sanción económica | 50,000 |"))
    ninguna(_tabla_en_lineas("| Tema | Tipo | Monto (S/) |||---|---|---||| Relaves | Riesgo de sanción económica | 50,000 |"))
    ninguna(_tabla_en_lineas("| Escenario | Multa estimada (S/) |||---|---||| Base | 40,000 |"))


def test_un_cero_con_moneda_en_el_encabezado_no_es_una_sancion():
    resultado = ninguna(_tabla_en_lineas("| Tipo | Entidad | Monto (S/) |||---|---|---||| Multa | OEFA | 0 |"))
    assert resultado.descartes_por_motivo == {"monto_cero": 1}


def test_una_fila_deteriorada_no_se_interpreta_por_columnas():
    texto = "| Tipo | Entidad | Monto (S/) |" + NL + "|---|---|---|" + NL + "| Multa impuesta por OEFA | 32,000 |" + NL
    s = una(texto)
    assert s.monto is None  # una columna menos: el encabezado no se usa
    assert s.entidad.texto == "OEFA"  # de la propia celda
    assert s.cita == "Multa impuesta por OEFA"


def test_dos_entidades_en_la_celda_de_entidad_no_se_resuelven():
    s = una(_tabla_en_lineas("| Tipo | Entidad | Monto (S/) |||---|---|---||| Multa | OEFA / SUNAFIL | 1,000 |"))
    assert s.entidad is None
    assert "entidad_ambigua" in s.motivos_revision
    assert s.monto.texto == "1,000"


def test_una_celda_que_es_una_entidad_conocida_la_aporta_aunque_su_encabezado_no_la_nombre():
    s = una(_tabla_en_lineas("| Concepto | Quien | Monto (S/) |||---|---|---||| Multa | OEFA | 500 |"))
    assert s.entidad.texto == "OEFA"
    # Una celda que no es una entidad conocida y cuyo encabezado no la nombra no se toma como entidad.
    assert una(_tabla_en_lineas("| Concepto | Quien | Monto (S/) |||---|---|---||| Multa | Planta Sur | 500 |")).entidad is None


def test_un_cero_con_la_moneda_en_otra_columna_tampoco_es_una_sancion():
    resultado = ninguna(_tabla_en_lineas("| Concepto | Monto | Moneda |||---|---|---||| Multa | 0 | Soles |"))
    assert resultado.descartes_por_motivo == {"monto_cero": 1}


def test_los_calificadores_conservan_su_texto_literal_aunque_haya_formato():
    texto = "Se impuso una **multa** de S/ 500, que fue **pagada** en 2024 y luego *impugnada*."
    s = una(texto)
    assert [(c.codigo, c.texto) for c in s.calificadores] == [("pagada", "pagada"), ("impugnada", "impugnada")]
    assert s.cita == texto


def test_una_celda_de_entidad_sin_dato_no_inventa_entidad():
    assert una(_tabla_en_lineas("| Tipo | Entidad | Monto (S/) |||---|---|---||| Multa impuesta | N/A | 1,000 |")).entidad is None


def test_el_estado_de_la_fila_aporta_calificadores_literales():
    s = una(_tabla_en_lineas("| Tipo | Entidad | Monto (S/) | Estado |||---|---|---|---||| Multa | OEFA | 900 | Impugnada |"))
    assert [(c.codigo, c.texto) for c in s.calificadores] == [("impugnada", "Impugnada")]
    s = una(_tabla_en_lineas("| Tipo | Entidad | Monto (S/) | Estado |||---|---|---|---||| Multa | OEFA | 900 | No pagada |"))
    assert [c.codigo for c in s.calificadores] == []  # negar el pago no lo registra


# ============================================ 4. Importe desconocido: no es «no hay sanción» ============================================

@pytest.mark.parametrize(
    ("texto", "entidad"),
    [
        ("Se impuso una sanción económica a la empresa, pero el monto no ha sido determinado.", None),
        ("No se especificó el monto de la multa impuesta por la SUNAT.", "SUNAT"),
        ("No se especifica el monto de la sanción económica impuesta por el OEFA.", "OEFA"),
        ("El importe de la sanción impuesta por la SUNAT está pendiente de determinación.", "SUNAT"),
        ("El OEFA impuso una sanción administrativa cuyo monto no se especifica.", "OEFA"),
        ("La empresa recibió una multa sin monto determinado.", None),
        ("La SUNAFIL emitió una sanción administrativa a la empresa. La resolución no indica el importe y queda pendiente de definición.", "SUNAFIL"),
        ("El organismo regulador aplicó una sanción administrativa. Su monto está por determinar.", None),
    ],
)
def test_una_sancion_impuesta_con_importe_desconocido_se_conserva_como_no_cuantificada(texto, entidad):
    s = una(texto)
    assert s.monto is None  # nunca cero
    assert "monto_no_especificado_en_el_documento" in s.motivos_revision
    assert (s.entidad.texto if s.entidad else None) == entidad


def test_la_oracion_que_dice_que_el_importe_no_consta_forma_parte_de_la_cita():
    texto = "La SUNAFIL emitió una sanción administrativa a la empresa. La resolución no indica el importe. Otro asunto distinto."
    s = una(texto)
    assert s.cita == "La SUNAFIL emitió una sanción administrativa a la empresa. La resolución no indica el importe."


def test_el_caracter_economico_de_una_sancion_generica_lo_da_el_contexto_no_la_palabra():
    s = una("La SUNAFIL emitió una sanción administrativa a la empresa. La resolución no indica el importe.")
    assert "caracter_economico_en_contexto" in s.base_deteccion
    assert "verbo_de_imposicion_o_pago" in s.base_deteccion
    # Sin ese contexto, «sanción administrativa» no es una sanción económica.
    ninguna("La SUNAFIL emitió una sanción administrativa a la empresa por incumplir un plazo.")
    ninguna("El OEFA impuso una sanción administrativa de amonestación.")


def test_el_importe_desconocido_exige_que_sea_el_de_la_sancion():
    ninguna("La SUNAFIL emitió una sanción administrativa a la empresa. No se especifica el monto de la inversión ambiental.")
    ninguna("La SUNAFIL emitió una sanción administrativa. Las ventas del año quedan pendientes de determinación.")


@pytest.mark.parametrize(
    "tabla",
    [
        "| Tipo | Entidad | Monto (S/) |||---|---|---||| Sanción administrativa | OEFA | No especificado |",
        "| Tipo | Entidad | Monto (S/) |||---|---|---||| Sanción administrativa | OEFA | No determinado |",
        "| Tipo | Entidad | Monto (S/) |||---|---|---||| Sanción administrativa | OEFA | Por determinar |",
        "| Tipo | Entidad | Monto (S/) |||---|---|---||| Sanción administrativa | OEFA | Pendiente de determinación |",
        "| Tipo | Entidad | Monto |||---|---|---||| Sanción administrativa | OEFA | N/D |",
        "| Autoridad | Importe | Concepto |||---|---|---||| SUNAFIL | No informado | Multa por seguridad |",
    ],
)
def test_una_columna_de_importes_que_dice_no_especificado_deja_la_sancion_no_cuantificada(tabla):
    s = una(_tabla_en_lineas(tabla))
    assert s.monto is None
    assert "monto_no_especificado_en_el_documento" in s.motivos_revision
    assert s.en_tabla
    assert s.entidad is not None


def test_no_especificado_en_una_columna_que_no_es_de_importes_no_crea_una_sancion():
    ninguna(_tabla_en_lineas("| Tipo | Entidad | Observación |||---|---|---||| Sanción administrativa | OEFA | No especificado |"))


@pytest.mark.parametrize(
    "texto",
    [
        "No se impuso ninguna sanción económica a la empresa.",
        "La empresa no recibió sanciones económicas durante 2024.",
        "Sin sanciones económicas en el periodo.",
        "No se registró multa alguna; el monto es cero.",
        "No se impuso sanción económica alguna y el monto pendiente de determinación corresponde a otro trámite.",
        "Aún no se ha impuesto sanción económica ni se conoce el monto.",
    ],
)
def test_negar_la_sancion_sigue_excluyendo(texto):
    ninguna(texto)


@pytest.mark.parametrize(
    "texto",
    ["No se impuso ninguna sanción económica a la empresa.", "La empresa no recibió sanciones económicas durante 2024."],
)
def test_la_negacion_de_la_sancion_queda_registrada_como_descarte(texto):
    assert ninguna(texto).descartes_por_motivo == {"negacion": 1}


def test_negar_el_monto_no_es_negar_la_sancion_y_negar_la_sancion_si_lo_es():
    assert una("No se especifica el monto de la multa impuesta por la SUNAT.").monto is None
    ninguna("No se impuso multa alguna por la SUNAT; el monto no se especifica.")


def test_un_cero_explicito_sigue_sin_ser_un_importe_desconocido():
    assert ninguna("Se aplicó una multa de S/ 0 en el periodo.").descartes_por_motivo == {"monto_cero": 1}


# ============================================ Exclusiones que se conservan ============================================

@pytest.mark.parametrize(
    "texto",
    [
        "Podrían imponerse sanciones económicas de hasta 100 UIT.",
        "Existe riesgo de sanción económica por incumplimiento ambiental.",
        "El reglamento establece sanciones económicas de entre 1 y 50 UIT.",
        "En caso de incumplimiento se aplicaría una sanción económica cuyo monto no se ha determinado.",
        "Se evaluará imponer una sanción económica cuyo monto no se ha determinado.",
        "El reglamento interno prevé sanciones disciplinarias económicas, como descuentos.",
        "Se aplicó una sanción laboral económica de S/ 100 a un trabajador por faltas graves.",
        "La sanción económica podría afectar los ingresos de S/ 5,000,000.",
        "La empresa fue sancionada por la autoridad ambiental.",
    ],
)
def test_hipotesis_riesgos_sanciones_internas_y_sanciones_no_economicas_siguen_excluidas(texto):
    ninguna(texto)


def test_un_monto_cercano_y_ajeno_no_convierte_en_economica_una_sancion_generica():
    ninguna("La sanción administrativa se discutió. La empresa invirtió S/ 500,000 en mitigación.")
    ninguna("La empresa ahorró US$ 8,000 y el OEFA emitió una resolución sobre la sanción administrativa de un tercero.")


# ============================================ Citas, offsets y estabilidad ============================================

def test_citas_y_offsets_exactos_con_formato_crlf_acentos_y_tablas():
    texto = (
        "## Cumplimiento **normativo**\r\n\r\n"
        "En 2023, la empresa recibió una **sanción** económica de la *SUNAFIL*\r\npor un monto de **S/ 7,500** (ver ñandú).\r\n\r\n"
        "| **Tipo** | Entidad | Monto (S/) |\r\n|---|---|---|\r\n| Multa ambiental | OEFA | 1,250 |\r\n"
    )
    resultado = detectar(texto)
    assert resultado.total == 2
    parrafo, fila = resultado.sanciones
    assert parrafo.periodo.texto == "2023"
    assert parrafo.seccion == "Cumplimiento normativo"
    assert fila.cita == "| Multa ambiental | OEFA | 1,250 |"
    assert texto[parrafo.cita_inicio:parrafo.cita_fin].startswith("En 2023,")


def test_determinismo_y_el_texto_original_no_se_modifica():
    texto = VARIANTES["saltos_y_negrita"]
    copia = str(texto)
    primera = detectar_sanciones(texto)
    segunda = detectar_sanciones(texto)
    assert segunda == primera
    assert texto == copia


UNIDAD_DE_FORMATO = "**a** _b_ [c](d) `e` ~~f~~ "  # un delimitador cada 4-5 caracteres: densidad patológica
FACTOR_DE_TAMANO = 4
# Un coste lineal da un cociente de tiempos ≈ 4 (medido entre 3,9 y 4,6, con y sin cobertura); uno cuadrático, ≈ 16.
RAZON_MAXIMA_ACEPTADA = 8


def _texto_con_formato(repeticiones: int) -> str:
    return (UNIDAD_DE_FORMATO * repeticiones) + NL + "Recibimos una **multa** de **S/ 5,000**." + NL


def _mejor_tiempo(texto: str, ejecuciones: int) -> float:
    """Mejor (menor) de varias ejecuciones: descarta las interrumpidas por otras cargas de la máquina."""
    mejor = float("inf")
    for _ in range(ejecuciones):
        gc.collect()
        inicio = time.perf_counter()
        detectar_sanciones(texto)
        mejor = min(mejor, time.perf_counter() - inicio)
    return mejor


def test_el_formato_abundante_crece_linealmente_con_el_tamano_del_documento():
    """Garantiza que NO hay crecimiento cuadrático (el defecto que tuvo la vista con muchos fragmentos de código en una línea), no
    una velocidad absoluta: un tope en segundos depende de la máquina y de la medición (con `--cov` el mismo trabajo tarda ≈2,5
    veces más). Se compara el tiempo con N y con 4·N repeticiones del mismo texto: la lentitud del entorno y la cobertura
    multiplican ambos tiempos por igual y se cancelan en el cociente."""
    pequeno = _texto_con_formato(4_000)
    grande = _texto_con_formato(4_000 * FACTOR_DE_TAMANO)
    _mejor_tiempo(pequeno, 1)  # calentamiento: compila expresiones regulares y llena la memoización
    t_pequeno = _mejor_tiempo(pequeno, 3)
    t_grande = _mejor_tiempo(grande, 2)
    razon = t_grande / t_pequeno
    assert razon < RAZON_MAXIMA_ACEPTADA, (
        f"El tiempo creció ×{razon:.1f} al multiplicar por {FACTOR_DE_TAMANO} el tamaño ({t_pequeno:.2f} s → {t_grande:.2f} s): "
        f"lineal ≈ {FACTOR_DE_TAMANO}, cuadrático ≈ {FACTOR_DE_TAMANO ** 2}."
    )


def test_el_formato_abundante_conserva_la_deteccion_y_las_posiciones():
    texto = _texto_con_formato(8_000)
    resultado = detectar(texto)
    assert resultado.total == 1
    s = resultado.sanciones[0]
    assert s.monto.numero_texto == "5,000"
    # El texto repetido no tiene puntos: es una sola oración enorme y la cita es la ventana acotada alrededor del término.
    assert s.cita.endswith("Recibimos una **multa** de **S/ 5,000**.")
    # Acotada, no el documento entero. La ventana (≈ MAX_LONGITUD_CITA) se mide en la vista sin marcas; al volver al original
    # recupera las marcas retiradas, por eso puede superar ese valor en una fracción.
    assert len(s.cita) < 2 * MAX_LONGITUD_CITA < len(texto)


# ============================================ Del detector al análisis persistido ============================================

def test_el_analisis_serializado_conserva_el_importe_no_cuantificado_y_la_version_del_detector():
    texto = leer("sanciones_mixtas_gri_2024.md")
    datos = analizar_texto(texto).a_dict()
    assert datos["version_detector_sanciones"] == VERSION_DETECTOR_SANCIONES
    assert datos["resultado"] == "CON_HALLAZGOS"
    sanciones = datos["sanciones"]
    assert [s["monto"] is None for s in sanciones] == [False, True, False, True]
    sin_monto = sanciones[1]
    assert sin_monto["monto"] is None
    assert "monto_no_especificado_en_el_documento" in sin_monto["motivos_revision"]
    con_unidad_de_encabezado = sanciones[2]["monto"]
    assert (con_unidad_de_encabezado["unidad"], con_unidad_de_encabezado["vinculo"]) == ("S/", "encabezado_de_columna")
    assert texto[con_unidad_de_encabezado["unidad_inicio"]:con_unidad_de_encabezado["unidad_fin"]] == "S/"
    advertencias = {a["detalles"]["motivo"] for a in datos["advertencias"] if a["categoria"] == "sanciones"}
    assert "monto_no_especificado_en_el_documento" in advertencias


def test_un_documento_con_solo_una_sancion_no_cuantificada_ya_no_es_observado():
    texto = "# Informe" + NL + NL + "La SUNAFIL emitió una sanción administrativa. La resolución no especifica un monto económico." + NL
    datos = analizar_texto(texto).a_dict()
    assert datos["resultado"] == "CON_HALLAZGOS"
    assert datos["motivos"] == ["sanciones_economicas"]

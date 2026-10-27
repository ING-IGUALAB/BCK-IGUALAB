"""Citas y secciones LEGIBLES (presentación) y límites de cita sin cortes a mitad de palabra. Sin red, BD ni proveedores.

`texto_legible` / `seccion_legible` solo cambian la presentación: el original (con sus marcas) sigue siendo la evidencia. Se
comprueba que retiran únicamente formato y que conservan el contenido (números, monedas, símbolos, acentos, enlaces, celdas).
`recortar_cita` y los detectores producen citas que son fragmentos EXACTOS del texto y no empiezan ni terminan dentro de una palabra.
"""
import pytest

from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from app.modules.ingesta.domain.legibilidad import seccion_legible, texto_legible
from app.modules.ingesta.domain.texto import recortar_cita


@pytest.mark.parametrize(
    ("original", "esperado"),
    [
        pytest.param("### Emisiones", "Emisiones", id="encabezado"),
        pytest.param("## Gestión ambiental ##", "Gestión ambiental", id="encabezado-con-cierre"),
        pytest.param("**Gestión ambiental**", "Gestión ambiental", id="negrita"),
        pytest.param("_Gestión_ y *ambiental* y ~~antes~~", "Gestión y ambiental y antes", id="cursiva-y-tachado"),
        pytest.param("La empresa<br>redujo emisiones", "La empresa\nredujo emisiones", id="br-conserva-salto"),
        pytest.param("La empresa<BR />redujo<br/>emisiones", "La empresa\nredujo\nemisiones", id="br-variantes"),
        pytest.param("[informe 2025](https://x.pe/a.pdf) y [otro][1]", "informe 2025 y otro", id="enlaces-conservan-el-texto"),
        pytest.param("Ver ![logo](http://externo.example/x.png) fin", "Ver logo fin", id="imagen-solo-texto-alternativo"),
        pytest.param("Dato[^1] citado", "Dato citado", id="nota-al-pie"),
        pytest.param("> primera\n> segunda", "primera\nsegunda", id="cita-en-bloque"),
        pytest.param("- uno\n- dos\n\n\n\n1. tres", "- uno\n- dos\n\n1. tres", id="listas-y-parrafos-se-conservan"),
        pytest.param("Ver `GRI 305-1` y **`código`**", "Ver GRI 305-1 y código", id="codigo-en-linea"),
        pytest.param("Texto \\*literal\\* y C:\\Users\\x", "Texto *literal* y C:\\Users\\x", id="escapes-de-markdown"),
        pytest.param("  muchos     espacios\t\ty tabs  ", "muchos espacios y tabs", id="espacios"),
        pytest.param("---", "", id="regla-horizontal"),
    ],
)
def test_se_retira_solo_el_formato(original, esperado):
    assert texto_legible(original) == esperado


@pytest.mark.parametrize(
    ("original", "esperado"),
    [
        pytest.param("| **Emisiones** | GRI 305-1 |", "Emisiones | GRI 305-1", id="fila"),
        pytest.param("|---|:--:|", "", id="delimitador"),
        pytest.param("| a |  | c |", "a | | c", id="celda-vacia-interior-mantiene-columnas"),
        pytest.param("| a | b \\| c |", "a | b | c", id="pipe-escapado"),
        pytest.param("<table><tr><td>Uno</td><td>Dos</td></tr></table>", "Uno | Dos", id="tabla-html"),
        pytest.param("| uno<br>dos | tres |", "uno dos | tres", id="br-dentro-de-una-celda-no-desplaza-las-columnas"),
    ],
)
def test_las_celdas_de_una_tabla_siguen_separadas(original, esperado):
    assert texto_legible(original) == esperado


@pytest.mark.parametrize(
    "contenido",
    [
        pytest.param("Pérdida de 3*4 = 12, 5% de S/ 1,200.50 y US$ 3 < 5 > 2", id="numeros-monedas-y-comparadores"),
        pytest.param("snake_case_name, a * b y -5 °C", id="guion-bajo-asterisco-solitario-y-grados"),
        pytest.param("Emisión de CO₂ ≥ 10 ± 2 — «ok» ñandú ✓", id="unicode-y-simbolos"),
        pytest.param("GRI 305-1 (2016), 305-2; ISO 14001:2015; 12.5 %", id="codigos-y-porcentajes"),
        pytest.param("α + β ≠ γ; ∑ x² ≤ 9", id="signos-matematicos"),
    ],
)
def test_el_contenido_legitimo_no_se_altera(contenido):
    assert texto_legible(contenido) == contenido


def test_el_html_no_se_ejecuta_ni_se_cargan_recursos():
    resultado = texto_legible('<p>Texto <a href="http://externo.example/x">enlace</a></p><script>alert(1)</script>')
    assert resultado == "Texto enlace\n<script>alert(1)</script>"  # texto plano: la etiqueta desconocida queda como texto
    assert "http" not in texto_legible("[a](http://externo.example) ![b](http://externo.example/i.png)")


def test_las_entidades_conocidas_se_decodifican_y_las_dudosas_no():
    assert texto_legible("AT&amp;T&nbsp;ok &#8364; &lt;b&gt; &copy &#0; &#xD800;") == "AT&T ok € <b> &copy &#0; &#xD800;"


def test_los_marcadores_internos_no_pueden_venir_del_contenido():
    assert texto_legible("a\x00b\x01c") == "a b c"


def test_el_camino_rapido_equivale_al_camino_completo():
    """Un texto sin marcas no pasa por el análisis de formato; el resultado debe ser el mismo que daría el camino completo."""
    sin_marcas = "  La empresa redujo   emisiones en 2025.  "
    con_marca_inocua = sin_marcas + "\n"  # fuerza el camino completo (hay un salto de línea)
    assert texto_legible(sin_marcas) == texto_legible(con_marca_inocua) == "La empresa redujo emisiones en 2025."


def test_es_idempotente_sobre_texto_ya_legible():
    for original in ("### **Emisiones**<br>2025", "| a | **b** |", "[x](y) y `z`"):
        legible = texto_legible(original)
        assert texto_legible(legible) == legible


def test_la_seccion_legible_limpia_cada_titulo_y_los_deja_en_una_linea():
    assert seccion_legible("**Informe** de gestión<br>2025 > ### Emisiones > `GRI` 305") == (
        "Informe de gestión 2025 > Emisiones > GRI 305"
    )
    assert seccion_legible("") == ""
    assert seccion_legible("Informe >  > Emisiones") == "Informe > Emisiones"  # un título vacío no deja separadores sueltos


# ================================================ Límites de cita ================================================

TEXTO_LARGO = "Gestión ambiental certificada " * 40  # una sola línea de ~1200 caracteres


def _palabras_completas(texto: str, inicio: int, fin: int) -> bool:
    antes = inicio == 0 or texto[inicio - 1].isspace()
    despues = fin == len(texto) or texto[fin].isspace()
    return antes and despues


def test_la_ventana_no_corta_palabras_y_conserva_la_referencia():
    texto = TEXTO_LARGO + "GRI 305-1" + " con datos verificados" + TEXTO_LARGO
    inicio = texto.index("GRI 305-1")
    desde, hasta = recortar_cita(texto, 0, len(texto), inicio, inicio + 9, 400, 150)
    assert desde <= inicio
    assert hasta >= inicio + 9
    assert hasta - desde <= 400  # ventana acotada
    assert _palabras_completas(texto, desde, hasta)
    assert "GRI 305-1" in texto[desde:hasta]


def test_el_limite_de_la_linea_no_se_modifica():
    texto = "Corta GRI 305-1 línea"
    assert recortar_cita(texto, 0, len(texto), 6, 15, 400, 150) == (0, len(texto))


def test_una_palabra_larguisima_junto_a_la_referencia_no_hace_que_la_cita_cruce_la_referencia():
    texto = "x" * 500 + " GRI 305-1 " + "y" * 500
    inicio = texto.index("GRI")
    desde, hasta = recortar_cita(texto, 0, len(texto), inicio, inicio + 9, 400, 150)
    assert desde <= inicio
    assert hasta >= inicio + 9


def test_la_ventana_no_parte_un_token_de_formato_y_la_cita_sigue_siendo_legible():
    texto = TEXTO_LARGO + "GRI 305-1 con **datos verificados** por terceros. " + TEXTO_LARGO
    inicio = texto.index("GRI 305-1")
    for margen in range(20, 70):
        desde, hasta = recortar_cita(texto, 0, len(texto), inicio, inicio + 9, 60, margen)
        cita = texto[desde:hasta]
        assert _palabras_completas(texto, desde, hasta), (margen, cita)  # ningún «*» suelto en un extremo
        assert "*" not in texto_legible(cita), (margen, cita)  # aunque la ventana abra o cierre el énfasis, la lectura queda limpia


@pytest.mark.parametrize("resto", [" con **datos verificados** y mucho más texto.", " según el informe anual."])
def test_las_citas_detectadas_son_fragmentos_exactos_y_no_empiezan_ni_terminan_en_mitad_de_palabra(resto):
    texto = "# Informe\n\n" + TEXTO_LARGO + "Reportamos GRI 305-1" + resto + " " + TEXTO_LARGO + "\n"
    analisis = analizar_texto(texto).a_dict()
    menciones = [m for g in analisis["gri"] for m in g["menciones"]]
    assert menciones
    for m in menciones:
        assert texto[m["cita_inicio"]:m["cita_fin"]] == m["cita"]  # las posiciones coinciden con el original
        assert m["cita_inicio"] <= m["inicio"]
        assert m["fin"] <= m["cita_fin"]
        assert _palabras_completas(texto, m["cita_inicio"], m["cita_fin"]), m["cita"]
        assert len(m["cita"]) <= 400


def test_la_cita_de_una_sancion_tampoco_se_corta_a_mitad_de_palabra():
    texto = "# Cumplimiento\n\n" + TEXTO_LARGO + "El OEFA impuso una multa de S/ 5000 en 2023. " + TEXTO_LARGO + "\n"
    analisis = analizar_texto(texto).a_dict()
    assert len(analisis["sanciones"]) == 1
    sancion = analisis["sanciones"][0]
    assert texto[sancion["cita_inicio"]:sancion["cita_fin"]] == sancion["cita"]
    assert _palabras_completas(texto, sancion["cita_inicio"], sancion["cita_fin"]), sancion["cita"]
    assert "multa" in sancion["cita"]

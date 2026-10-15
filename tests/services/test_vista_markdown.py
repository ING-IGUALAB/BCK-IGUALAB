"""Vista auxiliar de lectura del Markdown (`vista_markdown`).

Nivel: unidad, sin BD ni red. La vista existe para que los detectores léxicos no pierdan relaciones entre palabras por el
formato en línea, SIN tocar el original: estas pruebas fijan que (1) solo se retira formato, (2) el mapa de posiciones es
exacto y monótono y (3) lo que no es formato (código, listas, tablas, cifras) se conserva.
"""
import gc
import random
import time

import pytest

from app.services.ingesta.vista_markdown import construir_vista

NL = "\n"
BARRA = "\\"


@pytest.mark.parametrize(
    ("original", "plano"),
    [
        ("una **sanción** grave", "una sanción grave"),
        ("una *sanción* grave", "una sanción grave"),
        ("una _sanción_ grave", "una sanción grave"),
        ("una ***sanción*** grave", "una sanción grave"),
        ("una __sanción__ grave", "una sanción grave"),
        ("una ~~sanción~~ grave", "una sanción grave"),
        ("**Sanción:** grave", "Sanción: grave"),
        ('**"sanción"** grave', '"sanción" grave'),
        ("un [monto](https://x.pe/a_b) grave", "un monto grave"),
        ("un [monto][ref] grave", "un monto grave"),
        ("sanción[^1] grave", "sanción grave"),
        ("sanción<br>grave", "sanción grave"),
        ("sanción<br/>grave", "sanción grave"),
        ("sanción <b>grave</b>", "sanción grave"),
        ("de&nbsp;S/ 500", "de S/ 500"),
        ("S/ **500,000**", "S/ 500,000"),
        ("**S/ 500,000** soles", "S/ 500,000 soles"),
    ],
)
def test_retira_solo_la_marca_de_formato(original, plano):
    vista = construir_vista(original)
    assert vista.plano == plano
    assert vista.original is original


def test_quita_el_prefijo_de_cita_y_el_salto_duro_sin_unir_palabras():
    original = "> una **sanción**" + NL + "> grave" + BARRA + NL + "del OEFA"
    assert construir_vista(original).plano == "una sanción" + NL + "grave" + NL + "del OEFA"


def test_sin_marcas_la_vista_es_el_propio_texto_sin_mapa():
    texto = "Una multa de S/ 5,000 impuesta por el OEFA." + NL + "Otra línea."
    vista = construir_vista(texto)
    assert vista.plano is texto
    assert vista.es_identidad
    assert vista.inicio_en_original(5) == 5
    assert vista.fin_en_original(9) == 9


@pytest.mark.parametrize(
    "texto",
    [
        "```" + NL + "**no se toca** _ni esto_ [a](b)" + NL + "```",
        "~~~" + NL + "**no se toca**" + NL + "~~~",
        "usa `**literal**` y `_x_` en código",
        "``dos ` backticks **x**`` aparte",
        "un `[enlace](https://x.pe)` y `sanción<br>x` literales",
    ],
)
def test_no_toca_el_codigo_cercado_ni_el_codigo_en_linea(texto):
    assert construir_vista(texto).plano == texto


@pytest.mark.parametrize(
    "texto",
    [
        "* elemento de lista",
        "- elemento de lista",
        "1. elemento numerado",
        "a * b y 3*4 y 5 * 6",
        "un_nombre_con_guiones y snake_case",
        "https://x.pe/a_b_c?d=1",
        "ruta C:\\datos\\mi_archivo",
        "| A | B |" + NL + "|---|:--:|" + NL + "| 1 | 2 |",
        "# Título de nivel uno",
        "***",
        "Texto con asterisco suelto * y guion bajo suelto _ aquí",
        "caracter escapado \\*literal\\* ahí",
    ],
)
def test_conserva_lo_que_no_es_formato(texto):
    assert construir_vista(texto).plano == texto


def test_conserva_la_estructura_de_tabla_y_celdas():
    original = "| **Tipo** | *Entidad* | **Monto (S/)** |" + NL + "|---|---|---|" + NL + "| **Sanción** | *OEFA* | **32,000** |"
    assert construir_vista(original).plano == (
        "| Tipo | Entidad | Monto (S/) |" + NL + "|---|---|---|" + NL + "| Sanción | OEFA | 32,000 |"
    )


def test_un_tramo_de_la_vista_equivale_a_un_tramo_contiguo_del_original():
    original = "recibió una **sanción** *económica* del OEFA"
    vista = construir_vista(original)
    inicio = vista.plano.index("sanción")
    # Tramo exacto: sin las marcas de los extremos.
    a, b = vista.inicio_en_original(inicio), vista.fin_en_original(inicio + len("sanción"))
    assert original[a:b] == "sanción"
    # Un tramo que contiene marcas internas las conserva: es un trozo contiguo del original.
    fin = vista.plano.index("económica") + len("económica")
    a, b = vista.inicio_en_original(inicio), vista.fin_en_original(fin)
    assert original[a:b] == "sanción** *económica"
    # Con marcas adyacentes (para citas): el formato queda equilibrado.
    a, b = vista.inicio_con_marcas(inicio), vista.fin_con_marcas(fin)
    assert original[a:b] == "**sanción** *económica*"


def test_una_cita_no_parte_un_caracter_sustituido():
    original = "uno<br>**sanción** dos"
    vista = construir_vista(original)
    inicio = vista.plano.index("sanción")
    assert original[vista.inicio_con_marcas(inicio):vista.fin_con_marcas(inicio + len("sanción"))] == "**sanción**"


def test_los_extremos_del_texto_se_mapean():
    original = "**a**"
    vista = construir_vista(original)
    assert vista.plano == "a"
    assert (vista.inicio_en_original(0), vista.fin_en_original(1)) == (2, 3)
    assert vista.fin_en_original(0) == 0
    assert (vista.inicio_con_marcas(0), vista.fin_con_marcas(1)) == (0, 5)
    assert vista.inicio_en_original(len(vista.plano)) == len(original)


FRAGMENTOS = [
    "**", "*", "_", "__", "~~", "`", "``", "[", "](http://x.pe/a_b)", "](", "]", "<br>", "&nbsp;", "<b>", "</b>",
    "[^1]", NL, NL + NL, " ", "  ", "\t", "palabra", "sanción", "S/ 5,000", "32,000", "|", "|---|", "> ", "- ", "1. ", "# ",
    BARRA, BARRA + NL, "3*4", "snake_case", "```", "~~~", "multa", "ñ", "€",
]


@pytest.mark.parametrize("semilla", range(40))
def test_el_mapa_de_posiciones_es_exacto_y_monotono_con_entradas_aleatorias(semilla):
    """Propiedad: cada carácter de la vista corresponde a UN carácter del original (igual, o un espacio sustituto), en el
    mismo orden, y cualquier tramo se vuelve a leer como un trozo contiguo del original."""
    azar = random.Random(semilla)
    original = "".join(azar.choice(FRAGMENTOS) for _ in range(azar.randint(1, 60)))
    vista = construir_vista(original)
    previo = -1
    for i, caracter in enumerate(vista.plano):
        posicion = vista.inicio_en_original(i)
        assert posicion > previo
        assert posicion < len(original)
        if caracter != " ":  # lo único que se sustituye es un espacio
            assert original[posicion] == caracter
        previo = posicion
    assert len(vista.plano) <= len(original)
    if vista.plano:
        assert vista.inicio_con_marcas(0) == 0
        assert vista.fin_con_marcas(len(vista.plano)) == len(original)
    for _ in range(5):
        if len(vista.plano) < 2:
            break
        i = azar.randrange(len(vista.plano))
        j = azar.randrange(i + 1, len(vista.plano) + 1)
        a, b = vista.inicio_en_original(i), vista.fin_en_original(j)
        assert a < b <= len(original)
        assert vista.inicio_con_marcas(i) <= a
        assert vista.fin_con_marcas(j) >= b
        if vista.plano[i] != " ":
            assert original[a] == vista.plano[i]


def test_el_original_no_se_modifica():
    texto = "una **sanción** del OEFA"
    vista = construir_vista(texto)
    assert vista.original == "una **sanción** del OEFA"
    assert vista.plano == "una sanción del OEFA"


@pytest.mark.parametrize(
    ("texto", "plano"),
    [
        # Una racha abre un fragmento que cierra la siguiente racha de EXACTAMENTE la misma longitud.
        ("`**a**` **b**", "`**a**` b"),
        ("``a ` **x** `` **b**", "``a ` **x** `` b"),
        ("```x``` **b**", "```x``` b"),
        # Sin una racha igual que la cierre, los acentos graves son texto y el formato sí se retira.
        ("`a **b**", "`a b"),
        ("``a` **b**", "``a` b"),
        ("`a ``b`` **c**", "`a ``b`` c"),
        ("` `` ``` **b**", "` `` ``` b"),
        # Dos fragmentos seguidos y uno que contiene a otro de distinta longitud.
        ("`**a**` y `**b**` **c**", "`**a**` y `**b**` c"),
        ("`` `**a**` `` **c**", "`` `**a**` `` c"),
    ],
)
def test_los_fragmentos_de_codigo_en_linea_cierran_con_una_racha_de_la_misma_longitud(texto, plano):
    assert construir_vista(texto).plano == plano


def _linea_con_rachas_sin_cierre(k: int) -> str:
    """Rachas de acentos graves de longitud 1..k, todas distintas: ninguna tiene una igual que la cierre."""
    return "**x** " + " ".join("`" * n for n in range(1, k + 1))


def _tiempo_por_llamada(texto: str, minimo_s: float = 0.15, intentos: int = 5) -> float:
    """Mejor de varios intentos del tiempo medio por llamada. Se repite hasta acumular `minimo_s` para que un coste de pocos
    milisegundos no quede dominado por el ruido del reloj."""
    mejor = float("inf")
    for _ in range(intentos):
        gc.collect()
        llamadas = 0
        inicio = time.perf_counter()
        while True:
            construir_vista(texto)
            llamadas += 1
            transcurrido = time.perf_counter() - inicio
            if transcurrido >= minimo_s:
                break
        mejor = min(mejor, transcurrido / llamadas)
    return mejor


def test_muchas_rachas_de_acentos_graves_sin_cierre_crecen_linealmente():
    """Con el regex anterior (`.*?` y retrovinculación) cuadruplicar el tamaño multiplicaba el tiempo por más de 50: una línea de
    13 KB tardaba 3 s y una de 320 KB no terminaba (una línea hostil bloqueaba la ingesta). Se compara el tiempo con N y 4·N
    caracteres, así que no depende de la velocidad de la máquina ni de la cobertura: lineal ≈ 4, cuadrático ≈ 16."""
    pequeno, grande = _linea_con_rachas_sin_cierre(200), _linea_con_rachas_sin_cierre(400)
    assert len(grande) / len(pequeno) == pytest.approx(4, rel=0.1)
    razon = _tiempo_por_llamada(grande) / _tiempo_por_llamada(pequeno)
    assert razon < 8, f"El tiempo creció ×{razon:.1f} al cuadruplicar el tamaño (lineal ≈ 4, cuadrático ≈ 16)."


def test_las_rachas_sin_cierre_no_impiden_retirar_el_formato():
    assert construir_vista(_linea_con_rachas_sin_cierre(50)).plano.startswith("x ")

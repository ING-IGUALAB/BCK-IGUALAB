"""Reconocimiento mínimo de Markdown compartido por validación y fragmentación.

Cubre el criterio único de línea, cerca de código, encabezado ATX y tabla pipes.
Nivel: unidad.
"""
import time

import pytest

from app.services.ingesta import markdown


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("", []),
        ("a", ["a"]),
        ("a\n", ["a"]),
        ("a\n\n", ["a", ""]),
        ("a\r\nb\rc\nd", ["a", "b", "c", "d"]),
        # Estos NO son saltos de línea en Markdown (sí lo son para str.splitlines()).
        ("a\u2028b\u2029c\x85d\x0ce", ["a\u2028b\u2029c\x85d\x0ce"]),
    ],
)
def test_dividir_lineas_solo_separa_en_lf_crlf_y_cr(texto, esperado):
    assert markdown.dividir_lineas(texto) == esperado


@pytest.mark.parametrize(
    ("texto", "inicio", "esperado"),
    [
        ("ab\ncd", 0, (2, 3)),
        ("ab\r\ncd", 0, (2, 4)),
        ("ab\rcd", 0, (2, 3)),
        ("ab\ncd", 3, (5, 5)),  # última línea sin terminador
        ("ab\n\ncd", 3, (3, 4)),  # línea vacía
    ],
)
def test_limites_de_linea(texto, inicio, esperado):
    assert markdown.limites_de_linea(texto, inicio) == esperado


def test_es_blanca_usa_el_rango_sin_copiar():
    texto = "x   \ny"
    assert markdown.es_blanca(texto, 1, 4) is True
    assert markdown.es_blanca(texto, 0, 4) is False
    assert markdown.es_blanca("\x0c\t ") is True


@pytest.mark.parametrize(
    ("linea", "marca"),
    [
        ("```", "```"),
        ("````python", "````"),
        ("~~~ info con ` acento", "~~~"),
        ("   ```md", "```"),
    ],
)
def test_apertura_de_cerca(linea, marca):
    assert markdown.marca_apertura_cerca(linea) == marca


@pytest.mark.parametrize(
    "linea",
    ["``", "```x``` en línea", "    ```", "texto ```", "~~", "-```"],
)
def test_lineas_que_no_abren_cerca(linea):
    assert markdown.marca_apertura_cerca(linea) is None


@pytest.mark.parametrize(
    ("linea", "marca", "cierra"),
    [
        ("```", "```", True),
        ("`````  ", "```", True),
        ("~~~", "~~~", True),
        ("``", "```", False),            # más corta
        ("~~~", "```", False),           # otro carácter
        ("```python", "```", False),     # con información
        ("    ```", "```", False),       # sangría de código
        ("``` ```", "```", False),
    ],
)
def test_cierre_de_cerca(linea, marca, cierra):
    assert markdown.cierra_cerca(linea, marca) is cierra


def test_actualizar_cerca_sigue_el_estado_de_un_bloque():
    estado, dentro = markdown.actualizar_cerca("texto", None)
    assert (estado, dentro) == (None, False)
    estado, dentro = markdown.actualizar_cerca("```py", estado)
    assert (estado, dentro) == ("```", True)
    estado, dentro = markdown.actualizar_cerca("```otra", estado)
    assert (estado, dentro) == ("```", True)
    estado, dentro = markdown.actualizar_cerca("```", estado)
    assert (estado, dentro) == (None, True)
    assert markdown.actualizar_cerca("texto", estado) == (None, False)


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [
        ("# Uno", (1, "Uno")),
        ("###### Seis ######", (6, "Seis")),
        ("##", (2, "")),
        ("## ###", (2, "")),
        ("#\tTab", (1, "Tab")),
        ("  # Sangría", (1, "Sangría")),
        ("# con # medio", (1, "con # medio")),
        ("# cierra#", (1, "cierra#")),
        ("#sin espacio", None),
        ("####### siete", None),
        ("    # código", None),
        ("texto", None),
        ("# a ## b ##", (1, "a ## b")),
        ("# a   ##   ", (1, "a")),
        ("#   ", (1, "")),
        ("#\t", (1, "")),
        ("##  ##", (2, "")),
        ("# ## x", (1, "## x")),
        ("# a\nb", None),
    ],
)
def test_titulo_atx(linea, esperado):
    assert markdown.titulo_atx(linea) == esperado


def test_titulo_atx_no_es_cuadratico_con_muchos_blancos_internos():
    """Antes el patrón con cuantificador perezoso tardaba ~12 s con 40 000 espacios."""
    linea = "# a" + " " * 200_000 + "b"
    inicio = time.perf_counter()
    assert markdown.titulo_atx(linea) == (1, "a" + " " * 200_000 + "b")
    assert markdown.inicia_otro_bloque(linea) is True
    assert time.perf_counter() - inicio < 2


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [("# t", True), ("```", True), ("~~~x", True), ("> cita", True), ("  > cita", True),
     ("texto | pipe", False), ("| a | b |", False), ("    > código", False)],
)
def test_inicia_otro_bloque(linea, esperado):
    assert markdown.inicia_otro_bloque(linea) is esperado


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [
        ("| a | b |", ["a", "b"]),
        ("a | b", ["a", "b"]),
        ("| a \\| b | c |", ["a \\| b", "c"]),
        ("| a | b \\|", ["a", "b \\|"]),
        ("|---|:--:|", ["---", ":--:"]),
        ("| | |", ["", ""]),
    ],
)
def test_celdas(linea, esperado):
    assert markdown.celdas(linea) == esperado


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [
        ("| a |", True),
        ("a | b", True),
        ("a \\| b", False),         # pipe escapado
        ("sin pipes", False),
        ("    | a |", False),         # sangría de código
        ("\t| a |", False),          # tabulación = 4 columnas
        ("   | a |", True),
        ("# t | x | y", False),       # encabezado
        ("> a | b", False),           # cita
        ("```a|b", False),            # cerca
    ],
)
def test_puede_ser_fila(linea, esperado):
    assert markdown.puede_ser_fila(linea) is esperado


@pytest.mark.parametrize(
    ("linea", "esperado"),
    [
        ("|---|---|", True),
        ("---|---", True),
        ("| :-- | --: | :-: |", True),
        ("---", False),          # sin pipe: línea horizontal o setext, no delimitador
        ("|--|x|", False),
        ("| a | b |", False),
        ("|::|", False),
    ],
)
def test_es_delimitador(linea, esperado):
    assert markdown.es_delimitador(linea) is esperado


@pytest.mark.parametrize(
    ("linea", "siguiente", "esperado"),
    [
        ("| A | B |", "|---|---|", True),
        ("A | B", "---|---", True),
        ("Prosa con | pipe", "Otra línea", False),
        ("Prosa con | pipe", "---", False),
        ("|---|---|", "|---|---|", False),
        ("# T | x", "---|---", False),
        ("Sin pipes", "|---|", False),
    ],
)
def test_es_inicio_tabla(linea, siguiente, esperado):
    assert markdown.es_inicio_tabla(linea, siguiente) is esperado


def test_tiene_pipe_respeta_el_rango():
    texto = "sin\n|\n"
    assert markdown.tiene_pipe(texto) is True
    assert markdown.tiene_pipe(texto, 0, 3) is False


@pytest.mark.parametrize(
    "linea",
    ["| a | b |", "a | b", "|a|b|", "  | a |  b  |  ", "| a \| b | c |", "|", "||", "| |", "a", "", "| a | b \|",
     "\t| x |\t", "| ñ | é |", "| a | | c |"],
)
def test_rangos_de_celdas_coinciden_con_celdas(linea):
    prefijo = "xx\n"
    texto = prefijo + linea + "\nyy"
    inicio, fin = len(prefijo), len(prefijo) + len(linea)
    rangos = markdown.rangos_de_celdas(texto, inicio, fin)
    assert [texto[a:b] for a, b in rangos] == markdown.celdas(linea)
    assert all(inicio <= a <= b <= fin for a, b in rangos)

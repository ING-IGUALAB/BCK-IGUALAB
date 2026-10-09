"""Etapa 2 de ingesta: fragmentación trazable de Markdown.

T08 (tablas no pierden correspondencia) y T15 (texto, índice y ubicación
rastreables, sin inventar páginas); LTX:RF-016 (solo la parte de fragmentación),
RNF-015, RNF-025, RN-018/019. Nivel: unidad, sin BD, proveedor ni HTTP.

No prueban embeddings, indexación ni rendimiento con 50 MB.
"""
import hashlib
import io
import random

import pytest
from starlette.datastructures import UploadFile

from app.services.ingesta import validacion
from app.services.ingesta.fragmentacion import (
    MAX_CARACTERES_CONTEXTO_PREDETERMINADO,
    MAX_CARACTERES_PREDETERMINADO,
    Fragmento,
    ParametrosFragmentacion,
    fragmentar,
    iterar_fragmentos,
)

BOM = b"\xef\xbb\xbf"


def params(maximo: int, contexto: int = MAX_CARACTERES_CONTEXTO_PREDETERMINADO):
    return ParametrosFragmentacion(max_caracteres=maximo, max_caracteres_contexto=contexto)


def verificar_contrato(texto: str, fragmentos: list[Fragmento], parametros: ParametrosFragmentacion):
    """Propiedades que debe cumplir CUALQUIER resultado, sin reproducir el algoritmo."""
    assert [f.indice for f in fragmentos] == list(range(len(fragmentos)))
    if not texto:
        assert fragmentos == []
        return
    # Rangos válidos, ordenados, sin huecos ni solapamientos.
    assert fragmentos[0].inicio == 0
    assert fragmentos[-1].fin == len(texto)
    for anterior, actual in zip(fragmentos, fragmentos[1:]):
        assert actual.inicio == anterior.fin
    for f in fragmentos:
        assert 0 <= f.inicio < f.fin <= len(texto)
        # Evidencia literal exacta de su rango.
        assert f.texto_literal == texto[f.inicio:f.fin]
        # Contrato de límites.
        assert len(f.texto_literal) <= parametros.max_caracteres
        assert len(f.contexto) <= parametros.max_caracteres_contexto
        assert len(f.texto_embedding) <= (
            parametros.max_caracteres + parametros.max_caracteres_contexto
        )
        # El contexto va aparte de la evidencia.
        assert f.texto_embedding == f.contexto + f.texto_literal
        assert f.contexto == "" or f.contexto.endswith("\n\n")
        # Un corte nunca separa CRLF.
        assert not (f.texto_literal.endswith("\r") and texto[f.fin:f.fin + 1] == "\n")
    # Reconstrucción exacta del texto interpretado.
    assert "".join(f.texto_literal for f in fragmentos) == texto
    # La primera pieza nunca es continuación.
    assert fragmentos[0].continuacion is False


def comprobar(texto: str, parametros: ParametrosFragmentacion | None = None) -> list[Fragmento]:
    parametros = parametros or ParametrosFragmentacion()
    fragmentos = fragmentar(texto, parametros)
    verificar_contrato(texto, fragmentos, parametros)
    assert fragmentar(texto, parametros) == fragmentos  # determinista
    return fragmentos


def lector(datos: bytes):
    return UploadFile(file=io.BytesIO(datos), filename="memoria.md").read


# --- Texto breve, largo y sin encabezados ------------------------------------

def test_texto_breve_es_un_solo_fragmento_con_el_rango_completo():
    texto = "Una sola línea.\n"
    [f] = comprobar(texto)
    assert (f.indice, f.inicio, f.fin) == (0, 0, len(texto))
    assert f.texto_literal == texto
    assert f.ruta_encabezados == () and f.seccion == ""
    assert f.contexto == "" and f.continuacion is False


def test_documento_sin_encabezados_se_divide_por_parrafos_sin_ruta_ni_contexto():
    parrafos = [f"Párrafo {i} " + "palabra " * 10 for i in range(6)]
    texto = "\n\n".join(parrafos) + "\n"
    fragmentos = comprobar(texto, params(200))
    assert len(fragmentos) > 1
    assert all(f.ruta_encabezados == () and f.contexto == "" for f in fragmentos)
    # Cada corte cae en un límite de párrafo: no se parte ninguna palabra.
    assert all(f.texto_literal.endswith("\n\n") for f in fragmentos[:-1])
    assert all(not f.continuacion for f in fragmentos)


def test_la_cantidad_de_fragmentos_depende_del_contenido_no_es_fija():
    def cantidad(parrafos: int) -> int:
        texto = "\n\n".join("Contenido de prueba número %d." % i * 5 for i in range(parrafos))
        return len(comprobar(texto, params(300)))

    cantidades = [cantidad(n) for n in (1, 10, 50, 100)]
    assert cantidades == sorted(cantidades) and len(set(cantidades)) == 4
    assert cantidades[0] == 1 and cantidades[-1] != 100


def test_texto_largo_respeta_el_limite_predeterminado_provisional():
    assert MAX_CARACTERES_PREDETERMINADO == 2000
    texto = ("Frase de relleno número uno. " * 40 + "\n\n") * 30
    fragmentos = comprobar(texto)
    assert len(fragmentos) > 1
    assert max(len(f.texto_literal) for f in fragmentos) <= 2000


# --- Secciones y subsecciones --------------------------------------------------

def test_secciones_y_subsecciones_con_rangos_ruta_y_contexto_esperados():
    texto = (
        "# Memoria 2025\n\nIntroducción.\n\n"
        "## Ambiente\n\nTexto A.\n\n"
        "### Agua\n\nTexto B.\n\n"
        "## Social\n\nTexto C.\n"
    )
    fragmentos = comprobar(texto)
    esperado = [
        ("# Memoria 2025\n\nIntroducción.\n\n", ("Memoria 2025",), ""),
        ("## Ambiente\n\nTexto A.\n\n", ("Memoria 2025", "Ambiente"), "Sección: Memoria 2025\n\n"),
        ("### Agua\n\nTexto B.\n\n", ("Memoria 2025", "Ambiente", "Agua"),
         "Sección: Memoria 2025 > Ambiente\n\n"),
        ("## Social\n\nTexto C.\n", ("Memoria 2025", "Social"), "Sección: Memoria 2025\n\n"),
    ]
    assert [(f.texto_literal, f.ruta_encabezados, f.contexto) for f in fragmentos] == esperado
    inicio = 0
    for f, (literal, _, _) in zip(fragmentos, esperado):
        assert (f.inicio, f.fin) == (inicio, inicio + len(literal))
        inicio = f.fin
    assert fragmentos[2].seccion == "Memoria 2025 > Ambiente > Agua"
    assert fragmentos[2].texto_embedding == (
        "Sección: Memoria 2025 > Ambiente\n\n### Agua\n\nTexto B.\n\n"
    )


def test_encabezados_sin_cuerpo_se_acumulan_hasta_el_primer_contenido():
    texto = "# A\n## B\n\n### C\nTexto de C.\n"
    [f] = comprobar(texto)
    assert f.ruta_encabezados == ("A", "B", "C")
    assert f.contexto == ""


def test_un_encabezado_de_nivel_menor_cierra_la_ruta_de_los_niveles_inferiores():
    texto = "# A\n\na\n\n## B\n\nb\n\n### C\n\nc\n\n## D\n\nd\n\n# E\n\ne\n"
    rutas = [f.ruta_encabezados for f in comprobar(texto)]
    assert rutas == [("A",), ("A", "B"), ("A", "B", "C"), ("A", "D"), ("E",)]


@pytest.mark.parametrize(
    ("linea", "titulo"),
    [
        ("# Título", "Título"),
        ("## Con cierre ##", "Con cierre"),
        ("   ### Con sangría", "Con sangría"),
        ("###### Nivel seis", "Nivel seis"),
        ("#\tCon tab", "Con tab"),
    ],
)
def test_encabezados_atx_reconocidos(linea, titulo):
    fragmentos = comprobar(f"{linea}\n\ncontenido\n")
    assert fragmentos[0].ruta_encabezados == (titulo,)


@pytest.mark.parametrize(
    "linea",
    ["#sin espacio", "####### siete", "    # sangría de código", "Título\n=====", "texto # no título"],
)
def test_lineas_que_no_son_encabezados_no_crean_ruta(linea):
    fragmentos = comprobar(f"{linea}\n\ncontenido\n")
    assert all(f.ruta_encabezados == () for f in fragmentos)


def test_encabezado_vacio_cuenta_en_la_jerarquia_pero_no_en_la_ruta():
    fragmentos = comprobar("# A\n\n##\n\ntexto\n")
    assert fragmentos[0].ruta_encabezados == ("A",)


# --- Párrafos mayores que el límite -------------------------------------------

def test_parrafo_mayor_que_el_limite_se_divide_en_espacios_sin_perder_texto():
    texto = "aaaa bbbb cccc dddd eeee"
    fragmentos = comprobar(texto, params(10))
    assert [f.texto_literal for f in fragmentos] == ["aaaa bbbb ", "cccc dddd ", "eeee"]
    assert [(f.inicio, f.fin) for f in fragmentos] == [(0, 10), (10, 20), (20, 24)]
    assert [f.continuacion for f in fragmentos] == [False, True, True]


def test_parrafo_largo_prefiere_cortar_en_salto_de_linea_y_luego_en_fin_de_oracion():
    con_lineas = "primera linea\nsegunda linea\ntercera linea"
    assert [f.texto_literal for f in comprobar(con_lineas, params(30))] == [
        "primera linea\nsegunda linea\n",
        "tercera linea",
    ]
    con_oraciones = "Uno dos tres. Cuatro cinco seis siete"
    assert [f.texto_literal for f in comprobar(con_oraciones, params(26))] == [
        "Uno dos tres. ",
        "Cuatro cinco seis siete",
    ]


def test_texto_largo_sin_espacios_se_corta_de_forma_dura_y_exacta():
    texto = "x" * 25
    fragmentos = comprobar(texto, params(10))
    assert [f.texto_literal for f in fragmentos] == ["x" * 10, "x" * 10, "x" * 5]
    assert [f.continuacion for f in fragmentos] == [False, True, True]


def test_la_continuacion_repite_el_contexto_no_el_texto():
    texto = "# Sección larga\n\n" + "palabra " * 20
    fragmentos = comprobar(texto, params(60))
    assert len(fragmentos) > 2
    assert all(f.contexto in ("", "Sección: Sección larga\n\n") for f in fragmentos)
    assert fragmentos[1].continuacion is True
    assert fragmentos[1].contexto == "Sección: Sección larga\n\n"
    assert fragmentos[1].texto_literal not in fragmentos[0].texto_literal


def test_corte_duro_no_separa_marca_combinante_de_su_letra_base():
    texto = "abcdefghi" + "é" + "fgh"  # «é» descompuesta cruzaría el límite de 10
    fragmentos = comprobar(texto, params(10))
    assert [f.texto_literal for f in fragmentos] == ["abcdefghi", "éfgh"]


def test_corte_duro_no_separa_una_secuencia_con_zwj():
    familia = "👨\N{ZERO WIDTH JOINER}👩"  # el límite de 10 caía entre 👨, ZWJ y 👩
    fragmentos = comprobar("a" * 8 + familia + "bbbbb", params(10))
    assert [f.texto_literal for f in fragmentos] == ["a" * 8, familia + "bbbbb"]


def test_corte_duro_no_separa_un_crlf():
    texto = "a" * 9 + "\r\n" + "b" * 5
    fragmentos = comprobar(texto, params(10))
    assert [f.texto_literal for f in fragmentos] == ["a" * 9, "\r\n" + "b" * 5]


def test_corte_con_secuencia_larga_de_marcas_combinantes_no_se_queda_sin_avanzar():
    texto = "a" + "́" * 40
    fragmentos = comprobar(texto, params(10))
    assert len(fragmentos) >= 1


# --- Tablas ---------------------------------------------------------------------

ENCABEZADO = "| Indicador | Valor |\n|---|---|\n"
FILAS = ["| Agua | 10 |\n", "| Energía | 20 |\n", "| Residuos | 30 |\n"]


def test_tabla_pequena_se_mantiene_junta_con_su_texto_previo_y_posterior():
    texto = "Antes de la tabla.\n\n" + ENCABEZADO + "".join(FILAS) + "\nDespués de la tabla.\n"
    [f] = comprobar(texto)
    assert f.texto_literal == texto
    assert f.contexto == ""


def test_tabla_que_cabe_pero_no_en_el_espacio_restante_empieza_fragmento_nuevo():
    tabla = ENCABEZADO + "".join(FILAS)  # 81 caracteres
    texto = "x" * 50 + "\n\n" + tabla
    fragmentos = comprobar(texto, params(100))
    assert [f.texto_literal for f in fragmentos] == ["x" * 50 + "\n\n", tabla]
    assert fragmentos[1].contexto == ""  # empieza en el encabezado: no hace falta repetirlo
    assert all(not f.continuacion for f in fragmentos)


def test_tabla_grande_se_divide_por_filas_y_repite_el_encabezado_solo_como_contexto():
    tabla = ENCABEZADO + "".join(FILAS)
    fragmentos = comprobar(tabla, params(40))
    assert [f.texto_literal for f in fragmentos] == [
        ENCABEZADO,
        FILAS[0] + FILAS[1],
        FILAS[2],
    ]
    contexto = "Encabezado de tabla: | Indicador | Valor |\n\n"
    assert [f.contexto for f in fragmentos] == ["", contexto, contexto]
    assert all(not f.continuacion for f in fragmentos)
    # La evidencia no contiene el encabezado repetido; el embedding sí.
    assert "Indicador" not in fragmentos[2].texto_literal
    assert fragmentos[2].texto_embedding == contexto + FILAS[2]
    # No se repite la tabla completa, solo una línea de contexto.
    assert "Agua" not in fragmentos[2].contexto


TABLA_DETERIORADA = (
    "| Indicador | Valor | Unidad |\n"
    "|---|---|\n"
    "| Agua | 10 |\n"
    "| Energía | 20 | GWh | extra |\n"
    "| Residuos |\n"
)


async def test_tabla_inconsistente_se_fragmenta_con_literales_exactos_y_sin_contexto_de_encabezado():
    # La decisión 2026-10-07 deja pasar la tabla; la fragmentación no inventa
    # correspondencias columna-celda ni completa celdas.
    texto = "# Datos\n\n" + TABLA_DETERIORADA
    original = texto.encode("utf-8")
    documento = await validacion.validar_archivo("memoria.md", lector(original))
    assert documento.diagnostico_tablas.total_inconsistencias == 4
    assert documento.contenido == original
    assert documento.sha256 == hashlib.sha256(original).hexdigest()

    fragmentos = comprobar(documento.texto, params(50))
    assert len(fragmentos) > 2  # se dividió de verdad
    assert "".join(f.texto_literal for f in fragmentos) == texto
    # Ninguna fila se alteró ni se reordenó: cada línea original sigue íntegra.
    for fila in TABLA_DETERIORADA.splitlines():
        assert any(fila in f.texto_literal for f in fragmentos)
    assert all("Encabezado de tabla" not in f.contexto for f in fragmentos)
    # Los offsets del diagnóstico coinciden con los de los fragmentos.
    for detalle in documento.diagnostico_tablas.detalles:
        dueno = next(f for f in fragmentos if f.inicio <= detalle.inicio < f.fin)
        assert dueno.texto_literal[detalle.inicio - dueno.inicio] == "|"
        assert dueno.texto_literal[detalle.inicio - dueno.inicio - 1:][:1] in ("\n", "|")


def test_tabla_inconsistente_pequena_se_mantiene_junta_y_literal():
    texto = TABLA_DETERIORADA
    [f] = comprobar(texto)
    assert f.texto_literal == texto and f.contexto == ""


def test_tabla_bien_formada_sigue_repitiendo_el_encabezado_como_contexto():
    tabla = "| A | B |\n|---|---|\n" + "".join(f"| {i} | {i} |\n" for i in range(10))
    fragmentos = comprobar(tabla, params(40))
    assert all(f.contexto == "Encabezado de tabla: | A | B |\n\n" for f in fragmentos[1:])


def test_una_fila_inconsistente_basta_para_omitir_el_contexto_de_encabezado():
    tabla = "| A | B |\n|---|---|\n" + "| 1 | 2 |\n" * 6 + "| 3 |\n" + "| 1 | 2 |\n" * 6
    fragmentos = comprobar(tabla, params(40))
    assert len(fragmentos) > 2
    assert all("Encabezado de tabla" not in f.contexto for f in fragmentos)


def test_encabezado_y_delimitador_de_tabla_no_se_separan():
    tabla = ENCABEZADO + "".join(FILAS)
    for maximo in range(len(ENCABEZADO), len(tabla)):
        fragmentos = comprobar(tabla, params(maximo))
        assert fragmentos[0].texto_literal.startswith(ENCABEZADO), maximo


def test_fila_mayor_que_el_limite_se_divide_sin_perdida_y_conserva_el_contexto():
    fila = "| " + "x" * 50 + " | y |\n"
    texto = "| A | B |\n|---|---|\n" + fila
    fragmentos = comprobar(texto, params(30))
    assert [f.texto_literal for f in fragmentos] == [
        "| A | B |\n|---|---|\n",
        fila[:30],
        fila[30:],
    ]
    assert [f.continuacion for f in fragmentos] == [False, False, True]
    assert fragmentos[1].contexto == fragmentos[2].contexto == "Encabezado de tabla: | A | B |\n\n"


def test_encabezado_de_tabla_mayor_que_el_limite_se_divide_sin_perdida():
    texto = "| " + "c" * 60 + " | B |\n|---|---|\n| 1 | 2 |\n"
    fragmentos = comprobar(texto, params(25))
    assert len(fragmentos) > 3
    assert any(f.continuacion for f in fragmentos)


def test_contexto_de_tabla_se_acota_y_se_marca_con_puntos_suspensivos():
    encabezado = "| " + " | ".join(f"Columna{i}" for i in range(30)) + " |\n"
    delimitador = "|" + "---|" * 30 + "\n"
    fila = "| " + " | ".join(str(i) for i in range(30)) + " |\n"  # 30 columnas: tabla bien formada
    texto = encabezado + delimitador + fila * 10
    assert len(encabezado + delimitador) < 600 < len(texto)
    fragmentos = comprobar(texto, params(600, 60))
    assert len(fragmentos) > 1
    contextos = {f.contexto for f in fragmentos[1:]}
    assert len(contextos) == 1
    [contexto] = contextos
    assert len(contexto) == 60 and contexto.endswith("…\n\n")
    assert contexto.startswith("Encabezado de tabla: | Columna0 |")


def test_tabla_sin_filas_de_cuerpo_y_tabla_al_final_sin_salto_de_linea():
    assert comprobar("| A | B |\n|---|---|")[0].texto_literal == "| A | B |\n|---|---|"
    texto = "| A | B |\n|---|---|\n\n\n" + "p " * 40
    comprobar(texto, params(30))


def test_prosa_con_pipes_no_es_tabla_y_no_genera_contexto_de_tabla():
    texto = "Opción a | opción b | opción c\nOtra línea | con pipes | sueltas\nTercera | línea\n"
    fragmentos = comprobar(texto, params(40))
    assert len(fragmentos) > 1
    assert all("Encabezado de tabla" not in f.contexto for f in fragmentos)
    assert [f.texto_literal for f in fragmentos][0].endswith("\n")


def test_pipes_escapados_no_crean_tabla_pero_las_filas_de_una_tabla_real_los_admiten():
    solo_escapados = "a \\| b\n\\|---\\|\nc \\| d\n"
    assert all("Encabezado de tabla" not in f.contexto for f in comprobar(solo_escapados, params(10)))

    tabla = "| Nombre | Detalle |\n|---|---|\n| Energía \\| total | 3,5 \\| GWh |\n| otra | fila |\n"
    fragmentos = comprobar(tabla, params(50))
    assert len(fragmentos) > 1
    assert fragmentos[-1].contexto == "Encabezado de tabla: | Nombre | Detalle |\n\n"


def test_encabezado_pegado_a_la_tabla_no_se_trata_como_fila():
    texto = "| A | B |\n|---|---|\n| 1 | 2 |\n## Sección | con | pipes\n\ncontenido\n"
    fragmentos = comprobar(texto, params(40))
    assert fragmentos[-1].ruta_encabezados == ("Sección | con | pipes",)


# --- Bloques de código cercados --------------------------------------------------

def test_encabezados_y_tablas_dentro_de_un_bloque_cercado_no_son_estructura():
    texto = (
        "# Real\n\n"
        "```md\n# No es título\n| a | b |\n|---|---|\n| 1 | 2 |\n## Tampoco\n```\n\n"
        "~~~~\n# Otro falso\n~~~\n# sigue dentro\n~~~~\n\n"
        "Texto final.\n"
    )
    for maximo in (30, 60, 2000):
        fragmentos = comprobar(texto, params(maximo))
        assert all(f.ruta_encabezados == ("Real",) for f in fragmentos), maximo
        assert all("Encabezado de tabla" not in f.contexto for f in fragmentos), maximo


def test_cerca_sin_cerrar_cubre_hasta_el_final_y_un_titulo_posterior_no_cuenta():
    fragmentos = comprobar("# Real\n\n```\ncódigo\n# no es título\nmás código\n")
    assert [f.ruta_encabezados for f in fragmentos] == [("Real",)]


def test_cerca_con_informacion_no_cierra_y_acentos_en_linea_no_abren():
    texto = "# T\n\n```\n```python\n# dentro\n```\n\n```x``` en línea\n\n## Después\n\nfin\n"
    fragmentos = comprobar(texto, params(40))
    assert fragmentos[-1].ruta_encabezados == ("T", "Después")
    assert all("dentro" not in " ".join(f.ruta_encabezados) for f in fragmentos)


def test_bloque_de_codigo_mayor_que_el_limite_se_divide_por_lineas():
    codigo = "```python\n" + "".join(f"linea_{i:02d} = {i}\n" for i in range(20)) + "```\n"
    fragmentos = comprobar("# Código\n\n" + codigo, params(80))
    assert len(fragmentos) > 3
    assert all(f.ruta_encabezados == ("Código",) for f in fragmentos)
    # Con líneas de 14 caracteres hay saltos disponibles: ningún corte parte una línea.
    assert all(f.texto_literal.endswith("\n") for f in fragmentos)


# --- Caracteres: tildes, ñ, Unicode, CRLF y saltos finales ----------------------

def test_tildes_enie_y_unicode_se_conservan_exactamente_y_los_rangos_son_de_caracteres():
    texto = "# Año ñandú\n\nÁrbol, acción, pingüino, € y «comillas». 日本語 😀 emoji.\n"
    fragmentos = comprobar(texto, params(25))
    assert "".join(f.texto_literal for f in fragmentos) == texto
    # 😀 ocupa 1 posición (punto de código), no 2 unidades UTF-16 ni 4 bytes.
    posicion = texto.index("😀")
    [fragmento] = [f for f in fragmentos if f.inicio <= posicion < f.fin]
    assert fragmento.texto_literal[posicion - fragmento.inicio] == "😀"
    assert fragmentos[0].ruta_encabezados == ("Año ñandú",)


def test_crlf_se_conserva_sin_normalizar():
    texto = "# Título\r\n\r\nPrimera línea\r\nSegunda línea\r\n\r\n| A | B |\r\n|---|---|\r\n| 1 | 2 |\r\n"
    for maximo in (10, 25, 40, 2000):
        fragmentos = comprobar(texto, params(maximo))
        assert "".join(f.texto_literal for f in fragmentos) == texto
        assert fragmentos[0].ruta_encabezados == ("Título",)
    assert "\r\n" in comprobar(texto)[0].texto_literal


def test_cr_suelto_es_salto_de_linea_pero_se_conserva():
    texto = "# A\r\rtexto uno\rtexto dos\r"
    fragmentos = comprobar(texto, params(12))
    assert fragmentos[0].ruta_encabezados == ("A",)


@pytest.mark.parametrize(
    "texto",
    ["fin sin salto", "fin con un salto\n", "fin con varios\n\n\n\n", "\n\n\ninicio con blancos\n", "a\n \t \n"],
)
def test_saltos_iniciales_y_finales_se_conservan(texto):
    comprobar(texto)
    comprobar(texto, params(7))


def test_separadores_unicode_y_salto_de_pagina_no_son_saltos_de_linea():
    texto = "| A | B |\n|---|---|\n| uno\u2028dos | tres\x0c |\n\n# Título\x85con NEL\n"
    fragmentos = comprobar(texto, params(40))
    assert fragmentos[-1].ruta_encabezados == ("Título\x85con NEL",)


def test_texto_vacio_o_solo_blancos_no_pierde_contenido_ni_valida_admision():
    assert fragmentar("") == []
    [f] = comprobar("  \n\t\n")
    assert f.texto_literal == "  \n\t\n"


# --- Encabezados y secciones con límites pequeños ---------------------------------

def test_un_titulo_no_queda_huerfano_cuando_el_parrafo_siguiente_excede_el_limite():
    texto = "## Título\n\n" + "palabra " * 30
    fragmentos = comprobar(texto, params(60))
    assert fragmentos[0].texto_literal.startswith("## Título\n\npalabra")
    assert fragmentos[0].ruta_encabezados == ("Título",)


def test_encabezado_mayor_que_el_limite_se_divide_y_su_ruta_se_conserva():
    titulo = "T" * 5 + " " + "largo " * 20
    fragmentos = comprobar(f"# {titulo}\n\ntexto\n", params(30))
    assert len(fragmentos) > 3
    assert all(f.ruta_encabezados == (titulo.strip(),) for f in fragmentos)
    assert fragmentos[1].contexto.startswith("Sección: TTTTT largo")


def test_titulo_largo_cuyo_siguiente_encabezado_no_cabe_no_contamina_la_ruta():
    primero = "# " + "a" * 40 + "\n"
    texto = primero + "# " + "b" * 40 + "\n\ncuerpo\n"
    fragmentos = comprobar(texto, params(50))
    assert fragmentos[0].texto_literal == primero
    assert fragmentos[0].ruta_encabezados == ("a" * 40,)


# --- Contexto y límites ---------------------------------------------------------------

def test_contexto_se_acota_con_puntos_suspensivos_dentro_del_limite_declarado():
    titulo = "Sección " + "muy larga " * 20
    texto = f"# {titulo}\n\n## Hijo\n\n" + "palabra " * 30
    fragmentos = comprobar(texto, params(40, 30))
    largos = [f for f in fragmentos if f.contexto]
    assert largos
    assert all(len(f.contexto) <= 30 for f in largos)
    assert all(f.contexto.endswith("…\n\n") for f in largos)


@pytest.mark.parametrize("contexto", [0, 1, 2])
def test_sin_espacio_para_contexto_no_se_agrega_ninguno(contexto):
    texto = "# A\n\n## B\n\n" + "palabra " * 30
    fragmentos = comprobar(texto, params(30, contexto))
    assert all(f.contexto == "" for f in fragmentos)
    assert all(f.texto_embedding == f.texto_literal for f in fragmentos)


def test_contexto_minimo_posible_es_solo_los_puntos_suspensivos():
    fragmentos = comprobar("# Título\n\n## Sub\n\n" + "palabra " * 20, params(30, 3))
    assert {f.contexto for f in fragmentos if f.contexto} == {"…\n\n"}


def test_el_contexto_no_forma_parte_de_la_evidencia_ni_de_la_reconstruccion():
    texto = "# Informe\n\n" + ENCABEZADO + "".join(FILAS) * 3
    fragmentos = comprobar(texto, params(70))
    con_contexto = [f for f in fragmentos if f.contexto]
    assert con_contexto
    for f in con_contexto:
        assert f.texto_literal == texto[f.inicio:f.fin]
        assert f.contexto not in texto  # texto inventado por el fragmentador, no evidencia
    assert "".join(f.texto_literal for f in fragmentos) == texto


# --- Parámetros inválidos ---------------------------------------------------------------

@pytest.mark.parametrize("valor", [0, 1, -5])
def test_max_caracteres_inferior_al_minimo_se_rechaza(valor):
    with pytest.raises(ValueError, match="max_caracteres debe ser al menos 2"):
        ParametrosFragmentacion(max_caracteres=valor)


@pytest.mark.parametrize("valor", [True, False, 2.5, "10", None, 1e3])
def test_max_caracteres_no_entero_se_rechaza(valor):
    with pytest.raises(ValueError, match="max_caracteres debe ser un entero"):
        ParametrosFragmentacion(max_caracteres=valor)


@pytest.mark.parametrize("valor", [-1, -100])
def test_contexto_negativo_se_rechaza(valor):
    with pytest.raises(ValueError, match="max_caracteres_contexto debe ser al menos 0"):
        ParametrosFragmentacion(max_caracteres_contexto=valor)


@pytest.mark.parametrize("valor", [True, 1.5, "5", None])
def test_contexto_no_entero_se_rechaza(valor):
    with pytest.raises(ValueError, match="max_caracteres_contexto debe ser un entero"):
        ParametrosFragmentacion(max_caracteres_contexto=valor)


def test_tipos_incorrectos_de_entrada_se_rechazan_al_llamar_no_al_consumir():
    with pytest.raises(TypeError, match="debe ser str"):
        iterar_fragmentos(b"bytes")
    with pytest.raises(TypeError, match="debe ser str"):
        fragmentar(None)
    with pytest.raises(TypeError, match="ParametrosFragmentacion"):
        iterar_fragmentos("texto", {"max_caracteres": 10})


def test_los_parametros_son_inmutables_y_el_minimo_funciona():
    p = ParametrosFragmentacion(max_caracteres=2, max_caracteres_contexto=0)
    with pytest.raises(AttributeError):
        p.max_caracteres = 5
    comprobar("ab\r\ncd ef\n", p)


# --- Incrementalidad y determinismo -----------------------------------------------------

def test_iterar_fragmentos_produce_de_forma_incremental_y_coincide_con_fragmentar():
    texto = "# T\n\n" + "párrafo. " * 200
    iterador = iterar_fragmentos(texto, params(100))
    primero = next(iterador)
    assert primero.indice == 0 and primero.inicio == 0
    assert [primero, *iterador] == fragmentar(texto, params(100))


def test_el_texto_de_entrada_no_se_modifica_y_los_literales_son_cadenas_propias():
    texto = "# Título\r\n\r\ncontenido  con  espacios\r\n"
    copia = str(texto)
    comprobar(texto, params(15))
    assert texto == copia


# --- Integración con el resultado real de validación de la Etapa 1 ---------------------

async def test_integracion_con_el_documento_validado_de_la_etapa_1_con_bom():
    fuente = (
        "# Memoria 2025\r\n\r\n"
        "Ñandú, acción y pingüino.\r\n\r\n"
        "| Indicador | Valor |\r\n|---|---|\r\n| Agua | 10 |\r\n| Energía | 20 |\r\n"
    )
    original = BOM + fuente.encode("utf-8")
    documento = await validacion.validar_archivo("memoria.md", lector(original))
    assert documento.tiene_bom is True

    parametros = params(60)
    fragmentos = comprobar(documento.texto, parametros)

    # Los rangos se miden sobre el texto interpretado, que omite el BOM inicial.
    assert documento.texto == fuente
    assert fragmentos[0].inicio == 0 and fragmentos[-1].fin == len(fuente)
    assert "".join(f.texto_literal for f in fragmentos) == fuente
    assert "\ufeff" not in "".join(f.texto_literal for f in fragmentos)
    assert fragmentos[0].ruta_encabezados == ("Memoria 2025",)
    assert fragmentos[-1].contexto == (
        "Sección: Memoria 2025\nEncabezado de tabla: | Indicador | Valor |\n\n"
    )
    assert fragmentos[-1].texto_literal == "| Energía | 20 |\r\n"
    # La fragmentación no toca los originales: bytes y SHA-256 siguen siendo los de la carga.
    assert documento.contenido == original
    assert documento.sha256 == hashlib.sha256(original).hexdigest()
    # No son offsets de bytes: con caracteres multibyte difieren.
    assert len(fuente.encode("utf-8")) > len(fuente)


async def test_integracion_con_documento_sin_tablas_ni_encabezados_validado():
    texto = "Texto plano de una memoria sin estructura. " * 50
    documento = await validacion.validar_archivo("plano.md", lector(texto.encode("utf-8")))
    fragmentos = comprobar(documento.texto, params(300))
    assert len(fragmentos) > 1 and all(f.ruta_encabezados == () for f in fragmentos)


# --- Barrido determinista de documentos generados -----------------------------------------

PALABRAS = ["agua", "energía", "emisión", "niño", "señal", "GRI", "302-1", "ñandú", "acción", "tonelada"]


def generar_documento(semilla: int) -> str:
    azar = random.Random(semilla)
    eol = azar.choice(["\n", "\r\n"])
    bloques: list[str] = []
    total = azar.randint(3, 30)
    for posicion in range(total):
        tipo = azar.choice(["h", "p", "p", "t", "c", "l", "b"])
        if tipo == "h":
            bloques.append("#" * azar.randint(1, 4) + " Título " + azar.choice(PALABRAS))
        elif tipo == "p":
            palabras = [azar.choice(PALABRAS) for _ in range(azar.randint(1, 80))]
            if azar.random() < 0.15:
                palabras.append("x" * azar.randint(50, 400))  # palabra enorme sin espacios
            bloques.append(" ".join(palabras) + azar.choice(["", ".", " | pipe suelto"]))
        elif tipo == "t":
            columnas = azar.randint(1, 4)
            filas = [
                "| " + " | ".join(azar.choice(PALABRAS) * azar.randint(1, 12) for _ in range(columnas)) + " |"
                for _ in range(azar.randint(0, 25))
            ]
            cabecera = "| " + " | ".join(f"C{i}" for i in range(columnas)) + " |"
            delimitador = "|" + "---|" * columnas
            bloques.append(eol.join([cabecera, delimitador, *filas]))
        elif tipo == "c":
            lineas = [f"# código {azar.choice(PALABRAS)} | {i}" for i in range(azar.randint(0, 30))]
            # Solo el último bloque puede quedar sin cerrar; a mitad de documento la
            # apertura siguiente lo cerraría (comportamiento correcto de Markdown).
            sin_cerrar = posicion == total - 1 and azar.random() < 0.3
            cierre = [] if sin_cerrar else ["```"]
            bloques.append(eol.join(["```", *lineas, *cierre]))
        elif tipo == "l":
            bloques.append(eol.join(f"- {azar.choice(PALABRAS)}" for _ in range(azar.randint(1, 10))))
        else:
            bloques.append(" " * azar.randint(0, 3))
    separador = eol * azar.choice([1, 2, 3])
    return azar.choice(["", "\n"]) + separador.join(bloques) + azar.choice(["", eol, eol * 2])


@pytest.mark.parametrize("maximo", [2, 7, 40, 200, 2000])
@pytest.mark.parametrize("semilla", range(40))
def test_propiedades_del_contrato_en_documentos_generados(semilla, maximo):
    texto = generar_documento(semilla)
    fragmentos = comprobar(texto, params(maximo, 80))
    # Las rutas solo contienen títulos de encabezados reales, nunca texto de código.
    assert all("código" not in t for f in fragmentos for t in f.ruta_encabezados)


def test_documento_grande_mantiene_el_contrato_sin_garantizar_rendimiento():
    # Humo de escala (~1,5 MB). NO valida el consumo ni el tiempo a 50 MB (T27 pendiente).
    texto = "".join(generar_documento(s) + "\n\n" for s in range(400))
    assert len(texto) > 1_000_000
    comprobar(texto, params(2000))

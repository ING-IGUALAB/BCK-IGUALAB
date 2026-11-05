"""Citas agrupadas para la pantalla de Análisis GRI (texto idéntico mostrado una vez). Sin base de datos, red ni proveedores.

Los análisis salen de los detectores REALES de la ingesta. Se comprueba qué agrupa y qué no (documento, fila GRI, texto exacto),
que cada mención se cuenta una sola vez, que `pasajes.py` conserva sus reglas y que conteos, estados, huellas y puntaje ESG no
cambian por esta presentación.
"""
import copy
import uuid
from datetime import datetime, timezone

import pytest

from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.domain import calculo
from app.modules.analisis_gri.domain.calculo import EstadoAsignado
from app.modules.analisis_gri.domain.citas_agrupadas import agrupar_citas
from app.modules.analisis_gri.domain.pasajes import agrupar_pasajes
from app.modules.analisis_gri.schemas import ActorResumen, AparicionPasaje, PasajeEvidencia
from tests.services.test_analisis_gri_citas_pasajes import TRES_REFERENCIAS_EN_UNA_LINEA, doc_de, fila

ACTOR = ActorResumen(id=uuid.uuid4(), nombre="Admin")
FRASE = "Cumplimos GRI 204-1 con proveedores locales."
MEMORIA = "memoria.md"
SECCION_COMPRAS = "Informe > Compras"
INICIO = "# Informe\n\n"


def citas_de(texto: str, identidad: str = "204:2016", nombre: str = "informe.md"):
    d, _ = doc_de(texto, nombre)
    filas, _ = calculo.construir_filas([d])
    return fila(filas, identidad)


# ============================================ Qué se agrupa ============================================

def test_dos_citas_identicas_de_la_misma_fila_y_documento_son_un_grupo_con_dos_menciones():
    f = citas_de(INICIO + f"## Compras\n\n{FRASE}\n\nUn párrafo distinto.\n\n{FRASE}\n")
    assert f.total_menciones == 2
    assert f.total_citas_agrupadas == len(f.citas_agrupadas) == 1
    cita = f.citas_agrupadas[0]
    assert cita.total_menciones == 2
    assert cita.cita_legible == FRASE
    assert cita.documento_nombre == "informe.md"
    assert cita.referencias == ["GRI 204-1"]
    assert [(u.seccion_legible, u.total_menciones, u.indices_evidencia) for u in cita.ubicaciones] == [(SECCION_COMPRAS, 2, [0, 1])]
    assert len(f.pasajes) == 2  # los pasajes posicionales siguen siendo dos: la agrupación por igualdad es adicional


def test_la_cita_identica_en_varias_secciones_del_mismo_documento_conserva_todas_las_ubicaciones():
    f = citas_de(INICIO + f"## Compras\n\n{FRASE}\n\n## Contratos\n\n{FRASE}\n\n## Anexo\n\n{FRASE}\n")
    assert f.total_menciones == 3
    assert f.total_citas_agrupadas == 1
    cita = f.citas_agrupadas[0]
    assert cita.total_menciones == 3
    assert [(u.seccion_legible, u.total_menciones) for u in cita.ubicaciones] == [
        (SECCION_COMPRAS, 1), ("Informe > Contratos", 1), ("Informe > Anexo", 1)
    ]  # el texto no se atribuye solo a la primera sección
    assert cita.indices_evidencia == [0, 1, 2]
    assert cita.indices_pasaje == [0, 1, 2]


def test_el_mismo_texto_en_documentos_distintos_permanece_separado_aunque_se_llamen_igual():
    texto = INICIO + f"{FRASE}\n"
    primero, _ = doc_de(texto, MEMORIA)
    segundo, _ = doc_de(texto, MEMORIA)  # mismo nombre, distinto documento_id
    tercero, _ = doc_de(texto, "reporte.md")
    filas, _ = calculo.construir_filas([primero, segundo, tercero])
    f = fila(filas, "204:2016")
    assert f.total_menciones == 3
    assert f.total_citas_agrupadas == 3
    assert [(c.documento_id, c.documento_nombre, c.total_menciones) for c in f.citas_agrupadas] == [
        (primero.id, MEMORIA, 1), (segundo.id, MEMORIA, 1), (tercero.id, "reporte.md", 1)
    ]


def test_el_mismo_texto_en_identidades_gri_distintas_no_comparte_grupo():
    d, _ = doc_de(INICIO + "Cumplimos GRI 204-1 y GRI 302-1 en el año.\n")
    filas, _ = calculo.construir_filas([d])
    assert {f.identidad for f in filas} == {"204:2016", "302:2016"}
    for f in filas:
        assert f.total_citas_agrupadas == 1
        assert f.citas_agrupadas[0].total_menciones == 1
        assert f.citas_agrupadas[0].referencias == [f.evidencias[0].referencia_original]


def test_el_mismo_texto_en_ediciones_distintas_de_un_codigo_no_comparte_grupo():
    d, _ = doc_de(INICIO + "Agua según GRI 303-1 (2016) y GRI 303-1 (2018) en la planta.\n")
    filas, _ = calculo.construir_filas([d])
    identidades = sorted(f.identidad for f in filas)
    assert len(identidades) == 2
    assert len({f.pasajes[0].cita_legible for f in filas}) == 1  # el texto es el mismo en las dos filas
    for f in filas:
        assert f.total_citas_agrupadas == 1
        assert f.citas_agrupadas[0].total_menciones == f.total_menciones == 1


def test_textos_con_cifras_puntuacion_mayusculas_o_espacios_distintos_no_se_fusionan():
    variantes = [
        "Reducimos emisiones GRI 305-1 en 10%.",
        "Reducimos emisiones GRI 305-1 en 12%.",
        "Reducimos emisiones GRI 305-1 en 10 %.",
        "Reducimos emisiones GRI 305-1 en 10%",
        "reducimos emisiones GRI 305-1 en 10%.",
        "Reducimos emisiones GRI 305-1 en 2024.",
        "Reducimos emisiónes GRI 305-1 en 10%.",
    ]
    f = citas_de(INICIO + "\n\n".join(variantes) + "\n", "305:2016")
    assert f.total_menciones == len(variantes)
    assert f.total_citas_agrupadas == len(variantes)
    assert [c.cita_legible for c in f.citas_agrupadas] == variantes
    assert all(c.total_menciones == 1 for c in f.citas_agrupadas)


def test_textos_que_solo_difieren_en_la_limpieza_de_presentacion_si_coinciden():
    """La única limpieza es la de presentación existente: `**` y `<br>` se quitan antes de comparar, nada más."""
    f = citas_de(INICIO + "Cumplimos **GRI 204-1** con proveedores.\n\nCumplimos GRI 204-1 con proveedores.\n")
    assert f.total_citas_agrupadas == 1
    assert f.citas_agrupadas[0].total_menciones == 2


# ============================================ Contadores y trazabilidad ============================================

def test_varias_referencias_en_un_mismo_pasaje_cuentan_una_vez_cada_mencion_sin_multiplicaciones():
    d, _ = doc_de(TRES_REFERENCIAS_EN_UNA_LINEA)
    filas, _ = calculo.construir_filas([d])
    f = fila(filas, "305:2016")
    assert f.total_menciones == 3
    assert f.total_citas_agrupadas == 1
    cita = f.citas_agrupadas[0]
    assert cita.total_menciones == 3  # tres menciones, un solo pasaje: ni 1 ni 9
    assert cita.referencias == ["GRI 305-1", "GRI 305-2", "GRI 305-3"]
    assert cita.indices_evidencia == [0, 1, 2]
    assert [u.total_menciones for u in cita.ubicaciones] == [3]


def test_una_referencia_repetida_cuenta_todas_sus_menciones_pero_se_lista_una_vez():
    f = citas_de(INICIO + "GRI 305-1 se repite: GRI 305-1, GRI 305-1.\n", "305:2016")
    assert f.total_menciones == 3
    assert f.citas_agrupadas[0].total_menciones == 3
    assert f.citas_agrupadas[0].referencias == ["GRI 305-1"]


def test_los_pasajes_solapados_conservan_su_agrupacion_y_la_trazabilidad():
    d, _ = doc_de(INICIO + "Cumplimos GRI 204-1 en compras. Y también GRI 204-1 en contratos.\n")
    filas, _ = calculo.construir_filas([d])
    f = fila(filas, "204:2016")
    assert f.pasajes == agrupar_pasajes(f.evidencias)  # `pasajes.py` no se alteró ni se recalculó distinto
    assert len(f.pasajes) == 1  # las ventanas solapadas siguen siendo un pasaje
    assert f.pasajes[0].total_apariciones == 2
    cita = f.citas_agrupadas[0]
    assert cita.cita_legible == f.pasajes[0].cita_legible
    assert cita.total_menciones == 2
    assert cita.indices_pasaje == [0]
    assert cita.indices_evidencia == [a.indice_evidencia for a in f.pasajes[0].apariciones]


def test_una_cadena_larga_de_ventanas_se_corta_en_pasajes_y_ninguna_mencion_se_pierde_ni_se_cuenta_dos_veces():
    relleno = "Compras locales con trazabilidad completa de proveedores y contratos del periodo. "
    linea = "".join(f"{relleno * 3}GRI 204-1 aplicado. " for _ in range(14))
    f = citas_de(INICIO + linea + "\n")
    assert len(f.pasajes) > 1
    assert sum(c.total_menciones for c in f.citas_agrupadas) == f.total_menciones == 14
    assert sorted(i for c in f.citas_agrupadas for i in c.indices_evidencia) == list(range(14))
    assert sorted(i for c in f.citas_agrupadas for i in c.indices_pasaje) == list(range(len(f.pasajes)))  # cada pasaje, una vez


def test_el_orden_es_el_de_primera_aparicion_y_es_estable():
    texto = (
        INICIO + "Cumplimos GRI 204-1 con A.\n\nCumplimos GRI 204-1 con B.\n\nCumplimos GRI 204-1 con A.\n\n"
        "Cumplimos GRI 204-1 con C.\n\nCumplimos GRI 204-1 con B.\n"
    )
    d, _ = doc_de(texto)
    primero = calculo.construir_filas([d])[0]
    segundo = calculo.construir_filas([d])[0]
    f = fila(primero, "204:2016")
    assert [(c.cita_legible[-2:-1], c.total_menciones) for c in f.citas_agrupadas] == [("A", 2), ("B", 2), ("C", 1)]
    assert [f.model_dump() for f in primero] == [f.model_dump() for f in segundo]  # mismo dato, misma respuesta


def test_los_documentos_conservan_su_orden_de_primera_aparicion():
    texto = INICIO + f"{FRASE}\n\nOtra: GRI 204-1 distinta.\n"
    a, _ = doc_de(texto, "a.md")
    b, _ = doc_de(INICIO + f"{FRASE}\n", "b.md")
    f = fila(calculo.construir_filas([a, b])[0], "204:2016")
    assert [(c.documento_nombre, c.cita_legible) for c in f.citas_agrupadas] == [
        ("a.md", FRASE), ("a.md", "Otra: GRI 204-1 distinta."), ("b.md", FRASE)
    ]


def test_sin_seccion_la_ubicacion_es_cadena_vacia_de_forma_consistente():
    f = citas_de(f"{FRASE}\n\n{FRASE}\n")
    assert [(u.seccion_legible, u.total_menciones) for u in f.citas_agrupadas[0].ubicaciones] == [("", 2)]


def test_una_cita_con_una_sola_mencion():
    f = citas_de(INICIO + f"## Compras\n\n{FRASE}\n")
    cita = f.citas_agrupadas[0]
    assert (f.total_menciones, f.total_citas_agrupadas, cita.total_menciones) == (1, 1, 1)
    assert [u.seccion_legible for u in cita.ubicaciones] == [SECCION_COMPRAS]


def test_las_citas_unicas_se_calculan_de_los_datos_y_no_son_las_menciones():
    texto = INICIO + "".join(f"Cumplimos GRI 204-1 con proveedor {i % 4}.\n\n" for i in range(10))
    f = citas_de(texto)
    assert f.total_menciones == 10
    assert f.total_citas_agrupadas == 4
    assert sum(c.total_menciones for c in f.citas_agrupadas) == 10


# ============================================ Casos límite de la función pura ============================================

def pasaje(indices, *, doc, texto="x", seccion="S", referencia="GRI 204-1", nombre="d.md") -> PasajeEvidencia:
    return PasajeEvidencia(
        documento_id=doc, documento_nombre=nombre, seccion_legible=seccion, rol="cuerpo", cita_legible=texto, cita_inicio=0, cita_fin=1,
        total_apariciones=len(indices),
        apariciones=[AparicionPasaje(indice_evidencia=i, referencia_original=referencia, inicio=i, fin=i + 1) for i in indices],
    )


def test_un_indice_que_ya_se_conto_no_se_suma_otra_vez_ni_crea_un_grupo_vacio():
    doc = uuid.uuid4()
    citas = agrupar_citas([pasaje([0, 1], doc=doc), pasaje([1], doc=doc), pasaje([1, 2], doc=doc, texto="otro")])
    assert [(c.cita_legible, c.indices_evidencia) for c in citas] == [("x", [0, 1]), ("otro", [2])]
    assert [c.indices_pasaje for c in citas] == [[0], [2]]  # el segundo pasaje no aporta nada nuevo y no figura


def test_sin_pasajes_no_hay_citas_agrupadas():
    assert agrupar_citas([]) == []


def test_la_clave_incluye_el_documento_y_el_texto_exacto():
    uno, dos = uuid.uuid4(), uuid.uuid4()
    citas = agrupar_citas([pasaje([0], doc=uno), pasaje([1], doc=dos), pasaje([2], doc=uno, texto="X"), pasaje([3], doc=uno)])
    assert [(c.documento_id, c.cita_legible, c.total_menciones) for c in citas] == [(uno, "x", 2), (dos, "x", 1), (uno, "X", 1)]


# ============================================ Lo que no cambia ============================================

def test_conteos_evidencias_estados_y_puntaje_no_cambian_por_las_citas_agrupadas():
    d, _ = doc_de(
        TRES_REFERENCIAS_EN_UNA_LINEA
        + f"\n{FRASE}\n\n## Otra\n\n{FRASE}\n\nAdemás GRI 302-1 y GRI 302-1.\n"
    )
    huella_antes = calculo.huella_de_documentos([d])
    original_antes = copy.deepcopy((d.id, d.sha256, d.analisis))  # copia independiente de lo que la huella y el análisis usan
    con, _ = calculo.construir_filas([d])
    sin, _ = calculo.construir_filas([d], presentacion=False)
    assert [(f.identidad, f.total_menciones, f.menciones_en_indice, f.menciones_en_cuerpo, f.documentos) for f in con] == [
        (f.identidad, f.total_menciones, f.menciones_en_indice, f.menciones_en_cuerpo, f.documentos) for f in sin
    ]
    legibles = {"cita_legible", "seccion_legible"}
    assert [[e.model_dump(exclude=legibles) for e in f.evidencias] for f in con] == [
        [e.model_dump(exclude=legibles) for e in f.evidencias] for f in sin
    ]
    assert all(f.citas_agrupadas == [] and f.total_citas_agrupadas == 0 for f in sin)  # las escrituras no pagan la presentación

    def puntaje(filas):
        asignados = {f.identidad: EstadoAsignado(f.identidad, EstadoGri.OK, 1, ACTOR, datetime(2026, 10, 2, tzinfo=timezone.utc)) for f in filas}
        return calculo.calcular_puntaje(calculo.aplicar_estados(filas, asignados), vigente=False, hay_documentos=True)

    assert puntaje(con) == puntaje(sin)
    assert (d.id, d.sha256, d.analisis) == original_antes  # construir filas con presentación y agrupación no toca el original
    assert calculo.huella_de_documentos([d]) == huella_antes  # y la huella de vigencia no cambió


@pytest.mark.parametrize(
    "texto",
    [
        TRES_REFERENCIAS_EN_UNA_LINEA,
        f"# I\n\n## A\n\n{FRASE}\n\n## B\n\n{FRASE}\n\n{FRASE}\n",
        "# I\n\nCumplimos GRI 204-1 en compras. Y también GRI 204-1 en contratos. GRI 204-1 otra vez.\n",
        f"{FRASE}\n\n{FRASE}\n",
    ],
)
def test_invariantes_de_toda_fila(texto):
    d, _ = doc_de(texto)
    for f in calculo.construir_filas([d])[0]:
        n = f.total_menciones
        assert n == len(f.evidencias)
        assert f.total_citas_agrupadas == len(f.citas_agrupadas) <= n
        assert sum(c.total_menciones for c in f.citas_agrupadas) == n
        assert sorted(i for c in f.citas_agrupadas for i in c.indices_evidencia) == list(range(n))  # partición de las menciones
        for c in f.citas_agrupadas:
            assert c.total_menciones == len(c.indices_evidencia) == sum(u.total_menciones for u in c.ubicaciones)
            assert sorted(i for u in c.ubicaciones for i in u.indices_evidencia) == sorted(c.indices_evidencia)
            assert all(f.evidencias[i].documento_id == c.documento_id for i in c.indices_evidencia)

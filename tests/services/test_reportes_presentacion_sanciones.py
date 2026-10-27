"""Presentación de las sanciones en el reporte de prospección (textos visibles aprobados el 2026-10-26). Sin BD, red ni proveedores.

Los análisis salen de los detectores REALES de la ingesta sobre textos sintéticos; el «antiguo» se obtiene quitando de uno nuevo lo
que el detector anterior no producía (ver `antiguo` en el módulo de agrupación). Lo que se prueba es el COMPORTAMIENTO del reporte
(conteos, motivos, textos), no nombres, montos ni archivos concretos.
"""
import copy
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.modules.analisis_gri.domain.calculo import DocumentoBase
from app.modules.reportes.domain import plantilla
from app.modules.reportes.domain.plantilla import MOTIVOS_TOTAL, datos_desde_analisis, renderizar
from tests.services.test_sanciones_agrupacion_analisis_reporte import (
    MISMA_EN_PARRAFO_Y_TABLA,
    POSIBLE_DUPLICADA,
    TRES_MENCIONES_DE_UNA_AUTORIDAD,
    _FakeDB,
    _parchear,
    analisis_real,
    antiguo,
    doc,
)

NOTA_REFERENCIAS = (
    "Una misma multa puede mencionarse varias veces. Este número cuenta las referencias encontradas, "
    "no necesariamente multas distintas."
)
NOTA_SIN_MONTO = "Que no se detecte un monto en una referencia no significa que no aparezca en otra parte del documento."
NOTA_FORMATO = "El formato del número puede tener más de una interpretación. Por eso no se suma al total."

# Un monto con separador ambiguo y otra referencia a la multa sin monto.
MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO = (
    "# Cumplimiento\n\nLa empresa recibió una multa de S/ 5,000 por el OEFA. La multa fue pagada en el mismo año.\n"
)
DOS_MULTAS_UNA_SIN_MONTO = (
    "# Informe\n\nEl OEFA impuso una multa de S/ 1000 en 2023.\n\nLa SUNAFIL impuso una multa a la empresa.\n"
)
AGRUPACION_CON_MONTO_EN_UNA_CITA = (
    "# Informe\n\nEl OEFA impuso una multa de S/ 3000 por la Resolución N° 31-2023-OEFA/DFAI, infracción N° 4.\n\n"
    "La Resolución N° 31-2023-OEFA/DFAI, infracción N° 4, es una multa impuesta por el OEFA; no se especifica el monto.\n"
)
DOS_MONEDAS = "# Informe\n\nEl OEFA impuso una multa de S/ 800.\n\nLa SUNAT impuso una multa de US$ 450 por otros hechos.\n"
UNA_SOLA = "# Informe\n\nLa SUNAFIL impuso una multa de S/ 2000 en 2024.\n"
SIN_SANCIONES = "# Informe\n\nTexto sin sanciones.\n"
CITA_CON_LA_PALABRA_IMPORTE = "# Informe\n\nEl OEFA impuso una multa por un importe de S/ 2000 en 2024.\n"


def _html(datos: dict) -> str:
    return renderizar(datos)


def _texto_visible(html: str) -> str:
    """Texto que ve quien lee el PDF: sin etiquetas, con espacios normalizados."""
    cuerpo = re.sub(r"<(?:style|title)\b.*?</(?:style|title)>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cuerpo))


def _datos(texto: str, *, viejo: bool = False, sin_autoridad: bool = False) -> dict:
    d = doc(texto)
    if viejo:
        d = antiguo(d)
    if sin_autoridad:  # el detector anterior no identificaba la autoridad en «multa de S/ 5,000 por el OEFA»
        analisis = copy.deepcopy(d.analisis)
        for s in analisis["sanciones"]:
            s["entidad"] = None
        d = DocumentoBase(**{**d.__dict__, "analisis": analisis})
    return datos_desde_analisis(analisis_real([d]), "2026-10-26")


def _veces(html: str, frase: str) -> int:
    return _texto_visible(html).count(frase)


# ============================== monto pendiente de revisión y otra referencia sin monto ==============================

def test_un_monto_pendiente_de_revision_y_una_referencia_sin_monto_se_explican_sin_contradicciones():
    datos = _datos(MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO, viejo=True, sin_autoridad=True)
    assert datos["descripcion_sanciones"] == (
        "Se encontraron 2 referencias a sanciones: 1 con monto detectado y 1 sin monto detectado. "
        "El monto encontrado necesita revisión, por lo que el total no está disponible."
    )
    assert (datos["referencias_total"], datos["referencias_con_monto"], datos["referencias_sin_monto"]) == (2, 1, 1)
    assert datos["referencias_pendientes"] == 1  # forma parte de las que tienen monto, no suma aparte
    assert datos["money"] == []
    assert datos["money_disponible"] is False
    assert datos["money_motivos_codigos"] == ["REVISION", "ANALISIS_ANTIGUO"]
    assert datos["money_motivos"] == [
        "Hay montos que necesitan revisión.",
        "El análisis guardado no incluye la información necesaria para calcular el total.",
    ]

    html = _html(datos)
    visible = _texto_visible(html)
    for frase in (
        "Referencias a sanciones", NOTA_REFERENCIAS, "Referencias sin monto detectado", "Monto de la multa no detectado",
        "Monto pendiente de revisión", NOTA_FORMATO, "Monto total de las multas", "Total no disponible",
        "Referencias con monto detectado: 1.", "Referencias sin monto detectado: 1.",
        "Referencias con monto pendiente de revisión: 1",
        "Autoridad que aplicó la sanción: no identificada",
    ):
        assert frase in visible, frase
    assert "S/ 5,000" in html  # el monto sale tal como está en el documento
    assert "S/ 5000" not in html
    assert "5000" not in visible.replace("5,000", "")
    assert _veces(html, NOTA_SIN_MONTO) == 1  # una sola aclaración general junto al listado
    assert _veces(html, "Monto de la multa no detectado") == 1  # solo la referencia que no trae monto
    assert "---" not in html
    assert "Total sanciones" not in visible
    assert "Sanciones cuantificadas" not in visible
    assert "(0)" not in visible
    assert "importe" not in visible.lower()


def test_la_referencia_con_monto_pendiente_no_se_clasifica_como_sin_monto():
    datos = _datos(MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO, viejo=True)
    con_monto, sin_monto = datos["sanciones"]
    assert con_monto["monto"] == "S/ 5,000"
    assert con_monto["monto_etiqueta"] == "Monto pendiente de revisión"
    assert sin_monto["monto"] == ""
    assert sin_monto["monto_etiqueta"] == "Monto de la multa no detectado"


def test_con_la_autoridad_identificada_se_dice_cual_es():
    datos = _datos(MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO)  # análisis nuevo: el detector actual reconoce al OEFA
    primera, segunda = datos["sanciones"]
    assert primera["autoridad_texto"] == "Autoridad que aplicó la sanción: OEFA"
    assert segunda["autoridad_texto"] == "Autoridad que aplicó la sanción: no identificada"


# ============================== dos multas distintas: una con monto fiable y otra sin monto ==============================

def test_dos_multas_distintas_una_con_monto_y_otra_sin_monto_no_presentan_un_total_parcial():
    datos = _datos(DOS_MULTAS_UNA_SIN_MONTO)
    assert datos["descripcion_sanciones"] == (
        "Se encontraron 2 referencias a sanciones: 1 con monto detectado y 1 sin monto detectado. El total no está disponible."
    )
    assert datos["referencias_pendientes"] == 0
    assert datos["money"] == []
    assert datos["money_disponible"] is False
    assert datos["money_motivos_codigos"] == ["SIN_MONTO"]
    visible = _texto_visible(_html(datos))
    assert "No se detectó el monto de algunas multas." in visible
    assert "S/ 1000" in visible  # el monto fiable se conserva, pero no se presenta como total
    assert "Referencias con monto pendiente de revisión" not in visible
    # Solo se afirma lo que los datos respaldan.
    for ausente in (
        "Hay montos que necesitan revisión.", "Algunas referencias podrían corresponder a la misma multa.",
        "No se pudo comprobar si todas las referencias corresponden a multas distintas.",
        "El análisis guardado no incluye la información necesaria",
    ):
        assert ausente not in visible, ausente


# ============================== agrupación válida con monto en una cita y sin monto en otra ==============================

def test_una_agrupacion_con_monto_en_una_cita_no_atribuye_un_monto_desconocido_a_la_multa():
    datos = _datos(AGRUPACION_CON_MONTO_EN_UNA_CITA)
    (tarjeta,) = datos["sanciones"]
    assert tarjeta["tipo"] == "agrupada"
    assert len(tarjeta["citas"]) == 2  # se conservan todas las citas
    assert tarjeta["monto"] == "S/ 3000"
    assert tarjeta["monto_etiqueta"] == ""
    # La referencia sin monto se cuenta como tal, pero la multa SÍ tiene monto: el total es calculable.
    assert (datos["referencias_total"], datos["referencias_con_monto"], datos["referencias_sin_monto"]) == (2, 1, 1)
    assert datos["money_disponible"] is True
    assert datos["money"] == [{"unidad": "S/", "total": "3,000"}]
    assert datos["money_motivos"] == []
    assert datos["descripcion_sanciones"] == (
        "Se encontraron 2 referencias a sanciones: 1 con monto detectado y 1 sin monto detectado. "
        "2 de ellas se reúnen en 1 multa agrupada porque el documento las identifica como referencias a una misma multa."
    )
    visible = _texto_visible(_html(datos))
    assert "S/ 3,000" in visible
    assert "Total no disponible" not in visible
    assert "No se detectó el monto de algunas multas." not in visible
    assert "Monto de la multa no detectado" not in visible  # la multa agrupada no tiene el monto «desconocido»
    assert visible.count(NOTA_SIN_MONTO) == 1  # pero la referencia sin monto sí lleva la aclaración general


# ============================== montos fiables ==============================

def test_con_montos_fiables_el_total_se_muestra_por_moneda_por_separado():
    datos = _datos(DOS_MONEDAS)
    assert datos["money_disponible"] is True
    assert datos["money_motivos_codigos"] == []
    assert datos["money"] == [{"unidad": "S/", "total": "800"}, {"unidad": "US$", "total": "450"}]
    assert datos["descripcion_sanciones"] == "Se encontraron 2 referencias a sanciones: 2 con monto detectado."
    html = _html(datos)
    visible = _texto_visible(html)
    assert "Monto total de las multas" in visible
    assert "Total no disponible" not in visible
    assert re.search(r'<div class="cvalue">S/ 800</div>\s*<div class="cvalue">US\$ 450</div>', html)  # una línea por moneda
    assert NOTA_SIN_MONTO not in visible  # no hay referencias sin monto
    assert "Referencias sin monto detectado" in visible
    assert datos["sanciones_sin_monto"] == 0


def test_la_descripcion_usa_singular_y_plural_segun_los_datos():
    una = _datos(UNA_SOLA)["descripcion_sanciones"]
    assert una == "Se encontró 1 referencia a una sanción: con monto detectado."
    varias = _datos(DOS_MONEDAS)["descripcion_sanciones"]
    assert varias.startswith("Se encontraron 2 referencias a sanciones")
    sin_monto = _datos("# I\n\nLa SUNAFIL impuso una multa a la empresa.\n")["descripcion_sanciones"]
    assert sin_monto == "Se encontró 1 referencia a una sanción: sin monto detectado. El total no está disponible."


@pytest.mark.parametrize(
    ("pendientes", "con", "esperado"),
    [
        (1, 1, "El monto encontrado necesita revisión"),
        (2, 2, "Los montos encontrados necesitan revisión"),
        (1, 3, "1 de los 3 montos encontrados necesita revisión"),
        (2, 3, "2 de los 3 montos encontrados necesitan revisión"),
    ],
)
def test_la_frase_de_montos_pendientes_concuerda_con_los_conteos(pendientes, con, esperado):
    assert plantilla._frase_de_pendientes(con, pendientes) == esperado


# ============================== ausencia de sanciones ==============================

def test_sin_sanciones_no_se_confunde_con_un_total_imposible_de_calcular():
    datos = _datos(SIN_SANCIONES)
    assert datos["sin_sanciones"] is True
    assert datos["money_disponible"] is False
    assert datos["money"] == []
    assert datos["money_motivos"] == []
    assert datos["descripcion_sanciones"] == "No se detectaron sanciones económicas."
    assert (datos["referencias_total"], datos["referencias_sin_monto"], datos["casos_a_revisar"]) == (0, 0, 0)
    html = _html(datos)
    visible = _texto_visible(html)
    assert "Monto total de las multas No se detectaron sanciones económicas" in visible
    assert "Total no disponible" not in visible
    assert "---" not in html
    assert NOTA_REFERENCIAS not in visible
    assert NOTA_SIN_MONTO not in visible


# ============================== análisis antiguo, sin agrupación ==============================

def test_un_analisis_antiguo_con_montos_fiables_se_lee_pero_no_da_total():
    datos = _datos(MISMA_EN_PARRAFO_Y_TABLA, viejo=True)
    assert [t["tipo"] for t in datos["sanciones"]] == ["sin_evaluar", "sin_evaluar"]
    assert datos["money"] == []
    assert datos["money_motivos_codigos"] == ["ANALISIS_ANTIGUO"]
    assert datos["descripcion_sanciones"] == (
        "Se encontraron 2 referencias a sanciones: 2 con monto detectado. El total no está disponible."
    )
    visible = _texto_visible(_html(datos))
    assert "S/ 12500" in visible  # cada monto se conserva
    assert "Multa agrupada" not in visible
    assert visible.count("No se ha comprobado si esta referencia repite una multa") == 2  # una por referencia
    assert "sin comparar con otras" not in visible
    assert "Hay montos que necesitan revisión." not in visible  # los montos no están marcados para revisión
    assert "No se detectó el monto de algunas multas." not in visible


def test_un_analisis_con_documentos_nuevos_y_antiguos_explica_solo_lo_que_se_sabe():
    nuevo, viejo = doc(MISMA_EN_PARRAFO_Y_TABLA, "nuevo.md"), antiguo(doc(MISMA_EN_PARRAFO_Y_TABLA, "viejo.md"))
    datos = datos_desde_analisis(analisis_real([nuevo, viejo]), "2026-10-26")
    assert datos["money_motivos_codigos"] == ["ANALISIS_ANTIGUO"]  # no se afirma nada más sobre el documento evaluado
    assert datos["agrupacion"]["evaluacion"] == "PARCIAL"
    assert datos["agrupacion"]["sin_evaluar"] == 2
    assert "En 2 referencia(s) no se hizo la comparación" in _texto_visible(_html(datos))


def test_superar_el_limite_de_comparacion_indica_que_no_se_pudo_comprobar(monkeypatch):
    from app.modules.ingesta.domain import agrupacion_sanciones

    monkeypatch.setattr(agrupacion_sanciones, "LIMITE_MENCIONES_POR_AUTORIDAD", 2)
    datos = datos_desde_analisis(analisis_real([doc(TRES_MENCIONES_DE_UNA_AUTORIDAD)]), "2026-10-26")
    assert datos["money_motivos_codigos"] == ["NO_COMPARADAS"]
    assert datos["money_motivos"] == [MOTIVOS_TOTAL["NO_COMPARADAS"]]


def test_los_posibles_duplicados_dicen_que_podrian_ser_la_misma_multa_y_no_se_unen():
    datos = _datos(POSIBLE_DUPLICADA)
    assert datos["money_motivos_codigos"] == ["POSIBLE_MISMA_MULTA"]
    assert len(datos["sanciones"]) == 2
    assert len(datos["posibles_duplicados"]) == 1  # se conservan separadas
    visible = _texto_visible(_html(datos))
    assert "Algunas referencias podrían corresponder a la misma multa." in visible
    assert "Autoridad que aplicó la sanción: OEFA · S/ 9000" in visible


# ============================== coherencia entre conteos, descripciones y motivos ==============================

TEXTOS_VARIADOS = [
    pytest.param(MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO, id="ambiguo-y-sin-monto"),
    pytest.param(DOS_MULTAS_UNA_SIN_MONTO, id="dos-multas"),
    pytest.param(AGRUPACION_CON_MONTO_EN_UNA_CITA, id="agrupacion-parcial"),
    pytest.param(DOS_MONEDAS, id="dos-monedas"),
    pytest.param(UNA_SOLA, id="una-sola"),
    pytest.param(SIN_SANCIONES, id="sin-sanciones"),
    pytest.param(POSIBLE_DUPLICADA, id="posible-duplicada"),
    pytest.param(MISMA_EN_PARRAFO_Y_TABLA, id="parrafo-y-tabla"),
    pytest.param("# I\n\nEl OEFA impuso una multa de S/ 5 millones.\n\nLa SUNAFIL impuso una multa de S/ 3000.50.\n", id="escala-y-decimales"),
    pytest.param("# I\n\nEl OEFA impuso una multa de S/ 59.078 en 2023.\n", id="monto-ambiguo"),
]


@pytest.mark.parametrize("texto", TEXTOS_VARIADOS)
@pytest.mark.parametrize("viejo", [False, True], ids=["nuevo", "antiguo"])
def test_conteos_descripcion_y_motivos_son_coherentes(texto, viejo):
    datos = _datos(texto, viejo=viejo)
    total, con, sin = datos["referencias_total"], datos["referencias_con_monto"], datos["referencias_sin_monto"]
    pendientes = datos["referencias_pendientes"]
    assert con + sin == total
    assert pendientes <= con  # el pendiente es parte del «con monto», no una categoría aparte
    descripcion = datos["descripcion_sanciones"]
    if total:
        assert (f"{con} con monto detectado" in descripcion) == (con > 0 and total > 1)
        assert (f"{sin} sin monto detectado" in descripcion) == (sin > 0 and total > 1)
        assert ("necesit" in descripcion) == (pendientes > 0)
    else:
        assert descripcion == "No se detectaron sanciones económicas."
    # Disponible = hay sanciones y ningún motivo; sin total no hay ninguna suma; cada motivo tiene su texto aprobado.
    assert datos["money_disponible"] == (total > 0 and not datos["money_motivos_codigos"])
    assert bool(datos["money"]) == datos["money_disponible"]
    assert datos["money_motivos"] == [MOTIVOS_TOTAL[c] for c in datos["money_motivos_codigos"]]
    # Motivos respaldados: la revisión exige montos pendientes; "sin monto" exige referencias sin monto.
    if pendientes:
        assert "REVISION" in datos["money_motivos_codigos"]
        assert not datos["money_disponible"]
    if "SIN_MONTO" in datos["money_motivos_codigos"]:
        assert sin > 0
    if "REVISION" in datos["money_motivos_codigos"]:
        assert pendientes > 0
    # Lo que se lee en el PDF.
    html = _html(datos)
    visible = _texto_visible(html)
    assert "---" not in html
    assert "importe" not in visible.lower()
    assert "cuantificad" not in visible.lower()
    assert ("Total no disponible" in visible) == (total > 0 and not datos["money_disponible"])
    assert (NOTA_SIN_MONTO in visible) == (sin > 0)
    assert visible.count(NOTA_SIN_MONTO) <= 1
    assert (NOTA_REFERENCIAS in visible) == (total > 0)
    assert datos["casos_a_revisar"] <= len(datos["sanciones"])


def test_las_citas_tomadas_del_documento_se_conservan_aunque_usen_palabras_que_el_reporte_evita():
    datos = _datos(CITA_CON_LA_PALABRA_IMPORTE)
    (tarjeta,) = datos["sanciones"]
    assert "un importe de S/ 2000" in tarjeta["citas"][0]["cita"]  # la fuente no se altera para ajustar el lenguaje
    html = _html(datos)
    assert "un importe de S/ 2000" in html
    visible_sin_citas = _texto_visible(html.replace(tarjeta["citas"][0]["cita"], ""))
    assert "importe" not in visible_sin_citas.lower()


def test_los_textos_de_las_advertencias_no_dependen_del_mensaje_guardado():
    conflicto = SimpleNamespace(codigo="DATOS_EN_CONFLICTO", mensaje="Las menciones tienen datos que se contradicen")
    desconocida = SimpleNamespace(codigo="OTRA", mensaje="Aviso propio")
    assert plantilla._texto_advertencia(conflicto) == "Las referencias tienen datos que se contradicen; se muestran por separado"
    assert plantilla._texto_advertencia(desconocida) == "Aviso propio"


def test_un_analisis_guardado_con_el_mensaje_anterior_de_revision_no_lo_muestra_con_la_palabra_antigua():
    d = doc("# Cumplimiento\n\nEl OEFA impuso una multa de S/ 59.078 en 2023.\n")
    analisis = analisis_real([d])
    (unidad,) = analisis.agrupaciones_sanciones
    assert [(a.codigo, a.mensaje) for a in unidad.advertencias] == [("IMPORTE_A_REVISAR", "Importe que requiere revisión")]
    html = _html(datos_desde_analisis(analisis, "2026-10-26"))
    assert "Importe que requiere revisión" not in html  # el mensaje guardado no se copia: se muestra junto al monto
    assert _veces(html, "Monto pendiente de revisión") == 1


def test_cifras_distintas_dentro_de_una_multa_no_se_explican_como_formato_ambiguo():
    cien = SimpleNamespace(texto="S/ 100", unidad="S/", numero_texto="100", separador_ambiguo=False)
    doscientos = SimpleNamespace(texto="S/ 200", unidad="S/", numero_texto="200", separador_ambiguo=False)
    datos = plantilla._presentar_monto("REQUIERE_REVISION", [cien, doscientos])
    assert datos["monto"] == "S/ 100 / S/ 200"  # todos los textos originales, ninguno se elige
    assert datos["monto_etiqueta"] == "Monto pendiente de revisión"
    assert datos["monto_nota"] == "Las referencias a esta multa indican montos distintos. Por eso no se suma al total."


def test_un_monto_no_especificado_por_el_documento_no_se_presenta_como_no_detectado():
    datos = plantilla._presentar_monto("NO_ESPECIFICADO", [])
    assert datos["monto_etiqueta"] == "Monto no especificado en el documento"
    assert datos["tiene_monto"] is False
    assert datos["monto"] == ""


def test_sin_puntaje_no_se_muestran_guiones_ni_un_valor_sobre_cien():
    datos = _datos(SIN_SANCIONES)
    assert datos["puntaje_disponible"] is False
    assert datos["puntaje_texto"] == "No disponible"
    html = _html(datos)
    assert "---" not in html
    assert "No disponible/100" not in html
    assert "no tiene un puntaje ESG disponible" in _texto_visible(html)


# ============================== vista previa, reportes futuros y reportes ya emitidos ==============================

async def test_la_vista_previa_usa_los_textos_nuevos_y_no_guarda_nada(monkeypatch):
    from app.modules.reportes.service import vista_previa

    _parchear(monkeypatch, analisis_real([antiguo(doc(MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO))]))
    db = _FakeDB()
    compilados = []

    def compilar(html):
        compilados.append(html)
        return b"%PDF-vista-previa"

    pdf = await vista_previa(db, ambiente="development", empresa_id=uuid.uuid4(), anio=2025, compilar_pdf=compilar)
    assert pdf == b"%PDF-vista-previa"
    assert db.agregados == []
    assert "Referencias a sanciones" in compilados[0]
    assert "Monto pendiente de revisión" in compilados[0]


async def test_un_reporte_futuro_usa_los_textos_nuevos_y_su_snapshot_conserva_el_analisis(monkeypatch):
    from app.modules.reportes.service import generar_reporte

    analisis = analisis_real([doc(DOS_MULTAS_UNA_SIN_MONTO)])
    _parchear(monkeypatch, analisis)
    db = _FakeDB()
    compilados = []

    def compilar(html):
        compilados.append(html)
        return b"%PDF-1.4 nuevo"

    await generar_reporte(db, ambiente="development", empresa_id=uuid.uuid4(), anio=2025, usuario_id=uuid.uuid4(), compilar_pdf=compilar)
    (reporte,) = [o for o in db.agregados if hasattr(o, "snapshot")]
    assert "Total no disponible" in compilados[0]
    assert "No se detectó el monto de algunas multas." in compilados[0]
    assert reporte.snapshot == analisis.model_dump(mode="json")  # el snapshot es el análisis, no el texto del reporte
    assert "descripcion_sanciones" not in reporte.snapshot  # nada del lenguaje del reporte se guarda en el análisis


async def test_la_descarga_de_un_reporte_ya_emitido_entrega_el_pdf_guardado_sin_volver_a_renderizar(monkeypatch):
    import app.modules.reportes.service as svc

    def no_renderiza(*_a, **_k):
        raise AssertionError("un reporte emitido no se vuelve a renderizar")

    monkeypatch.setattr(svc, "renderizar", no_renderiza)
    monkeypatch.setattr(svc, "datos_desde_analisis", no_renderiza)
    snapshot_anterior = {"sanciones": [{"cita": "formato anterior"}], "resumen_sanciones": {"total": 2}}
    emitido = SimpleNamespace(
        id=uuid.uuid4(), empresa_id=uuid.uuid4(), anio=2024, pdf=b"%PDF-EMITIDO-ANTES", sha256="x",
        snapshot=copy.deepcopy(snapshot_anterior), generado_por=uuid.uuid4(), creado_en=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    pdf = await svc.obtener_pdf(_FakeDB(existente=emitido), emitido.id, usuario_id=uuid.uuid4())
    assert pdf == b"%PDF-EMITIDO-ANTES"
    assert (emitido.pdf, emitido.sha256, emitido.snapshot) == (b"%PDF-EMITIDO-ANTES", "x", snapshot_anterior)


def test_el_pdf_real_dice_lo_aprobado_y_no_lo_prohibido():
    pytest.importorskip("weasyprint", exc_type=(ImportError, OSError))
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO

    from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf

    datos = _datos(MONTO_AMBIGUO_Y_REFERENCIA_SIN_MONTO, viejo=True, sin_autoridad=True)
    lector = pypdf.PdfReader(BytesIO(compilar_html_a_pdf(_html(datos))))
    texto = re.sub(r"\s+", " ", " ".join(pagina.extract_text() for pagina in lector.pages))
    for frase in (
        datos["descripcion_sanciones"], NOTA_REFERENCIAS, NOTA_SIN_MONTO, NOTA_FORMATO, "Monto de la multa no detectado",
        "Monto pendiente de revisión", "Monto total de las multas", "Total no disponible", "Hay montos que necesitan revisión.",
        "Autoridad que aplicó la sanción: no identificada", "S/ 5,000",
    ):
        assert frase in texto, frase
    minusculas = texto.lower()
    for prohibida in ("---", "importe", "total sanciones", "cuantificad", "(0)", "anáfora"):
        assert prohibida not in minusculas, prohibida


@pytest.mark.parametrize(
    ("numero", "esperado"),
    [
        pytest.param("1 250 000", 1250000, id="espacio-repetido"),
        pytest.param("1 250 000", 1250000, id="espacio-de-no-corte-repetido"),
        pytest.param("1 250 000", 1250000, id="espacio-fino-de-no-corte-repetido"),
        pytest.param("1,250,000", 1250000, id="coma-repetida"),
        pytest.param("12 500", None, id="un-solo-separador-no-se-suma"),
        pytest.param("1,500", None, id="un-solo-separador-es-ambiguo"),
        pytest.param("12,50", None, id="decimal-no-se-interpreta"),
        pytest.param("1,250.000", None, id="separadores-mezclados"),
    ],
)
def test_solo_los_enteros_inequivocos_se_pueden_sumar(numero, esperado):
    monto = SimpleNamespace(texto=f"S/ {numero}", unidad="S/", numero_texto=numero, separador_ambiguo=False)
    assert plantilla._valor_entero(monto) == esperado

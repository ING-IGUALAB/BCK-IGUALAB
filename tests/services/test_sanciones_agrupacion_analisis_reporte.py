"""Agrupación de sanciones en el análisis GRI, el snapshot y el PDF del reporte (decisión 2026-10-19). Sin BD, red ni proveedores.

Los análisis se generan con los detectores REALES de la ingesta sobre textos sintéticos. Los análisis «antiguos» se obtienen
quitando de uno nuevo lo que el detector anterior no producía (agrupación, referencias, numeral, infracción): así se prueba la
lectura compatible sin fabricar un formato que el sistema nunca persistió.
"""
import copy
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.core.exceptions import InternalProcessingError
from app.models import SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import ResultadoAnalisis
from app.modules.analisis_gri.domain import calculo
from app.modules.analisis_gri.domain.calculo import DocumentoBase
from app.modules.analisis_gri.schemas import (
    AnalisisResponse,
    EmpresaResumen,
    EstadoFlujo,
    EstadoImporte,
    EstadoPuntaje,
    EvaluacionAgrupacion,
    PuntajeEsg,
)
from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from app.modules.ingesta.domain import agrupacion_sanciones
from app.modules.reportes.domain.plantilla import datos_desde_analisis, renderizar
from app.modules.reportes.service import generar_reporte

# La misma sanción en un párrafo y en una tabla (importe entero: sumable sin interpretar decimales).
MISMA_EN_PARRAFO_Y_TABLA = (
    "# Cumplimiento\n\nMediante la Resolución Directoral N° 0456-2024-SUNAFIL/IR, la SUNAFIL impuso una multa de S/ 12500 por la infracción N° 3.\n\n"
    "| Autoridad | Resolución | Infracción | Tipo | Monto (S/) |\n|---|---|---|---|---|\n"
    "| SUNAFIL | 0456-2024-SUNAFIL/IR | N° 3 | Multa impuesta | 12500 |\n"
)
# Dos menciones que solo coinciden en autoridad e importe.
POSIBLE_DUPLICADA = (
    "# Cumplimiento\n\nEl OEFA impuso una multa de S/ 9000 en 2023.\n\n"
    "Durante el ejercicio, la empresa recibió una multa del OEFA por S/ 9000.\n"
)
IMPORTE_AMBIGUO = "# Cumplimiento\n\nEl OEFA impuso una multa de S/ 59.078 en 2023.\n"
# El mismo párrafo (mismo texto) en dos posiciones distintas, con identidad explícita.
MISMO_TEXTO_DOS_POSICIONES = (
    "# Informe\n\nEn la Resolución N° 5-2024-OEFA/DFAI, infracción N° 1, el OEFA impuso una multa de S/ 100.\n\n"
    "Otro apartado.\n\n"
    "En la Resolución N° 5-2024-OEFA/DFAI, infracción N° 1, el OEFA impuso una multa de S/ 100.\n"
)
# Más menciones de una misma autoridad que el límite de comparación que fija la prueba.
TRES_MENCIONES_DE_UNA_AUTORIDAD = (
    "# Informe\n\nEl OEFA impuso una multa de S/ 1100 por la Resolución N° 11-2024-OEFA/DFAI.\n\n"
    "El OEFA impuso una multa de S/ 2200 por la Resolución N° 12-2024-OEFA/DFAI.\n\n"
    "El OEFA impuso una multa de S/ 3300 por la Resolución N° 13-2024-OEFA/DFAI.\n"
)


def doc(texto: str, nombre: str = "informe.md") -> DocumentoBase:
    analisis = analizar_texto(texto).a_dict()
    return DocumentoBase(
        id=uuid.uuid4(), nombre_archivo=nombre, sha256=uuid.uuid4().hex * 2, tipo=TipoDocumento.MEMORIA_ANUAL,
        resultado_analisis=ResultadoAnalisis(analisis["resultado"]),
        completado_en=datetime(2026, 10, 1, tzinfo=timezone.utc), analisis=analisis,
    )


def antiguo(documento: DocumentoBase) -> DocumentoBase:
    """El mismo documento tal como lo habría persistido el detector anterior a 2026-10-19."""
    analisis = copy.deepcopy(documento.analisis)
    for clave in ("agrupacion_sanciones", "version_agrupacion_sanciones"):
        analisis.pop(clave, None)
    analisis["version_detector_sanciones"] = "2026-10-15.1"
    for s in analisis["sanciones"]:
        for clave in ("referencias", "numeral", "infraccion"):
            s.pop(clave, None)
    return DocumentoBase(**{**documento.__dict__, "analisis": analisis})


def analisis_real(documentos: list[DocumentoBase]) -> AnalisisResponse:
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones(documentos)
    return AnalisisResponse(
        ambiente="development",
        empresa=EmpresaResumen(id=uuid.uuid4(), nombre="Empresa de prueba", sector=SectorEmpresa.MINERIA, activa=True),
        anio=2025, version=1, huella_documentos="h" * 64, estado_flujo=EstadoFlujo.VALIDADO, habilita_reporte=True,
        version_catalogo=[], documentos=calculo.resumir_documentos(documentos), filas=[], advertencias=[],
        sanciones=sanciones, agrupaciones_sanciones=agrupaciones, resumen_sanciones=resumen,
        puntaje=PuntajeEsg(
            estado=EstadoPuntaje.NO_DISPONIBLE, definitivo=False, valor=None, valor_presentacion=None,
            decimales_presentacion=1, suma_puntos=None, filas_contadas=0, filas_evaluables=0, filas_sin_estado=0,
            motivo="NO_DISPONIBLE",
        ),
        pendientes=[], validacion=None,
    )


# ============================================ análisis GRI ============================================

def test_el_analisis_expone_la_agrupacion_con_todas_las_citas_y_su_procedencia():
    d = doc(MISMA_EN_PARRAFO_Y_TABLA, nombre="memoria.md")
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([d])
    (grupo,) = agrupaciones
    assert grupo.agrupada is True
    assert grupo.menciones == 2
    assert grupo.autoridad.texto == "SUNAFIL"
    assert [r.identificador for r in grupo.identificadores] == ["0456-2024-SUNAFIL/IR"]
    assert grupo.estado_importe is EstadoImporte.CUANTIFICADO
    assert len(grupo.citas) == 2
    for cita in grupo.citas:
        assert (cita.documento_id, cita.documento_nombre) == (d.id, "memoria.md")
        assert MISMA_EN_PARRAFO_Y_TABLA[cita.cita_inicio:cita.cita_fin] == cita.cita
        assert MISMA_EN_PARRAFO_Y_TABLA[cita.inicio:cita.fin] == cita.referencia_original
        assert cita.seccion
    assert {s.agrupacion_id for s in sanciones} == {grupo.id}
    assert resumen.total == len(sanciones) == 2  # `total` sigue contando menciones
    assert resumen.evaluacion_agrupacion is EvaluacionAgrupacion.COMPLETA
    assert (resumen.unidades, resumen.agrupaciones_identificadas, resumen.menciones_agrupadas) == (1, 1, 2)


def test_la_funcion_anterior_conserva_su_contrato_de_dos_valores():
    sanciones, resumen = calculo.construir_sanciones([doc(MISMA_EN_PARRAFO_Y_TABLA)])
    assert len(sanciones) == resumen.total == 2


def test_el_mismo_texto_en_dos_posiciones_son_dos_evidencias():
    d = doc(MISMO_TEXTO_DOS_POSICIONES)
    (grupo,) = calculo.construir_sanciones_y_agrupaciones([d])[2]
    assert grupo.menciones == 2
    assert len(grupo.citas) == 2
    assert grupo.citas[0].cita == grupo.citas[1].cita
    assert grupo.citas[0].cita_inicio != grupo.citas[1].cita_inicio


def test_la_misma_evidencia_exacta_de_una_misma_posicion_no_se_duplica():
    d = doc(MISMO_TEXTO_DOS_POSICIONES)
    analisis = copy.deepcopy(d.analisis)
    analisis["sanciones"][1] = copy.deepcopy(analisis["sanciones"][0])
    repetido = DocumentoBase(**{**d.__dict__, "analisis": analisis})
    (grupo,) = calculo.construir_sanciones_y_agrupaciones([repetido])[2]
    assert grupo.menciones == 2
    assert len(grupo.citas) == 1


def test_documentos_distintos_nunca_se_fusionan_aunque_digan_lo_mismo():
    uno, otro = doc(MISMA_EN_PARRAFO_Y_TABLA, "uno.md"), doc(MISMA_EN_PARRAFO_Y_TABLA, "otro.md")
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([uno, otro])
    assert len(agrupaciones) == 2
    assert {a.documento_id for a in agrupaciones} == {uno.id, otro.id}
    assert len({a.id for a in agrupaciones}) == 2
    assert all(a.menciones == 2 and len({c.documento_id for c in a.citas}) == 1 for a in agrupaciones)
    assert resumen.total == 4
    assert resumen.agrupaciones_identificadas == 2


def test_posibles_duplicados_de_documentos_distintos_no_se_relacionan():
    uno = doc("# Cumplimiento\n\nEl OEFA impuso una multa de S/ 9000 en 2023.\n", "uno.md")
    otro = doc("# Cumplimiento\n\nEl OEFA impuso una multa de S/ 9000 en 2022.\n", "otro.md")
    _, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([uno, otro])
    assert len(agrupaciones) == 2
    assert all(a.advertencias == [] for a in agrupaciones)
    assert resumen.posibles_duplicados == 0


def test_posible_duplicado_dentro_de_un_documento_se_expone_con_el_otro_id_y_sin_fusionar():
    _, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([doc(POSIBLE_DUPLICADA)])
    primera, segunda = agrupaciones
    assert not primera.agrupada
    assert [(a.codigo, a.mensaje, a.con) for a in primera.advertencias] == [
        ("POSIBLE_SANCION_DUPLICADA", "Posible sanción duplicada", [segunda.id])
    ]
    assert resumen.posibles_duplicados == 1
    assert resumen.agrupaciones_identificadas == 0
    assert resumen.unidades == 2  # no es un total de sanciones únicas


def test_un_importe_ambiguo_conserva_su_texto_y_marca_la_unidad_a_revisar():
    sanciones, resumen, (unidad,) = calculo.construir_sanciones_y_agrupaciones([doc(IMPORTE_AMBIGUO)])
    assert unidad.estado_importe is EstadoImporte.REQUIERE_REVISION
    assert [m.texto for m in unidad.importes] == ["S/ 59.078"]
    assert unidad.advertencias[0].mensaje == "Importe que requiere revisión"
    assert resumen.importes_a_revisar == 1
    assert sanciones[0].cuantificada is True  # la mención sí tiene un monto escrito; no se interpreta ni se anula


def test_un_analisis_antiguo_se_lee_sin_afirmar_que_fue_agrupado_ni_revisado():
    antiguo_doc = antiguo(doc(MISMA_EN_PARRAFO_Y_TABLA))
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([antiguo_doc])
    assert agrupaciones == []
    assert all(s.agrupacion_id is None and s.referencias == [] and s.numeral is None for s in sanciones)
    assert resumen.total == 2
    assert resumen.evaluacion_agrupacion is EvaluacionAgrupacion.NO_EVALUADA
    assert resumen.unidades is None
    assert (resumen.menciones_sin_evaluar, resumen.agrupaciones_identificadas, resumen.posibles_duplicados) == (2, 0, 0)
    assert (resumen.cuantificadas, resumen.no_cuantificadas) == (2, 0)  # los campos anteriores no cambian de significado


def test_un_analisis_con_documentos_nuevos_y_antiguos_es_parcial():
    nuevo, viejo = doc(MISMA_EN_PARRAFO_Y_TABLA, "nuevo.md"), antiguo(doc(MISMA_EN_PARRAFO_Y_TABLA, "viejo.md"))
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([nuevo, viejo])
    assert len(agrupaciones) == 1
    assert resumen.evaluacion_agrupacion is EvaluacionAgrupacion.PARCIAL
    assert resumen.menciones_sin_evaluar == 2
    assert [s.agrupacion_id is None for s in sanciones] == [False, False, True, True]


def test_sin_sanciones_la_evaluacion_no_aplica():
    _, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([doc("# Informe\n\nTexto sin sanciones.\n")])
    assert agrupaciones == []
    assert resumen.evaluacion_agrupacion is EvaluacionAgrupacion.NO_APLICA


@pytest.mark.parametrize(
    "dano",
    [
        pytest.param(lambda a: a["agrupacion_sanciones"]["unidades"].pop(), id="menciones-sin-unidad"),
        pytest.param(lambda a: a["agrupacion_sanciones"]["unidades"][0].update(estado_importe="OTRO"), id="estado-desconocido"),
        pytest.param(lambda a: a["agrupacion_sanciones"].update(evaluacion="NADA"), id="evaluacion-desconocida"),
        pytest.param(lambda a: a.update(agrupacion_sanciones=[]), id="no-es-objeto"),
    ],
)
def test_una_agrupacion_almacenada_con_estructura_invalida_no_se_corrige_en_silencio(dano):
    d = doc(POSIBLE_DUPLICADA)
    analisis = copy.deepcopy(d.analisis)
    dano(analisis)
    danado = [DocumentoBase(**{**d.__dict__, "analisis": analisis})]
    with pytest.raises(InternalProcessingError) as capturado:
        calculo.construir_sanciones_y_agrupaciones(danado)
    assert capturado.value.code == calculo.CODIGO_ERROR_DATOS_ALMACENADOS


def test_el_analisis_sigue_siendo_json_y_el_puntaje_y_los_estados_no_dependen_de_la_agrupacion():
    d = doc(MISMA_EN_PARRAFO_Y_TABLA + "\nSe aplica GRI 305-1 y GRI 302-1.\n")
    con_agrupacion, _ = calculo.construir_filas([d])
    sin_agrupacion, _ = calculo.construir_filas([antiguo(d)])
    assert [f.model_dump() for f in con_agrupacion] == [f.model_dump() for f in sin_agrupacion]
    vista = analisis_real([d]).model_dump(mode="json")
    assert vista["agrupaciones_sanciones"][0]["citas"]
    assert vista["resumen_sanciones"]["evaluacion_agrupacion"] == "COMPLETA"


# ============================================ reporte: datos y HTML ============================================

def test_el_reporte_muestra_la_agrupacion_con_todas_sus_citas_y_suma_un_solo_importe():
    datos = datos_desde_analisis(analisis_real([doc(MISMA_EN_PARRAFO_Y_TABLA)]), "2026-10-19")
    (tarjeta,) = datos["sanciones"]
    assert tarjeta["tipo"] == "agrupada"
    assert tarjeta["entidad"] == "SUNAFIL"
    assert tarjeta["identificador"] == "0456-2024-SUNAFIL/IR"
    assert len(tarjeta["citas"]) == 2
    assert all(c["fuente"] and c["posicion"] for c in tarjeta["citas"])
    assert datos["sanciones_total"] == 2  # menciones
    assert datos["money"] == [{"unidad": "S/", "total": "12,500"}]  # 12 500, no 25 000: las menciones repetidas no se suman
    assert datos["money_disponible"] is True
    assert "Total monetario no disponible" not in renderizar(datos)


def test_con_posibles_duplicados_no_hay_total_pero_se_conservan_los_importes_individuales():
    datos = datos_desde_analisis(analisis_real([doc(POSIBLE_DUPLICADA)]), "2026-10-19")
    assert [t["tipo"] for t in datos["sanciones"]] == ["individual", "individual"]
    assert all(t["advertencias"] == ["Posible sanción duplicada"] for t in datos["sanciones"])
    assert [t["monto"] for t in datos["sanciones"]] == ["S/ 9000", "S/ 9000"]  # cada importe sigue visible
    (grupo,) = datos["posibles_duplicados"]
    assert len(grupo) == 2
    assert datos["money"] == []  # ninguna suma parcial se presenta como total
    assert datos["money_disponible"] is False
    assert any("posibles duplicados" in m for m in datos["money_motivos"])
    html = renderizar(datos)
    assert "Posible sanción duplicada" in html
    assert "Posibles duplicados sin resolver" in html
    assert "Total monetario no disponible" in html
    assert "S/ 9000" in html


def test_el_reporte_muestra_el_importe_ambiguo_con_su_aviso_y_no_lo_suma():
    datos = datos_desde_analisis(analisis_real([doc(IMPORTE_AMBIGUO)]), "2026-10-19")
    (tarjeta,) = datos["sanciones"]
    assert tarjeta["monto"] == "S/ 59.078"
    assert tarjeta["estado_importe"] == "REQUIERE_REVISION"
    assert tarjeta["advertencias"] == ["Importe que requiere revisión"]
    assert datos["money"] == []
    assert datos["money_disponible"] is False
    html = renderizar(datos)
    assert "Importe que requiere revisión" in html
    assert "S/ 59.078" in html
    assert "Total monetario no disponible" in html


def test_el_reporte_de_un_analisis_antiguo_no_lo_presenta_como_deduplicado():
    datos = datos_desde_analisis(analisis_real([antiguo(doc(MISMA_EN_PARRAFO_Y_TABLA))]), "2026-10-19")
    assert [t["tipo"] for t in datos["sanciones"]] == ["sin_evaluar", "sin_evaluar"]
    assert datos["agrupacion"]["evaluacion"] == "NO_EVALUADA"
    assert datos["money"] == []
    assert datos["money_disponible"] is False
    html = renderizar(datos)
    assert "Mención sin agrupación evaluada" in html
    assert "no se presentan como deduplicadas" in html
    assert "Sanción agrupada" not in html
    assert "Total monetario no disponible" in html


def test_un_importe_con_escala_o_decimales_impide_el_total_pero_se_muestra():
    texto = "# I\n\nEl OEFA impuso una multa de S/ 5 millones.\n\nLa SUNAFIL impuso una multa de S/ 3000.50.\n"
    datos = datos_desde_analisis(analisis_real([doc(texto)]), "2026-10-19")
    assert datos["money"] == []
    assert datos["money_disponible"] is False
    assert any("escala o decimales" in m for m in datos["money_motivos"])
    assert {t["monto"] for t in datos["sanciones"]} == {"S/ 5 millones", "S/ 3000.50"}  # sin interpretar ni convertir
    assert "Total monetario no disponible" in renderizar(datos)


def test_cuando_el_total_si_es_completo_se_suma_un_solo_importe_por_agrupacion_y_moneda():
    texto = MISMA_EN_PARRAFO_Y_TABLA + "\nEl OEFA impuso una multa de S/ 800 y la SUNAT una multa de US$ 450 por otros hechos.\n"
    datos = datos_desde_analisis(analisis_real([doc(texto)]), "2026-10-19")
    assert datos["money_disponible"] is True
    assert datos["money"] == [{"unidad": "S/", "total": "13,300"}, {"unidad": "US$", "total": "450"}]


@pytest.mark.parametrize(
    ("segunda", "estado"),
    [
        pytest.param(
            "La SUNAFIL impuso una multa a la empresa; no se especifica el monto.", "NO_ESPECIFICADO", id="monto-no-especificado"
        ),
        pytest.param("La SUNAFIL impuso una multa a la empresa.", "NO_CONSTA", id="sin-importe-asociado"),
    ],
)
def test_una_sancion_distinta_sin_importe_impide_presentar_una_suma_parcial_como_total(segunda, estado):
    texto = f"# Informe\n\nEl OEFA impuso una multa de S/ 1000 en 2023.\n\n{segunda}\n"
    datos = datos_desde_analisis(analisis_real([doc(texto)]), "2026-10-19")
    conocida, sin_importe = datos["sanciones"]
    assert (conocida["estado_importe"], sin_importe["estado_importe"]) == ("CUANTIFICADO", estado)
    assert datos["money"] == []  # S/ 1000 no se presenta como total
    assert datos["money_disponible"] is False
    assert any("sin importe determinado" in m for m in datos["money_motivos"])
    html = renderizar(datos)
    assert "Total monetario no disponible" in html
    assert "S/ 1000" in html  # el importe individual y las citas se conservan
    assert all(t["citas"] for t in datos["sanciones"])


def test_una_agrupacion_con_monto_en_una_cita_y_ausente_en_otra_no_bloquea_el_total():
    texto = (
        "# Informe\n\nEl OEFA impuso una multa de S/ 3000 por la Resolución N° 31-2023-OEFA/DFAI, infracción N° 4.\n\n"
        "La Resolución N° 31-2023-OEFA/DFAI, infracción N° 4, es una multa impuesta por el OEFA; no se especifica el monto.\n"
    )
    analisis = analisis_real([doc(texto)])
    (unidad,) = analisis.agrupaciones_sanciones
    assert (unidad.agrupada, unidad.menciones, len(unidad.citas)) == (True, 2, 2)
    assert [s.monto is None for s in analisis.sanciones] == [False, True]  # una cita omite el monto
    datos = datos_desde_analisis(analisis, "2026-10-19")
    assert datos["money_disponible"] is True
    assert datos["money"] == [{"unidad": "S/", "total": "3,000"}]
    assert "Total monetario no disponible" not in renderizar(datos)


def test_sin_sanciones_el_reporte_lo_dice_y_el_total_no_se_inventa():
    datos = datos_desde_analisis(analisis_real([doc("# Informe\n\nTexto sin sanciones.\n")]), "2026-10-19")
    assert datos["sanciones"] == []
    assert datos["money"] == []
    assert "No se identificaron sanciones." in renderizar(datos)


# ============================================ evaluación PARCIAL ============================================

def test_superar_el_limite_de_comparacion_no_elimina_menciones_ni_citas_y_se_indica_parcial(monkeypatch):
    monkeypatch.setattr(agrupacion_sanciones, "LIMITE_MENCIONES_POR_AUTORIDAD", 2)
    d = doc(TRES_MENCIONES_DE_UNA_AUTORIDAD)
    assert d.analisis["agrupacion_sanciones"]["evaluacion"] == "PARCIAL"
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones([d])
    assert resumen.evaluacion_agrupacion is EvaluacionAgrupacion.PARCIAL
    assert resumen.total == len(sanciones) == 3  # ninguna mención se pierde
    assert len(agrupaciones) == 3
    citas = [c.cita for a in agrupaciones for c in a.citas]
    assert len(citas) == 3
    assert all(TRES_MENCIONES_DE_UNA_AUTORIDAD[c.cita_inicio:c.cita_fin] == c.cita for a in agrupaciones for c in a.citas)

    datos = datos_desde_analisis(analisis_real([d]), "2026-10-19")
    assert datos["agrupacion"]["evaluacion"] == "PARCIAL"
    assert len(datos["sanciones"]) == 3
    assert datos["money"] == []
    assert any("PARCIAL" in m for m in datos["money_motivos"])
    html = renderizar(datos)
    assert "Evaluación de agrupación PARCIAL" in html
    assert "Total monetario no disponible" in html
    for importe in ("S/ 1100", "S/ 2200", "S/ 3300"):  # los importes individuales siguen visibles
        assert importe in html


def test_el_snapshot_de_un_analisis_parcial_conserva_todas_las_menciones_y_el_estado(monkeypatch):
    monkeypatch.setattr(agrupacion_sanciones, "LIMITE_MENCIONES_POR_AUTORIDAD", 2)
    vista = analisis_real([doc(TRES_MENCIONES_DE_UNA_AUTORIDAD)]).model_dump(mode="json")
    assert vista["resumen_sanciones"]["evaluacion_agrupacion"] == "PARCIAL"
    assert len(vista["sanciones"]) == len(vista["agrupaciones_sanciones"]) == 3


def test_el_pdf_real_muestra_agrupaciones_y_advertencias():
    pytest.importorskip("weasyprint", exc_type=(ImportError, OSError))
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO

    from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf

    html = renderizar(datos_desde_analisis(analisis_real([doc(MISMA_EN_PARRAFO_Y_TABLA), doc(POSIBLE_DUPLICADA)]), "2026-10-19"))
    lector = pypdf.PdfReader(BytesIO(compilar_html_a_pdf(html)))
    texto = " ".join(pagina.extract_text() for pagina in lector.pages)
    assert "Sanción agrupada" in texto
    assert "Posible sanción duplicada" in texto
    assert "0456-2024-SUNAFIL/IR" in texto


# ============================================ reporte: snapshot e inmutabilidad ============================================

class _FakeDB:
    def __init__(self, existente=None):
        self.existente = existente
        self.agregados = []

    def add(self, obj):
        self.agregados.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, obj):
        obj.id = getattr(obj, "id", None) or uuid.uuid4()
        obj.creado_en = getattr(obj, "creado_en", None) or datetime.now(timezone.utc)

    async def execute(self, _q):
        existente = self.existente

        class R:
            def scalar_one_or_none(self):
                return existente

            def first(self):
                return existente

        return R()

    async def get(self, _modelo, _id):
        return self.existente


def _parchear(monkeypatch, analisis):
    import app.modules.reportes.service as svc

    async def validado(*_a, **_k):
        return analisis

    async def nombres(*_a, **_k):
        return ("Empresa de prueba", "Usuario de prueba")

    monkeypatch.setattr(svc, "obtener_analisis_validado", validado)
    monkeypatch.setattr(svc, "_nombres", nombres)


async def test_el_snapshot_de_un_reporte_nuevo_conserva_agrupaciones_citas_y_advertencias(monkeypatch):
    analisis = analisis_real([doc(MISMA_EN_PARRAFO_Y_TABLA), doc(POSIBLE_DUPLICADA)])
    _parchear(monkeypatch, analisis)
    db = _FakeDB()
    html_compilado = []

    def compilar(html):
        html_compilado.append(html)
        return b"%PDF-1.4 fake"

    await generar_reporte(db, ambiente="development", empresa_id=uuid.uuid4(), anio=2025, usuario_id=uuid.uuid4(), compilar_pdf=compilar)
    (reporte,) = [o for o in db.agregados if hasattr(o, "snapshot")]
    agrupaciones = reporte.snapshot["agrupaciones_sanciones"]
    assert len(agrupaciones) == 3
    assert sum(a["agrupada"] for a in agrupaciones) == 1
    assert [len(a["citas"]) for a in agrupaciones if a["agrupada"]] == [2]
    assert {adv["mensaje"] for a in agrupaciones for adv in a["advertencias"]} == {"Posible sanción duplicada"}
    assert all(s["agrupacion_id"] for s in reporte.snapshot["sanciones"])
    assert reporte.snapshot["resumen_sanciones"]["posibles_duplicados"] == 1
    assert "Sanción agrupada" in html_compilado[0]


async def test_un_reporte_existente_no_se_modifica_aunque_el_analisis_actual_tenga_agrupaciones(monkeypatch):
    from app.core.exceptions import ConflictError

    _parchear(monkeypatch, analisis_real([doc(MISMA_EN_PARRAFO_Y_TABLA)]))
    snapshot_anterior = {"empresa": {"nombre": "Empresa"}, "sanciones": [{"cita": "formato anterior"}]}
    existente = SimpleNamespace(
        id=uuid.uuid4(), empresa_id=uuid.uuid4(), anio=2025, pdf=b"%PDF-ORIGINAL", sha256="x", snapshot=copy.deepcopy(snapshot_anterior),
        generado_por=uuid.uuid4(), creado_en=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    db = _FakeDB(existente=existente)
    usuario_id = uuid.uuid4()

    def no_compila(_html):
        raise AssertionError("un reporte existente no se regenera")

    with pytest.raises(ConflictError) as capturado:
        await generar_reporte(
            db, ambiente="development", empresa_id=existente.empresa_id, anio=2025, usuario_id=usuario_id, compilar_pdf=no_compila
        )
    assert capturado.value.code == "REPORT_ALREADY_EXISTS"
    assert db.agregados == []
    assert (existente.pdf, existente.sha256, existente.snapshot) == (b"%PDF-ORIGINAL", "x", snapshot_anterior)

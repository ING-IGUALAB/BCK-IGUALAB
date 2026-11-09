"""Estado registrado en Análisis GRI para el Asistente IA — reglas PURAS (sin BD, sin red, sin modelo): reconocimiento del código explícito y
composición del texto con los datos del análisis. El flujo completo (lectura real de Análisis GRI, ambas rutas, aislamiento) está en
`test_estado_gri_asistente_postgres.py` y `test_seguimiento_asistente_postgres.py`; aquí los análisis son dobles mínimos."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.models.analisis_gri import EstadoGri, TipoIdentidadGri
from app.modules.analisis_gri.schemas import ETIQUETAS_ESTADO, EstadoFlujo, MotivoNoVigente
from app.modules.asistente.domain.estado_gri import (
    MAX_CODIGOS,
    codigos_explicitos,
    codigos_para_estado,
    componer_estado,
    menciona_brecha,
    pide_estado,
    solo_estado,
)
from app.modules.asistente.domain.historial import MAX_MENSAJE_AUSENCIA

EMPRESA = "Camposol S.A. (SINTETICA)"


def codigos(pregunta: str) -> list[str]:
    return [c.codigo for c in codigos_explicitos(pregunta)]


# ============================================ Reconocimiento del código explícito ============================================

@pytest.mark.parametrize(
    ("pregunta", "esperados"),
    [
        ("Sobre el GRI 1, ¿qué me puedes comentar? ¿Hay una brecha?", ["1"]),
        ("¿Qué estado tiene GRI 13?", ["13"]),  # GRI 13 es el 13, nunca el 1
        ("¿Qué estado tiene GRI 102?", ["102"]),  # ni el 1 ni el 10
        ("¿Qué estado tiene GRI 1 y GRI 13?", ["1", "13"]),
        ("¿Estado de gri-2 y de GRI 303?", ["2", "303"]),  # sin distinguir mayúsculas; guion y espacio duro
        ("¿Cuál es el estado de GRI 1?", ["1"]),
        ("¿Estado de GRI 1 y otra vez GRI 1?", ["1"]),  # sin repetir
        ("¿Hay brecha en GRI 1234?", []),  # cuatro cifras: no es ningún estándar
        ("¿Hay brecha en AGRI 1?", []),  # «GRI» dentro de otra palabra
        ("¿Qué estado tiene el estándar 305?", []),  # sin el prefijo GRI no es un código explícito
        ("¿Qué estado tiene GRI?", []),
        ("¿Qué estado tiene GRI 0 y GRI 01?", []),  # sin ceros a la izquierda
    ],
)
def test_solo_se_reconoce_un_codigo_gri_explicito_y_completo(pregunta, esperados):
    assert codigos(pregunta) == esperados


def test_una_revelacion_pertenece_a_su_estandar_y_lo_avisa():
    (pedido,) = codigos_explicitos("¿Cuál es el estado de GRI 305-1?")
    assert (pedido.codigo, pedido.revelacion, pedido.referencia) == ("305", True, "GRI 305-1")
    (con_letra,) = codigos_explicitos("¿Estado de GRI 305-1-a?")
    assert (con_letra.codigo, con_letra.revelacion) == ("305", True)
    (simple,) = codigos_explicitos("¿Estado de GRI 305?")
    assert simple.revelacion is False


def test_una_edicion_solo_cuenta_si_se_escribe_pegada_al_codigo():
    assert [(c.codigo, c.edicion) for c in codigos_explicitos("¿Estado de GRI 102 (2016) y GRI 102-2025?")] == [("102", "2016"), ("102", "2025")]
    assert codigos_explicitos("¿Estado de GRI 1: Fundamentos 2021?")[0].edicion is None
    assert codigos_explicitos("¿Estado de GRI 1 en 2025?")[0].edicion is None  # el año de la consulta no es una edición


@pytest.mark.parametrize(
    ("pregunta", "pide"),
    [
        ("¿Qué estado tiene GRI 1?", True),
        ("¿Hay una brecha en GRI 1?", True),
        ("¿Está validado GRI 1?", True),
        ("¿Sigue vigente la validación de GRI 1?", True),
        ("¿Es GRI 1 Sub-reportado o de Baja sustancia?", True),
        ("¿Qué dice el documento sobre GRI 1?", False),
        ("Sobre el GRI 1, ¿qué me puedes comentar?", False),
        ("¿Cuánto redujo su consumo de agua?", False),
    ],
)
def test_solo_una_pregunta_de_estado_o_brecha_pide_el_estado(pregunta, pide):
    assert pide_estado(pregunta) is pide
    assert bool(codigos_para_estado(pregunta)) is (pide and bool(codigos_explicitos(pregunta)))


def test_pedir_estado_sin_nombrar_un_codigo_no_consulta_nada():
    assert codigos_para_estado("¿Hay alguna brecha en la empresa?") == []


def test_menciona_brecha_distingue_la_brecha_de_los_demas_terminos_de_estado():
    assert menciona_brecha("¿Hay una BRECHA en GRI 1?") and not menciona_brecha("¿Qué estado tiene GRI 1?")


# ============================================ «Solo estado»: gramática cerrada (preparada, sin conectar) ============================================

@pytest.mark.parametrize(
    "pregunta",
    [
        "¿Qué estado tiene GRI 1?",
        "que estado tiene el GRI 1",
        "¿Cuál estado tiene GRI 13?",
        "¿Cuál es el estado de GRI 1?",
        "¿Cuál es el estado registrado de GRI 305?",
        "Dime el estado de GRI 102.",
        "Estado de GRI 1",
        "Estado del GRI 1",
        "¿Y qué estado tiene GRI 13?",  # un seguimiento que repite el código es autosuficiente
        "¿Qué estado tiene GRI 1 en 2025?",  # el año de la conversación es relleno admitido
        "¿Qué estado tiene GRI 1 de la empresa para 2025, por favor?",
        "¿Cuál es el estado actual de GRI 1 en el Análisis GRI?",
        "¿Qué estado tiene GRI 102 (2016)?",  # una edición explícita no es una revelación
    ],
)
def test_una_peticion_clara_del_estado_de_un_codigo_es_solo_estado(pregunta):
    pedido = solo_estado(pregunta, 2025)
    assert pedido is not None and pedido == codigos_explicitos(pregunta)[0]


@pytest.mark.parametrize(
    "pregunta",
    [
        # Mixtas o con algo más: conservan el flujo documental y la verificación de citas.
        "¿Qué dice el documento sobre GRI 1 y qué estado tiene?",
        "Sobre el GRI 1, ¿qué me puedes comentar? ¿Hay una brecha?",
        "¿Qué estado tiene GRI 1 y por qué?",
        "¿Qué estado tiene GRI 1 según el reporte?",
        "¿Qué estado tiene GRI 1 en el informe?",
        "¿Qué estado tiene GRI 1 y qué menciona el documento?",
        "Explícame el estado de GRI 1",
        "¿Qué estado tiene GRI 1 comparado con GRI 13?",
        # Ambiguas: la ausencia de las palabras «documento» o «menciona» NO basta.
        "¿Y GRI 1?",
        "GRI 1",
        "Sobre GRI 1, estado",
        "¿Hay una brecha en GRI 1?",  # «brecha» sigue el flujo documental (el estado se agrega al mensaje)
        "¿Está validado GRI 1?",
        "¿Qué opina el informe de GRI 1 en cuanto a su estado?",
        # Sin código explícito: necesita contexto (en la continuación, el modelo).
        "¿Y qué estado tiene ese?",
        "¿Qué estado tiene el estándar anterior?",
        "¿Qué estado tiene el estándar 305?",
        # Más de un código, una revelación o un código inexistente.
        "¿Qué estado tienen GRI 1 y GRI 13?",
        "¿Qué estado tiene GRI 305-1?",
        "¿Qué estado tiene GRI 1234?",
        # Otro año: es un cambio de contexto, no «solo estado».
        "¿Qué estado tiene GRI 1 en 2024?",
        "¿Qué estado tiene GRI 1 el año anterior?",
    ],
)
def test_una_pregunta_mixta_ambigua_o_con_contexto_no_es_solo_estado(pregunta):
    assert solo_estado(pregunta, 2025) is None


def test_solo_estado_no_confunde_gri_1_con_gri_13_ni_gri_102():
    assert solo_estado("¿Qué estado tiene GRI 13?", 2025).codigo == "13"
    assert solo_estado("¿Qué estado tiene GRI 102?", 2025).codigo == "102"
    assert solo_estado("¿Qué estado tiene GRI 1?", 2025).codigo == "1"


def test_el_anio_admitido_como_relleno_es_el_de_la_conversacion():
    assert solo_estado("¿Qué estado tiene GRI 1 en 2024?", 2024) is not None
    assert solo_estado("¿Qué estado tiene GRI 1 en 2024?", 2025) is None


# ============================================ Composición con los datos del análisis ============================================

def fila(codigo, edicion, estado=None, *, tipo=TipoIdentidadGri.EDICION_RESUELTA):
    estandar = f"GRI {codigo} ({edicion})" if edicion else f"GRI {codigo} (edición sin resolver)"
    return SimpleNamespace(
        codigo=codigo, edicion=edicion, identidad_tipo=tipo, estandar=estandar, estado=estado,
        estado_etiqueta=None if estado is None else ETIQUETAS_ESTADO[estado],
    )


def validacion(*, vigente=True, motivos=(), numero=3):
    return SimpleNamespace(
        vigente=vigente, motivos_no_vigente=list(motivos), numero=numero, validado_en=dt.datetime(2026, 11, 8, 15, 0, tzinfo=dt.timezone.utc)
    )


def analisis(filas, flujo=EstadoFlujo.VALIDADO, val="vigente"):
    if val == "vigente":
        val = validacion()
    return SimpleNamespace(anio=2025, filas=filas, estado_flujo=flujo, validacion=val)


def componer(pregunta, datos, maximo=MAX_MENSAJE_AUSENCIA):
    return componer_estado(
        codigos_para_estado(pregunta), datos, empresa_nombre=EMPRESA, brecha=menciona_brecha(pregunta), maximo=maximo
    )


def test_con_estado_y_validacion_vigente_informa_ambos_con_la_etiqueta_exacta():
    texto = componer("¿Qué estado tiene GRI 1?", analisis([fila("1", "2021", EstadoGri.BAJA_SUSTANCIA)]))
    assert texto.splitlines()[0] == f"Estado registrado en Análisis GRI ({EMPRESA}, 2025):"
    assert "- GRI 1 (2021): Baja sustancia. Validación vigente (n.º 3, validada el 2026-11-08)." in texto
    assert "BAJA_SUSTANCIA" not in texto  # la etiqueta, no el nombre interno


@pytest.mark.parametrize(
    ("estado", "etiqueta"),
    [(EstadoGri.OK, "OK"), (EstadoGri.BAJA_SUSTANCIA, "Baja sustancia"), (EstadoGri.SUB_REPORTADO, "Sub-reportado")],
)
def test_cada_estado_se_informa_con_su_etiqueta_y_no_se_convierte_en_un_si_o_no_sobre_la_brecha(estado, etiqueta):
    texto = componer("¿Hay una brecha en GRI 1?", analisis([fila("1", "2021", estado)]))
    assert f"GRI 1 (2021): {etiqueta}." in texto
    assert "No hay una regla registrada que convierta el estado en «hay» o «no hay» brecha" in texto
    for conclusion in ("sí hay una brecha", "no hay brecha", "existe una brecha", "no existe brecha", "sin brecha"):
        assert conclusion not in texto.casefold()


def test_la_aclaracion_de_brecha_solo_aparece_si_la_pregunta_la_menciona():
    assert "regla registrada" not in componer("¿Qué estado tiene GRI 1?", analisis([fila("1", "2021", EstadoGri.OK)]))


def test_si_la_ultima_validacion_perdio_vigencia_informa_el_estado_actual_y_que_no_esta_vigente():
    datos = analisis(
        [fila("1", "2021", EstadoGri.OK)], flujo=EstadoFlujo.LISTO_PARA_VALIDAR,
        val=validacion(vigente=False, motivos=[MotivoNoVigente.ESTADOS_CAMBIARON, MotivoNoVigente.DOCUMENTOS_CAMBIARON], numero=2),
    )
    texto = componer("¿Qué estado tiene GRI 1?", datos)
    assert "GRI 1 (2021): OK. No cuenta con validación vigente: la última validación (n.º 2) perdió vigencia" in texto
    assert "cambiaron los estados" in texto and "cambió el conjunto de documentos elegibles" in texto
    assert "Validación vigente" not in texto


def test_si_el_analisis_aun_no_se_ha_validado_lo_dice():
    texto = componer("¿Qué estado tiene GRI 1?", analisis([fila("1", "2021", EstadoGri.OK)], flujo=EstadoFlujo.LISTO_PARA_VALIDAR, val=None))
    assert "GRI 1 (2021): OK. No cuenta con validación vigente: el análisis aún no se ha validado." in texto


def test_la_vigencia_exige_el_flujo_validado_y_la_validacion_vigente_a_la_vez():
    # La misma regla de `obtener_analisis_validado`: ni un flujo VALIDADO con validación no vigente ni al revés bastan.
    incoherente = analisis([fila("1", "2021", EstadoGri.OK)], flujo=EstadoFlujo.VALIDADO, val=validacion(vigente=False))
    assert "No cuenta con validación vigente" in componer("¿Qué estado tiene GRI 1?", incoherente)
    pendiente = analisis([fila("1", "2021", EstadoGri.OK)], flujo=EstadoFlujo.PENDIENTE_ESTADOS, val=validacion(vigente=True))
    assert "No cuenta con validación vigente" in componer("¿Qué estado tiene GRI 1?", pendiente)


def test_un_codigo_detectado_sin_estado_asignado_lo_dice_y_no_inventa_un_estado():
    texto = componer("¿Qué estado tiene GRI 1?", analisis([fila("1", "2021")], flujo=EstadoFlujo.PENDIENTE_ESTADOS, val=None))
    assert "GRI 1 (2021): figura en el análisis, pero no tiene estado asignado." in texto
    for etiqueta in ETIQUETAS_ESTADO.values():
        assert etiqueta not in texto


def test_un_codigo_que_no_figura_en_el_analisis_no_concluye_nada_sobre_el_documento_ni_sobre_una_brecha():
    texto = componer("¿Hay una brecha en GRI 55?", analisis([fila("1", "2021", EstadoGri.OK)]))
    assert "GRI 55: no figura en el Análisis GRI de esta empresa y año (no se concluye nada sobre el documento)." in texto
    for afirmacion in ("ausente", "no está en el documento", "no aparece en el documento", "hay una brecha", "existe una brecha"):
        assert afirmacion not in texto.casefold()


def test_sin_documentos_elegibles_lo_dice_en_lugar_de_dar_a_entender_que_el_codigo_no_existe():
    texto = componer("¿Qué estado tiene GRI 1?", analisis([], flujo=EstadoFlujo.DOCUMENTACION_INSUFICIENTE, val=None))
    assert "no tiene documentos elegibles" in texto and "no figura" not in texto


def test_gri_1_gri_13_y_gri_102_se_resuelven_cada_uno_con_su_propia_fila():
    datos = analisis([
        fila("1", "2021", EstadoGri.BAJA_SUSTANCIA), fila("13", "2022", EstadoGri.OK), fila("102", "2016", EstadoGri.SUB_REPORTADO),
        fila("305", "2016", EstadoGri.OK),
    ])
    uno = componer("¿Qué estado tiene GRI 1?", datos)
    assert "GRI 1 (2021): Baja sustancia" in uno and "GRI 13" not in uno and "GRI 102" not in uno
    trece = componer("¿Qué estado tiene GRI 13?", datos)
    assert "GRI 13 (2022): OK" in trece and "GRI 1 (2021)" not in trece
    ciento_dos = componer("¿Qué estado tiene GRI 102?", datos)
    assert "GRI 102 (2016): Sub-reportado" in ciento_dos and "GRI 1 (2021)" not in ciento_dos and "GRI 13" not in ciento_dos


def test_una_identidad_ambigua_o_con_varias_ediciones_se_informa_completa_sin_elegir_una():
    datos = analisis([
        fila("102", "2016", EstadoGri.OK), fila("102", None, EstadoGri.SUB_REPORTADO, tipo=TipoIdentidadGri.EDICION_AMBIGUA),
    ])
    texto = componer("¿Qué estado tiene GRI 102?", datos)
    assert "GRI 102 (2016): OK" in texto and "GRI 102 (edición sin resolver): Sub-reportado" in texto
    assert "tiene más de una identidad en el análisis; indique la edición" in texto


def test_una_edicion_explicita_nunca_toma_el_estado_de_otra_identidad():
    solo_ambigua = analisis([fila("102", None, EstadoGri.SUB_REPORTADO, tipo=TipoIdentidadGri.EDICION_AMBIGUA)])
    texto = componer("¿Qué estado tiene GRI 102 (2016)?", solo_ambigua)
    assert "no hay una fila de esa edición" in texto and "GRI 102 (edición sin resolver)" in texto
    assert "Sub-reportado" not in texto  # el estado de la identidad ambigua no se atribuye a la edición pedida
    resuelta = analisis([fila("102", "2016", EstadoGri.OK), fila("102", "2025", EstadoGri.SUB_REPORTADO)])
    texto = componer("¿Qué estado tiene GRI 102 (2016)?", resuelta)
    assert "GRI 102 (2016): OK" in texto and "2025" not in texto.split("\n", 1)[1]


def test_una_revelacion_hereda_el_estado_del_estandar_y_lo_avisa():
    texto = componer("¿Cuál es el estado de GRI 305-1?", analisis([fila("305", "2016", EstadoGri.OK)]))
    assert "GRI 305-1: el estado se registra por estándar (GRI 305), no por revelación." in texto
    assert "GRI 305 (2016): OK" in texto


def test_el_texto_cabe_siempre_en_el_maximo_y_omite_codigos_con_aviso_antes_que_recortar_un_estado():
    filas = [fila(c, "2021", EstadoGri.BAJA_SUSTANCIA) for c in ("1", "2", "3", "4", "5")]
    pregunta = "¿Estado de GRI 1? ¿Estado de GRI 2? ¿Estado de GRI 3? ¿Estado de GRI 4? ¿Estado de GRI 5? ¿Hay brecha?"
    completo = componer(pregunta, analisis(filas))
    assert len(completo) <= MAX_MENSAJE_AUSENCIA and f"se omiten {5 - MAX_CODIGOS} códigos más" in completo
    assert completo.count("Baja sustancia") == MAX_CODIGOS
    justo = componer(pregunta, analisis(filas), maximo=300)
    assert len(justo) <= 300 and "Baja sustancia" in justo and "GRI 1 (2021)" in justo
    assert "se omiten" in justo

"""Tests del Módulo 7 (Reportes de Prospección): plantilla HTML y compilación PDF."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.modules.analisis_gri.schemas import EstadoGri
from app.modules.reportes.domain.plantilla import datos_desde_analisis, renderizar


def _monto_fake(texto="S/ 4,200,000", numero="4200000", ambiguo=False):
    return SimpleNamespace(texto=texto, unidad="S/", numero_texto=numero, separador_ambiguo=ambiguo)


def _mencion_fake(monto):
    return SimpleNamespace(entidad=SimpleNamespace(texto="SUNAFIL"), cita="Motivo", cita_inicio=10, cita_fin=16,
                           documento_nombre="Doc", seccion="p. 1", monto=monto)


def _analisis_fake():
    evid = SimpleNamespace(cita="Emisiones de 1.2 t CO2")
    fila_ok = SimpleNamespace(codigo="305", nombre="Emisiones", estandar="GRI 305 (2016)", estado=EstadoGri.OK,
                              estado_etiqueta="OK", evidencias=[evid])
    fila_sub = SimpleNamespace(codigo="401", nombre="Empleo", estandar="GRI 401 (2016)", estado=EstadoGri.SUB_REPORTADO,
                               estado_etiqueta="Sub-reportado", evidencias=[evid])
    return SimpleNamespace(
        empresa=SimpleNamespace(nombre="Minera Local", sector="MINERIA"),
        anio=2025, version=4, filas=[fila_ok, fila_sub],
        puntaje=SimpleNamespace(valor_presentacion=50.0),
        resumen_sanciones=SimpleNamespace(total=2, cuantificadas=1, no_cuantificadas=1),
        sanciones=[_mencion_fake(_monto_fake()), _mencion_fake(None)],
        validacion=SimpleNamespace(huella_documentos="a" * 64,
                                   validado_por=SimpleNamespace(nombre="Admin"),
                                   validado_en=SimpleNamespace(date=lambda: SimpleNamespace(isoformat=lambda: "2026-10-15"))),
    )


def _datos_ejemplo():
    return {"empresa_nombre": "Minera", "anio": "2025", "sector": "MINERIA", "fecha_generacion": "hoy",
            "puntaje": "82", "conteo_ok": 1, "conteo_baja": 0, "conteo_sub": 0,
            "sanciones_total": 0, "sanciones_cuantificadas": 0, "sanciones_sin_monto": 0,
            "money": [], "sanciones": [],
            "filas": [{"codigo": "305", "tema": "Emisiones", "estado_color": "ok", "estado_texto": "OK",
                       "cita": "cita", "fuente": "p. 1"}],
            "version_estados": 1, "huella": "abc", "validado_por": "A", "validado_en": "hoy"}


# --- plantilla: render y datos -----------------------------------------------------------

def test_renderizar_produce_html_valido():
    html = renderizar(_datos_ejemplo())
    assert "<!DOCTYPE html>" in html
    assert "Resumen ejecutivo" in html
    assert "Minera" in html
    assert "305" in html


def test_sin_sanciones_el_reporte_no_muestra_guiones_ni_un_total_imposible():
    html = renderizar(_datos_ejemplo())
    assert "---" not in html
    assert "Total no disponible" not in html
    assert html.count("No se detectaron sanciones económicas") >= 1
    assert "Monto total de las multas" in html


def test_datos_desde_analisis_construye_conteo_y_puntaje():
    d = datos_desde_analisis(_analisis_fake(), "2026-10-15")
    assert d["conteo_ok"] == 1
    assert d["conteo_sub"] == 1
    assert d["puntaje"] == "50"
    assert d["sanciones_total"] == 2
    assert (d["sanciones_cuantificadas"], d["sanciones_sin_monto"]) == (1, 1)  # con + sin = total: salen de la misma lista
    assert len(d["filas"]) == 2
    assert d["filas"][0]["cita"] == "Emisiones de 1.2 t CO2"
    assert d["filas"][0]["estado_color"] == "ok"


def test_suma_montos_por_moneda():
    a = _analisis_fake()
    a.sanciones = [_mencion_fake(_monto_fake())]
    a.resumen_sanciones = SimpleNamespace(
        total=1, cuantificadas=1, no_cuantificadas=0, evaluacion_agrupacion=SimpleNamespace(value="COMPLETA")
    )
    d = datos_desde_analisis(a, "x")
    assert d["money"] == [{"unidad": "S/", "total": "4,200,000"}]
    assert d["money_disponible"] is True


def test_sin_evaluacion_de_agrupacion_no_se_presenta_ningun_total():
    a = _analisis_fake()
    a.sanciones = [_mencion_fake(_monto_fake())]
    d = datos_desde_analisis(a, "x")
    assert d["money"] == []
    assert d["money_disponible"] is False
    assert d["money_motivos_codigos"] == ["ANALISIS_ANTIGUO"]
    html = renderizar(d)
    assert "Total no disponible" in html
    assert "El análisis guardado no incluye la información necesaria para calcular el total." in html
    assert "S/ 4,200,000" in html  # el monto individual se conserva


# --- compilación PDF real (WeasyPrint) ---------------------------------------------------

def test_compila_pdf_real():
    pytest.importorskip("weasyprint", exc_type=(ImportError, OSError))
    from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf

    pdf = compilar_html_a_pdf(renderizar(_datos_ejemplo()))
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000

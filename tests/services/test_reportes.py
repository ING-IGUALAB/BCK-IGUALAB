"""Tests del Módulo 7 (Reportes de Prospección): plantilla HTML y compilación PDF."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.modules.analisis_gri.schemas import EstadoGri
from app.modules.reportes.domain.plantilla import datos_desde_analisis, renderizar


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
        sanciones=[],
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
    assert "<!DOCTYPE html>" in html and "Resumen ejecutivo" in html
    assert "Minera" in html and "305" in html


def test_datos_desde_analisis_construye_conteo_y_puntaje():
    d = datos_desde_analisis(_analisis_fake(), "2026-10-15")
    assert d["conteo_ok"] == 1 and d["conteo_sub"] == 1
    assert d["puntaje"] == "50"
    assert d["sanciones_total"] == 2
    assert len(d["filas"]) == 2
    assert d["filas"][0]["cita"] == "Emisiones de 1.2 t CO2"
    assert d["filas"][0]["estado_color"] == "ok"


def test_suma_montos_por_moneda():
    s = SimpleNamespace(entidad=SimpleNamespace(texto="SUNAFIL"), cita="Motivo",
                        documento_nombre="Doc", seccion="p. 1",
                        monto=SimpleNamespace(texto="S/ 4,200,000", unidad="S/", numero_texto="4200000",
                                              separador_ambiguo=False))
    a = _analisis_fake()
    a.sanciones = [s]
    d = datos_desde_analisis(a, "x")
    assert d["money"] == [{"unidad": "S/", "total": "4,200,000"}]


# --- compilación PDF real (WeasyPrint) ---------------------------------------------------

def test_compila_pdf_real():
    pytest.importorskip("weasyprint")
    from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf

    pdf = compilar_html_a_pdf(renderizar(_datos_ejemplo()))
    assert pdf[:5] == b"%PDF-" and len(pdf) > 1000

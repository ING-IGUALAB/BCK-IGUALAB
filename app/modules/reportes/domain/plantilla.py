"""Relleno de la plantilla HTML del reporte de prospección (Módulo 7). Regla pura: no llama a IA,
solo convierte el análisis validado en el HTML. El PDF es un render de datos ya validados (RN-040):
la plantilla HTML se rellena con jinja2 (autoescape) y se compila a PDF con WeasyPrint.
"""
from __future__ import annotations

from pathlib import Path

import jinja2

from app.modules.analisis_gri.schemas import AnalisisResponse, EstadoGri, SancionVista

_RUTA_PLANTILLA = Path(__file__).resolve().parent.parent / "plantillas" / "reporte_prospeccion.html"

# Clases CSS de los badges de estado (definidas en la plantilla).
_CLASE_ESTADO = {
    "OK": ("ok", "OK"),
    "BAJA_SUSTANCIA": ("baja", "Baja sustancia"),
    "SUB_REPORTADO": ("sub", "Sub-reportado"),
}


def _monto_a_numero(numero_texto: str) -> float | None:
    """Convierte «4,200,000» o «1240» a número; None si no se puede interpretar con seguridad."""
    limpio = numero_texto.replace(",", "").replace(" ", "").replace(".", "")
    return float(limpio) if limpio.isdigit() else None


def _fmt_puntaje(analisis: AnalisisResponse) -> str:
    valor = analisis.puntaje.valor_presentacion
    if valor is None:
        return "---"
    return str(int(valor)) if float(valor).is_integer() else str(valor)


def _cita_de_fila(fila) -> tuple[str, str]:
    if not fila.evidencias:
        return "(sin cita de respaldo)", ""
    ev = fila.evidencias[0]
    fuente = " · ".join(x for x in [getattr(ev, "documento_nombre", ""), getattr(ev, "seccion", "")] if x)
    return ev.cita, fuente


def _fila_a_dict(fila) -> dict:
    tema = fila.nombre or fila.estandar
    estado_id = fila.estado.value if fila.estado else ""
    clase, etiqueta = _CLASE_ESTADO.get(estado_id, ("neutro", fila.estado_etiqueta or "sin estado"))
    cita, fuente = _cita_de_fila(fila)
    return {"codigo": fila.codigo, "tema": tema, "estado_color": clase,
            "estado_texto": etiqueta, "cita": cita, "fuente": fuente}


def _sancion_a_dict(s: SancionVista) -> dict:
    entidad = s.entidad.texto if s.entidad else "(entidad no identificada)"
    fuente = " · ".join(x for x in [s.documento_nombre, s.seccion] if x)
    return {
        "entidad": entidad,
        "descripcion": s.cita,
        "fuente": fuente,
        "tiene_monto": s.monto is not None,
        "monto": s.monto.texto if s.monto else "No cuantificada",
    }


def _sumas_por_unidad(sanciones: list[SancionVista]) -> dict[str, float]:
    """Suma los montos por moneda, SOLO de montos no ambiguos (RN-031/032: no se inventa)."""
    sumas: dict[str, float] = {}
    for s in sanciones:
        m = s.monto
        if m is None or m.separador_ambiguo:
            continue
        numero = _monto_a_numero(m.numero_texto)
        if numero is not None:
            sumas[m.unidad] = sumas.get(m.unidad, 0.0) + numero
    return sumas


def datos_desde_analisis(analisis: AnalisisResponse, fecha_generacion: str) -> dict:
    """Convierte el análisis validado en el diccionario de la plantilla (valores crudos; jinja2 los escapa)."""
    empresa = analisis.empresa
    san = analisis.resumen_sanciones
    val = analisis.validacion

    conteo = {"ok": 0, "baja": 0, "sub": 0}
    for f in analisis.filas:
        if f.estado is EstadoGri.OK:
            conteo["ok"] += 1
        elif f.estado is EstadoGri.BAJA_SUSTANCIA:
            conteo["baja"] += 1
        elif f.estado is EstadoGri.SUB_REPORTADO:
            conteo["sub"] += 1

    sumas = _sumas_por_unidad(analisis.sanciones)
    money = [{"unidad": u, "total": f"{v:,.0f}"} for u, v in sumas.items()]

    return {
        "empresa_nombre": empresa.nombre,
        "anio": analisis.anio,
        "sector": getattr(empresa.sector, "value", empresa.sector),
        "fecha_generacion": fecha_generacion,
        "puntaje": _fmt_puntaje(analisis),
        "conteo_ok": conteo["ok"], "conteo_baja": conteo["baja"], "conteo_sub": conteo["sub"],
        "sanciones_total": san.total,
        "sanciones_cuantificadas": san.cuantificadas,
        "sanciones_sin_monto": san.no_cuantificadas,
        "money": money,
        "filas": [_fila_a_dict(f) for f in analisis.filas],
        "sanciones": [_sancion_a_dict(s) for s in analisis.sanciones],
        "version_estados": analisis.version,
        "huella": (val.huella_documentos[:12] + "...") if val else "—",
        "validado_por": val.validado_por.nombre if val else "—",
        "validado_en": val.validado_en.date().isoformat() if val else "—",
    }


def renderizar(datos: dict) -> str:
    """Rellena la plantilla HTML con los datos (jinja2 autoescape = seguro ante XSS en los datos)."""
    plantilla = _RUTA_PLANTILLA.read_text(encoding="utf-8")
    return jinja2.Template(plantilla, autoescape=True).render(**datos)

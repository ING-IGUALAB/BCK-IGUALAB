"""Utilidades de texto compartidas por los detectores del dominio de ingesta.

`recortar_cita` centraliza el recorte de una cita textual: acota a una ventana alrededor de la
referencia (`max_longitud`/`margen`) y quita blancos en los extremos sin dejar fuera la referencia.
Antes estaba reimplementado en `deteccion_gri` y `deteccion_sanciones`.
"""
from __future__ import annotations


def recortar_cita(
    texto: str,
    desde: int,
    hasta: int,
    ref_inicio: int,
    ref_fin: int,
    max_longitud: int,
    margen: int,
) -> tuple[int, int]:
    """Devuelve `(desde, hasta)` recortado: si supera `max_longitud`, se abre una ventana de `margen`
    alrededor de la referencia; luego se recortan los blancos de los extremos sin salir de la referencia."""
    if hasta - desde > max_longitud:
        desde = max(desde, ref_inicio - margen)
        hasta = min(hasta, ref_fin + margen)
    while desde < ref_inicio and texto[desde].isspace():
        desde += 1
    while hasta > ref_fin and texto[hasta - 1].isspace():
        hasta -= 1
    return desde, hasta

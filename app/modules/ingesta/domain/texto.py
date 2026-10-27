"""Utilidades de texto compartidas por los detectores del dominio de ingesta.

`recortar_cita` centraliza el recorte de una cita textual: acota a una ventana alrededor de la
referencia (`max_longitud`/`margen`) y quita blancos en los extremos sin dejar fuera la referencia.
Antes estaba reimplementado en `deteccion_gri` y `deteccion_sanciones`.
"""
from __future__ import annotations


def _inicio_de_palabra(texto: str, posicion: int, tope: int) -> int:
    """Primera posición >= `posicion` (sin pasar de `tope`) que empieza una palabra, es decir, precedida por un blanco."""
    while posicion < tope and not texto[posicion - 1].isspace():
        posicion += 1
    return posicion


def _fin_de_palabra(texto: str, posicion: int, tope: int) -> int:
    """Última posición <= `posicion` (sin bajar de `tope`) donde termina una palabra: lo que sigue es un blanco."""
    while posicion > tope and not texto[posicion].isspace():
        posicion -= 1
    return posicion


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
    alrededor de la referencia; luego se recortan los blancos de los extremos sin salir de la referencia.

    Un extremo que la ventana acorta (no el que coincide con el límite de la línea o del bloque) se ajusta HACIA ADENTRO a un
    límite de palabra (un blanco): la cita no empieza ni termina a mitad de palabra ni parte un token como «**dato**» (puede
    abrir o cerrar un énfasis, que la versión legible retira). El ajuste nunca
    cruza la referencia, así que la ventana sigue acotada y el fragmento sigue siendo exacto del texto."""
    if hasta - desde > max_longitud:
        ventana_desde = max(desde, ref_inicio - margen)
        ventana_hasta = min(hasta, ref_fin + margen)
        if ventana_desde > desde:
            desde = _inicio_de_palabra(texto, ventana_desde, ref_inicio)
        if ventana_hasta < hasta:
            hasta = _fin_de_palabra(texto, ventana_hasta, ref_fin)
    while desde < ref_inicio and texto[desde].isspace():
        desde += 1
    while hasta > ref_fin and texto[hasta - 1].isspace():
        hasta -= 1
    return desde, hasta

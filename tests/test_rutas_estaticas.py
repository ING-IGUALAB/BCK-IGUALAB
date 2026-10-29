"""Ninguna ruta estática de la aplicación REAL puede ser capturada por una ruta con identificador declarada antes.

Starlette atiende la primera ruta cuyo patrón coincide. `GET /documentos/disponibilidad` también coincide con
`GET /documentos/{documento_id}`; si esta se declarara antes, la consulta respondería 422 (UUID inválido) en lugar de ejecutarse.
Sin base de datos ni red: solo se evalúa el enrutamiento de `app.main.app`.
"""
import pytest

from tests.ayudantes_rutas import operaciones_openapi, ruta_que_atiende

# El enrutamiento se evalúa despachando por la aplicación real (ver tests/ayudantes_rutas.py): `app.routes` ya no es una lista plana.


@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [("GET", "/documentos/disponibilidad"), ("GET", "/reportes/disponibilidad")],
)
async def test_las_rutas_de_disponibilidad_las_atiende_su_propio_endpoint(metodo, ruta):
    atiende = await ruta_que_atiende(metodo, ruta)
    assert atiende is not None
    assert atiende.path == ruta  # la ruta estática, no una con parámetros


async def test_ninguna_ruta_estatica_queda_oculta_por_una_con_identificador():
    capturadas = []
    estaticas = [(metodo, ruta) for metodo, ruta in operaciones_openapi() if "{" not in ruta]
    assert estaticas, "la aplicación debe documentar rutas estáticas"
    for metodo, ruta in estaticas:
        atiende = await ruta_que_atiende(metodo, ruta)
        if getattr(atiende, "path", None) != ruta or metodo not in atiende.methods:
            capturadas.append((metodo, ruta, getattr(atiende, "path", None)))
    assert capturadas == []

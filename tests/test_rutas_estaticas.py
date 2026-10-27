"""Ninguna ruta estática de la aplicación REAL puede ser capturada por una ruta con identificador declarada antes.

Starlette atiende la primera ruta cuyo patrón coincide. `GET /documentos/disponibilidad` también coincide con
`GET /documentos/{documento_id}`; si esta se declarara antes, la consulta respondería 422 (UUID inválido) en lugar de ejecutarse.
Sin base de datos ni red: solo se evalúa el enrutamiento de `app.main.app`.
"""
import pytest
from starlette.routing import Match

from app.main import app


def _primera_ruta_que_atiende(metodo: str, ruta: str):
    alcance = {"type": "http", "method": metodo, "path": ruta, "root_path": ""}
    for candidata in app.routes:
        coincidencia, _ = candidata.matches(alcance)
        if coincidencia is Match.FULL:
            return candidata
    return None


@pytest.mark.parametrize(
    ("metodo", "ruta"),
    [("GET", "/documentos/disponibilidad"), ("GET", "/reportes/disponibilidad")],
)
def test_las_rutas_de_disponibilidad_las_atiende_su_propio_endpoint(metodo, ruta):
    atiende = _primera_ruta_que_atiende(metodo, ruta)
    assert atiende is not None
    assert atiende.path == ruta  # la ruta estática, no una con parámetros


def test_ninguna_ruta_estatica_queda_oculta_por_una_con_identificador():
    capturadas = []
    for ruta in app.routes:
        metodos = getattr(ruta, "methods", None)
        if not metodos or "{" in ruta.path:
            continue
        for metodo in metodos - {"HEAD", "OPTIONS"}:
            atiende = _primera_ruta_que_atiende(metodo, ruta.path)
            if atiende is not ruta:
                capturadas.append((metodo, ruta.path, getattr(atiende, "path", None)))
    assert capturadas == []

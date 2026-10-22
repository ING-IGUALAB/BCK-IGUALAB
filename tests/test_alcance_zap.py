"""Guard del alcance del escaneo OWASP ZAP (pruebas-zap/alcance/operaciones.json).

El stage «OWASP ZAP» (rama qa) falla si el OpenAPI real tiene una operación que nadie clasificó. Este test adelanta ese fallo al stage
de pruebas de cualquier rama: una operación nueva obliga a decidir, en el mismo cambio, si se escanea o se excluye y por qué.
No ejecuta ZAP ni toca la red.
"""
import json
from pathlib import Path

import pytest

from app.main import app

RUTA_ALCANCE = Path(__file__).resolve().parent.parent / "pruebas-zap" / "alcance" / "operaciones.json"
METODOS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}

# Operaciones que NUNCA deben escanearse de forma automática (credenciales, sesión del escáner, cuentas, servicios externos).
SIEMPRE_EXCLUIDAS = {
    ("POST", "/auth/logout"),
    ("PATCH", "/auth/mi-contrasena"),
    ("POST", "/auth/recuperar-contrasena"),
    ("POST", "/auth/restablecer-contrasena"),
    ("POST", "/usuarios"),
    ("PATCH", "/usuarios/{usuario_id}/habilitar"),
    ("PATCH", "/usuarios/{usuario_id}/deshabilitar"),
    ("POST", "/usuarios/transferir-superadmin"),
    ("POST", "/documentos/operaciones/{operacion_id}/ingesta"),
    ("POST", "/documentos/operaciones/{operacion_id}/reintentar-publicacion"),
    ("POST", "/asistente/consultas"),
}


@pytest.fixture(scope="module")
def alcance():
    return json.loads(RUTA_ALCANCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def operaciones_reales():
    esquema = app.openapi()
    return {
        (metodo.upper(), ruta)
        for ruta, item in esquema["paths"].items()
        for metodo in item
        if metodo in METODOS
    }


def test_toda_operacion_del_openapi_esta_clasificada(alcance, operaciones_reales):
    clasificadas = {(op["metodo"], op["ruta"]) for op in alcance["operaciones"]}
    sin_clasificar = sorted(operaciones_reales - clasificadas)
    assert not sin_clasificar, (
        "Operaciones sin clasificar en pruebas-zap/alcance/operaciones.json (decida si el ZAP las incluye o las excluye): "
        f"{sin_clasificar}"
    )


def test_el_alcance_no_conserva_operaciones_que_ya_no_existen(alcance, operaciones_reales):
    obsoletas = sorted({(op["metodo"], op["ruta"]) for op in alcance["operaciones"]} - operaciones_reales)
    assert not obsoletas, f"Operaciones clasificadas que ya no existen en el OpenAPI: {obsoletas}"


def test_las_operaciones_sensibles_siguen_excluidas(alcance):
    decisiones = {(op["metodo"], op["ruta"]): op["decision"] for op in alcance["operaciones"]}
    incluidas = sorted(clave for clave in SIEMPRE_EXCLUIDAS if decisiones.get(clave) != "excluida")
    assert not incluidas, f"Operaciones que deben permanecer excluidas del escaneo automático: {incluidas}"


def test_toda_clasificacion_tiene_motivo_y_categoria_valida(alcance):
    for op in alcance["operaciones"]:
        assert op["decision"] in {"incluida", "excluida"}
        assert op["categoria"] in alcance["categorias"]
        assert op["rol"] in alcance["roles"]
        assert op["motivo"].strip(), f"{op['metodo']} {op['ruta']} sin motivo"

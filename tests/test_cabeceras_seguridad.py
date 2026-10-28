"""Cabeceras de seguridad (X-Content-Type-Options y Cross-Origin-Resource-Policy) en respuestas exitosas, errores y PDF, sin alterar CORS.

Usa la aplicación real (``app.main.app``) sin arrancar su ``lifespan`` (no toca bases de datos) y dobles solo en el punto de entrada.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.core.cabeceras_seguridad import CabecerasDeSeguridadMiddleware
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models import RolUsuario
from app.modules.reportes import router as reportes_router

NOSNIFF = "nosniff"
CORP = "same-origin"


def con_cabeceras(respuesta):
    assert respuesta.headers.get("x-content-type-options") == NOSNIFF
    assert respuesta.headers.get("cross-origin-resource-policy") == CORP
    # Una sola vez cada una (no duplicadas por pasar por dos capas).
    assert [k for k, _ in respuesta.headers.raw if k.lower() == b"x-content-type-options"].__len__() == 1
    assert [k for k, _ in respuesta.headers.raw if k.lower() == b"cross-origin-resource-policy"].__len__() == 1


@pytest.fixture
def cliente_sin_lifespan():
    """La aplicación real SIN ejecutar el lifespan (que crea tablas y se conecta a PostgreSQL)."""
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


def test_respuesta_exitosa(cliente_sin_lifespan):
    r = cliente_sin_lifespan.get("/health")
    assert r.status_code == 200
    con_cabeceras(r)


def test_error_404(cliente_sin_lifespan):
    r = cliente_sin_lifespan.get(f"/{uuid.uuid4()}")
    assert r.status_code == 404
    con_cabeceras(r)


def test_error_422_de_validacion(cliente_sin_lifespan):
    r = cliente_sin_lifespan.post("/auth/login", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    con_cabeceras(r)


def test_error_401_sin_sesion(cliente_sin_lifespan):
    r = cliente_sin_lifespan.get("/usuarios")
    assert r.status_code == 401
    con_cabeceras(r)


def test_error_500_uniforme_tambien_las_lleva(cliente_sin_lifespan, monkeypatch):
    from app.modules.auth import service

    monkeypatch.setattr(service, "autenticar", AsyncMock(side_effect=RuntimeError("fallo inesperado")))
    r = cliente_sin_lifespan.post("/auth/login", json={"correo": "a@igualab-prueba.org", "password": "x"})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    con_cabeceras(r)


def test_pdf_descargado_con_su_content_type_y_las_cabeceras(cliente_sin_lifespan, monkeypatch):
    pdf = b"%PDF-1.4\n%prueba\n"
    monkeypatch.setattr(reportes_router, "obtener_pdf", AsyncMock(return_value=pdf))
    admin = SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.ADMINISTRADOR, habilitado=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    reporte_id = uuid.uuid4()
    r = cliente_sin_lifespan.get(f"/reportes/{reporte_id}/archivo")
    assert r.status_code == 200
    assert r.content == pdf
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="reporte_{reporte_id}.pdf"'
    con_cabeceras(r)


def test_la_vista_previa_de_pdf_tambien_las_lleva(cliente_sin_lifespan, monkeypatch):
    monkeypatch.setattr(reportes_router, "vista_previa", AsyncMock(return_value=b"%PDF-1.4\n"))
    admin = SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.ADMINISTRADOR, habilitado=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    r = cliente_sin_lifespan.post("/reportes/vista-previa", json={"empresa_id": str(uuid.uuid4()), "anio": 2025})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    con_cabeceras(r)


def test_cors_no_cambia(cliente_sin_lifespan):
    origen = settings.CORS_ORIGINS[0]
    simple = cliente_sin_lifespan.get("/health", headers={"Origin": origen})
    assert simple.headers["access-control-allow-origin"] == origen
    assert simple.headers["access-control-allow-credentials"] == "true"
    con_cabeceras(simple)

    preflight = cliente_sin_lifespan.options(
        "/usuarios", headers={"Origin": origen, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origen
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()

    ajeno = cliente_sin_lifespan.get("/health", headers={"Origin": "https://sitio-ajeno.example"})
    assert "access-control-allow-origin" not in ajeno.headers


def test_el_frontend_autorizado_sigue_recibiendo_el_pdf_por_cors_con_corp_same_origin(cliente_sin_lifespan, monkeypatch):
    """CORP no se evalúa en `fetch` modo CORS: la respuesta lleva a la vez el permiso CORS del frontend y `same-origin`."""
    pdf = b"%PDF-1.4\n%prueba\n"
    monkeypatch.setattr(reportes_router, "obtener_pdf", AsyncMock(return_value=pdf))
    admin = SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.ADMINISTRADOR, habilitado=True)
    app.dependency_overrides[get_current_user] = lambda: admin
    origen = settings.CORS_ORIGINS[0]
    r = cliente_sin_lifespan.get(f"/reportes/{uuid.uuid4()}/archivo", headers={"Origin": origen, "Authorization": "Bearer prueba"})
    assert r.status_code == 200
    assert r.content == pdf
    assert r.headers["access-control-allow-origin"] == origen
    assert r.headers["cross-origin-resource-policy"] == "same-origin"


def test_no_se_anaden_otras_politicas(cliente_sin_lifespan):
    """Fuera de alcance de esta tarea: CSP y Cache-Control no cambian."""
    r = cliente_sin_lifespan.get("/health")
    for ausente in ("content-security-policy", "cache-control", "strict-transport-security", "x-frame-options", "referrer-policy"):
        assert ausente not in r.headers, ausente


def test_cors_sigue_siendo_el_middleware_mas_externo():
    assert app.user_middleware[0].cls.__name__ == "CORSMiddleware"
    nombres = [m.cls.__name__ for m in app.user_middleware]
    assert nombres.index("CabecerasDeSeguridadMiddleware") < nombres.index("ManejadorExcepcionesMiddleware"), "por fuera del manejador de errores"


# ---------------------------------------------------------------------------------------------------- el middleware solo
def _app_minima(**cabeceras_de_la_ruta):
    pequena = FastAPI()
    pequena.add_middleware(CabecerasDeSeguridadMiddleware)

    @pequena.get("/x")
    def x():
        return Response("ok", headers=cabeceras_de_la_ruta)

    return TestClient(pequena)


def test_no_pisa_una_cabecera_fijada_por_la_ruta():
    r = _app_minima(**{"Cross-Origin-Resource-Policy": "cross-origin"}).get("/x")
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"
    assert r.headers["x-content-type-options"] == NOSNIFF


@pytest.mark.asyncio
async def test_los_ambitos_que_no_son_http_pasan_sin_tocarse():
    recibidos = []

    async def interna(scope, receive, send):
        recibidos.append(scope["type"])

    await CabecerasDeSeguridadMiddleware(interna)({"type": "lifespan"}, None, None)
    assert recibidos == ["lifespan"]

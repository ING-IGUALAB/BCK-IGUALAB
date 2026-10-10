"""HTTP + servicios + SQL real en SQLite aislado; no conecta a QA/Dev."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.dependencies import get_current_user
from app.exception_handlers import register_exception_handlers
from app.models import Empresa, RolUsuario
from app.request_id import RequestIDMiddleware
from app.routers.empresas import router


@pytest.fixture
def entorno():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Empresa.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        # Solo adapta la interfaz async. Las consultas y restricciones se ejecutan.
        db = SimpleNamespace(add=Mock(side_effect=session.add))
        for metodo in ("execute", "get", "commit", "rollback", "refresh"):
            setattr(db, metodo, AsyncMock(side_effect=getattr(session, metodo)))
        app = FastAPI()
        register_exception_handlers(app)
        app.add_middleware(RequestIDMiddleware)
        app.include_router(router)
        actor = SimpleNamespace(rol=RolUsuario.SUPERADMIN)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: actor
        with TestClient(app) as client:
            yield client, actor, app, session
    engine.dispose()


def crear(client, nombre="Minera del Sur", sector="MINERIA"):
    return client.post("/empresas", json={"nombre": nombre, "sector": sector})


def comprobar_error(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["request_id"] != "unavailable"


@pytest.mark.parametrize("sector", ["MINERIA", "PETROLEO", "ENERGIA"])
def test_registrar_y_consultar(entorno, sector):
    client, _, _, _ = entorno
    response = crear(client, "  Empresa Sur  ", sector)
    assert response.status_code == 201
    empresa = response.json()
    assert empresa["nombre"] == "Empresa Sur"
    assert empresa["sector"] == sector
    assert empresa["activa"] is True
    assert empresa["creada_en"]
    assert uuid.UUID(empresa["id"])
    assert client.get(f"/empresas/{empresa['id']}").json() == empresa


@pytest.mark.parametrize("payload", [
    {}, {"nombre": "Empresa"}, {"sector": "MINERIA"},
    {"nombre": "", "sector": "MINERIA"},
    {"nombre": " \t\n ", "sector": "MINERIA"},
    {"nombre": "a" * 201, "sector": "MINERIA"},
    {"nombre": "Empresa", "sector": "OTRO"},
    {"nombre": None, "sector": "MINERIA"},
    {"nombre": "Empresa", "sector": None},
    {"nombre": "Empresa", "sector": "MINERIA", "activa": False},
])
def test_creacion_invalida(entorno, payload):
    client, _, _, _ = entorno
    comprobar_error(client.post("/empresas", json=payload), 422, "REQUEST_VALIDATION_ERROR")
    assert client.get("/empresas").json() == []


def test_nombre_longitud_maxima(entorno):
    assert crear(entorno[0], "a" * 200).status_code == 201


@pytest.mark.parametrize("nombre", ["Minera del Sur", "MINERA DEL SUR", " minera del sur "])
def test_duplicados_incluso_inactivos(entorno, nombre):
    client, _, _, _ = entorno
    empresa = crear(client).json()
    client.patch(f"/empresas/{empresa['id']}/desactivar")
    comprobar_error(crear(client, nombre), 409, "COMPANY_ALREADY_EXISTS")
    assert len(client.get("/empresas").json()) == 1


def test_editar_nombre_y_sector_y_conservar_identidad(entorno):
    client, _, _, _ = entorno
    empresa = crear(client).json()
    url = f"/empresas/{empresa['id']}"
    response = client.patch(url, json={"nombre": empresa["nombre"], "sector": "ENERGIA"})
    assert response.status_code == 200
    assert response.json()["sector"] == "ENERGIA"
    response = client.patch(url, json={"nombre": "  Empresa Nueva  "})
    assert response.json()["nombre"] == "Empresa Nueva"
    assert response.json()["sector"] == "ENERGIA"
    assert response.json()["id"] == empresa["id"]
    assert response.json()["creada_en"] == empresa["creada_en"]
    assert client.patch(url, json={"sector": "PETROLEO"}).json()["sector"] == "PETROLEO"


def test_edicion_no_usa_nombre_de_otra_empresa(entorno):
    client, _, _, _ = entorno
    primera = crear(client).json()
    crear(client, "Segunda")
    url = f"/empresas/{primera['id']}"
    comprobar_error(client.patch(url, json={"nombre": " SEGUNDA "}), 409, "COMPANY_ALREADY_EXISTS")
    assert client.get(url).json()["nombre"] == primera["nombre"]


@pytest.mark.parametrize("payload", [
    {}, {"nombre": "  "}, {"nombre": "a" * 201}, {"nombre": None},
    {"sector": None}, {"sector": "AGRO"}, {"activa": False},
])
def test_edicion_invalida(entorno, payload):
    client, _, _, _ = entorno
    empresa = crear(client).json()
    comprobar_error(
        client.patch(f"/empresas/{empresa['id']}", json=payload), 422, "REQUEST_VALIDATION_ERROR"
    )


def test_desactivar_filtrar_y_reactivar_sin_borrar(entorno):
    client, actor, _, session = entorno
    empresa = crear(client).json()
    otra = crear(client, "Energia Norte", "ENERGIA").json()
    url = f"/empresas/{empresa['id']}"
    for _ in range(2):
        assert client.patch(url + "/desactivar").json()["activa"] is False
    assert session.get(Empresa, uuid.UUID(empresa["id"])) is not None
    assert len(client.get("/empresas").json()) == 2
    assert client.get("/empresas?solo_activas=true").json() == [otra]
    assert client.get("/empresas?solo_activas=true&sector=MINERIA").json() == []
    actor.rol = RolUsuario.ADMINISTRADOR
    assert client.get("/empresas?solo_activas=true").json() == [otra]
    actor.rol = RolUsuario.SUPERADMIN
    for _ in range(2):
        assert client.patch(url + "/activar").json()["activa"] is True
    assert len(client.get("/empresas?solo_activas=true").json()) == 2


RUTAS_PROTEGIDAS = [
    ("POST", "/empresas"), ("GET", "/empresas"),
    ("GET", "/empresas/{id}"), ("PATCH", "/empresas/{id}"),
    ("PATCH", "/empresas/{id}/activar"), ("PATCH", "/empresas/{id}/desactivar"),
]


@pytest.mark.parametrize("metodo,ruta", RUTAS_PROTEGIDAS)
def test_administrador_no_gestiona_catalogo(entorno, metodo, ruta):
    client, actor, _, _ = entorno
    actor.rol = RolUsuario.ADMINISTRADOR
    response = client.request(metodo, ruta.format(id=uuid.uuid4()), json={"nombre": "Empresa", "sector": "MINERIA"})
    comprobar_error(response, 403, "FORBIDDEN")


@pytest.mark.parametrize("metodo,ruta", RUTAS_PROTEGIDAS + [("GET", "/empresas?solo_activas=true")])
def test_sin_sesion_no_accede(entorno, metodo, ruta):
    client, _, app, _ = entorno
    del app.dependency_overrides[get_current_user]
    response = client.request(metodo, ruta.format(id=uuid.uuid4()), json={"nombre": "Empresa", "sector": "MINERIA"})
    comprobar_error(response, 401, "INVALID_SESSION")


@pytest.mark.parametrize("metodo,sufijo", [("GET", ""), ("PATCH", ""), ("PATCH", "/activar"), ("PATCH", "/desactivar")])
def test_empresa_inexistente(entorno, metodo, sufijo):
    response = entorno[0].request(metodo, f"/empresas/{uuid.uuid4()}{sufijo}", json={"nombre": "Empresa"})
    comprobar_error(response, 404, "COMPANY_NOT_FOUND")


def test_no_se_expone_eliminacion_y_filtros_invalidos(entorno):
    client = entorno[0]
    assert client.delete(f"/empresas/{uuid.uuid4()}").status_code == 405
    comprobar_error(client.get("/empresas?sector=OTRO"), 422, "REQUEST_VALIDATION_ERROR")
    comprobar_error(client.get("/empresas/no-es-uuid"), 422, "REQUEST_VALIDATION_ERROR")

"""HTTP + servicios + SQL real en SQLite aislado; no conecta a QA/Dev."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exception_handlers import register_exception_handlers
from app.models import Empresa, RolUsuario, SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import (
    Documento,
    EstadoOperacion,
    EstadoProcesamiento,
    OperacionIngesta,
    ResultadoAnalisis,
)
from app.core.request_id import RequestIDMiddleware
from app.modules.empresas.router import router


@pytest.fixture
def entorno():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # `documentos` y `operaciones_ingesta` existen para comprobar la regla «con documentos no cambia de sector».
    Base.metadata.create_all(engine, tables=[Empresa.__table__, Documento.__table__, OperacionIngesta.__table__])
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


# ============ Una empresa con documentos no puede cambiar de sector ============

MENSAJE_SECTOR_BLOQUEADO = "No se puede cambiar el sector de una empresa que ya tiene documentos asociados"
AHORA = datetime(2026, 10, 20, tzinfo=timezone.utc)

ESTADOS_DE_DOCUMENTO = {
    "en-proceso": {},
    "completado-observado": dict(
        estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ResultadoAnalisis.OBSERVADO,
        completado_en=AHORA, original_almacenado_en=AHORA,
    ),
    "completado-con-hallazgos": dict(
        estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS,
        completado_en=AHORA, original_almacenado_en=AHORA,
    ),
    "fallido": dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR", fallido_en=AHORA),
    "fallido-con-reserva-liberada": dict(
        estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR", fallido_en=AHORA, reserva_activa=False,
    ),
    "otro-ambiente-y-tipo": dict(ambiente="qa", tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI, anio=2019),
}


def poner_documento(session, empresa_id, sector="MINERIA", **cambios):
    valores = dict(
        ambiente="development", empresa_id=uuid.UUID(str(empresa_id)), anio=2024, tipo=TipoDocumento.MEMORIA_ANUAL,
        sector=SectorEmpresa(sector), nombre_archivo="memoria.md", sha256=uuid.uuid4().hex * 2, tamano_bytes=10,
        usuario_id=uuid.uuid4(), clave_original=f"development/documentos/{uuid.uuid4()}/original.md",
    )
    valores.update(cambios)
    documento = Documento(**valores)
    session.add(documento)
    session.commit()
    return documento


def estado_guardado(session, empresa_id):
    session.expire_all()
    empresa = session.get(Empresa, uuid.UUID(empresa_id))
    return empresa.nombre, empresa.sector, empresa.activa


@pytest.mark.parametrize("destino", ["PETROLEO", "ENERGIA", "MINERIA"])
def test_sin_documentos_el_sector_cambia_entre_los_valores_admitidos(entorno, destino):
    client, _, _, session = entorno
    empresa = crear(client, sector="PETROLEO" if destino == "MINERIA" else "MINERIA").json()
    response = client.patch(f"/empresas/{empresa['id']}", json={"sector": destino})
    assert response.status_code == 200, response.text
    assert response.json()["sector"] == destino
    assert estado_guardado(session, empresa["id"])[1] is SectorEmpresa(destino)


@pytest.mark.parametrize("estado", ESTADOS_DE_DOCUMENTO.values(), ids=ESTADOS_DE_DOCUMENTO.keys())
def test_con_documentos_en_cualquier_estado_un_sector_distinto_devuelve_409_y_no_modifica(entorno, estado):
    client, _, _, session = entorno
    empresa = crear(client).json()
    poner_documento(session, empresa["id"], **estado)
    antes = estado_guardado(session, empresa["id"])
    response = client.patch(f"/empresas/{empresa['id']}", json={"sector": "ENERGIA"})
    comprobar_error(response, 409, "COMPANY_SECTOR_CHANGE_BLOCKED")
    assert response.json()["error"]["message"] == MENSAJE_SECTOR_BLOQUEADO
    assert estado_guardado(session, empresa["id"]) == antes
    assert client.get(f"/empresas/{empresa['id']}").json()["sector"] == "MINERIA"


def test_con_documentos_mantener_el_mismo_sector_no_genera_el_conflicto(entorno):
    client, _, _, session = entorno
    empresa = crear(client).json()
    poner_documento(session, empresa["id"])
    url = f"/empresas/{empresa['id']}"
    assert client.patch(url, json={"sector": "MINERIA"}).status_code == 200
    response = client.patch(url, json={"nombre": "Nombre Nuevo", "sector": "MINERIA"})
    assert response.status_code == 200, response.text
    assert (response.json()["nombre"], response.json()["sector"]) == ("Nombre Nuevo", "MINERIA")


def test_con_documentos_el_cambio_de_nombre_sin_sector_conserva_su_comportamiento(entorno):
    client, _, _, session = entorno
    empresa = crear(client).json()
    otra = crear(client, "Otra Empresa").json()
    poner_documento(session, empresa["id"])
    url = f"/empresas/{empresa['id']}"
    response = client.patch(url, json={"nombre": "  Minera Renombrada  "})
    assert response.status_code == 200, response.text
    assert (response.json()["nombre"], response.json()["sector"]) == ("Minera Renombrada", "MINERIA")
    comprobar_error(client.patch(url, json={"nombre": otra["nombre"]}), 409, "COMPANY_ALREADY_EXISTS")  # sigue validándose
    assert client.patch(url + "/desactivar").json()["activa"] is False  # el estado no está bloqueado
    assert client.patch(url + "/activar").json()["activa"] is True


def test_una_peticion_mixta_con_sector_bloqueado_se_rechaza_entera(entorno):
    client, _, _, session = entorno
    empresa = crear(client).json()
    poner_documento(session, empresa["id"])
    antes = estado_guardado(session, empresa["id"])
    response = client.patch(f"/empresas/{empresa['id']}", json={"nombre": "Nombre Que No Debe Quedar", "sector": "PETROLEO"})
    comprobar_error(response, 409, "COMPANY_SECTOR_CHANGE_BLOCKED")
    assert estado_guardado(session, empresa["id"]) == antes  # ni el nombre ni el sector cambiaron


def test_con_sector_bloqueado_y_nombre_repetido_se_informa_el_sector_y_no_se_modifica_nada(entorno):
    client, _, _, session = entorno
    empresa = crear(client).json()
    otra = crear(client, "Otra Empresa").json()
    poner_documento(session, empresa["id"])
    antes = estado_guardado(session, empresa["id"])
    response = client.patch(f"/empresas/{empresa['id']}", json={"nombre": otra["nombre"], "sector": "PETROLEO"})
    comprobar_error(response, 409, "COMPANY_SECTOR_CHANGE_BLOCKED")
    assert estado_guardado(session, empresa["id"]) == antes


@pytest.mark.parametrize("estado", [EstadoOperacion.CREADA, EstadoOperacion.EN_CARGA, EstadoOperacion.RECHAZADA])
def test_una_operacion_de_carga_sin_documento_no_bloquea_el_cambio(entorno, estado):
    client, _, _, session = entorno
    empresa = crear(client).json()
    ahora = datetime.now(timezone.utc)
    con_carga = estado is not EstadoOperacion.CREADA
    session.add(OperacionIngesta(
        ambiente="development", usuario_id=uuid.uuid4(), estado=estado.value,
        carga_iniciada_en=ahora if con_carga else None,
        carga_vigente_hasta=ahora + timedelta(minutes=5) if con_carga else None,
        codigo_error="INVALID_DOCUMENT" if estado is EstadoOperacion.RECHAZADA else None,
    ))
    session.commit()
    response = client.patch(f"/empresas/{empresa['id']}", json={"sector": "ENERGIA"})
    assert response.status_code == 200, response.text
    assert response.json()["sector"] == "ENERGIA"


def test_los_documentos_de_otra_empresa_no_bloquean(entorno):
    client, _, _, session = entorno
    empresa = crear(client).json()
    otra = crear(client, "Otra Empresa").json()
    poner_documento(session, otra["id"])
    assert client.patch(f"/empresas/{empresa['id']}", json={"sector": "ENERGIA"}).status_code == 200
    comprobar_error(client.patch(f"/empresas/{otra['id']}", json={"sector": "ENERGIA"}), 409, "COMPANY_SECTOR_CHANGE_BLOCKED")


def test_los_permisos_no_cambian_con_documentos(entorno):
    client, actor, app, session = entorno
    empresa = crear(client).json()
    poner_documento(session, empresa["id"])
    url = f"/empresas/{empresa['id']}"
    actor.rol = RolUsuario.ADMINISTRADOR
    comprobar_error(client.patch(url, json={"sector": "ENERGIA"}), 403, "FORBIDDEN")  # el permiso se decide antes que el conflicto
    actor.rol = RolUsuario.SUPERADMIN
    del app.dependency_overrides[get_current_user]
    comprobar_error(client.patch(url, json={"sector": "ENERGIA"}), 401, "INVALID_SESSION")
    assert estado_guardado(session, empresa["id"])[1] is SectorEmpresa.MINERIA

"""HTTP focalizado: entradas que antes terminaban en 500 y ahora se rechazan con el contrato uniforme de errores.

Mecanismos reproducidos en local (PostgreSQL aislado) a raíz de los 500 del build 37 de OWASP ZAP:
  * contraseña con NUL, de más de 4096 caracteres o con un sustituto suelto  -> excepción de passlib/UnicodeEncodeError en el login;
  * nombre con NUL en empresas y usuarios -> asyncpg.CharacterNotInRepertoireError; nombre de usuario de más de 200 caracteres -> columna.

Usa la lógica REAL de servicios y esquemas sobre SQLite aislado (sin PostgreSQL, sin QA/UAT, sin proveedores). Los fallos de infraestructura
NO deben disfrazarse de credenciales incorrectas: tienen su propia prueba.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exception_handlers import ManejadorExcepcionesMiddleware, register_exception_handlers
from app.core.request_id import RequestIDMiddleware
from app.core.security import LONGITUD_MAXIMA_PASSWORD, hash_password, hash_token_recuperacion
from app.models import Empresa, RegistroAuditoria, RolUsuario, SectorEmpresa, Sesion, TokenRecuperacion, Usuario
from app.models.documento_ingesta import Documento, OperacionIngesta
from app.modules.auth.router import router as auth_router
from app.modules.empresas.router import router as empresas_router
from app.modules.usuarios.router import router as usuarios_router

CORREO = "cuenta-existente@igualab-prueba.org"
PASSWORD = "Clave-Valida-1!"
NUL = chr(0)
SUSTITUTO_JSON = b"\\ud800"  # secuencia de 6 caracteres tal como la enviaría un cliente en el JSON: un sustituto suelto
CABECERA_JSON = {"content-type": "application/json"}


@pytest.fixture
def entorno():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[t.__table__ for t in (Usuario, Sesion, TokenRecuperacion, RegistroAuditoria, Empresa, Documento, OperacionIngesta)],
    )
    # En PostgreSQL `uq_un_solo_superadmin` es un índice único PARCIAL (solo para el SuperAdmin); SQLite lo crearía sobre todo `rol` y
    # impediría una segunda cuenta Administrador. Se reproduce el índice parcial para que la prueba se parezca a la base real.
    with engine.begin() as conexion:
        conexion.exec_driver_sql("DROP INDEX IF EXISTS uq_un_solo_superadmin")
        conexion.exec_driver_sql("CREATE UNIQUE INDEX uq_un_solo_superadmin ON usuarios (rol) WHERE rol = 'SUPERADMIN'")
    with Session(engine, expire_on_commit=False) as sesion:
        db = SimpleNamespace(add=Mock(side_effect=sesion.add))
        for metodo in ("execute", "get", "commit", "rollback", "refresh", "flush"):
            setattr(db, metodo, AsyncMock(side_effect=getattr(sesion, metodo)))
        cuenta = Usuario(nombre="Cuenta", correo=CORREO, password_hash=hash_password(PASSWORD), rol=RolUsuario.ADMINISTRADOR, habilitado=True)
        superadmin = Usuario(nombre="Super", correo="super@igualab-prueba.org", password_hash=hash_password("Otra-Clave-9?"), rol=RolUsuario.SUPERADMIN, habilitado=True)
        sesion.add_all([cuenta, superadmin])
        sesion.commit()

        app = FastAPI()
        register_exception_handlers(app)
        app.add_middleware(RequestIDMiddleware)
        app.add_middleware(ManejadorExcepcionesMiddleware)  # como en la aplicación real: todo error inesperado sale como 500 uniforme
        for r in (auth_router, usuarios_router, empresas_router):
            app.include_router(r)
        actor = SimpleNamespace(usuario=superadmin)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: actor.usuario
        with TestClient(app, raise_server_exceptions=False) as cliente:
            yield SimpleNamespace(cliente=cliente, sesion=sesion, cuenta=cuenta, superadmin=superadmin, actor=actor, app=app)
    engine.dispose()


def error(respuesta, estado, codigo):
    """El contrato uniforme: estado esperado y cuerpo {error:{code,message,details,request_id}}. Los errores controlados además llevan la
    cabecera x-request-id coherente; el 500 uniforme solo trae el request_id en el cuerpo (el manejador de excepciones envuelve por fuera
    a RequestIDMiddleware: comportamiento previo, que el hook de ZAP tiene en cuenta leyéndolo del cuerpo)."""
    assert respuesta.status_code == estado, f"{respuesta.status_code} {respuesta.text[:300]}"
    cuerpo = respuesta.json()["error"]
    assert cuerpo["code"] == codigo
    assert set(cuerpo) == {"code", "message", "details", "request_id"}
    assert uuid.UUID(cuerpo["request_id"])
    if estado != 500:
        assert respuesta.headers["x-request-id"] == cuerpo["request_id"]
    return cuerpo


def login(e, correo, password=None, crudo=None):
    if crudo is not None:
        return e.cliente.post("/auth/login", content=crudo, headers=CABECERA_JSON)
    return e.cliente.post("/auth/login", json={"correo": correo, "password": password})


# --------------------------------------------------------------------------------------------------------------------- login
CONTRASENAS_NO_PROCESABLES = [
    pytest.param("abc" + NUL + "def", id="con-NUL"),
    pytest.param("A" * (LONGITUD_MAXIMA_PASSWORD + 1), id="4097-caracteres"),
    pytest.param("A" * 100_000, id="100000-caracteres"),
]


@pytest.mark.parametrize("password", CONTRASENAS_NO_PROCESABLES)
@pytest.mark.parametrize("correo", ["zap-escaner-sin-cuenta@igualab-zap.example.com", CORREO], ids=["cuenta-inexistente", "cuenta-existente"])
def test_login_con_contrasena_no_procesable_es_401_y_no_500(entorno, correo, password):
    error(login(entorno, correo, password), 401, "INVALID_CREDENTIALS")


@pytest.mark.parametrize("correo", ["zap-escaner-sin-cuenta@igualab-zap.example.com", CORREO], ids=["cuenta-inexistente", "cuenta-existente"])
def test_login_con_sustituto_suelto_es_401_y_no_500(entorno, correo):
    crudo = b'{"correo":"' + correo.encode() + b'","password":"' + SUSTITUTO_JSON + b'"}'
    error(login(entorno, correo, crudo=crudo), 401, "INVALID_CREDENTIALS")


def test_el_login_hostil_cuenta_como_intento_fallido_y_no_filtra_si_la_cuenta_existe(entorno):
    antes = entorno.cuenta.intentos_fallidos
    e_existente = error(login(entorno, CORREO, "abc" + NUL + "def"), 401, "INVALID_CREDENTIALS")
    assert entorno.cuenta.intentos_fallidos == antes + 1, "un intento hostil cuenta para el bloqueo, como cualquier credencial incorrecta"
    e_inexistente = error(login(entorno, "nadie@igualab-prueba.org", "abc" + NUL + "def"), 401, "INVALID_CREDENTIALS")
    assert e_existente["message"] == e_inexistente["message"], "la respuesta no distingue entre cuenta existente e inexistente"


def test_las_contrasenas_validas_existentes_siguen_funcionando(entorno):
    assert login(entorno, CORREO, PASSWORD).status_code == 200
    # Una contraseña larga (más de 72 bytes, bcrypt la trunca como siempre) creada antes de este cambio sigue entrando; y el tope exacto también.
    larga = "Aa1!" + "x" * 150
    entorno.cuenta.password_hash = hash_password(larga)
    entorno.sesion.commit()
    assert login(entorno, CORREO, larga).status_code == 200
    exacta = "Aa1!" + "y" * (LONGITUD_MAXIMA_PASSWORD - 4)
    entorno.cuenta.password_hash = hash_password(exacta)
    entorno.sesion.commit()
    assert login(entorno, CORREO, exacta).status_code == 200, "una contraseña de exactamente 4096 caracteres sigue siendo válida"
    error(login(entorno, CORREO, exacta + "z"), 401, "INVALID_CREDENTIALS")


def test_un_fallo_de_infraestructura_NO_se_disfraza_de_credenciales_incorrectas(entorno, monkeypatch):
    def verify_roto(*_args, **_kwargs):
        raise RuntimeError("fallo inesperado de la biblioteca de hash")

    monkeypatch.setattr(security._pwd_context, "verify", verify_roto)
    error(login(entorno, CORREO, PASSWORD), 500, "INTERNAL_ERROR")


def test_un_hash_corrupto_NO_se_disfraza_de_credenciales_incorrectas(entorno):
    entorno.cuenta.password_hash = "esto-no-es-un-hash"
    entorno.sesion.commit()
    error(login(entorno, CORREO, PASSWORD), 500, "INTERNAL_ERROR")


# ------------------------------------------------------------------------------------------------------- crear usuario
def crear_usuario(e, **campos):
    cuerpo = {"nombre": "Persona de Prueba", "correo": f"nueva-{uuid.uuid4().hex[:8]}@igualab-prueba.org", "password": "Clave-Nueva-1!", **campos}
    return e.cliente.post("/usuarios", json=cuerpo)


def test_crear_usuario_con_nombre_hostil_es_422(entorno):
    for nombre in ("Ana" + NUL + "B", "Ana\tB", "x" * 201):
        error(crear_usuario(entorno, nombre=nombre), 422, "REQUEST_VALIDATION_ERROR")


def test_crear_usuario_conserva_nombres_legitimos(entorno):
    for nombre in ("José Peña Ñandú", "O'Brien-Núñez", "x" * 200):
        r = crear_usuario(entorno, nombre=nombre)
        assert r.status_code == 201, r.text
        assert r.json()["nombre"] == nombre, "el nombre no se recorta ni se normaliza"


@pytest.mark.parametrize(
    ("password", "fragmento"),
    [
        ("Aa1!" + NUL + "valida", "nulos"),
        ("Aa1!" + "x" * LONGITUD_MAXIMA_PASSWORD, "no puede superar"),
    ],
    ids=["con-NUL", "mas-de-4096"],
)
def test_crear_usuario_con_contrasena_no_procesable_incumple_la_politica_y_no_es_500(entorno, password, fragmento):
    cuerpo = error(crear_usuario(entorno, password=password), 400, "PASSWORD_POLICY_VIOLATION")
    assert any(fragmento in m for m in cuerpo["details"]["errors"])


def test_crear_usuario_con_sustituto_suelto_en_la_contrasena_no_es_500(entorno):
    crudo = (b'{"nombre":"Ana","correo":"sust@igualab-prueba.org","password":"Aa1!' + SUSTITUTO_JSON + b'ab"}')
    r = entorno.cliente.post("/usuarios", content=crudo, headers=CABECERA_JSON)
    cuerpo = error(r, 400, "PASSWORD_POLICY_VIOLATION")
    assert any("no válidos" in m for m in cuerpo["details"]["errors"])


def test_una_contrasena_valida_sigue_creando_la_cuenta(entorno):
    assert crear_usuario(entorno).status_code == 201


# ------------------------------------------------------------------------------------------------- cambio de contraseña
def cambiar(e, actual, nueva):
    e.actor.usuario = e.cuenta
    return e.cliente.patch("/auth/mi-contrasena", json={"password_actual": actual, "password_nueva": nueva})


def test_cambio_con_contrasena_actual_no_procesable_es_401(entorno):
    for actual in ("x" + NUL + "y", "A" * (LONGITUD_MAXIMA_PASSWORD + 1)):
        error(cambiar(entorno, actual, "Nueva-Clave-2@"), 401, "CURRENT_PASSWORD_INVALID")


def test_cambio_con_contrasena_nueva_no_procesable_incumple_la_politica(entorno):
    for nueva in ("Aa1!" + NUL + "valida", "Aa1!" + "x" * LONGITUD_MAXIMA_PASSWORD):
        error(cambiar(entorno, PASSWORD, nueva), 400, "PASSWORD_POLICY_VIOLATION")
    assert login(entorno, CORREO, PASSWORD).status_code == 200, "la contraseña actual no cambió"


def test_cambio_valido_sigue_funcionando(entorno):
    assert cambiar(entorno, PASSWORD, "Nueva-Clave-2@").status_code == 200
    assert login(entorno, CORREO, "Nueva-Clave-2@").status_code == 200


# ------------------------------------------------------------------------------------------- restablecer contraseña (token)
@pytest.fixture
def token_vigente(entorno):
    token = security.generar_token_recuperacion()
    entorno.sesion.add(TokenRecuperacion(usuario_id=entorno.cuenta.id, token_hash=hash_token_recuperacion(token), expira_en=datetime.now(timezone.utc) + timedelta(minutes=30)))
    entorno.sesion.commit()
    return token


def restablecer(e, token, nueva):
    return e.cliente.post("/auth/restablecer-contrasena", json={"token": token, "password_nueva": nueva, "password_nueva_confirmacion": nueva})


@pytest.mark.parametrize("token", ["abc" + NUL + "def", "T" * (LONGITUD_MAXIMA_PASSWORD + 1)], ids=["con-NUL", "mas-de-4096"])
def test_restablecer_con_token_no_procesable_es_400_y_no_500(entorno, token_vigente, token):
    error(restablecer(entorno, token, "Nueva-Clave-3#"), 400, "INVALID_OR_EXPIRED_RESET_TOKEN")


def test_restablecer_con_contrasena_nueva_no_procesable_no_consume_el_token(entorno, token_vigente):
    error(restablecer(entorno, token_vigente, "Aa1!" + NUL + "valida"), 400, "PASSWORD_POLICY_VIOLATION")
    assert restablecer(entorno, token_vigente, "Nueva-Clave-3#").status_code == 200, "el error de política no consumió el token"


# -------------------------------------------------------------------------------------------------------------- empresas
def crear_empresa(e, nombre, sector="ENERGIA"):
    return e.cliente.post("/empresas", json={"nombre": nombre, "sector": sector})


NOMBRES_RECHAZADOS = [
    pytest.param("Empresa" + NUL + "X", id="NUL"),
    pytest.param(NUL, id="solo-NUL"),
    pytest.param("<script>alert(1);</script>", id="etiqueta-script"),
    pytest.param("Empresa <b>negrita</b>", id="etiquetas"),
    pytest.param("A > B", id="mayor-que"),
    pytest.param("A < B", id="menor-que"),
    pytest.param("Línea1\nLínea2", id="salto-de-linea"),
    pytest.param("Col1\tCol2", id="tabulador"),
    pytest.param("Eco\x07", id="campana-BEL"),
    pytest.param("Borra\x7f", id="DEL"),
    pytest.param("Ctrl\x85C1", id="control-C1"),
]


@pytest.mark.parametrize("nombre", NOMBRES_RECHAZADOS)
def test_crear_empresa_con_nombre_hostil_es_422(entorno, nombre):
    cuerpo = error(crear_empresa(entorno, nombre), 422, "REQUEST_VALIDATION_ERROR")
    assert cuerpo["details"][0]["location"] == ["body", "nombre"]
    assert nombre not in json.dumps(cuerpo), "la respuesta de error no refleja la entrada"


@pytest.mark.parametrize("nombre", NOMBRES_RECHAZADOS)
def test_editar_empresa_con_nombre_hostil_es_422_y_no_modifica_nada(entorno, nombre):
    creada = crear_empresa(entorno, "Minera del Sur").json()
    error(entorno.cliente.patch(f"/empresas/{creada['id']}", json={"nombre": nombre}), 422, "REQUEST_VALIDATION_ERROR")
    assert entorno.cliente.get(f"/empresas/{creada['id']}").json()["nombre"] == "Minera del Sur"


@pytest.mark.parametrize(
    "nombre",
    ["Compañía Ñandú & Hijos S.A.", "O'Brien (Perú), Inc.", "Café-Ñ 2024", "Petróleos del Norte S.A.C.", "Mina “La Esperanza” — Lima", "n" * 200, "Empresa 日本"],
)
def test_los_nombres_legitimos_se_conservan_tal_cual(entorno, nombre):
    creada = crear_empresa(entorno, nombre)
    assert creada.status_code == 201, creada.text
    assert creada.json()["nombre"] == nombre
    editada = entorno.cliente.patch(f"/empresas/{creada.json()['id']}", json={"nombre": nombre + "."} if len(nombre) < 200 else {"sector": "MINERIA"})
    assert editada.status_code == 200, editada.text


def test_no_se_alteran_los_nombres_ya_almacenados(entorno):
    # Un nombre previo con etiquetas (anterior a la validación): se lee, se lista y se sigue pudiendo editar el sector sin tocarlo.
    previa = Empresa(nombre="<b>Heredada</b> & Cía", sector=SectorEmpresa.MINERIA, activa=True)
    entorno.sesion.add(previa)
    entorno.sesion.commit()
    assert entorno.cliente.get(f"/empresas/{previa.id}").json()["nombre"] == "<b>Heredada</b> & Cía"
    assert "<b>Heredada</b> & Cía" in [x["nombre"] for x in entorno.cliente.get("/empresas").json()]
    cambio = entorno.cliente.patch(f"/empresas/{previa.id}", json={"sector": "PETROLEO"})
    assert cambio.status_code == 200 and cambio.json()["nombre"] == "<b>Heredada</b> & Cía"
    assert entorno.sesion.scalar(select(Empresa.nombre).where(Empresa.id == previa.id)) == "<b>Heredada</b> & Cía"


def test_un_nombre_invalido_no_llega_a_la_base_de_datos(entorno):
    entorno.sesion.expire_all()
    antes = entorno.sesion.query(Empresa).count()
    crear_empresa(entorno, "Mala" + NUL + "Empresa")
    assert entorno.sesion.query(Empresa).count() == antes

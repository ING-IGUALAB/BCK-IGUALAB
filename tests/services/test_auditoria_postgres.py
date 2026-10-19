"""Auditoría (Módulo 8) y eventos de rechazo sobre un PostgreSQL REAL, AISLADO y TEMPORAL (`conftest.py`).

LTX:RN-042 (eventos), RN-043 (cuenta, UTC, tipo y RESULTADO), RN-044 / RNF-022 (solo inserción), RN-045 y CU007 (acceso denegado a
la auditoría), más el inicio de sesión fallido. REAL: la migración 0005 sobre una base con la forma ANTERIOR de `auditoria`, los
disparadores, las transacciones (los eventos de rechazo sobreviven al rollback del request), los roles por HTTP y los filtros de
la consulta. Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exception_handlers import register_exception_handlers
from app.core.request_id import RequestIDMiddleware
from app.core.security import hash_password
from app.migraciones import motor
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.motor import migrar
from app.models import RolUsuario, TipoEventoAuditoria, Usuario
from app.modules.auditoria import router as auditoria
from app.modules.auth import router as auth
from tests.ayudantes_analisis_gri import (
    asegurar_esquema_auditoria,
    cliente_http,
    comprobar_error,
    crear_administrador,
    crear_superadmin,
)
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar

CLAVE = "ClaveSegura1!"

TIPO_ANTERIOR = (
    "CREATE TYPE tipoeventoauditoria AS ENUM ('INICIO_SESION', 'CAMBIO_ROL', 'INGESTA_DATOS', 'RECHAZO_DOCUMENTO', "
    "'GENERACION_REPORTE', 'DESCARGA');"
)
TABLA_ANTERIOR = (
    "CREATE TABLE auditoria (id UUID NOT NULL PRIMARY KEY, usuario_id UUID REFERENCES usuarios (id), "
    "tipo_evento tipoeventoauditoria NOT NULL, detalle VARCHAR(500) NOT NULL, fecha_hora_utc TIMESTAMPTZ NOT NULL DEFAULT now());"
)


# ================================================ Migración 0005 sobre bases existentes ================================================

async def base_con_auditoria_anterior(url_pg_aislado: str) -> str:
    """Base como la desplegada ANTES de esta versión: `auditoria` sin `resultado` ni el valor `ACCESO_DENEGADO`, con eventos."""
    url = await base_transaccional_nueva(url_pg_aislado)
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    await ejecutar(url, "DROP TABLE auditoria; DROP TYPE tipoeventoauditoria;" + TIPO_ANTERIOR + TABLA_ANTERIOR)
    await ejecutar(
        url,
        "INSERT INTO auditoria (id, usuario_id, tipo_evento, detalle) VALUES ($1, $2, 'INICIO_SESION', 'Login exitoso (superadmin)')",
        uuid.uuid4(), usuario,
    )
    return url


async def firma(url: str, tabla: str) -> dict:
    conexion = await conectar(url)
    try:
        return await motor.firma_de_objeto(conexion, tabla)
    finally:
        await conexion.close()


async def test_la_migracion_actualiza_una_base_existente_sin_inventar_resultados_historicos(url_pg_aislado):
    url = await base_con_auditoria_anterior(url_pg_aislado)
    antes = [tuple(f) for f in await consultar(url, "SELECT id, usuario_id, tipo_evento::text, detalle, fecha_hora_utc FROM auditoria")]

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert resultado.aplicadas == (1, 2, 3, 4, 5)
    despues = [tuple(f) for f in await consultar(url, "SELECT id, usuario_id, tipo_evento::text, detalle, fecha_hora_utc FROM auditoria")]
    assert despues == antes  # los eventos existentes se conservan intactos
    assert (await consultar(url, "SELECT resultado FROM auditoria"))[0]["resultado"] is None  # no se deduce ni se inventa
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios  # el segundo arranque no repite nada
    # el nuevo valor del tipo enumerado ya se puede usar y el resultado queda acotado por su restricción
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    insertar = "INSERT INTO auditoria (id, usuario_id, tipo_evento, detalle, resultado) VALUES ($1, $2, $3::tipoeventoauditoria, 'x', $4)"
    await ejecutar(url, insertar, uuid.uuid4(), usuario, "ACCESO_DENEGADO", "DENEGADO")
    with pytest.raises(asyncpg.CheckViolationError):
        await ejecutar(url, insertar, uuid.uuid4(), usuario, "ACCESO_DENEGADO", "TALVEZ")


async def test_una_instalacion_nueva_y_una_actualizacion_dejan_la_misma_auditoria(url_pg_aislado):
    nueva = await base_transaccional_nueva(url_pg_aislado)  # create_all con los modelos actuales, como el arranque
    await migrar(nueva, ESQUEMA_TRANSACCIONAL)
    actualizada = await base_con_auditoria_anterior(url_pg_aislado)
    await migrar(actualizada, ESQUEMA_TRANSACCIONAL)
    assert await firma(nueva, "auditoria") == await firma(actualizada, "auditoria")
    for url in (nueva, actualizada):  # y en ambas existen los disparadores
        triggers = {f["tgname"] for f in await consultar(url, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")}
        assert {"tr_auditoria_solo_insercion", "tr_reportes_solo_insercion"} <= triggers


async def test_la_migracion_0005_es_idempotente_si_se_reaplica(url_pg_aislado):
    url = await base_con_auditoria_anterior(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    await ejecutar(url, ESQUEMA_TRANSACCIONAL.migraciones[4].sql())  # reaplicada a mano: no falla ni duplica
    assert len(await consultar(url, "SELECT 1 FROM pg_trigger WHERE tgname = 'tr_auditoria_solo_insercion'")) == 1


# ================================================ Mundo para HTTP ================================================

@pytest_asyncio.fixture
async def m8(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        activo = Usuario(nombre="Cuenta Activa", correo="activa@pruebas.example.com", password_hash=hash_password(CLAVE),
                         rol=RolUsuario.ADMINISTRADOR, habilitado=True)
        deshabilitado = Usuario(nombre="Cuenta Baja", correo="baja@pruebas.example.com", password_hash=hash_password(CLAVE),
                                rol=RolUsuario.ADMINISTRADOR, habilitado=False)
        db.add_all([activo, deshabilitado])
        await db.commit()
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, superadmin=superadmin, activo=activo, deshabilitado=deshabilitado)


def construir_app(m8, usuario=None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(auth.router)
    app.include_router(auditoria.router)

    async def db_de_prueba():
        async with m8.fabrica() as sesion:
            yield sesion  # al salir con error la sesión se cierra y revierte, como `get_db`

    app.dependency_overrides[get_db] = db_de_prueba
    if usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    return app


async def eventos(m8, **filtros):
    condicion = " AND ".join(f"{k} = :{k}" for k in filtros) or "TRUE"
    return (await _sql(m8, f"SELECT tipo_evento::text AS tipo, usuario_id, resultado, detalle FROM auditoria WHERE {condicion} ORDER BY fecha_hora_utc", **filtros))


async def _sql(m8, consulta: str, **parametros):
    async with m8.fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


async def login(cliente, correo: str, clave: str):
    return await cliente.post("/auth/login", json={"correo": correo, "password": clave})


# ================================================ Inicio de sesión (RN-042/043) ================================================

async def test_login_exitoso_registra_el_evento_con_resultado_exitoso(m8):
    async with cliente_http(construir_app(m8)) as c:
        assert (await login(c, "activa@pruebas.example.com", CLAVE)).status_code == 200
    (e,) = await eventos(m8, tipo_evento="INICIO_SESION")
    assert (e.usuario_id, e.resultado) == (m8.activo.id, "EXITOSO")


async def test_login_con_correo_inexistente_deja_un_evento_anonimo_fallido_sin_el_correo_ni_la_clave(m8):
    async with cliente_http(construir_app(m8)) as c:
        r = await login(c, "nadie@pruebas.example.com", "ClaveEscrita9!")
    comprobar_error(r, 401, "INVALID_CREDENTIALS")
    (e,) = await eventos(m8, tipo_evento="INICIO_SESION")
    assert e.usuario_id is None  # no se atribuye a ninguna cuenta
    assert e.resultado == "FALLIDO"
    assert "nadie@pruebas.example.com" not in e.detalle
    assert "ClaveEscrita9!" not in e.detalle


async def test_login_con_clave_incorrecta_registra_un_evento_por_intento_atribuido_a_la_cuenta_destinataria(m8):
    async with cliente_http(construir_app(m8)) as c:
        for _ in range(2):
            comprobar_error(await login(c, "activa@pruebas.example.com", "Incorrecta1!"), 401, "INVALID_CREDENTIALS")
    filas = await eventos(m8, tipo_evento="INICIO_SESION")
    assert [(f.usuario_id, f.resultado) for f in filas] == [(m8.activo.id, "FALLIDO")] * 2
    assert all("Incorrecta1!" not in f.detalle for f in filas)
    assert (await _sql(m8, "SELECT intentos_fallidos FROM usuarios WHERE correo = 'activa@pruebas.example.com'"))[0][0] == 2


async def test_el_quinto_intento_bloquea_con_un_solo_evento_y_el_intento_bloqueado_queda_denegado(m8):
    async with cliente_http(construir_app(m8)) as c:
        for _ in range(5):
            comprobar_error(await login(c, "activa@pruebas.example.com", "Incorrecta1!"), 401, "INVALID_CREDENTIALS")
        comprobar_error(await login(c, "activa@pruebas.example.com", CLAVE), 423, "ACCOUNT_LOCKED")  # la clave buena tampoco entra
    filas = await eventos(m8, tipo_evento="INICIO_SESION")
    assert len(filas) == 6  # 5 intentos (uno por intento, el quinto es el que bloquea) + 1 intento contra la cuenta bloqueada
    assert [f.resultado for f in filas] == ["FALLIDO"] * 5 + ["DENEGADO"]
    assert "cuenta bloqueada tras 5 intentos" in filas[4].detalle
    assert "bloqueada" not in filas[3].detalle


async def test_login_de_cuenta_deshabilitada_queda_denegado(m8):
    async with cliente_http(construir_app(m8)) as c:
        comprobar_error(await login(c, "baja@pruebas.example.com", CLAVE), 403, "ACCOUNT_DISABLED")
    (e,) = await eventos(m8, tipo_evento="INICIO_SESION")
    assert (e.usuario_id, e.resultado) == (m8.deshabilitado.id, "DENEGADO")
    assert "deshabilitada" in e.detalle


# ================================================ Acceso denegado a la auditoría (RN-045) ================================================

async def test_un_administrador_que_intenta_consultar_la_auditoria_recibe_403_y_deja_su_intento_registrado(m8):
    async with cliente_http(construir_app(m8, m8.admin)) as c:
        comprobar_error(await c.get("/auditoria"), 403, "FORBIDDEN")
        comprobar_error(await c.get("/auditoria"), 403, "FORBIDDEN")
    filas = await eventos(m8, tipo_evento="ACCESO_DENEGADO")
    assert [(f.usuario_id, f.resultado) for f in filas] == [(m8.admin.id, "DENEGADO")] * 2  # uno por intento, sin duplicados
    assert "rol=administrador" in filas[0].detalle


async def test_el_superadmin_consulta_sin_dejar_eventos_de_denegacion_y_sin_sesion_es_401_sin_evento(m8):
    async with cliente_http(construir_app(m8, m8.superadmin)) as c:
        r = await c.get("/auditoria")
    assert r.status_code == 200
    async with cliente_http(construir_app(m8, None)) as sin_sesion:
        comprobar_error(await sin_sesion.get("/auditoria"), 401, "INVALID_SESSION")
    assert await eventos(m8, tipo_evento="ACCESO_DENEGADO") == []


async def test_la_consulta_expone_el_resultado_y_filtra_por_tipo_usuario_y_fecha_en_postgres(m8):
    async with cliente_http(construir_app(m8)) as c:
        await login(c, "activa@pruebas.example.com", "Incorrecta1!")
        await login(c, "activa@pruebas.example.com", CLAVE)
    async with cliente_http(construir_app(m8, m8.admin)) as c:
        await c.get("/auditoria")  # denegado
    async with cliente_http(construir_app(m8, m8.superadmin)) as c:
        todo = (await c.get("/auditoria")).json()
        assert todo["total"] == 3
        assert {i["resultado"] for i in todo["items"]} == {"FALLIDO", "EXITOSO", "DENEGADO"}
        solo_denegados = (await c.get("/auditoria", params={"tipo_evento": "Acceso denegado"})).json()
        assert [i["usuario_id"] for i in solo_denegados["items"]] == [str(m8.admin.id)]
        del_usuario = (await c.get("/auditoria", params={"usuario_id": str(m8.activo.id)})).json()
        assert del_usuario["total"] == 2
        futuro = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        assert (await c.get("/auditoria", params={"desde": futuro})).json()["total"] == 0


# ================================================ Solo inserción (RN-044, RNF-022) ================================================

@pytest.mark.parametrize("sentencia", [
    "UPDATE auditoria SET detalle = 'alterado'",
    "UPDATE auditoria SET resultado = 'EXITOSO'",
    "DELETE FROM auditoria",
])
async def test_la_base_rechaza_modificar_o_eliminar_eventos_de_auditoria(m8, sentencia):
    async with cliente_http(construir_app(m8)) as c:
        await login(c, "activa@pruebas.example.com", CLAVE)
    antes = [tuple(f) for f in await _sql(m8, "SELECT id, detalle, resultado FROM auditoria")]
    assert antes
    async with m8.fabrica() as db:
        with pytest.raises(DBAPIError) as e:
            await db.execute(text(sentencia))
        assert "solo inserción" in str(e.value.orig)
    assert [tuple(f) for f in await _sql(m8, "SELECT id, detalle, resultado FROM auditoria")] == antes


async def test_el_tipo_de_evento_acceso_denegado_existe_en_el_modelo_y_en_la_base(m8):
    assert TipoEventoAuditoria.ACCESO_DENEGADO.value == "Acceso denegado"
    valores = {f[0] for f in await _sql(m8, "SELECT unnest(enum_range(NULL::tipoeventoauditoria))::text")}
    assert "ACCESO_DENEGADO" in valores

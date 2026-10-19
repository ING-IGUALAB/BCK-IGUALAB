"""Compatibilidad del esquema central (migración 0005) con el arranque y el inicio de sesión, sobre un PostgreSQL REAL, AISLADO y
TEMPORAL (`conftest.py`). Nunca usa `DATABASE_URL` ni una base compartida.

Cubre instalación nueva, actualización con datos, segundo arranque, rollback, fallo de la migración (permisos del rol y error a
mitad del SQL) y el `lifespan` REAL de `app.main` abortando cuando el esquema es incompatible. En el `lifespan` solo se sustituyen
`crear_tablas`, `crear_superadmin_inicial` e `iniciar_ingesta`/`detener_ingesta` (no hay OCI, MinIO ni base vectorial aquí); la
preparación del esquema y la comprobación de compatibilidad corren sin dobles. Si PostgreSQL no está disponible se OMITEN.
"""
from __future__ import annotations

import dataclasses
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.exceptions import AuthenticationError
from app.core.security import hash_password
from app.migraciones import compatibilidad
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.compatibilidad import CODIGO, elementos_faltantes, verificar_esquema_central
from app.migraciones.motor import ErrorMigracion, migrar
from app.models import RolUsuario, Sesion, Usuario
from app.modules.auth.service import autenticar
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar, migracion_de_texto, versiones_registradas
from tests.services.test_auditoria_postgres import base_con_auditoria_anterior

CLAVE = "ClaveSegura1!"
CORREO = "acceso@pruebas.example.com"
TRIGGERS = {"disparador:auditoria.tr_auditoria_solo_insercion", "disparador:reportes_prospeccion.tr_reportes_solo_insercion"}


# ================================================ Ayudantes ================================================

@asynccontextmanager
async def sesion_en(url: str):
    motor = create_async_engine(url)
    try:
        async with async_sessionmaker(motor, expire_on_commit=False)() as db:
            yield db
    finally:
        await motor.dispose()


async def preparar_usuario(url: str) -> uuid.UUID:
    """La tabla de sesiones (la usa el login) y una cuenta con contraseña real en la base desechable."""
    motor = create_async_engine(url)
    try:
        async with motor.begin() as conexion:
            await conexion.execute(CreateTable(Sesion.__table__))  # solo esta tabla: create_all(tables=...) crearía también los tipos de `documentos`
    finally:
        await motor.dispose()
    async with sesion_en(url) as db:
        usuario = Usuario(nombre="Acceso", correo=CORREO, password_hash=hash_password(CLAVE), rol=RolUsuario.ADMINISTRADOR, habilitado=True)
        db.add(usuario)
        await db.commit()
        return usuario.id


async def iniciar_sesion(url: str, password: str = CLAVE):
    async with sesion_en(url) as db:
        return await autenticar(db, CORREO, password)


async def faltantes(url: str) -> list[str]:
    conexion = await conectar(url)
    try:
        return await elementos_faltantes(conexion)
    finally:
        await conexion.close()


async def eventos(url: str) -> list[tuple]:
    filas = await consultar(url, "SELECT tipo_evento::text AS t, resultado, usuario_id, detalle FROM auditoria ORDER BY fecha_hora_utc, id")
    return [(f["t"], f["resultado"], f["usuario_id"], f["detalle"]) for f in filas]


def esquema_hasta(version: int):
    return dataclasses.replace(ESQUEMA_TRANSACCIONAL, migraciones=ESQUEMA_TRANSACCIONAL.migraciones[:version])


# ================================================ Instalación nueva ================================================

async def test_instalacion_nueva_create_all_deja_columna_y_tipo_pero_no_los_disparadores_hasta_migrar(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)  # create_all con los modelos actuales, como el arranque
    assert set(await faltantes(url)) == TRIGGERS  # la columna y el tipo ya vienen de los modelos; los disparadores no
    with pytest.raises(ErrorMigracion) as capturado:
        await verificar_esquema_central(url)
    assert capturado.value.codigo == CODIGO
    assert set(capturado.value.detalles["faltan"]) == TRIGGERS

    await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert await faltantes(url) == []
    await verificar_esquema_central(url)


async def test_con_el_esquema_al_dia_el_inicio_de_sesion_escribe_su_evento_de_auditoria(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    usuario_id = await preparar_usuario(url)
    await migrar(url, ESQUEMA_TRANSACCIONAL)

    token, usuario = await iniciar_sesion(url)
    assert token
    assert usuario.id == usuario_id
    with pytest.raises(AuthenticationError):
        await iniciar_sesion(url, "OtraClave1!")

    registrados = await eventos(url)
    assert [(t, r, u) for t, r, u, _ in registrados] == [
        ("INICIO_SESION", "EXITOSO", usuario_id),
        ("INICIO_SESION", "FALLIDO", usuario_id),
    ]


# ================================================ Actualización con datos ================================================

async def test_actualizacion_con_datos_rechaza_el_esquema_anterior_y_tras_migrar_conserva_los_eventos(url_pg_aislado):
    url = await base_con_auditoria_anterior(url_pg_aislado)  # `auditoria` sin `resultado`, con un evento
    await preparar_usuario(url)
    antes = await consultar(url, "SELECT id, usuario_id, tipo_evento::text AS t, detalle, fecha_hora_utc FROM auditoria")

    # Sin migrar, la comprobación lo dice y el inicio de sesión (que inserta `resultado`) FALLA: esto es lo que evita el arranque.
    detectados = set(await faltantes(url))
    assert "columna:auditoria.resultado" in detectados
    assert "valor_de_tipo:tipoeventoauditoria.ACCESO_DENEGADO" in detectados
    assert "restriccion:auditoria.ck_auditoria_resultado_valido" in detectados
    assert TRIGGERS <= detectados
    with pytest.raises(DBAPIError):
        await iniciar_sesion(url)
    assert await consultar(url, "SELECT 1 FROM sesiones") == []  # el intento no dejó una sesión a medias

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert resultado.aplicadas == (1, 2, 3, 4, 5)
    assert await faltantes(url) == []
    despues = await consultar(url, "SELECT id, usuario_id, tipo_evento::text AS t, detalle, fecha_hora_utc FROM auditoria WHERE resultado IS NULL")
    assert [tuple(f) for f in despues] == [tuple(f) for f in antes]  # el evento histórico sigue intacto y sin resultado inventado
    await iniciar_sesion(url)
    assert [(t, r) for t, r, _, _ in await eventos(url)][-1] == ("INICIO_SESION", "EXITOSO")


async def test_segundo_arranque_no_aplica_nada_y_la_comprobacion_sigue_pasando(url_pg_aislado):
    url = await base_con_auditoria_anterior(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)

    segundo = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert segundo.sin_cambios
    assert segundo.previas == (1, 2, 3, 4, 5)
    await verificar_esquema_central(url)


# ================================================ Por qué hace falta: la migración no declara objetos ================================================

async def test_el_registro_dice_aplicada_pero_la_comprobacion_detecta_que_el_efecto_ya_no_esta(url_pg_aislado):
    """Una migración sin `objetos` no se vuelve a verificar: tras registrarse, el motor la da por vigente. Aquí la columna
    desaparece (restauración de una copia anterior, DDL manual): el motor no lo ve, la comprobación de arranque sí."""
    url = await base_con_auditoria_anterior(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    await ejecutar(url, "ALTER TABLE auditoria DROP CONSTRAINT ck_auditoria_resultado_valido; ALTER TABLE auditoria DROP COLUMN resultado")

    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios  # el motor no repara ni se entera
    assert 5 in [v for v, _ in await versiones_registradas(url)]
    with pytest.raises(ErrorMigracion) as capturado:
        await verificar_esquema_central(url)
    assert capturado.value.codigo == CODIGO
    assert "columna:auditoria.resultado" in capturado.value.detalles["faltan"]


@pytest.mark.parametrize("tabla,disparador", [
    ("auditoria", "tr_auditoria_solo_insercion"),
    ("reportes_prospeccion", "tr_reportes_solo_insercion"),
])
async def test_un_disparador_de_solo_insercion_desactivado_o_ausente_impide_el_arranque(url_pg_aislado, tabla, disparador):
    url = await base_con_auditoria_anterior(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)

    await ejecutar(url, f"ALTER TABLE {tabla} DISABLE TRIGGER {disparador}")
    assert await faltantes(url) == [f"disparador_desactivado:{tabla}.{disparador}"]

    await ejecutar(url, f"ALTER TABLE {tabla} ENABLE TRIGGER {disparador}")
    assert await faltantes(url) == []

    await ejecutar(url, f"DROP TRIGGER {disparador} ON {tabla}")
    assert await faltantes(url) == [f"disparador:{tabla}.{disparador}"]


async def test_un_valor_del_tipo_de_evento_que_el_codigo_escribe_y_la_base_no_tiene_se_detecta(url_pg_aislado):
    """Se comprueban TODOS los miembros de `TipoEventoAuditoria`, no solo el añadido por la 0005."""
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    await ejecutar(url, "ALTER TYPE tipoeventoauditoria RENAME VALUE 'DESCARGA' TO 'DESCARGA_ANTIGUA'")
    assert await faltantes(url) == ["valor_de_tipo:tipoeventoauditoria.DESCARGA"]


# ================================================ Rollback y fallo de la migración ================================================

async def test_si_la_migracion_falla_a_mitad_se_deshace_por_completo_y_no_se_registra(url_pg_aislado, tmp_path):
    url = await base_con_auditoria_anterior(url_pg_aislado)
    await preparar_usuario(url)
    await migrar(url, esquema_hasta(4))
    sql_real = ESQUEMA_TRANSACCIONAL.migraciones[4].sql()
    rota = migracion_de_texto(tmp_path, 5, "auditoria_resultado_y_solo_insercion", sql_real + "\nSELECT 1 / 0;\n", ())
    esquema_roto = dataclasses.replace(ESQUEMA_TRANSACCIONAL, migraciones=(*ESQUEMA_TRANSACCIONAL.migraciones[:4], rota))

    with pytest.raises(ErrorMigracion) as capturado:
        await migrar(url, esquema_roto)

    assert capturado.value.codigo == "MIGRATION_FAILED"
    assert capturado.value.detalles["sqlstate"] == "22012"  # división por cero, tras ejecutar TODO el SQL real
    assert "se deshizo por completo" in capturado.value.mensaje
    assert 5 not in [v for v, _ in await versiones_registradas(url)]
    # Nada quedó a medias: ni la columna, ni el valor del tipo (se añade dentro de la misma transacción), ni los disparadores.
    assert set(await faltantes(url)) >= {"columna:auditoria.resultado", "valor_de_tipo:tipoeventoauditoria.ACCESO_DENEGADO"} | TRIGGERS
    assert await consultar(url, "SELECT 1 FROM pg_attribute WHERE attrelid = 'auditoria'::regclass AND attname = 'resultado'") == []
    with pytest.raises(ErrorMigracion):
        await verificar_esquema_central(url)
    with pytest.raises(DBAPIError):
        await iniciar_sesion(url)  # y por eso el arranque no debe anunciar disponibilidad

    # Corregida la causa, el siguiente arranque la aplica y todo queda en regla.
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).aplicadas == (5,)
    await verificar_esquema_central(url)
    await iniciar_sesion(url)


async def crear_rol_sin_ser_dueno(url: str) -> str:
    """Rol de la aplicación que puede leer y escribir pero NO es dueño de las tablas ni del tipo (caso típico de permisos)."""
    rol = f"app_{uuid.uuid4().hex[:8]}"
    await ejecutar(
        url,
        f'CREATE ROLE {rol} LOGIN; GRANT USAGE, CREATE ON SCHEMA public TO {rol}; '
        f'GRANT ALL ON ALL TABLES IN SCHEMA public TO {rol}; GRANT USAGE ON TYPE tipoeventoauditoria TO {rol};',
    )
    return url.replace("//pruebas@", f"//{rol}@")


async def test_con_un_rol_sin_permisos_la_migracion_falla_y_el_lifespan_real_no_arranca(url_pg_aislado, monkeypatch):
    from app import main

    url = await base_con_auditoria_anterior(url_pg_aislado)
    await preparar_usuario(url)
    await migrar(url, esquema_hasta(4))  # 1..4 ya aplicadas por quien administra la base
    url_app = await crear_rol_sin_ser_dueno(url)
    monkeypatch.setattr("app.core.config.settings.DATABASE_URL", url_app)
    for nombre in ("crear_tablas", "crear_superadmin_inicial", "iniciar_ingesta", "detener_ingesta"):
        monkeypatch.setattr(main, nombre, AsyncMock())

    # La preparación del módulo GRI sola degradaría a un 503 de /analisis-gri...
    with pytest.raises(ErrorMigracion) as capturado:
        await migrar(url_app, ESQUEMA_TRANSACCIONAL)
    assert capturado.value.codigo == "MIGRATION_FAILED"
    assert capturado.value.detalles["sqlstate"] == "42501"  # permiso insuficiente

    # ...pero el arranque real NO continúa: lanza en lugar de servir /health con el login roto.
    with pytest.raises(ErrorMigracion) as arranque:
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("la aplicación anunció disponibilidad con el esquema central incompatible")
    assert arranque.value.codigo == CODIGO
    assert "columna:auditoria.resultado" in arranque.value.detalles["faltan"]
    assert main.app.state.analisis_error is not None  # la causa también quedó visible para el módulo GRI
    main.detener_ingesta.assert_awaited()  # y se liberaron los recursos de ingesta antes de abortar
    assert "postgresql" not in str(arranque.value).lower()  # sin URL ni credenciales


async def test_el_lifespan_real_arranca_con_el_esquema_en_regla_y_sirve_el_health(url_pg_aislado, monkeypatch):
    import httpx

    from app import main

    url = await base_con_auditoria_anterior(url_pg_aislado)
    await preparar_usuario(url)
    monkeypatch.setattr("app.core.config.settings.DATABASE_URL", url)
    for nombre in ("crear_tablas", "crear_superadmin_inicial", "iniciar_ingesta", "detener_ingesta"):
        monkeypatch.setattr(main, nombre, AsyncMock())

    for _ in range(2):  # primer arranque (aplica la 0005) y segundo arranque (sin cambios)
        async with main.app.router.lifespan_context(main.app):
            assert main.app.state.analisis_error is None
            transporte = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transporte, base_url="http://prueba") as cliente:
                assert (await cliente.get("/health")).json() == {"status": "ok"}
    assert [v for v, _ in await versiones_registradas(url)] == [1, 2, 3, 4, 5]
    await iniciar_sesion(url)


async def test_si_la_comprobacion_no_puede_consultar_la_base_el_arranque_tambien_falla_sin_mostrar_la_url(url_pg_aislado, monkeypatch):
    url_inexistente = url_pg_aislado.rsplit("/", 1)[0] + "/base_que_no_existe_" + uuid.uuid4().hex[:6]
    monkeypatch.setattr("app.core.config.settings.DATABASE_URL", url_inexistente)
    with pytest.raises(ErrorMigracion) as capturado:
        await compatibilidad.exigir_esquema_central()
    assert capturado.value.codigo == "MIGRATION_DB_UNAVAILABLE"
    assert "127.0.0.1" not in str(capturado.value)
    assert "pruebas" not in str(capturado.value)

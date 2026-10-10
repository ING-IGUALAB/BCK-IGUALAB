"""SQL de `operaciones_ingesta` (`db/transaccional/003_operaciones_ingesta.sql`) EJECUTADO tal cual en un PostgreSQL REAL,
AISLADO y TEMPORAL (clúster de `conftest.py`), y garantías del servicio de operaciones. Si PostgreSQL no está disponible
las pruebas con base se OMITEN (`pytest -rs`) y no cuentan como ejecutadas.

- El archivo versionado coincide con lo que genera el modelo.
- Orden de aplicación: instalación NUEVA = 001 → 003; base EXISTENTE (Etapa 4A) = 002 → 003; 003 va siempre después de la
  tabla `documentos` y no se puede aplicar dos veces.
- Las restricciones de la propia BD rechazan combinaciones imposibles (la operación enlazada tiene documento, etc.).
- El arranque (`crear_tablas`) no crea `operaciones_ingesta` ni `documentos` aunque los modelos estén cargados.
"""
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.generar_ddl_documentos import generar_ddl, generar_ddl_operaciones
from tests.services.test_documento_ddl_postgres import _crear_base_con_prerequisitos

RAIZ = Path(__file__).resolve().parents[2]
MIGRACIONES = RAIZ / "app" / "migraciones" / "transaccional"
SQL_002 = MIGRACIONES / "0002_documentos_coordinador.sql"
SQL_003 = MIGRACIONES / "0003_operaciones_ingesta.sql"
SQL_ETAPA_4A = Path(__file__).parent / "datos" / "documentos_etapa_4a.sql"


def leer(ruta: Path) -> str:
    return ruta.read_text(encoding="utf-8")


async def ejecutar(esquema, sql: str) -> None:
    conexion = await esquema.conectar()
    try:
        await conexion.execute(sql)
    finally:
        await conexion.close()


async def consultar(esquema, sql: str, *args):
    conexion = await esquema.conectar()
    try:
        return await conexion.fetch(sql, *args)
    finally:
        await conexion.close()


@pytest_asyncio.fixture
async def esquema(url_pg_aislado):
    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    await ejecutar(esquema, generar_ddl())  # referencia de instalación nueva generada desde el modelo
    await ejecutar(esquema, leer(SQL_003))
    return esquema


def test_el_sql_versionado_coincide_con_el_generador_y_documenta_el_orden():
    texto = leer(SQL_003).replace("\r\n", "\n")
    assert texto == generar_ddl_operaciones()
    assert "Requiere las tablas `usuarios` y `documentos`" in texto
    assert "no se ejecuta a mano" in texto
    assert "CREATE TYPE" not in texto  # sin enumerados nuevos
    # El DDL de referencia del modelo sigue siendo generable (lo usan las pruebas de instalación nueva).
    assert "CREATE TABLE documentos" in generar_ddl()


async def test_sin_la_tabla_documentos_el_003_falla_y_no_deja_nada(url_pg_aislado):
    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    valor_leer = leer(SQL_003)
    with pytest.raises(asyncpg.UndefinedTableError):
        await ejecutar(esquema, valor_leer)
    assert await consultar(esquema, "SELECT 1 FROM pg_tables WHERE tablename = 'operaciones_ingesta'") == []


async def test_instalacion_nueva_001_003_y_actualizacion_4a_002_003_dejan_el_mismo_esquema(esquema, url_pg_aislado):
    vieja = await _crear_base_con_prerequisitos(url_pg_aislado)
    await ejecutar(vieja, leer(SQL_ETAPA_4A))
    await ejecutar(vieja, "BEGIN;" + chr(10) + leer(SQL_002) + chr(10) + "COMMIT;")
    await ejecutar(vieja, leer(SQL_003))

    async def descripcion(e):
        columnas = await consultar(
            e, "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
               "WHERE table_name = 'operaciones_ingesta' ORDER BY ordinal_position")
        restricciones = await consultar(
            e, "SELECT conname, pg_get_constraintdef(oid) AS d FROM pg_constraint "
               "WHERE conrelid = 'operaciones_ingesta'::regclass ORDER BY conname")
        indices = await consultar(
            e, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'operaciones_ingesta' ORDER BY indexname")
        return [tuple(c.values()) for c in columnas], [tuple(r.values()) for r in restricciones], [tuple(i.values()) for i in indices]

    nueva, actualizada = await descripcion(esquema), await descripcion(vieja)
    assert nueva == actualizada
    nombres = {n for n, _ in nueva[1]}
    assert {"ck_operaciones_estado_valido", "ck_operaciones_documento_solo_si_enlazada",
            "ck_operaciones_error_solo_si_rechazada", "ck_operaciones_carga_solo_si_iniciada",
            "ck_operaciones_vigencia_con_carga"} <= nombres


async def test_el_003_no_se_puede_aplicar_dos_veces(esquema):
    valor_leer_2 = leer(SQL_003)
    with pytest.raises(asyncpg.DuplicateTableError):
        await ejecutar(esquema, valor_leer_2)


async def insertar(esquema, **valores):
    conexion = await esquema.conectar()
    try:
        base = {"id": uuid.uuid4(), "ambiente": "development", "usuario_id": esquema.usuario, "estado": "CREADA"}
        base.update(valores)
        columnas = ", ".join(base)
        marcadores = ", ".join(f"${i}" for i in range(1, len(base) + 1))
        await conexion.execute(f"INSERT INTO operaciones_ingesta ({columnas}) VALUES ({marcadores})", *base.values())
        return base["id"]
    finally:
        await conexion.close()


@pytest.mark.parametrize(
    "cambios,restriccion",
    [
        ({"estado": "OTRA"}, "ck_operaciones_estado_valido"),
        ({"estado": "CON_DOCUMENTO"}, "ck_operaciones_documento_solo_si_enlazada"),
        ({"estado": "RECHAZADA"}, "ck_operaciones_error_solo_si_rechazada"),
        ({"codigo_error": "X"}, "ck_operaciones_error_solo_si_rechazada"),
        ({"estado": "EN_CARGA"}, "ck_operaciones_carga_solo_si_iniciada"),
    ],
)
async def test_la_bd_rechaza_estados_imposibles(esquema, cambios, restriccion):
    from datetime import datetime, timedelta, timezone

    if cambios.get("estado") not in (None, "EN_CARGA"):  # una operación ya cargada: solo falla la restricción probada
        ahora = datetime.now(timezone.utc)
        cambios = {**cambios, "carga_iniciada_en": ahora, "carga_vigente_hasta": ahora + timedelta(minutes=5)}
    with pytest.raises(asyncpg.CheckViolationError) as capturado:
        await insertar(esquema, **cambios)
    assert capturado.value.constraint_name == restriccion


async def test_estados_validos_se_aceptan_y_un_documento_solo_se_enlaza_una_vez(esquema):
    from datetime import datetime, timedelta, timezone

    ahora = datetime.now(timezone.utc)
    carga = {"carga_iniciada_en": ahora, "carga_vigente_hasta": ahora + timedelta(minutes=5)}
    await insertar(esquema)
    await insertar(esquema, estado="EN_CARGA", **carga)
    await insertar(esquema, estado="RECHAZADA", codigo_error="FILE_TOO_LARGE", mensaje_error="x", estado_http=413, **carga)
    # CON_DOCUMENTO exige un documento real (clave foránea) y es único por documento.
    conexion = await esquema.conectar()
    try:
        valor_uuid_uuid4 = uuid.uuid4()
        valor_uuid_uuid4_2 = uuid.uuid4()
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conexion.execute(
                "INSERT INTO operaciones_ingesta (id, ambiente, usuario_id, estado, documento_id, carga_iniciada_en, carga_vigente_hasta) "
                "VALUES ($1, 'development', $2, 'CON_DOCUMENTO', $3, now(), now())", valor_uuid_uuid4, esquema.usuario, valor_uuid_uuid4_2)
    finally:
        await conexion.close()


# ============================================ El arranque no crea las tablas ============================================

async def test_el_arranque_no_crea_operaciones_ni_documentos_aunque_los_modelos_esten_cargados(url_pg_aislado):
    from app.database import Base
    from app.main import app  # noqa: F401  (importa routers, gestor y modelos: el riesgo es real)
    from scripts import crear_tablas

    assert {"documentos", "operaciones_ingesta"} <= set(Base.metadata.tables)
    assert {"documentos", "operaciones_ingesta"}.isdisjoint({t.name for t in crear_tablas.tablas_a_crear()})

    base = f"arranque_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(url_pg_aislado.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await admin.execute(f'CREATE DATABASE "{base}"')
    finally:
        await admin.close()
    url = url_pg_aislado.rsplit("/", 1)[0] + f"/{base}"
    motor = create_async_engine(url)
    try:
        async with motor.begin() as conexion:
            await conexion.run_sync(crear_tablas._crear_permitidas)
            await conexion.run_sync(crear_tablas._crear_permitidas)  # idempotente
    finally:
        await motor.dispose()
    conexion = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        tablas = {r["tablename"] for r in await conexion.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        tipos = {r["typname"] for r in await conexion.fetch("SELECT typname FROM pg_type WHERE typtype = 'e'")}
    finally:
        await conexion.close()
    assert {"usuarios", "empresas", "auditoria"} <= tablas
    assert {"documentos", "operaciones_ingesta"}.isdisjoint(tablas)
    assert {"estado_procesamiento", "resultado_analisis", "estado_compensacion", "tipo_documento"}.isdisjoint(tipos)


def test_el_conjunto_de_tablas_sin_ddl_automatico_incluye_ambas():
    from scripts.crear_tablas import TABLAS_SIN_DDL_AUTOMATICO

    assert TABLAS_SIN_DDL_AUTOMATICO == {"documentos", "operaciones_ingesta"}

"""Migración 0004 (Módulo 5) sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

- El esquema que deja la migración es idéntico al que declaran los modelos.
- Instalación nueva (1→4) y ACTUALIZACIÓN de una base ya migrada hasta la 0003 con datos: se aplica solo la 0004 y no se pierde nada.
- Un segundo arranque no ejecuta nada y el arranque del módulo no necesita la ingesta.
Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
import uuid

import pytest

from app.core.database import Base
from app.migraciones import motor
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.motor import EsquemaBase, migrar
from app.models.analisis_gri import (
    TABLAS_ANALISIS_GRI,
    AnalisisGriControl,
    AnalisisGriEstado,
    AnalisisGriHistorial,
    AnalisisGriValidacion,
)
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar, tablas, versiones_registradas

PRIMERAS_TRES = EsquemaBase("transaccional", ESQUEMA_TRANSACCIONAL.migraciones[:3], prerrequisitos=ESQUEMA_TRANSACCIONAL.prerrequisitos)


async def firmas(url: str) -> dict:
    conexion = await conectar(url)
    try:
        return {t: await motor.firma_de_objeto(conexion, t) for t in TABLAS_ANALISIS_GRI}
    finally:
        await conexion.close()


async def test_el_esquema_migrado_es_identico_al_de_los_modelos(url_pg_aislado):
    migrada = await base_transaccional_nueva(url_pg_aislado)
    await migrar(migrada, ESQUEMA_TRANSACCIONAL)
    de_modelos = await base_transaccional_nueva(url_pg_aislado)
    motor_sql = await conectar(de_modelos)
    try:
        # Sin tipos enumerados nuevos: crear las tablas desde los modelos sólo necesita `create_all` de estas cuatro.
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateIndex, CreateTable

        dialecto = postgresql.dialect()
        for modelo in (AnalisisGriControl, AnalisisGriEstado, AnalisisGriHistorial, AnalisisGriValidacion):
            await motor_sql.execute(str(CreateTable(modelo.__table__).compile(dialect=dialecto)))
            for indice in modelo.__table__.indexes:
                await motor_sql.execute(str(CreateIndex(indice).compile(dialect=dialecto)))
    finally:
        await motor_sql.close()
    assert await firmas(migrada) == await firmas(de_modelos)
    assert set(TABLAS_ANALISIS_GRI) <= set(Base.metadata.tables)


async def test_instalacion_nueva_aplica_las_cuatro_migraciones_y_registra_la_0004(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.aplicadas == (1, 2, 3, 4)
    assert set(TABLAS_ANALISIS_GRI) <= await tablas(url)
    assert (await versiones_registradas(url))[-1] == (4, "aplicada")
    (fila,) = await consultar(url, "SELECT nombre, checksum FROM igualab_migraciones WHERE version = 4")
    assert fila["nombre"] == "analisis_gri"
    assert fila["checksum"].strip() == ESQUEMA_TRANSACCIONAL.migraciones[3].checksum()


async def test_actualiza_una_base_con_datos_aplicando_solo_la_0004_y_sin_perder_nada(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    anterior = await migrar(url, PRIMERAS_TRES)  # lo que ya hay desplegado antes del Módulo 5
    assert anterior.aplicadas == (1, 2, 3)
    assert not (set(TABLAS_ANALISIS_GRI) & await tablas(url))
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    documento = uuid.uuid4()
    await ejecutar(
        url,
        "INSERT INTO documentos (id, ambiente, empresa_id, anio, tipo, sector, nombre_archivo, sha256, tamano_bytes, usuario_id,"
        " clave_original, ejecucion_token, ejecucion_vigente_hasta, analisis) VALUES ($1, 'development', $2, 2025, 'MEMORIA_ANUAL',"
        " 'MINERIA', 'previo.md', $3, 10, $4, 'development/documentos/x/original.md', $5, now() + interval '1 hour', NULL)",
        documento, empresa, "b" * 64, usuario, uuid.uuid4(),
    )
    antes = await consultar(url, "SELECT * FROM documentos")
    checksums_previos = await consultar(url, "SELECT version, checksum, aplicada_en FROM igualab_migraciones ORDER BY version")

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)  # el arranque de la versión nueva

    assert (resultado.aplicadas, resultado.adoptadas, resultado.previas) == ((4,), (), (1, 2, 3))
    assert set(TABLAS_ANALISIS_GRI) <= await tablas(url)
    assert [tuple(f) for f in await consultar(url, "SELECT * FROM documentos")] == [tuple(f) for f in antes]  # datos intactos
    assert [tuple(f) for f in await consultar(url, "SELECT version, checksum, aplicada_en FROM igualab_migraciones WHERE version <= 3 ORDER BY version")] \
        == [tuple(f) for f in checksums_previos]  # las migraciones ya aplicadas ni se reaplicaron ni cambió su checksum
    for tabla in TABLAS_ANALISIS_GRI:
        assert (await consultar(url, f"SELECT count(*) AS n FROM {tabla}"))[0]["n"] == 0


async def test_segundo_arranque_no_ejecuta_nada_y_una_base_con_datos_del_modulo_conserva_estados_e_historial(url_pg_aislado, monkeypatch):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    await ejecutar(url, "INSERT INTO analisis_gri_control (ambiente, empresa_id, anio, version) VALUES ('development', $1, 2025, 1)", empresa)
    await ejecutar(
        url,
        "INSERT INTO analisis_gri_estados (id, ambiente, empresa_id, anio, identidad, codigo, edicion, identidad_tipo, estado, asignado_por)"
        " VALUES ($1, 'development', $2, 2025, '204:2016', '204', '2016', 'EDICION_RESUELTA', 'OK', $3)", uuid.uuid4(), empresa, usuario)
    antes = await consultar(url, "SELECT version, aplicada_en, xmin::text AS xmin FROM igualab_migraciones ORDER BY version")

    async def no_debe_ejecutarse(*args, **kwargs):
        raise AssertionError("el segundo arranque no debe aplicar ni registrar nada")

    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)
    monkeypatch.setattr(motor, "_registrar", no_debe_ejecutarse)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.sin_cambios
    assert resultado.previas == (1, 2, 3, 4)
    assert await consultar(url, "SELECT version, aplicada_en, xmin::text AS xmin FROM igualab_migraciones ORDER BY version") == antes
    assert (await consultar(url, "SELECT estado FROM analisis_gri_estados"))[0]["estado"] == "OK"


async def test_la_0004_sin_los_prerrequisitos_no_deja_nada(url_pg_aislado):
    import asyncpg

    from tests.ayudantes_migraciones import base_vacia

    url = await base_vacia(url_pg_aislado, "sin_prerreq")
    sql_0004 = ESQUEMA_TRANSACCIONAL.migraciones[3].sql()
    with pytest.raises(asyncpg.UndefinedTableError):
        await ejecutar(url, sql_0004)
    assert not (set(TABLAS_ANALISIS_GRI) & await tablas(url))


def test_la_migracion_no_toca_documentos_ni_crea_tipos_enumerados():
    sql = ESQUEMA_TRANSACCIONAL.migraciones[3].sql()
    assert "CREATE TYPE" not in sql
    assert "ALTER TABLE documentos" not in sql
    assert "UPDATE documentos" not in sql
    assert "analisis_gri_estados" in sql
    assert "solo inserción" in sql.lower().replace("solo insercion", "solo inserción")

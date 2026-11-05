"""Migración 0006 (reportes versionados, BUG-15) sobre un PostgreSQL REAL, AISLADO y TEMPORAL, partiendo de una base con la forma
ANTERIOR de `reportes_prospeccion` y reportes ya emitidos. Cubre la conservación de datos, la compatibilidad de los reportes
históricos (versión 1, sin validación vinculada), las protecciones que no se tocan y la idempotencia. Sin PostgreSQL se OMITE."""
from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.compatibilidad import verificar_esquema_central
from app.migraciones.motor import migrar
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar

COLUMNAS = "id, empresa_id, anio, generado_por, pdf, sha256, snapshot::text AS snapshot, creado_en"


def esquema_hasta(version: int):
    return dataclasses.replace(ESQUEMA_TRANSACCIONAL, migraciones=ESQUEMA_TRANSACCIONAL.migraciones[:version])


async def base_con_reportes_anteriores(url_pg_aislado: str, hasta: int = 5) -> tuple[str, list[tuple]]:
    """Base desplegada ANTES del versionado: `reportes_prospeccion` con UNIQUE(empresa_id, anio), sin `version` ni `validacion_id`,
    migraciones 0001–0005 aplicadas y tres reportes de distintas empresas y años."""
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(
        url,
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT uq_reporte_empresa_anio_version, DROP CONSTRAINT uq_reporte_validacion,"
        " DROP CONSTRAINT ck_reporte_version_positiva, DROP COLUMN version, DROP COLUMN validacion_id;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT uq_reporte_empresa_anio UNIQUE (empresa_id, anio);",
    )
    await migrar(url, esquema_hasta(hasta))  # 5: con los disparadores de solo inserción (0005); 6: además los reportes versionados
    autor = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresas = [f["id"] for f in await consultar(url, "SELECT id FROM empresas ORDER BY nombre LIMIT 2")]
    historicos = [
        (empresas[0], 2022, {"validacion": {"numero": 1, "huella_documentos": "a" * 64}, "puntaje": {"valor_presentacion": 50.0}}),
        (empresas[0], 2023, {"validacion": {"numero": 2, "huella_documentos": "b" * 64}}),
        (empresas[1], 2022, {"anio": 2022}),
    ]
    conexion = await conectar(url)
    try:
        for n, (empresa, anio, snapshot) in enumerate(historicos):
            columna_version = ", version" if hasta >= 6 else ""  # con la 0006 la columna existe y no tiene valor por defecto
            valor_version = ", 1" if hasta >= 6 else ""
            await conexion.execute(
                f"INSERT INTO reportes_prospeccion (id, empresa_id, anio, generado_por, pdf, sha256, snapshot, creado_en{columna_version})"
                f" VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8{valor_version})",
                uuid.uuid4(), empresa, anio, autor, f"%PDF-1.4 historico {n}".encode(), f"{n}" * 64, json.dumps(snapshot),
                datetime(2026, 10, 1 + n, 12, 0, tzinfo=timezone.utc),
            )
    finally:
        await conexion.close()
    return url, [tuple(f) for f in await consultar(url, f"SELECT {COLUMNAS} FROM reportes_prospeccion ORDER BY creado_en")]


async def sembrar_validacion(url: str, empresa, anio: int, autor, ambiente: str = "development") -> uuid.UUID:
    """Preparación de la base de prueba: fila de control y una validación del Módulo 5 de la empresa y el año (datos mínimos válidos)."""
    validacion = uuid.uuid4()
    await ejecutar(
        url,
        "INSERT INTO analisis_gri_control (ambiente, empresa_id, anio) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        ambiente, empresa, anio,
    )
    await ejecutar(
        url,
        "INSERT INTO analisis_gri_validaciones (id, ambiente, empresa_id, anio, numero, version_estados, huella_documentos, documentos,"
        " estados, puntaje_suma, puntaje_filas, validado_por) VALUES ($1, $2, $3, $4, 1, 1, $5, '[]'::jsonb, '[]'::jsonb, 100, 1, $6)",
        validacion, ambiente, empresa, anio, "a" * 64, autor,
    )
    return validacion


async def restricciones(url: str) -> set[str]:
    filas = await consultar(url, "SELECT conname FROM pg_constraint WHERE conrelid = 'public.reportes_prospeccion'::regclass")
    return {f["conname"] for f in filas}


async def test_la_migracion_conserva_identificadores_pdf_snapshot_fecha_y_autor_y_deja_todo_en_la_version_1(url_pg_aislado):
    url, antes = await base_con_reportes_anteriores(url_pg_aislado)
    assert "uq_reporte_empresa_anio" in await restricciones(url)

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert resultado.aplicadas == (6, 7)
    despues = [tuple(f) for f in await consultar(url, f"SELECT {COLUMNAS} FROM reportes_prospeccion ORDER BY creado_en")]
    assert despues == antes  # ids, PDF, SHA-256, snapshot, fecha y autor idénticos, sin una sola fila reescrita
    nuevas = await consultar(url, "SELECT version, validacion_id FROM reportes_prospeccion")
    assert [(f["version"], f["validacion_id"]) for f in nuevas] == [(1, None)] * 3  # histórico: versión 1 y sin vínculo inventado
    nombres = await restricciones(url)
    assert {"uq_reporte_empresa_anio_version", "uq_reporte_validacion", "fk_reporte_validacion", "ck_reporte_version_positiva"} <= nombres
    assert "uq_reporte_empresa_anio" not in nombres  # la unicidad por empresa/año ya no bloquea
    await verificar_esquema_central(url)
    # sin DEFAULT: toda inserción nueva debe indicar su versión
    assert (await consultar(url, "SELECT column_default FROM information_schema.columns WHERE table_name = 'reportes_prospeccion' AND column_name = 'version'"))[0][0] is None


async def test_tras_migrar_una_empresa_y_anio_admiten_otra_version_y_los_historicos_siguen_protegidos(url_pg_aislado):
    url, antes = await base_con_reportes_anteriores(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    empresa, anio, autor = antes[0][1], antes[0][2], antes[0][3]
    validacion = await sembrar_validacion(url, empresa, anio, autor)

    await ejecutar(
        url,
        "INSERT INTO reportes_prospeccion (id, empresa_id, anio, version, validacion_id, generado_por, pdf, sha256, snapshot)"
        " VALUES (gen_random_uuid(), $1, $2, 2, $3, $4, $5, $6, '{}'::jsonb)",
        empresa, anio, validacion, autor, b"%PDF-1.4 v2", "f" * 64,
    )
    assert [f["version"] for f in await consultar(url, "SELECT version FROM reportes_prospeccion WHERE empresa_id = $1 AND anio = $2 ORDER BY version", empresa, anio)] == [1, 2]
    for sentencia in ("UPDATE reportes_prospeccion SET pdf = '\\x00'::bytea", "DELETE FROM reportes_prospeccion"):
        with pytest.raises(Exception, match="solo inserción"):
            await ejecutar(url, sentencia)
    originales = [tuple(f) for f in await consultar(url, f"SELECT {COLUMNAS} FROM reportes_prospeccion WHERE version = 1 ORDER BY creado_en")]
    assert originales == antes


async def test_la_migracion_es_idempotente_y_un_segundo_arranque_no_aplica_nada(url_pg_aislado):
    url, antes = await base_con_reportes_anteriores(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios
    for _ in range(2):  # reaplicada a mano: no falla ni duplica
        await ejecutar(url, ESQUEMA_TRANSACCIONAL.migraciones[5].sql())
    despues = [tuple(f) for f in await consultar(url, f"SELECT {COLUMNAS} FROM reportes_prospeccion ORDER BY creado_en")]
    assert despues == antes
    assert len(await restricciones(url) & {"uq_reporte_empresa_anio_version", "uq_reporte_validacion"}) == 2


async def test_la_migracion_no_desactiva_disparadores_ni_trunca_ni_usa_cascade():
    sql = ESQUEMA_TRANSACCIONAL.migraciones[5].sql()
    ejecutable = "\n".join(linea for linea in sql.splitlines() if not linea.lstrip().startswith("--")).upper()
    for prohibido in ("DISABLE TRIGGER", "TRUNCATE", "CASCADE", "UPDATE ", "DELETE ", "DROP TABLE", "DROP TRIGGER"):
        assert prohibido not in ejecutable, prohibido


async def test_la_0007_sobre_una_base_con_la_0006_conserva_los_historicos_y_rechaza_los_nuevos_sin_validacion(url_pg_aislado):
    url, antes = await base_con_reportes_anteriores(url_pg_aislado, hasta=6)  # ambientes que ya tienen la 0006
    assert await elementos_faltantes_de(url) != []  # todavía no tienen las protecciones de la 0007

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert resultado.aplicadas == (7,)
    despues = [tuple(f) for f in await consultar(url, f"SELECT {COLUMNAS} FROM reportes_prospeccion ORDER BY creado_en")]
    assert despues == antes  # ids, PDF, SHA-256, snapshot, fecha y autor idénticos
    assert [f["validacion_id"] for f in await consultar(url, "SELECT validacion_id FROM reportes_prospeccion")] == [None] * 3
    assert await elementos_faltantes_de(url) == []
    empresa, autor = antes[0][1], antes[0][3]
    with pytest.raises(Exception, match="validacion GRI"):
        await ejecutar(
            url,
            "INSERT INTO reportes_prospeccion (id, empresa_id, anio, version, generado_por, pdf, sha256, snapshot)"
            " VALUES (gen_random_uuid(), $1, 2030, 1, $2, $3, $4, '{}'::jsonb)",
            empresa, autor, b"%PDF-1.4 nuevo", "e" * 64,
        )
    assert len(await consultar(url, "SELECT id FROM reportes_prospeccion")) == 3


async def elementos_faltantes_de(url: str) -> list[str]:
    from app.migraciones.compatibilidad import elementos_faltantes

    conexion = await conectar(url)
    try:
        return await elementos_faltantes(conexion)
    finally:
        await conexion.close()

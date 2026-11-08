"""Integridad de las emisiones de reportes versionados (BUG-15, entrega 1) sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

Las restricciones y los disparadores se prueban con sentencias directas contra la base (es la única forma de demostrar lo que la BASE
rechaza aunque el servicio estuviera mal); los datos de apoyo (validaciones) los crea el servicio real del Módulo 5. Cada aserción
comprueba UNA condición. Cubre: reportes nuevos sin validación, validación de otra empresa o año, validación inexistente,
duplicados de validación y de versión, versiones no positivas, instalación limpia, actualización con históricos, datos
incompatibles y el control de compatibilidad ante restricciones con el nombre esperado pero la definición equivocada.
"""
from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.compatibilidad import CODIGO, elementos_faltantes, verificar_esquema_central
from app.migraciones.motor import ErrorMigracion, migrar
from app.models.analisis_gri import EstadoGri
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    crear_administrador,
    crear_empresa,
    sembrar_documento,
)
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar
from tests.services.test_reportes_versionado_postgres import asignar_estados, validar

OK, SUB = EstadoGri.OK, EstadoGri.SUB_REPORTADO
ANIO = 2025


def _inserta(version: int, validacion, empresa, anio: int, autor, hash_: str):
    return (
        text(
            "INSERT INTO reportes_prospeccion (id, empresa_id, anio, version, validacion_id, generado_por, pdf, sha256, snapshot)"
            " VALUES (gen_random_uuid(), :e, :a, :v, :val, :u, :pdf, :h, '{}'::jsonb)"
        ),
        {"e": empresa.id, "a": anio, "v": version, "val": validacion, "u": autor.id, "pdf": b"%PDF-1.4 x", "h": hash_},
    )


@pytest_asyncio.fixture
async def ent(fabrica_pg, url_pg_aislado):
    """Dos empresas con validaciones reales: A/2025 (#1 y #2), A/2024 (#1) y B/2025 (#1)."""
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        a = await crear_empresa(db, "Empresa A")
        b = await crear_empresa(db, "Empresa B")
        for empresa, anio in ((a, 2025), (a, 2024), (b, 2025)):
            await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=anio)
    e = SimpleNamespace(fabrica=fabrica_pg, admin=admin, empresa=a, otra=b)
    await asignar_estados(e, [OK, SUB], a, 2025)
    await validar(e, a, 2025)
    await asignar_estados(e, [OK, OK], a, 2025)
    await validar(e, a, 2025)
    await asignar_estados(e, [OK, SUB], a, 2024)
    await validar(e, a, 2024)
    await asignar_estados(e, [OK, SUB], b, 2025)
    await validar(e, b, 2025)
    e.validaciones = {
        (empresa_id, anio, numero): vid
        for vid, empresa_id, anio, numero in await _validaciones(e)
    }
    return e


async def _validaciones(e):
    async with e.fabrica() as db:
        return [tuple(f) for f in (await db.execute(text("SELECT id, empresa_id, anio, numero FROM analisis_gri_validaciones"))).all()]


async def _rechaza(e, version, validacion, empresa, anio, hash_="1" * 64):
    """Intenta la inserción; devuelve el error (IntegrityError) y comprueba que no quedó nada."""
    sentencia, parametros = _inserta(version, validacion, empresa, anio, e.admin, hash_)
    async with e.fabrica() as db:
        with pytest.raises(IntegrityError) as capturado:  # las restricciones y el disparador actúan al ejecutar la sentencia
            await db.execute(sentencia, parametros)
    return capturado.value


def _restriccion(error) -> str | None:
    return getattr(getattr(error.orig, "__cause__", None), "constraint_name", None)


def _sqlstate(error) -> str | None:
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def _total(e) -> int:
    async with e.fabrica() as db:
        return (await db.execute(text("SELECT count(*) FROM reportes_prospeccion"))).scalar_one()


async def _insertar_valido(e, version, validacion, empresa, anio, hash_):
    sentencia, parametros = _inserta(version, validacion, empresa, anio, e.admin, hash_)
    async with e.fabrica() as db:
        await db.execute(sentencia, parametros)
        await db.commit()


# ============================================ 10 · Reportes nuevos sin validación ============================================

@pytest.mark.parametrize("version", [1, 2])
async def test_la_base_rechaza_un_reporte_nuevo_sin_validacion(ent, version):
    error = await _rechaza(ent, version, None, ent.empresa, ANIO)
    assert _sqlstate(error) == "23502"  # not_null_violation del disparador que exige la validación
    assert "validacion" in str(error.orig)
    assert await _total(ent) == 0


# ============================================ 11 · Validación de otra empresa o año ============================================

async def test_la_base_rechaza_la_validacion_de_otra_empresa(ent):
    de_b = ent.validaciones[(ent.otra.id, ANIO, 1)]
    error = await _rechaza(ent, 1, de_b, ent.empresa, ANIO)
    assert _restriccion(error) == "fk_reporte_validacion_empresa_anio"
    assert await _total(ent) == 0


async def test_la_base_rechaza_la_validacion_de_otro_anio(ent):
    de_2024 = ent.validaciones[(ent.empresa.id, 2024, 1)]
    error = await _rechaza(ent, 1, de_2024, ent.empresa, 2025)
    assert _restriccion(error) == "fk_reporte_validacion_empresa_anio"
    assert await _total(ent) == 0


async def test_la_base_acepta_la_validacion_de_la_misma_empresa_y_anio(ent):
    propia = ent.validaciones[(ent.empresa.id, ANIO, 1)]
    await _insertar_valido(ent, 1, propia, ent.empresa, ANIO, "a" * 64)
    assert await _total(ent) == 1


# ============================================ 12 · Validación inexistente ============================================

async def test_la_base_rechaza_una_validacion_inexistente(ent):
    error = await _rechaza(ent, 1, uuid.uuid4(), ent.empresa, ANIO)
    assert _restriccion(error) in {"fk_reporte_validacion", "fk_reporte_validacion_empresa_anio"}
    assert _sqlstate(error) == "23503"  # foreign_key_violation
    assert await _total(ent) == 0


# ============================================ 13 · Duplicados de validación y de versión ============================================

async def test_la_base_rechaza_dos_reportes_con_la_misma_validacion(ent):
    propia = ent.validaciones[(ent.empresa.id, ANIO, 1)]
    await _insertar_valido(ent, 1, propia, ent.empresa, ANIO, "a" * 64)
    error = await _rechaza(ent, 2, propia, ent.empresa, ANIO, "b" * 64)
    assert _restriccion(error) == "uq_reporte_validacion"
    assert await _total(ent) == 1


async def test_la_base_rechaza_dos_reportes_con_la_misma_version_de_la_empresa_y_el_anio(ent):
    await _insertar_valido(ent, 1, ent.validaciones[(ent.empresa.id, ANIO, 1)], ent.empresa, ANIO, "a" * 64)
    error = await _rechaza(ent, 1, ent.validaciones[(ent.empresa.id, ANIO, 2)], ent.empresa, ANIO, "b" * 64)
    assert _restriccion(error) == "uq_reporte_empresa_anio_version"
    assert await _total(ent) == 1


async def test_la_misma_version_puede_existir_en_otra_empresa_y_en_otro_anio(ent):
    await _insertar_valido(ent, 1, ent.validaciones[(ent.empresa.id, ANIO, 1)], ent.empresa, ANIO, "a" * 64)
    await _insertar_valido(ent, 1, ent.validaciones[(ent.otra.id, ANIO, 1)], ent.otra, ANIO, "b" * 64)
    await _insertar_valido(ent, 1, ent.validaciones[(ent.empresa.id, 2024, 1)], ent.empresa, 2024, "c" * 64)
    assert await _total(ent) == 3


# ============================================ 14 · Versiones no positivas ============================================

@pytest.mark.parametrize("version", [0, -1])
async def test_la_base_rechaza_una_version_no_positiva(ent, version):
    error = await _rechaza(ent, version, ent.validaciones[(ent.empresa.id, ANIO, 1)], ent.empresa, ANIO)
    assert _restriccion(error) == "ck_reporte_version_positiva"
    assert await _total(ent) == 0


# ============================================ 15 · Instalación limpia, actualización y segundo arranque ============================================

async def _definiciones_ok(url: str) -> list[str]:
    conexion = await conectar(url)
    try:
        return await elementos_faltantes(conexion)
    finally:
        await conexion.close()


async def test_instalacion_limpia_aplica_las_siete_migraciones_con_las_protecciones_exigidas(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.aplicadas == (1, 2, 3, 4, 5, 6, 7, 8)
    assert await _definiciones_ok(url) == []
    await verificar_esquema_central(url)


async def test_el_segundo_arranque_no_aplica_nada_y_las_protecciones_siguen_vigentes(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios
    assert await _definiciones_ok(url) == []


async def test_la_0007_es_idempotente_si_se_reaplica_a_mano(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    for _ in range(2):
        await ejecutar(url, ESQUEMA_TRANSACCIONAL.migraciones[6].sql())
    assert await _definiciones_ok(url) == []


# ============================================ 16 · Nombre esperado con definición equivocada ============================================

CASOS_INCORRECTOS = {
    "unicidad_de_validacion_sobre_otra_columna": (
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT uq_reporte_validacion;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT uq_reporte_validacion UNIQUE (generado_por)",
        "restriccion_incorrecta:reportes_prospeccion.uq_reporte_validacion",
    ),
    "unicidad_de_version_sin_la_version": (
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT uq_reporte_empresa_anio_version;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT uq_reporte_empresa_anio_version UNIQUE (empresa_id, anio)",
        "restriccion_incorrecta:reportes_prospeccion.uq_reporte_empresa_anio_version",
    ),
    "check_con_otra_condicion": (
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT ck_reporte_version_positiva;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT ck_reporte_version_positiva CHECK (version >= 0)",
        "restriccion_incorrecta:reportes_prospeccion.ck_reporte_version_positiva",
    ),
    "fk_simple_hacia_otra_tabla": (
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT fk_reporte_validacion;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT fk_reporte_validacion FOREIGN KEY (validacion_id) REFERENCES usuarios (id)",
        "restriccion_incorrecta:reportes_prospeccion.fk_reporte_validacion",
    ),
    "fk_simple_sin_validar": (
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT fk_reporte_validacion;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT fk_reporte_validacion FOREIGN KEY (validacion_id)"
        " REFERENCES analisis_gri_validaciones (id) NOT VALID",
        "restriccion_incorrecta:reportes_prospeccion.fk_reporte_validacion",
    ),
    "fk_compuesta_sin_empresa_ni_anio": (
        "ALTER TABLE reportes_prospeccion DROP CONSTRAINT fk_reporte_validacion_empresa_anio;"
        " ALTER TABLE reportes_prospeccion ADD CONSTRAINT fk_reporte_validacion_empresa_anio FOREIGN KEY (validacion_id)"
        " REFERENCES analisis_gri_validaciones (id)",
        "restriccion_incorrecta:reportes_prospeccion.fk_reporte_validacion_empresa_anio",
    ),
    "disparador_despues_de_insertar": (
        "DROP TRIGGER tr_reportes_exigir_validacion ON reportes_prospeccion;"
        " CREATE TRIGGER tr_reportes_exigir_validacion AFTER INSERT ON reportes_prospeccion"
        " FOR EACH ROW EXECUTE FUNCTION igualab_exigir_validacion_en_reporte()",
        "disparador_incorrecto:reportes_prospeccion.tr_reportes_exigir_validacion",
    ),
    "disparador_con_otra_funcion": (
        "DROP TRIGGER tr_reportes_exigir_validacion ON reportes_prospeccion;"
        " CREATE TRIGGER tr_reportes_exigir_validacion BEFORE INSERT ON reportes_prospeccion"
        " FOR EACH ROW EXECUTE FUNCTION igualab_rechazar_modificacion_solo_insercion()",
        "disparador_incorrecto:reportes_prospeccion.tr_reportes_exigir_validacion",
    ),
    "funcion_que_ya_no_rechaza": (
        "CREATE OR REPLACE FUNCTION igualab_exigir_validacion_en_reporte() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql",
        "disparador_incorrecto:reportes_prospeccion.tr_reportes_exigir_validacion",
    ),
    "disparador_desactivado": (
        "ALTER TABLE reportes_prospeccion DISABLE TRIGGER tr_reportes_exigir_validacion",
        "disparador_desactivado:reportes_prospeccion.tr_reportes_exigir_validacion",
    ),
    "inmutabilidad_solo_para_actualizar": (
        "DROP TRIGGER tr_reportes_solo_insercion ON reportes_prospeccion;"
        " CREATE TRIGGER tr_reportes_solo_insercion BEFORE UPDATE ON reportes_prospeccion"
        " FOR EACH ROW EXECUTE FUNCTION igualab_rechazar_modificacion_solo_insercion()",
        "disparador_incorrecto:reportes_prospeccion.tr_reportes_solo_insercion",
    ),
    "version_que_acepta_nulos": (
        "ALTER TABLE reportes_prospeccion ALTER COLUMN version DROP NOT NULL",
        "columna_incorrecta:reportes_prospeccion.version",
    ),
}


@pytest_asyncio.fixture
async def base_migrada(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    return url


@pytest.mark.parametrize("caso", sorted(CASOS_INCORRECTOS))
async def test_una_definicion_equivocada_con_el_nombre_esperado_no_supera_el_control(base_migrada, caso):
    ddl, etiqueta = CASOS_INCORRECTOS[caso]
    await ejecutar(base_migrada, ddl)
    assert await _definiciones_ok(base_migrada) == [etiqueta]


async def test_el_arranque_rechaza_la_definicion_equivocada(base_migrada):
    ddl, etiqueta = CASOS_INCORRECTOS["fk_compuesta_sin_empresa_ni_anio"]
    await ejecutar(base_migrada, ddl)
    with pytest.raises(ErrorMigracion) as capturado:
        await verificar_esquema_central(base_migrada)
    assert capturado.value.codigo == CODIGO
    assert capturado.value.detalles["faltan"] == [etiqueta]


async def test_la_base_correcta_no_reporta_nada(base_migrada):
    assert await _definiciones_ok(base_migrada) == []


# ============================================ Datos incompatibles (la migración no los toca) ============================================

async def test_con_un_reporte_de_validacion_ajena_la_0007_se_detiene_sin_modificar_nada(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, dataclasses_hasta(6))
    autor = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    e1, e2 = [f["id"] for f in await consultar(url, "SELECT id FROM empresas ORDER BY nombre LIMIT 2")]
    validacion = uuid.uuid4()
    await ejecutar(url, "INSERT INTO analisis_gri_control (ambiente, empresa_id, anio) VALUES ('development', $1, 2025)", e1)
    await ejecutar(
        url,
        "INSERT INTO analisis_gri_validaciones (id, ambiente, empresa_id, anio, numero, version_estados, huella_documentos, documentos,"
        " estados, puntaje_suma, puntaje_filas, validado_por) VALUES ($1, 'development', $2, 2025, 1, 1, $3, '[]'::jsonb, '[]'::jsonb, 100, 1, $4)",
        validacion, e1, "a" * 64, autor,
    )
    # Dato incompatible que la FK simple de la 0006 sí permitía: el reporte es de OTRA empresa que su validación.
    await ejecutar(
        url,
        "INSERT INTO reportes_prospeccion (id, empresa_id, anio, version, validacion_id, generado_por, pdf, sha256, snapshot)"
        " VALUES (gen_random_uuid(), $1, 2025, 1, $2, $3, $4, $5, '{}'::jsonb)",
        e2, validacion, autor, b"%PDF-1.4 ajeno", "d" * 64,
    )
    antes = [tuple(f) for f in await consultar(url, "SELECT * FROM reportes_prospeccion")]

    with pytest.raises(ErrorMigracion) as capturado:
        await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert capturado.value.codigo == "MIGRATION_DATA_INCOMPATIBLE"
    assert capturado.value.detalles == {"version": 7, "sqlstate": "IG001"}
    assert [tuple(f) for f in await consultar(url, "SELECT * FROM reportes_prospeccion")] == antes  # ni borrado ni corregido
    assert 7 not in [f["version"] for f in await consultar(url, "SELECT version FROM igualab_migraciones")]
    nombres = {f["conname"] for f in await consultar(url, "SELECT conname FROM pg_constraint WHERE conrelid = 'public.reportes_prospeccion'::regclass")}
    assert "fk_reporte_validacion_empresa_anio" not in nombres


def dataclasses_hasta(version: int):
    import dataclasses

    return dataclasses.replace(ESQUEMA_TRANSACCIONAL, migraciones=ESQUEMA_TRANSACCIONAL.migraciones[:version])


# ============================================ Checksums de las migraciones aplicadas ============================================

async def test_la_0006_y_la_0007_quedan_registradas_con_el_checksum_de_su_archivo_y_el_segundo_arranque_no_lo_altera(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    registrado = {f["version"]: f["checksum"].strip() for f in await consultar(url, "SELECT version, checksum FROM igualab_migraciones")}
    for migracion in ESQUEMA_TRANSACCIONAL.migraciones[5:]:
        contenido = (motor_directorio() / migracion.carpeta / migracion.archivo).read_bytes().decode("utf-8").replace("\r\n", "\n")
        assert registrado[migracion.version] == hashlib.sha256(contenido.encode("utf-8")).hexdigest()
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios
    assert {f["version"]: f["checksum"].strip() for f in await consultar(url, "SELECT version, checksum FROM igualab_migraciones")} == registrado


def motor_directorio():
    from app.migraciones.motor import DIRECTORIO

    return DIRECTORIO

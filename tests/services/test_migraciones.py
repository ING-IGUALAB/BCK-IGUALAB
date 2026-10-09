"""Motor de migraciones de ingesta contra PostgreSQL REAL y AISLADO (transaccional: `initdb` local; vectorial: pgvector en Docker).

Cubre: instalación nueva, actualización de instalaciones existentes sin perder datos, segundo arranque sin ejecutar SQL, adopción
verificada (nunca por la mera existencia de la tabla), esquemas distintos, checksum, concurrencia, bloqueo, rollback ante fallo,
migraciones no transaccionales, pgvector ausente/sin permiso, conexión sin fallback ni filtración de credenciales y la integración con
el arranque. Si PostgreSQL local o Docker/pgvector no están disponibles las pruebas con base se OMITEN (`pytest -rs`).
"""
import asyncio
import re
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.migraciones import catalogo, motor
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL, ESQUEMA_VECTORIAL
from app.migraciones.motor import ErrorMigracion, EsquemaBase, firma_de_objeto, migrar
from scripts.generar_ddl_documentos import generar_ddl, generar_ddl_operaciones
from tests.ayudantes_migraciones import (
    base_transaccional_nueva,
    base_vacia,
    conectar,
    consultar,
    ejecutar,
    migracion_de_texto,
    tablas,
    url_de_base,
    versiones_registradas,
)

RAIZ = Path(__file__).resolve().parents[2]
SQL_4A = Path(__file__).parent / "datos" / "documentos_etapa_4a.sql"
TABLAS_TRANSACCIONALES = {"documentos", "operaciones_ingesta", "igualab_migraciones"}
TABLAS_VECTORIALES = {"fragmentos_documento", "cierres_documento", "igualab_migraciones"}


async def esquema_de(url: str, objetos: tuple[str, ...]) -> dict:
    conexion = await conectar(url)
    try:
        return {o: await firma_de_objeto(conexion, o) for o in objetos}
    finally:
        await conexion.close()


async def no_debe_ejecutarse(*args, **kwargs):
    raise AssertionError("no se debía ejecutar ninguna migración")


async def vectorial_nueva(url_pgvector: str, extension: bool = False) -> str:
    url = await base_vacia(url_pgvector, "vec")
    if extension:
        await ejecutar(url, "CREATE EXTENSION IF NOT EXISTS vector")
    return url


# ============================================ Base transaccional ============================================

async def test_instalacion_nueva_crea_el_esquema_y_registra_las_migraciones(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert (resultado.base, resultado.aplicadas, resultado.adoptadas, resultado.previas) == ("transaccional", (1, 2, 3), (), ())
    assert TABLAS_TRANSACCIONALES <= await tablas(url)
    assert await versiones_registradas(url) == [(1, "aplicada"), (2, "aplicada"), (3, "aplicada")]
    filas = await consultar(url, "SELECT nombre, checksum FROM igualab_migraciones ORDER BY version")
    assert [f["nombre"] for f in filas] == ["documentos_etapa_4a", "documentos_coordinador", "operaciones_ingesta"]
    assert [f["checksum"].strip() for f in filas] == [m.checksum() for m in ESQUEMA_TRANSACCIONAL.migraciones]


async def test_el_esquema_migrado_es_identico_al_que_generan_los_modelos(url_pg_aislado):
    migrada = await base_transaccional_nueva(url_pg_aislado)
    await migrar(migrada, ESQUEMA_TRANSACCIONAL)
    de_modelos = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(de_modelos, generar_ddl())
    await ejecutar(de_modelos, generar_ddl_operaciones())
    objetos = ("documentos", "operaciones_ingesta")
    assert await esquema_de(migrada, objetos) == await esquema_de(de_modelos, objetos)


async def test_segundo_arranque_no_ejecuta_ninguna_migracion(url_pg_aislado, monkeypatch):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    antes = await consultar(url, "SELECT version, aplicada_en, xmin::text AS xmin FROM igualab_migraciones ORDER BY version")
    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)
    monkeypatch.setattr(motor, "_registrar", no_debe_ejecutarse)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.sin_cambios and resultado.previas == (1, 2, 3) and resultado.aplicadas == () == resultado.adoptadas
    assert await consultar(url, "SELECT version, aplicada_en, xmin::text AS xmin FROM igualab_migraciones ORDER BY version") == antes


async def test_actualiza_una_instalacion_de_la_etapa_4a_sin_perder_datos(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(url, SQL_4A.read_text(encoding="utf-8"))  # tabla creada a mano con el DDL de la Etapa 4A
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]  # los crea el helper (módulos existentes)
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    documento_id = uuid.uuid4()
    await ejecutar(
        url,
        "INSERT INTO documentos (id, ambiente, empresa_id, anio, tipo, sector, nombre_archivo, sha256, tamano_bytes, usuario_id,"
        " clave_original, ejecucion_token, ejecucion_vigente_hasta) VALUES ($1, 'development', $2, 2025, 'MEMORIA_ANUAL', 'MINERIA',"
        " 'viejo.md', $3, 10, $4, 'development/documentos/x/original.md', $5, now() + interval '1 hour')",
        documento_id, empresa, "a" * 64, usuario, uuid.uuid4(),
    )

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert (resultado.adoptadas, resultado.aplicadas) == ((1,), (2, 3))  # la 0001 se ADOPTA tras verificar su esquema
    assert await versiones_registradas(url) == [(1, "adoptada"), (2, "aplicada"), (3, "aplicada")]
    (fila,) = await consultar(url, "SELECT nombre_archivo, etapa_actual, fragmentos_procesados, advertencias::text AS adv FROM documentos")
    assert (fila["nombre_archivo"], fila["etapa_actual"], fila["fragmentos_procesados"], fila["adv"]) == ("viejo.md", "RESERVADO", 0, "[]")
    nuevo = await base_transaccional_nueva(url_pg_aislado)
    await migrar(nuevo, ESQUEMA_TRANSACCIONAL)
    objetos = ("documentos", "operaciones_ingesta")
    assert await esquema_de(url, objetos) == await esquema_de(nuevo, objetos)  # idéntico a una instalación nueva


async def test_adopta_una_instalacion_manual_completa_solo_si_el_esquema_coincide(url_pg_aislado, monkeypatch):
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(url, generar_ddl())  # lo que habría dejado aplicar a mano 001 (+ coordinador)
    await ejecutar(url, generar_ddl_operaciones())  # y 003
    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)  # adoptar NO ejecuta SQL de migración
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.adoptadas == (1, 2, 3) and resultado.aplicadas == ()
    assert await versiones_registradas(url) == [(1, "adoptada"), (2, "adoptada"), (3, "adoptada")]
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios


@pytest.mark.parametrize(
    "alteracion,diferencia",
    [
        ("ALTER TABLE documentos ADD COLUMN extra INTEGER", ("columnas_sobrantes", ["extra"])),
        ("ALTER TABLE documentos DROP COLUMN tamano_bytes", ("columnas_faltantes", ["tamano_bytes"])),
        ("ALTER TABLE documentos ALTER COLUMN nombre_archivo TYPE VARCHAR(100)", ("columnas_distintas", ["nombre_archivo"])),
        ("ALTER TABLE documentos ALTER COLUMN motivo_fallo SET NOT NULL", ("columnas_distintas", ["motivo_fallo"])),
        ("DROP INDEX uq_documentos_sha256_activo", ("indices_faltantes", ["uq_documentos_sha256_activo"])),
        ("ALTER TABLE documentos DROP CONSTRAINT ck_documentos_anio_minimo", ("restricciones_faltantes", ["ck_documentos_anio_minimo"])),
    ],
)
async def test_una_tabla_que_existe_pero_difiere_no_se_adopta_ni_se_modifica(url_pg_aislado, alteracion, diferencia):
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(url, generar_ddl())
    await ejecutar(url, alteracion)
    antes = await esquema_de(url, ("documentos",))
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and error.value.base == "transaccional"
    assert error.value.detalles["objeto"] == "documentos" and error.value.detalles["diferencias"][diferencia[0]] == diferencia[1]
    assert await esquema_de(url, ("documentos",)) == antes  # no se tocó
    assert "operaciones_ingesta" not in await tablas(url)
    assert await consultar(url, "SELECT 1 FROM igualab_migraciones") == []  # nada quedó registrado


async def test_una_tabla_con_el_mismo_nombre_pero_otra_estructura_es_un_conflicto(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(url, "CREATE TABLE documentos (id UUID PRIMARY KEY)")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH"
    assert "columnas_faltantes" in error.value.detalles["diferencias"]
    assert await tablas(url) >= {"documentos"} and "operaciones_ingesta" not in await tablas(url)


async def test_una_migracion_a_medias_se_rechaza(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(url, generar_ddl())
    # La 0001 y la 0002 tocan solo `documentos`; para «a medias» se usa una migración que toca dos objetos.
    esquema = EsquemaBase(
        "transaccional", (motor.Migracion(1, "uno", "0001_documentos_etapa_4a.sql", ("documentos", "operaciones_ingesta"), carpeta="transaccional"),),
        firmas={"documentos": {1: motor.firmas_versionadas()["transaccional"]["documentos"][2]},
                "operaciones_ingesta": {1: motor.firmas_versionadas()["transaccional"]["operaciones_ingesta"][3]}},
    )
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, esquema)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and "a medias" in error.value.mensaje


async def test_un_archivo_de_migracion_editado_despues_de_aplicarse_se_detecta(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    await ejecutar(url, "UPDATE igualab_migraciones SET checksum = repeat('0', 64) WHERE version = 2")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_CHECKSUM_MISMATCH" and error.value.detalles == {"version": 2}


async def test_lo_registrado_debe_seguir_existiendo(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    await ejecutar(url, "DROP TABLE operaciones_ingesta")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and error.value.detalles["objeto"] == "operaciones_ingesta"


async def test_sin_los_prerrequisitos_no_se_crea_nada(url_pg_aislado):
    url = await base_vacia(url_pg_aislado)
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_PREREQUISITE_MISSING"
    assert set(error.value.detalles["faltan"]) == {"tabla:usuarios", "tabla:empresas", "tipo:sector_empresa"}
    assert await tablas(url) == set()  # ni siquiera el registro


async def test_las_migraciones_de_una_version_mas_nueva_se_respetan(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    await ejecutar(url, "INSERT INTO igualab_migraciones (version, nombre, checksum, origen) VALUES (99, 'futura', repeat('a', 64), 'aplicada')")
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)  # una aplicación más vieja (rollback de despliegue) sigue funcionando
    assert resultado.desconocidas == (99,) and resultado.sin_cambios


# ============================================ Concurrencia y bloqueo ============================================

async def test_varias_instancias_a_la_vez_aplican_cada_migracion_exactamente_una_vez(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    resultados = await asyncio.gather(*(migrar(url, ESQUEMA_TRANSACCIONAL) for _ in range(5)))
    assert sorted(v for r in resultados for v in r.aplicadas) == [1, 2, 3]  # cada versión en UNA sola instancia
    assert sum(1 for r in resultados if r.sin_cambios) == 4
    assert await versiones_registradas(url) == [(1, "aplicada"), (2, "aplicada"), (3, "aplicada")]
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios


async def test_el_bloqueo_es_por_base_y_se_agota_con_error_claro(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    otra = await base_transaccional_nueva(url_pg_aislado)
    retenedor = await conectar(url)
    try:
        await retenedor.fetchval("SELECT pg_advisory_lock($1)", motor._clave_candado("transaccional"))
        with pytest.raises(ErrorMigracion) as error:
            await migrar(url, ESQUEMA_TRANSACCIONAL, espera_candado=0.6)
        assert error.value.codigo == "MIGRATION_LOCK_TIMEOUT"
        assert "igualab_migraciones" not in await tablas(url) and "documentos" not in await tablas(url)  # nada se aplicó
        # Otra BASE (otro candado, otra sesión) no se bloquea.
        assert (await migrar(otra, ESQUEMA_TRANSACCIONAL, espera_candado=0.6)).aplicadas == (1, 2, 3)
    finally:
        await retenedor.close()
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).aplicadas == (1, 2, 3)  # liberado: procede


async def test_el_bloqueo_se_libera_aunque_la_migracion_falle(url_pg_aislado, tmp_path):
    url = await base_transaccional_nueva(url_pg_aislado)
    mala = EsquemaBase("transaccional", (migracion_de_texto(tmp_path, 1, "mala", "SELECT 1/0;", ("x",)),))
    with pytest.raises(ErrorMigracion):
        await migrar(url, mala)
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL, espera_candado=1)).aplicadas == (1, 2, 3)


# ============================================ Rollback y tipos de migración ============================================

async def test_un_fallo_a_mitad_de_una_migracion_la_deshace_por_completo(url_pg_aislado, tmp_path):
    url = await base_transaccional_nueva(url_pg_aislado)
    esquema = EsquemaBase("transaccional", (
        migracion_de_texto(tmp_path, 1, "buena", "CREATE TABLE prueba_a (id INTEGER PRIMARY KEY); INSERT INTO prueba_a VALUES (1);", ("prueba_a",)),
        migracion_de_texto(tmp_path, 2, "mala", "CREATE TABLE prueba_b (id INTEGER); INSERT INTO prueba_b VALUES (1); CREATE TABLE prueba_b (x INTEGER);", ("prueba_b",)),
        migracion_de_texto(tmp_path, 3, "nunca", "CREATE TABLE prueba_c (id INTEGER);", ("prueba_c",)),
    ))
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, esquema)
    assert error.value.codigo == "MIGRATION_FAILED" and error.value.detalles == {"version": 2, "sqlstate": "42P07"}
    assert "prueba_a" in await tablas(url)  # la anterior, ya confirmada, se conserva
    assert not {"prueba_b", "prueba_c"} & await tablas(url)  # la fallida se deshizo entera; la siguiente no corrió
    assert await versiones_registradas(url) == [(1, "aplicada")]  # el registro solo tiene lo confirmado


async def test_el_fallo_de_la_migracion_real_deja_los_datos_previos_intactos(url_pg_aislado, tmp_path):
    url = await base_transaccional_nueva(url_pg_aislado)
    await ejecutar(url, SQL_4A.read_text(encoding="utf-8"))
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = await consultar(url, "SELECT id FROM empresas LIMIT 1")
    await ejecutar(
        url,
        "INSERT INTO documentos (id, ambiente, empresa_id, anio, tipo, sector, nombre_archivo, sha256, tamano_bytes, usuario_id,"
        " clave_original, ejecucion_token, ejecucion_vigente_hasta) VALUES (gen_random_uuid(), 'development', $1, 2025, 'MEMORIA_ANUAL',"
        " 'MINERIA', 'dato.md', $2, 10, $3, 'k/original.md', gen_random_uuid(), now() + interval '1 hour')",
        empresa[0]["id"], "b" * 64, usuario,
    )
    # Una restricción homónima ya existente hace fallar la 0002 DESPUÉS de añadir columnas.
    await ejecutar(url, "ALTER TABLE documentos ADD CONSTRAINT ck_documentos_etapa_valida CHECK (true)")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    # El esquema ya no coincide con ninguna versión conocida (restricción extra): se informa sin modificar nada.
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH"
    columnas = {c["column_name"] for c in await consultar(url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'documentos'")}
    assert "analisis" not in columnas
    assert (await consultar(url, "SELECT nombre_archivo FROM documentos"))[0]["nombre_archivo"] == "dato.md"


async def test_una_migracion_no_transaccional_se_ejecuta_fuera_de_transaccion_y_se_registra(url_pg_aislado, tmp_path):
    url = await base_transaccional_nueva(url_pg_aislado)
    esquema = EsquemaBase("transaccional", (
        migracion_de_texto(tmp_path, 1, "tabla", "CREATE TABLE prueba_i (id INTEGER, valor INTEGER);", ("prueba_i",)),
        migracion_de_texto(tmp_path, 2, "indice", "CREATE INDEX CONCURRENTLY ix_prueba_i ON prueba_i (valor);", (), transaccional=False),
    ))
    resultado = await migrar(url, esquema)
    assert resultado.aplicadas == (1, 2)
    assert any(f["indexname"] == "ix_prueba_i" for f in await consultar(url, "SELECT indexname FROM pg_indexes WHERE tablename = 'prueba_i'"))
    # La misma sentencia marcada como transaccional falla (por eso existe la opción) y no deja registro.
    url2 = await base_transaccional_nueva(url_pg_aislado)
    mal = EsquemaBase("transaccional", (
        migracion_de_texto(tmp_path, 1, "tabla", "CREATE TABLE prueba_i (id INTEGER, valor INTEGER);", ("prueba_i",)),
        migracion_de_texto(tmp_path, 2, "indice", "CREATE INDEX CONCURRENTLY ix_prueba_i ON prueba_i (valor);", ()),
    ))
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url2, mal)
    assert error.value.detalles["version"] == 2 and await versiones_registradas(url2) == [(1, "aplicada")]


async def test_versiones_duplicadas_o_desordenadas_se_rechazan(url_pg_aislado, tmp_path):
    a = migracion_de_texto(tmp_path, 1, "a", "SELECT 1;", ("a",))
    with pytest.raises(ValueError):
        await migrar("postgresql+asyncpg://x@127.0.0.1:1/x", EsquemaBase("t", (a, a)))


# ============================================ Conexión y credenciales ============================================

@pytest.mark.parametrize("url", [None, "", "   ", "no es una url", "mysql://u:p@h/db"])
async def test_sin_url_valida_se_informa_sin_conectar(url):
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_DB_NOT_CONFIGURED"


async def test_si_la_base_no_responde_el_error_no_filtra_la_url_ni_las_credenciales():
    with pytest.raises(ErrorMigracion) as error:
        await migrar("postgresql+asyncpg://usuario_secreto:CLAVE-SECRETA@127.0.0.1:1/base_secreta", ESQUEMA_TRANSACCIONAL, timeout_conexion=2)
    assert error.value.codigo == "MIGRATION_DB_UNAVAILABLE"
    texto = f"{error.value.mensaje} {error.value.detalles} {error.value!r}"
    assert not re.search(r"SECRET|127\.0\.0\.1|base_secreta", texto)


# ============================================ Base vectorial / pgvector ============================================

async def test_vectorial_instalacion_nueva_crea_la_extension_y_el_esquema(url_pgvector_aislado):
    url = await vectorial_nueva(url_pgvector_aislado)
    assert (await consultar(url, "SELECT 1 FROM pg_extension WHERE extname = 'vector'")) == []
    resultado = await migrar(url, ESQUEMA_VECTORIAL)
    assert (resultado.base, resultado.aplicadas) == ("vectorial", (1, 2))
    assert (await consultar(url, "SELECT 1 FROM pg_extension WHERE extname = 'vector'")) != []
    assert TABLAS_VECTORIALES <= await tablas(url)
    assert (await consultar(url, "SELECT count(*) AS n FROM fragmentos_consultables"))[0]["n"] == 0  # la vista existe
    assert await versiones_registradas(url) == [(1, "aplicada"), (2, "aplicada")]


async def test_vectorial_segundo_arranque_no_ejecuta_sql_de_migracion(url_pgvector_aislado, monkeypatch):
    url = await vectorial_nueva(url_pgvector_aislado)
    await migrar(url, ESQUEMA_VECTORIAL)
    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)
    assert (await migrar(url, ESQUEMA_VECTORIAL)).sin_cambios


async def test_vectorial_adopta_una_instalacion_manual_verificada(url_pgvector_aislado, monkeypatch):
    url = await vectorial_nueva(url_pgvector_aislado, extension=True)
    for m in ESQUEMA_VECTORIAL.migraciones:
        await ejecutar(url, m.sql())
    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)
    resultado = await migrar(url, ESQUEMA_VECTORIAL)
    assert resultado.adoptadas == (1, 2) and await versiones_registradas(url) == [(1, "adoptada"), (2, "adoptada")]


async def test_vectorial_con_una_dimension_distinta_no_se_adopta(url_pgvector_aislado):
    url = await vectorial_nueva(url_pgvector_aislado, extension=True)
    for m in ESQUEMA_VECTORIAL.migraciones:
        await ejecutar(url, m.sql())
    await ejecutar(url, "DROP VIEW fragmentos_consultables; ALTER TABLE fragmentos_documento DROP COLUMN embedding;"
                        " ALTER TABLE fragmentos_documento ADD COLUMN embedding vector(768)")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_VECTORIAL)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH"
    assert await consultar(url, "SELECT 1 FROM igualab_migraciones") == []


async def test_vectorial_concurrencia(url_pgvector_aislado):
    url = await vectorial_nueva(url_pgvector_aislado)
    resultados = await asyncio.gather(*(migrar(url, ESQUEMA_VECTORIAL) for _ in range(4)))
    assert sorted(v for r in resultados for v in r.aplicadas) == [1, 2]
    assert await versiones_registradas(url) == [(1, "aplicada"), (2, "aplicada")]


async def test_vectorial_rollback_ante_fallo(url_pgvector_aislado, tmp_path):
    url = await vectorial_nueva(url_pgvector_aislado)
    esquema = EsquemaBase("vectorial", (
        migracion_de_texto(tmp_path, 1, "buena", "CREATE TABLE v_a (e vector(3));", ("v_a",)),
        migracion_de_texto(tmp_path, 2, "mala", "CREATE TABLE v_b (e vector(3)); CREATE TABLE v_b (e vector(3));", ("v_b",)),
    ), requiere_pgvector=True)
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, esquema)
    assert error.value.codigo == "MIGRATION_FAILED" and error.value.detalles["version"] == 2
    assert "v_a" in await tablas(url) and "v_b" not in await tablas(url)
    assert await versiones_registradas(url) == [(1, "aplicada")]


async def test_sin_pgvector_instalado_en_el_servidor_se_informa_el_requisito(url_pg_aislado):
    """El PostgreSQL local de las pruebas no trae pgvector: la aplicación no puede crear la extensión y NO finge haberla creado."""
    url = await base_vacia(url_pg_aislado, "sinvector")
    disponible = await consultar(url, "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    if disponible:
        pytest.skip("Este PostgreSQL local sí tiene pgvector disponible: no se puede probar la ausencia del paquete.")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_VECTORIAL)
    assert error.value.codigo == "PGVECTOR_REQUIRED" and error.value.base == "vectorial"
    assert "CREATE EXTENSION vector" in error.value.mensaje and "instalar pgvector" in error.value.mensaje
    assert error.value.detalles["sqlstate"]
    assert await tablas(url) == set()  # no se creó ni el registro


async def test_sin_permiso_para_crear_la_extension_se_informa_el_requisito(url_pgvector_aislado):
    servidor = url_pgvector_aislado
    rol = f"sin_permiso_{uuid.uuid4().hex[:8]}"
    await ejecutar(servidor, f"CREATE ROLE {rol} LOGIN NOSUPERUSER PASSWORD 'x'")
    nombre = f"sp_{uuid.uuid4().hex[:10]}"
    await ejecutar(servidor, f'CREATE DATABASE "{nombre}" OWNER {rol}')
    admin_url = url_de_base(servidor, nombre)
    con_rol = admin_url.replace("pruebas@", f"{rol}:x@", 1)
    # ¿Puede este rol crear la extensión por ser «trusted»? Si sí, la ausencia de permiso no se puede simular aquí.
    conexion = await asyncpg.connect(re.sub(r"^postgresql\+asyncpg://", "postgresql://", con_rol))
    try:
        await conexion.execute("CREATE EXTENSION vector")
        puede = True
    except asyncpg.PostgresError:
        puede = False
    finally:
        await conexion.close()
    if puede:
        pytest.skip("pgvector es una extensión «trusted» en esta versión: el rol puede crearla; no se puede simular la falta de permiso.")
    with pytest.raises(ErrorMigracion) as error:
        await migrar(con_rol, ESQUEMA_VECTORIAL)
    assert error.value.codigo == "PGVECTOR_REQUIRED" and error.value.detalles["sqlstate"] == "42501"
    assert "x@" not in f"{error.value.detalles} {error.value.mensaje}"  # sin credenciales
    assert await tablas(admin_url) == set()


async def test_la_extension_ya_instalada_no_requiere_permiso_para_crearla(url_pgvector_aislado):
    url = await vectorial_nueva(url_pgvector_aislado, extension=True)
    assert (await migrar(url, ESQUEMA_VECTORIAL)).aplicadas == (1, 2)


# ============================================ Punto de entrada del arranque ============================================

async def test_asegurar_esquema_usa_cada_url_sin_fallback_entre_ellas(url_pg_aislado, url_pgvector_aislado, monkeypatch):
    transaccional = await base_transaccional_nueva(url_pg_aislado)
    vectorial = await vectorial_nueva(url_pgvector_aislado)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", transaccional)
    monkeypatch.setattr(catalogo.settings, "VECTOR_DATABASE_URL", vectorial)
    resultados = await catalogo.asegurar_esquema_ingesta()
    assert [r.base for r in resultados] == ["transaccional", "vectorial"]  # primero la transaccional
    assert TABLAS_TRANSACCIONALES <= await tablas(transaccional) and TABLAS_VECTORIALES <= await tablas(vectorial)
    assert "fragmentos_documento" not in await tablas(transaccional)  # nada de lo vectorial fue a la transaccional
    assert "documentos" not in await tablas(vectorial)
    resultados = await catalogo.asegurar_esquema_ingesta()
    assert all(r.sin_cambios for r in resultados)


async def test_sin_vector_database_url_no_hay_fallback_a_la_transaccional(url_pg_aislado, monkeypatch):
    transaccional = await base_transaccional_nueva(url_pg_aislado)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", transaccional)
    monkeypatch.setattr(catalogo.settings, "VECTOR_DATABASE_URL", None)
    with pytest.raises(ErrorMigracion) as error:
        await catalogo.asegurar_esquema_ingesta()
    assert error.value.codigo == "MIGRATION_DB_NOT_CONFIGURED" and error.value.base == "vectorial"
    assert "fragmentos_documento" not in await tablas(transaccional)
    assert "vector" not in {f["extname"] for f in await consultar(transaccional, "SELECT extname FROM pg_extension")}


# ============================================ Contenido de los archivos ============================================

def test_la_migracion_0001_transaccional_es_el_ddl_congelado_de_la_etapa_4a():
    def cuerpo(texto: str) -> str:
        return "\n".join(l for l in texto.replace("\r\n", "\n").splitlines() if not l.startswith("--") and l.strip())

    migracion = ESQUEMA_TRANSACCIONAL.migraciones[0]
    assert cuerpo(migracion.sql()) == cuerpo(SQL_4A.read_text(encoding="utf-8"))


def test_los_sql_se_empaquetan_dentro_de_app_para_la_imagen_docker():
    # El Dockerfile copia solo `app/` y `scripts/`: las migraciones deben vivir en `app/`.
    assert motor.DIRECTORIO == RAIZ / "app" / "migraciones"
    for esquema in (ESQUEMA_TRANSACCIONAL, ESQUEMA_VECTORIAL):
        for m in esquema.migraciones:
            assert (motor.DIRECTORIO / m.carpeta / m.archivo).is_file()
    assert not (RAIZ / "db").exists()

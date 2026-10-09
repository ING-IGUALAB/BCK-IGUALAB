"""Restricciones NOT NULL en la firma del esquema (PostgreSQL 18 las guarda en `pg_constraint`; las versiones anteriores no).

Causa del bloqueo `MIGRATION_SCHEMA_MISMATCH` en development: la firma de la tabla `documentos` incluía TODAS las filas de
`pg_constraint`. En PostgreSQL 18 cada columna NOT NULL añade una restricción `documentos_<columna>_not_null` (contype 'n') que las firmas
versionadas (generadas con PostgreSQL ≤17) no tienen, así que la tabla de la Etapa 4A no coincidía con ninguna versión y no se adoptaba.

Se prueba (1) con respuestas simuladas del catálogo, en las distintas representaciones, y (2) contra PostgreSQL REAL y AISLADO:
el clúster local (`initdb`) y el contenedor pgvector desechable (la versión sale de `IGUALAB_TEST_PGVECTOR_IMAGE`; con
`pgvector/pgvector:pg18` las restricciones NOT NULL existen de verdad). Si no están disponibles se OMITEN (`pytest -rs`).
"""
import uuid
from pathlib import Path

import pytest

from app.migraciones import motor
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.motor import ErrorMigracion, _version_del_objeto, diferencias, firma_de_objeto, firmas_versionadas, migrar
from scripts.generar_ddl_documentos import generar_ddl
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar, versiones_registradas

SQL_4A = Path(__file__).parent / "datos" / "documentos_etapa_4a.sql"
FIRMAS_DOCUMENTOS = firmas_versionadas()["transaccional"]["documentos"]


# ============================================ 1. Respuestas simuladas del catálogo ============================================

class CatalogoSimulado:
    """Responde a las tres consultas de `firma_de_objeto` con lo que devolvería PostgreSQL para la firma `base` más `extra`."""

    def __init__(self, base: dict, restricciones_extra=(), no_nulo_forzado=None):
        self.base, self.extra, self.forzado = base, list(restricciones_extra), no_nulo_forzado or {}

    async def fetch(self, sql, objeto):
        if "pg_attribute" in sql:
            return [{"nombre": n, "tipo": t, "no_nulo": self.forzado.get(n, nn)} for n, t, nn in self.base["columnas"]]
        if "pg_constraint" in sql:
            filas = [{"nombre": n, "tipo": "c", "validada": True} for n in self.base["restricciones"]]
            return sorted(filas + self.extra, key=lambda f: f["nombre"])
        return [{"nombre": n, "unico": u, "parcial": p} for n, u, p in self.base["indices"]]


def restriccion_no_nula(columna: str, nombre: str | None = None, validada: bool = True) -> dict:
    return {"nombre": nombre or f"documentos_{columna}_not_null", "tipo": "n", "validada": validada}


def no_nulas(firma: dict) -> list[str]:
    return [n for n, _, no_nulo in firma["columnas"] if no_nulo]


async def firma_simulada(catalogo: CatalogoSimulado) -> dict:
    return await firma_de_objeto(catalogo, "documentos")


async def test_pg_17_o_anterior_sin_restricciones_not_null_coincide_con_la_version_1():
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[1]))
    assert firma == FIRMAS_DOCUMENTOS[1] and _version_del_objeto(firma, FIRMAS_DOCUMENTOS) == 1


async def test_pg_18_con_restricciones_documentos_columna_not_null_coincide_con_la_version_1():
    extra = [restriccion_no_nula(c) for c in no_nulas(FIRMAS_DOCUMENTOS[1])]
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[1], extra))
    assert len(extra) > 10 and firma == FIRMAS_DOCUMENTOS[1] and _version_del_objeto(firma, FIRMAS_DOCUMENTOS) == 1


async def test_pg_18_con_restricciones_not_null_con_otros_nombres_tambien_coincide():
    # El nombre de una restricción NOT NULL no cambia su significado: la nulabilidad la decide la columna (attnotnull).
    extra = [restriccion_no_nula(c, nombre=f"nn_{i}") for i, c in enumerate(no_nulas(FIRMAS_DOCUMENTOS[2]))]
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[2], extra))
    assert _version_del_objeto(firma, FIRMAS_DOCUMENTOS) == 2


async def test_la_version_2_tambien_se_reconoce_con_y_sin_restricciones_not_null():
    sin = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[2]))
    con = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[2], [restriccion_no_nula(c) for c in no_nulas(FIRMAS_DOCUMENTOS[2])]))
    assert sin == con == FIRMAS_DOCUMENTOS[2] and _version_del_objeto(con, FIRMAS_DOCUMENTOS) == 2


async def test_una_restriccion_not_null_no_validada_no_se_ignora():
    extra = [restriccion_no_nula("nombre_archivo", validada=False)]
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[1], extra))
    assert _version_del_objeto(firma, FIRMAS_DOCUMENTOS) is None
    assert diferencias(firma, FIRMAS_DOCUMENTOS[1]) == {"restricciones_sobrantes": ["documentos_nombre_archivo_not_null"]}


async def test_una_diferencia_real_de_nulabilidad_se_detecta_aunque_existan_restricciones_not_null():
    nulable = no_nulas(FIRMAS_DOCUMENTOS[1])[0]
    extra = [restriccion_no_nula(c) for c in no_nulas(FIRMAS_DOCUMENTOS[1])]
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[1], extra, no_nulo_forzado={nulable: False}))
    assert _version_del_objeto(firma, FIRMAS_DOCUMENTOS) is None
    assert diferencias(firma, FIRMAS_DOCUMENTOS[1]) == {"columnas_distintas": [nulable]}


async def test_una_columna_que_deberia_ser_nulable_pero_es_not_null_se_detecta():
    nulable = next(n for n, _, no_nulo in FIRMAS_DOCUMENTOS[1]["columnas"] if not no_nulo)
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[1], [restriccion_no_nula(nulable)], {nulable: True}))
    assert _version_del_objeto(firma, FIRMAS_DOCUMENTOS) is None
    assert diferencias(firma, FIRMAS_DOCUMENTOS[1]) == {"columnas_distintas": [nulable]}


@pytest.mark.parametrize("tipo", ["c", "u", "f", "p", "x"])
async def test_las_demas_restricciones_desconocidas_no_se_ignoran(tipo):
    extra = [{"nombre": "restriccion_ajena", "tipo": tipo, "validada": True}]
    firma = await firma_simulada(CatalogoSimulado(FIRMAS_DOCUMENTOS[1], extra))
    assert _version_del_objeto(firma, FIRMAS_DOCUMENTOS) is None
    assert diferencias(firma, FIRMAS_DOCUMENTOS[1]) == {"restricciones_sobrantes": ["restriccion_ajena"]}


async def test_una_restriccion_que_falta_se_detecta():
    base = {**FIRMAS_DOCUMENTOS[1], "restricciones": FIRMAS_DOCUMENTOS[1]["restricciones"][1:]}
    firma = await firma_simulada(CatalogoSimulado(base, [restriccion_no_nula(c) for c in no_nulas(base)]))
    assert _version_del_objeto(firma, FIRMAS_DOCUMENTOS) is None
    assert diferencias(firma, FIRMAS_DOCUMENTOS[1]) == {"restricciones_faltantes": [FIRMAS_DOCUMENTOS[1]["restricciones"][0]]}


# ============================================ 2. PostgreSQL real y aislado ============================================

@pytest.fixture(params=["local", "contenedor"])
def url_servidor(request):
    """Servidor aislado donde correr: el clúster local o el contenedor desechable (cuya versión elige el entorno)."""
    return request.getfixturevalue("url_pg_aislado" if request.param == "local" else "url_pgvector_aislado")


async def version_servidor(url: str) -> int:
    return int((await consultar(url, "SHOW server_version_num"))[0]["server_version_num"])


async def base_4a(url_servidor: str) -> str:
    url = await base_transaccional_nueva(url_servidor)
    await ejecutar(url, SQL_4A.read_text(encoding="utf-8"))  # tabla creada a mano con el DDL de la Etapa 4A
    return url


async def insertar_documento_antiguo(url: str) -> uuid.UUID:
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    documento_id = uuid.uuid4()
    await ejecutar(
        url,
        "INSERT INTO documentos (id, ambiente, empresa_id, anio, tipo, sector, nombre_archivo, sha256, tamano_bytes, usuario_id,"
        " clave_original, ejecucion_token, ejecucion_vigente_hasta) VALUES ($1, 'development', $2, 2025, 'MEMORIA_ANUAL', 'MINERIA',"
        " 'viejo.md', $3, 10, $4, 'development/documentos/x/original.md', $5, now() + interval '1 hour')",
        documento_id, empresa, "a" * 64, usuario, uuid.uuid4(),
    )
    return documento_id


async def firma_real(url: str) -> dict:
    conexion = await conectar(url)
    try:
        return await firma_de_objeto(conexion, "documentos")
    finally:
        await conexion.close()


async def test_la_tabla_de_la_etapa_4a_se_reconoce_como_version_1_en_cualquier_version_de_postgresql(url_servidor):
    url = await base_4a(url_servidor)
    if await version_servidor(url) >= 180000:  # la prueba solo vale si el servidor REALMENTE guarda NOT NULL en pg_constraint
        assert (await consultar(url, "SELECT count(*) AS n FROM pg_constraint WHERE contype = 'n' AND conrelid = 'documentos'::regclass"))[0]["n"] > 10
    assert await firma_real(url) == FIRMAS_DOCUMENTOS[1]
    assert _version_del_objeto(await firma_real(url), FIRMAS_DOCUMENTOS) == 1


async def test_actualiza_la_etapa_4a_conservando_las_filas_y_el_segundo_arranque_no_repite_nada(url_servidor, monkeypatch):
    url = await base_4a(url_servidor)
    documento_id = await insertar_documento_antiguo(url)

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert (resultado.adoptadas, resultado.aplicadas) == ((1,), (2, 3))  # 0001 adoptada tras verificarla; 0002 y 0003 aplicadas
    assert await versiones_registradas(url) == [(1, "adoptada"), (2, "aplicada"), (3, "aplicada")]
    (fila,) = await consultar(url, "SELECT id, nombre_archivo, etapa_actual, fragmentos_procesados, advertencias::text AS adv FROM documentos")
    assert (fila["id"], fila["nombre_archivo"], fila["etapa_actual"], fila["fragmentos_procesados"], fila["adv"]) == (
        documento_id, "viejo.md", "RESERVADO", 0, "[]")
    assert await firma_real(url) == FIRMAS_DOCUMENTOS[2]  # ahora es exactamente la versión 2

    nuevo = await base_transaccional_nueva(url_servidor)
    await migrar(nuevo, ESQUEMA_TRANSACCIONAL)
    assert await firma_real(url) == await firma_real(nuevo)  # idéntica a una instalación nueva

    antes = await consultar(url, "SELECT version, aplicada_en, xmin::text AS xmin FROM igualab_migraciones ORDER BY version")

    async def no_debe_ejecutarse(*args, **kwargs):
        raise AssertionError("el segundo arranque no debe aplicar ni registrar nada")

    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)
    monkeypatch.setattr(motor, "_registrar", no_debe_ejecutarse)
    segundo = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert segundo.sin_cambios and segundo.previas == (1, 2, 3)
    assert await consultar(url, "SELECT version, aplicada_en, xmin::text AS xmin FROM igualab_migraciones ORDER BY version") == antes


@pytest.mark.parametrize(
    "alteracion,diferencia,con_fila",
    [
        # Nulabilidad real distinta, en las dos direcciones.
        ("ALTER TABLE documentos ALTER COLUMN nombre_archivo DROP NOT NULL", ("columnas_distintas", ["nombre_archivo"]), True),
        # Con una fila previa (motivo_fallo nulo) PostgreSQL no admite SET NOT NULL: esa alteración solo existe en una tabla vacía.
        ("ALTER TABLE documentos ALTER COLUMN motivo_fallo SET NOT NULL", ("columnas_distintas", ["motivo_fallo"]), False),
        # Restricciones reales distintas.
        ("ALTER TABLE documentos ADD CONSTRAINT ck_ajena CHECK (tamano_bytes < 100)", ("restricciones_sobrantes", ["ck_ajena"]), True),
        ("ALTER TABLE documentos DROP CONSTRAINT ck_documentos_anio_minimo", ("restricciones_faltantes", ["ck_documentos_anio_minimo"]), True),
    ],
)
async def test_las_diferencias_reales_de_nulabilidad_y_restricciones_siguen_rechazandose(url_servidor, alteracion, diferencia, con_fila):
    url = await base_4a(url_servidor)
    if con_fila:
        await insertar_documento_antiguo(url)
    await ejecutar(url, alteracion)
    antes = await firma_real(url)

    with pytest.raises(ErrorMigracion) as error:
        await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and error.value.detalles["objeto"] == "documentos"
    # La única diferencia respecto a la última versión incluye la real; las NOT NULL propias de PostgreSQL 18 no aparecen como sobrantes.
    assert not any("_not_null" in n for n in error.value.detalles["diferencias"].get("restricciones_sobrantes", []))
    ultima = diferencias(antes, FIRMAS_DOCUMENTOS[2])
    assert ultima == error.value.detalles["diferencias"]
    assert await firma_real(url) == antes  # no se tocó nada
    assert (await consultar(url, "SELECT count(*) AS n FROM documentos"))[0]["n"] == int(con_fila)  # los datos siguen ahí
    assert await consultar(url, "SELECT 1 FROM igualab_migraciones") == []  # nada quedó registrado
    # El diagnóstico contra la versión 1 también marca la diferencia real (y solo esa).
    contra_v1 = diferencias(antes, FIRMAS_DOCUMENTOS[1])
    assert contra_v1.get(diferencia[0]) == diferencia[1]
    assert all(k == diferencia[0] for k in contra_v1)


async def test_una_instalacion_manual_completa_se_adopta_en_cualquier_version(url_servidor, monkeypatch):
    from scripts.generar_ddl_documentos import generar_ddl_operaciones

    url = await base_transaccional_nueva(url_servidor)
    await ejecutar(url, generar_ddl())
    await ejecutar(url, generar_ddl_operaciones())

    async def no_debe_ejecutarse(*args, **kwargs):
        raise AssertionError("adoptar no ejecuta SQL de migración")

    monkeypatch.setattr(motor, "_aplicar", no_debe_ejecutarse)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.adoptadas == (1, 2, 3) and resultado.aplicadas == ()

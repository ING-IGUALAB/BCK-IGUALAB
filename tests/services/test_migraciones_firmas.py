"""Las firmas versionadas (`app/migraciones/firmas.json`) son las del esquema que producen las migraciones REALES.

Se aplican las migraciones una a una en una base desechable (PostgreSQL `initdb` aislado; pgvector en Docker aislado) y se calcula,
tras cada una, la firma de los objetos que toca. Si el archivo no coincide, esta prueba falla: así una migración nueva o editada no
puede dejar firmas desactualizadas (el motor las usa para decidir si ADOPTA un esquema existente).

Regenerar tras agregar una migración:  IGUALAB_REGENERAR_FIRMAS=1 python -m pytest tests/services/test_migraciones_firmas.py
Se OMITEN (no cuentan como ejecutadas) si el PostgreSQL local o Docker/pgvector no están disponibles.
"""
import json
import os

from app.migraciones import motor
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL, ESQUEMA_VECTORIAL
from tests.ayudantes_migraciones import base_transaccional_nueva, base_vacia, conectar

RUTA_FIRMAS = motor.DIRECTORIO / "firmas.json"


async def calcular(url: str, esquema) -> dict:
    """{objeto: {version: firma}} aplicando cada migración en orden sobre `url`."""
    firmas: dict = {}
    conexion = await conectar(url)
    try:
        for m in esquema.migraciones:
            async with conexion.transaction():
                await conexion.execute(m.sql())
            for objeto in m.objetos:
                firmas.setdefault(objeto, {})[str(m.version)] = await motor.firma_de_objeto(conexion, objeto)
    finally:
        await conexion.close()
    return firmas


def canonico(firmas: dict) -> dict:
    return json.loads(json.dumps(firmas, sort_keys=True))


def comparar_o_regenerar(base: str, calculadas: dict) -> None:
    almacenadas = json.loads(RUTA_FIRMAS.read_text(encoding="utf-8"))
    if os.environ.get("IGUALAB_REGENERAR_FIRMAS") == "1":
        almacenadas[base] = canonico(calculadas)
        RUTA_FIRMAS.write_text(json.dumps(almacenadas, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        motor.firmas_versionadas.cache_clear()
        return
    assert canonico(almacenadas.get(base, {})) == canonico(calculadas), (
        f"Las firmas de la base {base} están desactualizadas: regenere con IGUALAB_REGENERAR_FIRMAS=1."
    )


async def test_firmas_de_la_base_transaccional(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    comparar_o_regenerar("transaccional", await calcular(url, ESQUEMA_TRANSACCIONAL))


async def test_firmas_de_la_base_vectorial(url_pgvector_aislado):
    url = await base_vacia(url_pgvector_aislado, "firmas")
    conexion = await conectar(url)
    try:
        await conexion.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await conexion.close()
    comparar_o_regenerar("vectorial", await calcular(url, ESQUEMA_VECTORIAL))


def test_el_catalogo_y_las_firmas_declaran_lo_mismo():
    firmas = motor.firmas_versionadas()
    for esquema in (ESQUEMA_TRANSACCIONAL, ESQUEMA_VECTORIAL):
        for m in esquema.migraciones:
            for objeto in m.objetos:
                assert m.version in firmas[esquema.nombre][objeto], (esquema.nombre, m.version, objeto)
        versiones = [m.version for m in esquema.migraciones]
        assert versiones == sorted(set(versiones))
        assert versiones[0] == 1
        # El archivo de cada migración existe, no abre ni cierra transacciones y es el que numera el catálogo.
        for m in esquema.migraciones:
            texto = m.sql().upper()
            assert not any(linea.strip() in ("BEGIN;", "COMMIT;") for linea in texto.splitlines()), m.archivo
            assert m.archivo.startswith(f"{m.version:04d}_")

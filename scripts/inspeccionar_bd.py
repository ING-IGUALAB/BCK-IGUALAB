"""Inspección SOLO-LECTURA de las bases de datos (transaccional y vectorial).

Muestra las tablas/vistas con sus columnas y nº de filas aproximado. No escribe nada.
Lee la conexión del `.env` de BCK (`DATABASE_URL`, `VECTOR_DATABASE_URL`) — nunca imprime credenciales.

Uso (desde la raíz de BCK-IGUALAB, con el venv activado):
    PYTHONPATH=. python scripts/inspeccionar_bd.py            # resumen
    PYTHONPATH=. python scripts/inspeccionar_bd.py --sql "SELECT count(*) FROM documentos"
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg


def _cargar_env() -> dict:
    valores: dict[str, str] = {}
    try:
        with open(".env", encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if linea and not linea.startswith("#") and "=" in linea:
                    k, v = linea.split("=", 1)
                    valores[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return valores


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _inspeccionar(nombre: str, url: str, sql: str | None) -> None:
    print(f"\n===== {nombre} =====")
    try:
        conn = await asyncio.wait_for(asyncpg.connect(_dsn(url)), timeout=12)
    except Exception as exc:  # noqa: BLE001
        print(f"  no conecta: {type(exc).__name__}")
        return
    try:
        await conn.execute("SET statement_timeout='8s'")
        if sql:
            try:
                for fila in await conn.fetch(sql):
                    print("  ", dict(fila))
            except Exception as exc:  # noqa: BLE001 — la tabla puede no existir en esta base
                print(f"  error en la consulta: {type(exc).__name__}: {exc}")
            return
        vistas = {r["viewname"] for r in await conn.fetch("SELECT viewname FROM pg_views WHERE schemaname='public'")}
        tabs = await conn.fetch(
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
        )
        rel = {
            r["relname"]: int(r["reltuples"])
            for r in await conn.fetch("SELECT relname, reltuples FROM pg_class WHERE relkind IN ('r','v','m','p')")
        }
        for t in tabs:
            tn = t["table_name"]
            cols = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",
                tn,
            )
            marca = "  [VISTA]" if tn in vistas else ""
            print(f"  · {tn}{marca} — {len(cols)} cols · ~{rel.get(tn, '?')} filas")
            print("      " + ", ".join(f"{c['column_name']}:{c['data_type']}" for c in cols))
    finally:
        await conn.close()


def main() -> None:
    env = _cargar_env()
    sql = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--sql":
        sql = sys.argv[2]
    asyncio.run(_inspeccionar("TRANSACCIONAL", env.get("DATABASE_URL", ""), sql))
    asyncio.run(_inspeccionar("VECTORIAL", env.get("VECTOR_DATABASE_URL", ""), sql))


if __name__ == "__main__":
    main()

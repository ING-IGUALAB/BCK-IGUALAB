"""Ayudantes SOLO de pruebas para el motor de migraciones: bases desechables dentro de los clústeres AISLADOS de `conftest.py`
(PostgreSQL `initdb` local para la transaccional; contenedor pgvector desechable para la vectorial). Nunca tocan bases compartidas."""
import re
import uuid

import asyncpg

from app.migraciones.motor import Migracion


def _dsn(url: str, base: str | None = None) -> str:
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", url)
    return dsn.rsplit("/", 1)[0] + f"/{base}" if base else dsn


def url_de_base(url: str, base: str) -> str:
    return url.rsplit("/", 1)[0] + f"/{base}"


async def conectar(url: str) -> asyncpg.Connection:
    return await asyncpg.connect(_dsn(url))


async def base_vacia(url_servidor: str, prefijo: str = "mig") -> str:
    """Crea una base nueva en el servidor y devuelve su URL (con el driver asyncpg)."""
    nombre = f"{prefijo}_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_dsn(url_servidor))
    try:
        await admin.execute(f'CREATE DATABASE "{nombre}"')
    finally:
        await admin.close()
    return url_de_base(url_servidor, nombre)


async def base_transaccional_nueva(url_pg_aislado: str) -> str:
    """Base con los prerrequisitos que crean los módulos existentes (`usuarios`, `empresas`, tipo `sector_empresa`) y SIN
    tablas de ingesta: una instalación nueva."""
    from tests.services.test_documento_ddl_postgres import _crear_base_con_prerequisitos

    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    return url_de_base(url_pg_aislado, esquema.base)


async def ejecutar(url: str, sql: str, *argumentos) -> None:
    conexion = await conectar(url)
    try:
        await conexion.execute(sql, *argumentos)
    finally:
        await conexion.close()


async def consultar(url: str, sql: str, *argumentos) -> list:
    conexion = await conectar(url)
    try:
        return await conexion.fetch(sql, *argumentos)
    finally:
        await conexion.close()


async def tablas(url: str) -> set[str]:
    filas = await consultar(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {f["tablename"] for f in filas}


async def versiones_registradas(url: str) -> list[tuple[int, str]]:
    filas = await consultar(url, "SELECT version, origen FROM igualab_migraciones ORDER BY version")
    return [(f["version"], f["origen"]) for f in filas]


def migracion_de_texto(tmp_path, version: int, nombre: str, sql: str, objetos: tuple[str, ...], **kwargs) -> Migracion:
    """Migración sintética con su SQL en un archivo temporal (para probar fallos y rollback)."""
    carpeta = tmp_path / "sinteticas"
    carpeta.mkdir(exist_ok=True)
    archivo = f"{version:04d}_{nombre}.sql"
    (carpeta / archivo).write_text(sql, encoding="utf-8")
    return Migracion(version, nombre, archivo, objetos, carpeta=str(carpeta), **kwargs)

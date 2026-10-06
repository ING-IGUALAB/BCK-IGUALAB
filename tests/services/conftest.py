"""Fixtures de PostgreSQL AISLADO para las pruebas de concurrencia de la Etapa 4A.

Se crea un clúster TEMPORAL con `initdb` en un directorio de pruebas, escuchando solo en
127.0.0.1 y en un puerto libre, sin heredar ninguna variable `PG*` del entorno y sin
leer `DATABASE_URL`. Se destruye al terminar la sesión. Nunca toca bases compartidas.

Si no se encuentran los binarios de PostgreSQL (o `IGUALAB_SKIP_PG_TESTS=1`), las
pruebas que lo usan se OMITEN con un motivo visible (`pytest -rs`): SQLite o los dobles
no demuestran la concurrencia de PostgreSQL, así que esas pruebas no se dan por pasadas.
Variable opcional `IGUALAB_TEST_PG_BIN`: directorio con `initdb` y `pg_ctl`.
"""
import asyncio
import glob
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.documento_ingesta import Documento  # noqa: F401  (registra la tabla)

_EXE = ".exe" if os.name == "nt" else ""


def _directorio_binarios() -> Path | None:
    candidatos: list[str] = []
    if os.environ.get("IGUALAB_TEST_PG_BIN"):
        candidatos.append(os.environ["IGUALAB_TEST_PG_BIN"])
    encontrado = shutil.which("initdb")
    if encontrado:
        candidatos.append(str(Path(encontrado).parent))
    candidatos += sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
    candidatos += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for candidato in candidatos:
        base = Path(candidato)
        if (base / f"initdb{_EXE}").exists() and (base / f"pg_ctl{_EXE}").exists():
            return base
    return None


def _puerto_libre() -> int:
    with socket.socket() as servidor:
        servidor.bind(("127.0.0.1", 0))
        return servidor.getsockname()[1]


def _entorno_limpio() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("PG")}


def _ejecutar(argumentos: list[str], tiempo: int) -> None:
    # DEVNULL: en Windows, postgres hereda las tuberías y `run` no terminaría.
    subprocess.run(
        argumentos,
        check=True,
        timeout=tiempo,
        env=_entorno_limpio(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _crear_esquema(url: str) -> None:
    motor = create_async_engine(url)
    try:
        async with motor.begin() as conexion:
            await conexion.run_sync(Base.metadata.create_all)
    finally:
        await motor.dispose()


@pytest.fixture(scope="session")
def url_pg_aislado(tmp_path_factory):
    if os.environ.get("IGUALAB_SKIP_PG_TESTS") == "1":
        pytest.skip("IGUALAB_SKIP_PG_TESTS=1: pruebas de PostgreSQL omitidas a petición.")
    binarios = _directorio_binarios()
    if binarios is None:
        pytest.skip("PostgreSQL no disponible (no se encontró initdb/pg_ctl): pruebas de concurrencia NO ejecutadas.")
    directorio = tmp_path_factory.mktemp("pg_aislado")
    datos = directorio / "datos"
    puerto = _puerto_libre()
    opciones = f"-p {puerto} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off"
    if os.name != "nt":
        opciones += f" -c unix_socket_directories={directorio}"
    iniciado = False
    try:
        _ejecutar([str(binarios / f"initdb{_EXE}"), "-D", str(datos), "-U", "pruebas",
                   "--auth=trust", "-E", "UTF8", "--no-sync"], 180)
        _ejecutar([str(binarios / f"pg_ctl{_EXE}"), "-D", str(datos), "-l", str(directorio / "pg.log"),
                   "-o", opciones, "-w", "-t", "90", "start"], 120)
        iniciado = True
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"No se pudo iniciar PostgreSQL aislado ({type(exc).__name__}): pruebas NO ejecutadas.")
    try:
        url = f"postgresql+asyncpg://pruebas@127.0.0.1:{puerto}/postgres"
        from app.config import settings

        assert url != settings.DATABASE_URL  # nunca la base configurada
        asyncio.run(_crear_esquema(url))
        yield url
    finally:
        if iniciado:
            try:
                _ejecutar([str(binarios / f"pg_ctl{_EXE}"), "-D", str(datos), "-m", "immediate", "-w", "stop"], 60)
            except (subprocess.SubprocessError, OSError):
                pass
        shutil.rmtree(directorio, ignore_errors=True)


@pytest_asyncio.fixture
async def fabrica_pg(url_pg_aislado):
    """`async_sessionmaker` sobre el clúster aislado, con las tablas vacías en cada prueba."""
    motor = create_async_engine(url_pg_aislado, pool_size=12, max_overflow=0)
    tablas = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with motor.begin() as conexion:
        await conexion.execute(text(f"TRUNCATE {tablas} CASCADE"))
    try:
        yield async_sessionmaker(motor, expire_on_commit=False)
    finally:
        await motor.dispose()

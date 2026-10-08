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


# ============ PostgreSQL + pgvector DESECHABLE (Etapa 4B) ============
# Un contenedor Docker efímero (sin volumen: datos en tmpfs; `--rm`; solo 127.0.0.1; puerto libre)
# creado desde una imagen que YA debe existir localmente (no se descarga nada durante las pruebas:
# `docker pull pgvector/pgvector:pg16`). No toca el Docker Compose del proyecto, ni `DATABASE_URL`,
# ni `VECTOR_DATABASE_URL`, ni ninguna base compartida. Se destruye al terminar la sesión.
# Si Docker o la imagen no están disponibles (o `IGUALAB_SKIP_PGVECTOR_TESTS=1`), esas pruebas se
# OMITEN con motivo visible (`pytest -rs`) y NO cuentan como ejecutadas; no se sustituyen por mocks.
# Imagen: `IGUALAB_TEST_PGVECTOR_IMAGE` (por defecto `pgvector/pgvector:pg16`).
import uuid as _uuid

import asyncpg

_MIGRACIONES_VECTORIALES = Path(__file__).resolve().parents[2] / "app" / "migraciones" / "vectorial"
_SQL_VECTORIAL = _MIGRACIONES_VECTORIALES / "0001_fragmentos_documento.sql"
# Todas las migraciones vectoriales en orden numérico (0001 fragmentos, 0002 cierres, ...).
_SQL_VECTORIAL_TODOS = sorted(_MIGRACIONES_VECTORIALES.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _docker(argumentos: list[str], tiempo: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argumentos], capture_output=True, text=True, timeout=tiempo, stdin=subprocess.DEVNULL)


async def _esperar_tcp(dsn: str) -> None:
    async with asyncio.timeout(90):
        while True:
            try:
                conexion = await asyncpg.connect(dsn, timeout=3)
            except (OSError, asyncpg.PostgresError, asyncio.TimeoutError):
                await asyncio.sleep(0.5)
                continue
            try:
                await conexion.fetchval("SELECT 1")
                return
            finally:
                await conexion.close()


@pytest.fixture(scope="session")
def url_pgvector_aislado():
    if os.environ.get("IGUALAB_SKIP_PGVECTOR_TESTS") == "1":
        pytest.skip("IGUALAB_SKIP_PGVECTOR_TESTS=1: pruebas con pgvector omitidas a petición.")
    if shutil.which("docker") is None:
        pytest.skip("Docker no está instalado: pruebas con pgvector real NO ejecutadas.")
    imagen = os.environ.get("IGUALAB_TEST_PGVECTOR_IMAGE", "pgvector/pgvector:pg16")
    try:
        servidor = _docker(["version", "--format", "{{.Server.Version}}"], 30)
        if servidor.returncode != 0:
            pytest.skip("El motor de Docker no responde: pruebas con pgvector real NO ejecutadas.")
        if _docker(["image", "inspect", imagen], 30).returncode != 0:
            pytest.skip(f"Falta la imagen local {imagen} (docker pull {imagen}): pruebas con pgvector NO ejecutadas.")
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"Docker no disponible ({type(exc).__name__}): pruebas con pgvector NO ejecutadas.")
    nombre = f"igualab-test-pgvector-{_uuid.uuid4().hex[:8]}"
    creado = False
    try:
        ejecucion = _docker([
            "run", "-d", "--rm", "--name", nombre, "-p", "127.0.0.1::5432",
            "-e", "POSTGRES_USER=pruebas", "-e", "POSTGRES_DB=vectorial", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "--tmpfs", "/var/lib/postgresql/data", imagen,
        ], 120)
        if ejecucion.returncode != 0:
            pytest.skip("No se pudo iniciar el contenedor de pgvector: pruebas NO ejecutadas.")
        creado = True
        puerto = _docker(["port", nombre, "5432/tcp"], 30).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        dsn = f"postgresql://pruebas@127.0.0.1:{puerto}/vectorial"
        try:
            asyncio.run(_esperar_tcp(dsn))
        except (TimeoutError, asyncio.TimeoutError):
            pytest.skip("El contenedor de pgvector no aceptó conexiones a tiempo: pruebas NO ejecutadas.")
        url = f"postgresql+asyncpg://pruebas@127.0.0.1:{puerto}/vectorial"
        from app.config import settings

        assert url not in (settings.DATABASE_URL, settings.VECTOR_DATABASE_URL)  # nunca una base configurada
        yield url
    finally:
        if creado:
            try:
                _docker(["rm", "-f", nombre], 60)
            except (subprocess.SubprocessError, OSError):
                pass


@pytest_asyncio.fixture
async def fabrica_vectorial(url_pgvector_aislado):
    """`async_sessionmaker` sobre la instancia desechable, con el esquema REAL aplicado desde
    las migraciones de `app/migraciones/vectorial` (la extensión se crea aquí: es un prerrequisito)."""
    conexion = await asyncpg.connect(url_pgvector_aislado.replace("+asyncpg", "", 1))
    try:
        await conexion.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION vector;")
        for script in _SQL_VECTORIAL_TODOS:
            await conexion.execute(script.read_text(encoding="utf-8"))
    finally:
        await conexion.close()
    motor = create_async_engine(url_pgvector_aislado)
    try:
        yield async_sessionmaker(motor, expire_on_commit=False)
    finally:
        await motor.dispose()


# ============ Entorno del coordinador de ingesta (PostgreSQL + pgvector aislados, MinIO/OCI dobles) ============
from tests.ayudantes_coordinador import (  # noqa: E402
    Entorno,
    FabricaTransaccionalInstrumentada,
    FabricaVectorialInstrumentada,
    ProveedorDoble,
    config_prueba,
)
from tests.ayudantes_ingesta import AlmacenEnMemoria, crear_empresa, crear_usuario, metadatos  # noqa: E402


@pytest_asyncio.fixture
async def entorno(fabrica_pg, fabrica_vectorial):
    """Coordinador sobre las dos bases REALES aisladas; solo OCI y MinIO son dobles."""
    from app.services.ingesta.coordinador import DependenciasIngesta

    async with fabrica_pg() as db:
        usuario = await crear_usuario(db)
        empresa = await crear_empresa(db)
    almacen = AlmacenEnMemoria("development")
    proveedor = ProveedorDoble()
    transaccional = FabricaTransaccionalInstrumentada(fabrica_pg)
    vectorial = FabricaVectorialInstrumentada(fabrica_vectorial)
    return Entorno(
        deps=DependenciasIngesta(transaccional, vectorial, almacen, proveedor),
        config=config_prueba(),
        almacen=almacen,
        proveedor=proveedor,
        fabrica_pg=fabrica_pg,
        fabrica_vectorial=fabrica_vectorial,
        vectorial=vectorial,
        transaccional=transaccional,
        usuario=usuario,
        empresa=empresa,
        metadatos=metadatos(empresa),
    )

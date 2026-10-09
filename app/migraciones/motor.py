"""Motor de migraciones versionadas para las bases transaccional y vectorial (solo esquemas de INGESTA).

QUÉ HACE `migrar(url, esquema)` (una base cada vez; se conecta EXPLÍCITAMENTE a la URL recibida, sin ningún fallback entre
`DATABASE_URL` y `VECTOR_DATABASE_URL`):
 1. Toma un bloqueo asesor de sesión POR BASE (`pg_try_advisory_lock`, con espera acotada): dos instancias no migran a la vez; la
    segunda espera, ve el registro ya actualizado y no hace nada.
 2. Base vectorial: comprueba pgvector. Si falta intenta `CREATE EXTENSION vector`; si no puede (permiso o paquete no instalado)
    lanza `PGVECTOR_REQUIRED` con el requisito. Nunca finge haberla creado: la verifica después.
 3. Comprueba los prerrequisitos (tablas/tipos que crean otros módulos).
 4. Crea, si no existe, el registro `igualab_migraciones` y verifica el checksum de lo ya aplicado: un archivo editado después de
    aplicarse es un error (`MIGRATION_CHECKSUM_MISMATCH`), no se re-ejecuta ni se ignora.
 5. Para cada migración NO registrada, en orden, clasifica los objetos que toca comparando el esquema REAL con las firmas versionadas
    (`firmas.json`: columnas con tipo y nulabilidad, restricciones e índices):
      * todos en estado anterior (o ausentes)  → se APLICA el SQL y se registra, en UNA transacción (o se revierte todo);
      * todos ya en el estado de esta migración o posterior (instalación hecha a mano) → se ADOPTA: se registra como `adoptada` sin
        ejecutar nada, solo tras verificar que el esquema coincide;
      * cualquier otra cosa (la tabla existe pero distinta) → `MIGRATION_SCHEMA_MISMATCH`, sin modificar nada.
    Una tabla que existe NO se da por correcta: se compara con la firma.
 6. Libera el bloqueo y cierra la conexión (cerrar la conexión también libera el bloqueo si algo falla).

En un arranque sin cambios solo se leen el registro y la presencia de los objetos: no se ejecuta ningún SQL de migración.

LÍMITES CONOCIDOS. La firma compara nombres, tipos (con longitud y dimensión), nulabilidad (`attnotnull` de cada columna), nombres de
restricciones y nombres/unicidad/parcialidad de índices. Las restricciones NOT NULL que PostgreSQL 18 guarda en `pg_constraint`
(`<tabla>_<columna>_not_null`) se excluyen de la lista de restricciones: duplican la nulabilidad ya comparada y no existen en
PostgreSQL ≤17, así que la firma es la misma en todas las versiones; NO compara el texto de los CHECK ni los valores por defecto (varían entre versiones de PostgreSQL).
Un `ALTER` manual que cambie solo eso no se detecta. Las migraciones no transaccionales (`transaccional=False`, p. ej. índices
CONCURRENTLY) se ejecutan fuera de transacción y deben ser idempotentes; hoy no hay ninguna.

SEGURIDAD. Los errores llevan códigos, versión, base y SQLSTATE; nunca la URL, credenciales ni el texto del servidor (eso solo va al log
del servidor, sin la URL).
"""
import asyncio
import hashlib
import json
import logging
import time
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

logger = logging.getLogger("igualab.migraciones")

DIRECTORIO = Path(__file__).resolve().parent
TABLA_REGISTRO = "igualab_migraciones"
ORIGEN_APLICADA = "aplicada"
ORIGEN_ADOPTADA = "adoptada"

_DDL_REGISTRO = f"""
CREATE TABLE IF NOT EXISTS {TABLA_REGISTRO} (
    version     INTEGER      PRIMARY KEY,
    nombre      VARCHAR(120) NOT NULL,
    checksum    CHAR(64)     NOT NULL,
    origen      VARCHAR(16)  NOT NULL CHECK (origen IN ('{ORIGEN_APLICADA}', '{ORIGEN_ADOPTADA}')),
    aplicada_en TIMESTAMPTZ  NOT NULL DEFAULT now()
)
"""


class ErrorMigracion(Exception):
    """Error controlado de la preparación del esquema. `codigo` es estable; `detalles` nunca lleva credenciales ni URL."""

    def __init__(self, codigo: str, mensaje: str, base: str, detalles: dict | None = None) -> None:
        super().__init__(mensaje)
        self.codigo, self.mensaje, self.base, self.detalles = codigo, mensaje, base, detalles or {}


@dataclass(frozen=True)
class Migracion:
    version: int
    nombre: str
    archivo: str  # relativo a la carpeta de la base
    objetos: tuple[str, ...]  # tablas/vistas que crea o modifica (para verificar el esquema real)
    transaccional: bool = True
    carpeta: str = ""

    def sql(self) -> str:
        return (DIRECTORIO / self.carpeta / self.archivo).read_bytes().decode("utf-8").replace("\r\n", "\n")

    def checksum(self) -> str:
        return hashlib.sha256(self.sql().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EsquemaBase:
    nombre: str  # "transaccional" | "vectorial"
    migraciones: tuple[Migracion, ...]
    prerrequisitos: tuple[tuple[str, str], ...] = ()  # ("tabla"|"tipo", nombre)
    requiere_pgvector: bool = False
    firmas: dict = field(default_factory=dict)  # {objeto: {version: firma}}; vacío = las de firmas.json


@dataclass(frozen=True)
class ResultadoMigracion:
    base: str
    aplicadas: tuple[int, ...] = ()
    adoptadas: tuple[int, ...] = ()
    previas: tuple[int, ...] = ()  # ya registradas antes de esta ejecución
    desconocidas: tuple[int, ...] = ()  # registradas por una versión MÁS NUEVA de la aplicación: se respetan

    @property
    def sin_cambios(self) -> bool:
        return not self.aplicadas and not self.adoptadas


# --- Conexión y bloqueo -------------------------------------------------------------------------------------

def _argumentos_de_conexion(url: object, base: str) -> dict:
    if not isinstance(url, str) or not url.strip():
        raise ErrorMigracion("MIGRATION_DB_NOT_CONFIGURED", f"No hay URL configurada para la base {base}.", base)
    try:
        parsed = make_url(url.strip())
    except ArgumentError:
        raise ErrorMigracion("MIGRATION_DB_NOT_CONFIGURED", f"La URL de la base {base} no es válida.", base) from None
    if not parsed.drivername.startswith("postgresql"):
        raise ErrorMigracion("MIGRATION_DB_NOT_CONFIGURED", f"La URL de la base {base} debe ser de PostgreSQL.", base)
    argumentos = {
        "host": parsed.host or "localhost", "port": parsed.port or 5432, "user": parsed.username,
        "password": parsed.password, "database": parsed.database,
    }
    if "ssl" in parsed.query:
        argumentos["ssl"] = parsed.query["ssl"]
    return argumentos


async def _conectar(url: object, base: str, timeout: float) -> asyncpg.Connection:
    argumentos = _argumentos_de_conexion(url, base)
    try:
        return await asyncpg.connect(**argumentos, timeout=timeout, command_timeout=300)
    except (OSError, asyncio.TimeoutError, asyncpg.PostgresError) as exc:
        # Solo la clase del error: el mensaje puede contener el host o el usuario.
        raise ErrorMigracion(
            "MIGRATION_DB_UNAVAILABLE", f"No se pudo conectar a la base {base}.", base, {"causa": type(exc).__name__}
        ) from None


def _clave_candado(base: str) -> int:
    return zlib.crc32(f"igualab:migraciones:{base}".encode("utf-8"))


async def _tomar_candado(conn: asyncpg.Connection, base: str, espera: float) -> int:
    clave = _clave_candado(base)
    inicio = time.monotonic()
    limite = inicio + espera
    proximo_aviso = inicio + 10
    while not await conn.fetchval("SELECT pg_try_advisory_lock($1)", clave):
        if time.monotonic() >= proximo_aviso:
            proximo_aviso += 10
            logger.info("Base %s: esperando el bloqueo de migraciones (%.0f s de %.0f s)...", base, time.monotonic() - inicio, espera)
        if time.monotonic() >= limite:
            raise ErrorMigracion(
                "MIGRATION_LOCK_TIMEOUT",
                f"Otra instancia sigue migrando la base {base}; se agotó la espera del bloqueo.",
                base,
                {"espera_segundos": espera},
            )
        await asyncio.sleep(0.2)
    logger.info("Base %s: bloqueo de migraciones obtenido tras %.2f s.", base, time.monotonic() - inicio)
    return clave


# --- Firmas del esquema real ----------------------------------------------------------------------------------

_SQL_COLUMNAS = """
SELECT a.attname AS nombre, format_type(a.atttypid, a.atttypmod) AS tipo, a.attnotnull AS no_nulo
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = $1 AND c.relkind IN ('r', 'v') AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attname
"""
_SQL_RESTRICCIONES = """
SELECT con.conname AS nombre, con.contype::text AS tipo, con.convalidated AS validada
FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = $1 ORDER BY con.conname
"""
_SQL_INDICES = """
SELECT ic.relname AS nombre, i.indisunique AS unico, (i.indpred IS NOT NULL) AS parcial
FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = $1 ORDER BY ic.relname
"""


def _restricciones_comparables(filas) -> list[str]:
    """Nombres de las restricciones que forman parte de la firma.

    PostgreSQL 18 registra cada columna NOT NULL como una fila de `pg_constraint` con `contype = 'n'` y nombre
    `<tabla>_<columna>_not_null`; las versiones anteriores no tienen esas filas. Son la misma información que `attnotnull`, que la
    firma YA compara por columna (la nulabilidad real se sigue comprobando), así que se excluyen para que la firma sea la misma en
    todas las versiones. Se excluye SOLO ese tipo y solo si está validada: una NOT NULL pendiente de validar (`convalidated = false`)
    y cualquier otra restricción (CHECK, UNIQUE, PRIMARY KEY, FOREIGN KEY, EXCLUDE) se conservan, por nombre, como antes."""
    return [f["nombre"] for f in filas if not (f["tipo"] == "n" and f["validada"])]


async def firma_de_objeto(conn: asyncpg.Connection, objeto: str) -> dict | None:
    """Firma estructural de una tabla/vista del esquema `public`, o `None` si no existe."""
    columnas = await conn.fetch(_SQL_COLUMNAS, objeto)
    if not columnas:
        return None
    return {
        "columnas": [[c["nombre"], c["tipo"], bool(c["no_nulo"])] for c in columnas],
        "restricciones": _restricciones_comparables(await conn.fetch(_SQL_RESTRICCIONES, objeto)),
        "indices": [[i["nombre"], bool(i["unico"]), bool(i["parcial"])] for i in await conn.fetch(_SQL_INDICES, objeto)],
    }


def diferencias(actual: dict, esperado: dict) -> dict:
    """Resumen legible (solo nombres) de en qué difiere el esquema real del esperado."""
    resumen: dict = {}
    real, previsto = {c[0]: c[1:] for c in actual["columnas"]}, {c[0]: c[1:] for c in esperado["columnas"]}
    if faltan := sorted(set(previsto) - set(real)):
        resumen["columnas_faltantes"] = faltan
    if sobran := sorted(set(real) - set(previsto)):
        resumen["columnas_sobrantes"] = sobran
    if distintas := sorted(n for n in set(real) & set(previsto) if real[n] != previsto[n]):
        resumen["columnas_distintas"] = distintas
    if faltan := sorted(set(esperado["restricciones"]) - set(actual["restricciones"])):
        resumen["restricciones_faltantes"] = faltan
    if sobran := sorted(set(actual["restricciones"]) - set(esperado["restricciones"])):
        resumen["restricciones_sobrantes"] = sobran
    ind_real, ind_prev = {i[0]: i[1:] for i in actual["indices"]}, {i[0]: i[1:] for i in esperado["indices"]}
    if faltan := sorted(set(ind_prev) - set(ind_real)):
        resumen["indices_faltantes"] = faltan
    if sobran := sorted(set(ind_real) - set(ind_prev)):
        resumen["indices_sobrantes"] = sobran
    if distintos := sorted(n for n in set(ind_real) & set(ind_prev) if ind_real[n] != ind_prev[n]):
        resumen["indices_distintos"] = distintos
    return resumen


@lru_cache(maxsize=1)
def firmas_versionadas() -> dict:
    """{base: {objeto: {version(int): firma}}} desde `firmas.json` (se regenera con la prueba de firmas)."""
    crudo = json.loads((DIRECTORIO / "firmas.json").read_text(encoding="utf-8"))
    return {b: {o: {int(v): f for v, f in vs.items()} for o, vs in objetos.items()} for b, objetos in crudo.items()}


def _normalizada(firma: dict) -> dict:
    return json.loads(json.dumps(firma))


def _version_del_objeto(actual: dict | None, versiones: dict[int, dict]) -> int | None:
    """Versión más alta cuya firma coincide EXACTAMENTE con el esquema real; 0 si no existe; `None` si existe pero no coincide."""
    if actual is None:
        return 0
    coincidentes = [v for v, firma in versiones.items() if _normalizada(firma) == _normalizada(actual)]
    return max(coincidentes) if coincidentes else None


# --- Pasos --------------------------------------------------------------------------------------------------------

async def _asegurar_pgvector(conn: asyncpg.Connection, base: str) -> None:
    async def presente() -> bool:
        return bool(await conn.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))

    if await presente():
        return
    sqlstate = None
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except asyncpg.PostgresError as exc:
        sqlstate = exc.sqlstate
        logger.warning("No se pudo crear la extensión pgvector (SQLSTATE %s).", sqlstate)
    if not await presente():
        raise ErrorMigracion(
            "PGVECTOR_REQUIRED",
            "Falta la extensión pgvector ('vector') en la base vectorial y la aplicación no pudo crearla (permiso insuficiente o "
            "paquete no instalado en el servidor). Requisito: instalar pgvector en el servidor PostgreSQL y ejecutar "
            "CREATE EXTENSION vector; en esa base con un rol autorizado.",
            base,
            {"sqlstate": sqlstate},
        )


async def _comprobar_prerrequisitos(conn: asyncpg.Connection, esquema: EsquemaBase) -> None:
    faltan = []
    for tipo, nombre in esquema.prerrequisitos:
        if tipo == "tabla":
            existe = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{nombre}")
        else:
            existe = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_type WHERE typname = $1)", nombre)
        if not existe:
            faltan.append(f"{tipo}:{nombre}")
    if faltan:
        raise ErrorMigracion(
            "MIGRATION_PREREQUISITE_MISSING",
            f"Faltan objetos que deben existir antes de migrar la base {esquema.nombre}: {', '.join(faltan)}.",
            esquema.nombre,
            {"faltan": faltan},
        )


async def _leer_registro(conn: asyncpg.Connection, esquema: EsquemaBase) -> dict[int, dict]:
    await conn.execute(_DDL_REGISTRO)
    filas = await conn.fetch(f"SELECT version, nombre, checksum, origen FROM {TABLA_REGISTRO} ORDER BY version")
    registro = {f["version"]: dict(f) for f in filas}
    conocidas = {m.version: m for m in esquema.migraciones}
    for version, fila in registro.items():
        m = conocidas.get(version)
        if m is not None and fila["checksum"].strip() != m.checksum():
            raise ErrorMigracion(
                "MIGRATION_CHECKSUM_MISMATCH",
                f"La migración {version:04d} ({m.nombre}) ya estaba aplicada pero su archivo cambió: no se edita una migración "
                "aplicada; cree una nueva.",
                esquema.nombre,
                {"version": version},
            )
    return registro


def _firmas_de(esquema: EsquemaBase) -> dict:
    return esquema.firmas or firmas_versionadas().get(esquema.nombre, {})


async def _firmas_actuales(conn: asyncpg.Connection, objetos: tuple[str, ...]) -> dict[str, dict | None]:
    return {objeto: await firma_de_objeto(conn, objeto) for objeto in objetos}


def _clasificar(m: Migracion, actuales: dict[str, dict | None], firmas: dict, base: str) -> str:
    """"aplicar" | "adoptar"; o ErrorMigracion si el esquema real no corresponde a ninguna versión conocida."""
    if not m.objetos:
        return "aplicar"  # sin objetos que verificar no hay adopción posible
    estados = []
    for objeto in m.objetos:
        versiones = firmas.get(objeto, {})
        version_real = _version_del_objeto(actuales[objeto], versiones)
        if version_real is None:
            ultima = versiones[max(versiones)] if versiones else None
            raise ErrorMigracion(
                "MIGRATION_SCHEMA_MISMATCH",
                f"El objeto '{objeto}' ya existe en la base {base} pero su esquema no coincide con ninguna versión conocida; no se "
                "modificó nada. Revise las diferencias o corríjalo antes de reiniciar.",
                base,
                {"version": m.version, "objeto": objeto, "diferencias": diferencias(actuales[objeto], ultima) if ultima else {}},
            )
        estados.append("adoptar" if version_real >= m.version else "aplicar")
    if len(set(estados)) > 1:
        raise ErrorMigracion(
            "MIGRATION_SCHEMA_MISMATCH",
            f"La migración {m.version:04d} ({m.nombre}) está a medias en la base {base}: unos objetos están en su estado y otros no; "
            "no se modificó nada.",
            base,
            {"version": m.version, "objetos": dict(zip(m.objetos, estados))},
        )
    return estados[0]


async def _registrar(conn: asyncpg.Connection, m: Migracion, origen: str) -> None:
    await conn.execute(
        f"INSERT INTO {TABLA_REGISTRO} (version, nombre, checksum, origen) VALUES ($1, $2, $3, $4)",
        m.version, m.nombre, m.checksum(), origen,
    )


async def _aplicar(conn: asyncpg.Connection, m: Migracion, base: str) -> None:
    try:
        if m.transaccional:
            async with conn.transaction():
                await conn.execute(m.sql())
                await _registrar(conn, m, ORIGEN_APLICADA)
        else:
            await conn.execute(m.sql())
            await _registrar(conn, m, ORIGEN_APLICADA)
    except asyncpg.PostgresError as exc:
        logger.error("La migración %04d (%s) de la base %s falló: %s", m.version, m.nombre, base, exc)
        raise ErrorMigracion(
            "MIGRATION_FAILED",
            f"La migración {m.version:04d} ({m.nombre}) falló en la base {base}"
            + (" y se deshizo por completo." if m.transaccional else "; no es transaccional: revise el estado."),
            base,
            {"version": m.version, "sqlstate": exc.sqlstate},
        ) from None


async def _verificar_objetos_registrados(conn: asyncpg.Connection, esquema: EsquemaBase, registro: dict[int, dict]) -> None:
    """Un arranque sin cambios solo comprueba que lo registrado sigue existiendo (sin ejecutar SQL de migración)."""
    for m in esquema.migraciones:
        if m.version in registro:
            for objeto in m.objetos:
                if await firma_de_objeto(conn, objeto) is None:
                    raise ErrorMigracion(
                        "MIGRATION_SCHEMA_MISMATCH",
                        f"La migración {m.version:04d} figura aplicada en la base {esquema.nombre} pero falta '{objeto}'.",
                        esquema.nombre,
                        {"version": m.version, "objeto": objeto},
                    )


async def migrar(
    url: object, esquema: EsquemaBase, *, espera_candado: float = 60.0, timeout_conexion: float = 10.0
) -> ResultadoMigracion:
    """Lleva UNA base al último esquema de ingesta (ver el docstring del módulo). Idempotente y segura con varias instancias."""
    versiones = [m.version for m in esquema.migraciones]
    if versiones != sorted(set(versiones)):
        raise ValueError("Las migraciones deben tener versiones únicas y ordenadas.")
    base = esquema.nombre
    t0 = time.monotonic()
    conn = await _conectar(url, base, timeout_conexion)
    logger.info("Base %s: conexión establecida en %.2f s.", base, time.monotonic() - t0)
    candado = None
    try:
        candado = await _tomar_candado(conn, base, espera_candado)
        if esquema.requiere_pgvector:
            await _asegurar_pgvector(conn, base)
        await _comprobar_prerrequisitos(conn, esquema)
        logger.info("Base %s: requisitos comprobados; leyendo el registro de migraciones.", base)
        registro = await _leer_registro(conn, esquema)
        previas = tuple(sorted(v for v in registro if v in versiones))
        desconocidas = tuple(sorted(v for v in registro if v not in versiones))
        await _verificar_objetos_registrados(conn, esquema, registro)

        firmas = _firmas_de(esquema) if any(m.version not in registro for m in esquema.migraciones) else {}
        aplicadas, adoptadas = [], []
        for m in esquema.migraciones:
            if m.version in registro:
                continue
            actuales = await _firmas_actuales(conn, m.objetos)
            decision = _clasificar(m, actuales, firmas, base)
            if decision == "adoptar":
                await _registrar(conn, m, ORIGEN_ADOPTADA)
                adoptadas.append(m.version)
                logger.info("Migración %04d (%s) de la base %s adoptada: el esquema existente coincide.", m.version, m.nombre, base)
            else:
                await _aplicar(conn, m, base)
                aplicadas.append(m.version)
                logger.info("Migración %04d (%s) de la base %s aplicada.", m.version, m.nombre, base)
        return ResultadoMigracion(base, tuple(aplicadas), tuple(adoptadas), previas, desconocidas)
    except asyncpg.PostgresError as exc:
        logger.error("Error de PostgreSQL al preparar el esquema de la base %s: %s", base, exc)
        raise ErrorMigracion(
            "MIGRATION_FAILED", f"Error de PostgreSQL al preparar la base {base}.", base, {"sqlstate": exc.sqlstate}
        ) from None
    finally:
        try:
            if candado is not None:
                await conn.execute("SELECT pg_advisory_unlock($1)", candado)
        except Exception:  # noqa: BLE001  (cerrar la conexión también libera el bloqueo)
            pass
        await conn.close()

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import engine, Base
from app import models  # noqa: F401
from app.logging_config import configure_logging, paso_de_arranque

logger = logging.getLogger("igualab.startup")

# Tablas que el arranque NUNCA crea, aunque sus modelos estén cargados (D17): importar el coordinador de
# ingesta registra `Documento` y `OperacionIngesta` en los metadatos, y un `create_all` generaría tablas y tipos enumerados en
# ambientes compartidos sin una migración aprobada. Las crean las migraciones versionadas de `app/migraciones` (que corren después, solo para ingesta).
TABLAS_SIN_DDL_AUTOMATICO = frozenset({"documentos", "operaciones_ingesta"})

# Plazos explícitos de esta inicialización (antes no había ninguno por sentencia: una espera dejaba el arranque, y con él toda la
# API, colgado sin mensaje). Si uno se agota el arranque FALLA indicando el paso; no continúa como si hubiera terminado.
PLAZO_CONEXION_S = 20.0   # obtener la conexión a PostgreSQL (el valor por defecto de asyncpg es 60 s)
PLAZO_SENTENCIA_S = 30.0  # `statement_timeout` en el servidor para cada sentencia de la transacción
PLAZO_BLOQUEO_S = 15.0    # `lock_timeout` en el servidor: espera máxima por un bloqueo retenido por otra sesión
PLAZO_TABLA_S = 60.0      # tope en el cliente para comprobar/crear UNA tabla (por si el servidor deja de responder)
PLAZO_CIERRE_S = 5.0      # liberar la conexión al terminar o fallar

PASO = "1/6"  # mismo número que el paso de `app.main.lifespan`; los subpasos se registran como «1/6 › …»

_SIGNIFICADO_SQLSTATE = {
    "57014": "statement_timeout agotado en el servidor",
    "55P03": "lock_timeout agotado: un bloqueo retenido por otra sesión",
    "57P01": "el servidor cerró la conexión (apagado)",
    "08006": "conexión con el servidor perdida",
    "28P01": "autenticación rechazada por el servidor",
    "3D000": "la base de datos no existe",
}


class ErrorInicializacionTablas(RuntimeError):
    """La inicialización de tablas no terminó. `paso` dice cuál se interrumpió; el mensaje nunca incluye URLs ni credenciales."""

    def __init__(self, paso: str, motivo: str):
        super().__init__(f"Inicialización de tablas interrumpida en «{paso}»: {motivo}")
        self.paso = paso
        self.motivo = motivo


def _describir(exc: BaseException) -> str:
    """Descripción SEGURA: clases y SQLSTATE, nunca el mensaje (puede traer hosts o credenciales)."""
    orig = getattr(exc, "orig", None)
    codigo = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None) or getattr(exc, "sqlstate", None)
    clases = type(exc).__name__ if orig is None else f"{type(exc).__name__}/{type(orig).__name__}"
    if codigo:
        return f"{_SIGNIFICADO_SQLSTATE.get(codigo, 'error de base de datos')} (SQLSTATE {codigo}; {clases})"
    return f"error de tipo {clases}"


@asynccontextmanager
async def _con_plazo(paso: str, plazo_s: float):
    """Aplica un plazo en el cliente y convierte cualquier fallo en `ErrorInicializacionTablas` con el paso que falló."""
    try:
        async with asyncio.timeout(plazo_s):
            yield
    except ErrorInicializacionTablas:
        raise
    except TimeoutError:
        motivo = f"plazo de {plazo_s:.0f} s agotado esperando a PostgreSQL"
    except Exception as exc:  # noqa: BLE001 - se re-lanza con el paso y una descripción segura
        motivo = _describir(exc)
    else:
        return
    logger.error("Arranque | %s | %s", paso, motivo)
    raise ErrorInicializacionTablas(paso, motivo) from None


def tablas_a_crear() -> list:
    return [t for t in Base.metadata.sorted_tables if t.name not in TABLAS_SIN_DDL_AUTOMATICO]


def _crear_tabla(conexion, tabla) -> bool:
    """Comprueba y, si falta, crea UNA tabla (con sus tipos enumerados). Devuelve si ya existía."""
    existia = conexion.dialect.has_table(conexion, tabla.name, schema=tabla.schema)
    tabla.create(conexion, checkfirst=True)
    return existia


def _crear_permitidas(conexion) -> None:
    # Una a una (en orden de dependencias): `Table.create` solo crea los tipos enumerados de ESA tabla,
    # mientras que `create_all(tables=...)` también crea los de las demás tablas cargadas.
    for tabla in tablas_a_crear():
        _crear_tabla(conexion, tabla)


async def _liberar(conn, descartar: bool) -> None:
    """Devuelve la conexión sin colgarse. Si el paso falló se DESCARTA (terminate) en vez de hacer rollback contra un servidor
    que puede no responder."""
    try:
        async with asyncio.timeout(PLAZO_CIERRE_S):
            if descartar:
                await conn.invalidate()
            await conn.close()
    except Exception as exc:  # noqa: BLE001 - la liberación nunca debe tapar el error original
        logger.warning("Arranque | %s › cierre de la conexión | no se pudo liberar limpiamente (%s)", PASO, type(exc).__name__)


async def crear_tablas() -> list[str]:
    tablas = tablas_a_crear()
    total = len(tablas)
    logger.info(
        "Arranque | %s › plazos | conexión %.0f s, sentencia %.0f s, bloqueo %.0f s, por tabla %.0f s (cliente); %d tablas por comprobar",
        PASO, PLAZO_CONEXION_S, PLAZO_SENTENCIA_S, PLAZO_BLOQUEO_S, PLAZO_TABLA_S, total,
    )

    with paso_de_arranque(logger, f"{PASO} › conexión a PostgreSQL"):
        async with _con_plazo(f"{PASO} › conexión a PostgreSQL", PLAZO_CONEXION_S):
            conn = await engine.connect()

    creadas: list[str] = []
    descartar = True
    try:
        paso_sesion = f"{PASO} › plazos de sesión"
        with paso_de_arranque(logger, paso_sesion):
            async with _con_plazo(paso_sesion, PLAZO_SENTENCIA_S):
                # SET LOCAL: vale solo para esta transacción, no altera el resto de la aplicación.
                await conn.execute(text(f"SET LOCAL statement_timeout = {int(PLAZO_SENTENCIA_S * 1000)}"))
                await conn.execute(text(f"SET LOCAL lock_timeout = {int(PLAZO_BLOQUEO_S * 1000)}"))

        for numero, tabla in enumerate(tablas, start=1):
            paso_tabla = f"{PASO} › tabla {numero}/{total} {tabla.name}"
            with paso_de_arranque(logger, paso_tabla):
                async with _con_plazo(paso_tabla, PLAZO_TABLA_S):
                    existia = await conn.run_sync(_crear_tabla, tabla)
            if not existia:
                creadas.append(tabla.name)
            logger.info("Arranque | %s | %s", paso_tabla, "ya existía" if existia else "CREADA")

        paso_commit = f"{PASO} › confirmación de la transacción"
        with paso_de_arranque(logger, paso_commit):
            async with _con_plazo(paso_commit, PLAZO_SENTENCIA_S):
                await conn.commit()
        descartar = False
    finally:
        await _liberar(conn, descartar)

    nombres = [t.name for t in tablas]
    logger.info("Tablas verificadas correctamente: %s", nombres)
    if creadas:
        logger.info("Tablas creadas en este arranque: %s", creadas)
    return nombres


if __name__ == "__main__":
    configure_logging()
    asyncio.run(crear_tablas())

import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, Base
from app import models  # noqa: F401
from app.logging_config import configure_logging

logger = logging.getLogger("igualab.startup")

# Tablas que el arranque NUNCA crea, aunque sus modelos estén cargados (D17): importar el coordinador de
# ingesta registra `Documento` y `OperacionIngesta` en los metadatos, y un `create_all` generaría tablas y tipos enumerados en
# ambientes compartidos sin una migración aprobada. Las crean las migraciones versionadas de `app/migraciones` (que corren después, solo para ingesta).
TABLAS_SIN_DDL_AUTOMATICO = frozenset({"documentos", "operaciones_ingesta"})


def tablas_a_crear() -> list:
    return [t for t in Base.metadata.sorted_tables if t.name not in TABLAS_SIN_DDL_AUTOMATICO]


def _crear_permitidas(conexion) -> None:
    # Una a una (en orden de dependencias): `Table.create` solo crea los tipos enumerados de ESA tabla,
    # mientras que `create_all(tables=...)` también crea los de las demás tablas cargadas.
    for tabla in tablas_a_crear():
        tabla.create(conexion, checkfirst=True)


async def crear_tablas() -> list[str]:
    async with engine.begin() as conn:
        await conn.run_sync(_crear_permitidas)
    tablas = [t.name for t in tablas_a_crear()]
    logger.info("Tablas verificadas correctamente: %s", tablas)
    return tablas


if __name__ == "__main__":
    configure_logging()
    asyncio.run(crear_tablas())

import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, Base
from app import models  # noqa: F401
from app.logging_config import configure_logging

logger = logging.getLogger("igualab.startup")


async def crear_tablas() -> list[str]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    tablas = list(Base.metadata.tables.keys())
    logger.info("Tablas verificadas correctamente: %s", tablas)
    return tablas


if __name__ == "__main__":
    configure_logging()
    asyncio.run(crear_tablas())

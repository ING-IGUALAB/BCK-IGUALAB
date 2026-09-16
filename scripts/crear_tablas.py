import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, Base
from app import models  # noqa: F401


async def crear_tablas() -> list[str]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return list(Base.metadata.tables.keys())


if __name__ == "__main__":
    tablas = asyncio.run(crear_tablas())
    print(f"Tablas creadas correctamente: {tablas}")
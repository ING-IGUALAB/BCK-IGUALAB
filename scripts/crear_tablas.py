import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, Base
from app import models  # el import registra las tablas en Base.metadata


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print(f"Tablas creadas correctamente: {list(Base.metadata.tables.keys())}")


if __name__ == "__main__":
    asyncio.run(main())
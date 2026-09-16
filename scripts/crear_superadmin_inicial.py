import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Usuario, RolUsuario
from app.security import hash_password, validar_politica_password
from app.logging_config import configure_logging

logger = logging.getLogger("igualab.startup")


async def crear_superadmin_inicial() -> None:
    async with AsyncSessionLocal() as db:
        existente = await db.execute(select(Usuario).where(Usuario.rol == RolUsuario.SUPERADMIN))
        if existente.scalar_one_or_none() is not None:
            logger.info("La cuenta SuperAdmin ya existe; no se crea otra")
            return  # RN-002: ya existe uno, no se crea otro. Caso normal en cada reinicio.

        nombre = os.environ["SUPERADMIN_NOMBRE"]
        correo = os.environ["SUPERADMIN_CORREO"].lower()
        password = os.environ["SUPERADMIN_PASSWORD"]

        errores = validar_politica_password(password, correo)
        if errores:
            raise RuntimeError(f"SUPERADMIN_PASSWORD no cumple la política: {errores}")

        db.add(Usuario(
            nombre=nombre, correo=correo,
            password_hash=hash_password(password),
            rol=RolUsuario.SUPERADMIN, habilitado=True,
        ))
        await db.commit()
        logger.info("Cuenta SuperAdmin creada: %s", correo)


if __name__ == "__main__":
    configure_logging()
    asyncio.run(crear_superadmin_inicial())

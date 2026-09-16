import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Usuario, RolUsuario
from app.security import hash_password, validar_politica_password


async def crear_superadmin_inicial() -> None:
    async with AsyncSessionLocal() as db:
        existente = await db.execute(select(Usuario).where(Usuario.rol == RolUsuario.SUPERADMIN))
        if existente.scalar_one_or_none() is not None:
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
        print(f"Cuenta SuperAdmin creada: {correo}")


if __name__ == "__main__":
    asyncio.run(crear_superadmin_inicial())
"""
RN-006: "El sistema se inicializa con una única cuenta SuperAdmin creada
durante el despliegue. Esta cuenta no puede crearse mediante la aplicación."

Por eso este script vive fuera de app/ (no es un endpoint) — se corre UNA
vez, manualmente, al desplegar el sistema por primera vez:

    python -m scripts.crear_superadmin_inicial

Lee las credenciales de variables de entorno para no dejar ninguna
contraseña de ejemplo escrita en el repositorio.
"""
import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Usuario, RolUsuario
from app.security import hash_password, validar_politica_password


async def main():
    nombre = os.environ["SUPERADMIN_NOMBRE"]
    correo = os.environ["SUPERADMIN_CORREO"].lower()
    password = os.environ["SUPERADMIN_PASSWORD"]

    errores = validar_politica_password(password, correo)
    if errores:
        print("La contraseña no cumple la política de seguridad:")
        for e in errores:
            print(f"  - {e}")
        return

    async with AsyncSessionLocal() as db:
        existente = await db.execute(select(Usuario).where(Usuario.rol == RolUsuario.SUPERADMIN))
        if existente.scalar_one_or_none() is not None:
            print("Ya existe una cuenta SuperAdmin. RN-002 impide crear una segunda. Abortando.")
            return

        usuario = Usuario(
            nombre=nombre, correo=correo,
            password_hash=hash_password(password),
            rol=RolUsuario.SUPERADMIN, habilitado=True,
        )
        db.add(usuario)
        await db.commit()
        print(f"Cuenta SuperAdmin creada correctamente: {correo}")


if __name__ == "__main__":
    asyncio.run(main())

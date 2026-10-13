"""Restablece la contraseña de la cuenta SuperAdmin del entorno QA. Herramienta administrativa local.

Qué hace (todo en UNA transacción, solo con --ejecutar):
  1. Cambia el hash de la contraseña (política y `hash_password` del proyecto).
  2. Restablece los intentos fallidos y el bloqueo temporal.
  3. Revoca todas las sesiones de la cuenta.
  4. Invalida los tokens de recuperación pendientes.
  5. Registra el evento en la auditoría (sin contraseña, hash ni credenciales).

Qué NO hace: no crea usuarios, no cambia roles ni permisos, no toca `.env` ni el login.

Seguridad:
  - Lee SOLO `.env.qa` (nunca `.env`) y exige APP_ENV=qa y la base `igualab_qa`.
  - La contraseña se pide dos veces con `getpass` (sin eco); nunca se recibe por argumento ni se imprime.
  - Los errores se informan solo por su clase: los mensajes del driver pueden contener la URL de conexión.

Uso (desde la raíz del proyecto):
  python scripts/restablecer_superadmin.py              # verificación en modo lectura, no modifica nada
  python scripts/restablecer_superadmin.py --ejecutar   # pide la contraseña y aplica el cambio
"""
import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO_ENV = RAIZ / ".env.qa"
BASE_ESPERADA = "igualab_qa"
ENTORNO_ESPERADO = "qa"
VARIABLES_OBLIGATORIAS = ("APP_ENV", "DATABASE_URL", "JWT_SECRET_KEY", "SUPERADMIN_CORREO")
DETALLE_AUDITORIA = "Restablecimiento administrativo de la contraseña de la cuenta SuperAdmin (script local)"


class Abortar(Exception):
    """Condición de seguridad no cumplida. Su mensaje es seguro de mostrar (nunca lleva datos de conexión)."""


def cargar_entorno_qa() -> dict[str, str]:
    """Carga SOLO `.env.qa` en el entorno del proceso, antes de importar la aplicación.

    `app.config` llama a `load_dotenv()` al importarse y cargaría `.env` (desarrollo) para lo que falte; se neutraliza
    para que ninguna variable pueda venir de otro entorno."""
    import dotenv

    if not ARCHIVO_ENV.is_file():
        raise Abortar("No existe .env.qa en la raíz del proyecto.")
    valores = {k: v for k, v in dotenv.dotenv_values(ARCHIVO_ENV).items() if v is not None}
    faltan = [nombre for nombre in VARIABLES_OBLIGATORIAS if not valores.get(nombre, "").strip()]
    if faltan:
        raise Abortar(f"Faltan variables en .env.qa: {', '.join(faltan)}.")
    os.environ.update(valores)  # .env.qa manda sobre cualquier variable ya definida en la terminal
    dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
    return valores


def verificar_destino(valores: dict[str, str]) -> None:
    """Comprueba entorno y base ANTES de abrir ninguna conexión."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    if valores["APP_ENV"].strip().lower() != ENTORNO_ESPERADO:
        raise Abortar(f"APP_ENV en .env.qa no es '{ENTORNO_ESPERADO}'. No se hace nada.")
    try:
        base = make_url(valores["DATABASE_URL"].strip()).database
    except ArgumentError:
        raise Abortar("DATABASE_URL de .env.qa no es una URL válida.") from None
    if base != BASE_ESPERADA:
        raise Abortar(f"La base de destino no es '{BASE_ESPERADA}'. No se hace nada.")


def pedir_password(correo: str) -> str:
    """Pide la contraseña dos veces sin eco y aplica la política del proyecto."""
    from app.security import validar_politica_password

    primera = getpass.getpass("Nueva contraseña (no se mostrará): ")
    segunda = getpass.getpass("Repita la nueva contraseña: ")
    if primera != segunda:
        raise Abortar("Las dos contraseñas no coinciden.")
    errores = validar_politica_password(primera, correo)
    if errores:
        raise Abortar("La contraseña no cumple la política:\n  - " + "\n  - ".join(errores))
    return primera


async def buscar_superadmin(db, correo: str, bloquear: bool = False):
    """Usuario con ese correo, verificando que sea SuperAdmin. Nunca crea usuarios."""
    from sqlalchemy import select

    from app.models import RolUsuario, Usuario

    consulta = select(Usuario).where(Usuario.correo == correo)
    if bloquear:
        consulta = consulta.with_for_update()
    usuario = (await db.execute(consulta)).scalar_one_or_none()
    if usuario is None:
        raise Abortar("No existe un usuario con SUPERADMIN_CORREO. No se crea ninguno.")
    if usuario.rol is not RolUsuario.SUPERADMIN:
        raise Abortar("El usuario con SUPERADMIN_CORREO no tiene el rol SuperAdmin. No se modifica.")
    return usuario


async def verificar(correo: str) -> bool:
    """Modo lectura: confirma que el usuario existe y es SuperAdmin. Devuelve si la cuenta está habilitada."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        usuario = await buscar_superadmin(db, correo)
        return usuario.habilitado


async def restablecer(correo: str, password: str) -> dict[str, int]:
    """Aplica todos los cambios en una sola transacción (si algo falla, no se aplica nada)."""
    from sqlalchemy import update

    from app.audit import registrar_evento
    from app.database import AsyncSessionLocal
    from app.models import Sesion, TipoEventoAuditoria, TokenRecuperacion
    from app.security import hash_password

    async with AsyncSessionLocal() as db:
        async with db.begin():
            # Se vuelve a leer con bloqueo de fila y se revalida el rol dentro de la transacción.
            usuario = await buscar_superadmin(db, correo, bloquear=True)
            usuario.password_hash = hash_password(password)
            usuario.intentos_fallidos = 0
            usuario.bloqueado_hasta = None
            sesiones = await db.execute(
                update(Sesion).where(Sesion.usuario_id == usuario.id, Sesion.revocada.is_(False)).values(revocada=True)
            )
            tokens = await db.execute(
                update(TokenRecuperacion)
                .where(TokenRecuperacion.usuario_id == usuario.id, TokenRecuperacion.usado.is_(False))
                .values(usado=True)
            )
            # El proyecto no tiene un tipo de evento propio para esto; se usa el de administración de cuentas
            # (el mismo de habilitar/deshabilitar y transferir el SuperAdmin) sin añadir valores al enum de la BD.
            registrar_evento(db, TipoEventoAuditoria.CAMBIO_ROL, DETALLE_AUDITORIA, usuario_id=usuario.id)
        return {"sesiones_revocadas": sesiones.rowcount, "tokens_invalidados": tokens.rowcount}


async def principal(ejecutar: bool) -> int:
    valores = cargar_entorno_qa()
    verificar_destino(valores)
    correo = valores["SUPERADMIN_CORREO"].strip().lower()

    # Recién ahora se importa la aplicación (usa las variables de .env.qa ya cargadas).
    from app.config import settings

    if settings.APP_ENV.strip().lower() != ENTORNO_ESPERADO:
        raise Abortar("La configuración cargada no corresponde a QA. No se hace nada.")

    print(f"Entorno: {ENTORNO_ESPERADO} | base: {BASE_ESPERADA} | cuenta: {correo}")
    habilitado = await verificar(correo)
    print("Verificación: el usuario existe y tiene el rol SuperAdmin.")
    if not habilitado:
        print("AVISO: la cuenta está deshabilitada. Este script no la habilita.")

    if not ejecutar:
        print("Modo lectura: no se modificó nada. Use --ejecutar para restablecer la contraseña.")
        return 0

    password = pedir_password(correo)
    resultado = await restablecer(correo, password)
    print("Contraseña restablecida.")
    print(f"Intentos fallidos y bloqueo: restablecidos. Sesiones revocadas: {resultado['sesiones_revocadas']}. "
          f"Tokens de recuperación invalidados: {resultado['tokens_invalidados']}.")
    print("Evento registrado en la auditoría.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Restablece la contraseña del SuperAdmin de QA (solo con --ejecutar).")
    parser.add_argument("--ejecutar", action="store_true", help="Aplica el cambio; sin este indicador solo verifica.")
    argumentos = parser.parse_args()
    sys.path.insert(0, str(RAIZ))
    try:
        return asyncio.run(principal(argumentos.ejecutar))
    except Abortar as motivo:
        print(f"ABORTADO: {motivo}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelado por el usuario. No se modificó nada.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001  (solo la clase: el mensaje puede traer la URL de conexión)
        print(f"ERROR inesperado ({type(exc).__name__}). No se aplicó ningún cambio.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

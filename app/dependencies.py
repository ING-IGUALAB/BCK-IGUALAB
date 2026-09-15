"""
Dependencias de autenticación/autorización, inyectadas con `Depends(...)`
en cada router que las necesite.
"""
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Usuario, Sesion, RolUsuario
from app.security import decodificar_token

# HTTPBearer (no OAuth2PasswordBearer): nuestro login es un JSON propio
# ({correo, password}), no el formulario OAuth2 estándar (username,
# client_id, client_secret). OAuth2PasswordBearer le hacía creer a Swagger
# que debía mostrar ese formulario completo en "Authorize" — con
# HTTPBearer, Swagger solo pide pegar el token, que es lo que en realidad
# necesitamos.
esquema_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credenciales: HTTPAuthorizationCredentials | None = Depends(esquema_bearer),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    """
    Valida, en este orden:
      1. Que el token traiga una firma válida (RNF-003).
      2. Que la sesión (jti) exista, no esté revocada, y no haya expirado
         por inactividad (RN-036 / RF-005 / RNF-005).
      3. Que la cuenta siga habilitada y con el rol que dice tener
         (RNF-026) — nunca se confía en el `rol` que trae el JWT si la
         cuenta cambió de rol después de emitido (RF-050).
    Si todo pasa, actualiza `ultima_actividad` (sesión deslizante) y
    devuelve el Usuario.
    """
    credenciales_invalidas = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "No autenticado o sesión inválida.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credenciales is None:
        raise credenciales_invalidas
    token = credenciales.credentials

    payload = decodificar_token(token)
    if payload is None:
        raise credenciales_invalidas

    try:
        sesion_id = uuid.UUID(payload["jti"])
        usuario_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise credenciales_invalidas

    sesion = await db.get(Sesion, sesion_id)
    if sesion is None or sesion.revocada or sesion.usuario_id != usuario_id:
        raise credenciales_invalidas

    ahora = datetime.now(timezone.utc)
    if ahora - sesion.ultima_actividad > settings.SESSION_INACTIVITY_TIMEOUT:
        sesion.revocada = True
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión expirada por inactividad.")

    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not usuario.habilitado:
        raise credenciales_invalidas

    # Sesión deslizante: cada petición autenticada renueva la ventana de inactividad.
    sesion.ultima_actividad = ahora
    await db.commit()

    return usuario


def requerir_rol(*roles_permitidos: RolUsuario):
    async def _verificar(usuario: Usuario = Depends(get_current_user)) -> Usuario:
        if usuario.rol not in roles_permitidos:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tiene permisos para esta acción.")
        return usuario
    return _verificar
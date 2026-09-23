
import uuid
from datetime import datetime, timezone

from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from app.config import settings
from app.database import get_db
from app.models import Usuario, Sesion, RolUsuario
from app.security import decodificar_token
from app.exceptions import AuthenticationError, AuthorizationError

esquema_bearer = HTTPBearer(auto_error=False)

DatabaseSession = Annotated[
    AsyncSession,
    Depends(get_db),
]

OptionalBearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(esquema_bearer),
]

BearerCredentials = Annotated[
    HTTPAuthorizationCredentials,
    Depends(esquema_bearer),
]

async def get_current_user(
    credenciales: OptionalBearerCredentials,
    db: DatabaseSession,
) -> Usuario:
    """
    Valida, en este orden:
      1. Que el token traiga una firma válida (RNF-003)
      2. Que la sesión (jti) exista, no esté revocada, y no haya expirado
         por inactividad
      3. Que la cuenta siga habilitada y con el rol que dice tener
         nunca se confía en el rol que trae el JWT si la
         cuenta cambió de rol después de emitido (RF-050)
    Si todo pasa, actualiza ultima_actividad (sesión deslizante) y
    devuelve el Usuario
    """
    credenciales_invalidas = AuthenticationError(
        "INVALID_SESSION",
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
        raise AuthenticationError(
            "SESSION_EXPIRED",
            "Sesión expirada por inactividad.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not usuario.habilitado:
        raise credenciales_invalidas

    # cada petición autenticada renueva la ventana de inactividad
    sesion.ultima_actividad = ahora
    await db.commit()
    return usuario


CurrentUser = Annotated[
    Usuario,
    Depends(get_current_user),
]

def requerir_rol(*roles_permitidos: RolUsuario):
    def _verificar(
        usuario: CurrentUser,
    ) -> Usuario:
        if usuario.rol not in roles_permitidos:
            raise AuthorizationError(
                "FORBIDDEN",
                "No tiene permisos para esta acción.",
            )
        return usuario
    return _verificar

SuperAdminUser = Annotated[
    Usuario,
    Depends(requerir_rol(RolUsuario.SUPERADMIN)),
]
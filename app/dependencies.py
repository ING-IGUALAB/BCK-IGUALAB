"""
Dependencias de autenticación/autorización, inyectadas con `Depends(...)`
en cada router que las necesite.

RNF-004: "La autorización se evalúa en cada endpoint, con independencia de
lo que exponga la interfaz" — por diseño, get_current_user() se ejecuta en
TODA ruta protegida (nunca se confía en que el frontend oculte un botón),
y require_role() se agrega encima en las rutas que además necesitan
restringir por rol específico (RF-007, RF-008).
"""
import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Usuario, Sesion, RolUsuario
from app.security import decodificar_token

# tokenUrl solo se usa para la documentación automática (Swagger) — el
# login real de este proyecto no sigue el flujo OAuth2 password estándar
# de formulario, usa nuestro propio esquema JSON (ver routers/auth.py).
# Sin guión bajo a propósito: se reutiliza desde routers/auth.py para leer
# el token crudo en /logout (reusabilidad, evita decodificar dos veces).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
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

    if token is None:
        raise credenciales_invalidas

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
    """
    Fábrica de dependencias — se usa así en un router:
        Depends(requerir_rol(RolUsuario.SUPERADMIN))
    RF-007 / RF-008: restringe el endpoint a los roles indicados, devolviendo
    403 (no 404) para no filtrar si el recurso existe o no.
    """
    async def _verificar(usuario: Usuario = Depends(get_current_user)) -> Usuario:
        if usuario.rol not in roles_permitidos:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tiene permisos para esta acción.")
        return usuario
    return _verificar

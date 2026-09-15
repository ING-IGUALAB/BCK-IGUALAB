
from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, esquema_bearer
from app.models import Usuario
from app.schemas import (
    LoginRequest, LoginResponse, SolicitarRecuperacionRequest,
    RestablecerPasswordRequest, CambiarPasswordRequest,
)
from app.services import auth_service
from app.security import decodificar_token, verificar_password, hash_password, validar_politica_password
from fastapi import HTTPException

router = APIRouter(prefix="/auth", tags=["Autenticación"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """RF-001. Ver app/services/auth_service.py:autenticar para las reglas completas."""
    token, usuario = await auth_service.autenticar(db, payload.correo, payload.password)
    return LoginResponse(access_token=token, rol=usuario.rol, nombre=usuario.nombre)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    credenciales: HTTPAuthorizationCredentials = Depends(esquema_bearer),
    usuario: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """RF-006: cierre de sesión manual."""
    payload = decodificar_token(credenciales.credentials)
    await auth_service.cerrar_sesion(db, sesion_id=payload["jti"])


@router.post("/recuperar-contrasena", status_code=status.HTTP_202_ACCEPTED)
async def solicitar_recuperacion(payload: SolicitarRecuperacionRequest, db: AsyncSession = Depends(get_db)):

    await auth_service.solicitar_recuperacion(db, payload.correo)
    return {"mensaje": "Si el correo está registrado, recibirá un enlace de recuperación en los próximos minutos."}


@router.post("/restablecer-contrasena")
async def restablecer_password(payload: RestablecerPasswordRequest, db: AsyncSession = Depends(get_db)):
    """RF-003."""
    await auth_service.restablecer_password(db, payload.token, payload.password_nueva)
    return {"mensaje": "Contraseña actualizada correctamente. Ya puede iniciar sesión."}


@router.patch("/mi-contrasena")
async def cambiar_mi_password(
    payload: CambiarPasswordRequest,
    usuario: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verificar_password(payload.password_actual, usuario.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "La contraseña actual no es correcta.")
    errores = validar_politica_password(payload.password_nueva, usuario.correo)
    if errores:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"errores": errores})
    usuario.password_hash = hash_password(payload.password_nueva)
    await db.commit()
    return {"mensaje": "Contraseña actualizada correctamente."}
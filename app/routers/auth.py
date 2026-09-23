from fastapi import APIRouter, status

from app.schemas import (
    LoginRequest, LoginResponse, SolicitarRecuperacionRequest,
    RestablecerPasswordRequest, CambiarPasswordRequest,
)
from app.services import auth_service
from app.security import decodificar_token, verificar_password, hash_password, validar_politica_password
from app.exceptions import AuthenticationError, BusinessValidationError
from app.dependencies import (
    BearerCredentials,
    CurrentUser,
    DatabaseSession,
)

router = APIRouter(prefix="/auth", tags=["Autenticación"])

@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: DatabaseSession,
):
    token, usuario = await auth_service.autenticar(
        db,
        payload.correo,
        payload.password,
    )

    return LoginResponse(
        access_token=token,
        rol=usuario.rol,
        nombre=usuario.nombre,
    )

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    credenciales: BearerCredentials,
    usuario: CurrentUser,
    db: DatabaseSession,
):
    payload = decodificar_token(credenciales.credentials)

    await auth_service.cerrar_sesion(
        db,
        sesion_id=payload["jti"],
    )

@router.post(
    "/recuperar-contrasena",
    status_code=status.HTTP_202_ACCEPTED,
)
async def solicitar_recuperacion(
    payload: SolicitarRecuperacionRequest,
    db: DatabaseSession,
):
    await auth_service.solicitar_recuperacion(
        db,
        payload.correo,
    )

    return {
        "mensaje": (
            "Si el correo está registrado, recibirá un enlace "
            "de recuperación en los próximos minutos."
        )
    }


@router.post("/restablecer-contrasena")
async def restablecer_password(
    payload: RestablecerPasswordRequest,
    db: DatabaseSession,
):
    await auth_service.restablecer_password(
        db,
        payload.token,
        payload.password_nueva,
    )

    return {
        "mensaje": (
            "Contraseña actualizada correctamente. "
            "Ya puede iniciar sesión."
        )
    }

@router.patch("/mi-contrasena")
async def cambiar_mi_password(
    payload: CambiarPasswordRequest,
    usuario: CurrentUser,
    db: DatabaseSession,
):
    if not verificar_password(payload.password_actual, usuario.password_hash):
        raise AuthenticationError(
            "CURRENT_PASSWORD_INVALID",
            "La contraseña actual no es correcta.",
        )
    errores = validar_politica_password(payload.password_nueva, usuario.correo)
    if errores:
        raise BusinessValidationError(
            "PASSWORD_POLICY_VIOLATION",
            "La contraseña no cumple la política de seguridad.",
            details={"errors": errores},
        )
    usuario.password_hash = hash_password(payload.password_nueva)
    await db.commit()
    return {"mensaje": "Contraseña actualizada correctamente."}

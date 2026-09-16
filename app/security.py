
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from passlib.context import CryptContext

from app.config import settings

# hash de un solo sentido con sal (bcrypt genera la sal automáticamente
# y la incorpora al propio hash, por eso no se guarda en una columna aparte)
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")



# seguridad contraseñas

def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verificar_password(password_plano: str, password_hash: str) -> bool:
    return _pwd_context.verify(password_plano, password_hash)


def validar_politica_password(password: str, correo: str) -> list[str]:
    errores: list[str] = []
    if len(password) < 8:
        errores.append("La contraseña debe tener al menos 8 caracteres.")
    if not re.search(r"[A-Z]", password):
        errores.append("La contraseña debe incluir al menos una letra mayúscula.")
    if not re.search(r"[a-z]", password):
        errores.append("La contraseña debe incluir al menos una letra minúscula.")
    if not re.search(r"\d", password):
        errores.append("La contraseña debe incluir al menos un dígito.")
    if not re.search(r"[^\w\s]", password):
        errores.append("La contraseña debe incluir al menos un carácter especial.")
    if correo and password.lower() == correo.lower():
        errores.append("La contraseña no puede coincidir con el correo de la cuenta.")
    return errores



# JWT


def crear_access_token(usuario_id: uuid.UUID, rol: str, sesion_id: uuid.UUID) -> str:

    ahora = datetime.now(timezone.utc)
    payload = {
        "sub": str(usuario_id),
        "rol": rol,
        "jti": str(sesion_id),
        "iat": ahora,
        "exp": ahora + timedelta(hours=8),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decodificar_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


# tokens de recuperación de contraseña

def generar_token_recuperacion() -> str:
    return secrets.token_urlsafe(32)


def hash_token_recuperacion(token: str) -> str:
    return _pwd_context.hash(token)


def verificar_token_recuperacion(token_plano: str, token_hash: str) -> bool:
    return _pwd_context.verify(token_plano, token_hash)

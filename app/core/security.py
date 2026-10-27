
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from passlib.context import CryptContext
from passlib.utils import MAX_PASSWORD_SIZE

from app.core.config import settings

# hash de un solo sentido con sal (bcrypt genera la sal automáticamente
# y la incorpora al propio hash, por eso no se guarda en una columna aparte)
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")



# seguridad contraseñas

# Límite REAL de la biblioteca (passlib 1.7.4): más de 4096 caracteres lanza PasswordSizeError. Además bcrypt solo usa los primeros
# 72 bytes (passlib trunca en silencio: comportamiento previo que no se modifica aquí).
LONGITUD_MAXIMA_PASSWORD = MAX_PASSWORD_SIZE


def password_procesable(valor: str) -> bool:
    """¿Puede la biblioteca de hash procesar este texto? Falso con un NUL (PasswordValueError), con más de 4096 caracteres
    (PasswordSizeError) o con un sustituto suelto que no se codifica en UTF-8 (UnicodeEncodeError). Un texto así NUNCA pudo ser la
    contraseña de una cuenta, porque la misma biblioteca no habría podido crear su hash."""
    return len(valor) <= LONGITUD_MAXIMA_PASSWORD and "\x00" not in valor and _es_codificable(valor)


def _es_codificable(valor: str) -> bool:
    """Falso si el texto contiene sustitutos sueltos (U+D800..U+DFFF), que no se pueden codificar en UTF-8."""
    try:
        valor.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verificar_password(password_plano: str, password_hash: str) -> bool:
    """Falso si el texto recibido no es procesable (no puede ser la contraseña de nadie). Solo se evita ese caso esperado: un fallo de la
    biblioteca o un hash corrupto sigue propagándose como error, no se confunde con «credenciales incorrectas»."""
    if not password_procesable(password_plano):
        return False
    return _pwd_context.verify(password_plano, password_hash)


def validar_politica_password(password: str, correo: str) -> list[str]:
    errores: list[str] = []
    # Condiciones que la biblioteca de hash no puede procesar: se informan como incumplimiento de la política, nunca como un 500.
    if len(password) > LONGITUD_MAXIMA_PASSWORD:
        errores.append(f"La contraseña no puede superar {LONGITUD_MAXIMA_PASSWORD} caracteres.")
    if "\x00" in password:
        errores.append("La contraseña no puede contener caracteres nulos.")
    if not _es_codificable(password):
        errores.append("La contraseña contiene caracteres no válidos.")
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
    # El token llega del cliente: uno con NUL, demasiado largo o no codificable no pudo ser nunca uno emitido (son 43 caracteres base64url).
    if not password_procesable(token_plano):
        return False
    return _pwd_context.verify(token_plano, token_hash)


import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.email_service import enviar_correo_recuperacion
from app.models import Usuario, Sesion, TokenRecuperacion, TipoEventoAuditoria
from app.security import (
    verificar_password, crear_access_token, hash_password,
    generar_token_recuperacion, hash_token_recuperacion, verificar_token_recuperacion,
    validar_politica_password,
)
from app.audit import registrar_evento

from app.exceptions import (
    AccountDisabledError,
    AccountLockedError,
    AuthenticationError,
    BusinessValidationError,
)


# nunca debe distinguirse "no existe la cuenta" de "credencial incorrecta".
_MENSAJE_CREDENCIALES_INVALIDAS = "Credenciales inválidas."


_HASH_CRONOMETRO = hash_password("valor-que-nunca-se-usa-para-autenticar-una-cuenta-real")

async def autenticar(db: AsyncSession, correo: str, password: str) -> tuple[str, Usuario]:
    """
    RF-001: autenticación con correo y contraseña.
    RN-004: solo cuentas registradas y habilitadas pueden autenticarse.
    RNF-031: bloqueo temporal tras 5 intentos fallidos consecutivos.

    Devuelve (access_token, usuario) o lanza una excepción de aplicación si falla.
    """
    resultado = await db.execute(select(Usuario).where(Usuario.correo == correo.lower()))
    usuario = resultado.scalar_one_or_none()

    # el mensaje de error no distingue "no existe" de "clave mala"
    # por eso seguimos evaluando aunque `usuario` sea None, para no filtrar
    # información por tiempos de respuesta distintos entre ambos casos.
    if usuario is None:
        verificar_password(password, _HASH_CRONOMETRO)
        raise AuthenticationError("INVALID_CREDENTIALS", _MENSAJE_CREDENCIALES_INVALIDAS)

    ahora = datetime.now(timezone.utc)
    if usuario.bloqueado_hasta and usuario.bloqueado_hasta > ahora:
        minutos_restantes = int((usuario.bloqueado_hasta - ahora).total_seconds() // 60) + 1
        raise AccountLockedError(
            "ACCOUNT_LOCKED",
            f"Cuenta bloqueada temporalmente. Intenta de nuevo en {minutos_restantes} minutos",
            details={"minutes_remaining": minutos_restantes},
        )

    if not usuario.habilitado:
        raise AccountDisabledError(
            "ACCOUNT_DISABLED",
            "Usuario deshabilitado. Contacte al administrador.",
        )

    if not verificar_password(password, usuario.password_hash):
        await _registrar_intento_fallido(db, usuario)
        raise AuthenticationError("INVALID_CREDENTIALS", _MENSAJE_CREDENCIALES_INVALIDAS)

    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None

    sesion = Sesion(usuario_id=usuario.id)
    db.add(sesion)
    await db.flush()

    token = crear_access_token(usuario.id, usuario.rol.value, sesion.id)

    registrar_evento(
        db, TipoEventoAuditoria.INICIO_SESION,
        f"Login exitoso ({usuario.rol.value})", usuario_id=usuario.id
    )
    await db.commit()
    return token, usuario


async def _registrar_intento_fallido(db: AsyncSession, usuario: Usuario) -> None:
    usuario.intentos_fallidos += 1
    if usuario.intentos_fallidos >= settings.MAX_LOGIN_ATTEMPTS:
        usuario.bloqueado_hasta = datetime.now(timezone.utc) + timedelta(
            minutes=settings.LOGIN_LOCKOUT_MINUTES
        )
        registrar_evento(
            db, TipoEventoAuditoria.INICIO_SESION,
            f"Cuenta bloqueada tras {usuario.intentos_fallidos} intentos fallidos",
            usuario_id=usuario.id,
        )
    await db.commit()


async def cerrar_sesion(db: AsyncSession, sesion_id: uuid.UUID) -> None:
    """RF-006: cierre de sesión manual, invalidando la sesión activa."""
    resultado = await db.execute(select(Sesion).where(Sesion.id == sesion_id))
    sesion = resultado.scalar_one_or_none()
    if sesion:
        sesion.revocada = True
        await db.commit()


async def solicitar_recuperacion(db: AsyncSession, correo: str) -> None:
    resultado = await db.execute(select(Usuario).where(Usuario.correo == correo.lower()))
    usuario = resultado.scalar_one_or_none()
    if usuario is None:
        return

    await db.execute(
        update(TokenRecuperacion)
        .where(TokenRecuperacion.usuario_id == usuario.id, TokenRecuperacion.usado.is_(False))
        .values(usado=True)
    )

    token_plano = generar_token_recuperacion()
    registro = TokenRecuperacion(
        usuario_id=usuario.id,
        token_hash=hash_token_recuperacion(token_plano),
        expira_en=datetime.now(timezone.utc) + timedelta(
            minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
        ),
    )
    db.add(registro)
    await db.commit()

    await enviar_correo_recuperacion(usuario.correo, token_plano)


async def restablecer_password(db: AsyncSession, token_plano: str, password_nueva: str) -> None:
    resultado = await db.execute(
    select(TokenRecuperacion)
    .where(
        TokenRecuperacion.usado.is_(False),
        TokenRecuperacion.expira_en > func.now(),
    )
    .order_by(TokenRecuperacion.creado_en.desc())
    )

    candidatos = resultado.scalars().all()


    registro = next(
        (t for t in candidatos if verificar_token_recuperacion(token_plano, t.token_hash)),
        None,
    )
    if registro is None:
        raise BusinessValidationError(
            "INVALID_OR_EXPIRED_RESET_TOKEN",
            "El enlace de recuperación es inválido o ha expirado.",
        )

    errores = validar_politica_password(password_nueva, correo="")
    if errores:
        raise BusinessValidationError(
            "PASSWORD_POLICY_VIOLATION",
            "La contraseña no cumple la política de seguridad.",
            details={"errors": errores},
        )

    usuario = await db.get(Usuario, registro.usuario_id)
    usuario.password_hash = hash_password(password_nueva)

    await db.execute(
        update(TokenRecuperacion)
        .where(
            TokenRecuperacion.usuario_id == usuario.id,
            TokenRecuperacion.usado.is_(False),
        )
        .values(usado=True)
    )

    # tras restablecer la contraseña, se cierran todas las sesiones activas
    resultado_sesiones = await db.execute(
        select(Sesion).where(Sesion.usuario_id == usuario.id, Sesion.revocada.is_(False))
    )
    for s in resultado_sesiones.scalars().all():
        s.revocada = True

    await db.commit()

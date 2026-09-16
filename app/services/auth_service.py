"""
Servicio de Autenticación.

Cada función corresponde a un caso de uso completo (no a una operación CRUD
suelta), para que el router solo tenga que llamar UNA función por endpoint.
Esto es intencional por mantenibilidad: si mañana cambia una regla (ej. el
tiempo de bloqueo), se toca un solo lugar.
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
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


# Mensaje genérico reutilizado en dos lugares distintos (RNF-007, RNF-008):
# nunca debe distinguirse "no existe la cuenta" de "credencial incorrecta".
_MENSAJE_CREDENCIALES_INVALIDAS = "Credenciales inválidas."


_HASH_CRONOMETRO = hash_password("valor-que-nunca-se-usa-para-autenticar-una-cuenta-real")

async def autenticar(db: AsyncSession, correo: str, password: str) -> tuple[str, Usuario]:
    """
    RF-001: autenticación con correo y contraseña.
    RN-004: solo cuentas registradas y habilitadas pueden autenticarse.
    RNF-031: bloqueo temporal tras 5 intentos fallidos consecutivos.

    Devuelve (access_token, usuario) o lanza HTTPException si falla.
    """
    resultado = await db.execute(select(Usuario).where(Usuario.correo == correo.lower()))
    usuario = resultado.scalar_one_or_none()

    # RNF-008: el mensaje de error no distingue "no existe" de "clave mala" —
    # por eso seguimos evaluando aunque `usuario` sea None, para no filtrar
    # información por tiempos de respuesta distintos entre ambos casos.
    if usuario is None:
        verificar_password(password, _HASH_CRONOMETRO)  # solo para consumir tiempo
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _MENSAJE_CREDENCIALES_INVALIDAS)

    # --- RNF-031: verificar si la cuenta está bloqueada por intentos fallidos ---
    ahora = datetime.now(timezone.utc)
    if usuario.bloqueado_hasta and usuario.bloqueado_hasta > ahora:
        minutos_restantes = int((usuario.bloqueado_hasta - ahora).total_seconds() // 60) + 1
        raise HTTPException(
            status.HTTP_423_LOCKED,
            f"Cuenta bloqueada temporalmente. Intenta de nuevo en {minutos_restantes} minuto(s)."
        )

    # RN-005 / flujo alternativo de CU001: cuenta deshabilitada.
    if not usuario.habilitado:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Usuario deshabilitado. Contacte al administrador.")

    if not verificar_password(password, usuario.password_hash):
        await _registrar_intento_fallido(db, usuario)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _MENSAJE_CREDENCIALES_INVALIDAS)

    # Login correcto: resetear contador de intentos fallidos.
    usuario.intentos_fallidos = 0
    usuario.bloqueado_hasta = None

    sesion = Sesion(usuario_id=usuario.id)
    db.add(sesion)
    await db.flush()  # necesitamos sesion.id (jti) antes de generar el token

    token = crear_access_token(usuario.id, usuario.rol.value, sesion.id)

    await registrar_evento(
        db, TipoEventoAuditoria.INICIO_SESION,
        f"Login exitoso ({usuario.rol.value})", usuario_id=usuario.id
    )
    await db.commit()
    return token, usuario


async def _registrar_intento_fallido(db: AsyncSession, usuario: Usuario) -> None:
    """RNF-031: incrementa el contador y bloquea la cuenta al llegar al máximo."""
    usuario.intentos_fallidos += 1
    if usuario.intentos_fallidos >= settings.MAX_LOGIN_ATTEMPTS:
        usuario.bloqueado_hasta = datetime.now(timezone.utc) + timedelta(
            minutes=settings.LOGIN_LOCKOUT_MINUTES
        )
        # El bloqueo en sí también es un evento auditable (RNF-031, última línea).
        await registrar_evento(
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
    """
    RF-002: genera un enlace de recuperación válido por 30 minutos.
    RNF-007: SIEMPRE se comporta igual, exista o no la cuenta con ese correo
    — por eso esta función nunca lanza una excepción distinta según el caso,
    y el router siempre responde el mismo mensaje genérico al usuario.
    """
    resultado = await db.execute(select(Usuario).where(Usuario.correo == correo.lower()))
    usuario = resultado.scalar_one_or_none()
    if usuario is None:
        return  # silencioso a propósito (RNF-007/008)

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

    # Nunca se loggea ni se devuelve en la respuesta HTTP el token en texto
    # plano — solo viaja por el canal de correo electrónico.
    
    await enviar_correo_recuperacion(usuario.correo, token_plano)


async def restablecer_password(db: AsyncSession, token_plano: str, password_nueva: str) -> None:
    """RF-003: valida el token (un solo uso, no vencido) y actualiza la contraseña."""
    ahora = datetime.now(timezone.utc)
    resultado = await db.execute(
        select(TokenRecuperacion).where(TokenRecuperacion.usado.is_(False))
    )
    candidatos = resultado.scalars().all()

    # Se compara contra el hash de cada token vigente (no se puede hacer un
    # WHERE directo porque solo guardamos el hash, nunca el valor plano).
    registro = next(
        (t for t in candidatos if verificar_token_recuperacion(token_plano, t.token_hash)),
        None,
    )
    if registro is None or registro.expira_en < ahora:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El enlace de recuperación es inválido o ha expirado.")

    errores = validar_politica_password(password_nueva, correo="")
    if errores:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"errores": errores})

    usuario = await db.get(Usuario, registro.usuario_id)
    usuario.password_hash = hash_password(password_nueva)
    registro.usado = True  # RF-003: el enlace se invalida al usarse

    # RF-009 (aplicado también aquí, por buena práctica de seguridad):
    # tras restablecer la contraseña, se cierran todas las sesiones activas.
    resultado_sesiones = await db.execute(
        select(Sesion).where(Sesion.usuario_id == usuario.id, Sesion.revocada.is_(False))
    )
    for s in resultado_sesiones.scalars().all():
        s.revocada = True

    await db.commit()


import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Usuario, RolUsuario, Sesion, TipoEventoAuditoria
from app.security import hash_password, validar_politica_password
from app.audit import registrar_evento
from app.exceptions import (
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    NotFoundError,
)


async def crear_usuario(db: AsyncSession, nombre: str, correo: str, password: str) -> Usuario:

    correo = correo.lower()

    # rechazar correo ya existente
    existente = await db.execute(select(Usuario).where(Usuario.correo == correo))
    if existente.scalar_one_or_none() is not None:
        raise ConflictError(
            "EMAIL_ALREADY_EXISTS",
            "Ya existe una cuenta registrada con ese correo.",
        )

    errores = validar_politica_password(password, correo)
    if errores:
        raise BusinessValidationError(
            "PASSWORD_POLICY_VIOLATION",
            "La contraseña no cumple la política de seguridad.",
            details={"errors": errores},
        )

    usuario = Usuario(
        nombre=nombre,
        correo=correo,
        password_hash=hash_password(password),
        rol=RolUsuario.ADMINISTRADOR,  # ver docstring
        habilitado=True,
    )
    db.add(usuario)
    await db.commit()
    await db.refresh(usuario)
    return usuario


async def listar_usuarios(db: AsyncSession) -> list[Usuario]:
    resultado = await db.execute(select(Usuario).order_by(Usuario.creado_en))
    return list(resultado.scalars().all())


async def cambiar_estado_usuario(db: AsyncSession, usuario_id: uuid.UUID, habilitar: bool, actor: Usuario) -> Usuario:
    usuario = await db.get(Usuario, usuario_id)
    if usuario is None:
        raise NotFoundError("USER_NOT_FOUND", "Cuenta no encontrada.")

    if usuario.rol == RolUsuario.SUPERADMIN and not habilitar:
        raise ConflictError(
            "SUPERADMIN_CANNOT_BE_DISABLED",
            "No se puede deshabilitar la cuenta SuperAdmin. Transfiera el rol antes.",
        )

    usuario.habilitado = habilitar

    if not habilitar:
        resultado = await db.execute(
            select(Sesion).where(Sesion.usuario_id == usuario.id, Sesion.revocada.is_(False))
        )
        for s in resultado.scalars().all():
            s.revocada = True

    registrar_evento(
        db, TipoEventoAuditoria.CAMBIO_ROL,
        f"{'Habilitó' if habilitar else 'Deshabilitó'} la cuenta de '{usuario.nombre}'",
        usuario_id=actor.id,
    )
    await db.commit()
    await db.refresh(usuario)
    return usuario


async def transferir_superadmin(db: AsyncSession, origen: Usuario, destino_id: uuid.UUID) -> None:

    if origen.rol != RolUsuario.SUPERADMIN:
        raise AuthorizationError(
            "SUPERADMIN_REQUIRED",
            "Solo el SuperAdmin puede transferir este rol.",
        )

    destino = await db.get(Usuario, destino_id)

    # RF-015: condiciones de rechazo de la transferencia.
    if destino is None:
        raise NotFoundError("DESTINATION_USER_NOT_FOUND", "La cuenta destino no existe.")
    if not destino.habilitado:
        raise ConflictError(
            "DESTINATION_USER_DISABLED",
            "La cuenta destino debe estar habilitada.",
        )
    if destino.rol == RolUsuario.SUPERADMIN:
        raise ConflictError(
            "DESTINATION_ALREADY_SUPERADMIN",
            "La cuenta destino ya tiene el rol SuperAdmin.",
        )

    origen.rol = RolUsuario.ADMINISTRADOR
    await db.flush()
    destino.rol = RolUsuario.SUPERADMIN

    registrar_evento(
        db, TipoEventoAuditoria.CAMBIO_ROL,
        f"Transfirió el rol SuperAdmin de '{origen.nombre}' a '{destino.nombre}'",
        usuario_id=origen.id,
    )
    await db.commit()

    # si la cuenta origen tiene una sesión activa en este momento, su próximo request ya verá el rol actualizado
    # porque get_current_user (dependencies.py) siempre lee el rol vigente desde
    # la tabla Usuario, nunca confía en el valor que traía el JWT viejo

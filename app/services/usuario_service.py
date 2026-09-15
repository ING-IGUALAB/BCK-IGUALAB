"""
Servicio de gestión de cuentas (usuarios).

Todas las funciones asumen que la autorización de rol (¿puede este llamador
ejecutar esto?) YA se validó en el router vía dependencies.py — este
servicio solo se preocupa de las reglas de NEGOCIO, no de permisos de acceso.
Mantener esa separación es lo que permite reusar estas funciones más
adelante (ej. desde un script de administración) sin arrastrar lógica HTTP.
"""
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models import Usuario, RolUsuario, Sesion, TipoEventoAuditoria
from app.security import hash_password, validar_politica_password
from app.audit import registrar_evento


async def crear_usuario(db: AsyncSession, nombre: str, correo: str, password: str) -> Usuario:
    """
    RF-010: el SuperAdmin crea cuentas; nace SIEMPRE con rol Administrador
    (RN-008) — este servicio ni siquiera acepta un parámetro de rol, para
    que sea imposible crear un SuperAdmin por esta vía aunque el router
    tuviera un bug.
    """
    correo = correo.lower()

    # RF-011: rechazar correo ya existente.
    existente = await db.execute(select(Usuario).where(Usuario.correo == correo))
    if existente.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una cuenta registrada con ese correo.")

    errores = validar_politica_password(password, correo)
    if errores:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"errores": errores})

    usuario = Usuario(
        nombre=nombre,
        correo=correo,
        password_hash=hash_password(password),
        rol=RolUsuario.ADMINISTRADOR,  # RN-008, ver docstring
        habilitado=True,
    )
    db.add(usuario)
    await db.commit()
    await db.refresh(usuario)
    return usuario


async def listar_usuarios(db: AsyncSession) -> list[Usuario]:
    """RF-016: listar cuentas con su rol y estado."""
    resultado = await db.execute(select(Usuario).order_by(Usuario.creado_en))
    return list(resultado.scalars().all())


async def cambiar_estado_usuario(db: AsyncSession, usuario_id: uuid.UUID, habilitar: bool) -> Usuario:
    """
    RF-012: habilitar/deshabilitar una cuenta.
    RF-013: el SuperAdmin no puede deshabilitarse a sí mismo por esta vía —
    debe transferir el rol primero.
    RF-009: al deshabilitar, se invalidan TODAS sus sesiones activas.
    """
    usuario = await db.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cuenta no encontrada.")

    if usuario.rol == RolUsuario.SUPERADMIN and not habilitar:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No se puede deshabilitar la cuenta SuperAdmin. Transfiera el rol antes."
        )

    usuario.habilitado = habilitar

    if not habilitar:
        # RF-009: invalidar todas las sesiones activas de la cuenta.
        resultado = await db.execute(
            select(Sesion).where(Sesion.usuario_id == usuario.id, Sesion.revocada.is_(False))
        )
        for s in resultado.scalars().all():
            s.revocada = True

    await registrar_evento(
        db, TipoEventoAuditoria.CAMBIO_ROL,
        f"{'Habilitó' if habilitar else 'Deshabilitó'} la cuenta de '{usuario.nombre}'",
    )
    await db.commit()
    await db.refresh(usuario)
    return usuario


async def transferir_superadmin(db: AsyncSession, origen: Usuario, destino_id: uuid.UUID) -> None:
    """
    RF-014 / RF-015 / RN-009: transferencia atómica del rol SuperAdmin.
    RNF-011: toda la operación vive dentro de una sola transacción — si
    algo falla a mitad de camino, SQLAlchemy revierte ambos cambios
    (no se necesita código adicional de "rollback manual").

    `origen` se recibe ya cargado (viene de la dependencia get_current_user)
    para no hacer una consulta extra innecesaria.
    """
    if origen.rol != RolUsuario.SUPERADMIN:
        # Esto no debería poder pasar si el router valida el rol antes,
        # pero se deja como segunda barrera defensiva (RNF-004).
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el SuperAdmin puede transferir este rol.")

    destino = await db.get(Usuario, destino_id)

    # RF-015: condiciones de rechazo de la transferencia.
    if destino is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "La cuenta destino no existe.")
    if not destino.habilitado:
        raise HTTPException(status.HTTP_409_CONFLICT, "La cuenta destino debe estar habilitada.")
    if destino.rol == RolUsuario.SUPERADMIN:
        raise HTTPException(status.HTTP_409_CONFLICT, "La cuenta destino ya tiene el rol SuperAdmin.")

    # --- Transferencia atómica (RN-009) ---
    origen.rol = RolUsuario.ADMINISTRADOR
    destino.rol = RolUsuario.SUPERADMIN

    await registrar_evento(
        db, TipoEventoAuditoria.CAMBIO_ROL,
        f"Transfirió el rol SuperAdmin de '{origen.nombre}' a '{destino.nombre}'",
        usuario_id=origen.id,
    )
    # Un solo commit para ambos cambios + el registro de auditoría: si algo
    # de esto falla, SQLAlchemy revierte todo el bloque (atomicidad real).
    await db.commit()

    # RF-050: si la cuenta origen tiene una sesión activa en este momento,
    # su próximo request ya verá el rol actualizado — porque
    # get_current_user (dependencies.py) siempre lee el rol vigente desde
    # la tabla Usuario, nunca confía en el valor que traía el JWT viejo.

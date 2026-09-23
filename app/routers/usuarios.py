import uuid

from fastapi import APIRouter

from app.dependencies import (
    DatabaseSession,
    SuperAdminUser,
)
from app.schemas import (
    CrearUsuarioRequest,
    TransferirSuperAdminRequest,
    UsuarioResponse,
)
from app.services import usuario_service

router = APIRouter(prefix="/usuarios", tags=["Gestión de Usuarios"])


@router.post("", response_model=UsuarioResponse, status_code=201)
async def crear_usuario(payload: CrearUsuarioRequest, _actor: SuperAdminUser, db: DatabaseSession):
    # RF-010 / RF-011
    return await usuario_service.crear_usuario(db, payload.nombre, payload.correo, payload.password)


@router.get("", response_model=list[UsuarioResponse])
async def listar_usuarios(_actor: SuperAdminUser, db: DatabaseSession):
    return await usuario_service.listar_usuarios(db)


@router.patch("/{usuario_id}/habilitar", response_model=UsuarioResponse)
async def habilitar_usuario(
    usuario_id: uuid.UUID,
    actor: SuperAdminUser,
    db: DatabaseSession,
):
    return await usuario_service.cambiar_estado_usuario(
        db,
        usuario_id,
        habilitar=True,
        actor=actor,
    )


@router.patch("/{usuario_id}/deshabilitar", response_model=UsuarioResponse)
async def deshabilitar_usuario(
    usuario_id: uuid.UUID,
    actor: SuperAdminUser,
    db: DatabaseSession,
):
    return await usuario_service.cambiar_estado_usuario(
        db,
        usuario_id,
        habilitar=False,
        actor=actor,
    )


@router.post("/transferir-superadmin", status_code=204)
async def transferir_superadmin(
    payload: TransferirSuperAdminRequest,
    origen: SuperAdminUser,
    db: DatabaseSession,
):
    await usuario_service.transferir_superadmin(db, origen, payload.cuenta_destino_id)

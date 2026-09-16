
import uuid
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import requerir_rol
from app.models import Usuario, RolUsuario
from app.schemas import CrearUsuarioRequest, UsuarioResponse, TransferirSuperAdminRequest
from app.services import usuario_service

router = APIRouter(prefix="/usuarios", tags=["Gestión de Usuarios"])

_solo_superadmin = Depends(requerir_rol(RolUsuario.SUPERADMIN))


@router.post("", response_model=UsuarioResponse, status_code=201, dependencies=[_solo_superadmin])
async def crear_usuario(payload: CrearUsuarioRequest, db: AsyncSession = Depends(get_db)):
    # RF-010 / RF-011
    return await usuario_service.crear_usuario(db, payload.nombre, payload.correo, payload.password)


@router.get("", response_model=list[UsuarioResponse], dependencies=[_solo_superadmin])
async def listar_usuarios(db: AsyncSession = Depends(get_db)):
    # RF-016
    return await usuario_service.listar_usuarios(db)


@router.patch("/{usuario_id}/habilitar", response_model=UsuarioResponse, dependencies=[_solo_superadmin])
async def habilitar_usuario(usuario_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # RF-012
    return await usuario_service.cambiar_estado_usuario(db, usuario_id, habilitar=True)


@router.patch("/{usuario_id}/deshabilitar", response_model=UsuarioResponse, dependencies=[_solo_superadmin])
async def deshabilitar_usuario(usuario_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await usuario_service.cambiar_estado_usuario(db, usuario_id, habilitar=False)


@router.post("/transferir-superadmin", status_code=204)
async def transferir_superadmin(
    payload: TransferirSuperAdminRequest,
    origen: Usuario = Depends(requerir_rol(RolUsuario.SUPERADMIN)),
    db: AsyncSession = Depends(get_db),
):
    """RF-014 / RF-015 / RN-009. Requiere ser el SuperAdmin actual (no se
    inyecta como dependencia genérica arriba porque necesitamos el objeto
    `origen` completo, no solo validar el rol)."""
    await usuario_service.transferir_superadmin(db, origen, payload.cuenta_destino_id)

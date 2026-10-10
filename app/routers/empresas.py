import uuid

from fastapi import APIRouter

from app.dependencies import CurrentUser, DatabaseSession, SuperAdminUser
from app.exceptions import AuthorizationError
from app.models import RolUsuario, SectorEmpresa
from app.schemas import CrearEmpresaRequest, EditarEmpresaRequest, EmpresaResponse
from app.services import empresa_service

router = APIRouter(prefix="/empresas", tags=["Catálogo de Empresas"])


@router.post("", response_model=EmpresaResponse, status_code=201)
async def crear_empresa(payload: CrearEmpresaRequest, actor: SuperAdminUser, db: DatabaseSession):
    return await empresa_service.crear_empresa(db, payload)


@router.get("", response_model=list[EmpresaResponse])
async def listar_empresas(
    actor: CurrentUser,
    db: DatabaseSession,
    solo_activas: bool = False,
    sector: SectorEmpresa | None = None,
):
    if actor.rol != RolUsuario.SUPERADMIN and not solo_activas:
        raise AuthorizationError("FORBIDDEN", "Solo puede consultar empresas activas.")
    return await empresa_service.listar_empresas(db, solo_activas, sector)


@router.get("/{empresa_id}", response_model=EmpresaResponse)
async def obtener_empresa(empresa_id: uuid.UUID, actor: SuperAdminUser, db: DatabaseSession):
    return await empresa_service.obtener_empresa(db, empresa_id)


@router.patch("/{empresa_id}", response_model=EmpresaResponse)
async def editar_empresa(
    empresa_id: uuid.UUID, payload: EditarEmpresaRequest, actor: SuperAdminUser, db: DatabaseSession
):
    return await empresa_service.editar_empresa(db, empresa_id, payload)


@router.patch("/{empresa_id}/activar", response_model=EmpresaResponse)
async def activar_empresa(empresa_id: uuid.UUID, actor: SuperAdminUser, db: DatabaseSession):
    return await empresa_service.cambiar_estado_empresa(db, empresa_id, activa=True)


@router.patch("/{empresa_id}/desactivar", response_model=EmpresaResponse)
async def desactivar_empresa(empresa_id: uuid.UUID, actor: SuperAdminUser, db: DatabaseSession):
    return await empresa_service.cambiar_estado_empresa(db, empresa_id, activa=False)

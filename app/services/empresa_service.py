import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ConflictError, NotFoundError
from app.models import Empresa, SectorEmpresa
from app.schemas import CrearEmpresaRequest, EditarEmpresaRequest


def _duplicada() -> ConflictError:
    return ConflictError(
        "COMPANY_ALREADY_EXISTS",
        "Ya existe otra empresa registrada con ese nombre.",
    )


async def _validar_nombre_disponible(
    db: AsyncSession, nombre: str, empresa_id: uuid.UUID | None = None
) -> None:
    consulta = select(Empresa.id).where(
        func.lower(func.trim(Empresa.nombre)) == func.lower(nombre.strip())
    )
    if empresa_id is not None:
        consulta = consulta.where(Empresa.id != empresa_id)
    if (await db.execute(consulta)).scalar_one_or_none() is not None:
        raise _duplicada()


async def _guardar(db: AsyncSession, empresa: Empresa) -> Empresa:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # asyncpg conserva el nombre del índice en la causa del error adaptado.
        causa = getattr(exc.orig, "__cause__", None)
        restriccion = getattr(causa, "constraint_name", None)
        if restriccion == "uq_empresas_nombre_normalizado" or (
            "uq_empresas_nombre_normalizado" in str(exc.orig)
        ):
            raise _duplicada() from exc
        raise
    await db.refresh(empresa)
    return empresa


async def crear_empresa(db: AsyncSession, payload: CrearEmpresaRequest) -> Empresa:
    await _validar_nombre_disponible(db, payload.nombre)
    empresa = Empresa(nombre=payload.nombre, sector=payload.sector, activa=True)
    db.add(empresa)
    return await _guardar(db, empresa)


async def listar_empresas(
    db: AsyncSession, solo_activas: bool = False, sector: SectorEmpresa | None = None
) -> list[Empresa]:
    consulta = select(Empresa)
    if solo_activas:
        consulta = consulta.where(Empresa.activa.is_(True))
    if sector is not None:
        consulta = consulta.where(Empresa.sector == sector)
    consulta = consulta.order_by(func.lower(Empresa.nombre), Empresa.id)
    return list((await db.execute(consulta)).scalars().all())


async def obtener_empresa(db: AsyncSession, empresa_id: uuid.UUID) -> Empresa:
    empresa = await db.get(Empresa, empresa_id)
    if empresa is None:
        raise NotFoundError("COMPANY_NOT_FOUND", "La empresa no existe.")
    return empresa


async def editar_empresa(
    db: AsyncSession, empresa_id: uuid.UUID, payload: EditarEmpresaRequest
) -> Empresa:
    empresa = await obtener_empresa(db, empresa_id)
    cambios = payload.model_dump(exclude_unset=True)
    if "nombre" in cambios:
        await _validar_nombre_disponible(db, cambios["nombre"], empresa_id)
    for campo, valor in cambios.items():
        setattr(empresa, campo, valor)
    return await _guardar(db, empresa)


async def cambiar_estado_empresa(
    db: AsyncSession, empresa_id: uuid.UUID, activa: bool
) -> Empresa:
    empresa = await obtener_empresa(db, empresa_id)
    empresa.activa = activa
    return await _guardar(db, empresa)

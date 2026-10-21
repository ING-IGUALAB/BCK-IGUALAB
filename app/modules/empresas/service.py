import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models import Empresa, SectorEmpresa
from app.models.documento_ingesta import Documento
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


async def obtener_empresa_bloqueada(
    db: AsyncSession, empresa_id: uuid.UUID, *, para_modificar: bool
) -> Empresa:
    """Lee la empresa tomando un bloqueo de FILA que dura hasta el commit/rollback de la transacción actual.

    `para_modificar=True` (FOR NO KEY UPDATE): quien va a cambiar el sector. `para_modificar=False` (FOR SHARE): quien va a
    crear un documento que copia el sector. Ambos modos son incompatibles entre sí, pero dos reservas (FOR SHARE) no se
    bloquean entre ellas y no se frena a nadie más: el FK `documentos.empresa_id` solo toma FOR KEY SHARE, compatible con
    ambos. `populate_existing` obliga a leer el valor vigente tras la espera aunque la sesión ya tuviera la empresa.
    En SQLite (pruebas) la cláusula no se emite.
    """
    consulta = (
        select(Empresa)
        .where(Empresa.id == empresa_id)
        .with_for_update(read=not para_modificar, key_share=para_modificar)
        .execution_options(populate_existing=True)
    )
    empresa = (await db.execute(consulta)).scalar_one_or_none()
    if empresa is None:
        raise NotFoundError("COMPANY_NOT_FOUND", "La empresa no existe.")
    return empresa


async def _tiene_documentos(db: AsyncSession, empresa_id: uuid.UUID) -> bool:
    """¿Existe alguna fila de `documentos` de la empresa? Sin filtrar por ambiente, año, tipo, resultado, estado ni
    reserva (un FALLIDO o una reserva liberada también cuentan). Solo la existencia: no se leen contenido ni originales."""
    consulta = select(Documento.id).where(Documento.empresa_id == empresa_id).limit(1)
    return (await db.execute(consulta)).first() is not None


async def _exigir_sector_modificable(db: AsyncSession, empresa_id: uuid.UUID, sector: SectorEmpresa) -> Empresa:
    """Con la fila de la empresa BLOQUEADA, rechaza un sector distinto si ya hay documentos. Libera el bloqueo al rechazar."""
    empresa = await obtener_empresa_bloqueada(db, empresa_id, para_modificar=True)
    if sector != empresa.sector and await _tiene_documentos(db, empresa_id):
        await db.rollback()
        raise ConflictError(
            "COMPANY_SECTOR_CHANGE_BLOCKED",
            "No se puede cambiar el sector de una empresa que ya tiene documentos asociados",
        )
    return empresa


async def editar_empresa(
    db: AsyncSession, empresa_id: uuid.UUID, payload: EditarEmpresaRequest
) -> Empresa:
    """Edita nombre y/o sector. El sector de una empresa con documentos no cambia (409): el rechazo ocurre ANTES de modificar
    nada, así que una petición mixta tampoco cambia el nombre. Comprobación y UPDATE comparten la transacción con el
    bloqueo de la fila (ver `obtener_empresa_bloqueada`), el mismo que toma la reserva de documentos."""
    cambios = payload.model_dump(exclude_unset=True)
    if "sector" in cambios:
        empresa = await _exigir_sector_modificable(db, empresa_id, cambios["sector"])
    else:
        empresa = await obtener_empresa(db, empresa_id)
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

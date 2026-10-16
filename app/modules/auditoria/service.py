
import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RegistroAuditoria, TipoEventoAuditoria


def registrar_evento(
    db: AsyncSession,
    tipo_evento: TipoEventoAuditoria,
    detalle: str,
    usuario_id: uuid.UUID | None = None,
) -> None:
    evento = RegistroAuditoria(usuario_id=usuario_id, tipo_evento=tipo_evento, detalle=detalle)
    db.add(evento)
    # No se hace commit aquí a propósito: el evento de auditoría se guarda
    # como parte de la misma transacción que la acción que lo originó

async def listar_eventos(
    db: AsyncSession,
    *,
    tipo_evento: TipoEventoAuditoria | None = None,
    usuario_id: uuid.UUID | None = None,
    desde: datetime | None = None,
    hasta: datetime | None = None,
    pagina: int = 1,
    tamano: int = 20,
) -> dict:
    """Consulta los eventos de auditoría con filtros (RF-030) y paginación (RF-029). Solo lectura:
    los eventos son inmutables (append-only), no se modifican ni se borran."""
    from sqlalchemy import and_, func, select, true

    condiciones = []
    if tipo_evento is not None:
        condiciones.append(RegistroAuditoria.tipo_evento == tipo_evento)
    if usuario_id is not None:
        condiciones.append(RegistroAuditoria.usuario_id == usuario_id)
    if desde is not None:
        condiciones.append(RegistroAuditoria.fecha_hora_utc >= desde)
    if hasta is not None:
        condiciones.append(RegistroAuditoria.fecha_hora_utc <= hasta)
    where = and_(*condiciones) if condiciones else true()

    total = (
        await db.execute(select(func.count()).select_from(RegistroAuditoria).where(where))
    ).scalar_one()

    filas = (
        await db.execute(
            select(RegistroAuditoria)
            .where(where)
            .order_by(RegistroAuditoria.fecha_hora_utc.desc())
            .offset((pagina - 1) * tamano)
            .limit(tamano)
        )
    ).scalars().all()

    return {
        "items": [
            {
                "id": f.id,
                "usuario_id": f.usuario_id,
                "tipo_evento": f.tipo_evento,
                "detalle": f.detalle,
                "fecha_hora_utc": f.fecha_hora_utc,
            }
            for f in filas
        ],
        "pagina": pagina,
        "tamano": tamano,
        "total": total,
    }

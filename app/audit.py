
import uuid
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
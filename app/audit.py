"""
Único punto del sistema que escribe en la tabla de auditoría (RN-027,
RN-028). Centralizarlo en una sola función evita que cada servicio arme el
INSERT a su manera (reusabilidad) y asegura que ningún evento se registre
con un formato distinto al resto.

Importante: esta función solo hace `add`, nunca `update` ni `delete` sobre
RegistroAuditoria — esa ausencia deliberada es lo que sostiene RN-029
(inmutabilidad) a nivel de código de aplicación.
"""
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RegistroAuditoria, TipoEventoAuditoria


async def registrar_evento(
    db: AsyncSession,
    tipo_evento: TipoEventoAuditoria,
    detalle: str,
    usuario_id: uuid.UUID | None = None,
) -> None:
    evento = RegistroAuditoria(usuario_id=usuario_id, tipo_evento=tipo_evento, detalle=detalle)
    db.add(evento)
    # No se hace commit aquí a propósito: el evento de auditoría se guarda
    # como parte de la misma transacción que la acción que lo originó
    # (ej. login exitoso, cambio de rol) — así, si esa acción falla, el
    # registro de auditoría tampoco queda huérfano.

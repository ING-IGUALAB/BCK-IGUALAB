
import logging
import uuid
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RegistroAuditoria, ResultadoAuditoria, TipoEventoAuditoria, Usuario

logger = logging.getLogger("igualab.auditoria")


def registrar_evento(
    db: AsyncSession,
    tipo_evento: TipoEventoAuditoria,
    detalle: str,
    usuario_id: uuid.UUID | None = None,
    *,
    resultado: ResultadoAuditoria,
) -> None:
    """Agrega un evento a la sesión. `resultado` es obligatorio (LTX:RN-043): ningún evento se presenta como exitoso por omisión.
    `usuario_id` es la cuenta que originó la acción; `None` solo en eventos anónimos (p. ej. login de un correo inexistente)."""
    evento = RegistroAuditoria(usuario_id=usuario_id, tipo_evento=tipo_evento, detalle=detalle, resultado=resultado.value)
    db.add(evento)
    # No se hace commit aquí a propósito: el evento de auditoría se guarda
    # como parte de la misma transacción que la acción que lo originó


async def registrar_evento_aparte(
    db: AsyncSession,
    tipo_evento: TipoEventoAuditoria,
    detalle: str,
    usuario_id: uuid.UUID | None = None,
    *,
    resultado: ResultadoAuditoria,
) -> bool:
    """Registra y CONFIRMA el evento ahora, para que un rechazo sobreviva al rollback de la operación principal. Quien llama debe
    haber terminado antes sus propias escrituras. No lanza: devuelve si se guardó (el fallo se registra solo por su clase)."""
    try:
        registrar_evento(db, tipo_evento, detalle[:500], usuario_id, resultado=resultado)
        await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - la auditoría no puede convertir un rechazo en un 500
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("No se pudo registrar el evento de auditoría (%s).", type(exc).__name__)
        return False


async def registrar_acceso_denegado_a_auditoria(db: AsyncSession, usuario: Usuario) -> bool:
    """LTX:RN-045 / CU007: una cuenta que no es SuperAdmin intentó entrar a la auditoría. Se registra el intento con la cuenta
    AUTENTICADA (la identidad sale de la sesión validada) y sin datos de la petición."""
    return await registrar_evento_aparte(
        db,
        TipoEventoAuditoria.ACCESO_DENEGADO,
        f"Acceso denegado a la auditoría; rol={usuario.rol.value}",
        usuario.id,
        resultado=ResultadoAuditoria.DENEGADO,
    )


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
                "resultado": f.resultado,
            }
            for f in filas
        ],
        "pagina": pagina,
        "tamano": tamano,
        "total": total,
    }

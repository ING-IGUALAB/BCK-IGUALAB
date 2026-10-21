"""Auditoría (Módulo 8, CU007) por HTTP. EXCLUSIVO del rol SuperAdmin (`SuperAdminUser`):
es el panel de eventos sensibles (RF-029) con filtros por tipo/usuario/fecha (RF-030).
Los eventos son append-only: solo se LEEN (no se modifican ni borran)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import CurrentUser, DatabaseSession
from app.core.exceptions import AuthorizationError
from app.models import RolUsuario, TipoEventoAuditoria, Usuario
from app.modules.auditoria.schemas import PaginaEventos
from app.modules.auditoria.service import listar_eventos, registrar_acceso_denegado_a_auditoria

router = APIRouter(prefix="/auditoria", tags=["Auditoría"])


async def exigir_superadmin_registrando_intento(usuario: CurrentUser, db: DatabaseSession) -> Usuario:
    """Solo el SuperAdmin consulta la auditoría (LTX:RN-045, CU007). Una cuenta autenticada con otro rol recibe 403 y el intento
    queda registrado (con su identidad de sesión, en su propia transacción). Sin sesión válida es 401 y no se registra nada: no
    hay una cuenta a la que atribuirlo."""
    if usuario.rol is not RolUsuario.SUPERADMIN:
        await registrar_acceso_denegado_a_auditoria(db, usuario)
        raise AuthorizationError("FORBIDDEN", "No tiene permisos para esta acción.")
    return usuario


SuperAdminAuditoria = Annotated[Usuario, Depends(exigir_superadmin_registrando_intento)]


@router.get(
    "",
    summary="Visualizar eventos de auditoría con filtros y paginación",
    responses={
        401: {"description": "No autenticado o sesión inválida."},
        403: {"description": "Requiere rol SuperAdmin. El intento de otra cuenta queda registrado (ACCESO_DENEGADO)."},
    },
)
async def listar(
    db: DatabaseSession,
    _usuario: SuperAdminAuditoria,
    tipo_evento: Annotated[TipoEventoAuditoria | None, Query(description="Filtra por tipo de evento.")] = None,
    usuario_id: Annotated[uuid.UUID | None, Query(description="Filtra por usuario implicado.")] = None,
    desde: Annotated[datetime | None, Query(description="Eventos desde esta fecha (UTC).")] = None,
    hasta: Annotated[datetime | None, Query(description="Eventos hasta esta fecha (UTC).")] = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginaEventos:
    datos = await listar_eventos(
        db,
        tipo_evento=tipo_evento,
        usuario_id=usuario_id,
        desde=desde,
        hasta=hasta,
        pagina=pagina,
        tamano=tamano,
    )
    return PaginaEventos(**datos)

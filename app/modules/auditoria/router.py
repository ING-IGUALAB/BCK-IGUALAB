"""Auditoría (Módulo 8, CU007) por HTTP. EXCLUSIVO del rol SuperAdmin (`SuperAdminUser`):
es el panel de eventos sensibles (RF-029) con filtros por tipo/usuario/fecha (RF-030).
Los eventos son append-only: solo se LEEN (no se modifican ni borran)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import DatabaseSession, SuperAdminUser
from app.models import TipoEventoAuditoria
from app.modules.auditoria.schemas import PaginaEventos
from app.modules.auditoria.service import listar_eventos

router = APIRouter(prefix="/auditoria", tags=["Auditoría"])


@router.get(
    "",
    response_model=PaginaEventos,
    summary="Visualizar eventos de auditoría con filtros y paginación",
)
async def listar(
    db: DatabaseSession,
    _usuario: SuperAdminUser,
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

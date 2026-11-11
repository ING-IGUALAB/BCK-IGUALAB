"""Historial del Asistente IA (Módulo 6, CU004) por HTTP — ENTREGA 1: guardar, listar y recuperar conversaciones.

Mismos permisos que el resto del asistente: EXCLUSIVO del rol Administrador (`AdministradorUser`: sin sesión 401, SuperAdmin 403).
El propietario sale de la sesión autenticada y el ambiente de la configuración del servidor; el cliente no los envía.

Estas rutas NO dependen del LLM, de OCI (embeddings), de la base vectorial ni de su esquema: solo de la base transaccional. Si el
esquema transaccional no pudo prepararse al arrancar responden 503, sin afectar a `POST /asistente/consultas`.

El historial lo aporta el cliente: ver `historial_schemas` para la condición de PROCEDENCIA de los mensajes.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.dependencies import AdministradorUser, DatabaseSession
from app.core.exceptions import ServiceUnavailableError
from app.modules.asistente.historial_schemas import (
    ConversacionDetalle,
    ConversacionGuardada,
    GuardarConversacionRequest,
    PaginaConversaciones,
)
from app.modules.asistente.historial_service import guardar_conversacion, listar_conversaciones, obtener_conversacion
from app.modules.asistente.router import obtener_ambiente

router = APIRouter(prefix="/asistente", tags=["Asistente IA (historial)"])


def esquema_historial_listo(request: Request) -> None:
    """503 si el arranque no pudo preparar el esquema de la base transaccional (migración 0009 incluida). Se declara DESPUÉS de la
    autenticación. Los detalles ya vienen saneados (código y versión de la migración), sin URL ni credenciales."""
    error = getattr(request.app.state, "analisis_error", None)
    if error is not None:
        _codigo, _mensaje, detalles = error
        raise ServiceUnavailableError(
            "ASSISTANT_HISTORY_SCHEMA_NOT_READY",
            "El historial del asistente no está disponible: no se pudo preparar el esquema de la base transaccional.",
            details=detalles,
        )


_ERRORES = {
    401: {"description": "No autenticado o sesión inválida."},
    403: {"description": "Requiere rol Administrador."},
    503: {"description": "Esquema del historial no preparado o ambiente inválido."},
}


@router.post(
    "/conversaciones",
    status_code=201,
    summary="Guardar una conversación del asistente (historial aportado por el cliente)",
    description=(
        "Guarda la conversación y todos sus mensajes en una sola transacción. Es IDEMPOTENTE por `clave_idempotencia` (delimitada por "
        "usuario y ambiente): la misma clave con el mismo contenido devuelve la conversación ya guardada (200, `reutilizada`); con "
        "contenido distinto responde 409. El propietario y el ambiente los fija el servidor. **Procedencia:** el contenido lo aporta "
        "el cliente; el servidor valida su estructura y consistencia interna, pero NO demuestra que lo generó el backend ni que sus "
        "citas se verificaron originalmente, y lo marca como `APORTADO_POR_CLIENTE`. No invoca al LLM ni a la base vectorial."
    ),
    responses={
        **_ERRORES,
        200: {"description": "Reintento: la conversación ya estaba guardada con la misma clave y el mismo contenido."},
        400: {"description": "Empresa inactiva (`COMPANY_INACTIVE`) o de otro sector (`COMPANY_SECTOR_MISMATCH`)."},
        404: {"description": "La empresa no existe (`COMPANY_NOT_FOUND`)."},
        409: {"description": "La clave de idempotencia ya se usó con un contenido distinto (`CONVERSATION_IDEMPOTENCY_CONFLICT`)."},
        413: {"description": "La conversación supera el tamaño máximo (`CONVERSATION_TOO_LARGE`)."},
        422: {"description": "Estructura inválida: vacía, roles no admitidos, contexto inconsistente o límites excedidos."},
    },
)
async def guardar(
    payload: GuardarConversacionRequest,
    response: Response,
    usuario: AdministradorUser,
    db: DatabaseSession,
    _esquema: Annotated[None, Depends(esquema_historial_listo)],
    ambiente: Annotated[str, Depends(obtener_ambiente)],
) -> ConversacionGuardada:
    guardada = await guardar_conversacion(db, usuario_id=usuario.id, ambiente=ambiente, payload=payload)
    if guardada.reutilizada:
        response.status_code = 200
    return guardada


@router.get(
    "/conversaciones",
    summary="Listar las conversaciones guardadas del usuario autenticado",
    description=(
        "Solo las del usuario de la sesión y del ambiente del servidor, de la más reciente a la más antigua. Devuelve metadatos "
        "(título, empresa, sector, año, número de mensajes y fechas), no los mensajes."
    ),
    responses={**_ERRORES},
)
async def listar(
    usuario: AdministradorUser,
    db: DatabaseSession,
    _esquema: Annotated[None, Depends(esquema_historial_listo)],
    ambiente: Annotated[str, Depends(obtener_ambiente)],
    pagina: Annotated[int, Query(ge=1, description="Página (base 1).")] = 1,
    tamano: Annotated[int, Query(ge=1, le=100, description="Conversaciones por página.")] = 20,
) -> PaginaConversaciones:
    return await listar_conversaciones(db, usuario_id=usuario.id, ambiente=ambiente, pagina=pagina, tamano=tamano)


@router.get(
    "/conversaciones/{conversacion_id}",
    summary="Recuperar una conversación guardada con sus mensajes y citas",
    description=(
        "Mensajes en orden estable, con la respuesta estructurada del asistente (citas, afirmaciones, ausencia de evidencia) y su "
        "procedencia. Una conversación inexistente, de otro usuario o de otro ambiente responde igual (404). Recuperar es consultar "
        "historial: `contexto.empresa_activa` informa el estado actual de la empresa y NO autoriza nuevas consultas."
    ),
    responses={**_ERRORES, 404: {"description": "La conversación no existe (o no es del usuario) (`CONVERSATION_NOT_FOUND`)."}},
)
async def recuperar(
    conversacion_id: uuid.UUID,
    usuario: AdministradorUser,
    db: DatabaseSession,
    _esquema: Annotated[None, Depends(esquema_historial_listo)],
    ambiente: Annotated[str, Depends(obtener_ambiente)],
) -> ConversacionDetalle:
    return await obtener_conversacion(db, usuario_id=usuario.id, ambiente=ambiente, conversacion_id=conversacion_id)

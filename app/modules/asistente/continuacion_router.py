"""Continuación de conversaciones del Asistente IA (Módulo 6, CU004) por HTTP — ENTREGA 2.

`POST /asistente/conversaciones/{conversacion_id}/consultas`: EXCLUSIVO del rol Administrador (`AdministradorUser`: sin sesión 401,
SuperAdmin 403). El propietario sale de la sesión y el ambiente de la configuración. La empresa, el sector y el año salen de la
conversación guardada; el cliente solo envía la pregunta y la clave de idempotencia.

ORDEN: autenticación y rol -> la conversación es del usuario (404 uniforme) -> idempotencia (un intercambio completado se devuelve
desde la base transaccional) -> SOLO una consulta nueva prepara los recursos del RAG. Por eso esta ruta NO declara como dependencias
el esquema vectorial, la sesión vectorial ni los proveedores: se entregan como `RecursosRag` DIFERIDOS (nada se resuelve hasta que el
servicio los necesita, y la sesión vectorial se abre con cierre garantizado). Una respuesta ya guardada sigue accesible aunque el
esquema o la configuración vectorial, los embeddings o el LLM no estén disponibles. Las rutas de lectura y de guardado inicial
(`historial_router`) siguen sin depender de ellos.

Entrega 3: la pregunta se interpreta con un contexto conversacional acotado antes de consultar al RAG. El historial ayuda a entender
referencias; no es evidencia y no cambia la empresa, el sector ni el año (siempre los de la conversación).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.core.dependencies import AdministradorUser, DatabaseSession
from app.modules.asistente import router as router_consultas
from app.modules.asistente.continuacion_schemas import ContinuarConversacionRequest, IntercambioGuardado
from app.core.database_vectorial import fabrica_sesiones_vectoriales, url_vectorial
from app.modules.asistente.continuacion_service import RecursosRag, continuar_conversacion
from app.modules.asistente.historial_router import esquema_historial_listo
from app.modules.asistente.router import esquema_vectorial_listo, obtener_ambiente

router = APIRouter(prefix="/asistente", tags=["Asistente IA (historial)"])


def get_recursos_rag(request: Request) -> RecursosRag:
    """Recursos DIFERIDOS del RAG. Crear este objeto no valida, construye, conecta ni abre nada: cada acción ocurre solo cuando el
    servicio la invoca, es decir, cuando una consulta NUEVA la necesita. Reutiliza las comprobaciones y fábricas existentes (no
    duplica la gestión de motores ni consume dependencias generadoras a mano)."""

    def preparar():
        esquema_vectorial_listo(request)  # 503 con el mismo motivo que `POST /asistente/consultas`
        url_vectorial()  # 503/502 controlado si falta o es inválida VECTOR_DATABASE_URL; solo valida, no conecta
        # Mismos errores 503 que `POST /asistente/consultas` (embeddings y chat sin configurar).
        return router_consultas.get_proveedor_embeddings(), router_consultas.get_proveedor_llm()

    @asynccontextmanager
    async def sesion_vectorial():
        # El motor lo gestiona `database_vectorial` (perezoso y compartido); aquí solo se abre y se CIERRA una sesión por solicitud.
        async with fabrica_sesiones_vectoriales()() as sesion:
            yield sesion

    return RecursosRag(preparar=preparar, sesion_vectorial=sesion_vectorial)


@router.post(
    "/conversaciones/{conversacion_id}/consultas",
    status_code=201,
    summary="Añadir una consulta a una conversación guardada (el servidor guarda pregunta y respuesta)",
    description=(
        "Interpreta la pregunta con un contexto conversacional ACOTADO (los últimos 4 intercambios, 12 000 caracteres), la responde con el "
        "RAG actual dentro de la empresa, el sector y el año de la conversación, y guarda la pregunta ORIGINAL (`ENVIADO_POR_USUARIO`) y la "
        "respuesta completa con sus citas (`GENERADO_POR_SERVIDOR`) en una sola transacción. Una llamada adicional al LLM resuelve las "
        "referencias y decide `CONSULTAR`, `ACLARAR` o `CAMBIAR_CONTEXTO`; el historial es dato, no evidencia: la respuesta se vuelve a "
        "recuperar y verificar con los documentos actuales y la empresa, el sector y el año nunca cambian. Una sola consulta en curso por "
        "conversación. IDEMPOTENTE por `clave_idempotencia` (delimitada por conversación): la misma clave con la misma pregunta ya "
        "completada devuelve lo almacenado (200, `reutilizado`) sin interpretar ni consultar a la IA; con otra pregunta responde 409; si "
        "sigue en curso responde 409 `CONVERSATION_QUERY_IN_PROGRESS` con `Retry-After`. Una consulta NUEVA exige empresa activa y prepara "
        "los recursos del RAG (esquema vectorial, configuración, proveedores) solo entonces, con un presupuesto global de 240 s compartido "
        "por la interpretación y el RAG. Garantiza como máximo un intercambio guardado por clave, no exactamente una llamada al "
        "proveedor: cancelar la espera no garantiza detener el trabajo externo ya iniciado. Un fragmento recuperado no demuestra que "
        "contenga la respuesta completa: las comparaciones («la de mayor importe») se limitan a lo recuperado."
    ),
    responses={
        200: {"description": "Repetición: el intercambio ya estaba completado con la misma clave y la misma pregunta (`reutilizado: true`)."},
        400: {
            "description": "Empresa inactiva (`COMPANY_INACTIVE`) o de otro sector (`COMPANY_SECTOR_MISMATCH`); `CONVERSATION_CLARIFICATION_REQUIRED` "
            "(no se puede determinar a qué se refiere la pregunta: mostrar `message` y pedir que se reformule; el usuario envía de nuevo con "
            "una CLAVE NUEVA) y `CONVERSATION_CONTEXT_CHANGE_REQUIRED` (la pregunta exige otra empresa, sector o año: iniciar una "
            "conversación nueva con ese contexto). En ambos casos no se busca nada, no se guarda ningún mensaje y la reserva queda "
            "`FALLIDO`; `details.accion` indica la acción sugerida."
        },
        401: {"description": "No autenticado o sesión inválida."},
        403: {"description": "Requiere rol Administrador."},
        404: {"description": "La conversación no existe, es de otro usuario o de otro ambiente (`CONVERSATION_NOT_FOUND`)."},
        409: {
            "description": "`QUERY_IDEMPOTENCY_CONFLICT` (misma clave, otra pregunta); `CONVERSATION_QUERY_IN_PROGRESS` (esta misma consulta sigue "
            "en curso); `CONVERSATION_BUSY` (otra consulta en curso); `CONVERSATION_MESSAGE_LIMIT_REACHED` (40 mensajes); "
            "`CONVERSATION_SIZE_LIMIT_REACHED` (1 000 000 bytes); `CONVERSATION_QUERY_SUPERSEDED` (la reserva venció y la sustituyó otra); "
            "`ANALYSIS_CHANGING` (solo una pregunta de SOLO ESTADO GRI: el Análisis GRI está cambiando; reintente con la misma clave)."
        },
        422: {"description": "Pregunta vacía o de más de 2 000 caracteres, o campos no admitidos (`REQUEST_VALIDATION_ERROR`)."},
        500: {
            "description": "`ASSISTANT_RESPONSE_NOT_PERSISTABLE`: la respuesta generada contiene contenido vacío o el carácter NUL, que PostgreSQL no "
            "puede almacenar. No se guardó nada y no se altera ni se trunca la respuesta. **No es un fallo temporal: NO reintentar "
            "automáticamente en bucle** (la misma pregunta puede producir el mismo resultado); reformular la pregunta o informar al "
            "soporte. La reserva queda `FALLIDO`."
        },
        502: {
            "description": "Falló el proveedor de embeddings o de chat, o la base vectorial no está configurada (`VECTOR_DATABASE_NOT_CONFIGURED`, "
            "`VECTOR_DATABASE_URL_INVALID`, igual que `POST /asistente/consultas`), o la salida de la interpretación fue inválida "
            "(`CONVERSATION_INTERPRETATION_INVALID`: no se busca con una pregunta inventada). No se guardó nada; reintente con la misma clave."
        },
        503: {
            "description": "Recursos del RAG no disponibles al preparar una consulta NUEVA (esquema vectorial no preparado: `INGESTION_NOT_CONFIGURED` "
            "o el motivo del arranque; `LLM_NOT_CONFIGURED`, `EMBEDDINGS_NOT_CONFIGURED`), o "
            "`CONVERSATION_PERSISTENCE_FAILED` (fallo temporal al guardar). Una repetición ya completada NO devuelve 503 por esto. "
            "No se guardó nada; reintente con la misma clave."
        },
        504: {
            "description": "El proveedor de embeddings no respondió a tiempo (`EMBEDDING_PROVIDER_TIMEOUT`) o se agotó el presupuesto global de "
            "generación de 240 s, compartido por la interpretación y el RAG (`ASSISTANT_GENERATION_TIMEOUT`). No se guardó nada; reintente con "
            "la misma clave."
        },
    },
)
async def continuar(
    conversacion_id: uuid.UUID,
    payload: ContinuarConversacionRequest,
    response: Response,
    # 401/403 ANTES que 503 (esquema del historial): la autenticación tiene prioridad. NADA vectorial ni de IA se declara aquí.
    usuario: AdministradorUser,
    db: DatabaseSession,
    _esquema: Annotated[None, Depends(esquema_historial_listo)],
    ambiente: Annotated[str, Depends(obtener_ambiente)],
    recursos: Annotated[RecursosRag, Depends(get_recursos_rag)],
) -> IntercambioGuardado:
    intercambio = await continuar_conversacion(
        db,
        usuario_id=usuario.id,
        ambiente=ambiente,
        conversacion_id=conversacion_id,
        pregunta=payload.pregunta,
        clave_idempotencia=payload.clave_idempotencia,
        recursos=recursos,
    )
    if intercambio.reutilizado:
        response.status_code = 200
    return intercambio

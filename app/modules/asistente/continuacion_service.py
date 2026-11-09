"""Casos de uso de la CONTINUACIÓN de conversaciones del Asistente IA (Módulo 6, CU004) — ENTREGA 2.

Añade una consulta a una conversación guardada: el servidor INTERPRETA la pregunta con un contexto conversacional acotado
(entrega 3), la responde con el RAG actual (`service.responder_consulta`, SIN cambios) dentro de la empresa, el sector y el año de la
conversación, y guarda la pregunta ORIGINAL y la respuesta completa.

MEMORIA CONVERSACIONAL (entrega 3), solo para INTERPRETAR. Antes del RAG hay como máximo UNA llamada adicional al LLM existente que
decide `CONSULTAR` (con una pregunta autónoma), `ACLARAR` o `CAMBIAR_CONTEXTO` (`domain/interpretacion.py`). El historial son los
últimos 4 intercambios completos (12 000 caracteres como máximo), leídos de la conversación autorizada una vez que la ejecución es
dueña de la reserva (así no puede cambiar bajo sus pies). El historial NO es evidencia: la respuesta se recupera y se verifica de
nuevo con los documentos elegibles actuales. La empresa, el sector, el año y el ambiente salen SIEMPRE de la conversación y del
servidor: la decisión del modelo nunca controla un filtro. `ACLARAR` y `CAMBIAR_CONTEXTO` son errores de negocio 400: no se
busca nada, no se publica ningún mensaje y la reserva pasa a FALLIDO; el usuario envía de nuevo con una clave nueva.

FLUJO EN TRES FASES (ninguna transacción ni bloqueo del historial permanece abierto mientras se llama a los proveedores):
  1. RESERVAR (transacción corta, termina con commit). Autoriza (propietario y ambiente de la sesión/configuración; 404 uniforme),
     resuelve la idempotencia (un intercambio COMPLETADO se devuelve aquí, sin tocar nada vectorial ni de IA), aplica los límites,
     revalida empresa activa y sector y SOLO ENTONCES prepara los recursos del RAG (`RecursosRag.preparar`: esquema vectorial,
     configuración y proveedores). Después inserta (o recupera) la fila de `intercambios_asistente` con estado EN_CURSO, un token de propiedad nuevo y un vencimiento.
  2. INTERPRETAR Y EJECUTAR EL RAG, sin ninguna transacción del historial abierta, con la sesión vectorial abierta de forma diferida
     (cierre garantizado) y bajo UN MISMO presupuesto global `PRESUPUESTO_GENERACION_SEGUNDOS` para la lectura del contexto, la
     interpretación y el RAG juntos. Si falla, se aclara, cambia de contexto o se agota, la reserva pasa a FALLIDO.
  3. PUBLICAR (transacción corta). Bloquea la reserva SOLO SI el token sigue siendo suyo y continúa EN_CURSO; entonces comprueba los
     límites con el tamaño real, inserta la pregunta y la respuesta (orden definitivo = siguiente libre, único por restricción),
     marca el intercambio COMPLETADO y actualiza `actualizada_en`: todo o nada.

CONCURRENCIA entre procesos e instancias, decidida por PostgreSQL (sin mutex en memoria, sin colas, sin Redis):
  · UNIQUE (conversacion_id, clave_idempotencia): como máximo un intercambio por clave.
  · índice único parcial sobre (conversacion_id) WHERE estado = 'EN_CURSO': una sola consulta en curso por conversación. Una segunda
    consulta distinta recibe 409 sin invocar a la IA. Conversaciones distintas no se serializan entre sí.
  · actualizaciones condicionadas por (estado, token): si dos solicitudes intentan recuperar la misma reserva solo una cambia la fila.
  · RESERVA VENCIDA. Si `vence_en` pasó, otra solicitud puede recuperarla (nuevo token) o sustituirla (VENCIDO). La ejecución
    anterior ya no puede publicar: en la fase 3 su token no coincide y recibe 409 (`CONVERSATION_QUERY_SUPERSEDED`). Sin heartbeat:
    el plazo de la reserva (`PLAZO_RESERVA_SEGUNDOS`, 300 s) es MAYOR que el presupuesto global de la generación
    (`PRESUPUESTO_GENERACION_SEGUNDOS`, 240 s, que SÍ impone un máximo total a contexto + interpretación + embeddings + búsqueda + LLM + verificación).

GARANTÍAS Y LÍMITES. Hay COMO MÁXIMO UN intercambio persistido por clave. NO se promete exactamente UNA llamada externa: si el proceso
cae o se pierde la conexión después de llamar al proveedor y antes de publicar, un reintento volverá a consultar al RAG (el
proveedor no ofrece idempotencia). Los fallos no dejan un par incompleto ni bloquean la conversación: tras un fallo la clave se
reintenta; tras una caída, al vencer la reserva.

LÍMITES DE LA CONVERSACIÓN (los de la entrega 1, ahora sobre la conversación COMPLETA): 40 mensajes y 1 000 000 bytes de contenido
canónico (la misma definición que el guardado inicial). Nada se trunca: si el intercambio no cabe, no se publica ninguno de los dos
mensajes y se responde un error específico.

El guardado inicial no se toca: `huella_contenido` y `clave_idempotencia` de la conversación quedan como se guardaron.
Los registros del servidor solo llevan identificadores y conteos, nunca preguntas, respuestas ni citas.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AppException,
    BusinessValidationError,
    ConflictError,
    ExternalServiceError,
    ExternalServiceTimeoutError,
    InternalProcessingError,
    ServiceUnavailableError,
    codigo_seguro,
)
from app.models import SectorEmpresa
from app.models.asistente import (
    ConversacionAsistente,
    EstadoIntercambio,
    IntercambioAsistente,
    MensajeAsistente,
    ProcedenciaMensaje,
    RolMensaje,
)
from app.modules.asistente.continuacion_schemas import IntercambioGuardado
from app.modules.asistente.domain.estado_gri import solo_estado
from app.modules.asistente.domain.historial import (
    MAX_BYTES_CONVERSACION,
    MAX_MENSAJES,
    contenido_canonico,
    contenido_de_respuesta,
    contiene_nul,
    huella_de_contenido,
)
from app.modules.asistente.domain.interpretacion import (
    MAX_INTERCAMBIOS_CONTEXTO,
    DECISION_ACLARAR,
    DECISION_CAMBIAR_CONTEXTO,
    SISTEMA_INTERPRETACION,
    TEXTO_ACLARACION,
    TEXTO_CAMBIO_DE_CONTEXTO,
    MensajeContexto,
    construir_prompt_interpretacion,
    parsear_decision,
    seleccionar_contexto,
)
from app.modules.asistente.domain.prompt import nuevo_codigo_de_marcador
from app.modules.asistente.historial_service import _mensaje_guardado, _no_encontrada, _nombre_de_restriccion
from app.modules.asistente.infrastructure.llm import ProveedorLlm
from app.modules.asistente.schemas import ConsultaResponse
from app.modules.asistente.service import _empresa_consultable, responder_consulta, responder_solo_estado
from app.modules.ingesta.domain.embeddings import ProveedorEmbeddingsConsulta

logger = logging.getLogger("igualab.asistente.continuacion")

# Embeddings (120 s por llamada) + chat (60 s) + búsqueda vectorial y publicación, con margen. Constante del módulo: las pruebas
# fijan el vencimiento en la base en vez de esperar.
PLAZO_RESERVA_SEGUNDOS = 300
# PRESUPUESTO GLOBAL de la fase de generación (fase 2): un único plazo (`asyncio.timeout`) que envuelve TODO `responder_consulta`:
# embeddings de la consulta, búsqueda vectorial, visibilidad transaccional, LLM y verificación de citas. Hoy NO existe ningún
# máximo total en el RAG: solo hay plazos individuales (embeddings 120 s por llamada, sin reintentos; httpx del LLM 60 s POR FASE de
# red, no en total; ni la búsqueda vectorial ni la espera de conexiones tienen límite propio). Este presupuesto lo impone. Deja 60 s
# de margen respecto de la reserva para las fases cortas que quedan FUERA de él (reservar, marcar el fallo y publicar). Debe ser
# estrictamente menor que `PLAZO_RESERVA_SEGUNDOS` (una prueba lo exige). No añade heartbeat.
PRESUPUESTO_GENERACION_SEGUNDOS = 240
REINTENTO_SUGERIDO_SEGUNDOS = 5
MAX_PASADAS_RESERVA = 3

RESTRICCION_CLAVE = "uq_intercambios_asistente_clave"
RESTRICCION_EN_CURSO = "uq_intercambios_asistente_en_curso"



@dataclass(frozen=True)
class RecursosRag:
    """Recursos del RAG que se preparan de forma DIFERIDA: nada de esto se toca hasta que una consulta NUEVA los necesita.

    - `preparar`: valida que el esquema y la configuración vectoriales estén listos y construye los proveedores de embeddings y chat.
      No abre conexiones ni escribe nada; lanza el error controlado de configuración (503) si falta algo.
    - `sesion_vectorial`: gestor de contexto que ABRE una sesión vectorial y garantiza su cierre (éxito, fallo o cancelación).

    Una repetición de un intercambio ya completado se resuelve con la base transaccional y nunca llama a ninguno de los dos.
    """

    preparar: Callable[[], tuple[ProveedorEmbeddingsConsulta, ProveedorLlm]]
    sesion_vectorial: Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class Reserva:
    """Lo que la ejecución necesita tras reservar. `token` prueba que esta ejecución es la dueña de la reserva."""

    intercambio_id: uuid.UUID
    token: uuid.UUID
    conversacion_id: uuid.UUID
    empresa_id: uuid.UUID
    sector: SectorEmpresa
    anio: int
    empresa_nombre: str
    embeddings: ProveedorEmbeddingsConsulta | None  # None si es una pregunta de solo estado: no se preparan recursos documentales
    llm: ProveedorLlm | None
    solo_estado: bool = False


class _Reintentar(Exception):
    """Una solicitud concurrente cambió el estado entre la lectura y la escritura: se vuelve a evaluar con lo ya confirmado."""


# ------------------------------------------------------------------ Errores de dominio ------------------------------------------------------------------

def _en_curso_misma_clave() -> ConflictError:
    return ConflictError(
        "CONVERSATION_QUERY_IN_PROGRESS",
        "Esta consulta sigue en curso. Espere y reintente con la misma clave de idempotencia: no se inició otra ejecución.",
        details={"reintentar_en_segundos": REINTENTO_SUGERIDO_SEGUNDOS},
        headers={"Retry-After": str(REINTENTO_SUGERIDO_SEGUNDOS)},
    )


def _conversacion_ocupada() -> ConflictError:
    return ConflictError(
        "CONVERSATION_BUSY",
        "La conversación tiene otra consulta en curso. Espere a que termine y envíe la pregunta de nuevo con una clave nueva.",
        details={"reintentar_en_segundos": REINTENTO_SUGERIDO_SEGUNDOS},
        headers={"Retry-After": str(REINTENTO_SUGERIDO_SEGUNDOS)},
    )


def _clave_con_otra_pregunta() -> ConflictError:
    return ConflictError(
        "QUERY_IDEMPOTENCY_CONFLICT",
        "La clave de idempotencia ya se usó con otra pregunta en esta conversación. Genere una clave nueva para una pregunta nueva.",
    )


def _limite_de_mensajes() -> ConflictError:
    return ConflictError(
        "CONVERSATION_MESSAGE_LIMIT_REACHED",
        f"La conversación alcanzó el máximo de {MAX_MENSAJES} mensajes y no admite otro intercambio. Puede seguir leyéndose.",
    )


def _limite_de_tamano() -> ConflictError:
    return ConflictError(
        "CONVERSATION_SIZE_LIMIT_REACHED",
        f"El intercambio no cabe en la conversación: superaría el tamaño máximo de {MAX_BYTES_CONVERSACION} bytes de contenido. "
        "No se guardó ningún mensaje.",
    )


def _desplazada() -> ConflictError:
    return ConflictError(
        "CONVERSATION_QUERY_SUPERSEDED",
        "Esta consulta ya no es la vigente de la conversación (su reserva venció y otra solicitud la sustituyó). No se guardó nada; "
        "reintente con la misma clave.",
    )


# ------------------------------------------------------------------ Lecturas ------------------------------------------------------------------

async def _conversacion_propia(
    db: AsyncSession, conversacion_id: uuid.UUID, ambiente: str, usuario_id: uuid.UUID
) -> ConversacionAsistente:
    consulta = select(ConversacionAsistente).where(
        ConversacionAsistente.id == conversacion_id,
        ConversacionAsistente.ambiente == ambiente,
        ConversacionAsistente.usuario_id == usuario_id,
    )
    conversacion = (await db.execute(consulta)).scalar_one_or_none()
    if conversacion is None:
        raise _no_encontrada()  # MISMA respuesta para inexistente, ajena u otro ambiente
    return conversacion


async def _almacenados(db: AsyncSession, conversacion_id: uuid.UUID) -> tuple[list[dict], int]:
    """Los mensajes guardados con la MISMA forma que el guardado inicial (rol, contenido, datos_respuesta) y el último `orden`."""
    filas = (
        await db.execute(
            select(MensajeAsistente.orden, MensajeAsistente.rol, MensajeAsistente.contenido, MensajeAsistente.datos_respuesta)
            .where(MensajeAsistente.conversacion_id == conversacion_id)
            .order_by(MensajeAsistente.orden)
        )
    ).all()
    mensajes = [{"rol": f.rol, "contenido": f.contenido, "datos_respuesta": f.datos_respuesta} for f in filas]
    return mensajes, (filas[-1].orden if filas else 0)


def _bytes_de_conversacion(conversacion: ConversacionAsistente, mensajes: list[dict]) -> int:
    """Tamaño con la definición del guardado inicial: el JSON canónico UTF-8 del contexto y de todos los mensajes."""
    return len(contenido_canonico(conversacion.empresa_id, conversacion.sector, conversacion.anio, mensajes).encode("utf-8"))


def _mensaje_pregunta(pregunta: str) -> dict:
    return {"rol": RolMensaje.USUARIO.value, "contenido": pregunta, "datos_respuesta": None}


def _mensaje_respuesta(respuesta: ConsultaResponse) -> dict:
    return {
        "rol": RolMensaje.ASISTENTE.value,
        "contenido": contenido_de_respuesta(respuesta),
        "datos_respuesta": respuesta.model_dump(mode="json"),
    }


async def _intercambio_guardado(db: AsyncSession, intercambio_id: uuid.UUID, *, reutilizado: bool) -> IntercambioGuardado:
    """El intercambio COMPLETADO tal como está almacenado, con el estado ACTUAL de la conversación."""
    intercambio = (
        await db.execute(select(IntercambioAsistente).where(IntercambioAsistente.id == intercambio_id))
    ).scalar_one()
    mensajes = {
        m.id: m
        for m in (
            await db.execute(
                select(MensajeAsistente).where(
                    MensajeAsistente.id.in_([intercambio.mensaje_pregunta_id, intercambio.mensaje_respuesta_id])
                )
            )
        ).scalars()
    }
    conversacion = (
        await db.execute(select(ConversacionAsistente).where(ConversacionAsistente.id == intercambio.conversacion_id))
    ).scalar_one()
    total = (
        await db.execute(select(func.count()).select_from(MensajeAsistente).where(MensajeAsistente.conversacion_id == conversacion.id))
    ).scalar_one()
    return IntercambioGuardado(
        conversacion_id=conversacion.id,
        clave_idempotencia=intercambio.clave_idempotencia,
        pregunta=_mensaje_guardado(mensajes[intercambio.mensaje_pregunta_id]),
        respuesta=_mensaje_guardado(mensajes[intercambio.mensaje_respuesta_id]),
        total_mensajes=total,
        actualizada_en=conversacion.actualizada_en,
        reutilizado=reutilizado,
    )


# ------------------------------------------------------------------ Fase 1: reservar ------------------------------------------------------------------

def _vence_en():
    """Vencimiento calculado con el reloj de la BASE (el mismo para todas las instancias)."""
    return func.now() + timedelta(seconds=PLAZO_RESERVA_SEGUNDOS)


async def _por_clave(db: AsyncSession, conversacion_id: uuid.UUID, clave: uuid.UUID):
    consulta = select(IntercambioAsistente, (IntercambioAsistente.vence_en > func.now()).label("vigente")).where(
        IntercambioAsistente.conversacion_id == conversacion_id, IntercambioAsistente.clave_idempotencia == clave
    )
    return (await db.execute(consulta)).first()


async def _otra_en_curso(db: AsyncSession, conversacion_id: uuid.UUID, excepto: uuid.UUID | None):
    consulta = select(IntercambioAsistente, (IntercambioAsistente.vence_en > func.now()).label("vigente")).where(
        IntercambioAsistente.conversacion_id == conversacion_id,
        IntercambioAsistente.estado == EstadoIntercambio.EN_CURSO.value,
    )
    if excepto is not None:
        consulta = consulta.where(IntercambioAsistente.id != excepto)
    return (await db.execute(consulta)).first()


async def _resolver_previa(
    db: AsyncSession, conversacion: ConversacionAsistente, clave: uuid.UUID, huella: str
) -> tuple[IntercambioAsistente | None, IntercambioGuardado | None]:
    """La idempotencia de la clave: (intercambio previo recuperable, None), (None, None) si es nueva o (_, ya completado)."""
    previa = await _por_clave(db, conversacion.id, clave)
    if previa is None:
        return None, None
    intercambio, vigente = previa[0], previa[1]
    if intercambio.huella_pregunta != huella:
        raise _clave_con_otra_pregunta()
    if intercambio.estado == EstadoIntercambio.COMPLETADO.value:
        # Repetición de algo ya completado: se devuelve lo almacenado. NO es una consulta nueva: no revalida la empresa ni usa IA.
        return intercambio, await _intercambio_guardado(db, intercambio.id, reutilizado=True)
    if intercambio.estado == EstadoIntercambio.EN_CURSO.value and vigente:
        raise _en_curso_misma_clave()
    return intercambio, None


async def _verificar_cupo(db: AsyncSession, conversacion: ConversacionAsistente, pregunta: str) -> None:
    """Límites que ya se pueden decidir antes de llamar a los proveedores (el tamaño final se vuelve a comprobar al publicar)."""
    mensajes, _ultimo = await _almacenados(db, conversacion.id)
    if len(mensajes) + 2 > MAX_MENSAJES:
        raise _limite_de_mensajes()
    if _bytes_de_conversacion(conversacion, [*mensajes, _mensaje_pregunta(pregunta)]) > MAX_BYTES_CONVERSACION:
        raise _limite_de_tamano()


async def _vencer_la_otra(db: AsyncSession, otra: IntercambioAsistente) -> None:
    """Reserva vencida de OTRA consulta: se sustituye (VENCIDO) y su ejecutor ya no podrá publicar."""
    vencida = await db.execute(
        update(IntercambioAsistente)
        .where(
            IntercambioAsistente.id == otra.id,
            IntercambioAsistente.estado == EstadoIntercambio.EN_CURSO.value,
            IntercambioAsistente.token_ejecucion == otra.token_ejecucion,
        )
        .values(estado=EstadoIntercambio.VENCIDO.value, actualizado_en=func.now())
    )
    if vencida.rowcount != 1:
        raise _Reintentar


async def _recuperar(db: AsyncSession, intercambio: IntercambioAsistente, token: uuid.UUID) -> None:
    """FALLIDO, VENCIDO o EN_CURSO vencida de ESTA clave: se recupera con un token nuevo (compare-and-set por estado y token)."""
    recuperada = await db.execute(
        update(IntercambioAsistente)
        .where(
            IntercambioAsistente.id == intercambio.id,
            IntercambioAsistente.estado == intercambio.estado,
            IntercambioAsistente.token_ejecucion == intercambio.token_ejecucion,
        )
        .values(
            estado=EstadoIntercambio.EN_CURSO.value,
            token_ejecucion=token,
            vence_en=_vence_en(),
            intentos=IntercambioAsistente.intentos + 1,
            codigo_error=None,
            actualizado_en=func.now(),
        )
    )
    if recuperada.rowcount != 1:
        raise _Reintentar


async def _escribir_reserva(
    db: AsyncSession,
    *,
    conversacion_id: uuid.UUID,
    clave: uuid.UUID,
    huella: str,
    intercambio: IntercambioAsistente | None,
    otra: IntercambioAsistente | None,
    token: uuid.UUID,
) -> uuid.UUID:
    """Fase 1, escritura: sustituye la reserva vencida ajena, recupera la propia o inserta la nueva, y confirma. Una restricción
    única perdida (clave o consulta en curso) significa que una solicitud concurrente ganó la carrera: `_Reintentar`."""
    try:
        if otra is not None:
            await _vencer_la_otra(db, otra)
        if intercambio is not None:
            await _recuperar(db, intercambio, token)
            intercambio_id = intercambio.id
        else:
            intercambio_id = uuid.uuid4()
            await db.execute(
                insert(IntercambioAsistente).values(
                    id=intercambio_id,
                    conversacion_id=conversacion_id,
                    clave_idempotencia=clave,
                    huella_pregunta=huella,
                    estado=EstadoIntercambio.EN_CURSO.value,
                    token_ejecucion=token,
                    intentos=1,
                    vence_en=_vence_en(),
                )
            )
        await db.commit()  # fin de la fase 1: la reserva es duradera y NO queda ninguna transacción abierta
    except IntegrityError as exc:
        await db.rollback()
        if _nombre_de_restriccion(exc) in (RESTRICCION_CLAVE, RESTRICCION_EN_CURSO):
            raise _Reintentar from None  # perdió la carrera: decidió la restricción única; se resuelve con lo ya confirmado
        raise
    except BaseException:
        await db.rollback()
        raise
    return intercambio_id


async def _intentar_reservar(
    db: AsyncSession,
    *,
    usuario_id: uuid.UUID,
    ambiente: str,
    conversacion_id: uuid.UUID,
    pregunta: str,
    clave: uuid.UUID,
    preparar: Callable[[], tuple[ProveedorEmbeddingsConsulta, ProveedorLlm]],
) -> Reserva | IntercambioGuardado:
    conversacion = await _conversacion_propia(db, conversacion_id, ambiente, usuario_id)
    huella = huella_de_contenido(pregunta)

    intercambio, completado = await _resolver_previa(db, conversacion, clave, huella)
    if completado is not None:
        return completado

    otra_fila = await _otra_en_curso(db, conversacion.id, intercambio.id if intercambio is not None else None)
    if otra_fila is not None and otra_fila[1]:
        raise _conversacion_ocupada()
    otra = otra_fila[0] if otra_fila is not None else None

    await _verificar_cupo(db, conversacion, pregunta)
    # Una consulta NUEVA exige empresa activa y del sector guardado (404/400, las reglas de /asistente/consultas).
    empresa = await _empresa_consultable(db, conversacion.empresa_id, conversacion.sector)
    # Se decide sobre la pregunta ORIGINAL, ya autorizada la conversación y comprobados cupo y empresa, y ANTES de preparar el RAG o de
    # interpretar con el modelo: una pregunta inequívoca de solo estado no necesita recursos vectoriales, embeddings ni modelo.
    solo_estado_gri = solo_estado(pregunta, conversacion.anio) is not None
    embeddings, llm = (None, None) if solo_estado_gri else preparar()  # 503 si falta el esquema vectorial o un proveedor: aún no se escribió nada

    token = uuid.uuid4()
    intercambio_id = await _escribir_reserva(
        db, conversacion_id=conversacion.id, clave=clave, huella=huella, intercambio=intercambio, otra=otra, token=token
    )
    return Reserva(
        intercambio_id, token, conversacion.id, conversacion.empresa_id, conversacion.sector, conversacion.anio, empresa.nombre,
        embeddings, llm, solo_estado_gri
    )


async def _reservar(db: AsyncSession, **parametros) -> Reserva | IntercambioGuardado:
    for _ in range(MAX_PASADAS_RESERVA):
        try:
            return await _intentar_reservar(db, **parametros)
        except _Reintentar:
            await db.rollback()
    raise _en_curso_misma_clave()  # nunca se alcanzó un estado estable: se informa como operación en curso, sin iniciar otra


# ------------------------------------------------------------------ Fase 2 y 3 ------------------------------------------------------------------

async def _marcar_fallido(db: AsyncSession, reserva: Reserva, codigo: str) -> None:
    """EN_CURSO -> FALLIDO solo si la ejecución sigue siendo la dueña (token). Nunca enmascara el error original: si no se puede
    marcar, la reserva simplemente vence y se recupera."""
    try:
        await db.rollback()
        await db.execute(
            update(IntercambioAsistente)
            .where(
                IntercambioAsistente.id == reserva.intercambio_id,
                IntercambioAsistente.token_ejecucion == reserva.token,
                IntercambioAsistente.estado == EstadoIntercambio.EN_CURSO.value,
            )
            .values(estado=EstadoIntercambio.FALLIDO.value, codigo_error=codigo[:64], actualizado_en=func.now())
        )
        await db.commit()
    except Exception:  # noqa: BLE001 - best effort; la reserva vence sola
        await db.rollback()
        logger.warning("No se pudo marcar como fallida la reserva %s; vencerá sola.", reserva.intercambio_id)


async def _escribir_par(db: AsyncSession, reserva: Reserva, pregunta: str, respuesta: ConsultaResponse) -> uuid.UUID:
    """Transacción de la fase 3. Lanza AppException (nada queda escrito) o devuelve el id del intercambio publicado."""
    # 1) Propiedad: bloquea la reserva SOLO si el token sigue siendo el de esta ejecución y continúa EN_CURSO. Una recuperación
    #    concurrente cambió el token o el estado: esta ejecución llegó tarde y no puede publicar.
    propia = (
        await db.execute(
            select(IntercambioAsistente.id)
            .where(
                IntercambioAsistente.id == reserva.intercambio_id,
                IntercambioAsistente.token_ejecucion == reserva.token,
                IntercambioAsistente.estado == EstadoIntercambio.EN_CURSO.value,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if propia is None:
        raise _desplazada()

    # 2) Límites con lo REALMENTE guardado y el tamaño real del par (misma definición que el guardado inicial). Sin truncar.
    conversacion = (
        await db.execute(select(ConversacionAsistente).where(ConversacionAsistente.id == reserva.conversacion_id))
    ).scalar_one()
    mensajes, ultimo = await _almacenados(db, reserva.conversacion_id)
    nuevos = [_mensaje_pregunta(pregunta), _mensaje_respuesta(respuesta)]
    if len(mensajes) + 2 > MAX_MENSAJES:
        raise _limite_de_mensajes()
    if _bytes_de_conversacion(conversacion, [*mensajes, *nuevos]) > MAX_BYTES_CONVERSACION:
        raise _limite_de_tamano()

    # 3) Pregunta y respuesta juntas, con el orden definitivo (único por restricción), el intercambio y `actualizada_en`.
    pregunta_id, respuesta_id = uuid.uuid4(), uuid.uuid4()
    db.add_all(
        [
            MensajeAsistente(
                id=pregunta_id, conversacion_id=reserva.conversacion_id, orden=ultimo + 1, rol=RolMensaje.USUARIO.value,
                contenido=nuevos[0]["contenido"], datos_respuesta=None, procedencia=ProcedenciaMensaje.ENVIADO_POR_USUARIO.value,
            ),
            MensajeAsistente(
                id=respuesta_id, conversacion_id=reserva.conversacion_id, orden=ultimo + 2, rol=RolMensaje.ASISTENTE.value,
                contenido=nuevos[1]["contenido"], datos_respuesta=nuevos[1]["datos_respuesta"],
                procedencia=ProcedenciaMensaje.GENERADO_POR_SERVIDOR.value,
            ),
        ]
    )
    await db.flush()
    await db.execute(
        update(IntercambioAsistente)
        .where(IntercambioAsistente.id == reserva.intercambio_id)
        .values(
            estado=EstadoIntercambio.COMPLETADO.value,
            mensaje_pregunta_id=pregunta_id,
            mensaje_respuesta_id=respuesta_id,
            completado_en=func.now(),
            actualizado_en=func.now(),
            codigo_error=None,
        )
    )
    await db.execute(
        update(ConversacionAsistente).where(ConversacionAsistente.id == reserva.conversacion_id).values(actualizada_en=func.now())
    )
    await db.commit()  # el par, el intercambio y la fecha se publican juntos
    return reserva.intercambio_id


async def _publicar(db: AsyncSession, reserva: Reserva, pregunta: str, respuesta: ConsultaResponse) -> IntercambioGuardado:
    if not contenido_de_respuesta(respuesta).strip() or contiene_nul(respuesta.model_dump(mode="json")):
        # Una respuesta válida del RAG que PostgreSQL no puede guardar (texto vacío o NUL): no se altera ni se inventa otra.
        await _marcar_fallido(db, reserva, "ASSISTANT_RESPONSE_NOT_PERSISTABLE")
        raise InternalProcessingError(
            "ASSISTANT_RESPONSE_NOT_PERSISTABLE", "La respuesta del asistente no se pudo guardar en el historial. No se guardó nada."
        )
    try:
        intercambio_id = await _escribir_par(db, reserva, pregunta, respuesta)
    except AppException as exc:
        await _marcar_fallido(db, reserva, codigo_seguro(exc))  # no-op si la ejecución ya no es la dueña
        raise
    except Exception as exc:  # la cancelación (BaseException) no se captura: la reserva vencerá y se recuperará
        await _marcar_fallido(db, reserva, codigo_seguro(exc))
        logger.error("Fallo al persistir un intercambio del asistente (reserva=%s, causa=%s)", reserva.intercambio_id, type(exc).__name__)
        raise ServiceUnavailableError(
            "CONVERSATION_PERSISTENCE_FAILED",
            "No se pudo guardar el intercambio. No se guardó ningún mensaje; reintente con la misma clave de idempotencia.",
        ) from None
    resultado = await _intercambio_guardado(db, intercambio_id, reutilizado=False)
    logger.info(
        "Intercambio del asistente guardado (conversacion=%s, intercambio=%s, ordenes=%d-%d)",
        reserva.conversacion_id, intercambio_id, resultado.pregunta.orden, resultado.respuesta.orden,
    )
    return resultado


async def _mensajes_de_contexto(db: AsyncSession, conversacion_id: uuid.UUID) -> list[MensajeContexto]:
    """Los últimos mensajes de ESTA conversación (ya autorizada), solo con lo necesario para interpretar: contenido visible, rol y
    procedencia. Se pide el doble de mensajes de la ventana por si hay que descartar un mensaje suelto. Se lee tras reservar: como
    solo la ejecución dueña de la reserva puede añadir mensajes, el contexto no cambia mientras se interpreta."""
    sin_evidencia = MensajeAsistente.datos_respuesta["sin_evidencia"].as_boolean()
    filas = (
        await db.execute(
            select(
                MensajeAsistente.orden, MensajeAsistente.rol, MensajeAsistente.contenido, MensajeAsistente.procedencia, sin_evidencia
            )
            .where(MensajeAsistente.conversacion_id == conversacion_id)
            .order_by(MensajeAsistente.orden.desc())
            .limit(2 * MAX_INTERCAMBIOS_CONTEXTO)
        )
    ).all()
    await db.rollback()  # lectura corta: nada del historial queda abierto durante las llamadas externas
    return [MensajeContexto(f.orden, f.rol, f.contenido, f.procedencia, bool(f[4])) for f in reversed(filas)]


async def _interpretar(db: AsyncSession, reserva: Reserva, pregunta: str) -> str:
    """A) Interpreta la pregunta con el contexto acotado. Devuelve la pregunta AUTÓNOMA o lanza un error controlado. Como máximo UNA
    llamada al LLM; nunca se ejecuta nada que produzca el modelo y su decisión nunca controla un filtro."""
    contexto = seleccionar_contexto(await _mensajes_de_contexto(db, reserva.conversacion_id))
    prompt = construir_prompt_interpretacion(
        pregunta, contexto, empresa_nombre=reserva.empresa_nombre, sector=reserva.sector.value, anio=reserva.anio,
        codigo=nuevo_codigo_de_marcador(),
    )
    salida = await reserva.llm.completar(SISTEMA_INTERPRETACION, prompt)  # historial como DATO de un mensaje de usuario, no como system
    try:
        decision = parsear_decision(salida)
    except ValueError:
        # Salida inválida o fuera del esquema: error controlado. NO se busca con una pregunta inventada ni se repite la salida.
        raise ExternalServiceError(
            "CONVERSATION_INTERPRETATION_INVALID",
            "El asistente no pudo interpretar la pregunta en esta conversación. No se guardó nada; reintente con la misma clave o "
            "reformule la pregunta con una clave nueva.",
        ) from None
    logger.info(
        "Interpretación de la conversación (conversacion=%s, ordenes=%s, caracteres=%d, limitado=%s, decision=%s)",
        reserva.conversacion_id, list(contexto.ordenes), len(contexto.historial_json), contexto.limitado, decision.decision,
    )
    if decision.decision == DECISION_ACLARAR:
        raise BusinessValidationError(
            "CONVERSATION_CLARIFICATION_REQUIRED", TEXTO_ACLARACION, details={"accion": "REFORMULAR_CON_CLAVE_NUEVA"}
        )
    if decision.decision == DECISION_CAMBIAR_CONTEXTO:
        raise BusinessValidationError(
            "CONVERSATION_CONTEXT_CHANGE_REQUIRED", TEXTO_CAMBIO_DE_CONTEXTO, details={"accion": "INICIAR_CONVERSACION_NUEVA"}
        )
    return decision.pregunta_autonoma or ""


async def _generar_solo_estado(db: AsyncSession, reserva: Reserva, *, pregunta: str, ambiente: str) -> ConsultaResponse:
    """Fase 2 de una pregunta de SOLO ESTADO: la lee de Análisis GRI (empresa, sector, año y ambiente de la conversación) bajo el mismo
    presupuesto global, sin contexto, sin interpretación, sin recursos vectoriales y sin llamar a embeddings ni al modelo."""
    return await _bajo_presupuesto(
        lambda: responder_solo_estado(
            db, pregunta=pregunta, ambiente=ambiente, sector=reserva.sector, empresa_id=reserva.empresa_id, anio=reserva.anio
        )
    )


async def _generar(
    db: AsyncSession, db_vectorial: AsyncSession, reserva: Reserva, *, pregunta: str, ambiente: str
) -> ConsultaResponse:
    """Fase 2 bajo UN SOLO presupuesto global que cubre la lectura del contexto, la interpretación y el RAG. Cancelar la ESPERA no
    garantiza detener el trabajo externo ya iniciado (p. ej. la llamada a OCI corre en un hilo): el proveedor puede terminar aunque
    aquí se haya agotado el plazo. Por eso la garantía es «como máximo un intercambio persistido por clave», nunca «exactamente una
    llamada al proveedor»."""

    async def interpretar_y_responder() -> ConsultaResponse:
        autonoma = await _interpretar(db, reserva, pregunta)
        # B) El RAG ACTUAL, sin cambios: recupera y verifica de nuevo con la pregunta autónoma, la empresa, el sector y el año de
        # la conversación. El historial no es evidencia y no se reutiliza ninguna cita anterior.
        return await responder_consulta(
            db,
            db_vectorial,
            pregunta=autonoma,
            ambiente=ambiente,
            proveedor_embeddings=reserva.embeddings,
            proveedor_llm=reserva.llm,
            sector=reserva.sector,
            empresa_id=reserva.empresa_id,
            anio=reserva.anio,
        )

    return await _bajo_presupuesto(interpretar_y_responder)


async def _bajo_presupuesto(operacion: Callable[[], Awaitable[ConsultaResponse]]) -> ConsultaResponse:
    """Ejecuta la fase 2 bajo el presupuesto global (`PRESUPUESTO_GENERACION_SEGUNDOS`)."""
    plazo = asyncio.timeout(PRESUPUESTO_GENERACION_SEGUNDOS)
    try:
        async with plazo:
            return await operacion()
    except TimeoutError:
        if not plazo.expired():
            raise  # un TimeoutError ajeno al presupuesto no se reinterpreta
        raise ExternalServiceTimeoutError(
            "ASSISTANT_GENERATION_TIMEOUT",
            f"El asistente no terminó de generar la respuesta en {PRESUPUESTO_GENERACION_SEGUNDOS:g} segundos. No se guardó nada; "
            "reintente con la misma clave de idempotencia.",
            details={"presupuesto_segundos": PRESUPUESTO_GENERACION_SEGUNDOS},
        ) from None


async def continuar_conversacion(
    db: AsyncSession,
    *,
    usuario_id: uuid.UUID,
    ambiente: str,
    conversacion_id: uuid.UUID,
    pregunta: str,
    clave_idempotencia: uuid.UUID,
    recursos: RecursosRag,
) -> IntercambioGuardado:
    """Añade UNA consulta a una conversación guardada y devuelve el intercambio persistido (nuevo o ya completado).

    ORDEN: (1) autenticación y rol (el router) -> (2) la conversación es del usuario y ambiente (404 uniforme) -> (3) idempotencia: un
    intercambio completado se devuelve YA, desde la base transaccional -> (4) solo una consulta nueva: límites, empresa activa,
    `recursos.preparar()` (esquema vectorial, configuración, proveedores) y reserva -> (5) sesión vectorial; contexto,
    interpretación y RAG bajo el presupuesto global -> (6) publicación. Un intercambio completado nunca interpreta ni lee contexto."""
    reserva = await _reservar(
        db,
        usuario_id=usuario_id,
        ambiente=ambiente,
        conversacion_id=conversacion_id,
        pregunta=pregunta,
        clave=clave_idempotencia,
        preparar=recursos.preparar,
    )
    if isinstance(reserva, IntercambioGuardado):
        return reserva

    try:
        if reserva.solo_estado:  # sin sesión vectorial, contexto, interpretación ni RAG
            respuesta = await _generar_solo_estado(db, reserva, pregunta=pregunta, ambiente=ambiente)
        else:
            # Si abrir la sesión vectorial falla (configuración, motor), ocurre DESPUÉS de reservar: se marca FALLIDO como cualquier fallo.
            async with recursos.sesion_vectorial() as db_vectorial:
                respuesta = await _generar(db, db_vectorial, reserva, pregunta=pregunta, ambiente=ambiente)
    except Exception as exc:
        # Cancelación (BaseException) no se captura: la reserva vencerá y se recuperará.
        await _marcar_fallido(db, reserva, codigo_seguro(exc))
        raise
    await db.rollback()  # la recuperación dejó una lectura abierta; la fase 3 empieza limpia
    return await _publicar(db, reserva, pregunta, respuesta)

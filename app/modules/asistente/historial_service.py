"""Casos de uso del historial del Asistente IA (Módulo 6, CU004) — ENTREGA 1: guardar, listar y recuperar conversaciones.

Ninguna operación de este módulo invoca al LLM, genera embeddings ni consulta la base vectorial: solo usa la base transaccional.

GUARDAR (atómico e idempotente).
  1. Se calcula la huella (SHA-256) del contexto y los mensajes recibidos.
  2. Si la clave de idempotencia ya existe para (ambiente, usuario): misma huella -> se devuelve lo guardado (sin revalidar la
     empresa: es una repetición, no una operación nueva); huella distinta -> 409.
  3. Si no existe, se valida el contexto en el servidor (empresa existente, activa y del sector indicado: las mismas reglas que
     `POST /asistente/consultas`) y se inserta la conversación con TODOS sus mensajes en una sola transacción.
  4. Dos solicitudes simultáneas con la misma clave: la restricción única de la base deja pasar a una; la otra captura la violación,
     deshace su transacción y resuelve como en el paso 2.

PROPIETARIO Y AMBIENTE. Siempre salen de la sesión autenticada y de la configuración del servidor; el cliente no los elige. Una
conversación inexistente, de otro usuario o de otro ambiente produce la MISMA respuesta (404): no se revela su existencia.

EMPRESA DESACTIVADA DESPUÉS DEL GUARDADO. La recuperación es histórica y sigue permitida; `contexto.empresa_activa` informa el estado
actual. Eso no autoriza nuevas consultas: `POST /asistente/consultas` sigue exigiendo empresa activa.

Los registros del servidor no incluyen preguntas, respuestas ni citas: solo identificadores y conteos.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, PayloadTooLargeError
from app.models import Empresa
from app.models.asistente import ConversacionAsistente, MensajeAsistente, ProcedenciaMensaje, RolMensaje
from app.modules.asistente.domain.historial import (
    MAX_BYTES_CONVERSACION,
    contenido_canonico,
    contenido_de_respuesta,
    huella_de_contenido,
    titulo_desde_pregunta,
)
from app.modules.asistente.historial_schemas import (
    ContextoConversacion,
    ConversacionDetalle,
    ConversacionGuardada,
    ConversacionResumen,
    GuardarConversacionRequest,
    MensajeAsistenteEntrada,
    MensajeGuardado,
    PaginaConversaciones,
)
from app.modules.asistente.schemas import ConsultaResponse
from app.modules.asistente.service import _empresa_consultable

logger = logging.getLogger("igualab.asistente.historial")

RESTRICCION_IDEMPOTENCIA = "uq_conversaciones_asistente_idempotencia"


def _no_encontrada() -> NotFoundError:
    # MISMA respuesta para inexistente, ajena u otro ambiente: no se revela si el identificador existe.
    return NotFoundError("CONVERSATION_NOT_FOUND", "La conversación no existe.")


def _nombre_de_restriccion(exc: IntegrityError) -> str | None:
    causa = getattr(exc.orig, "__cause__", None)
    return getattr(causa, "constraint_name", None)


def _mensajes_normalizados(payload: GuardarConversacionRequest) -> list[dict]:
    """Lo que se guarda de cada mensaje (y lo que entra en la huella): el rol, el contenido y, en el asistente, su respuesta."""
    normalizados: list[dict] = []
    for mensaje in payload.mensajes:
        if isinstance(mensaje, MensajeAsistenteEntrada):
            normalizados.append(
                {
                    "rol": RolMensaje.ASISTENTE.value,
                    "contenido": contenido_de_respuesta(mensaje.respuesta),
                    "datos_respuesta": mensaje.respuesta.model_dump(mode="json"),
                }
            )
        else:
            normalizados.append({"rol": RolMensaje.USUARIO.value, "contenido": mensaje.contenido, "datos_respuesta": None})
    return normalizados


async def _por_clave(db: AsyncSession, ambiente: str, usuario_id: uuid.UUID, clave: uuid.UUID) -> ConversacionAsistente | None:
    consulta = select(ConversacionAsistente).where(
        ConversacionAsistente.ambiente == ambiente,
        ConversacionAsistente.usuario_id == usuario_id,
        ConversacionAsistente.clave_idempotencia == clave,
    )
    return (await db.execute(consulta)).scalar_one_or_none()


def _contexto(empresa_id, nombre, activa, sector, anio) -> ContextoConversacion:
    return ContextoConversacion(empresa_id=empresa_id, empresa_nombre=nombre, empresa_activa=activa, sector=sector, anio=anio)


def _resumen(c: ConversacionAsistente, nombre: str, activa: bool, total: int) -> dict:
    return {
        "id": c.id,
        "titulo": c.titulo,
        "contexto": _contexto(c.empresa_id, nombre, activa, c.sector, c.anio),
        "total_mensajes": total,
        "creada_en": c.creada_en,
        "actualizada_en": c.actualizada_en,
    }


def _mensaje_guardado(m: MensajeAsistente) -> MensajeGuardado:
    respuesta = ConsultaResponse.model_validate(m.datos_respuesta) if m.datos_respuesta is not None else None
    return MensajeGuardado(
        id=m.id,
        orden=m.orden,
        rol=m.rol,
        contenido=m.contenido,
        respuesta=respuesta,
        procedencia=ProcedenciaMensaje(m.procedencia),
        creado_en=m.creado_en,
    )


async def _detalle(db: AsyncSession, conversacion_id: uuid.UUID, ambiente: str, usuario_id: uuid.UUID) -> ConversacionDetalle:
    consulta = (
        select(ConversacionAsistente, Empresa.nombre, Empresa.activa)
        .join(Empresa, Empresa.id == ConversacionAsistente.empresa_id)
        .where(
            ConversacionAsistente.id == conversacion_id,
            ConversacionAsistente.ambiente == ambiente,
            ConversacionAsistente.usuario_id == usuario_id,
        )
    )
    fila = (await db.execute(consulta)).first()
    if fila is None:
        raise _no_encontrada()
    conversacion, empresa_nombre, empresa_activa = fila
    mensajes = (
        await db.execute(
            select(MensajeAsistente).where(MensajeAsistente.conversacion_id == conversacion.id).order_by(MensajeAsistente.orden)
        )
    ).scalars().all()
    return ConversacionDetalle(
        **_resumen(conversacion, empresa_nombre, empresa_activa, len(mensajes)),
        mensajes=[_mensaje_guardado(m) for m in mensajes],
    )


async def _resolver_repeticion(
    db: AsyncSession, existente: ConversacionAsistente, huella: str, ambiente: str, usuario_id: uuid.UUID
) -> ConversacionGuardada:
    """La clave ya existía: mismo contenido -> lo guardado; contenido distinto -> conflicto explícito."""
    if existente.huella_contenido != huella:
        raise ConflictError(
            "CONVERSATION_IDEMPOTENCY_CONFLICT",
            "La clave de idempotencia ya se usó con un contenido distinto. Genere una clave nueva para guardar otra conversación.",
        )
    detalle = await _detalle(db, existente.id, ambiente, usuario_id)
    return ConversacionGuardada(**detalle.model_dump(), reutilizada=True)


async def guardar_conversacion(
    db: AsyncSession, *, usuario_id: uuid.UUID, ambiente: str, payload: GuardarConversacionRequest
) -> ConversacionGuardada:
    """Guarda una conversación nueva con todos sus mensajes, o devuelve la ya guardada con la misma clave y el mismo contenido."""
    normalizados = _mensajes_normalizados(payload)
    canonico = contenido_canonico(payload.empresa_id, payload.sector, payload.anio, normalizados)
    if len(canonico.encode("utf-8")) > MAX_BYTES_CONVERSACION:
        raise PayloadTooLargeError(
            "CONVERSATION_TOO_LARGE",
            f"La conversación supera el tamaño máximo admitido ({MAX_BYTES_CONVERSACION} bytes de contenido).",
        )
    huella = huella_de_contenido(canonico)

    existente = await _por_clave(db, ambiente, usuario_id, payload.clave_idempotencia)
    if existente is not None:
        return await _resolver_repeticion(db, existente, huella, ambiente, usuario_id)

    await _empresa_consultable(db, payload.empresa_id, payload.sector)  # 404 / 400 con las reglas de las consultas

    conversacion = ConversacionAsistente(
        id=uuid.uuid4(),
        ambiente=ambiente,
        usuario_id=usuario_id,
        empresa_id=payload.empresa_id,
        sector=payload.sector,
        anio=payload.anio,
        titulo=titulo_desde_pregunta(payload.mensajes[0].contenido),
        clave_idempotencia=payload.clave_idempotencia,
        huella_contenido=huella,
    )
    db.add(conversacion)
    db.add_all(
        MensajeAsistente(
            id=uuid.uuid4(),
            conversacion_id=conversacion.id,
            orden=orden,
            rol=datos["rol"],
            contenido=datos["contenido"],
            datos_respuesta=datos["datos_respuesta"],
            procedencia=ProcedenciaMensaje.APORTADO_POR_CLIENTE.value,
        )
        for orden, datos in enumerate(normalizados, start=1)
    )
    conversacion_id = conversacion.id
    try:
        await db.commit()  # conversación y mensajes en UNA transacción: o se guardan todos o ninguno
    except IntegrityError as exc:
        await db.rollback()
        if _nombre_de_restriccion(exc) != RESTRICCION_IDEMPOTENCIA:
            raise
        ganadora = await _por_clave(db, ambiente, usuario_id, payload.clave_idempotencia)
        if ganadora is None:  # la restricción saltó pero la fila no es visible: no se inventa un resultado
            raise
        return await _resolver_repeticion(db, ganadora, huella, ambiente, usuario_id)
    except BaseException:
        await db.rollback()
        raise

    logger.info("Conversación del asistente guardada (id=%s, mensajes=%d)", conversacion_id, len(normalizados))
    detalle = await _detalle(db, conversacion_id, ambiente, usuario_id)
    return ConversacionGuardada(**detalle.model_dump(), reutilizada=False)


async def listar_conversaciones(
    db: AsyncSession, *, usuario_id: uuid.UUID, ambiente: str, pagina: int, tamano: int
) -> PaginaConversaciones:
    """Metadatos de las conversaciones del usuario en este ambiente, de la más reciente a la más antigua. Sin mensajes."""
    propias = (ConversacionAsistente.ambiente == ambiente, ConversacionAsistente.usuario_id == usuario_id)
    total = (await db.execute(select(func.count()).select_from(ConversacionAsistente).where(*propias))).scalar_one()
    mensajes = (
        select(func.count())
        .where(MensajeAsistente.conversacion_id == ConversacionAsistente.id)
        .correlate(ConversacionAsistente)
        .scalar_subquery()
    )
    consulta = (
        select(ConversacionAsistente, Empresa.nombre, Empresa.activa, mensajes)
        .join(Empresa, Empresa.id == ConversacionAsistente.empresa_id)
        .where(*propias)
        .order_by(ConversacionAsistente.actualizada_en.desc(), ConversacionAsistente.id.desc())
        .limit(tamano)
        .offset((pagina - 1) * tamano)
    )
    filas = (await db.execute(consulta)).all()
    return PaginaConversaciones(
        items=[ConversacionResumen(**_resumen(c, nombre, activa, cantidad)) for c, nombre, activa, cantidad in filas],
        pagina=pagina,
        tamano=tamano,
        total=total,
    )


async def obtener_conversacion(
    db: AsyncSession, *, usuario_id: uuid.UUID, ambiente: str, conversacion_id: uuid.UUID
) -> ConversacionDetalle:
    """La conversación con sus mensajes en orden estable. Solo la ve su propietario, en su ambiente."""
    return await _detalle(db, conversacion_id, ambiente, usuario_id)

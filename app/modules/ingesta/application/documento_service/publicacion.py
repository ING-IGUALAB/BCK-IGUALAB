"""Compuerta de publicación transaccional y publicación idempotente en la base vectorial."""

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import ConflictError, ExternalServiceError
from app.modules.auditoria.service import registrar_evento
from app.models import TipoEventoAuditoria
from app.models.documento_ingesta import Documento, EstadoProcesamiento, EtapaIngesta, ResultadoAnalisis
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores
from app.modules.ingesta.domain import validacion
from ._comun import _ahora, _cargar, _condiciones_ejecutor, _conflicto_de_estado, _exigir_token, _terminar_lectura, _transicion, auditar_aparte

# --- Compuerta de publicación (no publica nada en 4A) --------------------------------------

async def publicar_documento(
    db: AsyncSession,
    documento_id: uuid.UUID,
    *,
    token: uuid.UUID,
    indexacion_confirmada: bool,
    resultado_analisis: ResultadoAnalisis | None,
    exigir_analisis_persistido: bool = False,
) -> Documento:
    """COMPUERTA de finalización transaccional: pasa el documento a `COMPLETADO`.

    La invoca el coordinador DESPUÉS de insertar los fragmentos (no publicados) y de persistir el
    análisis, y ANTES de publicar los fragmentos vectoriales. Con `exigir_analisis_persistido` exige
    además que `documentos.analisis` exista y coincida con `resultado_analisis`; el registro de
    auditoría del éxito se escribe en la MISMA transacción que `COMPLETADO` (si falla, no se completa).

    Exige original almacenado, indexación vectorial confirmada y un análisis ejecutado
    (`resultado_analisis`; OBSERVADO solo si el detector corrió bien sin hallazgos), y
    REVALIDA la empresa (activa y con el mismo sector) antes de pasar a `COMPLETADO`.
    Quien llama es responsable de que las confirmaciones sean verdaderas: este módulo no
    puede verificar vectores ni análisis que todavía no existen. Si algo falta o la
    empresa dejó de ser válida, no se modifica nada. Solo publica quien posee la operación
    (`token`): si la recuperaron, el token rotó y la publicación se rechaza.
    """
    token = _exigir_token(token)
    documento = await _cargar(db, documento_id, bloquear=True)
    faltantes = []
    if documento.estado_procesamiento is not EstadoProcesamiento.EN_PROCESO:
        faltantes.append("documento_en_proceso")
    if documento.ejecucion_token != token:
        faltantes.append("propiedad_de_la_operacion")
    if documento.original_almacenado_en is None:
        faltantes.append("original_almacenado")
    if indexacion_confirmada is not True:
        faltantes.append("indexacion_confirmada")
    if not isinstance(resultado_analisis, ResultadoAnalisis):
        faltantes.append("resultado_analisis")
    elif exigir_analisis_persistido:
        if documento.analisis is None:
            faltantes.append("analisis_persistido")
        elif documento.analisis.get("resultado") != resultado_analisis.value:
            faltantes.append("analisis_consistente")
    if faltantes:
        await db.rollback()
        raise ConflictError(
            "DOCUMENT_NOT_PUBLISHABLE",
            "El documento aún no puede publicarse: faltan etapas por completar.",
            details={"faltantes": faltantes},
        )
    try:
        await validacion.obtener_empresa_activa(db, documento.empresa_id, documento.sector)
    except Exception:
        await db.rollback()
        raise
    if not await _transicion(
        db,
        documento_id,
        [
            Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
            Documento.ejecucion_token == token,
            Documento.original_almacenado_en.is_not(None),
        ],
        {
            "estado_procesamiento": EstadoProcesamiento.COMPLETADO,
            "resultado_analisis": resultado_analisis,
            "completado_en": _ahora(),
            "etapa_actual": EtapaIngesta.PUBLICANDO.value,
            "progreso_actualizado_en": _ahora(),
        },
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    registrar_evento(
        db,
        TipoEventoAuditoria.INGESTA_DATOS,
        f"Ingesta completada (base transaccional); documento_id={documento_id}; "
        f"resultado={resultado_analisis.value}; publicacion_vectorial=pendiente",
        documento.usuario_id,
    )
    await db.commit()
    documento = await _cargar(db, documento_id)
    await _terminar_lectura(db)
    return documento


async def registrar_intento_vectorial(
    db: AsyncSession, documento_id: uuid.UUID, *, token: uuid.UUID
) -> None:
    """Deja constancia DURADERA, ANTES de insertar el primer lote, de que este documento va a escribir en
    la base vectorial. Desde ese momento su compensación exige limpiar también esa base. UPDATE condicional:
    solo el ejecutor (`token`), con el original ya almacenado, la vigencia no vencida y una única vez."""
    if not await _transicion(
        db,
        documento_id,
        [
            *_condiciones_ejecutor(token),
            Documento.ejecucion_vigente_hasta > _ahora(),
            Documento.original_almacenado_en.is_not(None),
            Documento.vector_escritura_intentada_en.is_(None),
        ],
        {"vector_escritura_intentada_en": _ahora(), "progreso_actualizado_en": _ahora()},
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()


async def _registrar_fallo_publicacion(db: AsyncSession, documento_id: uuid.UUID, codigo: str) -> None:
    """Rastro duradero de un intento fallido de publicar (el documento sigue COMPLETADO y recuperable).
    Mejor esfuerzo: si la propia base transaccional falla, el documento sigue siendo recuperable."""
    try:
        await db.rollback()
        await _transicion(
            db,
            documento_id,
            [
                Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
                Documento.vector_publicado_en.is_(None),
            ],
            {
                "vector_publicacion_intentos": Documento.vector_publicacion_intentos + 1,
                "vector_ultimo_error": codigo,
                "progreso_actualizado_en": _ahora(),
            },
        )
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


async def publicar_en_base_vectorial(
    db: AsyncSession, vectorial: AsyncSession, documento_id: uuid.UUID
) -> bool:
    """Publica los fragmentos de un documento `COMPLETADO` y registra la publicación confirmada.

    IDEMPOTENTE y REPETIBLE: no vuelve a generar embeddings ni a analizar. Devuelve `True` si esta llamada
    dejó registrada la publicación y `False` si ya estaba registrada. Éxito = los fragmentos persistidos
    (`fragmentos_total`) están TODOS publicados según la base vectorial; si el conteo no coincide, no se
    registra y se lanza `ExternalServiceError` (`VECTOR_PUBLICATION_NOT_CONFIRMED`); si la base vectorial
    falla, `VECTOR_PUBLICATION_FAILED`. En ambos casos el documento SIGUE `COMPLETADO` con la publicación
    pendiente (intentos y último código quedan en la fila) y se puede repetir. Nunca anuncia éxito antes.
    No hay atomicidad entre las bases: un fallo después de publicar y antes de registrar se resuelve
    repitiendo esta función (publicar es idempotente). Una cancelación se propaga sin registrar nada.
    """
    documento = await _cargar(db, documento_id)
    if documento.estado_procesamiento is not EstadoProcesamiento.COMPLETADO or documento.vector_escritura_intentada_en is None:
        await db.rollback()
        raise _conflicto_de_estado()
    if documento.vector_publicado_en is not None:
        await _terminar_lectura(db)
        return False
    ambiente, total, usuario_id = documento.ambiente, documento.fragmentos_total, documento.usuario_id
    await _terminar_lectura(db)

    try:
        if vectorial.in_transaction():
            await vectorial.rollback()
        await vectores.publicar_fragmentos(vectorial, ambiente=ambiente, documento_id=documento_id)
        conteo = await vectores.contar_fragmentos(vectorial, ambiente=ambiente, documento_id=documento_id)
    except Exception:
        await _registrar_fallo_publicacion(db, documento_id, "VECTOR_PUBLICATION_FAILED")
        raise ExternalServiceError(
            "VECTOR_PUBLICATION_FAILED", "No se pudo publicar en la base vectorial; la publicación sigue pendiente."
        ) from None
    if not total or conteo.total != total or conteo.publicados != total:
        await _registrar_fallo_publicacion(db, documento_id, "VECTOR_PUBLICATION_NOT_CONFIRMED")
        raise ExternalServiceError(
            "VECTOR_PUBLICATION_NOT_CONFIRMED",
            "No se pudo confirmar que todos los fragmentos estén publicados; la publicación sigue pendiente.",
        )

    ahora = _ahora()
    registrado = await _transicion(
        db,
        documento_id,
        [
            Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
            Documento.vector_publicado_en.is_(None),
        ],
        {
            "vector_publicado_en": ahora,
            "vector_ultimo_error": None,
            "etapa_actual": EtapaIngesta.FINALIZADO.value,
            "fragmentos_procesados": total,
            "progreso_actualizado_en": ahora,
        },
    )
    await db.commit()
    if registrado:
        await auditar_aparte(
            db,
            TipoEventoAuditoria.INGESTA_DATOS,
            f"Publicación vectorial confirmada; documento_id={documento_id}; fragmentos={total}",
            usuario_id,
        )
    return registrado

"""Almacenamiento del original, limpieza con incertidumbre y compensación conjunta."""

import hashlib
import uuid
from datetime import timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import BusinessValidationError, ExternalServiceError, codigo_seguro
from app.models.documento_ingesta import VIGENCIA_PREDETERMINADA, Documento, EstadoCompensacion, EstadoProcesamiento
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores
from app.modules.ingesta.domain.almacenamiento import AlmacenOriginales, EstadoSubida, ReferenciaOriginal
from ._comun import _ahora, _cargar, _conflicto_de_estado, _exigir_duracion, _exigir_token, _terminar_lectura, _transicion
from .procesamiento import fallar_documento

async def _anotar_existencia(db: AsyncSession, documento_id: uuid.UUID, version: str | None) -> None:
    """Deja constancia DURABLE de que el original existió, ANTES de eliminarlo. Así, si la
    compensación se interrumpe tras borrar, un reintento sabe que la ausencia es limpieza y
    no un objeto que quizá nunca se creó."""
    await _transicion(
        db,
        documento_id,
        [
            Documento.estado_procesamiento == EstadoProcesamiento.FALLIDO,
            Documento.estado_compensacion == EstadoCompensacion.PENDIENTE,
            Documento.original_almacenado_en.is_(None),
        ],
        {"original_almacenado_en": _ahora(), "version_id_original": version},
    )
    await db.commit()


def _sin_confirmar(codigo: str, mensaje: str) -> ExternalServiceError:
    return ExternalServiceError(codigo, mensaje)


async def _eliminar_y_verificar(almacen: AlmacenOriginales, referencia: ReferenciaOriginal) -> None:
    await almacen.eliminar(referencia)
    if await almacen.existe(referencia):
        raise _sin_confirmar("STORAGE_CLEANUP_NOT_CONFIRMED", "No se pudo confirmar la eliminación del original.")


async def _reconciliar_y_eliminar(
    db: AsyncSession,
    almacen: AlmacenOriginales,
    documento_id: uuid.UUID,
    clave: str,
    tamano: int,
    version_bd: str | None,
    existencia_conocida: bool,
) -> None:
    """Retorna SOLO si el original quedó eliminado y verificado, o se probó que nunca se creó.
    En cualquier otro caso lanza `ExternalServiceError` con un código estable.

    1. Subida aún en curso en este proceso: no hay nada que concluir, el objeto puede aparecer.
    2. Versión conocida (la subida terminó y la devolvió, o la BD la guardó): se elimina ESA
       versión y se comprueba ESA versión. Una subida terminada sin versión en la respuesta no
       creó versiones: se elimina la clave y se comprueba.
    3. Rechazo definitivo (nunca creada): basta comprobar que no hay objeto.
    4. Desenlace incierto o desconocido: se listan TODAS las versiones de la clave exacta.
       - un objeto de otro tamaño, o más de uno: no es lo esperado, no se borra nada;
       - objeto hallado: se anota su existencia, se elimina por `VersionId` y se relista, que
         debe quedar vacío (hay un único intento por clave y ya terminó: no puede reaparecer);
       - nada hallado: solo es limpieza si ya constaba que existió; si no, la ausencia no
         prueba que no se vaya a crear (petición lenta, otro proceso) y sigue PENDIENTE;
       - sin permiso para listar: PENDIENTE, sin presumir nada.
    """
    subida = almacen.estado_subida(clave)
    if subida.estado is EstadoSubida.EN_CURSO:
        raise _sin_confirmar("STORAGE_UPLOAD_IN_FLIGHT", "La subida sigue en curso: el objeto aún puede crearse.")

    version = subida.version_id if subida.estado is EstadoSubida.CREADA else version_bd
    if subida.estado is EstadoSubida.CREADA or version is not None:
        await _anotar_existencia(db, documento_id, version)
        await _eliminar_y_verificar(almacen, ReferenciaOriginal(clave, version))
        return

    if subida.estado is EstadoSubida.NO_CREADA:
        if await almacen.existe(ReferenciaOriginal(clave)):
            raise _sin_confirmar("STORAGE_UNEXPECTED_OBJECT", "Existe un objeto que esta subida no pudo crear.")
        return

    try:
        versiones = await almacen.listar_versiones(clave)
    except Exception:
        raise _sin_confirmar(
            "STORAGE_RECONCILIATION_UNAVAILABLE",
            "No se pudieron listar las versiones del objeto (permisos o error del servidor).",
        ) from None
    objetos = [v for v in versiones if not v.es_marca_de_borrado]
    if len(objetos) > 1 or any(v.tamano != tamano for v in objetos):
        raise _sin_confirmar("STORAGE_UNEXPECTED_OBJECT", "El objeto hallado no es el que se esperaba.")
    if not objetos and not existencia_conocida:
        raise _sin_confirmar("STORAGE_OUTCOME_UNCERTAIN", "No se puede probar que la subida no creó el objeto.")
    if objetos:
        await _anotar_existencia(db, documento_id, objetos[0].version_id)
    for hallada in versiones:
        await almacen.eliminar(ReferenciaOriginal(clave, hallada.version_id))
    if await almacen.listar_versiones(clave):
        raise _sin_confirmar("STORAGE_CLEANUP_NOT_CONFIRMED", "No se pudo confirmar la eliminación del original.")


async def _limpiar_vectorial(vectorial: AsyncSession | None, ambiente: str, documento_id: uuid.UUID) -> None:
    """Retorna SOLO si los fragmentos del documento quedaron eliminados y CERRADO el documento en la
    base vectorial (sin posibilidad de escrituras tardías) y se comprobó que no queda ninguno.
    Cualquier otro desenlace lanza `ExternalServiceError` con un código estable."""
    if vectorial is None:
        raise ExternalServiceError(
            "VECTOR_CLEANUP_UNAVAILABLE",
            "El documento escribió en la base vectorial y no se dispone de ella para limpiarla.",
        )
    try:
        if vectorial.in_transaction():
            await vectorial.rollback()
        await vectores.cerrar_y_eliminar_fragmentos(vectorial, ambiente=ambiente, documento_id=documento_id)
        conteo = await vectores.contar_fragmentos(vectorial, ambiente=ambiente, documento_id=documento_id)
    except Exception:
        raise ExternalServiceError(
            "VECTOR_CLEANUP_FAILED", "No se pudo limpiar la base vectorial."
        ) from None
    if conteo.total != 0:
        raise ExternalServiceError(
            "VECTOR_CLEANUP_NOT_CONFIRMED", "Tras la limpieza aún quedan fragmentos del documento."
        )


async def compensar_documento(
    db: AsyncSession,
    almacen: AlmacenOriginales,
    documento_id: uuid.UUID,
    *,
    vectorial: AsyncSession | None = None,
) -> EstadoCompensacion:
    """Limpia lo externo de un intento fallido (base vectorial Y original en MinIO) y libera su reserva.

    CONTRATO CONJUNTO. Si el documento llegó a registrar una escritura vectorial
    (`vector_escritura_intentada_en`), la limpieza exige la sesión `vectorial` (SIN transacción abierta):
    `cerrar_y_eliminar_fragmentos` + `contar_fragmentos == 0`; sin ella (o si falla o queda algo) la
    compensación sigue PENDIENTE y la reserva ACTIVA (`VECTOR_CLEANUP_*`). La reserva solo se libera
    cuando TODA la limpieza (vectorial y MinIO) está confirmada. El cierre del documento en la base
    vectorial impide que una escritura tardía de un ejecutor anterior reintroduzca fragmentos después.
    Un documento que nunca escribió vectores no necesita `vectorial`.

    Idempotente y repetible. Solo actúa sobre documentos `FALLIDO` con compensación
    `PENDIENTE`: jamás sobre `EN_PROCESO` ni `COMPLETADO`. La limpieza solo se declara si
    `_reconciliar_y_eliminar` la prueba (subida en curso, versión conocida o no, resultado
    incierto). Si no, deja la compensación `PENDIENTE` (con contador y código del último
    error), CONSERVA la reserva y no informa limpieza. No relanza errores del almacén:
    devuelve el estado resultante.
    """
    documento = await _cargar(db, documento_id)
    if documento.estado_compensacion is EstadoCompensacion.COMPLETADA:
        await _terminar_lectura(db)
        return EstadoCompensacion.COMPLETADA
    if (
        documento.estado_procesamiento is not EstadoProcesamiento.FALLIDO
        or documento.estado_compensacion is not EstadoCompensacion.PENDIENTE
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    if documento.ambiente != almacen.ambiente:
        await db.rollback()
        raise BusinessValidationError(
            "STORAGE_ENVIRONMENT_MISMATCH", "El almacén no corresponde al ambiente del documento."
        )
    clave, tamano = documento.clave_original, documento.tamano_bytes
    version_bd, existencia_conocida = documento.version_id_original, documento.original_almacenado_en is not None
    ambiente_documento = documento.ambiente
    limpieza_vectorial = documento.vector_escritura_intentada_en is not None
    await _terminar_lectura(db)  # sin transacción abierta durante la llamada externa

    pendiente = [
        Documento.estado_procesamiento == EstadoProcesamiento.FALLIDO,
        Documento.estado_compensacion == EstadoCompensacion.PENDIENTE,
    ]
    try:
        if limpieza_vectorial:
            await _limpiar_vectorial(vectorial, ambiente_documento, documento_id)
        await _reconciliar_y_eliminar(db, almacen, documento_id, clave, tamano, version_bd, existencia_conocida)
    except Exception as exc:
        await db.rollback()
        await _transicion(
            db,
            documento_id,
            pendiente,
            {
                "compensacion_intentos": Documento.compensacion_intentos + 1,
                "ultimo_error_compensacion": codigo_seguro(exc, ExternalServiceError),
            },
        )
        await db.commit()
        return EstadoCompensacion.PENDIENTE

    # Limpieza confirmada: la reserva se libera en la MISMA sentencia.
    await _transicion(
        db,
        documento_id,
        pendiente,
        {
            "estado_compensacion": EstadoCompensacion.COMPLETADA,
            "reserva_activa": False,
            "compensada_en": _ahora(),
            "ultimo_error_compensacion": None,
        },
    )
    await db.commit()
    return EstadoCompensacion.COMPLETADA


async def _fallar_y_compensar(
    db: AsyncSession, almacen: AlmacenOriginales, documento_id: uuid.UUID, motivo: str, token: uuid.UUID
) -> None:
    """Mejor esfuerzo tras un fallo de subida. Si la propia BD falla, el documento queda
    `EN_PROCESO` con el intento registrado y lo recupera `recuperar_documentos_pendientes`
    cuando su vigencia venza. Si la subida sigue en curso, la compensación queda PENDIENTE."""
    try:
        await fallar_documento(db, documento_id, motivo, requiere_compensacion=True, token=token)
        await compensar_documento(db, almacen, documento_id)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


# --- Almacenamiento del original ------------------------------------------------------

async def almacenar_original(
    db: AsyncSession,
    almacen: AlmacenOriginales,
    documento_id: uuid.UUID,
    contenido: bytes,
    *,
    token: uuid.UUID,
    duracion_vigencia: timedelta = VIGENCIA_PREDETERMINADA,
) -> Documento:
    """Sube los bytes originales (BOM incluido) de un documento `EN_PROCESO` que el
    llamador posee (`token`).

    El intento se registra ANTES de subir con un UPDATE condicional que SOLO una llamada
    puede ganar: dos intentos simultáneos (incluso desde procesos distintos) para el mismo
    documento no llegan ambos al almacén. Esa es la protección contra la sobrescritura; el
    UUID de la clave solo evita colisiones entre documentos y no se usa escritura condicional
    de S3. Al registrar el intento se extiende la vigencia `duracion_vigencia`, que debe
    cubrir el plazo de la subida (D24). Un fallo o timeout marca el documento `FALLIDO`,
    intenta compensar y relanza el error del almacén: si la subida sigue en curso, la
    compensación queda PENDIENTE hasta conocer su desenlace. Una CANCELACIÓN se propaga sin
    tocar la BD: el documento queda `EN_PROCESO` y la recuperación lo toma al vencer su
    vigencia.
    """
    token = _exigir_token(token)
    duracion_vigencia = _exigir_duracion(duracion_vigencia)
    documento = await _cargar(db, documento_id)
    clave, sha256, tamano = documento.clave_original, documento.sha256, documento.tamano_bytes
    es_ejecutor = (
        documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO and documento.ejecucion_token == token
    )
    ambiente = documento.ambiente
    await _terminar_lectura(db)
    if not es_ejecutor:
        raise _conflicto_de_estado()
    if ambiente != almacen.ambiente:
        raise BusinessValidationError(
            "STORAGE_ENVIRONMENT_MISMATCH", "El almacén no corresponde al ambiente del documento."
        )
    if (
        not isinstance(contenido, (bytes, bytearray))
        or len(contenido) != tamano
        or hashlib.sha256(contenido).hexdigest() != sha256
    ):
        raise BusinessValidationError(
            "ORIGINAL_CONTENT_MISMATCH", "El contenido no corresponde al documento reservado."
        )

    ahora = _ahora()
    if not await _transicion(
        db,
        documento_id,
        [
            Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
            Documento.ejecucion_token == token,
            Documento.ejecucion_vigente_hasta > ahora,  # una operación vencida no empieza a subir
            Documento.almacenamiento_intentado_en.is_(None),
        ],
        {"almacenamiento_intentado_en": ahora, "ejecucion_vigente_hasta": ahora + duracion_vigencia},
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()

    try:
        referencia = await almacen.guardar(clave, bytes(contenido), sha256)
    except Exception as exc:
        await _fallar_y_compensar(db, almacen, documento_id, codigo_seguro(exc, ExternalServiceError), token)
        raise

    if not await _transicion(
        db,
        documento_id,
        [
            Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
            Documento.ejecucion_token == token,  # si la recuperaron, ya no es suyo
        ],
        {"original_almacenado_en": _ahora(), "version_id_original": referencia.version_id},
    ):
        await db.rollback()
        raise _conflicto_de_estado()  # el objeto queda para la compensación/recuperación
    await db.commit()
    documento = await _cargar(db, documento_id)
    await _terminar_lectura(db)
    return documento

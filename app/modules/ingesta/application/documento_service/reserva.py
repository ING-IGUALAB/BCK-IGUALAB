"""Reserva de documentos, enlace con la operación, vigencia y verificación del original."""

import hashlib
import re
import uuid
from datetime import datetime, timedelta
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import AuthorizationError, BusinessValidationError, ConflictError, ExternalServiceError
from app.models import RolUsuario, Usuario
from app.models.documento_ingesta import VIGENCIA_PREDETERMINADA, Documento, EstadoProcesamiento, EstadoOperacion, OperacionIngesta
from app.schemas import MetadatosIngestaRequest
from app.modules.ingesta.domain import validacion
from app.modules.ingesta.domain.almacenamiento import AlmacenOriginales, ReferenciaOriginal, generar_clave_original, validar_ambiente
from app.modules.ingesta.domain.reglas import TAMANO_MAXIMO_BYTES
from ._comun import _ahora, _buscar_conflicto, _cargar, _conflicto_de_estado, _es_violacion_de_reserva, _exigir_duracion, _exigir_token, _terminar_lectura, _transicion

_SHA256_REGEX = re.compile(r"[0-9a-f]{64}")


# --- Reserva ---------------------------------------------------------------------

def _validar_entrada(nombre_archivo: str, sha256: str, tamano_bytes: int, ambiente: str) -> None:
    validacion.validar_nombre_archivo(nombre_archivo)
    validar_ambiente(ambiente)
    if not isinstance(sha256, str) or _SHA256_REGEX.fullmatch(sha256) is None:
        raise BusinessValidationError(
            "INVALID_DOCUMENT_HASH", "El SHA-256 debe ser un resumen hexadecimal en minúsculas de 64 caracteres."
        )
    if (
        isinstance(tamano_bytes, bool)
        or not isinstance(tamano_bytes, int)
        or not 0 < tamano_bytes <= TAMANO_MAXIMO_BYTES
    ):
        raise BusinessValidationError(
            "INVALID_DOCUMENT_SIZE", "El tamaño del documento no es válido."
        )


async def reservar_documento(
    db: AsyncSession,
    *,
    metadatos: MetadatosIngestaRequest,
    nombre_archivo: str,
    sha256: str,
    tamano_bytes: int,
    usuario_id: uuid.UUID,
    ambiente: str,
    operacion_id: uuid.UUID | None = None,
) -> Documento:
    """Reserva SHA-256 y empresa/año/tipo para una carga y la deja `EN_PROCESO`.

    Con `operacion_id`, la operación de ingesta (que debe estar `EN_CARGA`, ser del mismo ambiente y del mismo
    actor) se enlaza al documento en la MISMA transacción que lo reserva: no existe un documento reservado sin
    operación enlazada ni una operación enlazada sin documento.

    El sector se toma de la empresa (el declarado solo se contrasta). `usuario_id`
    debe provenir de la sesión validada, nunca del payload; aquí se comprueba además
    que sea un SuperAdmin habilitado. Una transacción corta, sin llamadas externas.

    Esta es la ÚNICA captura del sector en un documento. La fila de la empresa se lee con un bloqueo compartido que se
    mantiene hasta el commit que crea el documento: un cambio de sector concurrente espera a ese commit y, al verlo,
    se rechaza (`COMPANY_SECTOR_CHANGE_BLOCKED`); si el cambio llegó primero, esta reserva espera y copia el sector ya
    cambiado. En ambos órdenes el documento queda con el sector de su empresa. El bloqueo no abarca la subida a MinIO,
    OCI ni el procesamiento: termina con esta transacción.
    """
    _validar_entrada(nombre_archivo, sha256, tamano_bytes, ambiente)
    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not usuario.habilitado or usuario.rol is not RolUsuario.SUPERADMIN:
        raise AuthorizationError(
            "INGESTION_FORBIDDEN", "Solo un SuperAdmin habilitado puede ingerir documentos."
        )
    try:
        empresa = await validacion.obtener_empresa_activa(db, metadatos.empresa_id, metadatos.sector, bloquear=True)
    except Exception:
        await db.rollback()  # no se deja el bloqueo de la empresa tomado si la empresa no sirve
        raise

    empresa_id, anio, tipo = empresa.id, metadatos.anio, metadatos.tipo
    conflicto = await _buscar_conflicto(db, ambiente, sha256, empresa_id, anio, tipo)
    if conflicto is not None:
        await db.rollback()  # libera el bloqueo de la empresa antes de rechazar
        raise conflicto

    documento_id = uuid.uuid4()
    documento = Documento(
        id=documento_id,
        ambiente=ambiente,
        empresa_id=empresa_id,
        anio=anio,
        tipo=tipo,
        sector=empresa.sector,
        nombre_archivo=nombre_archivo,
        sha256=sha256,
        tamano_bytes=tamano_bytes,
        usuario_id=usuario.id,
        clave_original=generar_clave_original(ambiente, documento_id),
        ejecucion_token=uuid.uuid4(),
        ejecucion_vigente_hasta=_ahora() + VIGENCIA_PREDETERMINADA,
    )
    db.add(documento)
    try:
        if operacion_id is not None:
            await db.flush()  # el documento debe existir antes de enlazarlo (FK)
            await _enlazar_operacion(db, operacion_id, documento_id, ambiente, usuario.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not _es_violacion_de_reserva(exc):
            raise
        # Carrera: otra carga reservó entre la consulta y el INSERT. Gana la BD.
        conflicto = await _buscar_conflicto(db, ambiente, sha256, empresa_id, anio, tipo)
        await db.rollback()
        raise (
            conflicto
            or ConflictError(
                "DOCUMENT_RESERVATION_CONFLICT",
                "Otra carga reservó el mismo documento al mismo tiempo. Reintente.",
            )
        ) from None
    await db.refresh(documento)
    await _terminar_lectura(db)  # `refresh` abre una transacción de lectura: no se deja abierta
    return documento


async def _enlazar_operacion(
    db: AsyncSession, operacion_id: uuid.UUID, documento_id: uuid.UUID, ambiente: str, usuario_id: uuid.UUID
) -> None:
    """UPDATE condicional EN_CARGA -> CON_DOCUMENTO dentro de la transacción de la reserva. Si la operación no
    está en carga (otra solicitud la tomó, ya terminó, es de otro ambiente o de otro actor) no se reserva nada."""
    resultado = await db.execute(
        update(OperacionIngesta)
        .where(
            OperacionIngesta.id == operacion_id,
            OperacionIngesta.ambiente == ambiente,
            OperacionIngesta.usuario_id == usuario_id,
            OperacionIngesta.estado == EstadoOperacion.EN_CARGA.value,
        )
        .values(estado=EstadoOperacion.CON_DOCUMENTO.value, documento_id=documento_id, actualizada_en=_ahora())
        .execution_options(synchronize_session=False)
    )
    if resultado.rowcount != 1:
        await db.rollback()
        raise ConflictError(
            "OPERATION_STATE_CONFLICT",
            "La operación de ingesta no está en un estado que permita reservar el documento.",
        )


async def renovar_vigencia(
    db: AsyncSession,
    documento_id: uuid.UUID,
    *,
    token: uuid.UUID,
    duracion: timedelta = VIGENCIA_PREDETERMINADA,
) -> datetime:
    """El ejecutor demuestra que sigue vivo: extiende su vigencia. Falla (conflicto) si ya no
    posee la operación porque la recuperaron o terminó; entonces debe abandonar el trabajo."""
    token = _exigir_token(token)
    nueva = _ahora() + _exigir_duracion(duracion)
    if not await _transicion(
        db,
        documento_id,
        [Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO, Documento.ejecucion_token == token],
        {"ejecucion_vigente_hasta": nueva},
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()
    return nueva


async def verificar_original(db: AsyncSession, almacen: AlmacenOriginales, documento_id: uuid.UUID) -> None:
    """Lee el original y comprueba tamaño y SHA-256 contra lo reservado."""
    documento = await _cargar(db, documento_id)
    if documento.original_almacenado_en is None:
        await db.rollback()
        raise _conflicto_de_estado()
    referencia = ReferenciaOriginal(documento.clave_original, documento.version_id_original)
    tamano, sha256 = documento.tamano_bytes, documento.sha256
    await _terminar_lectura(db)
    datos = await almacen.leer(referencia)
    if len(datos) != tamano or hashlib.sha256(datos).hexdigest() != sha256:
        raise ExternalServiceError(
            "STORAGE_INTEGRITY_ERROR",
            "El original almacenado no coincide con el documento reservado.",
            details={"operacion": "verificar"},
        )

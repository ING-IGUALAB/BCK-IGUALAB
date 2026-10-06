"""Reserva de documentos y operaciones de almacenamiento/compensación (Etapa 4A).

Secuencia, estados e invariantes: `docs/ingesta/03-arquitectura-y-decisiones.md`
(Etapa 4A). Resumen de las garantías que implementa este módulo:

- NINGUNA transacción de BD permanece abierta durante una llamada al almacén: cada
  paso es una transacción corta que termina con `commit` antes de llamar a MinIO.
- PostgreSQL y MinIO no comparten transacción. No hay atomicidad distribuida: la
  consistencia se logra con estados duraderos, reserva retenida y compensación
  repetible. Nada que no esté `COMPLETADO` es consultable.
- La exclusión de duplicados la imponen índices únicos parciales de la BD; la
  consulta previa solo sirve para informar. Solo se traducen a 409 las violaciones
  de esos índices; cualquier otra `IntegrityError` se propaga.
- La reserva de un intento fallido NO se libera hasta confirmar la limpieza.
- Este módulo NO publica nada: no existe coordinación con vectores ni detector.
  `publicar_documento` es solo una compuerta que exige lo que aún no existe.

Cada función recibe una `AsyncSession` y la usa de forma secuencial (nunca se
comparte entre tareas concurrentes). No registra auditoría (Etapa 6).
"""
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
)
from app.models import RolUsuario, Usuario
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta import validacion
from app.services.ingesta.almacenamiento import (
    AlmacenOriginales,
    ReferenciaOriginal,
    generar_clave_original,
    validar_ambiente,
)
from app.services.ingesta.reglas import TAMANO_MAXIMO_BYTES

_SHA256_REGEX = re.compile(r"[0-9a-f]{64}")
_CODIGO_REGEX = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_RESTRICCIONES_DE_RESERVA = ("uq_documentos_sha256_activo", "uq_documentos_empresa_anio_tipo_activo")
# Mensajes de SQLite para los mismos índices (pruebas); PostgreSQL incluye el nombre.
_COLUMNAS_DE_RESERVA_SQLITE = ("documentos.sha256", "documentos.empresa_id")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(valor: datetime) -> str:
    return (valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _conflicto_de_estado() -> ConflictError:
    return ConflictError(
        "DOCUMENT_STATE_CONFLICT",
        "El documento no está en un estado que permita esta operación.",
    )


# --- Lectura y transiciones condicionales --------------------------------------

async def _cargar(db: AsyncSession, documento_id: uuid.UUID, *, bloquear: bool = False) -> Documento:
    consulta = select(Documento).where(Documento.id == documento_id).execution_options(populate_existing=True)
    if bloquear:
        consulta = consulta.with_for_update()
    documento = (await db.execute(consulta)).scalar_one_or_none()
    if documento is None:
        raise NotFoundError("DOCUMENT_NOT_FOUND", "El documento no existe.")
    return documento


async def _transicion(db: AsyncSession, documento_id: uuid.UUID, condiciones: list, valores: dict) -> bool:
    """UPDATE condicional (compare-and-set). True si cambió exactamente una fila."""
    resultado = await db.execute(
        update(Documento)
        .where(Documento.id == documento_id, *condiciones)
        .values(**valores)
        .execution_options(synchronize_session=False)
    )
    return resultado.rowcount == 1


async def _terminar_lectura(db: AsyncSession) -> None:
    """Cierra la transacción abierta por una lectura antes de una llamada externa."""
    await db.commit()


# --- Duplicados ----------------------------------------------------------------

_PRIORIDAD = {
    EstadoProcesamiento.COMPLETADO: 0,
    EstadoProcesamiento.EN_PROCESO: 1,
    EstadoProcesamiento.FALLIDO: 2,
}


def _error_de_conflicto(documento: Documento, correo: str, criterios: list[str]) -> ConflictError:
    if documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO:
        # Fecha y cuenta de la carga original (RF-014). Sin contenido ni secretos.
        return ConflictError(
            "DOCUMENT_ALREADY_INGESTED",
            "Ya existe un documento ingestado con ese contenido o para esa empresa, año y tipo.",
            details={
                "criterio": criterios,
                "documento_id": str(documento.id),
                "cargado_en": _iso_utc(documento.creado_en),
                "cuenta": correo,
            },
        )
    if documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO:
        return ConflictError(
            "DOCUMENT_UPLOAD_IN_PROGRESS",
            "Hay una carga en curso con ese contenido o para esa empresa, año y tipo. "
            "Espere a que termine.",
            details={"criterio": criterios},
        )
    return ConflictError(
        "DOCUMENT_CLEANUP_PENDING",
        "Un intento anterior falló y su limpieza sigue pendiente; podrá reintentar al completarse.",
        details={"criterio": criterios},
    )


async def _buscar_conflicto(
    db: AsyncSession, ambiente: str, sha256: str, empresa_id: uuid.UUID, anio: int, tipo
) -> ConflictError | None:
    consulta = (
        select(Documento, Usuario.correo)
        .join(Usuario, Usuario.id == Documento.usuario_id)
        .where(
            Documento.ambiente == ambiente,
            Documento.reserva_activa.is_(True),
            or_(
                Documento.sha256 == sha256,
                and_(Documento.empresa_id == empresa_id, Documento.anio == anio, Documento.tipo == tipo),
            ),
        )
    )
    filas = (await db.execute(consulta)).all()
    if not filas:
        return None
    documento, correo = min(filas, key=lambda f: (_PRIORIDAD[f[0].estado_procesamiento], f[0].creado_en))
    criterios = []
    if documento.sha256 == sha256:
        criterios.append("sha256")
    if (documento.empresa_id, documento.anio, documento.tipo) == (empresa_id, anio, tipo):
        criterios.append("empresa_anio_tipo")
    return _error_de_conflicto(documento, correo, criterios)


def _es_violacion_de_reserva(exc: IntegrityError) -> bool:
    causa = getattr(exc.orig, "__cause__", None)
    texto = f"{exc.orig} {getattr(causa, 'constraint_name', '')}"
    return any(n in texto for n in _RESTRICCIONES_DE_RESERVA) or any(
        c in texto for c in _COLUMNAS_DE_RESERVA_SQLITE
    )


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
) -> Documento:
    """Reserva SHA-256 y empresa/año/tipo para una carga y la deja `EN_PROCESO`.

    El sector se toma de la empresa (el declarado solo se contrasta). `usuario_id`
    debe provenir de la sesión validada, nunca del payload; aquí se comprueba además
    que sea un SuperAdmin habilitado. Una transacción corta, sin llamadas externas.
    """
    _validar_entrada(nombre_archivo, sha256, tamano_bytes, ambiente)
    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not usuario.habilitado or usuario.rol is not RolUsuario.SUPERADMIN:
        raise AuthorizationError(
            "INGESTION_FORBIDDEN", "Solo un SuperAdmin habilitado puede ingerir documentos."
        )
    empresa = await validacion.obtener_empresa_activa(db, metadatos.empresa_id, metadatos.sector)

    empresa_id, anio, tipo = empresa.id, metadatos.anio, metadatos.tipo
    conflicto = await _buscar_conflicto(db, ambiente, sha256, empresa_id, anio, tipo)
    if conflicto is not None:
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
    )
    db.add(documento)
    try:
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


# --- Fallo y compensación ------------------------------------------------------------

async def fallar_documento(
    db: AsyncSession,
    documento_id: uuid.UUID,
    motivo: str,
    *,
    requiere_compensacion: bool | None = None,
) -> Documento:
    """Marca `FALLIDO` un documento `EN_PROCESO`.

    `motivo` es un código (p. ej. `STORAGE_ERROR`), nunca texto libre. Con compensación
    pendiente la reserva se CONSERVA; solo se libera de inmediato si no hay nada
    externo que limpiar. `None` lo deduce: hay algo que limpiar si ya se registró un
    intento de subida. Con `False` explícito, un intento registrado es un conflicto.
    """
    if not isinstance(motivo, str) or _CODIGO_REGEX.fullmatch(motivo) is None:
        raise ValueError("El motivo del fallo debe ser un código en mayúsculas, no texto libre.")
    ahora = _ahora()
    comunes = {
        "estado_procesamiento": EstadoProcesamiento.FALLIDO,
        "motivo_fallo": motivo,
        "fallido_en": ahora,
    }
    en_proceso = Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO
    liberado = False
    if requiere_compensacion is not True:
        liberado = await _transicion(
            db,
            documento_id,
            [en_proceso, Documento.almacenamiento_intentado_en.is_(None)],
            {**comunes, "estado_compensacion": EstadoCompensacion.NINGUNA, "reserva_activa": False},
        )
    if not liberado:
        if requiere_compensacion is False:
            await db.rollback()
            raise _conflicto_de_estado()
        if not await _transicion(
            db, documento_id, [en_proceso], {**comunes, "estado_compensacion": EstadoCompensacion.PENDIENTE}
        ):
            await db.rollback()
            raise _conflicto_de_estado()
    await db.commit()
    documento = await _cargar(db, documento_id)
    await _terminar_lectura(db)
    return documento


def _codigo_seguro(exc: Exception) -> str:
    codigo = getattr(exc, "code", None) if isinstance(exc, ExternalServiceError) else None
    return codigo if isinstance(codigo, str) and _CODIGO_REGEX.fullmatch(codigo) else "UNEXPECTED_ERROR"


async def compensar_documento(
    db: AsyncSession, almacen: AlmacenOriginales, documento_id: uuid.UUID
) -> EstadoCompensacion:
    """Elimina el original propio de un intento fallido y libera su reserva.

    Idempotente y repetible. Solo actúa sobre documentos `FALLIDO` con compensación
    `PENDIENTE`: jamás sobre `EN_PROCESO` ni `COMPLETADO`. Si eliminar o comprobar la
    ausencia falla, deja la compensación `PENDIENTE` (con contador y código del último
    error) y conserva la reserva; no se informa limpieza. No relanza errores del almacén:
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
    referencia = ReferenciaOriginal(documento.clave_original, documento.version_id_original)
    await _terminar_lectura(db)  # sin transacción abierta durante la llamada externa

    pendiente = [
        Documento.estado_procesamiento == EstadoProcesamiento.FALLIDO,
        Documento.estado_compensacion == EstadoCompensacion.PENDIENTE,
    ]
    try:
        await almacen.eliminar(referencia)
        if await almacen.existe(referencia):
            raise ExternalServiceError(
                "STORAGE_CLEANUP_NOT_CONFIRMED", "No se pudo confirmar la eliminación del original."
            )
    except Exception as exc:
        await _transicion(
            db,
            documento_id,
            pendiente,
            {
                "compensacion_intentos": Documento.compensacion_intentos + 1,
                "ultimo_error_compensacion": _codigo_seguro(exc),
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
    db: AsyncSession, almacen: AlmacenOriginales, documento_id: uuid.UUID, motivo: str
) -> None:
    """Mejor esfuerzo tras un fallo de subida. Si la propia BD falla, el documento queda
    `EN_PROCESO` con el intento registrado y lo recupera `recuperar_documentos_pendientes`."""
    try:
        await fallar_documento(db, documento_id, motivo, requiere_compensacion=True)
        await compensar_documento(db, almacen, documento_id)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


# --- Almacenamiento del original ------------------------------------------------------

async def almacenar_original(
    db: AsyncSession, almacen: AlmacenOriginales, documento_id: uuid.UUID, contenido: bytes
) -> Documento:
    """Sube los bytes originales (BOM incluido) de un documento `EN_PROCESO`.

    El intento se registra ANTES de subir. Un fallo de la subida (o un timeout, que
    puede dejar el objeto creado) marca el documento `FALLIDO`, intenta compensar y
    relanza el error del almacén. Una CANCELACIÓN se propaga sin tocar la BD: el
    documento queda `EN_PROCESO` con el intento registrado para su recuperación.
    Solo se permite un intento por documento: nunca se sobrescribe su clave.
    """
    documento = await _cargar(db, documento_id)
    clave, sha256, tamano = documento.clave_original, documento.sha256, documento.tamano_bytes
    en_proceso = documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    ambiente = documento.ambiente
    await _terminar_lectura(db)
    if not en_proceso:
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

    if not await _transicion(
        db,
        documento_id,
        [
            Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
            Documento.almacenamiento_intentado_en.is_(None),
        ],
        {"almacenamiento_intentado_en": _ahora()},
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()

    try:
        referencia = await almacen.guardar(clave, bytes(contenido), sha256)
    except Exception as exc:
        await _fallar_y_compensar(db, almacen, documento_id, _codigo_seguro(exc))
        raise

    if not await _transicion(
        db,
        documento_id,
        [Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO],
        {"original_almacenado_en": _ahora(), "version_id_original": referencia.version_id},
    ):
        await db.rollback()
        raise _conflicto_de_estado()  # el objeto queda para la compensación/recuperación
    await db.commit()
    documento = await _cargar(db, documento_id)
    await _terminar_lectura(db)
    return documento


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


# --- Recuperación ---------------------------------------------------------------------

@dataclass(frozen=True)
class ResumenRecuperacion:
    abandonados: int  # reservas EN_PROCESO marcadas como fallidas
    compensados: int  # compensaciones que llegaron a COMPLETADA
    pendientes: int  # compensaciones que siguen PENDIENTE


async def recuperar_documentos_pendientes(
    db: AsyncSession,
    almacen: AlmacenOriginales,
    *,
    abandonados_antes_de: datetime,
    limite: int,
) -> ResumenRecuperacion:
    """Marca como fallidas las reservas `EN_PROCESO` anteriores a `abandonados_antes_de`
    y reintenta las compensaciones `PENDIENTE`, solo del ambiente del almacén.

    El umbral lo fija quien llama y debe superar el tiempo máximo de una ingesta: sin
    latido de vida no se distingue una ingesta lenta de una caída. Se procesa de forma
    secuencial, con transacciones cortas.
    """
    if not isinstance(abandonados_antes_de, datetime) or abandonados_antes_de.tzinfo is None:
        raise ValueError("abandonados_antes_de debe ser un datetime con zona horaria.")
    if isinstance(limite, bool) or not isinstance(limite, int) or limite < 1:
        raise ValueError("limite debe ser un entero de al menos 1.")

    ids_abandonados = (
        await db.execute(
            select(Documento.id)
            .where(
                Documento.ambiente == almacen.ambiente,
                Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
                Documento.creado_en < abandonados_antes_de,
            )
            .order_by(Documento.creado_en)
            .limit(limite)
        )
    ).scalars().all()
    await _terminar_lectura(db)
    abandonados = 0
    for documento_id in ids_abandonados:
        try:
            await fallar_documento(db, documento_id, "RESERVA_ABANDONADA")
        except ConflictError:
            continue  # otro proceso lo resolvió entre la lectura y la marca
        abandonados += 1

    ids_pendientes = (
        await db.execute(
            select(Documento.id)
            .where(
                Documento.ambiente == almacen.ambiente,
                Documento.estado_procesamiento == EstadoProcesamiento.FALLIDO,
                Documento.estado_compensacion == EstadoCompensacion.PENDIENTE,
            )
            .order_by(Documento.fallido_en)
            .limit(limite)
        )
    ).scalars().all()
    await _terminar_lectura(db)
    compensados = pendientes = 0
    for documento_id in ids_pendientes:
        try:
            resultado = await compensar_documento(db, almacen, documento_id)
        except ConflictError:
            continue
        if resultado is EstadoCompensacion.COMPLETADA:
            compensados += 1
        else:
            pendientes += 1
    return ResumenRecuperacion(abandonados, compensados, pendientes)


# --- Compuerta de publicación (no publica nada en 4A) --------------------------------------

async def publicar_documento(
    db: AsyncSession,
    documento_id: uuid.UUID,
    *,
    indexacion_confirmada: bool,
    resultado_analisis: ResultadoAnalisis | None,
) -> Documento:
    """COMPUERTA de la futura publicación. En 4A nada la invoca.

    Exige original almacenado, indexación vectorial confirmada y un análisis ejecutado
    (`resultado_analisis`; OBSERVADO solo si el detector corrió bien sin hallazgos), y
    REVALIDA la empresa (activa y con el mismo sector) antes de pasar a `COMPLETADO`.
    Quien llama es responsable de que las confirmaciones sean verdaderas: este módulo no
    puede verificar vectores ni análisis que todavía no existen. Si algo falta o la
    empresa dejó de ser válida, no se modifica nada.
    """
    documento = await _cargar(db, documento_id, bloquear=True)
    faltantes = []
    if documento.estado_procesamiento is not EstadoProcesamiento.EN_PROCESO:
        faltantes.append("documento_en_proceso")
    if documento.original_almacenado_en is None:
        faltantes.append("original_almacenado")
    if indexacion_confirmada is not True:
        faltantes.append("indexacion_confirmada")
    if not isinstance(resultado_analisis, ResultadoAnalisis):
        faltantes.append("resultado_analisis")
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
            Documento.original_almacenado_en.is_not(None),
        ],
        {
            "estado_procesamiento": EstadoProcesamiento.COMPLETADO,
            "resultado_analisis": resultado_analisis,
            "completado_en": _ahora(),
        },
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()
    documento = await _cargar(db, documento_id)
    await _terminar_lectura(db)
    return documento

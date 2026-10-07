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
- PROPIEDAD DE LA OPERACIÓN. `reservar_documento` entrega un token de ejecución y una
  vigencia. Toda transición del ejecutor (registrar el intento, registrar el original,
  fallar, publicar) exige el token; el ejecutor renueva la vigencia con `renovar_vigencia`.
  La recuperación solo toma operaciones con la vigencia VENCIDA (la antigüedad no cuenta) y
  ROTA el token, de modo que el ejecutor anterior ya no puede publicar.
- LIMPIEZA CON INCERTIDUMBRE. Una subida pudo terminar después del timeout o la
  cancelación. La compensación consulta el desenlace real (`estado_subida`), elimina la
  versión concreta si se conoce, o reconcilia listando las versiones de la clave exacta.
  Nunca declara limpio por «DELETE + HEAD 404», ni mientras la subida siga en curso, ni
  cuando la ausencia no prueba que el objeto no se creó (queda PENDIENTE y requiere
  intervención documentada en `docs/ingesta/07-almacenamiento-minio.md`).
- Este módulo NO publica nada: no existe coordinación con vectores ni detector.
  `publicar_documento` es solo una compuerta que exige lo que aún no existe.

Cada función recibe una `AsyncSession` y la usa de forma secuencial (nunca se
comparte entre tareas concurrentes). No registra auditoría (Etapa 6).
"""
import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
from app.models import Empresa, RolUsuario, Usuario
from app.models.documento_ingesta import (
    VIGENCIA_PREDETERMINADA,
    Documento,
    EstadoCompensacion,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta import validacion
from app.services.ingesta.almacenamiento import (
    AlmacenOriginales,
    EstadoSubida,
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


def _exigir_token(token: object) -> uuid.UUID:
    if not isinstance(token, uuid.UUID):
        raise TypeError("token debe ser el UUID de ejecución entregado al reservar.")
    return token


def _exigir_instante(valor: object) -> datetime:
    if not isinstance(valor, datetime) or valor.tzinfo is None:
        raise ValueError("Se requiere un datetime con zona horaria.")
    return valor


def _exigir_duracion(duracion: object) -> timedelta:
    if not isinstance(duracion, timedelta) or duracion <= timedelta(0):
        raise ValueError("La vigencia debe ser un timedelta positivo.")
    return duracion


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
        ejecucion_token=uuid.uuid4(),
        ejecucion_vigente_hasta=_ahora() + VIGENCIA_PREDETERMINADA,
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
    token: uuid.UUID | None = None,
    vencido_antes_de: datetime | None = None,
) -> Documento:
    """Marca `FALLIDO` un documento `EN_PROCESO` y ROTA su token de ejecución.

    `motivo` es un código (p. ej. `STORAGE_ERROR`), nunca texto libre. Con compensación
    pendiente la reserva se CONSERVA; solo se libera de inmediato si no hay nada
    externo que limpiar. `None` lo deduce: hay algo que limpiar si ya se registró un
    intento de subida. Con `False` explícito, un intento registrado es un conflicto.

    Quien llama debe demostrar su derecho: el ejecutor, con su `token`; la recuperación, con
    `vencido_antes_de` (solo actúa si la vigencia ya venció en ese instante). Exactamente uno.
    """
    if not isinstance(motivo, str) or _CODIGO_REGEX.fullmatch(motivo) is None:
        raise ValueError("El motivo del fallo debe ser un código en mayúsculas, no texto libre.")
    if (token is None) == (vencido_antes_de is None):
        raise ValueError("Indique exactamente uno: el token del ejecutor o `vencido_antes_de`.")
    ahora = _ahora()
    comunes = {
        "estado_procesamiento": EstadoProcesamiento.FALLIDO,
        "motivo_fallo": motivo,
        "fallido_en": ahora,
        "ejecucion_token": uuid.uuid4(),  # el ejecutor anterior ya no puede publicar
    }
    propiedad = [Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO]
    if token is not None:
        propiedad.append(Documento.ejecucion_token == _exigir_token(token))
    else:
        propiedad.append(Documento.ejecucion_vigente_hasta < _exigir_instante(vencido_antes_de))
    liberado = False
    if requiere_compensacion is not True:
        liberado = await _transicion(
            db,
            documento_id,
            [*propiedad, Documento.almacenamiento_intentado_en.is_(None)],
            {**comunes, "estado_compensacion": EstadoCompensacion.NINGUNA, "reserva_activa": False},
        )
    if not liberado:
        if requiere_compensacion is False:
            await db.rollback()
            raise _conflicto_de_estado()
        if not await _transicion(
            db, documento_id, propiedad, {**comunes, "estado_compensacion": EstadoCompensacion.PENDIENTE}
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
    subida = await almacen.estado_subida(clave)
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


async def compensar_documento(
    db: AsyncSession, almacen: AlmacenOriginales, documento_id: uuid.UUID
) -> EstadoCompensacion:
    """Elimina el original propio de un intento fallido y libera su reserva.

    SOLO limpia el almacén de originales (MinIO): no toca la base vectorial. El coordinador y la
    recuperación deberán confirmar además `fragmentos_vectoriales.eliminar_fragmentos` +
    `contar_fragmentos == 0` antes de liberar la reserva de un documento que haya llegado a indexarse.

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
    await _terminar_lectura(db)  # sin transacción abierta durante la llamada externa

    pendiente = [
        Documento.estado_procesamiento == EstadoProcesamiento.FALLIDO,
        Documento.estado_compensacion == EstadoCompensacion.PENDIENTE,
    ]
    try:
        await _reconciliar_y_eliminar(db, almacen, documento_id, clave, tamano, version_bd, existencia_conocida)
    except Exception as exc:
        await db.rollback()
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
        await _fallar_y_compensar(db, almacen, documento_id, _codigo_seguro(exc), token)
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
    limite: int,
    ahora: datetime | None = None,
) -> ResumenRecuperacion:
    """Marca como fallidas las operaciones `EN_PROCESO` cuya VIGENCIA venció y reintenta las
    compensaciones `PENDIENTE`, solo del ambiente del almacén.

    La antigüedad de la reserva NO es criterio: un ejecutor vivo renueva su vigencia
    (`renovar_vigencia`) y una operación lenta no se recupera. Al recuperar se rota el
    token: el ejecutor anterior ya no puede registrar el original ni publicar. Limitación:
    la vigencia se compara con la hora de quien recupera; entre instancias con relojes
    desfasados hace falta margen. Un ejecutor vivo que no renovó a tiempo pierde la operación
    (falla); nunca se publica dos veces. Procesa de forma secuencial, con transacciones cortas.
    """
    ahora = _ahora() if ahora is None else _exigir_instante(ahora)
    if isinstance(limite, bool) or not isinstance(limite, int) or limite < 1:
        raise ValueError("limite debe ser un entero de al menos 1.")

    ids_abandonados = (
        await db.execute(
            select(Documento.id)
            .where(
                Documento.ambiente == almacen.ambiente,
                Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
                Documento.ejecucion_vigente_hasta < ahora,
            )
            .order_by(Documento.ejecucion_vigente_hasta)
            .limit(limite)
        )
    ).scalars().all()
    await _terminar_lectura(db)
    abandonados = 0
    for documento_id in ids_abandonados:
        try:
            await fallar_documento(db, documento_id, "RESERVA_ABANDONADA", vencido_antes_de=ahora)
        except ConflictError:
            continue  # otro proceso lo resolvió, o su ejecutor renovó entre la lectura y la marca
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
    token: uuid.UUID,
    indexacion_confirmada: bool,
    resultado_analisis: ResultadoAnalisis | None,
) -> Documento:
    """COMPUERTA de la futura publicación. En 4A nada la invoca.

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
        },
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()
    documento = await _cargar(db, documento_id)
    await _terminar_lectura(db)
    return documento


# --- Recuperabilidad (condición transaccional de la Etapa 4B) -------------------------------

async def documentos_recuperables(
    db: AsyncSession, *, ambiente: str, documento_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """De los `documento_ids` dados, los que SÍ pueden aparecer en una recuperación: documento
    `COMPLETADO` con análisis ejecutado (`resultado_analisis` no nulo; incluye OBSERVADO) de una empresa
    ACTIVA, en ese ambiente.

    Es la mitad transaccional del contrato de visibilidad: un fragmento está `publicado` en la base
    vectorial, pero esa marca NO sustituye esta comprobación (las bases no comparten transacción). Quien
    recupere debe descartar los fragmentos cuyo `documento_id` no esté en el resultado.
    """
    validar_ambiente(ambiente)
    ids = [i for i in documento_ids]
    if not all(isinstance(i, uuid.UUID) for i in ids):
        raise ValueError("documento_ids debe contener solo UUID.")
    recuperables: set[uuid.UUID] = set()
    for inicio in range(0, len(ids), 500):
        consulta = (
            select(Documento.id)
            .join(Empresa, Empresa.id == Documento.empresa_id)
            .where(
                Documento.ambiente == ambiente,
                Documento.id.in_(ids[inicio:inicio + 500]),
                Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
                Documento.resultado_analisis.is_not(None),
                Empresa.activa.is_(True),
            )
        )
        recuperables.update((await db.execute(consulta)).scalars().all())
    await _terminar_lectura(db)
    return recuperables

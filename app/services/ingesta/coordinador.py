"""Coordinador SÍNCRONO de ingesta de documentos (2026-10-07).

Conecta, sin duplicar, las piezas existentes: `validacion` (archivo y empresa), `documento_service`
(reserva con controles de duplicados, original en MinIO, estado duradero, compensación y
recuperación), `fragmentacion`, `embeddings` (lotes), `fragmentos_vectoriales` (pgvector),
`analisis_ingesta` (GRI + sanciones) y la auditoría existente. No introduce colas, workers, Celery,
Redis ni otro proveedor: es una corrutina que el endpoint espera.

SECUENCIA (`ingerir_documento`)
 1. Validar archivo (formato, UTF-8, tamaño real…) y comprobar empresa activa. Un rechazo se audita
    (`RECHAZO_DOCUMENTO`) y no deja nada: no hay reserva, original ni fragmentos.
 2. Reservar el documento (índices únicos parciales; duplicados concurrentes → 409). Desde aquí hay token de
    ejecución y vigencia; la vigencia se ajusta a `ConfigCoordinador.vigencia`.
 3. Guardar el original en MinIO (`almacenar_original`).
 4. Fragmentar y generar embeddings por lotes; registrar DURABLEMENTE el intento de escritura vectorial;
    guardar cada lote como NO publicado.
 5. Ejecutar `analizar_documento` DESPUÉS de la indexación (hilo aparte: es CPU).
 6. Persistir el análisis completo.
 7. Finalizar en la base transaccional (`publicar_documento`): revalida empresa activa y propiedad de la
    ejecución y deja `COMPLETADO` + `resultado_analisis` (+ auditoría de éxito en la MISMA transacción).
 8. Publicar los fragmentos en la base vectorial y confirmar el conteo.
 9. Informar éxito SOLO si todo lo anterior está confirmado.
OBSERVADO es una ingesta válida (original, fragmentos y análisis; disponible para RAG; no habilita por sí solo
reportes). Un error del detector NUNCA se convierte en OBSERVADO (`INGESTION_ANALYSIS_FAILED` ⇒ intento fallido).
No calcula ESG ni asigna estados de cumplimiento.

PROPIEDAD, VIGENCIA Y LATIDO. El ejecutor posee la operación mientras presente su `token`. La vigencia NO es una
duración máxima: un latido (tarea con SU PROPIA sesión, nunca compartida) la renueva cada `intervalo_latido`. Si
el latido pierde la propiedad (otro la tomó, o no pudo renovar durante una vigencia entera) el trabajo se
detiene en el siguiente punto de control y se lanza `INGESTION_OWNERSHIP_LOST` SIN tocar el estado: la
recuperación es dueña. Una CANCELACIÓN (`CancelledError`) se propaga sin tocar la BD, igual que en 4A: el
documento queda `EN_PROCESO` y la recuperación lo toma al vencer su vigencia.

FALLOS ANTES DE COMPLETAR. Se marca `FALLIDO` (rotando el token, retirando el análisis parcial) y se COMPENSA de
forma conjunta (`compensar_documento`): base vectorial (con el cierre del documento que impide escrituras
tardías) y original en MinIO; la reserva se libera SOLO tras confirmar toda la limpieza. Si algo queda incierto la
compensación queda PENDIENTE con la reserva activa y la reintenta la recuperación (`ejecutar_recuperacion`).

PUBLICACIÓN PENDIENTE. Si el documento ya está `COMPLETADO` y falla la publicación vectorial, se lanza
`PublicacionVectorialPendiente` (nunca éxito): el documento sigue `COMPLETADO` con `vector_publicado_en` nulo, es
recuperable y `reintentar_publicacion` / la recuperación repiten la publicación (idempotente) SIN volver a generar
embeddings ni a analizar.

NO HAY ATOMICIDAD entre las dos bases ni MinIO. La consistencia viene de estados duraderos, orden de pasos,
cierre del documento y compensación repetible; nunca de dos commits seguidos.

AUDITORÍA. Rechazo y fallo: transacción propia de mejor esfuerzo (el estado es lo prioritario); éxito: en la
misma transacción que `COMPLETADO`. Solo identificadores y códigos: jamás contenido, credenciales ni respuestas
de proveedores.

LÍMITES CONOCIDOS. La fragmentación se consume en el hilo del bucle entre llamadas al proveedor (el conteo previo va
a un hilo); el rendimiento con 50 MB no está medido (T27). `tamano_lote` y los plazos son provisionales.
"""
import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    AppException,
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    PayloadTooLargeError,
)
from app.models import TipoEventoAuditoria
from app.models.documento_ingesta import VIGENCIA_PREDETERMINADA, EtapaIngesta, ResultadoAnalisis
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta import documento_service as servicio
from app.services.ingesta import fragmentos_vectoriales as vectores
from app.services.ingesta import validacion
from app.services.ingesta.almacenamiento import AlmacenOriginales, validar_ambiente
from app.services.ingesta.analisis_ingesta import CATEGORIA_CALIDAD, AdvertenciaAnalisis, analizar_documento
from app.services.ingesta.embeddings import ProveedorEmbeddings, embeber_fragmentos
from app.services.ingesta.fragmentacion import ParametrosFragmentacion, iterar_fragmentos

_CODIGO_REGEX = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_RECHAZOS_DEL_CLIENTE = (BusinessValidationError, PayloadTooLargeError, ConflictError, AuthorizationError, NotFoundError)

# Fábrica de sesiones: `async_sessionmaker` (o cualquier callable que devuelva un gestor de contexto
# asíncrono de `AsyncSession`). Cada tarea abre las suyas: una sesión nunca se comparte entre tareas.
FabricaSesiones = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class DependenciasIngesta:
    sesiones: FabricaSesiones  # base transaccional
    sesiones_vectoriales: FabricaSesiones  # base vectorial (pgvector)
    almacen: AlmacenOriginales  # MinIO
    embeddings: ProveedorEmbeddings  # OCI en producción; un doble en pruebas


@dataclass(frozen=True)
class ConfigCoordinador:
    """Parámetros PROVISIONALES: el tamaño de lote y los plazos dependen del proveedor real (D12, D23)."""

    tamano_lote: int = 16
    timeout_embeddings_segundos: float = 120.0
    fragmentacion: ParametrosFragmentacion = field(default_factory=ParametrosFragmentacion)
    # Vigencia RENOVABLE de la operación (no una duración máxima) y cada cuánto la renueva el latido.
    vigencia: timedelta = VIGENCIA_PREDETERMINADA
    intervalo_latido: timedelta | None = None  # por defecto, un tercio de la vigencia

    def __post_init__(self) -> None:
        if isinstance(self.tamano_lote, bool) or not isinstance(self.tamano_lote, int) or self.tamano_lote < 1:
            raise ValueError("tamano_lote debe ser un entero de al menos 1.")
        if not isinstance(self.vigencia, timedelta) or self.vigencia <= timedelta(0):
            raise ValueError("vigencia debe ser un timedelta positivo.")
        if self.intervalo_latido is not None and (
            not isinstance(self.intervalo_latido, timedelta)
            or self.intervalo_latido <= timedelta(0)
            or self.intervalo_latido >= self.vigencia
        ):
            raise ValueError("intervalo_latido debe ser positivo y menor que la vigencia.")

    @property
    def intervalo(self) -> timedelta:
        return self.intervalo_latido or self.vigencia / 3


@dataclass(frozen=True)
class ResultadoIngesta:
    """Ingesta COMPLETADA y PUBLICADA. Solo se construye con todo confirmado."""

    documento_id: uuid.UUID
    resultado_analisis: ResultadoAnalisis
    motivos: tuple[str, ...]
    fragmentos: int
    advertencias: tuple[dict, ...]
    progreso: servicio.ProgresoIngesta


class PublicacionVectorialPendiente(ExternalServiceError):
    """El documento quedó COMPLETADO pero sus fragmentos aún no están publicados: NO es un éxito. Recuperable:
    `reintentar_publicacion` repite la publicación sin regenerar embeddings ni reanalizar."""

    def __init__(self, documento_id: uuid.UUID, causa: str) -> None:
        super().__init__(
            "VECTOR_PUBLICATION_PENDING",
            "El documento se completó pero su publicación en la base vectorial quedó pendiente.",
            details={"documento_id": str(documento_id), "causa": causa},
        )
        self.documento_id = documento_id


class _PropiedadPerdida(Exception):
    """Interna: este ejecutor ya no posee la operación."""


def _propiedad_perdida() -> ConflictError:
    return ConflictError(
        "INGESTION_OWNERSHIP_LOST",
        "La ingesta perdió la propiedad de la operación y se detuvo; la recuperación decidirá su desenlace.",
    )


def _codigo(exc: BaseException) -> str:
    """Código estable y seguro del fallo. Solo los `AppException` propios (códigos constantes); cualquier
    otra excepción (de terceros) es `UNEXPECTED_ERROR` y nunca se repite su mensaje."""
    codigo = getattr(exc, "code", None) if isinstance(exc, AppException) else None
    return codigo if isinstance(codigo, str) and _CODIGO_REGEX.fullmatch(codigo) else "UNEXPECTED_ERROR"


# --- Latido ---------------------------------------------------------------------------------------

class _Latido:
    """Renueva la vigencia en una tarea aparte con SU PROPIA sesión por renovación. No cancela al ejecutor:
    marca `perdida` y el ejecutor lo comprueba en cada punto de control (`exigir`)."""

    def __init__(
        self, sesiones: FabricaSesiones, documento_id: uuid.UUID, token: uuid.UUID, vigencia: timedelta, intervalo: timedelta
    ) -> None:
        self._sesiones, self._id, self._token = sesiones, documento_id, token
        self._vigencia, self._intervalo = vigencia, intervalo
        self.perdida = False
        self.renovaciones = 0
        self._tarea: asyncio.Task | None = None

    async def __aenter__(self) -> "_Latido":
        self._tarea = asyncio.create_task(self._bucle(), name="latido-ingesta")
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._tarea is not None:
            self._tarea.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tarea

    def exigir(self) -> None:
        if self.perdida:
            raise _PropiedadPerdida

    async def _bucle(self) -> None:
        ultima_renovacion = time.monotonic()
        while True:
            await asyncio.sleep(self._intervalo.total_seconds())
            try:
                async with self._sesiones() as sesion:
                    await servicio.renovar_vigencia(sesion, self._id, token=self._token, duracion=self._vigencia)
                ultima_renovacion = time.monotonic()
                self.renovaciones += 1
            except ConflictError:
                self.perdida = True  # la recuperaron o terminó: se debe detener
                return
            except Exception:
                # Fallo transitorio de la base: se reintenta en el próximo latido. Si pasa una vigencia entera
                # sin renovar, la propiedad ya no se puede demostrar.
                if time.monotonic() - ultima_renovacion >= self._vigencia.total_seconds():
                    self.perdida = True
                    return


# --- Piezas auxiliares ---------------------------------------------------------------------------------

def _contar_fragmentos(texto: str, parametros: ParametrosFragmentacion) -> int:
    return sum(1 for _ in iterar_fragmentos(texto, parametros))


def _analizar(documento: validacion.DocumentoValidado):
    resultado = analizar_documento(documento)
    return resultado, resultado.a_dict()


def _advertencias_de_calidad(documento: validacion.DocumentoValidado) -> list[dict]:
    return [
        asdict(AdvertenciaAnalisis(a.codigo, CATEGORIA_CALIDAD, a.mensaje, a.detalles))
        for a in documento.advertencias
    ]


def _detalle_rechazo(exc: BaseException, metadatos: MetadatosIngestaRequest, sha256: str | None) -> str:
    partes = [f"Ingesta rechazada; codigo={_codigo(exc)}", f"empresa_id={metadatos.empresa_id}", f"anio={metadatos.anio}"]
    partes.append(f"tipo={getattr(metadatos.tipo, 'value', metadatos.tipo)}")
    if sha256:
        partes.append(f"sha256={sha256}")
    return "; ".join(partes)


async def _guardar_lote(
    vectorial: AsyncSession, documento: servicio.Documento, lote
) -> None:
    try:
        if vectorial.in_transaction():
            await vectorial.rollback()
        await vectores.guardar_lote(
            vectorial,
            ambiente=documento.ambiente,
            documento_id=documento.id,
            empresa_id=documento.empresa_id,
            anio=documento.anio,
            tipo=documento.tipo,
            sector=documento.sector,
            lote=lote,
        )
    except AppException:
        raise  # FRAGMENTS_DOCUMENT_CLOSED / FRAGMENTS_ALREADY_PERSISTED: códigos propios y seguros
    except Exception:
        raise ExternalServiceError(
            "VECTOR_STORE_ERROR", "No se pudo guardar un lote de fragmentos en la base vectorial."
        ) from None


async def _fallar_y_compensar(
    db: AsyncSession,
    vectorial: AsyncSession,
    almacen: AlmacenOriginales,
    documento_id: uuid.UUID,
    token: uuid.UUID,
    motivo: str,
) -> None:
    """Marca el intento FALLIDO y lo compensa de forma conjunta. Mejor esfuerzo: si la BD falla, el documento
    queda EN_PROCESO (o FALLIDO/PENDIENTE) y lo recupera `ejecutar_recuperacion` sin dar nada por limpio.
    Tolera que el fallo ya esté marcado (p. ej. por `almacenar_original`)."""
    try:
        await db.rollback()
        try:
            await servicio.fallar_documento(db, documento_id, motivo, token=token)
        except ConflictError:
            pass  # ya estaba marcado FALLIDO (o no es nuestro): la compensación es idempotente
        await servicio.compensar_documento(db, almacen, documento_id, vectorial=vectorial)
    except ConflictError:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await db.rollback()


# --- Ingesta ---------------------------------------------------------------------------------------------

async def ingerir_documento(
    dependencias: DependenciasIngesta,
    config: ConfigCoordinador,
    *,
    usuario_id: uuid.UUID,
    nombre_archivo: str | None,
    leer: validacion.LectorBytes,
    metadatos: MetadatosIngestaRequest,
    operacion_id: uuid.UUID | None = None,
) -> ResultadoIngesta:
    """Ingesta síncrona de UN documento. Devuelve `ResultadoIngesta` solo con el documento COMPLETADO y sus
    fragmentos PUBLICADOS. Errores: los de validación/reserva (400/409/413), `ExternalServiceError` de MinIO,
    embeddings o la base vectorial (502/504), `AnalisisIngestaError`, `INGESTION_OWNERSHIP_LOST` (409) y
    `PublicacionVectorialPendiente` (documento COMPLETADO sin publicar: recuperable).

    `usuario_id` debe venir de la sesión/JWT validado, nunca del payload. `leer` es el lector asíncrono del
    archivo (p. ej. `UploadFile.read`). El tamaño real se cuenta durante la lectura (50 000 000 bytes).

    `operacion_id` (opcional): operación de ingesta `EN_CARGA` que se enlaza al documento en la misma transacción
    de la reserva (ver `reservar_documento`). Quien la pasa es responsable de marcarla rechazada si esta función
    lanza antes de reservar (`operacion_service.marcar_rechazada`).
    """
    ambiente = validar_ambiente(dependencias.almacen.ambiente)
    identidad = dependencias.embeddings.identidad
    async with dependencias.sesiones() as db:
        # Validación previa y rechazos --------------------------------------------------------
        documento_validado = None
        try:
            if identidad.dimension != vectores.DIMENSION_VECTORIAL:
                raise ExternalServiceError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    "La dimensión del proveedor de embeddings no coincide con la de la base vectorial.",
                )
            documento_validado = await validacion.validar_archivo(nombre_archivo, leer)
            await validacion.obtener_empresa_activa(db, metadatos.empresa_id, metadatos.sector)
            await db.rollback()
            documento = await servicio.reservar_documento(
                db,
                metadatos=metadatos,
                nombre_archivo=documento_validado.nombre_archivo,
                sha256=documento_validado.sha256,
                tamano_bytes=documento_validado.tamano_bytes,
                usuario_id=usuario_id,
                ambiente=ambiente,
                operacion_id=operacion_id,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await db.rollback()
            sha = documento_validado.sha256 if documento_validado is not None else None
            if isinstance(exc, _RECHAZOS_DEL_CLIENTE):
                await servicio.auditar_aparte(
                    db, TipoEventoAuditoria.RECHAZO_DOCUMENTO, _detalle_rechazo(exc, metadatos, sha), usuario_id
                )
            else:
                await servicio.auditar_aparte(
                    db,
                    TipoEventoAuditoria.INGESTA_DATOS,
                    f"Ingesta no iniciada; codigo={_codigo(exc)}; empresa_id={metadatos.empresa_id}",
                    usuario_id,
                )
            raise

        documento_id, token = documento.id, documento.ejecucion_token
        async with dependencias.sesiones_vectoriales() as vectorial:
            try:
                async with _Latido(
                    dependencias.sesiones, documento_id, token, config.vigencia, config.intervalo
                ) as latido:
                    resultado, fragmentos = await _ejecutar_etapas(
                        dependencias, config, db, vectorial, latido, documento, documento_validado
                    )
            except asyncio.CancelledError:
                raise  # sin tocar la BD: el documento queda EN_PROCESO y la recuperación lo toma al vencer
            except _PropiedadPerdida:
                raise _propiedad_perdida() from None
            except ConflictError as exc:
                if exc.code in ("DOCUMENT_STATE_CONFLICT", "FRAGMENTS_DOCUMENT_CLOSED"):
                    # La recuperación tomó la operación (token rotado) o ya cerró el documento: no es nuestro.
                    raise _propiedad_perdida() from None
                await _fallar_y_compensar(db, vectorial, dependencias.almacen, documento_id, token, _codigo(exc))
                raise
            except Exception as exc:
                await _fallar_y_compensar(db, vectorial, dependencias.almacen, documento_id, token, _codigo(exc))
                raise

            # Publicación en la base vectorial (el documento ya está COMPLETADO) --------------------
            try:
                await servicio.publicar_en_base_vectorial(db, vectorial, documento_id)
            except ExternalServiceError as exc:
                raise PublicacionVectorialPendiente(documento_id, _codigo(exc)) from None

        progreso = await servicio.obtener_progreso(db, documento_id)
        return ResultadoIngesta(
            documento_id=documento_id,
            resultado_analisis=resultado.resultado,
            motivos=resultado.motivos,
            fragmentos=fragmentos,
            advertencias=progreso.advertencias,
            progreso=progreso,
        )


async def _ejecutar_etapas(
    dependencias: DependenciasIngesta,
    config: ConfigCoordinador,
    db: AsyncSession,
    vectorial: AsyncSession,
    latido: _Latido,
    documento,
    documento_validado: validacion.DocumentoValidado,
):
    """Etapas 3–7 con la operación en poder del ejecutor. Cada paso comprueba la propiedad antes de actuar."""
    documento_id, token = documento.id, documento.ejecucion_token

    # La vigencia pasa a ser la configurada; desde aquí la mantiene el latido.
    await servicio.renovar_vigencia(db, documento_id, token=token, duracion=config.vigencia)

    # 3. Original en MinIO ---------------------------------------------------------------------
    latido.exigir()
    await servicio.registrar_progreso(
        db,
        documento_id,
        token=token,
        etapa=EtapaIngesta.ALMACENANDO_ORIGINAL,
        advertencias=_advertencias_de_calidad(documento_validado),
    )
    await servicio.almacenar_original(
        db, dependencias.almacen, documento_id, documento_validado.contenido,
        token=token, duracion_vigencia=config.vigencia,
    )

    # 4. Fragmentos y embeddings por lotes (no publicados) ------------------------------------------
    latido.exigir()
    texto = documento_validado.texto
    total = await asyncio.to_thread(_contar_fragmentos, texto, config.fragmentacion)
    await servicio.registrar_progreso(
        db, documento_id, token=token, etapa=EtapaIngesta.INDEXANDO, fragmentos_procesados=0, fragmentos_total=total
    )
    await servicio.registrar_intento_vectorial(db, documento_id, token=token)
    procesados = 0
    lotes = embeber_fragmentos(
        iterar_fragmentos(texto, config.fragmentacion),
        dependencias.embeddings,
        tamano_lote=config.tamano_lote,
        timeout_segundos=config.timeout_embeddings_segundos,
    )
    async with contextlib.aclosing(lotes):
        async for lote in lotes:
            latido.exigir()
            await _guardar_lote(vectorial, documento, lote)
            procesados += len(lote.elementos)
            await servicio.registrar_progreso(db, documento_id, token=token, fragmentos_procesados=procesados)
    latido.exigir()
    conteo = await _contar(vectorial, documento)
    if procesados != total or conteo.total != total or conteo.publicados != 0:
        raise ExternalServiceError(
            "VECTOR_INDEX_NOT_CONFIRMED", "No se pudo confirmar que todos los fragmentos estén guardados sin publicar."
        )

    # 5. Análisis tras la indexación (un error del detector NUNCA es OBSERVADO) ------------------------------
    await servicio.registrar_progreso(db, documento_id, token=token, etapa=EtapaIngesta.ANALIZANDO)
    resultado, analisis = await asyncio.to_thread(_analizar, documento_validado)

    # 6. Persistir el análisis completo ---------------------------------------------------------------------
    latido.exigir()
    await servicio.persistir_analisis(db, documento_id, token=token, analisis=analisis)

    # 7. Finalizar en la base transaccional: revalida empresa activa y propiedad ---------------------------------
    latido.exigir()
    await servicio.registrar_progreso(db, documento_id, token=token, etapa=EtapaIngesta.COMPLETANDO)
    await servicio.publicar_documento(
        db,
        documento_id,
        token=token,
        indexacion_confirmada=True,
        resultado_analisis=resultado.resultado,
        exigir_analisis_persistido=True,
    )
    return resultado, total


async def _contar(vectorial: AsyncSession, documento) -> vectores.ConteoFragmentos:
    try:
        if vectorial.in_transaction():
            await vectorial.rollback()
        return await vectores.contar_fragmentos(vectorial, ambiente=documento.ambiente, documento_id=documento.id)
    except Exception:
        raise ExternalServiceError(
            "VECTOR_STORE_ERROR", "No se pudo consultar la base vectorial."
        ) from None


# --- Recuperación y reintento de publicación -----------------------------------------------------------------------

async def reintentar_publicacion(dependencias: DependenciasIngesta, documento_id: uuid.UUID) -> bool:
    """Repite la publicación vectorial de un documento COMPLETADO cuya publicación quedó pendiente. Idempotente;
    no regenera embeddings ni reanaliza. `True` si esta llamada la registró; `False` si ya estaba publicada.
    Si vuelve a fallar lanza `PublicacionVectorialPendiente`."""
    ambiente = validar_ambiente(dependencias.almacen.ambiente)
    async with dependencias.sesiones() as db, dependencias.sesiones_vectoriales() as vectorial:
        # Un documento de otro ambiente no existe para esta aplicación: 404 sin leerlo ni modificarlo.
        await servicio.exigir_documento_del_ambiente(db, documento_id, ambiente)
        try:
            return await servicio.publicar_en_base_vectorial(db, vectorial, documento_id)
        except ExternalServiceError as exc:
            raise PublicacionVectorialPendiente(documento_id, _codigo(exc)) from None


async def ejecutar_recuperacion(
    dependencias: DependenciasIngesta, *, limite: int = 50, ahora: datetime | None = None
) -> servicio.ResumenRecuperacion:
    """Un barrido de recuperación del ambiente del almacén: abandona las operaciones con vigencia vencida,
    reintenta las compensaciones CONJUNTAS pendientes (base vectorial + MinIO) y repite las publicaciones
    vectoriales pendientes. Secuencial, con transacciones cortas. Quien la programe (cron/arranque) decide la
    periodicidad: este módulo no crea tareas en segundo plano."""
    async with dependencias.sesiones() as db, dependencias.sesiones_vectoriales() as vectorial:
        return await servicio.recuperar_documentos_pendientes(
            db, dependencias.almacen, limite=limite, ahora=ahora, vectorial=vectorial
        )

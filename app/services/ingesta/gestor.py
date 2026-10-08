"""Gestor de la ingesta por HTTP: recursos compartidos, ejecución desacoplada de la petición y recuperación periódica.

Es el único lugar que conoce el ciclo de vida de la aplicación; el coordinador (`coordinador.py`) sigue siendo
una corrutina que se espera (síncrona, sin colas, workers, Celery ni Redis).

RECURSOS. Se construyen UNA vez en el `lifespan` (`iniciar_ingesta`) y se cierran en orden (`detener_ingesta`):
tareas en curso, cliente de OCI, cliente de MinIO y motor de la base vectorial. Cada tarea abre sus propias
`AsyncSession`; nunca se comparten. Si falta configuración, la aplicación arranca igual (autenticación, usuarios,
empresas y health siguen disponibles) y la ingesta responde 503 `INGESTION_NOT_CONFIGURED` nombrando los
componentes (nunca valores).

DESCONEXIÓN DEL CLIENTE. La petición espera la ingesta con `asyncio.shield`: si se cancela (el cliente se fue, un
proxy cortó o el servidor se apaga) la ingesta CONTINÚA hasta un desenlace cierto en una tarea registrada que
`cerrar()` espera. Nadie anuncia éxito a un cliente ausente: el resultado queda en la base y se consulta con
`GET /documentos/operaciones/{id}`. Si la propia tarea es cancelada (apagado que agota la espera) el coordinador no
toca el estado: el documento queda `EN_PROCESO`, no se libera ninguna reserva y la recuperación lo toma al vencer su
vigencia. El archivo se copia a memoria dentro de la petición porque Starlette cierra el `UploadFile` al terminar
(o cancelarse) esta.

RECUPERACIÓN. `_bucle_recuperacion` ejecuta `coordinador.ejecutar_recuperacion` cada `intervalo` segundos. Con
varias instancias, un `pg_try_advisory_lock` (por ambiente, sobre una conexión dedicada) deja barrer a UNA a la vez;
si no se consigue, el barrido se omite. El candado es una optimización: la seguridad la dan las garantías ya
existentes (UPDATE condicionales con token y vigencia, cierre del documento en la base vectorial, compensación
idempotente), que hacen inocuo un barrido repetido.
"""
import asyncio
import contextlib
import logging
import re
import uuid
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.exception_handlers import status_for_exception
from app.exceptions import AppException, ConflictError, ExternalServiceError, ServiceUnavailableError
from app.schemas import MetadatosIngestaRequest
from app.schemas_ingesta import (
    EstadoOperacionPublico,
    OperacionCreadaResponse,
    OperacionResponse,
    ResultadoIngestaResponse,
)
from app.services.ingesta import coordinador
from app.services.ingesta import documento_service as servicio
from app.services.ingesta import operacion_service
from app.services.ingesta.almacenamiento import config_desde_settings, validar_ambiente
from app.services.ingesta.coordinador import ConfigCoordinador, DependenciasIngesta, PublicacionVectorialPendiente
from app.services.ingesta.parametros import PARAMETROS_INGESTA, ParametrosIngesta
from app.services.ingesta.reglas import TAMANO_MAXIMO_BYTES

logger = logging.getLogger("igualab.ingesta.http")

TAMANO_BLOQUE_COPIA = 1024 * 1024
_MOTIVO_SEGURO = re.compile(r"[\w\s,.:()\-ÁÉÍÓÚÑáéíóúñ]{1,200}")


# --- Copia acotada del archivo recibido -----------------------------------------------------------------

class FuenteEnMemoria:
    """Lector asíncrono (`LectorBytes`) sobre los bytes ya copiados. Al llegar al final suelta la memoria."""

    def __init__(self, datos: bytearray) -> None:
        self._datos: bytearray | None = datos
        self._posicion = 0

    @property
    def tamano(self) -> int:
        return len(self._datos) if self._datos is not None else 0

    def liberar(self) -> None:
        self._datos = None

    async def leer(self, cantidad: int) -> bytes:
        if self._datos is None:
            return b""
        trozo = bytes(self._datos[self._posicion:self._posicion + cantidad])
        self._posicion += len(trozo)
        if not trozo:
            self._datos = None
        return trozo


async def copiar_archivo_acotado(leer: Callable[[int], Awaitable[bytes]]) -> FuenteEnMemoria:
    """Copia el archivo recibido deteniéndose al superar `TAMANO_MAXIMO_BYTES`. La decisión de 413 es del
    validador, que cuenta los bytes REALES (no Content-Length ni `size` declarados): esta copia solo evita leer un
    archivo ilimitado. El límite es del ARCHIVO, no del cuerpo multipart completo."""
    datos = bytearray()
    while True:
        bloque = await leer(TAMANO_BLOQUE_COPIA)
        if not bloque:
            break
        datos += bloque
        if len(datos) > TAMANO_MAXIMO_BYTES:
            break
    return FuenteEnMemoria(datos)


# --- Configuración ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OpcionesGestor:
    recuperacion_habilitada: bool = True
    intervalo_recuperacion: float = 60.0
    espera_cierre: float = 30.0
    retraso_primer_barrido: float = 10.0

    def __post_init__(self) -> None:
        for nombre in ("intervalo_recuperacion", "espera_cierre", "retraso_primer_barrido"):
            valor = getattr(self, nombre)
            if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not valor >= 0:
                raise ValueError(f"{nombre} debe ser un número de segundos no negativo.")
        if self.intervalo_recuperacion <= 0:
            raise ValueError("intervalo_recuperacion debe ser mayor que 0.")


def _clave_candado(ambiente: str) -> int:
    return zlib.crc32(f"igualab:ingesta:recuperacion:{ambiente}".encode("utf-8"))


class GestorIngesta:
    def __init__(
        self,
        dependencias: DependenciasIngesta,
        config: ConfigCoordinador | None = None,
        opciones: OpcionesGestor | None = None,
        *,
        liberar_recursos: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.dependencias = dependencias
        self.config = config or ConfigCoordinador()
        self.opciones = opciones or OpcionesGestor()
        self.ambiente = validar_ambiente(dependencias.almacen.ambiente)
        self._liberar = liberar_recursos
        self._tareas: set[asyncio.Task] = set()
        self._bucle: asyncio.Task | None = None
        self._cerrado = False
        self.barridos = 0
        self.barridos_omitidos = 0

    # --- ciclo de vida ----------------------------------------------------------------------------------

    async def iniciar(self) -> None:
        if self.opciones.recuperacion_habilitada and self._bucle is None:
            self._bucle = asyncio.create_task(self._bucle_recuperacion(), name="recuperacion-ingesta")

    async def cerrar(self) -> None:
        """Cierre ordenado y repetible: detiene la recuperación, espera las ingestas en curso (hasta
        `espera_cierre`), cancela las que no terminan (quedan EN_PROCESO para la recuperación) y libera recursos."""
        if self._cerrado:
            return
        self._cerrado = True
        if self._bucle is not None:
            self._bucle.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._bucle
        pendientes = [t for t in self._tareas if not t.done()]
        if pendientes:
            _, sin_terminar = await asyncio.wait(pendientes, timeout=self.opciones.espera_cierre)
            for tarea in sin_terminar:
                tarea.cancel()
            if sin_terminar:
                await asyncio.gather(*sin_terminar, return_exceptions=True)
        if self._liberar is not None:
            with contextlib.suppress(Exception):
                await self._liberar()

    @property
    def tareas_en_curso(self) -> int:
        return sum(1 for t in self._tareas if not t.done())

    def _exigir_activo(self) -> None:
        if self._cerrado:
            raise ServiceUnavailableError(
                "INGESTION_SHUTTING_DOWN", "El servicio se está deteniendo; vuelva a intentarlo en unos segundos."
            )

    def _lanzar(self, corrutina, nombre: str) -> asyncio.Task:
        tarea = asyncio.create_task(corrutina, name=nombre)
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tarea_terminada)
        return tarea

    def _tarea_terminada(self, tarea: asyncio.Task) -> None:
        self._tareas.discard(tarea)
        if tarea.cancelled():
            return
        error = tarea.exception()  # se recupera siempre: nadie queda sin leer la excepción (cliente ausente)
        if error is not None:
            codigo = error.code if isinstance(error, AppException) else type(error).__name__
            logger.warning("Ingesta terminó con error: tarea=%s codigo=%s", tarea.get_name(), codigo)

    # --- operaciones -------------------------------------------------------------------------------------

    async def crear_operacion(self, db, *, usuario_id: uuid.UUID) -> OperacionCreadaResponse:
        self._exigir_activo()
        return await operacion_service.crear_operacion(db, usuario_id=usuario_id, ambiente=self.ambiente)

    async def ingerir(
        self,
        *,
        operacion_id: uuid.UUID,
        usuario_id: uuid.UUID,
        nombre_archivo: str | None,
        leer: Callable[[int], Awaitable[bytes]],
        metadatos: MetadatosIngestaRequest,
    ) -> ResultadoIngestaResponse:
        """Toma la operación (exclusiva), copia el archivo y ejecuta la ingesta DESACOPLADA de la petición.
        Espera el desenlace con `shield`: si la petición se cancela la ingesta sigue (ver módulo)."""
        self._exigir_activo()
        async with self.dependencias.sesiones() as db:
            await operacion_service.tomar_carga(
                db, operacion_id, usuario_id=usuario_id, ambiente=self.ambiente, vigencia=self.config.vigencia
            )
        try:
            fuente = await copiar_archivo_acotado(leer)
        except asyncio.CancelledError:
            raise  # la carga quedó EN_CARGA sin documento: se muestra INTERRUMPIDA al vencer su plazo
        except Exception as exc:
            await self._marcar_rechazada(operacion_id, exc)
            raise
        tarea = self._lanzar(
            self._ejecutar(operacion_id, usuario_id, nombre_archivo, fuente, metadatos), f"ingesta-{operacion_id}"
        )
        return await asyncio.shield(tarea)

    async def _ejecutar(
        self,
        operacion_id: uuid.UUID,
        usuario_id: uuid.UUID,
        nombre_archivo: str | None,
        fuente: FuenteEnMemoria,
        metadatos: MetadatosIngestaRequest,
    ) -> ResultadoIngestaResponse:
        try:
            resultado = await coordinador.ingerir_documento(
                self.dependencias,
                self.config,
                usuario_id=usuario_id,
                nombre_archivo=nombre_archivo,
                leer=fuente.leer,
                metadatos=metadatos,
                operacion_id=operacion_id,
            )
        except PublicacionVectorialPendiente as exc:
            self._anotar_operacion(exc, operacion_id)
            raise
        except asyncio.CancelledError:
            raise  # el coordinador no tocó el estado: EN_PROCESO hasta que la recuperación decida
        except Exception as exc:
            await self._marcar_rechazada(operacion_id, exc)
            raise
        finally:
            fuente.liberar()  # suelta el archivo aunque no se haya leído hasta el final
        async with self.dependencias.sesiones() as db:
            vista = await operacion_service.obtener_vista(db, operacion_id, self.ambiente)
        if not vista.exitosa:
            # El coordinador solo devuelve con todo confirmado; si la base dice otra cosa, no se anuncia éxito.
            raise ExternalServiceError(
                "INGESTION_OUTCOME_NOT_CONFIRMED",
                "No se pudo confirmar el desenlace de la ingesta; consulte la operación.",
                details={"operacion_id": str(operacion_id), "estado": vista.estado.value},
            )
        return ResultadoIngestaResponse(
            **vista.model_dump(), motivos=list(resultado.motivos), fragmentos=resultado.fragmentos
        )

    @staticmethod
    def _anotar_operacion(exc: PublicacionVectorialPendiente, operacion_id: uuid.UUID) -> None:
        exc.details = {
            **(exc.details or {}),
            "operacion_id": str(operacion_id),
            "progreso_url": f"/documentos/operaciones/{operacion_id}",
            "reintentar_url": f"/documentos/operaciones/{operacion_id}/reintentar-publicacion",
        }

    async def _marcar_rechazada(self, operacion_id: uuid.UUID, exc: BaseException) -> None:
        """La carga terminó con error: si NO llegó a reservar documento, la operación queda RECHAZADA con el código
        del error. Si el documento ya existe, no se toca (manda el documento). Mejor esfuerzo."""
        if isinstance(exc, AppException):
            codigo, mensaje, estado_http = exc.code, exc.message, status_for_exception(exc)
        else:
            codigo, mensaje, estado_http = "INTERNAL_ERROR", "Ocurrió un error interno.", 500
        try:
            async with self.dependencias.sesiones() as db:
                await operacion_service.marcar_rechazada(
                    db, operacion_id, self.ambiente, codigo=codigo, mensaje=mensaje, estado_http=estado_http
                )
        except Exception as error:
            logger.warning("No se pudo marcar la operación como rechazada (%s).", type(error).__name__)

    async def reintentar_publicacion(self, operacion_id: uuid.UUID) -> OperacionResponse:
        """Repite SOLO la publicación vectorial de una operación en PUBLICACION_PENDIENTE: no regenera embeddings
        ni reanaliza. Cualquier otro estado es 409. Si vuelve a fallar: 502 con la operación y su reintento."""
        self._exigir_activo()
        async with self.dependencias.sesiones() as db:
            vista = await operacion_service.obtener_vista(db, operacion_id, self.ambiente)
        if vista.documento_id is None:
            raise ConflictError(
                "OPERATION_WITHOUT_DOCUMENT",
                "La operación no tiene un documento: no hay publicación que reintentar.",
                details={"estado": vista.estado.value},
            )
        if vista.estado is not EstadoOperacionPublico.PUBLICACION_PENDIENTE:
            raise ConflictError(
                "VECTOR_PUBLICATION_NOT_PENDING",
                "La operación no tiene una publicación vectorial pendiente.",
                details={"estado": vista.estado.value},
            )
        try:
            await coordinador.reintentar_publicacion(self.dependencias, vista.documento_id)
        except PublicacionVectorialPendiente as exc:
            self._anotar_operacion(exc, operacion_id)
            raise
        async with self.dependencias.sesiones() as db:
            return await operacion_service.obtener_vista(db, operacion_id, self.ambiente)

    # --- recuperación periódica ---------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _candado_de_barrido(self) -> AsyncIterator[bool]:
        """Exclusión entre instancias: `pg_try_advisory_lock` sobre una conexión dedicada que se mantiene durante
        el barrido. Si no se puede soltar con seguridad se descarta la conexión (cierra el candado). Fuera de
        PostgreSQL (pruebas con SQLite) no hay candado."""
        async with self.dependencias.sesiones() as sesion:
            conexion = await sesion.connection()
            if conexion.dialect.name != "postgresql":
                yield True
                return
            clave = _clave_candado(self.ambiente)
            obtenido = bool((await conexion.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": clave})).scalar_one())
            soltado = not obtenido
            try:
                yield obtenido
            finally:
                if obtenido:
                    with contextlib.suppress(Exception):
                        soltado = bool(
                            (await conexion.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": clave})).scalar_one()
                        )
                    if not soltado:
                        with contextlib.suppress(Exception):
                            await conexion.invalidate()  # cerrar la conexión libera el candado
                with contextlib.suppress(Exception):
                    await sesion.rollback()

    async def barrido(self, **argumentos: Any) -> servicio.ResumenRecuperacion | None:
        """Un barrido de recuperación. `None` si otra instancia lo está haciendo."""
        async with self._candado_de_barrido() as obtenido:
            if not obtenido:
                self.barridos_omitidos += 1
                return None
            self.barridos += 1
            return await coordinador.ejecutar_recuperacion(self.dependencias, **argumentos)

    async def _bucle_recuperacion(self) -> None:
        espera = min(self.opciones.retraso_primer_barrido, self.opciones.intervalo_recuperacion)
        while True:
            await asyncio.sleep(espera)
            espera = self.opciones.intervalo_recuperacion
            try:
                await self.barrido()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("El barrido de recuperación de la ingesta falló (%s).", type(exc).__name__)


# --- Construcción desde la configuración -------------------------------------------------------------------

def _motivo_seguro(exc: BaseException) -> str:
    """Mensaje del propio validador de configuración (nombra variables, nunca valores) o, si no lo es, solo el
    nombre de la clase de la excepción."""
    if isinstance(exc, ExternalServiceError):
        return exc.code
    texto = str(exc)
    if isinstance(exc, ValueError) and _MOTIVO_SEGURO.fullmatch(texto):
        return texto
    return type(exc).__name__


def _intentar(problemas: list[dict], componente: str, construir: Callable[[], Any], *, motivo_de_clase: bool = False):
    """Ejecuta `construir()`; si falla anota el componente y el motivo (seguro) y devuelve `None`."""
    try:
        return construir()
    except Exception as exc:
        problemas.append({"componente": componente, "motivo": type(exc).__name__ if motivo_de_clase else _motivo_seguro(exc)})
        return None


def _cerrar_recursos(*recursos: object) -> None:
    for recurso in recursos:
        cerrar = getattr(recurso, "cerrar", None)
        if callable(cerrar):
            with contextlib.suppress(Exception):
                cerrar()


def configuracion_del_gestor(parametros: ParametrosIngesta) -> tuple[ConfigCoordinador, OpcionesGestor]:
    """Traduce los parámetros fijos de código a la configuración del coordinador y a las opciones del gestor."""
    config = ConfigCoordinador(
        tamano_lote=parametros.tamano_lote,
        timeout_embeddings_segundos=parametros.timeout_embeddings_segundos,
        vigencia=parametros.vigencia,
    )
    opciones = OpcionesGestor(
        recuperacion_habilitada=parametros.recuperacion_habilitada,
        intervalo_recuperacion=parametros.intervalo_recuperacion_segundos,
        espera_cierre=parametros.espera_cierre_segundos,
    )
    return config, opciones


def _construir_almacen(configuracion: object, parametros: ParametrosIngesta, problemas: list[dict]):
    from app.services.ingesta.almacenamiento_s3 import AlmacenOriginalesS3

    config = _intentar(problemas, "almacenamiento", lambda: config_desde_settings(configuracion, parametros))
    if config is None:
        return None
    # Fallo al crear el cliente de MinIO: solo se informa la clase del error (nunca su mensaje).
    return _intentar(problemas, "almacenamiento", lambda: AlmacenOriginalesS3(config), motivo_de_clase=True)


def construir_gestor(configuracion: object | None = None, parametros: ParametrosIngesta | None = None) -> GestorIngesta:
    """Construye los recursos compartidos. `configuracion` son los ajustes del entorno (por defecto
    `app.config.settings`); `parametros` los valores fijos de código (por defecto `PARAMETROS_INGESTA`): en pruebas se
    sustituyen directamente, sin editar ningún `.env`. Si falta o es inválida alguna configuración lanza
    `ServiceUnavailableError('INGESTION_NOT_CONFIGURED')` con los componentes afectados y libera lo ya creado."""
    from app.database import AsyncSessionLocal
    from app.database_vectorial import cerrar_motor_vectorial, fabrica_sesiones_vectoriales
    from app.services.ingesta.proveedor_oci import ProveedorEmbeddingsOCI

    configuracion = configuracion if configuracion is not None else settings
    parametros = parametros if parametros is not None else PARAMETROS_INGESTA
    problemas: list[dict] = []
    almacen = _construir_almacen(configuracion, parametros, problemas)
    embeddings = _intentar(problemas, "embeddings", lambda: ProveedorEmbeddingsOCI(parametros))
    sesiones_vectoriales = _intentar(problemas, "base_vectorial", fabrica_sesiones_vectoriales)

    if problemas:
        _cerrar_recursos(embeddings, almacen)
        raise ServiceUnavailableError(
            "INGESTION_NOT_CONFIGURED",
            "La ingesta de documentos no está disponible: falta o es inválida la configuración de "
            + ", ".join(dict.fromkeys(p["componente"] for p in problemas))
            + ".",
            details={"componentes": problemas},
        )

    async def liberar() -> None:
        _cerrar_recursos(embeddings, almacen)
        with contextlib.suppress(Exception):
            await cerrar_motor_vectorial()

    dependencias = DependenciasIngesta(
        sesiones=AsyncSessionLocal, sesiones_vectoriales=sesiones_vectoriales, almacen=almacen, embeddings=embeddings
    )
    return GestorIngesta(dependencias, *configuracion_del_gestor(parametros), liberar_recursos=liberar)


async def iniciar_ingesta(app, parametros: ParametrosIngesta | None = None, preparar_esquema=None) -> None:
    """Para el `lifespan`: deja `app.state.ingesta` (gestor) o `app.state.ingesta_error` (por qué no está disponible).
    NUNCA impide el arranque ni afecta a los demás módulos.

    Orden: (1) construir los recursos (valida la configuración, sin red); (2) PREPARAR EL ESQUEMA de las dos bases con migraciones
    versionadas (`app.migraciones`; `preparar_esquema` permite sustituirlo en pruebas); (3) solo entonces habilitar la ingesta. Si
    algo falla, la ingesta responde 503 y los recursos ya creados se liberan."""
    from app.migraciones.catalogo import asegurar_esquema_ingesta
    from app.migraciones.motor import ErrorMigracion

    preparar = preparar_esquema if preparar_esquema is not None else asegurar_esquema_ingesta
    app.state.ingesta = None
    app.state.ingesta_error = None
    gestor = None
    try:
        gestor = construir_gestor(parametros=parametros)
        await preparar()
        await gestor.iniciar()
    except ServiceUnavailableError as exc:
        app.state.ingesta_error = (exc.code, exc.message, exc.details)
        logger.warning("Ingesta no disponible: %s", [c["componente"] for c in (exc.details or {}).get("componentes", [])])
    except ErrorMigracion as exc:
        app.state.ingesta_error = (
            "INGESTION_SCHEMA_NOT_READY",
            f"La ingesta no está disponible: no se pudo preparar el esquema de la base {exc.base}. {exc.mensaje}",
            {"base": exc.base, "motivo": exc.codigo, **exc.detalles},
        )
        logger.error("Ingesta no disponible: preparación del esquema de la base %s falló (%s).", exc.base, exc.codigo)
    except Exception as exc:
        app.state.ingesta_error = (
            "INGESTION_NOT_CONFIGURED",
            "La ingesta de documentos no está disponible por un error de inicialización.",
            {"motivo": type(exc).__name__},
        )
        logger.warning("Ingesta no disponible: error de inicialización (%s).", type(exc).__name__)
    else:
        app.state.ingesta = gestor
        return
    if gestor is not None:
        await gestor.cerrar()  # liberar lo ya construido (no llegó a iniciarse)


async def detener_ingesta(app) -> None:
    gestor = getattr(app.state, "ingesta", None)
    if gestor is not None:
        await gestor.cerrar()
        app.state.ingesta = None

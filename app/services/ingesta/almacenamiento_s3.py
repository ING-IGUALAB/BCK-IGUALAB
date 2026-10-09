"""Adaptador S3/MinIO para originales de ingesta (Etapa 4A). Implementa `AlmacenOriginales`.

CONEXIÓN. Solo HTTPS con certificado verificado (`verify=True`, sin opción para
desactivarlo), firma v4, direccionamiento por ruta (MinIO), plazos de conexión y de
lectura de socket explícitos, UNA sola petición por operación (`total_max_attempts=1`;
ojo: `max_attempts=1` de botocore significaría 2 peticiones) y sin sumas de verificación
automáticas de las versiones recientes de boto3 (`when_required`), que algunos servidores
S3 compatibles no aceptan. No crea buckets ni modifica permisos.

PLAZOS EFECTIVOS. `connect_timeout` y `read_timeout` los aplica el SDK (el segundo es la
inactividad máxima del socket, NO un plazo total). `operation_timeout` lo aplica
`asyncio.timeout` a la corrutina que espera al hilo: libera al llamador, pero NO detiene
la petición.

NO BLOQUEA EL BUCLE. El SDK es síncrono: cada operación corre en un hilo
(`asyncio.to_thread`). Un hilo no se interrumpe: tras un timeout o una cancelación la
operación puede seguir y terminar en el servidor MÁS TARDE. Por eso:
- `guardar` registra el desenlace REAL del hilo (`estado_subida`): `EN_CURSO` mientras
  corre, y después `CREADA` (con su `VersionId`), `NO_CREADA` o `INCIERTA`. El registro
  vive solo en este proceso: tras un reinicio el estado es `SIN_REGISTRO`.
- Un timeout de `guardar` o `eliminar` se informa con `resultado_incierto=True`. Quien
  coordina NO debe dar por limpio nada mientras la subida siga `EN_CURSO` ni confiar en
  que `DELETE` + `HEAD` demuestren ausencia: la subida pendiente puede crear el objeto
  después. La cancelación se propaga siempre.

NO SOBRESCRITURA. No se usa escritura condicional (`If-None-Match`): su soporte en el
servidor del equipo no está comprobado. Este adaptador solo impide una segunda subida de la
misma clave (`STORAGE_UPLOAD_ALREADY_ATTEMPTED`) MIENTRAS CONSERVA su registro en memoria. Ese
registro está acotado (`_MAXIMO_REGISTROS_SUBIDA`): al superarlo se descartan las claves ya
terminadas más antiguas (nunca las `EN_CURSO`), y se pierde al reiniciar. Una clave olvidada puede
subirse de nuevo y sobrescribirse; igual que otro proceso que invoque `guardar` con la misma clave.
La protección DURADERA es la del servicio (UPDATE condicional en BD: un único intento por
documento); el registro del adaptador es solo una defensa adicional y el origen de `estado_subida`.
No se amplía a un registro ilimitado.

INTEGRIDAD. `guardar` comprueba que los bytes coinciden con el SHA-256 validado y
envía `Content-MD5`, de modo que el servidor rechaza un cuerpo alterado en tránsito.
Se guardan los bytes exactos, BOM incluido.

ERRORES. `ExternalServiceError` / `ExternalServiceTimeoutError` con códigos
`STORAGE_ERROR`, `STORAGE_TIMEOUT`, `STORAGE_OBJECT_NOT_FOUND`, `STORAGE_INVALID_KEY`.
Los detalles solo llevan la operación, el nombre de la clase de la excepción, el
código S3 (si es un identificador corto) y el estado HTTP. Nunca credenciales,
endpoint, bucket, clave, contenido, URLs firmadas ni la respuesta del servidor ni
`str(excepción)`. El módulo no escribe logs.

VERSIONADO. No se presupone que el bucket tenga el versionado desactivado ni se cambia su
configuración. Con la versión conocida, `eliminar` borra esa versión concreta. Sin ella,
`eliminar` crearía una marca de borrado y la versión podría seguir detrás: para reconciliar
existe `listar_versiones` (solo la clave exacta; requiere el permiso
`s3:ListBucketVersions`, no confirmado). Si no hay permiso, quien coordina deja la
compensación pendiente. Nunca se borran versiones de otras claves.
"""
import asyncio
import base64
import hashlib
import re
import threading
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)

from app.exceptions import ConflictError, ExternalServiceError, ExternalServiceTimeoutError
from app.services.ingesta.almacenamiento import (
    ConfigAlmacenamiento,
    EstadoSubida,
    ReferenciaOriginal,
    SubidaConocida,
    VersionObjeto,
    clave_pertenece_al_ambiente,
)

CONTENT_TYPE_MARKDOWN = "text/markdown; charset=utf-8"
_SHA256_REGEX = re.compile(r"[0-9a-f]{64}")
_CODIGO_S3_REGEX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SIN_OBJETO = {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}
# Errores que garantizan que la petición NUNCA llegó al servidor (no se estableció la conexión).
_SIN_ENVIO = (ConnectTimeoutError, EndpointConnectionError, SSLError)
_MAXIMO_PAGINAS_VERSIONES = 100
_MAXIMO_REGISTROS_SUBIDA = 1024


def crear_cliente_s3(config: ConfigAlmacenamiento) -> Any:
    """Cliente boto3 con TLS verificado y plazos explícitos. No realiza ninguna llamada."""
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        verify=True,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=config.connect_timeout,
            read_timeout=config.read_timeout,
            retries={"total_max_attempts": 1, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _error(codigo: str, mensaje: str, operacion: str, **datos: object) -> ExternalServiceError:
    return ExternalServiceError(codigo, mensaje, details={"operacion": operacion, **datos})


def _datos_cliente(exc: ClientError) -> tuple[str | None, int | None]:
    respuesta = exc.response if isinstance(exc.response, dict) else {}
    codigo = respuesta.get("Error", {}).get("Code")
    estado = respuesta.get("ResponseMetadata", {}).get("HTTPStatusCode")
    codigo = str(codigo) if codigo is not None and _CODIGO_S3_REGEX.fullmatch(str(codigo)) else None
    return codigo, estado if isinstance(estado, int) and not isinstance(estado, bool) else None


def _sin_objeto(exc: ClientError) -> bool:
    codigo, estado = _datos_cliente(exc)
    return codigo in _SIN_OBJETO or estado == 404


def _es_rechazo_definitivo(exc: ClientError) -> bool:
    """Un 4xx (salvo 408/429) significa que el servidor NO guardó el objeto."""
    _, estado = _datos_cliente(exc)
    return estado is not None and 400 <= estado < 500 and estado not in (408, 429)


def listar_versiones_exactas(cliente: Any, bucket: str, clave: str) -> list[VersionObjeto]:
    """Versiones y marcas de borrado de la clave EXACTA (síncrono; lo usan el adaptador y el
    script manual). Lanza `ClientError` si faltan permisos: nunca devuelve una lista vacía
    por no poder consultar."""
    resultado: list[VersionObjeto] = []
    marcador: dict[str, str] | None = {}
    for _ in range(_MAXIMO_PAGINAS_VERSIONES):
        pagina = cliente.list_object_versions(Bucket=bucket, Prefix=clave, **marcador)
        resultado.extend(_versiones_de_pagina(pagina, clave))
        if not pagina.get("IsTruncated"):
            return resultado
        marcador = _marcador_siguiente(pagina)
        if marcador is None:
            break
    raise RuntimeError("listado de versiones incompleto")  # un listado cortado no se declara vacío


def _versiones_de_pagina(pagina: dict, clave: str) -> list[VersionObjeto]:
    """Versiones y luego marcas de borrado de la clave EXACTA en una página del listado."""
    encontradas: list[VersionObjeto] = []
    for entrada in pagina.get("Versions") or []:
        if entrada.get("Key") == clave and entrada.get("VersionId"):
            tamano = entrada.get("Size")
            encontradas.append(
                VersionObjeto(str(entrada["VersionId"]), False, tamano if isinstance(tamano, int) else None)
            )
    for entrada in pagina.get("DeleteMarkers") or []:
        if entrada.get("Key") == clave and entrada.get("VersionId"):
            encontradas.append(VersionObjeto(str(entrada["VersionId"]), True, None))
    return encontradas


def _marcador_siguiente(pagina: dict) -> dict[str, str] | None:
    """Marcador de la página siguiente, o None si una página truncada no lo informa."""
    siguiente_clave, siguiente_version = pagina.get("NextKeyMarker"), pagina.get("NextVersionIdMarker")
    if not siguiente_clave:
        return None
    marcador = {"KeyMarker": siguiente_clave}
    if siguiente_version:
        marcador["VersionIdMarker"] = siguiente_version
    return marcador


def _validar_contenido(contenido: object, sha256: object) -> bytes:
    """Contenido como `bytes` si es bytes y coincide con el SHA-256 indicado; si no, TypeError/ValueError."""
    if not isinstance(contenido, (bytes, bytearray)):
        raise TypeError("El contenido debe ser bytes.")
    if not isinstance(sha256, str) or _SHA256_REGEX.fullmatch(sha256) is None:
        raise ValueError("sha256 debe ser un resumen hexadecimal en minúsculas de 64 caracteres.")
    datos = bytes(contenido)
    if hashlib.sha256(datos).hexdigest() != sha256:
        raise ValueError("El contenido no corresponde al SHA-256 indicado.")
    return datos


class _Subida:
    __slots__ = ("estado", "version_id")

    def __init__(self) -> None:
        self.estado = EstadoSubida.EN_CURSO
        self.version_id: str | None = None


class AlmacenOriginalesS3:
    def __init__(self, config: ConfigAlmacenamiento, cliente: Any | None = None) -> None:
        if not isinstance(config, ConfigAlmacenamiento):
            raise TypeError("config debe ser una ConfigAlmacenamiento.")
        self._config = config
        self._cliente = cliente if cliente is not None else crear_cliente_s3(config)
        self._candado = threading.Lock()
        self._subidas: dict[str, _Subida] = {}  # desenlace real de cada `guardar` de este proceso

    @property
    def ambiente(self) -> str:
        return self._config.ambiente

    def __repr__(self) -> str:  # sin credenciales, endpoint ni bucket
        return f"AlmacenOriginalesS3(ambiente={self._config.ambiente!r})"

    def cerrar(self) -> None:
        """Libera las conexiones del cliente. Idempotente; no falla si el cliente no expone `close`."""
        cerrar = getattr(self._cliente, "close", None)
        if callable(cerrar):
            cerrar()

    # --- validaciones previas a cualquier llamada de red -----------------------

    def _exigir_propia(self, referencia: ReferenciaOriginal, operacion: str) -> None:
        version = referencia.version_id
        if not clave_pertenece_al_ambiente(self._config.ambiente, referencia.clave):
            raise _error(
                "STORAGE_INVALID_KEY", "La clave del objeto no pertenece a este almacén.", operacion
            )
        if version is not None and (
            not isinstance(version, str) or not 0 < len(version) <= 255 or not version.isprintable()
        ):
            raise _error("STORAGE_INVALID_KEY", "La versión del objeto no es válida.", operacion)

    # --- ejecución sin bloquear el bucle ---------------------------------------

    async def _ejecutar(self, operacion: str, funcion: Callable[[], Any], *, incierto: bool) -> Any:
        try:
            async with asyncio.timeout(self._config.operation_timeout):
                return await asyncio.to_thread(funcion)
        except (TimeoutError, ConnectTimeoutError, ReadTimeoutError):
            raise ExternalServiceTimeoutError(
                "STORAGE_TIMEOUT",
                "El almacenamiento no respondió dentro del plazo.",
                details={"operacion": operacion, "resultado_incierto": incierto},
            ) from None
        except ClientError as exc:
            codigo, estado = _datos_cliente(exc)
            raise _error(
                "STORAGE_ERROR",
                "El almacenamiento devolvió un error.",
                operacion,
                codigo_s3=codigo,
                estado_http=estado,
            ) from None
        except Exception as exc:
            # Solo el nombre de la clase: str(exc) puede incluir endpoint, clave o credenciales.
            raise _error(
                "STORAGE_ERROR",
                "El almacenamiento devolvió un error.",
                operacion,
                tipo_error=type(exc).__name__,
            ) from None

    # --- contrato ---------------------------------------------------------------

    async def guardar(self, clave: str, contenido: bytes, sha256: str) -> ReferenciaOriginal:
        referencia = ReferenciaOriginal(clave)
        self._exigir_propia(referencia, "guardar")
        datos = _validar_contenido(contenido, sha256)
        md5 = base64.b64encode(hashlib.md5(datos, usedforsecurity=False).digest()).decode("ascii")
        registro = self._registrar_intento(clave)
        respuesta = await self._ejecutar(
            "guardar", lambda: self._subir(registro, clave, datos, md5), incierto=True
        )
        version = respuesta.get("VersionId") if isinstance(respuesta, dict) else None
        return ReferenciaOriginal(clave, version if isinstance(version, str) and version else None)

    def _registrar_intento(self, clave: str) -> _Subida:
        """Reserva el intento de esta clave: una clave se intenta una sola vez por proceso."""
        registro = _Subida()
        with self._candado:
            if clave in self._subidas:
                raise ConflictError(
                    "STORAGE_UPLOAD_ALREADY_ATTEMPTED",
                    "Ya se intentó guardar esta clave: no se sobrescribe.",
                )
            self._podar_registro()
            self._subidas[clave] = registro
        return registro

    def _subir(self, registro: _Subida, clave: str, datos: bytes, md5: str) -> Any:
        # Corre en un hilo que puede sobrevivir a la corrutina: aquí se anota el
        # desenlace REAL, aunque nadie esté esperando ya.
        try:
            respuesta = self._cliente.put_object(
                Bucket=self._config.bucket,
                Key=clave,
                Body=datos,
                ContentLength=len(datos),
                ContentMD5=md5,
                ContentType=CONTENT_TYPE_MARKDOWN,
            )
        except ClientError as exc:
            self._cerrar_registro(
                registro, EstadoSubida.NO_CREADA if _es_rechazo_definitivo(exc) else EstadoSubida.INCIERTA
            )
            raise
        except _SIN_ENVIO:
            self._cerrar_registro(registro, EstadoSubida.NO_CREADA)
            raise
        except BaseException:
            self._cerrar_registro(registro, EstadoSubida.INCIERTA)
            raise
        version = respuesta.get("VersionId") if isinstance(respuesta, dict) else None
        self._cerrar_registro(
            registro, EstadoSubida.CREADA, version if isinstance(version, str) and version else None
        )
        return respuesta

    def _cerrar_registro(self, registro: _Subida, estado: EstadoSubida, version: str | None = None) -> None:
        with self._candado:
            registro.version_id = version  # la versión se fija antes de publicar el estado
            registro.estado = estado

    def _podar_registro(self) -> None:
        """Acota la memoria: descarta los registros terminados más antiguos (nunca EN_CURSO)."""
        exceso = len(self._subidas) - _MAXIMO_REGISTROS_SUBIDA + 1
        if exceso <= 0:
            return
        for clave in [c for c, r in self._subidas.items() if r.estado is not EstadoSubida.EN_CURSO][:exceso]:
            del self._subidas[clave]

    def estado_subida(self, clave: str) -> SubidaConocida:
        self._exigir_propia(ReferenciaOriginal(clave), "estado_subida")
        with self._candado:
            registro = self._subidas.get(clave)
            if registro is None:
                return SubidaConocida(EstadoSubida.SIN_REGISTRO)
            return SubidaConocida(registro.estado, registro.version_id)

    async def listar_versiones(self, clave: str) -> list[VersionObjeto]:
        self._exigir_propia(ReferenciaOriginal(clave), "listar_versiones")
        return await self._ejecutar(
            "listar_versiones",
            lambda: listar_versiones_exactas(self._cliente, self._config.bucket, clave),
            incierto=False,
        )

    def _argumentos(self, referencia: ReferenciaOriginal) -> dict[str, str]:
        argumentos = {"Bucket": self._config.bucket, "Key": referencia.clave}
        if referencia.version_id:
            argumentos["VersionId"] = referencia.version_id
        return argumentos

    async def leer(self, referencia: ReferenciaOriginal) -> bytes:
        self._exigir_propia(referencia, "leer")

        def descargar() -> bytes | None:
            try:
                cuerpo = self._cliente.get_object(**self._argumentos(referencia))["Body"]
            except ClientError as exc:
                if _sin_objeto(exc):
                    return None
                raise
            try:
                return cuerpo.read()
            finally:
                cuerpo.close()

        datos = await self._ejecutar("leer", descargar, incierto=False)
        if datos is None:
            raise _error("STORAGE_OBJECT_NOT_FOUND", "El objeto solicitado no existe.", "leer")
        return bytes(datos)

    async def existe(self, referencia: ReferenciaOriginal) -> bool:
        self._exigir_propia(referencia, "existe")

        def consultar() -> bool:
            try:
                self._cliente.head_object(**self._argumentos(referencia))
            except ClientError as exc:
                if _sin_objeto(exc):
                    return False
                raise
            return True

        return await self._ejecutar("existe", consultar, incierto=False)

    async def eliminar(self, referencia: ReferenciaOriginal) -> None:
        self._exigir_propia(referencia, "eliminar")

        def borrar() -> None:
            try:
                self._cliente.delete_object(**self._argumentos(referencia))
            except ClientError as exc:
                if not _sin_objeto(exc):  # ya inexistente: repetir la eliminación no es un error
                    raise

        await self._ejecutar("eliminar", borrar, incierto=True)

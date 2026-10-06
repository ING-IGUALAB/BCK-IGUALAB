"""Adaptador S3/MinIO para originales de ingesta (Etapa 4A). Implementa `AlmacenOriginales`.

CONEXIÓN. Solo HTTPS con certificado verificado (`verify=True`, sin opción para
desactivarlo), firma v4, direccionamiento por ruta (MinIO), plazos de conexión y de
lectura de socket explícitos, sin reintentos del SDK (un intento por operación) y
sin sumas de verificación automáticas de las versiones recientes de boto3
(`when_required`), que algunos servidores S3 compatibles no aceptan. No crea
buckets ni modifica permisos.

NO BLOQUEA EL BUCLE. El SDK es síncrono: cada operación corre en un hilo
(`asyncio.to_thread`) bajo `asyncio.timeout(operation_timeout)`. Un hilo no se
interrumpe: tras un timeout o una cancelación la operación puede seguir y terminar
en el servidor. Por eso un timeout de `guardar` o `eliminar` se informa con
`resultado_incierto=True` y quien coordina debe compensar (la eliminación es
idempotente). La cancelación se propaga siempre.

INTEGRIDAD. `guardar` comprueba que los bytes coinciden con el SHA-256 validado y
envía `Content-MD5`, de modo que el servidor rechaza un cuerpo alterado en tránsito.
Se guardan los bytes exactos, BOM incluido. No se usa escritura condicional
(`If-None-Match`): su soporte en el servidor del equipo no está comprobado; la no
sobrescritura se garantiza por claves únicas generadas en el servidor y porque cada
documento sube su original una sola vez.

ERRORES. `ExternalServiceError` / `ExternalServiceTimeoutError` con códigos
`STORAGE_ERROR`, `STORAGE_TIMEOUT`, `STORAGE_OBJECT_NOT_FOUND`, `STORAGE_INVALID_KEY`.
Los detalles solo llevan la operación, el nombre de la clase de la excepción, el
código S3 (si es un identificador corto) y el estado HTTP. Nunca credenciales,
endpoint, bucket, clave, contenido, URLs firmadas ni la respuesta del servidor ni
`str(excepción)`. El módulo no escribe logs.

LIMITACIÓN CON VERSIONADO. Si la versión del objeto no se conoce (subida de resultado
incierto) y el bucket tiene versionado, `eliminar` crea una marca de borrado y la
versión puede permanecer; no se lista el bucket porque esos permisos no están
confirmados. Pendiente de definir con infraestructura (D16).
"""
import asyncio
import base64
import hashlib
import re
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from app.exceptions import ExternalServiceError, ExternalServiceTimeoutError
from app.services.ingesta.almacenamiento import (
    ConfigAlmacenamiento,
    ReferenciaOriginal,
    clave_pertenece_al_ambiente,
)

CONTENT_TYPE_MARKDOWN = "text/markdown; charset=utf-8"
_SHA256_REGEX = re.compile(r"[0-9a-f]{64}")
_CODIGO_S3_REGEX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SIN_OBJETO = {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}


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
            retries={"max_attempts": 1, "mode": "standard"},
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


class AlmacenOriginalesS3:
    def __init__(self, config: ConfigAlmacenamiento, cliente: Any | None = None) -> None:
        if not isinstance(config, ConfigAlmacenamiento):
            raise TypeError("config debe ser una ConfigAlmacenamiento.")
        self._config = config
        self._cliente = cliente if cliente is not None else crear_cliente_s3(config)

    @property
    def ambiente(self) -> str:
        return self._config.ambiente

    def __repr__(self) -> str:  # sin credenciales, endpoint ni bucket
        return f"AlmacenOriginalesS3(ambiente={self._config.ambiente!r})"

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
        if not isinstance(contenido, (bytes, bytearray)):
            raise TypeError("El contenido debe ser bytes.")
        if not isinstance(sha256, str) or _SHA256_REGEX.fullmatch(sha256) is None:
            raise ValueError("sha256 debe ser un resumen hexadecimal en minúsculas de 64 caracteres.")
        datos = bytes(contenido)
        if hashlib.sha256(datos).hexdigest() != sha256:
            raise ValueError("El contenido no corresponde al SHA-256 indicado.")
        md5 = base64.b64encode(hashlib.md5(datos, usedforsecurity=False).digest()).decode("ascii")

        def subir() -> Any:
            return self._cliente.put_object(
                Bucket=self._config.bucket,
                Key=clave,
                Body=datos,
                ContentLength=len(datos),
                ContentMD5=md5,
                ContentType=CONTENT_TYPE_MARKDOWN,
            )

        respuesta = await self._ejecutar("guardar", subir, incierto=True)
        version = respuesta.get("VersionId") if isinstance(respuesta, dict) else None
        return ReferenciaOriginal(clave, version if isinstance(version, str) and version else None)

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

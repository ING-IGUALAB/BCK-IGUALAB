"""Contrato de almacenamiento de originales, claves de objeto y configuración (Etapa 4A).

Sin dependencias de boto3: el adaptador S3/MinIO vive en `almacenamiento_s3`.

CLAVES. `{ambiente}/documentos/{documento_id}/original.md`, con el prefijo del
ambiente (development/qa/uat) y el UUID del documento generados por el servidor.
El nombre de archivo recibido es metadata y NUNCA forma parte de la clave. Cada
documento tiene su propia clave y solo se sube una vez, así que ninguna operación
sobrescribe objetos de otra. Un almacén solo actúa sobre claves con su prefijo y
esta forma exacta.

SECRETOS. `ConfigAlmacenamiento` no muestra credenciales en `repr`/`str`.
"""
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

AMBIENTE_REGEX = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_BUCKET_REGEX = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_REGION_REGEX = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_UUID_HEX = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NOMBRE_OBJETO_ORIGINAL = "original.md"
LONGITUD_MAXIMA_CLAVE = 512


@dataclass(frozen=True)
class ReferenciaOriginal:
    """Dónde está el original: clave y, si el bucket tiene versionado, la versión.
    No contiene URL ni credenciales."""

    clave: str
    version_id: str | None = None


def validar_ambiente(ambiente: object) -> str:
    if not isinstance(ambiente, str) or AMBIENTE_REGEX.fullmatch(ambiente) is None:
        raise ValueError(
            "El prefijo de ambiente debe usar minúsculas, dígitos, «-» o «_» (máximo 32 caracteres)."
        )
    return ambiente


def generar_clave_original(ambiente: str, documento_id: uuid.UUID) -> str:
    if not isinstance(documento_id, uuid.UUID):
        raise TypeError("documento_id debe ser un UUID generado por el servidor.")
    return f"{validar_ambiente(ambiente)}/documentos/{documento_id}/{NOMBRE_OBJETO_ORIGINAL}"


def clave_pertenece_al_ambiente(ambiente: str, clave: object) -> bool:
    """True solo para claves con la forma exacta que genera `generar_clave_original`."""
    if not isinstance(clave, str) or len(clave) > LONGITUD_MAXIMA_CLAVE:
        return False
    patron = re.escape(validar_ambiente(ambiente)) + "/documentos/" + _UUID_HEX + r"/original\.md"
    return re.fullmatch(patron, clave) is not None


class AlmacenOriginales(Protocol):
    """Almacén de originales. Las operaciones asíncronas no bloquean el bucle de eventos.

    Los errores son `ExternalServiceError`/`ExternalServiceTimeoutError` con detalles
    seguros (sin credenciales, contenido, URLs ni respuestas del servidor).
    """

    @property
    def ambiente(self) -> str: ...

    async def guardar(self, clave: str, contenido: bytes, sha256: str) -> ReferenciaOriginal:
        """Guarda los bytes EXACTOS (BOM incluido) bajo `clave`. No sobrescribe por diseño."""
        ...

    async def leer(self, referencia: ReferenciaOriginal) -> bytes: ...

    async def existe(self, referencia: ReferenciaOriginal) -> bool: ...

    async def eliminar(self, referencia: ReferenciaOriginal) -> None:
        """Idempotente: eliminar algo ya eliminado no es un error."""
        ...


def _exigir_plazo(nombre: str, valor: object) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ValueError(f"{nombre} debe ser un número de segundos.")
    if not math.isfinite(valor) or valor <= 0:
        raise ValueError(f"{nombre} debe ser un número finito mayor que 0.")
    return float(valor)


@dataclass(frozen=True)
class ConfigAlmacenamiento:
    """Configuración explícita del almacén S3/MinIO. Solo HTTPS (certificado verificado)."""

    endpoint_url: str
    bucket: str
    ambiente: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    connect_timeout: float
    read_timeout: float
    operation_timeout: float
    region: str | None = None

    def __post_init__(self) -> None:
        partes = urlsplit(self.endpoint_url) if isinstance(self.endpoint_url, str) else None
        if (
            partes is None
            or partes.scheme != "https"
            or not partes.hostname
            or partes.username is not None
            or partes.password is not None
            or partes.query
            or partes.fragment
        ):
            # No se repite el valor recibido: podría contener credenciales.
            raise ValueError(
                "El endpoint debe ser una URL https sin credenciales, consulta ni fragmento."
            )
        if not isinstance(self.bucket, str) or _BUCKET_REGEX.fullmatch(self.bucket) is None:
            raise ValueError("El nombre del bucket no es válido.")
        validar_ambiente(self.ambiente)
        for nombre in ("access_key", "secret_key"):
            valor = getattr(self, nombre)
            if not isinstance(valor, str) or not valor.strip():
                raise ValueError(f"{nombre} no puede estar vacío.")
        if self.region is not None and (
            not isinstance(self.region, str) or _REGION_REGEX.fullmatch(self.region) is None
        ):
            raise ValueError("La región no es válida.")
        for nombre in ("connect_timeout", "read_timeout", "operation_timeout"):
            _exigir_plazo(nombre, getattr(self, nombre))


_VARIABLES_OBLIGATORIAS = (
    "MINIO_ENDPOINT_URL",
    "MINIO_BUCKET",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_PREFIX",
)


def _texto(valor: object) -> str | None:
    return valor.strip() if isinstance(valor, str) and valor.strip() else None


def _plazo_desde_texto(nombre: str, valor: object) -> float:
    try:
        return _exigir_plazo(nombre, float(valor))
    except (TypeError, ValueError):
        # Solo se nombra la variable, no su valor.
        raise ValueError(f"{nombre} debe ser un número de segundos mayor que 0.") from None


def config_desde_settings(configuracion: object | None = None) -> ConfigAlmacenamiento:
    """Construye la configuración desde `app.config.settings` (o un objeto equivalente).

    Falla nombrando las variables que faltan, nunca sus valores. No hay valores por
    defecto para endpoint, bucket, credenciales ni prefijo.
    """
    if configuracion is None:
        from app.config import settings  # import diferido: no se carga .env al importar este módulo

        configuracion = settings
    faltantes = [n for n in _VARIABLES_OBLIGATORIAS if _texto(getattr(configuracion, n, None)) is None]
    if faltantes:
        raise ValueError("Falta la configuración de almacenamiento: " + ", ".join(faltantes) + ".")
    return ConfigAlmacenamiento(
        endpoint_url=_texto(configuracion.MINIO_ENDPOINT_URL),
        bucket=_texto(configuracion.MINIO_BUCKET),
        ambiente=_texto(configuracion.MINIO_PREFIX),
        access_key=_texto(configuracion.MINIO_ACCESS_KEY),
        secret_key=_texto(configuracion.MINIO_SECRET_KEY),
        region=_texto(getattr(configuracion, "MINIO_REGION", None)),
        connect_timeout=_plazo_desde_texto(
            "MINIO_CONNECT_TIMEOUT_SECONDS", configuracion.MINIO_CONNECT_TIMEOUT_SECONDS
        ),
        read_timeout=_plazo_desde_texto("MINIO_READ_TIMEOUT_SECONDS", configuracion.MINIO_READ_TIMEOUT_SECONDS),
        operation_timeout=_plazo_desde_texto(
            "MINIO_OPERATION_TIMEOUT_SECONDS", configuracion.MINIO_OPERATION_TIMEOUT_SECONDS
        ),
    )

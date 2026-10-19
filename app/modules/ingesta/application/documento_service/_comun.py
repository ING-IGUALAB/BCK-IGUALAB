"""Helpers compartidos de `documento_service`: tiempos, propiedad de la operación,
lecturas/transiciones condicionales, duplicados y auditoría aparte."""

from app.core.exceptions import AuthorizationError  # noqa: F401
from app.core.exceptions import BusinessValidationError  # noqa: F401
from app.core.exceptions import ExternalServiceError  # noqa: F401
from app.core.exceptions import codigo_seguro  # noqa: F401
from app.core.exceptions import es_codigo_valido  # noqa: F401
from app.models.documento_ingesta import EstadoCompensacion  # noqa: F401
from app.models.documento_ingesta import EstadoOperacion  # noqa: F401
from app.models.documento_ingesta import EtapaIngesta  # noqa: F401
from app.models.documento_ingesta import OperacionIngesta  # noqa: F401
from app.models.documento_ingesta import ResultadoAnalisis  # noqa: F401
from app.models.documento_ingesta import VIGENCIA_PREDETERMINADA  # noqa: F401
from app.models.empresas import Empresa  # noqa: F401
from app.models.usuarios import RolUsuario  # noqa: F401
from app.modules.ingesta.domain import validacion  # noqa: F401
from app.modules.ingesta.domain.almacenamiento import AlmacenOriginales  # noqa: F401
from app.modules.ingesta.domain.almacenamiento import EstadoSubida  # noqa: F401
from app.modules.ingesta.domain.almacenamiento import ReferenciaOriginal  # noqa: F401
from app.modules.ingesta.domain.almacenamiento import generar_clave_original  # noqa: F401
from app.modules.ingesta.domain.almacenamiento import validar_ambiente  # noqa: F401
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores  # noqa: F401
from app.modules.ingesta.domain.reglas import TAMANO_MAXIMO_BYTES  # noqa: F401
from app.schemas import MetadatosIngestaRequest  # noqa: F401
from collections.abc import Sequence  # noqa: F401
from dataclasses import dataclass  # noqa: F401
import enum  # noqa: F401
import hashlib  # noqa: F401
import re  # noqa: F401
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
)
from app.modules.auditoria.service import registrar_evento
from app.models import ResultadoAuditoria, TipoEventoAuditoria, Usuario
from app.models.documento_ingesta import (
    Documento,
    EstadoProcesamiento,
)

logger = logging.getLogger("igualab.ingesta")


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

async def _cargar(
    db: AsyncSession, documento_id: uuid.UUID, *, bloquear: bool = False, ambiente: str | None = None
) -> Documento:
    """Con `ambiente`, el filtro va en la propia consulta: un documento de otro ambiente no se lee y se
    responde como inexistente."""
    consulta = select(Documento).where(Documento.id == documento_id).execution_options(populate_existing=True)
    if ambiente is not None:
        consulta = consulta.where(Documento.ambiente == ambiente)
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


async def auditar_aparte(
    db: AsyncSession, tipo: TipoEventoAuditoria, detalle: str, usuario_id: uuid.UUID | None, *, resultado: ResultadoAuditoria
) -> bool:
    """Registra un evento de auditoría en su propia transacción. Nunca lanza: devuelve si se guardó.
    El detalle solo lleva identificadores y códigos (jamás contenido del documento ni credenciales)."""
    try:
        registrar_evento(db, tipo, detalle[:500], usuario_id, resultado=resultado)
        await db.commit()
        return True
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("No se pudo registrar el evento de auditoría de la ingesta (%s).", type(exc).__name__)
        return False


# --- Estado duradero del coordinador: progreso, intento vectorial, análisis y publicación --------------

def _condiciones_ejecutor(token: uuid.UUID) -> list:
    return [
        Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO,
        Documento.ejecucion_token == _exigir_token(token),
    ]


def _entero_no_negativo(nombre: str, valor: object) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise ValueError(f"{nombre} debe ser un entero no negativo.")
    return valor


def _json_seguro(nombre: str, valor: object) -> object:
    """Devuelve `valor` si es serializable en JSON estricto (sin NaN/Infinity); si no, ValueError."""
    try:
        json.dumps(valor, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre} debe ser JSON estricto (sin NaN, Infinity ni objetos no serializables).") from None
    return valor
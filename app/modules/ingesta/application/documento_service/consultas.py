"""Estado y progreso persistidos de una operación, y consultas de documentos."""

import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import NotFoundError
from app.models import Empresa
from app.models.documento_ingesta import Documento, EstadoCompensacion, EstadoProcesamiento
from app.modules.ingesta.domain.almacenamiento import validar_ambiente
from ._comun import _cargar, _terminar_lectura

class EstadoProgreso(str, enum.Enum):
    """Estado de la OPERACIÓN (no solo del documento): `COMPLETADO` exige los fragmentos publicados."""

    EN_PROCESO = "EN_PROCESO"
    PUBLICACION_PENDIENTE = "PUBLICACION_PENDIENTE"  # COMPLETADO transaccional, vectores aún sin publicar
    COMPLETADO = "COMPLETADO"
    FALLIDO_LIMPIEZA_PENDIENTE = "FALLIDO_LIMPIEZA_PENDIENTE"
    FALLIDO = "FALLIDO"


@dataclass(frozen=True)
class ProgresoIngesta:
    """Contrato de progreso persistente, por identificador de operación (`documento_id`). Sin porcentajes:
    `fragmentos_total` es None hasta que se conoce. `finalizado` solo es verdadero con los fragmentos
    publicados; mientras la publicación esté pendiente `resultado_analisis` no se anuncia. `codigo_error`
    es un código estable y seguro (nunca texto del documento ni respuestas de proveedores)."""

    documento_id: uuid.UUID
    estado: EstadoProgreso
    etapa: str
    fragmentos_procesados: int
    fragmentos_total: int | None
    advertencias: tuple
    codigo_error: str | None
    resultado_analisis: str | None
    finalizado: bool
    actualizado_en: datetime


def estado_de_documento(documento: Documento) -> tuple[EstadoProgreso, str | None, str | None]:
    """(estado de la OPERACIÓN, código de error seguro, resultado del análisis) de un documento. Un
    COMPLETADO transaccional con publicación vectorial pendiente NO es un éxito: su resultado no se anuncia."""
    if documento.estado_procesamiento is EstadoProcesamiento.FALLIDO:
        estado = (
            EstadoProgreso.FALLIDO_LIMPIEZA_PENDIENTE
            if documento.estado_compensacion is EstadoCompensacion.PENDIENTE
            else EstadoProgreso.FALLIDO
        )
        return estado, documento.motivo_fallo, None
    if documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO:
        if documento.vector_publicado_en is None:
            return EstadoProgreso.PUBLICACION_PENDIENTE, documento.vector_ultimo_error, None
        return EstadoProgreso.COMPLETADO, None, (
            documento.resultado_analisis.value if documento.resultado_analisis else None
        )
    return EstadoProgreso.EN_PROCESO, None, None


def progreso_de_documento(documento: Documento) -> ProgresoIngesta:
    estado, codigo, resultado = estado_de_documento(documento)
    return ProgresoIngesta(
        documento_id=documento.id,
        estado=estado,
        etapa=documento.etapa_actual,
        fragmentos_procesados=documento.fragmentos_procesados,
        fragmentos_total=documento.fragmentos_total,
        advertencias=tuple(documento.advertencias or ()),
        codigo_error=codigo,
        resultado_analisis=resultado,
        finalizado=estado is EstadoProgreso.COMPLETADO,
        actualizado_en=documento.progreso_actualizado_en,
    )


async def obtener_progreso(
    db: AsyncSession, documento_id: uuid.UUID, *, ambiente: str | None = None
) -> ProgresoIngesta:
    """Lee el progreso persistido de una operación. No autoriza: el endpoint debe hacerlo (solo SuperAdmin).
    Con `ambiente`, un documento de otro ambiente es inexistente (no se lee)."""
    documento = await _cargar(db, documento_id, ambiente=ambiente)
    await _terminar_lectura(db)
    return progreso_de_documento(documento)


async def cargar_documento(db: AsyncSession, documento_id: uuid.UUID, *, ambiente: str) -> Documento:
    """El documento DEL ambiente (404 si no existe en él). Cierra la lectura con commit: no expira el objeto."""
    documento = await _cargar(db, documento_id, ambiente=ambiente)
    await _terminar_lectura(db)
    return documento


async def exigir_documento_del_ambiente(db: AsyncSession, documento_id: uuid.UUID, ambiente: str) -> None:
    """NotFoundError si el documento no existe EN ese ambiente. Solo consulta el identificador filtrando por
    ambiente: no lee ni modifica documentos de otros ambientes."""
    encontrado = (
        await db.execute(select(Documento.id).where(Documento.id == documento_id, Documento.ambiente == ambiente))
    ).scalar_one_or_none()
    await _terminar_lectura(db)
    if encontrado is None:
        raise NotFoundError("DOCUMENT_NOT_FOUND", "El documento no existe.")


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
    ids = list(documento_ids)
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

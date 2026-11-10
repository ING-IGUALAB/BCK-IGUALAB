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
from app.models.documento_ingesta import Documento, EstadoCompensacion, EstadoProcesamiento, EtapaIngesta
from app.modules.ingesta.domain.almacenamiento import validar_ambiente
from ._comun import _cargar, _terminar_lectura

class EstadoProgreso(str, enum.Enum):
    """Estado de la OPERACIÓN (no solo del documento): `COMPLETADO` exige los fragmentos publicados."""

    EN_PROCESO = "EN_PROCESO"
    CONFIRMACION_PENDIENTE = "CONFIRMACION_PENDIENTE"  # análisis listo; espera Continuar/Cancelar del SuperAdmin
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
    COMPLETADO transaccional con publicación vectorial pendiente NO es un éxito: su resultado no se anuncia.
    Un EN_PROCESO en etapa `CONFIRMANDO` es `CONFIRMACION_PENDIENTE` (doble check): el análisis ya corrió
    y su resultado se anuncia desde `documentos.analisis` para alimentar el resumen del SuperAdmin."""
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
    if documento.etapa_actual == EtapaIngesta.CONFIRMANDO.value:
        # NO se lee `analisis` aquí: los listados lo traen diferido (`defer`) y forzar su carga
        # en un contexto asíncrono sin greenlet revienta. El resultado del doble check lo expone
        # `operacion_service.construir_vista` leyendo el JSONB completo del documento.
        return EstadoProgreso.CONFIRMACION_PENDIENTE, None, None
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

def _condiciones_de_recuperacion(ambiente: str) -> list:
    """ÚNICA definición de qué documentos pueden aparecer en una recuperación (chat RAG): del ambiente, `COMPLETADO` con análisis
    ejecutado (`resultado_analisis` no nulo; incluye OBSERVADO), NO deshabilitado y de una empresa ACTIVA. La comparten
    `documentos_recuperables` (comprobación posterior) y `documentos_elegibles_para_consulta` (filtro previo a la búsqueda)."""
    return [
        Documento.ambiente == ambiente,
        Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
        Documento.resultado_analisis.is_not(None),
        Documento.deshabilitado_en.is_(None),
        Empresa.activa.is_(True),
    ]


async def documentos_recuperables(
    db: AsyncSession, *, ambiente: str, documento_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """De los `documento_ids` dados, los que SÍ pueden aparecer en una recuperación: documento
    `COMPLETADO` con análisis ejecutado (`resultado_analisis` no nulo; incluye OBSERVADO), NO deshabilitado, de una empresa
    ACTIVA, en ese ambiente.

    Es la mitad transaccional del contrato de visibilidad: un fragmento está `publicado` en la base
    vectorial, pero esa marca NO sustituye esta comprobación (las bases no comparten transacción). Quien
    recupere debe descartar los fragmentos cuyo `documento_id` no esté en el resultado. Se conserva DESPUÉS de la búsqueda aunque
    esta ya se haya limitado a `documentos_elegibles_para_consulta`: un documento puede deshabilitarse entre las dos lecturas.
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
            .where(*_condiciones_de_recuperacion(ambiente), Documento.id.in_(ids[inicio:inicio + 500]))
        )
        recuperables.update((await db.execute(consulta)).scalars().all())
    await _terminar_lectura(db)
    return recuperables


async def documentos_elegibles_para_consulta(
    db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int
) -> list[uuid.UUID]:
    """Identificadores de los documentos de la empresa y el año que pueden alimentar el chat (mismas condiciones que
    `documentos_recuperables`: completados, con análisis, NO deshabilitados, de empresa activa).

    Sirve para FILTRAR ANTES de buscar: la base vectorial no sabe qué documentos se deshabilitaron, y si la búsqueda los devolviera
    después de ordenar y limitar, sus fragmentos podrían ocupar los primeros lugares y dejar fuera la evidencia vigente. A lo sumo hay
    un documento elegible por tipo (la reserva es única por empresa, año y tipo), así que la lista es pequeña. Orden estable."""
    validar_ambiente(ambiente)
    if not isinstance(empresa_id, uuid.UUID):
        raise ValueError("empresa_id debe ser un UUID.")
    consulta = (
        select(Documento.id)
        .join(Empresa, Empresa.id == Documento.empresa_id)
        .where(*_condiciones_de_recuperacion(ambiente), Documento.empresa_id == empresa_id, Documento.anio == anio)
        .order_by(Documento.completado_en, Documento.id)
    )
    elegibles = list((await db.execute(consulta)).scalars().all())
    await _terminar_lectura(db)
    return elegibles

"""Recuperación de operaciones abandonadas, compensaciones y publicaciones pendientes."""

from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import ConflictError, ExternalServiceError
from app.models.documento_ingesta import Documento, EstadoCompensacion, EstadoProcesamiento
from app.modules.ingesta.domain.almacenamiento import AlmacenOriginales
from ._comun import _ahora, _exigir_instante, _terminar_lectura
from .procesamiento import fallar_documento
from .compensacion import compensar_documento
from .publicacion import publicar_en_base_vectorial

# --- Recuperación ---------------------------------------------------------------------

@dataclass(frozen=True)
class ResumenRecuperacion:
    abandonados: int  # reservas EN_PROCESO marcadas como fallidas
    compensados: int  # compensaciones que llegaron a COMPLETADA
    pendientes: int  # compensaciones que siguen PENDIENTE
    publicados: int = 0  # publicaciones vectoriales pendientes que se completaron
    publicaciones_pendientes: int = 0  # publicaciones vectoriales que siguen pendientes


async def _marcar_abandonados(db: AsyncSession, almacen: AlmacenOriginales, limite: int, ahora: datetime) -> int:
    """Falla las operaciones EN_PROCESO cuya vigencia venció. Devuelve cuántas marcó."""
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
    return abandonados


async def _compensar_pendientes(
    db: AsyncSession, almacen: AlmacenOriginales, limite: int, vectorial: AsyncSession | None
) -> tuple[int, int]:
    """Reintenta las compensaciones PENDIENTE. Devuelve (completadas, que siguen pendientes)."""
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
            resultado = await compensar_documento(db, almacen, documento_id, vectorial=vectorial)
        except ConflictError:
            continue
        if resultado is EstadoCompensacion.COMPLETADA:
            compensados += 1
        else:
            pendientes += 1
    return compensados, pendientes


async def _publicar_pendientes(
    db: AsyncSession, almacen: AlmacenOriginales, limite: int, vectorial: AsyncSession
) -> tuple[int, int]:
    """Repite la publicación vectorial de los COMPLETADOS cuya publicación quedó pendiente.
    Devuelve (publicados, que siguen pendientes)."""
    ids_publicacion = (
        await db.execute(
            select(Documento.id)
            .where(
                Documento.ambiente == almacen.ambiente,
                Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
                Documento.vector_publicado_en.is_(None),
                Documento.vector_escritura_intentada_en.is_not(None),
            )
            .order_by(Documento.completado_en)
            .limit(limite)
        )
    ).scalars().all()
    await _terminar_lectura(db)
    publicados = publicaciones_pendientes = 0
    for documento_id in ids_publicacion:
        try:
            await publicar_en_base_vectorial(db, vectorial, documento_id)
        except ConflictError:
            continue
        except ExternalServiceError:
            publicaciones_pendientes += 1
            continue
        publicados += 1
    return publicados, publicaciones_pendientes


async def recuperar_documentos_pendientes(
    db: AsyncSession,
    almacen: AlmacenOriginales,
    *,
    limite: int,
    ahora: datetime | None = None,
    vectorial: AsyncSession | None = None,
) -> ResumenRecuperacion:
    """Marca como fallidas las operaciones `EN_PROCESO` cuya VIGENCIA venció y reintenta las
    compensaciones `PENDIENTE`, solo del ambiente del almacén.

    `vectorial` (sesión de la base vectorial, sin transacción abierta) es OBLIGATORIA para limpiar los
    documentos que escribieron vectores: con ella la compensación es conjunta (ver `compensar_documento`);
    sin ella, esos documentos siguen PENDIENTES con la reserva activa. Con ella también se REPITE la
    publicación vectorial de los documentos COMPLETADOS cuya publicación quedó pendiente, sin volver a
    generar embeddings ni a analizar.

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

    abandonados = await _marcar_abandonados(db, almacen, limite, ahora)
    compensados, pendientes = await _compensar_pendientes(db, almacen, limite, vectorial)
    publicados = publicaciones_pendientes = 0
    if vectorial is not None:
        publicados, publicaciones_pendientes = await _publicar_pendientes(db, almacen, limite, vectorial)
    return ResumenRecuperacion(abandonados, compensados, pendientes, publicados, publicaciones_pendientes)

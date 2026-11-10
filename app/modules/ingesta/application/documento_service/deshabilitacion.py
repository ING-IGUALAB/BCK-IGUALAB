"""Deshabilitación administrativa de un documento COMPLETADO y PUBLICADO (ACT-02).

QUÉ ES. Retirar un documento ya incorporado al corpus (p. ej. una carga con metadatos equivocados) para poder reemplazarlo. NO es un
borrado ni un estado de procesamiento: el documento sigue `COMPLETADO` y se conservan el original (MinIO), los fragmentos (base
vectorial) y las referencias históricas (validaciones y reportes ya emitidos). Se representa aparte, con `deshabilitado_en`,
`deshabilitado_por` y `motivo_deshabilitacion`.

QUÉ HACE, EN UNA SOLA TRANSACCIÓN: (1) toma la protección común del origen del análisis de su empresa y año
(`coordinacion.proteger_origen_de_analisis`), (2) ejecuta UN `UPDATE` condicional que registra actor, instante y motivo y LIBERA
`reserva_activa` (con ello el cupo empresa/año/tipo y el SHA-256 quedan libres para el reemplazo, porque los dos índices únicos son
parciales sobre `reserva_activa`), y (3) registra el evento de auditoría. Todo o nada.

POR QUÉ LA PROTECCIÓN COMÚN. Los documentos elegibles forman el origen de las validaciones y de las emisiones de reportes. Cambiar ese
conjunto es lo mismo que confirmar `vector_publicado_en` (`publicacion._confirmar_publicacion`): se excluye con una validación o una
emisión en curso del mismo año, que terminan antes o ven el documento ya retirado. La validación anterior pierde vigencia sin tocarla
(la vigencia se calcula al consultar con la huella de los documentos elegibles) y el PDF emitido no se modifica. Orden de bloqueos:
empresa (compartido) → control (exclusivo) → fila del documento, el único de todos los caminos (nadie toma antes la fila).

IDEMPOTENCIA. El `UPDATE` exige `deshabilitado_en IS NULL`: un reintento (o una segunda solicitud simultánea) no encuentra la fila,
no sobrescribe el motivo original, no escribe otro evento de auditoría y responde con el estado ya registrado.

NO HAY REACTIVACIÓN. Nada de este paquete devuelve `reserva_activa` a verdadero en un COMPLETADO: la recuperación solo toma `EN_PROCESO`,
`FALLIDO` con limpieza pendiente y `COMPLETADO` con la publicación vectorial SIN confirmar (un documento deshabilitado ya la tiene).
"""

import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessValidationError, ConflictError
from app.models import ResultadoAuditoria, TipoEventoAuditoria
from app.models.documento_ingesta import MAXIMO_MOTIVO_DESHABILITACION, Documento, EstadoProcesamiento
from app.modules.analisis_gri.application.coordinacion import proteger_origen_de_analisis
from app.modules.auditoria.service import registrar_evento
from app.modules.ingesta.domain.almacenamiento import validar_ambiente
from ._comun import _ahora, _cargar, _terminar_lectura, _transicion
from .consultas import estado_de_documento

_MAXIMO_DETALLE_AUDITORIA = 500  # `auditoria.detalle` es VARCHAR(500)


@dataclass(frozen=True)
class ResultadoDeshabilitacion:
    """El documento tal como quedó registrado. `ya_estaba_deshabilitado` es verdadero si esta llamada NO cambió nada (reintento o
    solicitud simultánea): el motivo, el actor y el instante son los ORIGINALES."""

    documento: Documento
    ya_estaba_deshabilitado: bool


def validar_motivo(motivo: object) -> str:
    """Motivo sin espacios sobrantes, no vacío, de a lo sumo `MAXIMO_MOTIVO_DESHABILITACION` caracteres y sin caracteres de control
    (salvo salto de línea y tabulación): un NUL haría fallar la base con un error interno."""
    if not isinstance(motivo, str):
        raise BusinessValidationError("INVALID_DISABLE_REASON", "El motivo de la deshabilitación debe ser un texto.")
    limpio = motivo.strip()
    if not limpio:
        raise BusinessValidationError("INVALID_DISABLE_REASON", "El motivo de la deshabilitación es obligatorio.")
    if len(limpio) > MAXIMO_MOTIVO_DESHABILITACION:
        raise BusinessValidationError(
            "INVALID_DISABLE_REASON",
            f"El motivo de la deshabilitación admite como máximo {MAXIMO_MOTIVO_DESHABILITACION} caracteres.",
        )
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in limpio):
        raise BusinessValidationError("INVALID_DISABLE_REASON", "El motivo de la deshabilitación contiene caracteres no válidos.")
    return limpio


def _puede_deshabilitarse(documento: Documento) -> bool:
    return documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO and documento.vector_publicado_en is not None


def _no_deshabilitable(documento: Documento) -> ConflictError:
    estado, _, _ = estado_de_documento(documento)
    return ConflictError(
        "DOCUMENT_NOT_DISABLEABLE",
        "Solo se puede deshabilitar un documento completado y publicado; una carga no finalizada no se cancela desde aquí.",
        details={"estado": estado.value},
    )


def _detalle_de_auditoria(documento: Documento, motivo: str) -> str:
    base = (
        f"Documento deshabilitado; documento_id={documento.id}; empresa_id={documento.empresa_id}; anio={documento.anio}; "
        f"tipo={documento.tipo.value}; motivo="
    )
    return (base + motivo)[:_MAXIMO_DETALLE_AUDITORIA]


async def deshabilitar_documento(
    db: AsyncSession, documento_id: uuid.UUID, *, ambiente: str, usuario_id: uuid.UUID, motivo: str
) -> ResultadoDeshabilitacion:
    """Deshabilita el documento del `ambiente` (uno de otro ambiente es inexistente: 404) y libera su reserva.

    `usuario_id` debe ser el de la sesión validada, nunca el del payload; el permiso (solo SuperAdmin) lo exige el router.
    409 `DOCUMENT_NOT_DISABLEABLE` si no está COMPLETADO y PUBLICADO. Un documento ya deshabilitado no es un error: se devuelve tal
    como está registrado, sin tocarlo y sin auditar de nuevo."""
    motivo = validar_motivo(motivo)
    validar_ambiente(ambiente)
    documento = await _cargar(db, documento_id, ambiente=ambiente)  # sin bloqueo: la fila se toma DESPUÉS de la protección del origen
    await _terminar_lectura(db)
    if documento.deshabilitado:
        return ResultadoDeshabilitacion(documento, ya_estaba_deshabilitado=True)
    if not _puede_deshabilitarse(documento):
        raise _no_deshabilitable(documento)

    try:
        await proteger_origen_de_analisis(db, ambiente=ambiente, empresa_id=documento.empresa_id, anio=documento.anio)
        ahora = _ahora()
        cambiado = await _transicion(
            db,
            documento_id,
            [
                Documento.ambiente == ambiente,
                Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
                Documento.vector_publicado_en.is_not(None),
                Documento.deshabilitado_en.is_(None),
            ],
            {
                "deshabilitado_en": ahora,
                "deshabilitado_por": usuario_id,
                "motivo_deshabilitacion": motivo,
                "reserva_activa": False,
                "progreso_actualizado_en": ahora,
            },
        )
        if cambiado:
            registrar_evento(
                db, TipoEventoAuditoria.INGESTA_DATOS, _detalle_de_auditoria(documento, motivo), usuario_id,
                resultado=ResultadoAuditoria.EXITOSO,
            )
            await db.commit()
        else:
            await db.rollback()  # libera los bloqueos: otra solicitud ganó, o el documento dejó de ser deshabilitable
    except BaseException:
        await db.rollback()
        raise

    actual = await _cargar(db, documento_id, ambiente=ambiente)
    await _terminar_lectura(db)
    if cambiado or actual.deshabilitado:
        return ResultadoDeshabilitacion(actual, ya_estaba_deshabilitado=not cambiado)
    raise _no_deshabilitable(actual)

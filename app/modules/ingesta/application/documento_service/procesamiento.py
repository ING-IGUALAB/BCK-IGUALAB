"""Fallo, progreso, análisis y validación del análisis de un documento."""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import BusinessValidationError, es_codigo_valido
from app.models import TipoEventoAuditoria
from app.models.documento_ingesta import Documento, EstadoCompensacion, EstadoProcesamiento, EtapaIngesta, ResultadoAnalisis
from ._comun import _ahora, _cargar, _condiciones_ejecutor, _conflicto_de_estado, _entero_no_negativo, _exigir_instante, _exigir_token, _json_seguro, _terminar_lectura, _transicion, auditar_aparte

# Tope del análisis serializado (JSON) que se persiste en `documentos.analisis`.
MAXIMO_BYTES_ANALISIS = 32 * 1024 * 1024
MAXIMO_ADVERTENCIAS_PROGRESO = 100


# --- Fallo y compensación ------------------------------------------------------------

async def fallar_documento(
    db: AsyncSession,
    documento_id: uuid.UUID,
    motivo: str,
    *,
    requiere_compensacion: bool | None = None,
    token: uuid.UUID | None = None,
    vencido_antes_de: datetime | None = None,
) -> Documento:
    """Marca `FALLIDO` un documento `EN_PROCESO` y ROTA su token de ejecución.

    `motivo` es un código (p. ej. `STORAGE_ERROR`), nunca texto libre. Con compensación
    pendiente la reserva se CONSERVA; solo se libera de inmediato si no hay nada
    externo que limpiar. `None` lo deduce: hay algo que limpiar si ya se registró un
    intento de subida. Con `False` explícito, un intento registrado es un conflicto.

    Quien llama debe demostrar su derecho: el ejecutor, con su `token`; la recuperación, con
    `vencido_antes_de` (solo actúa si la vigencia ya venció en ese instante). Exactamente uno.
    """
    if not es_codigo_valido(motivo):
        raise ValueError("El motivo del fallo debe ser un código en mayúsculas, no texto libre.")
    if (token is None) == (vencido_antes_de is None):
        raise ValueError("Indique exactamente uno: el token del ejecutor o `vencido_antes_de`.")
    ahora = _ahora()
    comunes = {
        "estado_procesamiento": EstadoProcesamiento.FALLIDO,
        "motivo_fallo": motivo,
        "fallido_en": ahora,
        "ejecucion_token": uuid.uuid4(),  # el ejecutor anterior ya no puede publicar
        # Un intento fallido no conserva resultados parciales del análisis (contienen citas del documento).
        "analisis": None,
        "progreso_actualizado_en": ahora,
    }
    propiedad = [Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO]
    if token is not None:
        propiedad.append(Documento.ejecucion_token == _exigir_token(token))
    else:
        propiedad.append(Documento.ejecucion_vigente_hasta < _exigir_instante(vencido_antes_de))
    liberado = False
    if requiere_compensacion is not True:
        liberado = await _transicion(
            db,
            documento_id,
            [
                *propiedad,
                Documento.almacenamiento_intentado_en.is_(None),
                Documento.vector_escritura_intentada_en.is_(None),
            ],
            {**comunes, "estado_compensacion": EstadoCompensacion.NINGUNA, "reserva_activa": False},
        )
    if not liberado:
        if requiere_compensacion is False:
            await db.rollback()
            raise _conflicto_de_estado()
        if not await _transicion(
            db, documento_id, propiedad, {**comunes, "estado_compensacion": EstadoCompensacion.PENDIENTE}
        ):
            await db.rollback()
            raise _conflicto_de_estado()
    await db.commit()
    documento = await _cargar(db, documento_id)
    usuario_id = documento.usuario_id
    await _terminar_lectura(db)
    # Auditoría del fallo en una transacción PROPIA y de mejor esfuerzo: el estado FALLIDO (y su
    # compensación) es lo prioritario y ya es duradero; si la auditoría no se puede escribir, el fallo
    # no se revierte. Actor: el dueño de la ingesta; origen: quien detectó el fallo.
    await auditar_aparte(
        db,
        TipoEventoAuditoria.INGESTA_DATOS,
        f"Ingesta fallida; documento_id={documento_id}; motivo={motivo}; "
        f"origen={'recuperacion' if vencido_antes_de is not None else 'ejecutor'}",
        usuario_id,
    )
    return documento


def _problemas_de_estructura(analisis: dict) -> list[str]:
    problemas = []
    if analisis.get("resultado") not in {r.value for r in ResultadoAnalisis}:
        problemas.append("resultado")
    version = analisis.get("version_catalogo")
    if not isinstance(version, str) or not version.strip():
        problemas.append("version_catalogo")
    for campo in ("motivos", "gri", "sanciones", "advertencias"):
        if not isinstance(analisis.get(campo), list):
            problemas.append(campo)
    return problemas


def _problemas_de_tamano(analisis: dict) -> list[str]:
    try:
        tamano = len(json.dumps(analisis, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ["no_serializable"]
    return ["demasiado_grande"] if tamano > MAXIMO_BYTES_ANALISIS else []


def validar_analisis(analisis: object) -> dict:
    """Valida la estructura del análisis serializado (`ResultadoAnalisisIngesta.a_dict()`) antes de
    persistirlo y lo devuelve. Lanza `BusinessValidationError` (`INVALID_ANALYSIS`) sin repetir su contenido.
    La coincidencia con `resultado_analisis` se exige al completar y la impone un CHECK en PostgreSQL."""
    if not isinstance(analisis, dict):
        problemas = ["no_es_objeto"]
    else:
        problemas = _problemas_de_estructura(analisis)
        if not problemas:  # el tamaño solo se mide si la estructura es válida
            problemas = _problemas_de_tamano(analisis)
    if problemas:
        raise BusinessValidationError(
            "INVALID_ANALYSIS",
            "El análisis no tiene la estructura esperada y no se persiste.",
            details={"campos": problemas},
        )
    return analisis


async def registrar_progreso(
    db: AsyncSession,
    documento_id: uuid.UUID,
    *,
    token: uuid.UUID,
    etapa: EtapaIngesta | None = None,
    fragmentos_procesados: int | None = None,
    fragmentos_total: int | None = None,
    advertencias: Sequence[dict] | None = None,
) -> None:
    """Persiste el progreso REAL de la operación (etapa, contadores, advertencias). Solo lo escribe el
    ejecutor que posee la operación (`token`): si la perdió, conflicto y debe detenerse. Los contadores son
    absolutos y `fragmentos_procesados` no puede superar `fragmentos_total` (CHECK). No hay porcentajes."""
    valores: dict = {"progreso_actualizado_en": _ahora()}
    if etapa is not None:
        if not isinstance(etapa, EtapaIngesta):
            raise ValueError("etapa debe ser una EtapaIngesta.")
        valores["etapa_actual"] = etapa.value
    if fragmentos_procesados is not None:
        valores["fragmentos_procesados"] = _entero_no_negativo("fragmentos_procesados", fragmentos_procesados)
    if fragmentos_total is not None:
        valores["fragmentos_total"] = _entero_no_negativo("fragmentos_total", fragmentos_total)
    if advertencias is not None:
        lista = list(advertencias)[:MAXIMO_ADVERTENCIAS_PROGRESO]
        valores["advertencias"] = _json_seguro("advertencias", lista)
    try:
        registrado = await _transicion(db, documento_id, _condiciones_ejecutor(token), valores)
        if not registrado:
            await db.rollback()
            raise _conflicto_de_estado()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError("El progreso es inconsistente (procesados > total).") from None


async def persistir_analisis(
    db: AsyncSession, documento_id: uuid.UUID, *, token: uuid.UUID, analisis: dict
) -> None:
    """Guarda el análisis completo (`documentos.analisis`) y sus advertencias (progreso) de un documento
    `EN_PROCESO` ya indexado. `resultado_analisis` NO se toca aquí: lo fija la finalización
    (`publicar_documento`), que exige que ambas representaciones coincidan."""
    analisis = validar_analisis(analisis)
    advertencias = list(analisis["advertencias"])[:MAXIMO_ADVERTENCIAS_PROGRESO]
    if not await _transicion(
        db,
        documento_id,
        [
            *_condiciones_ejecutor(token),
            Documento.original_almacenado_en.is_not(None),
            Documento.vector_escritura_intentada_en.is_not(None),
        ],
        {"analisis": analisis, "advertencias": advertencias, "progreso_actualizado_en": _ahora()},
    ):
        await db.rollback()
        raise _conflicto_de_estado()
    await db.commit()

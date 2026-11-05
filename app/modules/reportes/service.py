"""Casos de uso de los Reportes de Prospección (Módulo 7, CU006). RF-025..027 · RN-038..041.

El reporte es un **render determinista de un snapshot** del análisis GRI ya validado (RN-040): los datos salen de
`obtener_analisis_validado` (exige validación vigente), se rellena la plantilla y se compila a PDF. El contenido de una versión
sale SIEMPRE de su snapshot/validación; una descarga histórica nunca se reconstruye con datos actuales.

VERSIONADO (LTX:RN-041 reformulada, BUG-15). Una empresa/año admite VARIAS versiones inmutables. Cada emisión crea una fila nueva
(`version` 1, 2, ...) con su PDF, su snapshot y la validación que la respalda (`validacion_id`); ninguna se modifica ni se
elimina (disparador de solo inserción, migración 0005). La vigente es la de mayor versión.

IDENTIDAD DE LA EMISIÓN = la validación. Si ya existe una versión para la validación vigente, `generar_reporte` NO crea otra: la
devuelve (`reutilizado=True`). Una validación posterior (vigente) habilita la versión siguiente. Cambiar estados GRI sin volver a
validar deja el análisis sin validación vigente y la emisión se rechaza (`ANALYSIS_NOT_VALIDATED`).

CONCURRENCIA. (1) Se captura el snapshot y se renderiza SIN ningún bloqueo. (2) Una transacción final y corta bloquea la fila de
control del análisis (la misma que toman las asignaciones de estado y las validaciones) y un candado asesor por empresa/año,
vuelve a comprobar que la validación sigue siendo la última y vigente, reutiliza la versión si otra solicitud ya la emitió y
asigna `max(version)+1` bajo el candado. Si lo anterior cambió mientras se renderizaba NO se publica nada (409
`REPORT_SOURCE_CHANGED`); si solo cambió el número candidato se vuelve a renderizar. Las restricciones únicas de la base
(`uq_reporte_validacion`, `uq_reporte_empresa_anio_version`) son la última defensa y solo ellas se traducen a conflictos.

VISTA PREVIA. Si la validación vigente YA tiene una emisión (por identificador o, en un histórico, por la compatibilidad de número y
huella del snapshot), la vista previa devuelve los bytes ALMACENADOS de esa emisión: no se compila ni se reconstruye con datos
actuales (nombre de empresa, nombres de usuarios, etc.). Si todavía no la tiene, es una vista previa CANDIDATA con el mismo
constructor que la emisión; no persiste ni reserva número, y la versión y la fecha que muestra no se prometen para una emisión futura.

LECTURAS COHERENTES. Disponibilidad, consulta de la última emisión y descarga del reporte actualizado leen la emisión, la última
validación y el análisis en UNA sola transacción de instantánea (`REPEATABLE READ`), sin bloqueos: todo lo que se evalúa
corresponde al mismo instante (el de la primera consulta de la transacción). No se promete que siga siendo lo último después de él.
La descarga lee los bytes y registra su auditoría en esa misma transacción; un rechazo no registra nada.

DETERMINISMO. Para el mismo snapshot, versión y fecha de emisión el HTML es idéntico. El PDF NO se garantiza byte a byte: el motor
puede incrustar fechas y metadatos propios. Lo que se garantiza es el contenido, no los bytes; por eso cada versión guarda y
entrega SUS bytes (y su SHA-256) sin recompilar.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.exceptions import ConflictError, NotFoundError
from app.models import Empresa, ReporteProspeccion, ResultadoAuditoria, TipoEventoAuditoria, Usuario
from app.models.analisis_gri import AnalisisGriValidacion
from app.modules.analisis_gri.application.servicio import (
    confirmar_validacion_vigente,
    obtener_analisis_validado,
    resumen_por_anio,
    ultimas_validaciones,
)
from app.modules.analisis_gri.schemas import AnalisisResponse, EstadoFlujo
from app.modules.auditoria.service import registrar_evento
from app.modules.reportes.domain.plantilla import datos_desde_analisis, renderizar
from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf
from app.modules.reportes.schemas import (
    AnioDisponible,
    DisponibilidadReportesResponse,
    ReporteEmision,
    ReporteResumen,
    ReporteVigente,
)
from app.modules.reportes.versionado import (
    Evaluacion,
    RechazoDeDescarga,
    ValidacionRef,
    accion_sugerida,
    evaluar,
    origen_verificable,
    rechazo_de_descarga,
    respalda,
)

logger = logging.getLogger("igualab.reportes")

MAX_INTENTOS_DE_VERSION = 3
RESTRICCION_UNICA_DE_VALIDACION = "uq_reporte_validacion"
RESTRICCION_UNICA_DE_VERSION = "uq_reporte_empresa_anio_version"
# Campos de PRESENTACIÓN de la pantalla de Análisis GRI (derivados de `pasajes`) que el snapshot NO guarda: así el snapshot de una
# emisión nueva conserva exactamente la forma de los anteriores y el reporte no depende de ellos.
SNAPSHOT_SIN_CITAS_AGRUPADAS = {"filas": {"__all__": {"citas_agrupadas", "total_citas_agrupadas"}}}


# --- Consultas de reportes (sin cargar el PDF) ------------------------------------------------------------------------

def _numero_en_snapshot():
    return ReporteProspeccion.snapshot["validacion"]["numero"].as_integer()


def _huella_en_snapshot():
    return ReporteProspeccion.snapshot["validacion"]["huella_documentos"].as_string()


def _consulta_de_reportes():
    """Metadatos de reportes con nombres, número de validación y si es la última versión. Nunca selecciona `pdf` ni `snapshot`."""
    r, v, otra = ReporteProspeccion, AnalisisGriValidacion, aliased(ReporteProspeccion)
    maxima = (
        select(func.max(otra.version)).where(otra.empresa_id == r.empresa_id, otra.anio == r.anio).correlate(r).scalar_subquery()
    )
    return (
        select(
            r.id, r.empresa_id, Empresa.nombre.label("empresa_nombre"), r.anio, r.version, r.validacion_id,
            func.coalesce(v.numero, _numero_en_snapshot()).label("validacion_numero"),
            _numero_en_snapshot().label("numero_snapshot"), _huella_en_snapshot().label("huella_snapshot"),
            r.generado_por, Usuario.nombre.label("generado_por_nombre"), r.sha256, r.creado_en,
            (r.version == maxima).label("es_ultima_version"),
        )
        .join(Empresa, Empresa.id == r.empresa_id)
        .join(Usuario, Usuario.id == r.generado_por)
        .outerjoin(v, v.id == r.validacion_id)
    )


_CAMPOS_RESUMEN = (
    "id", "empresa_id", "empresa_nombre", "anio", "version", "es_ultima_version", "validacion_id", "validacion_numero",
    "generado_por", "generado_por_nombre", "sha256", "creado_en",
)


def _datos_de_resumen(fila) -> dict:
    return {campo: getattr(fila, campo) for campo in _CAMPOS_RESUMEN}


def _resumen(fila) -> ReporteResumen:
    return ReporteResumen(**_datos_de_resumen(fila))


async def _reporte_por_id(db: AsyncSession, reporte_id: uuid.UUID):
    return (await db.execute(_consulta_de_reportes().where(ReporteProspeccion.id == reporte_id))).first()


async def _ultimos_reportes(db: AsyncSession, empresa_id: uuid.UUID, anios: set[int] | None = None) -> dict[int, object]:
    """Última versión emitida (la de mayor `version`) de cada año de la empresa, una fila por año."""
    r = ReporteProspeccion
    consulta = _consulta_de_reportes().where(r.empresa_id == empresa_id)
    if anios is not None:
        consulta = consulta.where(r.anio.in_(anios))
    filas = (await db.execute(consulta.order_by(r.anio, r.version.desc()))).all()
    ultimos: dict[int, object] = {}
    for fila in filas:
        ultimos.setdefault(fila.anio, fila)
    return ultimos


async def _versiones_por_anio(db: AsyncSession, empresa_id: uuid.UUID) -> dict[int, int]:
    r = ReporteProspeccion
    filas = await db.execute(select(r.anio, func.count()).where(r.empresa_id == empresa_id).group_by(r.anio))
    return {anio: total for anio, total in filas.all()}


def _validacion_ref(validacion) -> ValidacionRef | None:
    """`validacion` es una `ValidacionVista` o `ValidacionGuardada`/`ResumenAnio`; solo importan id, número y huella."""
    return None if validacion is None else ValidacionRef(validacion.id, validacion.numero, validacion.huella_documentos)


def _fila_respalda(fila, ultima: ValidacionRef | None) -> bool:
    return respalda(
        validacion_id=fila.validacion_id, numero_snapshot=fila.numero_snapshot, huella_snapshot=fila.huella_snapshot, ultima=ultima
    )


async def _reporte_de_validacion(db: AsyncSession, empresa_id: uuid.UUID, anio: int, validacion: ValidacionRef):
    """Versión que respalda la validación (por identificador o, en un reporte histórico, por número y huella del snapshot)."""
    r = ReporteProspeccion
    historico = (
        r.validacion_id.is_(None), _numero_en_snapshot() == validacion.numero, _huella_en_snapshot() == validacion.huella
    )
    consulta = (
        _consulta_de_reportes()
        .where(r.empresa_id == empresa_id, r.anio == anio)
        .where((r.validacion_id == validacion.id) | (historico[0] & historico[1] & historico[2]))
        .order_by(r.version.desc())
        .limit(1)
    )
    return (await db.execute(consulta)).first()


async def _version_maxima(db: AsyncSession, empresa_id: uuid.UUID, anio: int) -> int:
    r = ReporteProspeccion
    maximo = (await db.execute(select(func.max(r.version)).where(r.empresa_id == empresa_id, r.anio == anio))).scalar_one()
    return maximo or 0


# --- Preparación común a la vista previa y a la emisión ---------------------------------------------------------------

@dataclass(frozen=True)
class _Preparacion:
    analisis: AnalisisResponse
    validacion: ValidacionRef
    existente: object | None  # fila de la versión que ya respalda esta validación
    version: int  # la existente, o la candidata (max + 1)
    emitido_en: datetime  # la existente, o el instante de esta preparación

    def html(self) -> str:
        fecha = self.emitido_en.astimezone(timezone.utc).date().isoformat()
        return renderizar(datos_desde_analisis(self.analisis, fecha, self.version))


async def _preparar(db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int) -> _Preparacion:
    """Snapshot de la última validación VIGENTE y la versión que le corresponde. ÚNICO constructor: lo usan la vista previa y la
    emisión, por lo que ambas parten de los mismos datos y las mismas reglas. Termina su transacción de lectura al volver (no se
    renderiza con una transacción abierta)."""
    analisis = await obtener_analisis_validado(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
    validacion = _validacion_ref(analisis.validacion)
    try:
        existente = await _reporte_de_validacion(db, empresa_id, anio, validacion)
        if existente is not None:
            return _Preparacion(analisis, validacion, existente, existente.version, existente.creado_en)
        version = await _version_maxima(db, empresa_id, anio) + 1
        return _Preparacion(analisis, validacion, None, version, datetime.now(timezone.utc))
    finally:
        await db.rollback()


# --- Emisión -----------------------------------------------------------------------------------------------------------

def _nombre_de_restriccion(exc: IntegrityError) -> str | None:
    causa = getattr(exc.orig, "__cause__", None)
    return getattr(causa, "constraint_name", None)


def _origen_cambio() -> ConflictError:
    return ConflictError(
        "REPORT_SOURCE_CHANGED",
        "El análisis cambió o su validación dejó de ser la vigente mientras se generaba el reporte: no se publicó ninguna versión. "
        "Consulte el estado y vuelva a solicitar la emisión.",
    )


def _sin_numeracion() -> ConflictError:
    return ConflictError(
        "REPORT_VERSION_CONFLICT",
        "Otra emisión de la misma empresa y año asignó la versión mientras se generaba el reporte: no se publicó ninguna versión. "
        "Vuelva a solicitar la emisión.",
    )


async def _tomar_candado_de_numeracion(db: AsyncSession, empresa_id: uuid.UUID, anio: int) -> None:
    """Serializa la asignación de versión de la empresa/año hasta el fin de la transacción (candado asesor transaccional)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:clave, 0))"), {"clave": f"reportes:{empresa_id}:{anio}"})


async def _reutilizar(db: AsyncSession, existente_id: uuid.UUID) -> ReporteEmision:
    await db.rollback()
    return ReporteEmision(**_datos_de_resumen(await _reporte_por_id(db, existente_id)), reutilizado=True)


async def _publicar(
    db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int, usuario_id: uuid.UUID, prep: _Preparacion, pdf: bytes
) -> ReporteEmision | None:
    """Transacción final: comprobaciones bajo bloqueo + inserción. Devuelve la versión emitida o reutilizada, o `None` si el número
    candidato dejó de ser el siguiente (hay que volver a renderizar). Lanza `ConflictError` si el origen cambió."""
    sigue_vigente = await confirmar_validacion_vigente(
        db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, validacion_id=prep.validacion.id
    )
    await _tomar_candado_de_numeracion(db, empresa_id, anio)
    ya = await _reporte_de_validacion(db, empresa_id, anio, prep.validacion)
    if ya is not None:  # otra solicitud la emitió mientras esta renderizaba: misma validación, misma versión
        return await _reutilizar(db, ya.id)
    if not sigue_vigente:
        raise _origen_cambio()
    if await _version_maxima(db, empresa_id, anio) + 1 != prep.version:
        await db.rollback()
        return None
    reporte = ReporteProspeccion(
        empresa_id=empresa_id, anio=anio, version=prep.version, validacion_id=prep.validacion.id, generado_por=usuario_id,
        pdf=pdf, sha256=hashlib.sha256(pdf).hexdigest(), snapshot=prep.analisis.model_dump(mode="json", exclude=SNAPSHOT_SIN_CITAS_AGRUPADAS),
        creado_en=prep.emitido_en,
    )
    db.add(reporte)
    registrar_evento(
        db,
        TipoEventoAuditoria.GENERACION_REPORTE,
        f"Reporte de prospección generado (empresa={empresa_id}, anio={anio}, version={prep.version}, "
        f"validacion={prep.validacion.numero})",
        usuario_id=usuario_id,
        resultado=ResultadoAuditoria.EXITOSO,
    )
    await db.commit()
    return ReporteEmision(**_datos_de_resumen(await _reporte_por_id(db, reporte.id)), reutilizado=False)


async def _tras_violacion(db: AsyncSession, exc: IntegrityError, empresa_id: uuid.UUID, anio: int, prep: _Preparacion):
    """Traduce SOLO las dos violaciones esperadas; cualquier otra se propaga tal cual (es un defecto, no un conflicto)."""
    await db.rollback()
    nombre = _nombre_de_restriccion(exc)
    if nombre == RESTRICCION_UNICA_DE_VALIDACION:
        ya = await _reporte_de_validacion(db, empresa_id, anio, prep.validacion)
        if ya is not None:
            return await _reutilizar(db, ya.id)
    if nombre == RESTRICCION_UNICA_DE_VERSION:
        raise _sin_numeracion() from None
    raise exc


async def generar_reporte(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    usuario_id: uuid.UUID,
    compilar_pdf=compilar_html_a_pdf,
) -> ReporteEmision:
    """Emite la versión del reporte que respalda la última validación vigente, o devuelve la ya emitida para ella.

    Lanza `ConflictError("ANALYSIS_NOT_VALIDATED")` si no hay validación vigente (incluye el caso de estados cambiados sin volver a
    validar), `NotFoundError` si la empresa no existe, `ConflictError("REPORT_SOURCE_CHANGED")` si el análisis o su validación
    cambiaron durante la generación y `ConflictError("REPORT_VERSION_CONFLICT")` si la numeración no se pudo asignar. Un rechazo o un
    fallo del PDF o de la persistencia no escribe nada: las versiones anteriores quedan intactas."""
    for _ in range(MAX_INTENTOS_DE_VERSION):
        prep = await _preparar(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
        if prep.existente is not None:
            return ReporteEmision(**_datos_de_resumen(prep.existente), reutilizado=True)
        pdf = compilar_pdf(prep.html())  # sin transacción ni bloqueo abiertos
        try:
            emitido = await _publicar(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, usuario_id=usuario_id, prep=prep, pdf=pdf)
        except IntegrityError as exc:
            return await _tras_violacion(db, exc, empresa_id, anio, prep)
        except Exception:
            await db.rollback()
            raise
        if emitido is not None:
            return emitido
    raise _sin_numeracion()


async def _pdf_almacenado(db: AsyncSession, reporte_id: uuid.UUID) -> bytes:
    try:
        return (await db.execute(select(ReporteProspeccion.pdf).where(ReporteProspeccion.id == reporte_id))).scalar_one()
    finally:
        await db.rollback()


async def vista_previa(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    compilar_pdf=compilar_html_a_pdf,
) -> bytes:
    """PDF de lo que representa la validación vigente AHORA. Exige validación vigente (`ANALYSIS_NOT_VALIDATED` si no).

    - Si ya tiene una emisión (por identificador o, en un histórico, por número y huella del snapshot): devuelve exactamente los
      bytes almacenados de esa emisión; no compila ni reconstruye nada con datos actuales.
    - Si no la tiene: vista previa candidata con el mismo constructor que la emisión (versión = siguiente número, fecha = hoy); no
      persiste ni reserva número, y una emisión futura puede llevar otro número o fecha.
    Un histórico de origen no verificable no se presume equivalente a la validación: se trata como «sin emisión»."""
    prep = await _preparar(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
    if prep.existente is not None:
        return await _pdf_almacenado(db, prep.existente.id)
    return compilar_pdf(prep.html())


# --- Consulta, historial y descarga ------------------------------------------------------------------------------------

async def listar_reportes(
    db: AsyncSession,
    *,
    empresa_id: uuid.UUID | None = None,
    anio: int | None = None,
    limite: int | None = None,
    desplazamiento: int = 0,
) -> list[ReporteResumen]:
    """Historial: TODAS las versiones de todas las empresas/años (o las filtradas), la más reciente primero. No agrupa ni descarta
    versiones. Sin `limite` devuelve todo (comportamiento anterior)."""
    r = ReporteProspeccion
    consulta = _consulta_de_reportes()
    if empresa_id is not None:
        consulta = consulta.where(r.empresa_id == empresa_id)
    if anio is not None:
        consulta = consulta.where(r.anio == anio)
    consulta = consulta.order_by(r.creado_en.desc(), r.version.desc(), r.id).offset(desplazamiento)
    if limite is not None:
        consulta = consulta.limit(limite)
    filas = (await db.execute(consulta)).all()
    await db.rollback()
    return [_resumen(f) for f in filas]


async def _registrar_descarga(db: AsyncSession, reporte_id: uuid.UUID, empresa_id: uuid.UUID, anio: int, version: int, usuario_id) -> None:
    registrar_evento(
        db,
        TipoEventoAuditoria.DESCARGA,
        f"Descarga de reporte (reporte={reporte_id}, empresa={empresa_id}, anio={anio}, version={version})",
        usuario_id=usuario_id,
        resultado=ResultadoAuditoria.EXITOSO,
    )
    await db.commit()


async def obtener_pdf(db: AsyncSession, reporte_id: uuid.UUID, *, usuario_id: uuid.UUID) -> bytes:
    """Los bytes ALMACENADOS de ESA versión (histórica o no), sin recompilar (RN-041): la descarga por identificador nunca cambia
    de archivo aunque existan versiones posteriores."""
    reporte = await db.get(ReporteProspeccion, reporte_id)
    if reporte is None:
        raise NotFoundError("REPORT_NOT_FOUND", "El reporte solicitado no existe.")
    pdf = reporte.pdf
    await _registrar_descarga(db, reporte_id, reporte.empresa_id, reporte.anio, reporte.version, usuario_id)
    return pdf


@dataclass(frozen=True)
class _ContextoDeAnio:
    flujo: EstadoFlujo | None  # None: el año ya no tiene documentos elegibles
    ultima: ValidacionRef | None
    resumen: object | None  # `ResumenAnio` (None si no hay documentos elegibles)


@dataclass(frozen=True)
class _Lectura:
    """Lo leído de la empresa en UNA instantánea: reportes, años con documentos elegibles y última validación de cada año."""

    empresa: object | None
    ultimos: dict[int, object]  # última emisión de cada año
    versiones: dict[int, int]  # emisiones por año
    contextos: dict[int, _ContextoDeAnio]


async def _contextos(
    db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, *, anio: int | None, con_reporte: set[int]
) -> dict[int, _ContextoDeAnio]:
    """Flujo del análisis y última validación de los años con documentos elegibles (o solo de `anio`) y de los años con reporte,
    aunque ya no tengan documentos elegibles (en ese caso solo se conoce la última validación). No termina la transacción."""
    contextos: dict[int, _ContextoDeAnio] = {}
    for a, res in (await resumen_por_anio(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, liberar=False)).items():
        ultima = None if res.validacion_id is None else ValidacionRef(res.validacion_id, res.validacion_numero, res.validacion_huella)
        contextos[a] = _ContextoDeAnio(res.flujo, ultima, res)
    faltan = con_reporte - set(contextos)
    ultimas = await ultimas_validaciones(db, ambiente=ambiente, empresa_id=empresa_id, anios=faltan, liberar=False)
    for a in faltan:
        contextos[a] = _ContextoDeAnio(None, _validacion_ref(ultimas.get(a)), None)
    return contextos


async def _leer_origen(db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int | None) -> _Lectura:
    """Lectura COHERENTE para disponibilidad, consulta y descarga: una sola transacción de instantánea (`REPEATABLE READ`), sin
    bloqueos, de modo que la emisión elegida y la evaluación de la validación y del análisis corresponden al mismo instante.
    Deja la transacción abierta (la descarga lee los bytes y registra su auditoría en ella); quien llama la termina."""
    await db.rollback()  # transacción nueva: el nivel de aislamiento solo puede fijarse antes de la primera consulta
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    empresa = (await db.execute(select(Empresa.id, Empresa.nombre, Empresa.activa).where(Empresa.id == empresa_id))).one_or_none()
    ultimos = await _ultimos_reportes(db, empresa_id, None if anio is None else {anio})
    versiones = await _versiones_por_anio(db, empresa_id)
    contextos = await _contextos(db, ambiente, empresa_id, anio=anio, con_reporte=set(ultimos))
    return _Lectura(empresa, ultimos, versiones, contextos)


def _evaluacion(fila, contexto: _ContextoDeAnio) -> tuple[Evaluacion, bool]:
    """(evaluación, origen verificable) de una emisión (o de ninguna) frente al contexto de su año. Única forma de evaluar."""
    ultima = contexto.ultima
    if fila is None:
        verificable, respalda_ultima = False, False
    else:
        verificable = origen_verificable(
            validacion_id=fila.validacion_id, numero_snapshot=fila.numero_snapshot, huella_snapshot=fila.huella_snapshot
        )
        respalda_ultima = _fila_respalda(fila, ultima)
    evaluacion = evaluar(
        hay_reporte=fila is not None,
        origen_verificable=verificable,
        respalda_ultima=respalda_ultima,
        hay_validacion=ultima is not None,
        flujo=contexto.flujo,
    )
    return evaluacion, verificable


async def obtener_reporte_vigente(db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int) -> ReporteVigente:
    """Última versión emitida de la empresa/año con su relación con la última validación y el análisis actual, todo del MISMO
    instante. `REPORT_NOT_FOUND` si no hay ninguna. Describe también a un histórico de origen no verificable (sin fallar)."""
    try:
        lectura = await _leer_origen(db, ambiente, empresa_id, anio)
        fila = lectura.ultimos.get(anio)
        if fila is None:
            raise _sin_reportes(anio)
        return _vigente(fila, lectura.contextos[anio])
    finally:
        await db.rollback()


def _sin_reportes(anio: int) -> NotFoundError:
    return NotFoundError("REPORT_NOT_FOUND", "La empresa no tiene reportes emitidos para ese año.", details={"anio": anio})


def _vigente(fila, contexto: _ContextoDeAnio) -> ReporteVigente:
    evaluacion, verificable = _evaluacion(fila, contexto)
    ultima = contexto.ultima
    return ReporteVigente(
        **_datos_de_resumen(fila),
        ultima_validacion_id=None if ultima is None else ultima.id,
        ultima_validacion_numero=None if ultima is None else ultima.numero,
        origen_verificable=verificable,
        validacion_anterior_no_vigente=evaluacion.validacion_anterior_no_vigente,
        reporte_actualizado=evaluacion.reporte_actualizado is True,
        actualizacion_pendiente=evaluacion.actualizacion_pendiente,
        puede_generar=evaluacion.puede_generar,
        motivo_no_generar=evaluacion.motivo_no_generar,
    )


_MENSAJE_DE_RECHAZO = {
    RechazoDeDescarga.ACTUALIZACION_PENDIENTE: (
        "Existe una validación posterior a la última versión emitida: el reporte necesita actualizarse. Emita la versión nueva (si "
        "el análisis tiene validación vigente) o descargue la versión anterior por su identificador."
    ),
    RechazoDeDescarga.ORIGEN_NO_VERIFICABLE: (
        "La última versión es un reporte histórico cuyo origen no puede verificarse: no se presenta como actualizado. Descárguelo "
        "por su identificador o, si el análisis tiene validación vigente, emita una versión nueva."
    ),
    RechazoDeDescarga.ANALISIS_NO_VIGENTE: (
        "El análisis actual tiene cambios por revisar o validar (o ya no es elegible): el reporte no puede presentarse como "
        "actualizado. Revise y valide de nuevo, o descargue esta versión por su identificador."
    ),
}


def _rechazo(rechazo: RechazoDeDescarga, fila, contexto: _ContextoDeAnio) -> ConflictError:
    """409 con orientación para el consumidor y sin datos sensibles (solo identificadores, números y códigos)."""
    evaluacion, verificable = _evaluacion(fila, contexto)
    motivo = evaluacion.motivo_no_generar
    ultima = contexto.ultima
    return ConflictError(
        rechazo.value,
        _MENSAJE_DE_RECHAZO[rechazo],
        details={
            "reporte_id": str(fila.id),
            "version": fila.version,
            "ultima_validacion_numero": None if ultima is None else ultima.numero,
            "origen_verificable": verificable,
            "estado": evaluacion.estado.value,
            "puede_generar": evaluacion.puede_generar,
            "motivo_no_generar": None if motivo is None else motivo.value,
            "accion_sugerida": accion_sugerida(evaluacion).value,
        },
    )


async def obtener_pdf_vigente(
    db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int, usuario_id: uuid.UUID
) -> tuple[bytes, ReporteResumen]:
    """Bytes almacenados de la última versión emitida, SOLO si puede demostrarse que está actualizada (respalda la última
    validación y esta sigue vigente). Selección, evaluación, lectura del PDF y auditoría ocurren en UNA transacción de instantánea.

    409 `REPORT_UPDATE_PENDING` (validación posterior sin emitir), `REPORT_ORIGIN_UNVERIFIABLE` (histórico sin origen verificable) o
    `REPORT_ANALYSIS_NOT_CURRENT` (cambios por revisar o validar, o sin documentación elegible); `REPORT_NOT_FOUND` sin reportes.
    Un rechazo no registra auditoría de éxito. La versión histórica sigue disponible por su identificador."""
    try:
        lectura = await _leer_origen(db, ambiente, empresa_id, anio)
        fila = lectura.ultimos.get(anio)
        if fila is None:
            raise _sin_reportes(anio)
        contexto = lectura.contextos[anio]
        evaluacion, verificable = _evaluacion(fila, contexto)
        motivo = rechazo_de_descarga(evaluacion, origen_verificable=verificable)
        if motivo is not None:
            raise _rechazo(motivo, fila, contexto)
        pdf = (await db.execute(select(ReporteProspeccion.pdf).where(ReporteProspeccion.id == fila.id))).scalar_one()
        await _registrar_descarga(db, fila.id, fila.empresa_id, fila.anio, fila.version, usuario_id)
    except BaseException:
        await db.rollback()
        raise
    return pdf, _resumen(fila)


# --- Disponibilidad de años (consulta informativa) ------------------------------------------------------------------

def _anio_disponible(a: int, fila, contexto: _ContextoDeAnio, versiones: int) -> AnioDisponible:
    """Fila de un año construida SIEMPRE con `evaluar`: estado, puede_generar, motivo y banderas salen de la misma evaluación."""
    evaluacion, verificable = _evaluacion(fila, contexto)
    resumen = contexto.resumen
    ultima = contexto.ultima
    datos = dict(
        anio=a,
        estado=evaluacion.estado,
        puede_generar=evaluacion.puede_generar,
        motivo_no_generar=evaluacion.motivo_no_generar,
        documentos_elegibles=(0 if fila is None else None) if resumen is None else resumen.documentos,
        filas_gri=None if resumen is None else resumen.filas,
        filas_sin_estado=None if resumen is None else resumen.filas_sin_estado,
        validacion_anterior_no_vigente=evaluacion.validacion_anterior_no_vigente,
        ultima_validacion_numero=None if ultima is None else ultima.numero,
        actualizacion_pendiente=evaluacion.actualizacion_pendiente,
    )
    if fila is None:
        return AnioDisponible(**datos)
    return AnioDisponible(
        **datos,
        reporte_id=fila.id,
        reporte_version=fila.version,
        reporte_creado_en=fila.creado_en,
        reporte_validacion_numero=fila.validacion_numero,
        versiones_emitidas=versiones,
        origen_verificable=verificable,
        reporte_actualizado=evaluacion.reporte_actualizado,
    )


async def disponibilidad_de_reportes(
    db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int | None = None
) -> DisponibilidadReportesResponse:
    """Años de la empresa con documentos elegibles o con reportes emitidos, y en qué punto del flujo está cada uno, todo del MISMO
    instante (una transacción de instantánea; ver `_leer_origen`).

    El estado refleja SIEMPRE el análisis actual (un reporte anterior no lo oculta; queda como referencia con su identificador y
    versión): `REPORTE_GENERADO` solo si la última versión demostrablemente respalda la última validación y esta sigue vigente;
    `ACTUALIZACION_PENDIENTE` si hay una validación vigente posterior sin emitir; `PENDIENTE_REVISION` / `PENDIENTE_VALIDACION` /
    `SIN_CODIGOS_GRI` / `SIN_DOCUMENTACION_ELEGIBLE` según el análisis. Con `anio`, el año consultado figura siempre. Solo
    lectura, sin proveedores externos ni embeddings; el costo no crece con el número de años (consultas conjuntas)."""
    try:
        lectura = await _leer_origen(db, ambiente, empresa_id, anio)
    finally:
        await db.rollback()
    if lectura.empresa is None:
        raise NotFoundError("COMPANY_NOT_FOUND", "La empresa indicada no existe.")
    anios: dict[int, AnioDisponible] = {}
    for a, contexto in lectura.contextos.items():
        if a not in lectura.ultimos and contexto.resumen is not None:
            anios[a] = _anio_disponible(a, None, contexto, 0)
    for a, fila in lectura.ultimos.items():
        anios[a] = _anio_disponible(a, fila, lectura.contextos[a], lectura.versiones.get(a, 1))
    if anio is not None and anio not in anios:
        anios[anio] = _anio_disponible(anio, None, _ContextoDeAnio(None, None, None), 0)
    empresa = lectura.empresa
    return DisponibilidadReportesResponse(
        ambiente=ambiente, empresa_id=empresa.id, empresa_nombre=empresa.nombre, empresa_activa=empresa.activa,
        anios=[anios[a] for a in sorted(anios, reverse=True)],
    )

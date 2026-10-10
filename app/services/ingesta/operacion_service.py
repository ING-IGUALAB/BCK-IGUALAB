"""Operaciones de ingesta y consultas HTTP (vista de progreso, listado e historial, detalle).

OPERACIÓN ≠ DOCUMENTO. La operación es el identificador que el frontend recibe ANTES de enviar el archivo; el
documento (hash, empresa, año, tipo) no existe hasta que la carga lo reserva. Nada aquí crea documentos ficticios.

- `crear_operacion`: fila `CREADA`, con ambiente y actor de la sesión validada.
- `tomar_carga`: UPDATE condicional `CREADA → EN_CARGA`. Solo UNA carga puede tomar la operación; la segunda recibe
  409. Una operación es de un solo uso: tras un rechazo (`RECHAZADA`) se crea otra.
- El enlace con el documento ocurre en la transacción de la reserva (`documento_service.reservar_documento`).
- `marcar_rechazada`: UPDATE condicional `EN_CARGA → RECHAZADA`; no hace nada si el documento ya se reservó (el
  desenlace entonces lo dice el documento).
- Toda consulta filtra por ambiente DENTRO del SQL: un UUID de otro ambiente es 404 y no se lee ni se modifica.

No registra auditoría de rechazos (la hace el coordinador) ni decide permisos (el router: solo SuperAdmin).
"""
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.audit import registrar_evento
from app.exceptions import ConflictError, NotFoundError
from app.models import Empresa, TipoDocumento, TipoEventoAuditoria
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoOperacion,
    EstadoProcesamiento,
    OperacionIngesta,
)
from app.schemas_ingesta import (
    ESTADOS_TERMINALES,
    DocumentoDetalle,
    DocumentoResumen,
    ErrorOperacion,
    EstadoOperacionPublico,
    OperacionCreadaResponse,
    OperacionResponse,
    PaginaDocumentos,
)
from app.services.ingesta import documento_service as servicio
from app.services.ingesta.almacenamiento import validar_ambiente
from app.services.ingesta.documento_service import EstadoProgreso

_CODIGO_REGEX = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
MAXIMO_MENSAJE = 300
TAMANO_MAXIMO_PAGINA = 100

_MENSAJE_FALLO = "La ingesta no pudo completarse; el documento no se incorporó al corpus."
_MENSAJE_PUBLICACION = (
    "El documento se completó pero su publicación en la base vectorial sigue pendiente; reintente la publicación."
)
_MENSAJE_INTERRUMPIDA = (
    "La carga no llegó a reservar un documento en el plazo esperado; nada se incorporó al corpus. Cree otra operación."
)

# Estado de la OPERACIÓN que corresponde a cada estado de progreso del documento (mismos nombres).
_DESDE_PROGRESO = {e: EstadoOperacionPublico(e.value) for e in EstadoProgreso}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _con_zona(valor: datetime) -> datetime:
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


# --- Operaciones ---------------------------------------------------------------------------------------

async def crear_operacion(db: AsyncSession, *, usuario_id: uuid.UUID, ambiente: str) -> OperacionCreadaResponse:
    validar_ambiente(ambiente)
    operacion = OperacionIngesta(id=uuid.uuid4(), ambiente=ambiente, usuario_id=usuario_id)
    db.add(operacion)
    registrar_evento(
        db, TipoEventoAuditoria.INGESTA_DATOS, f"Operación de ingesta creada; operacion_id={operacion.id}", usuario_id
    )
    await db.commit()
    return OperacionCreadaResponse(
        operacion_id=operacion.id,
        estado=EstadoOperacionPublico.CREADA,
        creada_en=operacion.creada_en,
        ingesta_url=f"/documentos/operaciones/{operacion.id}/ingesta",
        progreso_url=f"/documentos/operaciones/{operacion.id}",
    )


async def cargar_operacion(db: AsyncSession, operacion_id: uuid.UUID, ambiente: str) -> OperacionIngesta:
    """La operación DEL ambiente; otra cosa es inexistente (el filtro va en el SQL)."""
    operacion = (
        await db.execute(
            select(OperacionIngesta)
            .where(OperacionIngesta.id == operacion_id, OperacionIngesta.ambiente == ambiente)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if operacion is None:
        raise NotFoundError("OPERATION_NOT_FOUND", "La operación de ingesta no existe.")
    return operacion


async def tomar_carga(
    db: AsyncSession, operacion_id: uuid.UUID, *, usuario_id: uuid.UUID, ambiente: str, vigencia: timedelta
) -> None:
    """Reclama la operación para UNA carga (`CREADA → EN_CARGA`, compare-and-set). Solo el actor que la creó puede
    cargar sobre ella; para cualquier otro es inexistente. Una segunda carga (concurrente o posterior) recibe 409."""
    ahora = _ahora()
    resultado = await db.execute(
        update(OperacionIngesta)
        .where(
            OperacionIngesta.id == operacion_id,
            OperacionIngesta.ambiente == ambiente,
            OperacionIngesta.usuario_id == usuario_id,
            OperacionIngesta.estado == EstadoOperacion.CREADA.value,
        )
        .values(
            estado=EstadoOperacion.EN_CARGA.value,
            carga_iniciada_en=ahora,
            carga_vigente_hasta=ahora + vigencia,
            actualizada_en=ahora,
        )
        .execution_options(synchronize_session=False)
    )
    if resultado.rowcount == 1:
        await db.commit()
        return
    await db.rollback()
    operacion = await cargar_operacion(db, operacion_id, ambiente)
    estado, creador = operacion.estado, operacion.usuario_id
    await db.commit()  # fin de la lectura (rollback expiraría el objeto)
    if creador != usuario_id:
        raise NotFoundError("OPERATION_NOT_FOUND", "La operación de ingesta no existe.")
    raise ConflictError(
        "OPERATION_ALREADY_STARTED",
        "La operación ya recibió una carga (en curso o terminada). Cree otra operación para otro archivo.",
        details={"estado": estado},
    )


async def marcar_rechazada(
    db: AsyncSession, operacion_id: uuid.UUID, ambiente: str, *, codigo: str, mensaje: str, estado_http: int
) -> bool:
    """`EN_CARGA → RECHAZADA` si la carga terminó SIN reservar documento. No toca una operación ya enlazada:
    ahí manda el documento. Devuelve si cambió la fila."""
    if not isinstance(codigo, str) or _CODIGO_REGEX.fullmatch(codigo) is None:
        codigo = "UNEXPECTED_ERROR"
    resultado = await db.execute(
        update(OperacionIngesta)
        .where(
            OperacionIngesta.id == operacion_id,
            OperacionIngesta.ambiente == ambiente,
            OperacionIngesta.estado == EstadoOperacion.EN_CARGA.value,
        )
        .values(
            estado=EstadoOperacion.RECHAZADA.value,
            codigo_error=codigo,
            mensaje_error=str(mensaje)[:MAXIMO_MENSAJE],
            estado_http=estado_http,
            actualizada_en=_ahora(),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return resultado.rowcount == 1


# --- Vista de la operación -----------------------------------------------------------------------------

def _estado_sin_documento(operacion: OperacionIngesta, ahora: datetime) -> EstadoOperacionPublico:
    estado = EstadoOperacion(operacion.estado)
    if estado is EstadoOperacion.CREADA:
        return EstadoOperacionPublico.CREADA
    if estado is EstadoOperacion.RECHAZADA:
        return EstadoOperacionPublico.RECHAZADA
    # EN_CARGA sin documento: vigente = se está validando; vencida = se muestra como interrumpida. NO se cierra:
    # una carga viva todavía puede reservar el documento (entonces pasa a CON_DOCUMENTO).
    if operacion.carga_vigente_hasta is not None and _con_zona(operacion.carga_vigente_hasta) < ahora:
        return EstadoOperacionPublico.INTERRUMPIDA
    return EstadoOperacionPublico.VALIDANDO


def _error_del_documento(estado: EstadoOperacionPublico, codigo: str | None) -> ErrorOperacion | None:
    if estado is EstadoOperacionPublico.PUBLICACION_PENDIENTE:
        return ErrorOperacion(code=codigo or "VECTOR_PUBLICATION_PENDING", message=_MENSAJE_PUBLICACION)
    if estado in (EstadoOperacionPublico.FALLIDO, EstadoOperacionPublico.FALLIDO_LIMPIEZA_PENDIENTE):
        return ErrorOperacion(code=codigo or "UNEXPECTED_ERROR", message=_MENSAJE_FALLO)
    return None


def construir_vista(operacion: OperacionIngesta, documento: Documento | None, *, ahora: datetime | None = None) -> OperacionResponse:
    ahora = ahora or _ahora()
    base = {"operacion_id": operacion.id, "creada_en": operacion.creada_en}
    if documento is None:
        estado = _estado_sin_documento(operacion, ahora)
        error = None
        if estado is EstadoOperacionPublico.RECHAZADA:
            error = ErrorOperacion(code=operacion.codigo_error, message=operacion.mensaje_error or "La carga fue rechazada.")
        elif estado is EstadoOperacionPublico.INTERRUMPIDA:
            error = ErrorOperacion(code="UPLOAD_INTERRUPTED", message=_MENSAJE_INTERRUMPIDA)
        return OperacionResponse(
            **base,
            estado=estado,
            terminal=estado in ESTADOS_TERMINALES,
            exitosa=False,
            error=error,
            actualizada_en=operacion.actualizada_en,
        )
    progreso = servicio.progreso_de_documento(documento)
    estado = _DESDE_PROGRESO[progreso.estado]
    error = _error_del_documento(estado, progreso.codigo_error)
    return OperacionResponse(
        **base,
        estado=estado,
        terminal=estado in ESTADOS_TERMINALES,
        exitosa=estado is EstadoOperacionPublico.COMPLETADO,
        etapa=progreso.etapa,
        documento_id=documento.id,
        fragmentos_procesados=progreso.fragmentos_procesados,
        fragmentos_total=progreso.fragmentos_total,
        advertencias=list(progreso.advertencias),
        resultado_analisis=progreso.resultado_analisis,
        publicacion_reintentable=estado is EstadoOperacionPublico.PUBLICACION_PENDIENTE,
        error=error,
        actualizada_en=max(progreso.actualizado_en, operacion.actualizada_en),
    )


async def obtener_vista(db: AsyncSession, operacion_id: uuid.UUID, ambiente: str) -> OperacionResponse:
    """Progreso, advertencias y desenlace REALES. 404 si la operación no existe en este ambiente."""
    operacion = await cargar_operacion(db, operacion_id, ambiente)
    documento = None
    if operacion.documento_id is not None:
        documento = await servicio.cargar_documento(db, operacion.documento_id, ambiente=ambiente)
    await db.commit()  # fin de la lectura: no deja una transacción abierta (commit: no expira los objetos)
    return construir_vista(operacion, documento)


# --- Listado e historial -------------------------------------------------------------------------------

def _condicion_de_estado(estado: EstadoProgreso):
    if estado is EstadoProgreso.EN_PROCESO:
        return Documento.estado_procesamiento == EstadoProcesamiento.EN_PROCESO
    if estado is EstadoProgreso.PUBLICACION_PENDIENTE:
        return (Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO) & Documento.vector_publicado_en.is_(None)
    if estado is EstadoProgreso.COMPLETADO:
        return (Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO) & Documento.vector_publicado_en.is_not(None)
    pendiente = Documento.estado_compensacion == EstadoCompensacion.PENDIENTE
    fallido = Documento.estado_procesamiento == EstadoProcesamiento.FALLIDO
    return fallido & (pendiente if estado is EstadoProgreso.FALLIDO_LIMPIEZA_PENDIENTE else ~pendiente)


def _resumen(documento: Documento, empresa_nombre: str, operacion_id: uuid.UUID | None) -> dict:
    estado, _, resultado = servicio.estado_de_documento(documento)
    return {
        "id": documento.id,
        "operacion_id": operacion_id,
        "empresa_id": documento.empresa_id,
        "empresa_nombre": empresa_nombre,
        "sector": documento.sector,
        "anio": documento.anio,
        "tipo": documento.tipo,
        "nombre_archivo": documento.nombre_archivo,
        "sha256": documento.sha256,
        "tamano_bytes": documento.tamano_bytes,
        "estado": estado,
        "resultado_analisis": resultado,
        "disponible_para_rag": estado is EstadoProgreso.COMPLETADO and documento.resultado_analisis is not None,
        "fragmentos_total": documento.fragmentos_total,
        "cantidad_advertencias": len(documento.advertencias or ()),
        "cargado_por": documento.usuario_id,
        "creado_en": documento.creado_en,
        "completado_en": documento.completado_en,
    }


async def listar_documentos(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID | None = None,
    anio: int | None = None,
    tipo: TipoDocumento | None = None,
    estado: EstadoProgreso | None = None,
    pagina: int = 1,
    tamano: int = 20,
) -> PaginaDocumentos:
    """Historial paginado (más recientes primero) del ambiente, filtrable por empresa, año, tipo y estado. No carga
    el análisis completo (puede ser grande): solo el resumen."""
    if pagina < 1 or not 1 <= tamano <= TAMANO_MAXIMO_PAGINA:
        raise ValueError("pagina >= 1 y 1 <= tamano <= 100.")
    filtros = [Documento.ambiente == ambiente]
    if empresa_id is not None:
        filtros.append(Documento.empresa_id == empresa_id)
    if anio is not None:
        filtros.append(Documento.anio == anio)
    if tipo is not None:
        filtros.append(Documento.tipo == tipo)
    if estado is not None:
        filtros.append(_condicion_de_estado(estado))
    total = (await db.execute(select(func.count()).select_from(Documento).where(*filtros))).scalar_one()
    filas = (
        await db.execute(
            select(Documento, Empresa.nombre, OperacionIngesta.id)
            .join(Empresa, Empresa.id == Documento.empresa_id)
            .outerjoin(OperacionIngesta, OperacionIngesta.documento_id == Documento.id)
            .where(*filtros)
            .options(defer(Documento.analisis))
            .order_by(Documento.creado_en.desc(), Documento.id.desc())
            .limit(tamano)
            .offset((pagina - 1) * tamano)
        )
    ).all()
    items = [DocumentoResumen(**_resumen(documento, nombre, operacion_id)) for documento, nombre, operacion_id in filas]
    await db.commit()
    return PaginaDocumentos(
        items=items, total=total, pagina=pagina, tamano=tamano, paginas=math.ceil(total / tamano) if total else 0
    )


async def obtener_detalle(db: AsyncSession, documento_id: uuid.UUID, ambiente: str) -> DocumentoDetalle:
    """Detalle con análisis y advertencias. 404 si el documento no existe en este ambiente."""
    fila = (
        await db.execute(
            select(Documento, Empresa.nombre, OperacionIngesta.id)
            .join(Empresa, Empresa.id == Documento.empresa_id)
            .outerjoin(OperacionIngesta, OperacionIngesta.documento_id == Documento.id)
            .where(Documento.id == documento_id, Documento.ambiente == ambiente)
            .execution_options(populate_existing=True)
        )
    ).first()
    await db.commit()
    if fila is None:
        raise NotFoundError("DOCUMENT_NOT_FOUND", "El documento no existe.")
    documento, nombre, operacion_id = fila
    progreso = servicio.progreso_de_documento(documento)
    return DocumentoDetalle(
        **_resumen(documento, nombre, operacion_id),
        etapa=progreso.etapa,
        fragmentos_procesados=progreso.fragmentos_procesados,
        advertencias=list(progreso.advertencias),
        publicacion_reintentable=progreso.estado is EstadoProgreso.PUBLICACION_PENDIENTE,
        error=_error_del_documento(_DESDE_PROGRESO[progreso.estado], progreso.codigo_error),
        analisis=documento.analisis,
        actualizado_en=progreso.actualizado_en,
    )

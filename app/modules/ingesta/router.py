"""Ingesta de documentos (Módulo 4) por HTTP. EXCLUSIVO del SuperAdmin (`SuperAdminUser`: sin sesión 401, rol
Administrador 403). El actor sale siempre de la sesión validada, nunca del payload. Contrato completo y ejemplos:
`docs/ingesta/08-contrato-http-y-despliegue.md`.

Flujo con doble check (202): `POST …/ingesta` puede responder **202** con la operación en
`CONFIRMACION_PENDIENTE` (análisis listo). El FE consulta `GET …/operaciones/{id}`, muestra la alerta
(`resumen_confirmacion`) y resuelve con `POST …/confirmar` (201, continúa 7–9) o `POST …/cancelar`
(200, FALLIDO + limpieza de residuos). Ver `docs/ingesta/09-confirmacion-y-flujo-9-pasos.md`.

Los routers solo coordinan HTTP: la lógica está en `GestorIngesta` (recursos, desconexión, recuperación),
`operacion_service` (operaciones, consultas) y `coordinador` (ingesta síncrona, confirmar/cancelar).
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import BeforeValidator, ValidationError

from app.core.dependencies import DatabaseSession, SuperAdminUser
from app.core.exceptions import ConfirmacionPendiente, ServiceUnavailableError
from app.models import TipoDocumento
from app.schemas import MetadatosIngestaRequest
from app.modules.ingesta.schemas import (
    DeshabilitarDocumentoRequest,
    DisponibilidadIngestaResponse,
    DocumentoDeshabilitadoResponse,
    DocumentoDetalle,
    OperacionCreadaResponse,
    OperacionResponse,
    PaginaDocumentos,
    ResultadoIngestaResponse,
)
from app.modules.ingesta.application import operacion_service
from app.modules.ingesta.application.documento_service import EstadoProgreso
from app.modules.ingesta.application.gestor import GestorIngesta
from app.modules.ingesta.domain.reglas import interpretar_anio

router = APIRouter(prefix="/documentos", tags=["Ingesta de Documentos"])


def obtener_gestor_ingesta(request: Request) -> GestorIngesta:
    """El gestor construido en el `lifespan`. Sin configuración, error controlado 503 (el resto de la aplicación no
    se ve afectado)."""
    gestor = getattr(request.app.state, "ingesta", None)
    if gestor is None:
        codigo, mensaje, detalles = getattr(request.app.state, "ingesta_error", None) or (
            "INGESTION_NOT_CONFIGURED",
            "La ingesta de documentos no está disponible.",
            None,
        )
        raise ServiceUnavailableError(codigo, mensaje, details=detalles)
    return gestor


# Declarada DESPUÉS de la autenticación en cada endpoint: 401/403 tienen prioridad sobre el 503.
GestorDep = Annotated[GestorIngesta, Depends(obtener_gestor_ingesta)]

_ERRORES = {
    401: {"description": "Sin sesión válida."},
    403: {"description": "Rol sin permiso (solo SuperAdmin)."},
    503: {"description": "Ingesta no configurada (componentes indicados, sin valores)."},
}


@router.post("/operaciones", response_model=OperacionCreadaResponse, status_code=201, responses=_ERRORES)
async def crear_operacion(actor: SuperAdminUser, gestor: GestorDep, db: DatabaseSession):
    """Crea una operación y devuelve su identificador ANTES de cargar el archivo. No crea ningún documento."""
    return await gestor.crear_operacion(db, usuario_id=actor.id)


@router.post(
    "/operaciones/{operacion_id}/ingesta",
    response_model=ResultadoIngestaResponse,
    status_code=201,
    responses={
        **_ERRORES,
        202: {
            "description": (
                "Doble check: el análisis terminó y la ingesta espera Continuar/Cancelar. "
                "Cuerpo = OperacionResponse en CONFIRMACION_PENDIENTE con resumen_confirmacion. "
                "Resuelva con POST …/confirmar o POST …/cancelar."
            ),
            "model": OperacionResponse,
        },
        400: {"description": "Archivo, empresa o contenido inválido (códigos específicos)."},
        404: {"description": "Operación inexistente (o de otro ambiente/actor)."},
        409: {"description": "Operación ya usada, duplicado, ejecución perdida."},
        413: {"description": "El archivo supera 50 000 000 bytes (bytes reales del archivo)."},
        422: {"description": "Metadatos inválidos (REQUEST_VALIDATION_ERROR)."},
        500: {"description": "Fallo interno del análisis."},
        502: {"description": "Fallo de un servicio externo o publicación vectorial pendiente."},
        504: {"description": "Un servicio externo no respondió a tiempo."},
    },
)
async def ingerir_documento(
    operacion_id: uuid.UUID,
    actor: SuperAdminUser,
    gestor: GestorDep,
    response: Response,
    archivo: Annotated[UploadFile, File(description="Documento Markdown (.md) en UTF-8, hasta 50 000 000 bytes.")],
    empresa_id: Annotated[uuid.UUID, Form()],
    anio: Annotated[str, Form(description="Entero entre 2000 y el año actual (America/Lima).")],
    tipo_documento: Annotated[TipoDocumento, Form()],
):
    """Espera SÍNCRONAMENTE al coordinador hasta terminar o hasta el doble check. Éxito solo si el
    documento queda COMPLETADO y PUBLICADO (201). Si el análisis dispa el doble check: **202** con la
    vista en `CONFIRMACION_PENDIENTE` (resuelva con confirmar/cancelar). Si el cliente se desconecta
    antes, la ingesta continúa y se consulta con `GET /documentos/operaciones/{operacion_id}`."""
    try:
        metadatos = MetadatosIngestaRequest(empresa_id=empresa_id, anio=anio, tipo=tipo_documento)
    except ValidationError as exc:
        # Mismo contrato 422 (REQUEST_VALIDATION_ERROR) que el resto de la API; no se devuelve `input`.
        raise RequestValidationError(
            [{"type": e["type"], "loc": ("body", *e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        ) from None
    try:
        return await gestor.ingerir(
            operacion_id=operacion_id,
            usuario_id=actor.id,
            nombre_archivo=archivo.filename,
            leer=archivo.read,
            metadatos=metadatos,
        )
    except ConfirmacionPendiente:
        # Doble check: 202 con la vista actual (no es un error del contrato de errores).
        response.status_code = 202
        async with gestor.dependencias.sesiones() as db:
            return await operacion_service.obtener_vista(db, operacion_id, gestor.ambiente)


@router.get("/operaciones/{operacion_id}", response_model=OperacionResponse, responses={**_ERRORES, 404: {}})
async def consultar_operacion(operacion_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep, db: DatabaseSession):
    """Progreso real (etapa y contadores, sin porcentajes), advertencias y desenlace. En
    `CONFIRMACION_PENDIENTE` incluye `confirmacion_pendiente=true` y `resumen_confirmacion` para el modal."""
    return await operacion_service.obtener_vista(db, operacion_id, gestor.ambiente)


@router.post(
    "/operaciones/{operacion_id}/confirmar",
    response_model=ResultadoIngestaResponse,
    status_code=201,
    responses={
        **_ERRORES,
        404: {"description": "Operación inexistente o sin documento."},
        409: {
            "description": "La operación no está en CONFIRMACION_PENDIENTE (CONFIRMATION_NOT_PENDING) o el token rotó.",
        },
        502: {"description": "Publicación vectorial pendiente tras confirmar (reintentable)."},
    },
)
async def confirmar_ingesta(operacion_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep):
    """Doble check — CONTINUAR: retoma los pasos 7–9 (completar + publicar fragmentos) sin reanalizar.
    Devuelve 201 con el resultado de la ingesta. 409 si ya no estaba en pausa."""
    return await gestor.confirmar(operacion_id)


@router.post(
    "/operaciones/{operacion_id}/cancelar",
    response_model=OperacionResponse,
    status_code=200,
    responses={
        **_ERRORES,
        404: {"description": "Operación inexistente o sin documento."},
        409: {"description": "La operación no está en CONFIRMACION_PENDIENTE (CONFIRMATION_NOT_PENDING)."},
    },
)
async def cancelar_ingesta(operacion_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep):
    """Doble check — CANCELAR: elimina los residuos del intento (fragmentos en la base vectorial y
    original en MinIO), libera la reserva (el mismo archivo/empresa/año puede reintentarse) y deja la
    operación FALLIDO con motivo `CONFIRMACION_RECHAZADA`. Devuelve 200 con la vista terminal."""
    return await gestor.cancelar(operacion_id)


@router.post(
    "/operaciones/{operacion_id}/reintentar-publicacion",
    response_model=OperacionResponse,
    responses={**_ERRORES, 404: {}, 409: {}, 502: {}, 504: {}},
)
async def reintentar_publicacion(operacion_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep):
    """Repite SOLO la publicación vectorial de una operación en PUBLICACION_PENDIENTE (idempotente). No regenera
    embeddings ni reanaliza."""
    return await gestor.reintentar_publicacion(operacion_id)


@router.get("", response_model=PaginaDocumentos, responses=_ERRORES)
async def listar_documentos(
    actor: SuperAdminUser,
    gestor: GestorDep,
    db: DatabaseSession,
    empresa_id: uuid.UUID | None = None,
    anio: Annotated[int | None, Query(ge=2000, le=9999)] = None,
    tipo: TipoDocumento | None = None,
    estado: EstadoProgreso | None = None,
    pagina: Annotated[int, Query(ge=1)] = 1,
    tamano: Annotated[int, Query(ge=1, le=operacion_service.TAMANO_MAXIMO_PAGINA)] = 20,
):
    """Historial paginado (recientes primero), filtrable por empresa, año, tipo y estado."""
    return await operacion_service.listar_documentos(
        db,
        ambiente=gestor.ambiente,
        empresa_id=empresa_id,
        anio=anio,
        tipo=tipo,
        estado=estado,
        pagina=pagina,
        tamano=tamano,
    )


@router.get(
    "/disponibilidad",
    response_model=DisponibilidadIngestaResponse,
    responses={**_ERRORES, 404: {"description": "Empresa inexistente (`COMPANY_NOT_FOUND`)."}, 422: {}},
)
async def disponibilidad_de_tipos(
    actor: SuperAdminUser,
    gestor: GestorDep,
    db: DatabaseSession,
    empresa_id: uuid.UUID,
    anio: Annotated[int, BeforeValidator(interpretar_anio), Query(description="Entero entre 2000 y el año actual (America/Lima).")],
):
    """Qué tipos de documento están DISPONIBLES, OCUPADOS o EN PROCESO para la empresa y el año, con el mismo criterio de
    reserva que la carga. Solo informativo y de solo lectura: la carga vuelve a comprobar sus reglas (y el SHA-256 del contenido)."""
    return await operacion_service.disponibilidad_de_tipos(db, ambiente=gestor.ambiente, empresa_id=empresa_id, anio=anio)


@router.post(
    "/{documento_id}/deshabilitar",
    response_model=DocumentoDeshabilitadoResponse,
    responses={
        **_ERRORES,
        404: {"description": "Documento inexistente (o de otro ambiente): `DOCUMENT_NOT_FOUND`."},
        409: {"description": "No está completado y publicado (`DOCUMENT_NOT_DISABLEABLE`)."},
        422: {"description": "Motivo ausente, vacío o demasiado largo (`REQUEST_VALIDATION_ERROR`)."},
    },
)
async def deshabilitar_documento(
    documento_id: uuid.UUID, cuerpo: DeshabilitarDocumentoRequest, actor: SuperAdminUser, gestor: GestorDep, db: DatabaseSession
):
    """Deshabilita un documento COMPLETADO y PUBLICADO: queda fuera del Análisis GRI, de los reportes nuevos y del chat, y su cupo
    empresa/año/tipo (y su SHA-256) quedan libres para un reemplazo. NO borra nada (original, fragmentos y reportes emitidos se
    conservan) ni se puede revertir. Registra actor, fecha y motivo; es idempotente (un reintento devuelve el registro original). El actor
    sale de la sesión. Una carga no finalizada no se cancela desde aquí."""
    return await operacion_service.deshabilitar_documento(
        db, documento_id, ambiente=gestor.ambiente, usuario_id=actor.id, motivo=cuerpo.motivo
    )


@router.get("/{documento_id}", response_model=DocumentoDetalle, responses={**_ERRORES, 404: {}})
async def detalle_documento(documento_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep, db: DatabaseSession):
    """Detalle con análisis (grupos GRI, sanciones, motivos) y advertencias."""
    return await operacion_service.obtener_detalle(db, documento_id, gestor.ambiente)

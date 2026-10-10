"""Ingesta de documentos (Módulo 4) por HTTP. EXCLUSIVO del SuperAdmin (`SuperAdminUser`: sin sesión 401, rol
Administrador 403). El actor sale siempre de la sesión validada, nunca del payload. Contrato completo y ejemplos:
`docs/ingesta/08-contrato-http-y-despliegue.md`.

Los routers solo coordinan HTTP: la lógica está en `GestorIngesta` (recursos, desconexión, recuperación),
`operacion_service` (operaciones, consultas) y `coordinador` (ingesta síncrona).
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.dependencies import DatabaseSession, SuperAdminUser
from app.exceptions import ServiceUnavailableError
from app.models import TipoDocumento
from app.schemas import MetadatosIngestaRequest
from app.schemas_ingesta import (
    DocumentoDetalle,
    OperacionCreadaResponse,
    OperacionResponse,
    PaginaDocumentos,
    ResultadoIngestaResponse,
)
from app.services.ingesta import operacion_service
from app.services.ingesta.documento_service import EstadoProgreso
from app.services.ingesta.gestor import GestorIngesta

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
    archivo: Annotated[UploadFile, File(description="Documento Markdown (.md) en UTF-8, hasta 50 000 000 bytes.")],
    empresa_id: Annotated[uuid.UUID, Form()],
    anio: Annotated[str, Form(description="Entero entre 2000 y el año actual (America/Lima).")],
    tipo_documento: Annotated[TipoDocumento, Form()],
):
    """Espera SÍNCRONAMENTE al coordinador hasta terminar. El sector se deriva de la empresa. Éxito solo si el
    documento queda COMPLETADO y PUBLICADO. Si el cliente se desconecta, la ingesta continúa y se consulta con
    `GET /documentos/operaciones/{operacion_id}`."""
    try:
        metadatos = MetadatosIngestaRequest(empresa_id=empresa_id, anio=anio, tipo=tipo_documento)
    except ValidationError as exc:
        # Mismo contrato 422 (REQUEST_VALIDATION_ERROR) que el resto de la API; no se devuelve `input`.
        raise RequestValidationError(
            [{"type": e["type"], "loc": ("body", *e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        ) from None
    return await gestor.ingerir(
        operacion_id=operacion_id,
        usuario_id=actor.id,
        nombre_archivo=archivo.filename,
        leer=archivo.read,
        metadatos=metadatos,
    )


@router.get("/operaciones/{operacion_id}", response_model=OperacionResponse, responses={**_ERRORES, 404: {}})
async def consultar_operacion(operacion_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep, db: DatabaseSession):
    """Progreso real (etapa y contadores, sin porcentajes), advertencias y desenlace."""
    return await operacion_service.obtener_vista(db, operacion_id, gestor.ambiente)


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


@router.get("/{documento_id}", response_model=DocumentoDetalle, responses={**_ERRORES, 404: {}})
async def detalle_documento(documento_id: uuid.UUID, actor: SuperAdminUser, gestor: GestorDep, db: DatabaseSession):
    """Detalle con análisis (grupos GRI, sanciones, motivos) y advertencias."""
    return await operacion_service.obtener_detalle(db, documento_id, gestor.ambiente)

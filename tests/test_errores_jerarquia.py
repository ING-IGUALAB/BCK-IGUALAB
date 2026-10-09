"""Mapeo de excepciones a HTTP respetando la JERARQUÍA (no el tipo exacto).

Antes, el handler buscaba `type(exc)` en el diccionario: `PublicacionVectorialPendiente` (subclase de
`ExternalServiceError`) y `AnalisisIngestaError` caían en 400. Estas pruebas recorren el contrato por HTTP y no necesitan
bases de datos.
"""
import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.exception_handlers import register_exception_handlers, status_for_exception
from app.exceptions import (
    AccountDisabledError,
    AccountLockedError,
    AppException,
    AuthenticationError,
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    ExternalServiceError,
    ExternalServiceTimeoutError,
    InternalProcessingError,
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
)
from app.request_id import RequestIDMiddleware
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta.analisis_ingesta import AnalisisIngestaError
from app.services.ingesta.coordinador import PublicacionVectorialPendiente


class ConflictoEspecifico(ConflictError):
    """Subclase propia: hereda el 409 de su padre."""


class TimeoutEspecifico(ExternalServiceTimeoutError):
    """Subclase del timeout: debe ser 504 y no 502 (su clase es más específica que ExternalServiceError)."""


class ErrorDesconocido(AppException):
    """Sin ninguna clase mapeada en su MRO: 400 (comportamiento previo)."""


def _construir(excepcion: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/falla")
    async def falla():
        raise excepcion

    @app.post("/metadatos")
    async def metadatos(carga: MetadatosIngestaRequest):
        return carga

    return app


async def _pedir(excepcion: Exception) -> httpx.Response:
    transporte = httpx.ASGITransport(app=_construir(excepcion), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://prueba") as cliente:
        return await cliente.get("/falla")


CASOS = [
    (PublicacionVectorialPendiente(uuid.uuid4(), "VECTOR_PUBLICATION_FAILED"), 502, "VECTOR_PUBLICATION_PENDING"),
    (AnalisisIngestaError("INGESTION_ANALYSIS_FAILED", "Falló el análisis.", details={"detector": "gri"}), 500, "INGESTION_ANALYSIS_FAILED"),
    (ExternalServiceTimeoutError("EMBEDDING_PROVIDER_TIMEOUT", "Plazo agotado."), 504, "EMBEDDING_PROVIDER_TIMEOUT"),
    (TimeoutEspecifico("OTRO_TIMEOUT", "Plazo agotado."), 504, "OTRO_TIMEOUT"),
    (ExternalServiceError("EMBEDDING_PROVIDER_ERROR", "Falló el proveedor."), 502, "EMBEDDING_PROVIDER_ERROR"),
    (ConflictError("DOCUMENT_ALREADY_INGESTED", "Duplicado."), 409, "DOCUMENT_ALREADY_INGESTED"),
    (ConflictoEspecifico("CONFLICTO_PROPIO", "Conflicto."), 409, "CONFLICTO_PROPIO"),
    (PayloadTooLargeError("FILE_TOO_LARGE", "Demasiado grande."), 413, "FILE_TOO_LARGE"),
    (BusinessValidationError("INVALID_FILE_TYPE", "Tipo inválido."), 400, "INVALID_FILE_TYPE"),
    (NotFoundError("DOCUMENT_NOT_FOUND", "No existe."), 404, "DOCUMENT_NOT_FOUND"),
    (AuthenticationError("INVALID_SESSION", "Sin sesión."), 401, "INVALID_SESSION"),
    (AuthorizationError("FORBIDDEN", "Sin permiso."), 403, "FORBIDDEN"),
    (AccountDisabledError("ACCOUNT_DISABLED", "Deshabilitada."), 403, "ACCOUNT_DISABLED"),
    (AccountLockedError("ACCOUNT_LOCKED", "Bloqueada."), 423, "ACCOUNT_LOCKED"),
    (InternalProcessingError("INTERNAL_PROCESSING", "Falló."), 500, "INTERNAL_PROCESSING"),
    (ServiceUnavailableError("INGESTION_NOT_CONFIGURED", "No disponible."), 503, "INGESTION_NOT_CONFIGURED"),
    (ErrorDesconocido("SIN_MAPEO", "Sin mapeo."), 400, "SIN_MAPEO"),
]


@pytest.mark.parametrize("excepcion,estado,codigo", CASOS, ids=[c[2] for c in CASOS])
async def test_cada_excepcion_responde_con_su_estado_y_su_codigo_especifico(excepcion, estado, codigo):
    assert status_for_exception(excepcion) == estado
    respuesta = await _pedir(excepcion)
    assert respuesta.status_code == estado, respuesta.text
    error = respuesta.json()["error"]
    assert error["code"] == codigo
    assert set(error) == {"code", "message", "details", "request_id"}  # contrato uniforme
    assert error["request_id"] == respuesta.headers["x-request-id"]


async def test_los_detalles_seguros_se_conservan_y_la_publicacion_pendiente_no_filtra_nada_mas():
    documento_id = uuid.uuid4()
    respuesta = await _pedir(PublicacionVectorialPendiente(documento_id, "VECTOR_PUBLICATION_FAILED"))
    assert respuesta.json()["error"]["details"] == {"documento_id": str(documento_id), "causa": "VECTOR_PUBLICATION_FAILED"}
    respuesta = await _pedir(AnalisisIngestaError("INGESTION_ANALYSIS_FAILED", "Falló.", details={"detector": "sanciones", "tipo_error": "ValueError"}))
    assert respuesta.json()["error"]["details"] == {"detector": "sanciones", "tipo_error": "ValueError"}


async def test_una_excepcion_no_prevista_sigue_siendo_500_generico_sin_detalles():
    respuesta = await _pedir(RuntimeError("texto del documento y credenciales"))
    assert respuesta.status_code == 500
    error = respuesta.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["details"] is None
    assert "credenciales" not in respuesta.text


async def test_las_cabeceras_de_la_excepcion_se_conservan():
    excepcion = AuthenticationError("INVALID_SESSION", "Sin sesión.", headers={"WWW-Authenticate": "Bearer"})
    respuesta = await _pedir(excepcion)
    assert respuesta.status_code == 401
    assert respuesta.headers["www-authenticate"] == "Bearer"


async def test_metadatos_invalidos_son_422_con_el_contrato_vigente():
    transporte = httpx.ASGITransport(app=_construir(RuntimeError()), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://prueba") as cliente:
        for carga in (
            {"empresa_id": str(uuid.uuid4()), "anio": 1999, "tipo": "MEMORIA_ANUAL"},
            {"empresa_id": str(uuid.uuid4()), "anio": 2025, "tipo": "OTRO"},
            {"empresa_id": "no-uuid", "anio": 2025, "tipo": "MEMORIA_ANUAL"},
            {"anio": 2025, "tipo": "MEMORIA_ANUAL"},
        ):
            respuesta = await cliente.post("/metadatos", json=carga)
            assert respuesta.status_code == 422, respuesta.text
            error = respuesta.json()["error"]
            assert error["code"] == "REQUEST_VALIDATION_ERROR"
            assert "input" not in str(error["details"])


def test_toda_subclase_de_una_clase_mapeada_hereda_su_estado():
    # La jerarquía manda: el orden de resolución de métodos elige la clase más específica.
    assert status_for_exception(PublicacionVectorialPendiente(uuid.uuid4(), "X")) == 502
    assert issubclass(PublicacionVectorialPendiente, ExternalServiceError)
    assert issubclass(AnalisisIngestaError, InternalProcessingError)
    assert status_for_exception(TimeoutEspecifico("A", "b")) == 504

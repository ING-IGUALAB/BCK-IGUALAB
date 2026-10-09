from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette import status
from starlette.exceptions import HTTPException as StarletteHTTPException

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

logger = logging.getLogger("igualab.errors")

_STATUS_BY_EXCEPTION: dict[type[AppException], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    AccountLockedError: status.HTTP_423_LOCKED,
    AccountDisabledError: status.HTTP_403_FORBIDDEN,
    AuthorizationError: status.HTTP_403_FORBIDDEN,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    BusinessValidationError: status.HTTP_400_BAD_REQUEST,
    PayloadTooLargeError: status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    ExternalServiceError: status.HTTP_502_BAD_GATEWAY,
    ExternalServiceTimeoutError: status.HTTP_504_GATEWAY_TIMEOUT,
    InternalProcessingError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ServiceUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}

_HTTP_ERROR_CODES = {
    status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHENTICATED",
    status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    status.HTTP_409_CONFLICT: "CONFLICT",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    status.HTTP_423_LOCKED: "ACCOUNT_LOCKED",
}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unavailable")


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content=jsonable_encoder(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "details": details,
                    "request_id": _request_id(request),
                }
            }
        ),
    )


def status_for_exception(exc: AppException) -> int:
    """Estado HTTP según la jerarquía: la clase más específica de la MRO que tenga mapeo. Buscar solo el
    tipo exacto enviaba a 400 a toda subclase (p. ej. PublicacionVectorialPendiente). Una subclase de
    ExternalServiceTimeoutError es 504 y no 502 porque su clase aparece antes en la MRO."""
    for clase in type(exc).__mro__:
        estado = _STATUS_BY_EXCEPTION.get(clase)
        if estado is not None:
            return estado
    return status.HTTP_400_BAD_REQUEST


def app_exception_handler(
    request: Request,
    exc: AppException,
) -> JSONResponse:
    status_code = status_for_exception(exc)

    logger.warning(
        "Solicitud rechazada: code=%s request_id=%s",
        exc.code,
        _request_id(request),
    )

    return _error_response(
        request,
        status_code=status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # No se devuelve `input`: podría contener una contraseña u otro dato sensible.
    details = [
        {
            "location": list(error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return _error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="REQUEST_VALIDATION_ERROR",
        message="La solicitud contiene datos inválidos.",
        details=details,
    )


def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    details = exc.detail if isinstance(exc.detail, (dict, list)) else None
    message = (
        exc.detail
        if isinstance(exc.detail, str)
        else "La solicitud no pudo completarse."
    )
    return _error_response(
        request,
        status_code=exc.status_code,
        code=_HTTP_ERROR_CODES.get(exc.status_code, "HTTP_ERROR"),
        message=message,
        details=details,
        headers=exc.headers,
    )


def database_exception_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    logger.exception(
        "Error de base de datos: request_id=%s",
        _request_id(request),
        exc_info=exc,
    )
    return _error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="Ocurrió un error interno.",
    )


def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Error inesperado: request_id=%s",
        _request_id(request),
        exc_info=exc,
    )
    return _error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="Ocurrió un error interno.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(SQLAlchemyError, database_exception_handler)
    app.add_exception_handler(Exception, unexpected_exception_handler)

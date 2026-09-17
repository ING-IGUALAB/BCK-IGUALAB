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
    NotFoundError,
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
}

_HTTP_ERROR_CODES = {
    status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHENTICATED",
    status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
    status.HTTP_409_CONFLICT: "CONFLICT",
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


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    status_code = _STATUS_BY_EXCEPTION.get(type(exc), status.HTTP_400_BAD_REQUEST)
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


async def validation_exception_handler(
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


async def http_exception_handler(
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


async def database_exception_handler(
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


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
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

from __future__ import annotations

from typing import Any


class AppException(Exception):
    """Error esperado de la aplicación, independiente del transporte HTTP."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


class AuthenticationError(AppException):
    """La identidad o la sesión no pudieron validarse."""


class AccountLockedError(AppException):
    """La cuenta está temporalmente bloqueada."""


class AccountDisabledError(AppException):
    """La cuenta fue deshabilitada."""


class AuthorizationError(AppException):
    """El usuario autenticado no puede realizar la operación."""


class NotFoundError(AppException):
    """El recurso solicitado no existe."""


class ConflictError(AppException):
    """La operación entra en conflicto con el estado actual."""


class BusinessValidationError(AppException):
    """Una regla funcional impide completar la operación."""


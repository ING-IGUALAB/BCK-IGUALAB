from __future__ import annotations

import re
from typing import Any

_CODIGO_REGEX = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


def es_codigo_valido(codigo: object) -> bool:
    """True si `codigo` es un código de error estable y seguro (constante propia de la app)."""
    return isinstance(codigo, str) and _CODIGO_REGEX.fullmatch(codigo) is not None


def codigo_seguro(exc: BaseException, tipo: type[AppException] = None) -> str:  # type: ignore[assignment]
    """Código estable y seguro del fallo: solo se reutiliza el `code` de una `AppException` del tipo
    indicado (por defecto `AppException`); cualquier otra excepción (de terceros) es `UNEXPECTED_ERROR`
    y nunca se repite su mensaje."""
    tipo = AppException if tipo is None else tipo
    codigo = getattr(exc, "code", None) if isinstance(exc, tipo) else None
    return codigo if es_codigo_valido(codigo) else "UNEXPECTED_ERROR"


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


class PayloadTooLargeError(AppException):
    """El contenido enviado supera el tamaño permitido."""


class ExternalServiceError(AppException):
    """Un servicio externo del que depende la operación falló o respondió de
    forma inválida. No es un error del cliente."""


class ExternalServiceTimeoutError(ExternalServiceError):
    """El servicio externo no respondió dentro del plazo."""



class InternalProcessingError(AppException):
    """Falló un proceso interno de la aplicación (no es un error del cliente ni de un servicio
    externo): se responde 500 con su código específico y sin detalles de la causa."""


class ServiceUnavailableError(AppException):
    """Una capacidad de la aplicación no está disponible por su configuración (p. ej. la ingesta sin
    almacenamiento configurado). Se responde 503 nombrando componentes o variables, nunca valores."""

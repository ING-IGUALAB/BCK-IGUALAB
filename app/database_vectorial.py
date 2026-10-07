"""Conexión a la base de datos VECTORIAL (PostgreSQL + pgvector), independiente de la transaccional.

- Usa EXCLUSIVAMENTE `VECTOR_DATABASE_URL`. No hay valor por defecto ni caída a `DATABASE_URL`:
  mezclar ambas bases rompería la separación acordada (dos bases coordinadas por la aplicación).
- El motor se crea de forma PEREZOSA al usar la funcionalidad vectorial: importar este módulo
  nunca conecta ni falla, así que un `VECTOR_DATABASE_URL` ausente no bloquea otros módulos ni el
  arranque. Al usarla sin configuración se informa un error controlado
  (`VECTOR_DATABASE_NOT_CONFIGURED`) que nunca revela la URL (puede contener credenciales).
- Este módulo no crea tablas ni extensiones: el esquema vectorial se aplica con
  `db/vector/001_fragmentos_documento.sql` por quien administre esa base (D17).
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.exceptions import ExternalServiceError

_PREFIJO = "postgresql+asyncpg://"
_motor: AsyncEngine | None = None
_url_del_motor: str | None = None


def url_vectorial() -> str:
    """URL configurada de la base vectorial; error controlado si falta o no es utilizable."""
    url = settings.VECTOR_DATABASE_URL
    if not isinstance(url, str) or not url.strip():
        raise ExternalServiceError(
            "VECTOR_DATABASE_NOT_CONFIGURED",
            "La base de datos vectorial no está configurada: defina VECTOR_DATABASE_URL.",
        )
    url = url.strip()
    if not url.startswith(_PREFIJO):
        # No se repite el valor recibido: podría contener credenciales.
        raise ExternalServiceError(
            "VECTOR_DATABASE_URL_INVALID",
            "VECTOR_DATABASE_URL debe usar el controlador postgresql+asyncpg://.",
        )
    return url


def obtener_motor_vectorial() -> AsyncEngine:
    """Motor asíncrono de la base vectorial, creado la primera vez que se necesita."""
    global _motor, _url_del_motor
    url = url_vectorial()
    if _motor is None or _url_del_motor != url:
        _motor = create_async_engine(url, echo=False)
        _url_del_motor = url
    return _motor


def fabrica_sesiones_vectoriales() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(obtener_motor_vectorial(), expire_on_commit=False)


async def get_vector_db() -> AsyncIterator[AsyncSession]:
    """Dependencia de FastAPI: una sesión vectorial por solicitud (nunca compartida entre tareas)."""
    async with fabrica_sesiones_vectoriales()() as sesion:
        try:
            yield sesion
        except Exception:
            await sesion.rollback()
            raise


async def cerrar_motor_vectorial() -> None:
    global _motor, _url_del_motor
    if _motor is not None:
        await _motor.dispose()
    _motor, _url_del_motor = None, None

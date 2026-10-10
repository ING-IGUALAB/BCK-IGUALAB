"""Ayudantes SOLO de pruebas para la API HTTP de ingesta (`/documentos`).

Aplicación mínima con los handlers y el middleware reales, el router de documentos y un `GestorIngesta` sobre las bases
REALES aisladas del `Entorno` (PostgreSQL transaccional y pgvector); OCI y MinIO son dobles. Se usa `httpx` con
`ASGITransport` para poder lanzar peticiones concurrentes, observar el progreso mientras corren y cancelarlas.
"""
import contextlib
import uuid
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.database import get_db
from app.dependencies import get_current_user
from app.exception_handlers import register_exception_handlers
from app.models import RolUsuario
from app.request_id import RequestIDMiddleware
from app.routers import documentos
from app.services.ingesta.gestor import GestorIngesta, OpcionesGestor


def opciones_de_prueba(**cambios) -> OpcionesGestor:
    base = dict(recuperacion_habilitada=False, intervalo_recuperacion=0.05, espera_cierre=5.0, retraso_primer_barrido=0.01)
    base.update(cambios)
    return OpcionesGestor(**base)


def construir_app(entorno, *, usuario="superadmin", gestor: GestorIngesta | None = None, con_gestor: bool = True) -> FastAPI:
    """`usuario`: 'superadmin' (el de la base), 'administrador', None (sin sesión: usa la dependencia real) u otro
    objeto con `id` y `rol`."""
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(documentos.router)
    app.state.ingesta = (gestor or GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())) if con_gestor else None
    app.state.ingesta_error = None if con_gestor else (
        "INGESTION_NOT_CONFIGURED", "La ingesta de documentos no está disponible.", {"componentes": []}
    )

    async def db_de_prueba():
        async with entorno.fabrica_pg() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    if usuario == "superadmin":
        app.dependency_overrides[get_current_user] = lambda: entorno.usuario
    elif usuario == "administrador":
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.ADMINISTRADOR)
    elif usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    return app


@contextlib.asynccontextmanager
async def cliente_http(app: FastAPI, **kwargs):
    transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://prueba", timeout=120, **kwargs) as cliente:
        yield cliente


def archivo(contenido: str | bytes, nombre: str = "informe.md", tipo_mime: str = "text/markdown"):
    datos = contenido if isinstance(contenido, bytes) else contenido.encode("utf-8")
    return {"archivo": (nombre, datos, tipo_mime)}


def formulario(entorno, **cambios) -> dict:
    datos = {"empresa_id": str(entorno.empresa.id), "anio": "2025", "tipo_documento": "MEMORIA_ANUAL"}
    datos.update(cambios)
    return datos


async def crear_operacion(cliente) -> str:
    respuesta = await cliente.post("/documentos/operaciones")
    assert respuesta.status_code == 201, respuesta.text
    return respuesta.json()["operacion_id"]


async def subir(cliente, operacion_id: str, entorno, contenido, nombre: str = "informe.md", **cambios):
    return await cliente.post(
        f"/documentos/operaciones/{operacion_id}/ingesta",
        data=formulario(entorno, **cambios),
        files=archivo(contenido, nombre),
    )


async def ingerir_por_http(cliente, entorno, contenido, nombre: str = "informe.md", **cambios):
    """Operación + carga. Devuelve (operacion_id, respuesta)."""
    operacion_id = await crear_operacion(cliente)
    return operacion_id, await subir(cliente, operacion_id, entorno, contenido, nombre, **cambios)


def comprobar_error(respuesta, estado: int, codigo: str) -> dict:
    assert respuesta.status_code == estado, respuesta.text
    error = respuesta.json()["error"]
    assert error["code"] == codigo, respuesta.text
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["request_id"] != "unavailable"
    return error

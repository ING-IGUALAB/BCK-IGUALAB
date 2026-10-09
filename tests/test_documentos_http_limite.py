"""Límite de 50 000 000 bytes del ARCHIVO por HTTP (multipart real), sin bases de datos ni servicios externos.

El multipart completo pesa más que el archivo (cabeceras y campos), y aun así un archivo de exactamente 50 000 000 bytes
se acepta; con 50 000 001 la respuesta es 413 con el contrato uniforme. Los bytes son los REALES recibidos, no
Content-Length ni `size` declarados. Se sustituyen el coordinador (tras la validación) y la persistencia de la operación;
la lectura del multipart, la copia acotada y la validación son las reales, de modo que lo que se prueba es el camino
HTTP → copia → validación de tamaño/UTF-8/formato.
"""
import contextlib
import uuid
from types import SimpleNamespace

import pytest

from app.exceptions import ConflictError
from app.models import RolUsuario
from app.services.ingesta import coordinador, operacion_service, validacion
from app.services.ingesta.coordinador import DependenciasIngesta
from app.services.ingesta.gestor import GestorIngesta
from app.services.ingesta.reglas import TAMANO_MAXIMO_BYTES
from tests.ayudantes_http_ingesta import comprobar_error, cliente_http, opciones_de_prueba
from tests.ayudantes_ingesta import AlmacenEnMemoria

import httpx
from fastapi import FastAPI

from app.dependencies import get_current_user
from app.exception_handlers import register_exception_handlers
from app.request_id import RequestIDMiddleware
from app.routers import documentos


@contextlib.asynccontextmanager
async def _sesion_nula():
    yield None


@pytest.fixture
def entorno_limite(monkeypatch):
    recibidos: list[dict] = []

    async def tomar_carga(db, operacion_id, **kwargs):
        return None

    async def marcar_rechazada(db, operacion_id, ambiente, **kwargs):
        recibidos.append({"rechazada": kwargs["codigo"]})
        return True

    async def ingerir_documento(dependencias, config, *, usuario_id, nombre_archivo, leer, metadatos, operacion_id=None):
        documento = await validacion.validar_archivo(nombre_archivo, leer)  # validación REAL
        recibidos.append({"tamano": documento.tamano_bytes, "sha256": documento.sha256, "nombre": documento.nombre_archivo})
        raise ConflictError("PRUEBA_ARCHIVO_ACEPTADO", "La validación aceptó el archivo.")

    monkeypatch.setattr(operacion_service, "tomar_carga", tomar_carga)
    monkeypatch.setattr(operacion_service, "marcar_rechazada", marcar_rechazada)
    monkeypatch.setattr(coordinador, "ingerir_documento", ingerir_documento)
    dependencias = DependenciasIngesta(_sesion_nula, _sesion_nula, AlmacenEnMemoria("development"), SimpleNamespace())
    gestor = GestorIngesta(dependencias, opciones=opciones_de_prueba())
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(documentos.router)
    app.state.ingesta = gestor
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.SUPERADMIN)
    return app, recibidos


def contenido_markdown(tamano: int) -> bytes:
    """Markdown UTF-8 válido de exactamente `tamano` bytes."""
    linea = ("Línea de prueba con acentos y contenido válido en UTF-8: 0123456789 abcdefghij klmnopqrst.\n").encode("utf-8")
    cuerpo = bytearray(b"# Documento de prueba\n\n")
    while len(cuerpo) + len(linea) <= tamano:
        cuerpo += linea
    cuerpo += b"a" * (tamano - len(cuerpo))
    assert len(cuerpo) == tamano
    return bytes(cuerpo)


async def enviar(app, datos: bytes) -> httpx.Response:
    async with cliente_http(app) as cliente:
        return await cliente.post(
            f"/documentos/operaciones/{uuid.uuid4()}/ingesta",
            data={"empresa_id": str(uuid.uuid4()), "anio": "2025", "tipo_documento": "MEMORIA_ANUAL"},
            files={"archivo": ("informe.md", datos, "text/markdown")},
        )


async def test_un_archivo_de_exactamente_50_000_000_bytes_se_acepta_aunque_el_multipart_pese_mas(entorno_limite):
    app, recibidos = entorno_limite
    assert TAMANO_MAXIMO_BYTES == 50_000_000
    respuesta = await enviar(app, contenido_markdown(50_000_000))
    # El «conflicto» de prueba indica que la validación aceptó el archivo y el flujo siguió.
    comprobar_error(respuesta, 409, "PRUEBA_ARCHIVO_ACEPTADO")
    assert int(respuesta.request.headers["content-length"]) > 50_000_000  # el multipart completo supera el límite
    (aceptado,) = [r for r in recibidos if "tamano" in r]
    assert aceptado["tamano"] == 50_000_000
    assert aceptado["nombre"] == "informe.md"


async def test_un_archivo_de_50_000_001_bytes_es_413_con_el_contrato_uniforme(entorno_limite):
    app, recibidos = entorno_limite
    respuesta = await enviar(app, contenido_markdown(50_000_001))
    error = comprobar_error(respuesta, 413, "FILE_TOO_LARGE")
    assert error["details"] == {"tamano_maximo_bytes": 50_000_000}
    assert not any("tamano" in r for r in recibidos)  # nunca llegó a aceptarse
    assert recibidos == [{"rechazada": "FILE_TOO_LARGE"}]  # la operación queda rechazada con ese código


async def test_el_limite_es_de_bytes_reales_no_de_caracteres(entorno_limite):
    app, recibidos = entorno_limite
    # 'é' ocupa 2 bytes: 25 000 001 caracteres = 50 000 002 bytes > límite aunque tengan menos caracteres que 50 000 000.
    datos = ("é" * 25_000_001).encode("utf-8")
    comprobar_error(await enviar(app, datos), 413, "FILE_TOO_LARGE")


async def test_un_archivo_pequeno_pasa_por_el_mismo_camino(entorno_limite):
    app, recibidos = entorno_limite
    comprobar_error(await enviar(app, b"# Hola\n\nDocumento breve.\n"), 409, "PRUEBA_ARCHIVO_ACEPTADO")
    (aceptado,) = [r for r in recibidos if "tamano" in r]
    assert aceptado["tamano"] == len(b"# Hola\n\nDocumento breve.\n")

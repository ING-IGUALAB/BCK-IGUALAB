"""Cableado del router `/documentos` con dobles (sin bases de datos): permisos, 503 controlado, 422 de metadatos y paso de
parámetros al servicio con el ambiente del gestor. El comportamiento real contra PostgreSQL está en
`tests/services/test_documentos_http.py`."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.database import get_db
from app.dependencies import get_current_user
from app.exception_handlers import register_exception_handlers
from app.models import RolUsuario
from app.request_id import RequestIDMiddleware
from app.routers import documentos
from app.schemas_ingesta import EstadoOperacionPublico, OperacionCreadaResponse, OperacionResponse, PaginaDocumentos
from app.services.ingesta import operacion_service
from tests.ayudantes_http_ingesta import comprobar_error

ACTOR = SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.SUPERADMIN)
AHORA = datetime.now(timezone.utc)


class GestorDoble:
    ambiente = "qa"

    def __init__(self):
        self.llamadas = []

    async def crear_operacion(self, db, *, usuario_id):
        self.llamadas.append(("crear", usuario_id))
        operacion = uuid.uuid4()
        return OperacionCreadaResponse(
            operacion_id=operacion, estado=EstadoOperacionPublico.CREADA, creada_en=AHORA, ingesta_url="i", progreso_url="p"
        )

    async def reintentar_publicacion(self, operacion_id):
        self.llamadas.append(("reintentar", operacion_id))
        return OperacionResponse(operacion_id=operacion_id, estado=EstadoOperacionPublico.COMPLETADO, terminal=True,
                                 exitosa=True, creada_en=AHORA, actualizada_en=AHORA)

    async def ingerir(self, **kwargs):
        self.llamadas.append(("ingerir", kwargs))
        raise AssertionError("no debe llegar aquí con metadatos inválidos")


def construir(gestor="doble", usuario=ACTOR):
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(documentos.router)
    app.state.ingesta = GestorDoble() if gestor == "doble" else None
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    app.dependency_overrides[get_current_user] = lambda: usuario
    return app


async def pedir(app, metodo, ruta, **kwargs) -> httpx.Response:
    transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://prueba") as cliente:
        return await getattr(cliente, metodo)(ruta, **kwargs)


RUTAS = [("post", "/documentos/operaciones"), ("get", "/documentos"), ("get", f"/documentos/{uuid.uuid4()}"),
         ("get", f"/documentos/operaciones/{uuid.uuid4()}"), ("post", f"/documentos/operaciones/{uuid.uuid4()}/reintentar-publicacion")]


@pytest.mark.parametrize("metodo,ruta", RUTAS)
async def test_sin_gestor_cada_endpoint_responde_503_controlado(metodo, ruta):
    app = construir(gestor=None)
    error = comprobar_error(await pedir(app, metodo, ruta), 503, "INGESTION_NOT_CONFIGURED")
    assert error["details"] is None  # sin motivo registrado: mensaje genérico, nada que filtrar

    app.state.ingesta_error = ("INGESTION_NOT_CONFIGURED", "Falta configuración.", {"componentes": [{"componente": "embeddings", "motivo": "X"}]})
    error = comprobar_error(await pedir(app, metodo, ruta), 503, "INGESTION_NOT_CONFIGURED")
    assert error["details"]["componentes"][0]["componente"] == "embeddings"


@pytest.mark.parametrize("metodo,ruta", RUTAS)
async def test_el_administrador_recibe_403_antes_que_cualquier_otra_cosa(metodo, ruta):
    administrador = SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.ADMINISTRADOR)
    comprobar_error(await pedir(construir(gestor=None, usuario=administrador), metodo, ruta), 403, "FORBIDDEN")


async def test_crear_y_reintentar_usan_al_actor_de_la_sesion():
    app = construir()
    respuesta = await pedir(app, "post", "/documentos/operaciones")
    assert respuesta.status_code == 201
    assert app.state.ingesta.llamadas[0] == ("crear", ACTOR.id)
    operacion = uuid.uuid4()
    respuesta = await pedir(app, "post", f"/documentos/operaciones/{operacion}/reintentar-publicacion")
    assert respuesta.status_code == 200
    assert respuesta.json()["exitosa"] is True
    assert app.state.ingesta.llamadas[1] == ("reintentar", operacion)


@pytest.mark.parametrize(
    "campos",
    [{"anio": "1999"}, {"anio": "dos mil"}, {"anio": "2025.0"}, {"tipo_documento": "OTRO"}, {"empresa_id": "x"}],
)
async def test_metadatos_invalidos_dan_422_sin_llegar_al_gestor(campos):
    app = construir()
    datos = {"empresa_id": str(uuid.uuid4()), "anio": "2025", "tipo_documento": "MEMORIA_ANUAL", **campos}
    respuesta = await pedir(
        app, "post", f"/documentos/operaciones/{uuid.uuid4()}/ingesta", data=datos,
        files={"archivo": ("a.md", b"# Hola\n", "text/markdown")},
    )
    error = comprobar_error(respuesta, 422, "REQUEST_VALIDATION_ERROR")
    assert "input" not in str(error["details"])
    assert app.state.ingesta.llamadas == []


async def test_las_consultas_pasan_los_filtros_y_el_ambiente_del_gestor(monkeypatch):
    capturado = {}

    async def listar(db, **kwargs):
        capturado["listar"] = kwargs
        return PaginaDocumentos(items=[], total=0, pagina=kwargs["pagina"], tamano=kwargs["tamano"], paginas=0)

    async def vista(db, operacion_id, ambiente):
        capturado["vista"] = (operacion_id, ambiente)
        return OperacionResponse(operacion_id=operacion_id, estado=EstadoOperacionPublico.CREADA, terminal=False,
                                 exitosa=False, creada_en=AHORA, actualizada_en=AHORA)

    monkeypatch.setattr(operacion_service, "listar_documentos", listar)
    monkeypatch.setattr(operacion_service, "obtener_vista", vista)
    app = construir()
    empresa, operacion = uuid.uuid4(), uuid.uuid4()
    respuesta = await pedir(app, "get", "/documentos", params={
        "empresa_id": str(empresa), "anio": 2024, "tipo": "MEMORIA_ANUAL", "estado": "FALLIDO", "pagina": 2, "tamano": 5})
    assert respuesta.status_code == 200
    argumentos = capturado["listar"]
    assert argumentos["ambiente"] == "qa"
    assert argumentos["empresa_id"] == empresa
    assert argumentos["anio"] == 2024
    assert argumentos["tipo"].value == "MEMORIA_ANUAL"
    assert argumentos["estado"].value == "FALLIDO"
    assert (argumentos["pagina"], argumentos["tamano"]) == (2, 5)
    assert (await pedir(app, "get", f"/documentos/operaciones/{operacion}")).status_code == 200
    assert capturado["vista"] == (operacion, "qa")  # el ambiente sale del gestor, no de la petición

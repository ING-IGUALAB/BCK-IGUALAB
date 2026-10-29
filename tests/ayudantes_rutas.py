"""Enumeración y despacho de rutas de la aplicación REAL sin depender de `app.routes`.

Desde FastAPI 0.137 `app.routes` / `router.routes` es un detalle interno (puede contener `_IncludedRouter`, un árbol de routers), así que las
pruebas enumeran las rutas con el esquema OpenAPI (API pública, en el orden de declaración) y comprueban qué ruta atiende una petición
DESPACHANDO por el enrutamiento real de la aplicación, no inspeccionando su estructura interna.
"""
from types import SimpleNamespace

from app.core.database import get_db
from app.main import app

METODOS_HTTP = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}


def operaciones_openapi() -> list[tuple[str, str]]:
    """(MÉTODO, ruta) de cada operación documentada, en el orden en que la aplicación las declara."""
    return [(metodo.upper(), ruta) for ruta, operaciones in app.openapi()["paths"].items() for metodo in operaciones if metodo in METODOS_HTTP]


async def ruta_que_atiende(metodo: str, ruta: str):
    """La ruta (APIRoute) que el enrutamiento real asigna a la petición, o None si ninguna la atiende. No exige respuesta correcta: sin
    credenciales la ruta responde 401/403/422, pero el enrutamiento ya dejó registrada en el alcance la ruta elegida."""
    alcance = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": metodo, "scheme": "http", "path": ruta,
        "raw_path": ruta.encode(), "query_string": b"", "root_path": "", "headers": [], "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }

    async def recibir():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def enviar(_mensaje):
        return None

    anterior = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    try:
        await app(alcance, recibir, enviar)
    finally:
        if anterior is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = anterior
    return alcance.get("route")

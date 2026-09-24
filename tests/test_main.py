from fastapi.middleware.cors import CORSMiddleware

from app.main import app


def test_aplicacion_fastapi_se_crea_correctamente():
    assert app is not None
    assert app.title
    assert app.openapi_url == "/openapi.json"


def test_cors_es_el_middleware_externo():
    middleware = app.user_middleware

    assert middleware
    assert middleware[0].cls is CORSMiddleware


def test_request_id_middleware_esta_registrado():
    nombres_middleware = [
        middleware.cls.__name__
        for middleware in app.user_middleware
    ]

    assert "RequestIDMiddleware" in nombres_middleware


def test_cors_se_ejecuta_antes_que_request_id():
    nombres_middleware = [
        middleware.cls.__name__
        for middleware in app.user_middleware
    ]

    posicion_cors = nombres_middleware.index("CORSMiddleware")
    posicion_request_id = nombres_middleware.index("RequestIDMiddleware")

    assert posicion_cors < posicion_request_id


def test_rutas_principales_estan_registradas():
    rutas = {
        route.path
        for route in app.routes
    }

    assert "/auth/login" in rutas
    assert "/usuarios" in rutas
    assert "/openapi.json" in rutas
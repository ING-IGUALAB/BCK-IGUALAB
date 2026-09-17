import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.exception_handlers import register_exception_handlers
from app.exceptions import ConflictError
from app.request_id import RequestIDMiddleware


class _Payload(BaseModel):
    correo: str


def _test_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/app-error")
    async def app_error():
        raise ConflictError("EMAIL_ALREADY_EXISTS", "El correo ya existe.")

    @app.get("/http-error")
    async def http_error():
        raise HTTPException(
            status_code=401,
            detail="No autenticado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.post("/validation")
    async def validation(payload: _Payload):
        return payload

    @app.get("/unexpected")
    async def unexpected():
        raise RuntimeError("detalle interno que no debe exponerse")

    @app.get("/database-error")
    async def database_error():
        raise SQLAlchemyError("consulta interna que no debe exponerse")

    return app


def test_app_exception_uses_uniform_contract_and_request_id():
    request_id = str(uuid.uuid4())
    with TestClient(_test_app()) as client:
        response = client.get("/app-error", headers={"X-Request-ID": request_id})

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == request_id
    assert response.json() == {
        "error": {
            "code": "EMAIL_ALREADY_EXISTS",
            "message": "El correo ya existe.",
            "details": None,
            "request_id": request_id,
        }
    }


def test_invalid_request_id_is_replaced_with_uuid():
    with TestClient(_test_app()) as client:
        response = client.get("/app-error", headers={"X-Request-ID": "no-es-un-uuid"})

    generated = response.headers["X-Request-ID"]
    assert str(uuid.UUID(generated)) == generated
    assert response.json()["error"]["request_id"] == generated


def test_http_exception_preserves_authentication_header():
    with TestClient(_test_app()) as client:
        response = client.get("/http-error")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_framework_404_also_uses_uniform_contract():
    with TestClient(_test_app()) as client:
        response = client.get("/ruta-inexistente")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_pydantic_validation_uses_contract_without_echoing_input():
    with TestClient(_test_app()) as client:
        response = client.post("/validation", json={})

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert body["error"]["details"][0]["location"] == ["body", "correo"]
    assert "input" not in body["error"]["details"][0]


def test_unexpected_exception_is_sanitized():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/unexpected")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "detalle interno" not in response.text


def test_database_exception_is_sanitized():
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/database-error")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "consulta interna" not in response.text

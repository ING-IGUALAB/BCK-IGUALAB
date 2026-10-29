"""Contrato HTTP conservado tras actualizar FastAPI: un cuerpo JSON sin `Content-Type` se sigue aceptando; uno con otro tipo de contenido se
sigue rechazando (422). Con la versión anterior (0.115) era así; FastAPI >= 0.132 lo rechazaba por defecto (`strict_content_type`).

Usa la aplicación real sin lifespan ni base de datos: el servicio de autenticación se sustituye por un doble que falla de forma controlada,
de modo que un 500 significa «el cuerpo se interpretó» y un 422 «el cuerpo se rechazó».
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.modules.auth import service

RUTA_LOGIN = "/auth/login"
CUERPO = json.dumps({"correo": "a@igualab-prueba.org", "password": "x"}).encode()


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setattr(service, "autenticar", AsyncMock(side_effect=RuntimeError("fallo controlado")))
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_json_sin_content_type_se_acepta_como_antes(cliente):
    respuesta = cliente.post(RUTA_LOGIN, content=CUERPO)
    assert respuesta.status_code == 500  # llegó al servicio: el cuerpo se interpretó como JSON
    assert respuesta.json()["error"]["code"] == "INTERNAL_ERROR"


def test_json_con_content_type_json_se_acepta(cliente):
    respuesta = cliente.post(RUTA_LOGIN, content=CUERPO, headers={"Content-Type": "application/json"})
    assert respuesta.status_code == 500


def test_json_con_otro_content_type_se_rechaza_como_antes(cliente):
    respuesta = cliente.post(RUTA_LOGIN, content=CUERPO, headers={"Content-Type": "text/plain"})
    assert respuesta.status_code == 422
    assert respuesta.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"

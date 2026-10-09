"""Etapa 1 de ingesta: metadatos y año (T04; LTX:RF-012, RN-020, RN-022; D20)."""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.exception_handlers import register_exception_handlers
from app.models import TipoDocumento
from app.request_id import RequestIDMiddleware
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta import reglas

AHORA_FIJO = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def reloj(monkeypatch):
    def fijar(instante: datetime) -> None:
        monkeypatch.setattr(reglas, "_ahora", lambda: instante)

    fijar(AHORA_FIJO)
    return fijar


def metadatos(**cambios):
    datos = {
        "empresa_id": str(uuid.uuid4()),
        "anio": 2025,
        "tipo": "MEMORIA_ANUAL",
    }
    datos.update(cambios)
    return datos


def test_zona_horaria_america_lima_se_resuelve():
    zona = reglas.ZONA_HORARIA_NEGOCIO
    assert zona.key == "America/Lima"
    assert datetime(2026, 1, 1, tzinfo=zona).utcoffset() == timedelta(hours=-5)


def test_anio_actual_no_es_constante_y_usa_hora_de_lima():
    assert reglas.anio_actual() == datetime.now(ZoneInfo("America/Lima")).year


@pytest.mark.parametrize(
    ("instante_utc", "anio_lima"),
    [
        (datetime(2027, 1, 1, 4, 59, 59, tzinfo=timezone.utc), 2026),
        (datetime(2027, 1, 1, 5, 0, 0, tzinfo=timezone.utc), 2027),
    ],
)
def test_cambio_de_anio_en_lima_frente_a_utc(reloj, instante_utc, anio_lima):
    reloj(instante_utc)
    # En UTC ya es 2027 en ambos instantes; en Lima solo en el segundo.
    assert instante_utc.year == 2027
    assert reglas.anio_actual() == anio_lima


def test_anio_siguiente_en_utc_se_rechaza_mientras_lima_sigue_en_el_anterior(reloj):
    reloj(datetime(2027, 1, 1, 4, 59, 59, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="entre 2000 y 2026"):
        reglas.interpretar_anio(2027)
    reloj(datetime(2027, 1, 1, 5, 0, 0, tzinfo=timezone.utc))
    assert reglas.interpretar_anio(2027) == 2027


@pytest.mark.parametrize("valor", [2000, 2025, 2026, "2000", "2025", "2026"])
def test_anio_valido_como_entero_o_texto(reloj, valor):
    payload = MetadatosIngestaRequest(**metadatos(anio=valor))
    assert payload.anio == int(valor)
    assert type(payload.anio) is int


@pytest.mark.parametrize(
    "valor",
    [
        1999, 2027, "1999", "2027", 0, -2025,
        True, False, "true",
        2025.0, 2025.5, "2025.0", "2025.5", "2025,5",
        "", " 2025", "2025 ", "+2025", "-2025", "dos mil", "２０２５", "2e3",
        None, [2025],
    ],
)
def test_anio_invalido_se_rechaza_sin_truncar_ni_redondear(reloj, valor):
    with pytest.raises(ValidationError) as capturado:
        MetadatosIngestaRequest(**metadatos(anio=valor))
    assert [error["loc"] for error in capturado.value.errors()] == [("anio",)]


@pytest.mark.parametrize("valor", [True, False])
def test_booleano_se_rechaza_por_tipo_y_no_solo_por_rango(reloj, valor):
    # True == 1 quedaría fuera de rango igualmente; se exige el motivo correcto.
    with pytest.raises(ValueError, match="valor lógico"):
        reglas.interpretar_anio(valor)


@pytest.mark.parametrize("valor", [2025.0, "2025.0"])
def test_fraccionario_exacto_no_se_convierte_a_entero(reloj, valor):
    with pytest.raises(ValueError, match="sin decimales"):
        reglas.interpretar_anio(valor)


@pytest.mark.parametrize("tipo", list(TipoDocumento))
def test_tipos_permitidos(reloj, tipo):
    assert MetadatosIngestaRequest(**metadatos(tipo=tipo.value)).tipo is tipo


@pytest.mark.parametrize("tipo", ["memoria_anual", "INFORME", "", None])
def test_tipo_no_permitido(reloj, tipo):
    with pytest.raises(ValidationError):
        MetadatosIngestaRequest(**metadatos(tipo=tipo))


def test_campos_obligatorios_y_extra_prohibidos(reloj):
    with pytest.raises(ValidationError) as capturado:
        MetadatosIngestaRequest(usuario_id=str(uuid.uuid4()))
    ubicaciones = {error["loc"] for error in capturado.value.errors()}
    assert ubicaciones == {("empresa_id",), ("anio",), ("tipo",), ("usuario_id",)}


def _app_validacion() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    # Ruta solo de prueba para verificar el contrato 422 del schema.
    @app.post("/metadatos")
    async def recibir(payload: MetadatosIngestaRequest):
        return {"anio": payload.anio}

    return app


@pytest.mark.parametrize("anio", [True, 2025.5, "2025.0", 1999, 2027])
def test_anio_invalido_usa_contrato_422_vigente(reloj, anio):
    with TestClient(_app_validacion()) as client:
        response = client.post("/metadatos", json=metadatos(anio=anio))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_ERROR"
    assert error["request_id"] != "unavailable"
    assert [detalle["location"] for detalle in error["details"]] == [["body", "anio"]]
    assert all("input" not in detalle for detalle in error["details"])


def test_anio_textual_valido_por_http(reloj):
    with TestClient(_app_validacion()) as client:
        response = client.post("/metadatos", json=metadatos(anio="2025"))
    assert response.status_code == 200
    assert response.json() == {"anio": 2025}

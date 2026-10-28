"""Compatibilidad del JWT tras sustituir python-jose por PyJWT (app/core/security.py).

Los vectores fijos se generaron con la implementación ANTERIOR (python-jose 3.5.0) y la clave de prueba de abajo: demuestran que los
tokens ya emitidos siguen siendo válidos y que se rechazan los alterados, vencidos o con algoritmo no permitido. Conservan algoritmo
permitido (`JWT_ALGORITHM`), clave, claims, vencimiento de 8 horas y el resultado `None` ante cualquier token inválido.
"""
import base64
import json
import uuid
import warnings
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core import security
from app.core.config import settings

CLAVE = "clave-de-prueba-compatibilidad-jwt-0123456789"
SUB = "11111111-1111-4111-8111-111111111111"
JTI = "22222222-2222-4222-8222-222222222222"
SUPERADMIN = "superadmin"
ADMINISTRADOR = "administrador"
PAYLOAD_VECTORES = {"sub": SUB, "rol": SUPERADMIN, "jti": JTI, "iat": 1767225600, "exp": 4102444800}

# Tokens emitidos por python-jose 3.5.0 con CLAVE.
VALIDO_HS256 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2wiOiJzdXBlcmFkbWluIiwianRpIjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIiwiaWF0IjoxNzY3MjI1NjAwLCJleHAiOjQxMDI0NDQ4MDB9.4hDul3Gv3_zidWmUpvdC6_F0SjiP7OEbxrcSN1Mht0A"
VENCIDO_HS256 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2wiOiJzdXBlcmFkbWluIiwianRpIjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIiwiaWF0IjoxNTc3ODM2ODAwLCJleHAiOjE1Nzc4NDA0MDB9.bLO1UTYpkYzt2aJ748uhil4bRRTSH5YPxIxlTl-Bp5M"
VALIDO_HS512 = "eyJhbGciOiJIUzUxMiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2wiOiJzdXBlcmFkbWluIiwianRpIjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIiwiaWF0IjoxNzY3MjI1NjAwLCJleHAiOjQxMDI0NDQ4MDB9.1Xu9mp3bNaDFA0Od23CffDVRnuEC-PLJ4-pwO5CMRNHKh1-ESD9d5sx6XAaVZzpwnLmfRyjqhSIBUIeennLGQw"
IAT_FUTURO_HS256 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2wiOiJzdXBlcmFkbWluIiwianRpIjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIiwiaWF0Ijo0MTAyNDQ0NzAwLCJleHAiOjQxMDI0NDQ4MDB9.zZH-grcLauPMHCvEUCgArEoTJtUiJvtJ8b2Y0sb2r5Y"
OTRA_CLAVE_HS256 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2wiOiJzdXBlcmFkbWluIiwianRpIjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIiwiaWF0IjoxNzY3MjI1NjAwLCJleHAiOjQxMDI0NDQ4MDB9.I2HvBuStN4bkczU5KhD-G0KdJxc0gH8We1cY-ztLaxg"
SIN_FIRMA_ALG_NONE = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTEiLCJyb2wiOiJzdXBlcmFkbWluIiwianRpIjoiMjIyMjIyMjItMjIyMi00MjIyLTgyMjItMjIyMjIyMjIyMjIyIiwiaWF0IjoxNzY3MjI1NjAwLCJleHAiOjQxMDI0NDQ4MDB9."


@pytest.fixture(autouse=True)
def clave_y_algoritmo(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", CLAVE)
    monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS256")


def b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def emitir(**cambios):
    ahora = datetime.now(timezone.utc)
    base = {"sub": SUB, "rol": ADMINISTRADOR, "jti": JTI, "iat": ahora, "exp": ahora + timedelta(hours=8)}
    return jwt.encode({**base, **cambios}, CLAVE, algorithm="HS256")


# ============================================ tokens emitidos por la implementación anterior ============================================

def test_un_token_emitido_por_python_jose_sigue_siendo_valido():
    assert security.decodificar_token(VALIDO_HS256) == PAYLOAD_VECTORES


def test_un_token_vencido_emitido_por_python_jose_se_rechaza():
    assert security.decodificar_token(VENCIDO_HS256) is None


def test_un_iat_posterior_al_momento_actual_sigue_aceptandose_como_antes():
    assert security.decodificar_token(IAT_FUTURO_HS256)["iat"] == 4102444700


# ============================================ rechazos ============================================

def test_un_token_con_otro_algoritmo_no_permitido_se_rechaza():
    assert security.decodificar_token(VALIDO_HS512) is None


def test_un_token_firmado_con_otra_clave_se_rechaza():
    assert security.decodificar_token(OTRA_CLAVE_HS256) is None


def test_un_token_sin_firma_con_alg_none_se_rechaza():
    assert security.decodificar_token(SIN_FIRMA_ALG_NONE) is None


@pytest.mark.parametrize("parte", [0, 1, 2], ids=["encabezado", "cuerpo", "firma"])
def test_un_token_alterado_en_cualquier_parte_se_rechaza(parte):
    partes = VALIDO_HS256.split(".")
    original = partes[parte]
    partes[parte] = original[:-2] + ("AA" if original[-2:] != "AA" else "BB")
    assert security.decodificar_token(".".join(partes)) is None


def test_un_cuerpo_reemplazado_con_la_firma_original_se_rechaza():
    partes = VALIDO_HS256.split(".")
    cuerpo = b64(json.dumps({**PAYLOAD_VECTORES, "rol": SUPERADMIN, "sub": str(uuid.uuid4())}).encode())
    assert security.decodificar_token(f"{partes[0]}.{cuerpo}.{partes[2]}") is None


@pytest.mark.parametrize("token", ["", " ", "abc", "a.b", "a.b.c", "...", "Bearer " + VALIDO_HS256, VALIDO_HS256 + ".extra", "ñ.ñ.ñ"])
def test_un_token_malformado_devuelve_none_sin_lanzar(token):
    assert security.decodificar_token(token) is None


def test_un_token_que_se_acaba_de_vencer_se_rechaza():
    assert security.decodificar_token(emitir(exp=datetime.now(timezone.utc) - timedelta(seconds=5))) is None


def test_un_sub_o_jti_no_textual_se_rechaza():
    assert security.decodificar_token(emitir(sub=12345)) is None
    assert security.decodificar_token(emitir(jti=777)) is None


# ============================================ emisión: mismos claims, algoritmo y vencimiento ============================================

def test_crear_access_token_conserva_claims_algoritmo_y_vencimiento_de_8_horas():
    usuario, sesion = uuid.uuid4(), uuid.uuid4()
    antes = int(datetime.now(timezone.utc).timestamp())
    token = security.crear_access_token(usuario, ADMINISTRADOR, sesion)
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    payload = security.decodificar_token(token)
    assert set(payload) == {"sub", "rol", "jti", "iat", "exp"}
    assert (payload["sub"], payload["rol"], payload["jti"]) == (str(usuario), ADMINISTRADOR, str(sesion))
    assert payload["exp"] - payload["iat"] == 8 * 3600
    assert antes <= payload["iat"] <= antes + 5


def test_el_token_emitido_ahora_lo_puede_leer_la_implementacion_anterior_en_formato():
    """Compatibilidad hacia atrás en el despliegue: encabezado y claims con el mismo formato que producía python-jose."""
    token = security.crear_access_token(uuid.UUID(SUB), SUPERADMIN, uuid.UUID(JTI))
    encabezado, cuerpo, _ = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(encabezado + "==")) == {"alg": "HS256", "typ": "JWT"}
    assert all(isinstance(json.loads(base64.urlsafe_b64decode(cuerpo + "=="))[c], int) for c in ("iat", "exp"))


def test_con_otro_algoritmo_configurado_solo_ese_se_acepta(monkeypatch):
    monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS512")
    assert security.decodificar_token(VALIDO_HS512) == PAYLOAD_VECTORES
    assert security.decodificar_token(VALIDO_HS256) is None


def test_una_clave_corta_solo_avisa_y_no_cambia_el_resultado(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "ci-test-secret")
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        token = security.crear_access_token(uuid.uuid4(), ADMINISTRADOR, uuid.uuid4())
        assert security.decodificar_token(token)["rol"] == ADMINISTRADOR

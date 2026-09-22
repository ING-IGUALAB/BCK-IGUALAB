import uuid

import pytest

from app.models import RolUsuario
from app.security import (
    crear_access_token,
    decodificar_token,
    generar_token_recuperacion,
    hash_password,
    hash_token_recuperacion,
    validar_politica_password,
    verificar_password,
    verificar_token_recuperacion,
)


def test_hash_password_no_guarda_texto_plano_y_permite_verificarlo():
    password = "ClaveSegura1!"
    password_hash = hash_password(password)

    assert password_hash != password
    assert verificar_password(password, password_hash) is True
    assert verificar_password("ClaveIncorrecta1!", password_hash) is False


def test_hash_password_usa_una_sal_distinta_en_cada_ejecucion():
    password = "ClaveSegura1!"

    assert hash_password(password) != hash_password(password)


@pytest.mark.parametrize(
    ("password", "mensaje_esperado"),
    [
        ("Aa1!", "al menos 8 caracteres"),
        ("minuscula1!", "letra mayúscula"),
        ("MAYUSCULA1!", "letra minúscula"),
        ("SinDigito!", "al menos un dígito"),
        ("SinEspecial1", "carácter especial"),
        ("Usuario1!@correo.com", "no puede coincidir con el correo"),
    ],
)
def test_politica_password_detecta_cada_regla_incumplida(
    password,
    mensaje_esperado,
):
    correo = "usuario1!@correo.com"

    errores = validar_politica_password(password, correo)

    assert any(mensaje_esperado in error for error in errores)


def test_politica_password_acepta_clave_valida():
    errores = validar_politica_password(
        "ClaveSegura1!",
        "usuario@correo.com",
    )

    assert errores == []


def test_jwt_valido_contiene_identidad_rol_y_sesion():
    usuario_id = uuid.uuid4()
    sesion_id = uuid.uuid4()

    token = crear_access_token(
        usuario_id,
        RolUsuario.ADMINISTRADOR.value,
        sesion_id,
    )
    payload = decodificar_token(token)

    assert payload is not None
    assert payload["sub"] == str(usuario_id)
    assert payload["rol"] == RolUsuario.ADMINISTRADOR.value
    assert payload["jti"] == str(sesion_id)
    assert payload["exp"] > payload["iat"]


def test_jwt_alterado_es_rechazado():
    assert decodificar_token("token.invalido.manipulado") is None


def test_token_recuperacion_es_aleatorio_y_se_almacena_como_hash():
    token_a = generar_token_recuperacion()
    token_b = generar_token_recuperacion()
    token_hash = hash_token_recuperacion(token_a)

    assert token_a != token_b
    assert token_hash != token_a
    assert verificar_token_recuperacion(token_a, token_hash) is True
    assert verificar_token_recuperacion(token_b, token_hash) is False
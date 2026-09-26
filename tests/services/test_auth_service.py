from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.exceptions import AuthenticationError
from app.services import auth_service


def _fake_db_sin_usuario():
    db = MagicMock()
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=resultado)
    return db


def _fake_db_con_usuario():
    usuario = MagicMock()
    usuario.password_hash = "hash-simulado"
    usuario.bloqueado_hasta = None
    usuario.habilitado = True
    usuario.intentos_fallidos = 0

    db = MagicMock()
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = usuario
    db.execute = AsyncMock(return_value=resultado)

    return db


@pytest.mark.asyncio
async def test_login_no_filtra_existencia_de_cuenta(monkeypatch):
    verificar_password = Mock(return_value=False)
    registrar_intento = AsyncMock()

    monkeypatch.setattr(
        auth_service,
        "verificar_password",
        verificar_password,
    )
    monkeypatch.setattr(
        auth_service,
        "_registrar_intento_fallido",
        registrar_intento,
    )

    with pytest.raises(AuthenticationError) as error_sin_usuario:
        await auth_service.autenticar(
            _fake_db_sin_usuario(),
            "no-existe@igualab.org",
            "clave-incorrecta",
        )

    assert verificar_password.call_count == 1

    verificar_password.reset_mock()

    with pytest.raises(AuthenticationError) as error_con_usuario:
        await auth_service.autenticar(
            _fake_db_con_usuario(),
            "existe@igualab.org",
            "clave-incorrecta",
        )

    assert verificar_password.call_count == 1

    assert error_sin_usuario.value.code == "INVALID_CREDENTIALS"
    assert error_con_usuario.value.code == "INVALID_CREDENTIALS"
    assert error_sin_usuario.value.message == error_con_usuario.value.message
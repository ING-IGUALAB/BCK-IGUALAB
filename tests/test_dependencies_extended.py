import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials

import app.dependencies as dependencies
from app.exceptions import AuthenticationError
from app.models import RolUsuario


def _credenciales():
    return HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="token",
    )


@pytest.mark.asyncio
async def test_token_con_firma_invalida_es_rechazado(monkeypatch):
    monkeypatch.setattr(
        dependencies,
        "decodificar_token",
        lambda _: None,
    )

    db = MagicMock()
    db.get = AsyncMock()

    with pytest.raises(AuthenticationError) as captured:
        await dependencies.get_current_user(_credenciales(), db)

    assert captured.value.code == "INVALID_SESSION"
    db.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"sub": str(uuid.uuid4())},
        {"jti": str(uuid.uuid4())},
        {
            "sub": "no-es-uuid",
            "jti": str(uuid.uuid4()),
        },
    ],
)
async def test_payload_jwt_incompleto_o_malformado_es_rechazado(
    monkeypatch,
    payload,
):
    monkeypatch.setattr(
        dependencies,
        "decodificar_token",
        lambda _: payload,
    )

    db = MagicMock()
    db.get = AsyncMock()

    with pytest.raises(AuthenticationError) as captured:
        await dependencies.get_current_user(_credenciales(), db)

    assert captured.value.code == "INVALID_SESSION"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estado",
    ["inexistente", "revocada", "otro_usuario"],
)
async def test_sesion_invalida_es_rechazada(monkeypatch, estado):
    usuario_id = uuid.uuid4()
    sesion_id = uuid.uuid4()

    monkeypatch.setattr(
        dependencies,
        "decodificar_token",
        lambda _: {
            "sub": str(usuario_id),
            "jti": str(sesion_id),
        },
    )

    sesion = None

    if estado != "inexistente":
        sesion = MagicMock()
        sesion.revocada = estado == "revocada"
        sesion.usuario_id = (
            uuid.uuid4()
            if estado == "otro_usuario"
            else usuario_id
        )

    db = MagicMock()
    db.get = AsyncMock(return_value=sesion)

    with pytest.raises(AuthenticationError) as captured:
        await dependencies.get_current_user(_credenciales(), db)

    assert captured.value.code == "INVALID_SESSION"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usuario",
    [
        None,
        MagicMock(habilitado=False),
    ],
)
async def test_usuario_inexistente_o_deshabilitado_es_rechazado(
    monkeypatch,
    usuario,
):
    usuario_id = uuid.uuid4()
    sesion_id = uuid.uuid4()

    monkeypatch.setattr(
        dependencies,
        "decodificar_token",
        lambda _: {
            "sub": str(usuario_id),
            "jti": str(sesion_id),
        },
    )

    sesion = MagicMock(
        usuario_id=usuario_id,
        revocada=False,
        ultima_actividad=datetime.now(timezone.utc),
    )

    db = MagicMock()
    db.get = AsyncMock(side_effect=[sesion, usuario])

    with pytest.raises(AuthenticationError) as captured:
        await dependencies.get_current_user(_credenciales(), db)

    assert captured.value.code == "INVALID_SESSION"


@pytest.mark.asyncio
async def test_sesion_valida_actualiza_ultima_actividad_y_devuelve_usuario(
    monkeypatch,
):
    usuario_id = uuid.uuid4()
    sesion_id = uuid.uuid4()

    monkeypatch.setattr(
        dependencies,
        "decodificar_token",
        lambda _: {
            "sub": str(usuario_id),
            "jti": str(sesion_id),
        },
    )

    actividad_anterior = datetime.now(timezone.utc)

    sesion = MagicMock(
        usuario_id=usuario_id,
        revocada=False,
        ultima_actividad=actividad_anterior,
    )
    usuario = MagicMock(
        habilitado=True,
        rol=RolUsuario.ADMINISTRADOR,
    )

    db = MagicMock()
    db.get = AsyncMock(side_effect=[sesion, usuario])
    db.commit = AsyncMock()

    resultado = await dependencies.get_current_user(
        _credenciales(),
        db,
    )

    assert resultado is usuario
    assert sesion.ultima_actividad >= actividad_anterior
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dependencia_de_rol_permite_rol_autorizado():
    usuario = MagicMock(rol=RolUsuario.SUPERADMIN)
    verificar = dependencies.requerir_rol(
        RolUsuario.SUPERADMIN,
    )

    resultado = await verificar(usuario)

    assert resultado is usuario
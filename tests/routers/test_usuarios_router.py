import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import RolUsuario
from app.routers import usuarios as usuarios_router
from app.schemas import CrearUsuarioRequest, TransferirSuperAdminRequest


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def superadmin():
    actor = MagicMock()
    actor.id = uuid.uuid4()
    actor.nombre = "SuperAdmin"
    actor.correo = "superadmin@igualab.com"
    actor.rol = RolUsuario.SUPERADMIN
    actor.habilitado = True
    return actor


@pytest.mark.asyncio
async def test_crear_usuario_delega_al_servicio(db, superadmin):
    payload = CrearUsuarioRequest(
        nombre="Administrador",
        correo="admin@igualab.com",
        password="Password123!",
    )
    usuario_creado = MagicMock()

    with patch.object(
        usuarios_router.usuario_service,
        "crear_usuario",
        new=AsyncMock(return_value=usuario_creado),
    ) as crear_mock:
        resultado = await usuarios_router.crear_usuario(
            payload,
            superadmin,
            db,
        )

    assert resultado is usuario_creado
    crear_mock.assert_awaited_once_with(
        db,
        payload.nombre,
        payload.correo,
        payload.password,
    )


@pytest.mark.asyncio
async def test_listar_usuarios_delega_al_servicio(db, superadmin):
    usuarios = [MagicMock(), MagicMock()]

    with patch.object(
        usuarios_router.usuario_service,
        "listar_usuarios",
        new=AsyncMock(return_value=usuarios),
    ) as listar_mock:
        resultado = await usuarios_router.listar_usuarios(
            superadmin,
            db,
        )

    assert resultado == usuarios
    listar_mock.assert_awaited_once_with(db)


@pytest.mark.asyncio
async def test_habilitar_usuario_delega_al_servicio(db, superadmin):
    usuario_id = uuid.uuid4()
    usuario_actualizado = MagicMock()

    with patch.object(
        usuarios_router.usuario_service,
        "cambiar_estado_usuario",
        new=AsyncMock(return_value=usuario_actualizado),
    ) as cambiar_estado_mock:
        resultado = await usuarios_router.habilitar_usuario(
            usuario_id,
            superadmin,
            db,
        )

    assert resultado is usuario_actualizado
    cambiar_estado_mock.assert_awaited_once_with(
        db,
        usuario_id,
        habilitar=True,
        actor=superadmin,
    )


@pytest.mark.asyncio
async def test_deshabilitar_usuario_delega_al_servicio(db, superadmin):
    usuario_id = uuid.uuid4()
    usuario_actualizado = MagicMock()

    with patch.object(
        usuarios_router.usuario_service,
        "cambiar_estado_usuario",
        new=AsyncMock(return_value=usuario_actualizado),
    ) as cambiar_estado_mock:
        resultado = await usuarios_router.deshabilitar_usuario(
            usuario_id,
            superadmin,
            db,
        )

    assert resultado is usuario_actualizado
    cambiar_estado_mock.assert_awaited_once_with(
        db,
        usuario_id,
        habilitar=False,
        actor=superadmin,
    )


@pytest.mark.asyncio
async def test_transferir_superadmin_delega_al_servicio(db, superadmin):
    cuenta_destino_id = uuid.uuid4()
    payload = TransferirSuperAdminRequest(
        cuenta_destino_id=cuenta_destino_id,
    )

    with patch.object(
        usuarios_router.usuario_service,
        "transferir_superadmin",
        new=AsyncMock(),
    ) as transferir_mock:
        resultado = await usuarios_router.transferir_superadmin(
            payload,
            superadmin,
            db,
        )

    assert resultado is None
    transferir_mock.assert_awaited_once_with(
        db,
        superadmin,
        cuenta_destino_id,
    )
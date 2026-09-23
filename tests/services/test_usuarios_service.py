import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.usuario_service as usuario_service
from app.exceptions import ConflictError, NotFoundError
from app.models import RolUsuario


def _resultado_unico(valor):
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = valor
    return resultado


def _resultado_lista(valores):
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = valores
    return resultado


def _usuario(
    rol=RolUsuario.ADMINISTRADOR,
    habilitado=True,
):
    usuario = MagicMock()
    usuario.id = uuid.uuid4()
    usuario.nombre = "Usuario de prueba"
    usuario.correo = "usuario@igualab.org"
    usuario.rol = rol
    usuario.habilitado = habilitado
    return usuario


@pytest.mark.asyncio
async def test_crear_usuario_normaliza_correo_y_asigna_rol_administrador(
    monkeypatch,
):
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(None),
    )
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    monkeypatch.setattr(
        usuario_service,
        "hash_password",
        lambda _: "hash-seguro",
    )

    creado = await usuario_service.crear_usuario(
        db,
        "Nuevo usuario",
        "NUEVO@IGUALAB.ORG",
        "ClaveSegura1!",
    )

    assert creado.correo == "nuevo@igualab.org"
    assert creado.rol == RolUsuario.ADMINISTRADOR
    assert creado.habilitado is True
    assert creado.password_hash == "hash-seguro"

    db.add.assert_called_once_with(creado)
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(creado)


@pytest.mark.asyncio
async def test_listar_usuarios_devuelve_todos_los_resultados():
    usuarios = [
        _usuario(),
        _usuario(RolUsuario.SUPERADMIN),
    ]

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_lista(usuarios),
    )

    resultado = await usuario_service.listar_usuarios(db)

    assert resultado == usuarios


@pytest.mark.asyncio
async def test_no_se_puede_deshabilitar_al_superadmin():
    superadmin = _usuario(RolUsuario.SUPERADMIN)

    db = MagicMock()
    db.get = AsyncMock(return_value=superadmin)

    with pytest.raises(ConflictError) as captured:
        await usuario_service.cambiar_estado_usuario(
            db,
            superadmin.id,
            habilitar=False,
            actor=superadmin,
        )

    assert (
        captured.value.code
        == "SUPERADMIN_CANNOT_BE_DISABLED"
    )


@pytest.mark.asyncio
async def test_deshabilitar_usuario_revoca_todas_sus_sesiones(
    monkeypatch,
):
    actor = _usuario(RolUsuario.SUPERADMIN)
    usuario = _usuario()

    sesiones = [
        MagicMock(revocada=False),
        MagicMock(revocada=False),
    ]

    db = MagicMock()
    db.get = AsyncMock(return_value=usuario)
    db.execute = AsyncMock(
        return_value=_resultado_lista(sesiones),
    )
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    registrar = AsyncMock()

    monkeypatch.setattr(
        usuario_service,
        "registrar_evento",
        registrar,
    )

    resultado = await usuario_service.cambiar_estado_usuario(
        db,
        usuario.id,
        habilitar=False,
        actor=actor,
    )

    assert resultado is usuario
    assert usuario.habilitado is False
    assert all(
        sesion.revocada is True
        for sesion in sesiones
    )

    registrar.assert_awaited_once()
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once_with(usuario)


@pytest.mark.asyncio
async def test_habilitar_usuario_no_consulta_sesiones(
    monkeypatch,
):
    actor = _usuario(RolUsuario.SUPERADMIN)
    usuario = _usuario(habilitado=False)

    db = MagicMock()
    db.get = AsyncMock(return_value=usuario)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    monkeypatch.setattr(
        usuario_service,
        "registrar_evento",
        AsyncMock(),
    )

    await usuario_service.cambiar_estado_usuario(
        db,
        usuario.id,
        habilitar=True,
        actor=actor,
    )

    assert usuario.habilitado is True
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_transferencia_rechaza_destino_inexistente():
    origen = _usuario(RolUsuario.SUPERADMIN)

    db = MagicMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as captured:
        await usuario_service.transferir_superadmin(
            db,
            origen,
            uuid.uuid4(),
        )

    assert (
        captured.value.code
        == "DESTINATION_USER_NOT_FOUND"
    )


@pytest.mark.asyncio
async def test_transferencia_rechaza_destino_deshabilitado():
    origen = _usuario(RolUsuario.SUPERADMIN)
    destino = _usuario(habilitado=False)

    db = MagicMock()
    db.get = AsyncMock(return_value=destino)

    with pytest.raises(ConflictError) as captured:
        await usuario_service.transferir_superadmin(
            db,
            origen,
            destino.id,
        )

    assert captured.value.code == "DESTINATION_USER_DISABLED"


@pytest.mark.asyncio
async def test_transferencia_rechaza_destino_que_ya_es_superadmin():
    origen = _usuario(RolUsuario.SUPERADMIN)
    destino = _usuario(RolUsuario.SUPERADMIN)

    db = MagicMock()
    db.get = AsyncMock(return_value=destino)

    with pytest.raises(ConflictError) as captured:
        await usuario_service.transferir_superadmin(
            db,
            origen,
            destino.id,
        )

    assert (
        captured.value.code
        == "DESTINATION_ALREADY_SUPERADMIN"
    )


@pytest.mark.asyncio
async def test_transferencia_exitosa_cambia_ambos_roles_y_audita(
    monkeypatch,
):
    origen = _usuario(RolUsuario.SUPERADMIN)
    destino = _usuario(RolUsuario.ADMINISTRADOR)

    db = MagicMock()
    db.get = AsyncMock(return_value=destino)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    registrar = AsyncMock()

    monkeypatch.setattr(
        usuario_service,
        "registrar_evento",
        registrar,
    )

    await usuario_service.transferir_superadmin(
        db,
        origen,
        destino.id,
    )

    assert origen.rol == RolUsuario.ADMINISTRADOR
    assert destino.rol == RolUsuario.SUPERADMIN

    db.flush.assert_awaited_once()
    registrar.assert_awaited_once()
    db.commit.assert_awaited_once()
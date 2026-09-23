import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.auth_service as auth_service
from app.exceptions import (
    AuthenticationError,
    BusinessValidationError,
)
from app.models import RolUsuario
from app.security import hash_password


def _resultado_unico(valor):
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = valor
    return resultado


def _resultado_lista(valores):
    resultado = MagicMock()
    resultado.scalars.return_value.all.return_value = valores
    return resultado


def _usuario(password="ClaveSegura1!"):
    usuario = MagicMock()
    usuario.id = uuid.uuid4()
    usuario.correo = "usuario@igualab.org"
    usuario.nombre = "Usuario de prueba"
    usuario.rol = RolUsuario.ADMINISTRADOR
    usuario.habilitado = True
    usuario.bloqueado_hasta = None
    usuario.intentos_fallidos = 0
    usuario.password_hash = hash_password(password)
    return usuario


@pytest.mark.asyncio
async def test_login_exitoso_reinicia_intentos_crea_sesion_y_audita(
    monkeypatch,
):
    usuario = _usuario()

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(usuario),
    )
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    registrar = MagicMock()

    monkeypatch.setattr(
        auth_service,
        "registrar_evento",
        registrar,
    )
    monkeypatch.setattr(
        auth_service,
        "crear_access_token",
        lambda *_: "jwt-prueba",
    )

    token, autenticado = await auth_service.autenticar(
        db,
        "USUARIO@IGUALAB.ORG",
        "ClaveSegura1!",
    )

    assert token == "jwt-prueba"
    assert autenticado is usuario
    assert usuario.intentos_fallidos == 0
    assert usuario.bloqueado_hasta is None

    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    registrar.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_password_incorrecto_incrementa_intentos_sin_bloquear(
    monkeypatch,
):
    usuario = _usuario()

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(usuario),
    )
    db.commit = AsyncMock()

    registrar = MagicMock()

    monkeypatch.setattr(
        auth_service,
        "registrar_evento",
        registrar,
    )
    monkeypatch.setattr(
        auth_service.settings,
        "MAX_LOGIN_ATTEMPTS",
        5,
    )

    with pytest.raises(AuthenticationError) as captured:
        await auth_service.autenticar(
            db,
            usuario.correo,
            "ClaveIncorrecta1!",
        )

    assert captured.value.code == "INVALID_CREDENTIALS"
    assert usuario.intentos_fallidos == 1
    assert usuario.bloqueado_hasta is None

    registrar.assert_not_called()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_quinto_intento_incorrecto_bloquea_y_registra_auditoria(
    monkeypatch,
):
    usuario = _usuario()
    usuario.intentos_fallidos = 4

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(usuario),
    )
    db.commit = AsyncMock()

    registrar = MagicMock()

    monkeypatch.setattr(
        auth_service,
        "registrar_evento",
        registrar,
    )
    monkeypatch.setattr(
        auth_service.settings,
        "MAX_LOGIN_ATTEMPTS",
        5,
    )
    monkeypatch.setattr(
        auth_service.settings,
        "LOGIN_LOCKOUT_MINUTES",
        15,
    )

    with pytest.raises(AuthenticationError):
        await auth_service.autenticar(
            db,
            usuario.correo,
            "ClaveIncorrecta1!",
        )

    assert usuario.intentos_fallidos == 5
    assert usuario.bloqueado_hasta > datetime.now(timezone.utc)

    registrar.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cerrar_sesion_revoca_sesion_existente():
    sesion = MagicMock(revocada=False)

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(sesion),
    )
    db.commit = AsyncMock()

    await auth_service.cerrar_sesion(
        db,
        uuid.uuid4(),
    )

    assert sesion.revocada is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cerrar_sesion_inexistente_no_hace_commit():
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(None),
    )
    db.commit = AsyncMock()

    await auth_service.cerrar_sesion(
        db,
        uuid.uuid4(),
    )

    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_recuperacion_de_correo_inexistente_no_revela_la_cuenta(
    monkeypatch,
):
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(None),
    )
    db.add = MagicMock()
    db.commit = AsyncMock()

    enviar = AsyncMock()

    monkeypatch.setattr(
        auth_service,
        "enviar_correo_recuperacion",
        enviar,
    )

    resultado = await auth_service.solicitar_recuperacion(
        db,
        "nadie@igualab.org",
    )

    assert resultado is None
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    enviar.assert_not_awaited()


@pytest.mark.asyncio
async def test_recuperacion_valida_guarda_hash_y_envia_token_plano(
    monkeypatch,
):
    usuario = _usuario()

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_unico(usuario),
    )
    db.add = MagicMock()
    db.commit = AsyncMock()

    enviar = AsyncMock()

    monkeypatch.setattr(
        auth_service,
        "generar_token_recuperacion",
        lambda: "token-plano",
    )
    monkeypatch.setattr(
        auth_service,
        "hash_token_recuperacion",
        lambda _: "token-hash",
    )
    monkeypatch.setattr(
        auth_service,
        "enviar_correo_recuperacion",
        enviar,
    )

    await auth_service.solicitar_recuperacion(
        db,
        usuario.correo.upper(),
    )

    registro = db.add.call_args.args[0]

    assert registro.usuario_id == usuario.id
    assert registro.token_hash == "token-hash"
    assert registro.expira_en > datetime.now(timezone.utc)

    db.commit.assert_awaited_once()
    enviar.assert_awaited_once_with(
        usuario.correo,
        "token-plano",
    )


@pytest.mark.asyncio
async def test_token_recuperacion_expirado_es_rechazado(
    monkeypatch,
):
    registro = MagicMock(
        token_hash="hash",
        expira_en=(
            datetime.now(timezone.utc)
            - timedelta(minutes=1)
        ),
    )

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_lista([registro]),
    )

    monkeypatch.setattr(
        auth_service,
        "verificar_token_recuperacion",
        lambda *_: True,
    )

    with pytest.raises(BusinessValidationError) as captured:
        await auth_service.restablecer_password(
            db,
            "token",
            "NuevaClave1!",
        )

    assert (
        captured.value.code
        == "INVALID_OR_EXPIRED_RESET_TOKEN"
    )


@pytest.mark.asyncio
async def test_nueva_password_insegura_es_rechazada(
    monkeypatch,
):
    registro = MagicMock(
        token_hash="hash",
        expira_en=(
            datetime.now(timezone.utc)
            + timedelta(minutes=10)
        ),
    )

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_resultado_lista([registro]),
    )

    monkeypatch.setattr(
        auth_service,
        "verificar_token_recuperacion",
        lambda *_: True,
    )

    with pytest.raises(BusinessValidationError) as captured:
        await auth_service.restablecer_password(
            db,
            "token",
            "debil",
        )

    assert captured.value.code == "PASSWORD_POLICY_VIOLATION"
    assert captured.value.details["errors"]


@pytest.mark.asyncio
async def test_restablecer_password_invalida_token_y_revoca_sesiones(
    monkeypatch,
):
    usuario = _usuario()

    registro = MagicMock(
        usuario_id=usuario.id,
        token_hash="hash",
        usado=False,
        expira_en=(
            datetime.now(timezone.utc)
            + timedelta(minutes=10)
        ),
    )

    sesiones = [
        MagicMock(revocada=False),
        MagicMock(revocada=False),
    ]

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _resultado_lista([registro]),
            _resultado_lista(sesiones),
        ],
    )
    db.get = AsyncMock(return_value=usuario)
    db.commit = AsyncMock()

    monkeypatch.setattr(
        auth_service,
        "verificar_token_recuperacion",
        lambda *_: True,
    )
    monkeypatch.setattr(
        auth_service,
        "hash_password",
        lambda _: "nuevo-hash",
    )

    await auth_service.restablecer_password(
        db,
        "token",
        "NuevaClave1!",
    )

    assert usuario.password_hash == "nuevo-hash"
    assert registro.usado is True
    assert all(
        sesion.revocada is True
        for sesion in sesiones
    )

    db.commit.assert_awaited_once()
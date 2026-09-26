from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials

import app.routers.auth as auth_router
from app.exceptions import (
    AuthenticationError,
    BusinessValidationError,
)
from app.models import RolUsuario
from app.schemas import (
    CambiarPasswordRequest,
    LoginRequest,
    RestablecerPasswordRequest,
    SolicitarRecuperacionRequest,
)


@pytest.mark.asyncio
async def test_login_devuelve_token_y_datos_del_usuario(
    monkeypatch,
):
    db = MagicMock()
    usuario = MagicMock(
        rol=RolUsuario.ADMINISTRADOR,
        nombre="Administrador",
    )

    autenticar = AsyncMock(
        return_value=("token-prueba", usuario),
    )

    monkeypatch.setattr(
        auth_router.auth_service,
        "autenticar",
        autenticar,
    )

    payload = LoginRequest(
        correo="admin@igualab.org",
        password="ClaveSegura1!",
    )

    respuesta = await auth_router.login(
        payload,
        db,
    )

    autenticar.assert_awaited_once_with(
        db,
        payload.correo,
        payload.password,
    )

    assert respuesta.access_token == "token-prueba"
    assert respuesta.rol == RolUsuario.ADMINISTRADOR
    assert respuesta.nombre == "Administrador"


@pytest.mark.asyncio
async def test_logout_decodifica_token_y_cierra_sesion(
    monkeypatch,
):
    db = MagicMock()
    usuario = MagicMock()
    sesion_id = "550e8400-e29b-41d4-a716-446655440000"

    credenciales = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials="token-prueba",
    )

    cerrar_sesion = AsyncMock()

    monkeypatch.setattr(
        auth_router,
        "decodificar_token",
        lambda _: {"jti": sesion_id},
    )
    monkeypatch.setattr(
        auth_router.auth_service,
        "cerrar_sesion",
        cerrar_sesion,
    )

    resultado = await auth_router.logout(
        credenciales,
        usuario,
        db,
    )

    cerrar_sesion.assert_awaited_once_with(
        db,
        sesion_id=sesion_id,
    )
    assert resultado is None


@pytest.mark.asyncio
async def test_solicitar_recuperacion_devuelve_mensaje_generico(
    monkeypatch,
):
    db = MagicMock()
    solicitar = AsyncMock()

    monkeypatch.setattr(
        auth_router.auth_service,
        "solicitar_recuperacion",
        solicitar,
    )

    payload = SolicitarRecuperacionRequest(
        correo="admin@igualab.org",
    )

    respuesta = await auth_router.solicitar_recuperacion(
        payload,
        db,
    )

    solicitar.assert_awaited_once_with(
        db,
        payload.correo,
    )

    assert "Si el correo está registrado" in respuesta["mensaje"]


@pytest.mark.asyncio
async def test_restablecer_password_actualiza_contrasena(
    monkeypatch,
):
    db = MagicMock()
    restablecer = AsyncMock()

    monkeypatch.setattr(
        auth_router.auth_service,
        "restablecer_password",
        restablecer,
    )

    payload = RestablecerPasswordRequest(
        token="token-recuperacion",
        password_nueva="NuevaClave1!",
        password_nueva_confirmacion="NuevaClave1!",
    )

    respuesta = await auth_router.restablecer_password(
        payload,
        db,
    )

    restablecer.assert_awaited_once_with(
        db,
        payload.token,
        payload.password_nueva,
    )

    assert respuesta == {
        "mensaje": (
            "Contraseña actualizada correctamente. "
            "Ya puede iniciar sesión."
        )
    }


@pytest.mark.asyncio
async def test_cambiar_password_actualiza_hash(
    monkeypatch,
):
    db = MagicMock()
    db.commit = AsyncMock()

    usuario = MagicMock()
    usuario.correo = "admin@igualab.org"
    usuario.password_hash = "hash-anterior"

    monkeypatch.setattr(
        auth_router,
        "verificar_password",
        lambda *_: True,
    )
    monkeypatch.setattr(
        auth_router,
        "validar_politica_password",
        lambda *_: [],
    )
    monkeypatch.setattr(
        auth_router,
        "hash_password",
        lambda _: "hash-nuevo",
    )

    payload = CambiarPasswordRequest(
        password_actual="ClaveAnterior1!",
        password_nueva="NuevaClave1!",
    )

    respuesta = await auth_router.cambiar_mi_password(
        payload,
        usuario,
        db,
    )

    assert usuario.password_hash == "hash-nuevo"
    db.commit.assert_awaited_once()

    assert respuesta == {
        "mensaje": "Contraseña actualizada correctamente."
    }


@pytest.mark.asyncio
async def test_cambiar_password_rechaza_password_actual_incorrecta(
    monkeypatch,
):
    db = MagicMock()
    db.commit = AsyncMock()

    usuario = MagicMock()
    usuario.password_hash = "hash-anterior"
    usuario.correo = "admin@igualab.org"

    monkeypatch.setattr(
        auth_router,
        "verificar_password",
        lambda *_: False,
    )

    payload = CambiarPasswordRequest(
        password_actual="ClaveIncorrecta1!",
        password_nueva="NuevaClave1!",
    )

    with pytest.raises(AuthenticationError) as captured:
        await auth_router.cambiar_mi_password(
            payload,
            usuario,
            db,
        )

    assert captured.value.code == "CURRENT_PASSWORD_INVALID"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cambiar_password_rechaza_politica_invalida(
    monkeypatch,
):
    db = MagicMock()
    db.commit = AsyncMock()

    usuario = MagicMock()
    usuario.password_hash = "hash-anterior"
    usuario.correo = "admin@igualab.org"

    errores = [
        "La contraseña debe incluir un carácter especial."
    ]

    monkeypatch.setattr(
        auth_router,
        "verificar_password",
        lambda *_: True,
    )
    monkeypatch.setattr(
        auth_router,
        "validar_politica_password",
        lambda *_: errores,
    )

    payload = CambiarPasswordRequest(
        password_actual="ClaveAnterior1!",
        password_nueva="ClaveInvalida1",
    )

    with pytest.raises(BusinessValidationError) as captured:
        await auth_router.cambiar_mi_password(
            payload,
            usuario,
            db,
        )

    assert captured.value.code == "PASSWORD_POLICY_VIOLATION"
    assert captured.value.details == {"errors": errores}
    db.commit.assert_not_awaited()
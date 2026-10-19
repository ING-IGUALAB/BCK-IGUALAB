import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock
from app.models import Usuario

import pytest

import app.modules.auth.service as auth_service
from app.core.exceptions import (
    AuthenticationError,
    BusinessValidationError,
)
from app.models import ResultadoAuditoria, RolUsuario, TipoEventoAuditoria
from app.core.security import hash_password


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

    # LTX:RN-043: el intento fallido queda registrado (cuenta destinataria, resultado FALLIDO) en la misma confirmación.
    registrar.assert_called_once()
    assert registrar.call_args.args[1] is TipoEventoAuditoria.INICIO_SESION
    assert registrar.call_args.kwargs["usuario_id"] == usuario.id
    assert registrar.call_args.kwargs["resultado"] is ResultadoAuditoria.FALLIDO
    assert "intentos consecutivos=1" in registrar.call_args.args[2]
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

    registrar.assert_called_once()  # UN solo evento para el intento que bloquea (no uno de intento fallido y otro de bloqueo)
    assert registrar.call_args.kwargs["resultado"] is ResultadoAuditoria.FALLIDO
    assert "cuenta bloqueada tras 5 intentos" in registrar.call_args.args[2]
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
async def test_token_recuperacion_expirado_es_rechazado(monkeypatch):
    db = MagicMock()

    db.execute = AsyncMock(
        return_value=_resultado_lista([])
    )

    verificar = Mock(return_value=True)

    monkeypatch.setattr(
        auth_service,
        "verificar_token_recuperacion",
        verificar,
    )

    with pytest.raises(BusinessValidationError) as captured:
        await auth_service.restablecer_password(
            db,
            "token-vencido",
            "NuevaClave1!",
        )

    assert captured.value.code == "INVALID_OR_EXPIRED_RESET_TOKEN"

    verificar.assert_not_called()


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
    db.get = AsyncMock(return_value=_usuario())

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


def _db_de_restablecimiento(usuario, registro, sesiones, *, filas_consumidas=1):
    db = MagicMock()
    consumo = MagicMock()
    consumo.rowcount = filas_consumidas
    db.execute = AsyncMock(
        side_effect=[
            _resultado_lista([registro]),  # SELECT del token válido
            consumo,                       # UPDATE condicional que consume el token
            MagicMock(),                   # UPDATE que invalida los demás tokens
            _resultado_lista(sesiones),    # SELECT de sesiones
        ],
    )
    db.get = AsyncMock(return_value=usuario)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _registro_de(usuario):
    return MagicMock(
        usuario_id=usuario.id,
        token_hash="hash",
        usado=False,
        expira_en=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_restablecer_password_consume_el_token_revoca_sesiones_y_levanta_el_bloqueo(monkeypatch):
    usuario = _usuario()
    usuario.intentos_fallidos = 5
    usuario.bloqueado_hasta = datetime.now(timezone.utc) + timedelta(minutes=10)
    sesiones = [MagicMock(revocada=False), MagicMock(revocada=False)]
    db = _db_de_restablecimiento(usuario, _registro_de(usuario), sesiones)

    monkeypatch.setattr(auth_service, "verificar_token_recuperacion", lambda *_: True)
    monkeypatch.setattr(auth_service, "hash_password", lambda _: "nuevo-hash")

    await auth_service.restablecer_password(db, "token", "NuevaClave1!")

    assert usuario.password_hash == "nuevo-hash"
    assert usuario.intentos_fallidos == 0 and usuario.bloqueado_hasta is None  # LTX:RN-012
    assert all(sesion.revocada is True for sesion in sesiones)
    assert db.execute.await_count == 4

    consumo = db.execute.await_args_list[1].args[0]
    assert consumo.is_update and consumo.table.name == "tokens_recuperacion"
    assert "usado" in str(consumo.whereclause)  # condicional: solo consume un token todavía no usado
    otros = db.execute.await_args_list[2].args[0]
    assert otros.is_update and otros.table.name == "tokens_recuperacion"

    db.get.assert_awaited_once_with(Usuario, usuario.id)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_restablecer_password_no_cambia_nada_si_otro_consumio_el_token_antes(monkeypatch):
    usuario = _usuario()
    usuario.intentos_fallidos = 5
    db = _db_de_restablecimiento(usuario, _registro_de(usuario), [], filas_consumidas=0)
    monkeypatch.setattr(auth_service, "verificar_token_recuperacion", lambda *_: True)

    with pytest.raises(BusinessValidationError) as captured:
        await auth_service.restablecer_password(db, "token", "NuevaClave1!")

    assert captured.value.code == "INVALID_OR_EXPIRED_RESET_TOKEN"
    assert usuario.password_hash != "nuevo-hash" and usuario.intentos_fallidos == 5
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_restablecer_password_rechaza_la_contrasena_igual_al_correo_real_de_la_cuenta(monkeypatch):
    usuario = _usuario()
    usuario.correo = "Usuario1@igualab.org"
    db = _db_de_restablecimiento(usuario, _registro_de(usuario), [])
    monkeypatch.setattr(auth_service, "verificar_token_recuperacion", lambda *_: True)

    with pytest.raises(BusinessValidationError) as captured:
        await auth_service.restablecer_password(db, "token", "usuario1@igualab.org")  # cumple la complejidad; es el correo

    assert captured.value.code == "PASSWORD_POLICY_VIOLATION"
    assert any("correo" in e for e in captured.value.details["errors"])
    assert db.execute.await_count == 1  # el token no se consumió: se puede reintentar

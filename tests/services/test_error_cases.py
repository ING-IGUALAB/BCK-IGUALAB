import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.security import HTTPAuthorizationCredentials

import app.dependencies as dependencies
from app.exceptions import (
    AccountDisabledError,
    AccountLockedError,
    AuthenticationError,
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    NotFoundError,
)
from app.models import RolUsuario
from app.services.auth_service import autenticar, restablecer_password
from app.services.usuario_service import (
    cambiar_estado_usuario,
    crear_usuario,
    transferir_superadmin,
)


def _query_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _auth_db(usuario):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_query_result(usuario))
    return db


@pytest.mark.asyncio
async def test_invalid_credentials_have_stable_code():
    with pytest.raises(AuthenticationError) as captured:
        await autenticar(_auth_db(None), "nadie@igualab.org", "Clave1!")

    assert captured.value.code == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_locked_account_has_stable_code():
    usuario = MagicMock()
    usuario.bloqueado_hasta = datetime.now(timezone.utc) + timedelta(minutes=5)

    with pytest.raises(AccountLockedError) as captured:
        await autenticar(_auth_db(usuario), "usuario@igualab.org", "Clave1!")

    assert captured.value.code == "ACCOUNT_LOCKED"
    assert captured.value.details["minutes_remaining"] >= 5


@pytest.mark.asyncio
async def test_disabled_account_has_stable_code():
    usuario = MagicMock()
    usuario.bloqueado_hasta = None
    usuario.habilitado = False

    with pytest.raises(AccountDisabledError) as captured:
        await autenticar(_auth_db(usuario), "usuario@igualab.org", "Clave1!")

    assert captured.value.code == "ACCOUNT_DISABLED"


@pytest.mark.asyncio
async def test_invalid_reset_token_has_stable_code():
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    with pytest.raises(BusinessValidationError) as captured:
        await restablecer_password(db, "token-invalido", "NuevaClave1!")

    assert captured.value.code == "INVALID_OR_EXPIRED_RESET_TOKEN"


@pytest.mark.asyncio
async def test_duplicate_email_has_stable_code():
    db = MagicMock()
    db.execute = AsyncMock(return_value=_query_result(MagicMock()))

    with pytest.raises(ConflictError) as captured:
        await crear_usuario(db, "Nombre", "existente@igualab.org", "Clave1!")

    assert captured.value.code == "EMAIL_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_password_policy_error_has_safe_details():
    db = MagicMock()
    db.execute = AsyncMock(return_value=_query_result(None))

    with pytest.raises(BusinessValidationError) as captured:
        await crear_usuario(db, "Nombre", "nuevo@igualab.org", "debil")

    assert captured.value.code == "PASSWORD_POLICY_VIOLATION"
    assert captured.value.details["errors"]


@pytest.mark.asyncio
async def test_user_not_found_has_stable_code():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as captured:
        actor = MagicMock()
        actor.id = uuid.uuid4()
        
        await cambiar_estado_usuario(db, uuid.uuid4(), True, actor)

    assert captured.value.code == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_only_superadmin_can_transfer_role():
    origen = MagicMock()
    origen.rol = RolUsuario.ADMINISTRADOR

    with pytest.raises(AuthorizationError) as captured:
        await transferir_superadmin(MagicMock(), origen, uuid.uuid4())

    assert captured.value.code == "SUPERADMIN_REQUIRED"


@pytest.mark.asyncio
async def test_missing_credentials_keep_bearer_header():
    with pytest.raises(AuthenticationError) as captured:
        await dependencies.get_current_user(None, MagicMock())

    assert captured.value.code == "INVALID_SESSION"
    assert captured.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
async def test_expired_session_is_revoked(monkeypatch):
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    monkeypatch.setattr(
        dependencies,
        "decodificar_token",
        lambda token: {"sub": str(user_id), "jti": str(session_id)},
    )
    session = MagicMock()
    session.usuario_id = user_id
    session.revocada = False
    session.ultima_actividad = datetime.now(timezone.utc) - timedelta(hours=3)
    db = MagicMock()
    db.get = AsyncMock(return_value=session)
    db.commit = AsyncMock()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")

    with pytest.raises(AuthenticationError) as captured:
        await dependencies.get_current_user(credentials, db)

    assert captured.value.code == "SESSION_EXPIRED"
    assert session.revocada is True
    db.commit.assert_awaited_once()


def test_role_dependency_rejects_insufficient_permissions():
    usuario = MagicMock()
    usuario.rol = RolUsuario.ADMINISTRADOR

    verificar = dependencies.requerir_rol(
        RolUsuario.SUPERADMIN
    )

    with pytest.raises(AuthorizationError) as captured:
        verificar(usuario)

    assert captured.value.code == "FORBIDDEN"

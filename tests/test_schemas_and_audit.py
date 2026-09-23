import uuid
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.audit import registrar_evento
from app.models import (
    RegistroAuditoria,
    TipoEventoAuditoria,
)
from app.schemas import RestablecerPasswordRequest


def test_confirmacion_de_password_debe_coincidir():
    with pytest.raises(ValidationError) as captured:
        RestablecerPasswordRequest(
            token="token",
            password_nueva="NuevaClave1!",
            password_nueva_confirmacion="OtraClave1!",
        )

    assert "Las contraseñas no coinciden" in str(
        captured.value
    )


def test_confirmacion_de_password_valida_es_aceptada():
    payload = RestablecerPasswordRequest(
        token="token",
        password_nueva="NuevaClave1!",
        password_nueva_confirmacion="NuevaClave1!",
    )

    assert (
        payload.password_nueva
        == payload.password_nueva_confirmacion
    )


def test_auditoria_agrega_evento_sin_commit_independiente():
    db = MagicMock()
    usuario_id = uuid.uuid4()

    registrar_evento(
        db,
        TipoEventoAuditoria.INICIO_SESION,
        "Login exitoso",
        usuario_id=usuario_id,
    )

    evento = db.add.call_args.args[0]

    assert isinstance(evento, RegistroAuditoria)
    assert evento.usuario_id == usuario_id
    assert (
        evento.tipo_evento
        == TipoEventoAuditoria.INICIO_SESION
    )
    assert evento.detalle == "Login exitoso"
    assert db.commit.call_count == 0
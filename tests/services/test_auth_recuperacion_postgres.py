"""Recuperación y restablecimiento de contraseña (CU001) sobre un PostgreSQL REAL, AISLADO y TEMPORAL (`conftest.py`).

LTX:RN-010 (enlace de un solo uso y vigencia limitada), RN-011 (política, contraseña ≠ correo), RN-012 (el bloqueo se levanta al
restablecer), RNF-008 (respuesta idéntica para toda cuenta). REAL: transacciones, consumo condicional del token y HTTP. CONTROLADO:
el envío de correo (nunca hay SMTP) que captura el token o falla a pedido.
Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.database import get_db
from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import BusinessValidationError, ServiceUnavailableError
from app.core.request_id import RequestIDMiddleware
from app.core.security import hash_password
from app.models import RolUsuario, Sesion, TokenRecuperacion, Usuario
from app.modules.auth import router as auth
from app.modules.auth import service as auth_service
from tests.ayudantes_analisis_gri import cliente_http, comprobar_error

CLAVE = "ClaveSegura1!"
NUEVA = "NuevaClave2@"
CORREO = "cuenta@pruebas.example.com"


@pytest_asyncio.fixture
async def m1(fabrica_pg, monkeypatch):
    async with fabrica_pg() as db:
        usuario = Usuario(nombre="Cuenta", correo=CORREO, password_hash=hash_password(CLAVE), rol=RolUsuario.ADMINISTRADOR, habilitado=True)
        db.add(usuario)
        await db.commit()
    enviados: list[tuple[str, str]] = []

    async def enviar(destinatario: str, token: str) -> None:
        enviados.append((destinatario, token))

    monkeypatch.setattr(auth_service, "enviar_correo_recuperacion", enviar)
    return SimpleNamespace(fabrica=fabrica_pg, usuario=usuario, enviados=enviados)


def construir_app(m1) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(auth.router)

    async def db_de_prueba():
        async with m1.fabrica() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    return app


async def solicitar(m1) -> str:
    async with m1.fabrica() as db:
        await auth_service.solicitar_recuperacion(db, CORREO)
    return m1.enviados[-1][1]


async def restablecer(m1, token: str, clave: str = NUEVA) -> None:
    async with m1.fabrica() as db:
        await auth_service.restablecer_password(db, token, clave)


async def usuario_actual(m1) -> Usuario:
    async with m1.fabrica() as db:
        return (await db.execute(select(Usuario).where(Usuario.correo == CORREO))).scalar_one()


async def bloquear(m1) -> None:
    async with m1.fabrica() as db:
        u = (await db.execute(select(Usuario).where(Usuario.correo == CORREO))).scalar_one()
        u.intentos_fallidos = 5
        u.bloqueado_hasta = datetime.now(timezone.utc) + timedelta(minutes=15)
        await db.commit()


async def login(m1, clave: str):
    async with cliente_http(construir_app(m1)) as c:
        return await c.post("/auth/login", json={"correo": CORREO, "password": clave})


# ================================================ Restablecimiento: política, desbloqueo, uso único ================================================

async def test_restablecer_levanta_el_bloqueo_y_la_cuenta_entra_de_inmediato_con_la_clave_nueva(m1):
    await bloquear(m1)
    comprobar_error(await login(m1, CLAVE), 423, "ACCOUNT_LOCKED")
    token = await solicitar(m1)

    await restablecer(m1, token)

    u = await usuario_actual(m1)
    assert u.intentos_fallidos == 0  # LTX:RN-012
    assert u.bloqueado_hasta is None
    assert (await login(m1, NUEVA)).status_code == 200
    comprobar_error(await login(m1, CLAVE), 401, "INVALID_CREDENTIALS")  # la anterior ya no sirve


async def test_la_contrasena_igual_al_correo_real_de_la_cuenta_se_rechaza_y_el_token_sigue_valido(m1):
    token = await solicitar(m1)
    clave_igual_al_correo = CORREO.title()  # «Cuenta@pruebas.example.com»: cumple la complejidad, es el correo
    with pytest.raises(BusinessValidationError) as e:
        await restablecer(m1, token, clave_igual_al_correo)
    assert e.value.code == "PASSWORD_POLICY_VIOLATION"
    assert any("correo" in x for x in e.value.details["errors"])
    async with m1.fabrica() as db:
        assert (await db.execute(select(TokenRecuperacion.usado))).scalar_one() is False
    await restablecer(m1, token)  # con una clave válida el mismo token aún sirve
    assert (await login(m1, NUEVA)).status_code == 200


async def test_el_token_es_de_un_solo_uso_y_el_restablecimiento_revoca_las_sesiones(m1):
    async with m1.fabrica() as db:
        sesion = Sesion(usuario_id=m1.usuario.id)
        db.add(sesion)
        await db.commit()
    token = await solicitar(m1)
    await restablecer(m1, token)
    with pytest.raises(BusinessValidationError) as e:
        await restablecer(m1, token, "Otra.Clave3#")
    assert e.value.code == "INVALID_OR_EXPIRED_RESET_TOKEN"
    async with m1.fabrica() as db:
        assert (await db.execute(select(Sesion.revocada))).scalar_one() is True
    assert (await login(m1, NUEVA)).status_code == 200  # la segunda clave nunca se aplicó


async def test_un_token_vencido_no_sirve(m1):
    token = await solicitar(m1)
    async with m1.fabrica() as db:
        await db.execute(text("UPDATE tokens_recuperacion SET expira_en = now() - interval '1 minute'"))
        await db.commit()
    with pytest.raises(BusinessValidationError) as e:
        await restablecer(m1, token)
    assert e.value.code == "INVALID_OR_EXPIRED_RESET_TOKEN"


async def test_si_otra_solicitud_consume_el_token_entre_la_lectura_y_el_consumo_no_se_cambia_la_contrasena(m1):
    """Carrera forzada: justo después de que esta solicitud LEE el token como vigente, otra solicitud lo usa y confirma. El consumo
    condicional debe fallar y esta solicitud no puede cambiar la contraseña ni el bloqueo (antes el UPDATE no era condicional)."""
    await bloquear(m1)
    token = await solicitar(m1)
    async with m1.fabrica() as perdedora:
        execute_real = perdedora.execute
        ya_corrio_la_ganadora = []

        async def execute_con_carrera(sentencia, *args, **kwargs):
            resultado = await execute_real(sentencia, *args, **kwargs)
            if not ya_corrio_la_ganadora:  # tras el SELECT de tokens (el primero), gana otra solicitud
                ya_corrio_la_ganadora.append(True)
                async with m1.fabrica() as ganadora:
                    await auth_service.restablecer_password(ganadora, token, "Ganadora.Clave3#")
            return resultado

        perdedora.execute = execute_con_carrera
        with pytest.raises(BusinessValidationError) as e:
            await auth_service.restablecer_password(perdedora, token, "Perdedora.Clave4$")
    assert e.value.code == "INVALID_OR_EXPIRED_RESET_TOKEN"
    assert (await login(m1, "Ganadora.Clave3#")).status_code == 200  # la clave de la ganadora se conserva
    comprobar_error(await login(m1, "Perdedora.Clave4$"), 401, "INVALID_CREDENTIALS")


async def test_dos_restablecimientos_simultaneos_con_el_mismo_token_dejan_una_sola_clave(m1):
    token = await solicitar(m1)

    async def intento(clave):
        async with m1.fabrica() as db:
            try:
                await auth_service.restablecer_password(db, token, clave)
                return None
            except BusinessValidationError as exc:
                return exc.code

    resultados = await asyncio.gather(intento("Primera.Clave3#"), intento("Segunda.Clave4$"))
    assert sorted(r or "OK" for r in resultados) == ["INVALID_OR_EXPIRED_RESET_TOKEN", "OK"]
    entraron = [(await login(m1, c)).status_code for c in ("Primera.Clave3#", "Segunda.Clave4$")]
    assert sorted(entraron) == [200, 401]  # exactamente una de las dos claves quedó aplicada


# ================================================ Solicitud: respuesta uniforme (RNF-008) ================================================

@pytest.mark.parametrize("falla", [None, ServiceUnavailableError("MAIL_NOT_CONFIGURED", "sin correo"), RuntimeError("smtp.interno:587 usuario@dominio rechazó")])
async def test_la_respuesta_publica_es_identica_para_cuentas_registradas_y_no_registradas_aunque_falle_el_correo(m1, monkeypatch, caplog, falla):
    if falla is not None:
        async def enviar_falla(destinatario, token):
            raise falla

        monkeypatch.setattr(auth_service, "enviar_correo_recuperacion", enviar_falla)
    caplog.set_level(logging.INFO)
    async with cliente_http(construir_app(m1)) as c:
        registrada = await c.post("/auth/recuperar-contrasena", json={"correo": CORREO})
        inexistente = await c.post("/auth/recuperar-contrasena", json={"correo": "nadie@pruebas.example.com"})
    assert registrada.status_code == inexistente.status_code == 202
    assert registrada.json() == inexistente.json()
    assert "entregado" not in registrada.json()["mensaje"].lower().replace("no ", "")  # no se afirma entrega
    # el fallo queda registrado internamente SIN el destinatario, el servidor SMTP ni el token
    registro = " ".join(r.getMessage() for r in caplog.records if r.name == "igualab.auth")
    for secreto in (CORREO, "nadie@pruebas.example.com", "smtp.interno", "usuario@dominio"):
        assert secreto not in registro
    if falla is not None:
        assert "No se pudo enviar el correo de recuperación" in registro
    async with m1.fabrica() as db:  # la cuenta registrada sí recibió un token vigente aunque el envío fallara
        assert len((await db.execute(select(TokenRecuperacion))).scalars().all()) == 1


async def test_con_el_correo_disponible_se_envia_un_token_y_la_respuesta_es_la_misma(m1):
    async with cliente_http(construir_app(m1)) as c:
        r = await c.post("/auth/recuperar-contrasena", json={"correo": CORREO})
    assert r.status_code == 202
    assert len(m1.enviados) == 1
    assert m1.enviados[0][0] == CORREO
    assert m1.enviados[0][1] not in r.text

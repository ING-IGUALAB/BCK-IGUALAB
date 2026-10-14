"""Script administrativo `scripts/restablecer_superadmin.py`.

Nivel: unidad (rutas de aborto, sin base de datos) e integración con el PostgreSQL TEMPORAL de las pruebas. Nunca se conecta
a QA: el archivo de entorno y la sesión de base de datos se sustituyen por dobles locales.
"""
import os
from datetime import datetime, timedelta, timezone

import dotenv
import pytest
from sqlalchemy import select

from app import audit
from app.models import RegistroAuditoria, RolUsuario, Sesion, TipoEventoAuditoria, TokenRecuperacion, Usuario
from app.security import verificar_password
from scripts import restablecer_superadmin as script
from tests.ayudantes_ingesta import crear_usuario

CORREO = "superadmin@pruebas.invalid"
PASSWORD_NUEVA = "NuevaClave#2026"
VALORES_QA = {
    "APP_ENV": "qa",
    "DATABASE_URL": "postgresql+asyncpg://usuario:SECRETO@servidor-privado:5432/igualab_qa",
    "JWT_SECRET_KEY": "x",
    "SUPERADMIN_CORREO": CORREO,
}


def escribir_env(ruta, **cambios):
    valores = {**VALORES_QA, **cambios}
    ruta.write_text("\n".join(f"{k}={v}" for k, v in valores.items() if v is not None), encoding="utf-8")
    return ruta


@pytest.fixture
def entorno_aislado(monkeypatch, tmp_path):
    """Evita que `cargar_entorno_qa` contamine el proceso de pruebas (os.environ y dotenv.load_dotenv)."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(dotenv, "load_dotenv", dotenv.load_dotenv)
    return tmp_path


# ============================================ Abortos sin base de datos ============================================

def test_sin_env_qa_aborta(monkeypatch, entorno_aislado):
    monkeypatch.setattr(script, "ARCHIVO_ENV", entorno_aislado / "no-existe.env")
    with pytest.raises(script.Abortar):
        script.cargar_entorno_qa()


@pytest.mark.parametrize("faltante", script.VARIABLES_OBLIGATORIAS)
def test_falta_de_una_variable_obligatoria_aborta(monkeypatch, entorno_aislado, faltante):
    monkeypatch.setattr(script, "ARCHIVO_ENV", escribir_env(entorno_aislado / ".env.qa", **{faltante: None}))
    with pytest.raises(script.Abortar) as capturado:
        script.cargar_entorno_qa()
    assert faltante in str(capturado.value)


def test_el_env_qa_manda_sobre_la_terminal_y_neutraliza_el_load_dotenv_de_la_app(monkeypatch, entorno_aislado):
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://otro@otro/igualab"
    monkeypatch.setattr(script, "ARCHIVO_ENV", escribir_env(entorno_aislado / ".env.qa"))
    valores = script.cargar_entorno_qa()
    assert os.environ["DATABASE_URL"] == VALORES_QA["DATABASE_URL"]
    assert valores["APP_ENV"] == "qa"
    assert dotenv.load_dotenv() is False  # `app.config` ya no puede cargar `.env` (desarrollo)


def test_un_entorno_distinto_de_qa_aborta():
    with pytest.raises(script.Abortar):
        script.verificar_destino({**VALORES_QA, "APP_ENV": "development"})


@pytest.mark.parametrize("base", ["igualab", "igualab_uat", "igualab_qa2", "IGUALAB_QA", ""])
def test_una_base_distinta_de_igualab_qa_aborta_sin_mostrar_la_url(base):
    url = f"postgresql+asyncpg://usuario:SECRETO@servidor-privado:5432/{base}"
    with pytest.raises(script.Abortar) as capturado:
        script.verificar_destino({**VALORES_QA, "DATABASE_URL": url})
    for dato in ("SECRETO", "servidor-privado", "usuario"):
        assert dato not in str(capturado.value)


def test_el_destino_correcto_se_acepta():
    script.verificar_destino(VALORES_QA)


def test_las_contrasenas_distintas_abortan(monkeypatch):
    respuestas = iter([PASSWORD_NUEVA, "OtraClave#2026"])
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: next(respuestas))
    with pytest.raises(script.Abortar):
        script.pedir_password(CORREO)


@pytest.mark.parametrize("debil", ["corta1!", "sinmayuscula1!", "SINMINUSCULA1!", "SinDigitos!!", "SinEspecial12"])
def test_una_contrasena_que_incumple_la_politica_aborta(monkeypatch, debil):
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: debil)
    with pytest.raises(script.Abortar):
        script.pedir_password(CORREO)


def test_una_contrasena_valida_se_acepta(monkeypatch):
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: PASSWORD_NUEVA)
    assert script.pedir_password(CORREO) == PASSWORD_NUEVA


# ============================================ Con el PostgreSQL temporal ============================================

@pytest.fixture
def sesion_local(monkeypatch, fabrica_pg):
    """El script abre sus sesiones con `app.database.AsyncSessionLocal`: apunta al clúster temporal."""
    import app.database

    monkeypatch.setattr(app.database, "AsyncSessionLocal", fabrica_pg)
    return fabrica_pg


async def preparar_cuenta(fabrica, **datos_usuario):
    ahora = datetime.now(timezone.utc)
    async with fabrica() as db:
        usuario = await crear_usuario(db, correo=CORREO, **datos_usuario)
        usuario.intentos_fallidos = 5
        usuario.bloqueado_hasta = ahora + timedelta(minutes=30)
        db.add_all([Sesion(usuario_id=usuario.id), Sesion(usuario_id=usuario.id), Sesion(usuario_id=usuario.id, revocada=True)])
        db.add_all([
            TokenRecuperacion(usuario_id=usuario.id, token_hash="h1", expira_en=ahora + timedelta(hours=1)),
            TokenRecuperacion(usuario_id=usuario.id, token_hash="h2", expira_en=ahora + timedelta(hours=1), usado=True),
        ])
        await db.commit()
        return usuario.id


async def test_restablece_todo_en_una_transaccion_y_audita_sin_secretos(sesion_local):
    usuario_id = await preparar_cuenta(sesion_local)
    async with sesion_local() as db:
        otro = await crear_usuario(db, rol=RolUsuario.ADMINISTRADOR, correo="otro@pruebas.invalid")
        db.add(Sesion(usuario_id=otro.id))
        await db.commit()
        otro_id = otro.id

    resultado = await script.restablecer(CORREO, PASSWORD_NUEVA)

    assert resultado == {"sesiones_revocadas": 2, "tokens_invalidados": 1}
    async with sesion_local() as db:
        usuario = await db.get(Usuario, usuario_id)
        assert verificar_password(PASSWORD_NUEVA, usuario.password_hash)
        assert usuario.password_hash != PASSWORD_NUEVA
        assert usuario.intentos_fallidos == 0
        assert usuario.bloqueado_hasta is None
        assert usuario.rol is RolUsuario.SUPERADMIN
        sesiones = (await db.execute(select(Sesion).where(Sesion.usuario_id == usuario_id))).scalars().all()
        assert sesiones and all(s.revocada for s in sesiones)
        tokens = (await db.execute(select(TokenRecuperacion).where(TokenRecuperacion.usuario_id == usuario_id))).scalars().all()
        assert tokens and all(t.usado for t in tokens)
        eventos = (await db.execute(select(RegistroAuditoria))).scalars().all()
        assert [(e.tipo_evento, e.usuario_id) for e in eventos] == [(TipoEventoAuditoria.CAMBIO_ROL, usuario_id)]
        texto = eventos[0].detalle
        assert PASSWORD_NUEVA not in texto
        assert usuario.password_hash not in texto
        # Otra cuenta no se toca.
        ajena = (await db.execute(select(Sesion).where(Sesion.usuario_id == otro_id))).scalar_one()
        assert ajena.revocada is False


async def test_si_algo_falla_no_se_aplica_ningun_cambio(sesion_local, monkeypatch):
    usuario_id = await preparar_cuenta(sesion_local)
    async with sesion_local() as db:
        antes = (await db.get(Usuario, usuario_id)).password_hash

    def auditoria_rota(*args, **kwargs):
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(audit, "registrar_evento", auditoria_rota)
    with pytest.raises(RuntimeError):
        await script.restablecer(CORREO, PASSWORD_NUEVA)

    async with sesion_local() as db:
        usuario = await db.get(Usuario, usuario_id)
        assert usuario.password_hash == antes
        assert usuario.intentos_fallidos == 5
        assert usuario.bloqueado_hasta is not None
        sesiones = (await db.execute(select(Sesion).where(Sesion.usuario_id == usuario_id, Sesion.revocada.is_(False)))).scalars().all()
        assert len(sesiones) == 2
        assert (await db.execute(select(RegistroAuditoria))).scalars().all() == []


async def test_un_usuario_inexistente_aborta_y_no_crea_nada(sesion_local):
    with pytest.raises(script.Abortar):
        await script.restablecer("nadie@pruebas.invalid", PASSWORD_NUEVA)
    async with sesion_local() as db:
        assert (await db.execute(select(Usuario))).scalars().all() == []


async def test_un_usuario_sin_rol_superadmin_aborta_sin_modificarlo(sesion_local):
    async with sesion_local() as db:
        admin = await crear_usuario(db, rol=RolUsuario.ADMINISTRADOR, correo=CORREO)
        hash_original = admin.password_hash
    with pytest.raises(script.Abortar):
        await script.restablecer(CORREO, PASSWORD_NUEVA)
    with pytest.raises(script.Abortar):
        await script.verificar(CORREO)
    async with sesion_local() as db:
        assert (await db.execute(select(Usuario.password_hash))).scalar_one() == hash_original
        assert (await db.execute(select(RegistroAuditoria))).scalars().all() == []


async def test_el_modo_lectura_no_modifica_nada_ni_pide_contrasena(sesion_local, monkeypatch, entorno_aislado, capsys):
    usuario_id = await preparar_cuenta(sesion_local)
    monkeypatch.setattr(script, "ARCHIVO_ENV", escribir_env(entorno_aislado / ".env.qa"))
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: pytest.fail("no debe pedir contraseña sin --ejecutar"))
    import app.config

    monkeypatch.setattr(app.config.settings, "APP_ENV", "qa")

    assert await script.principal(ejecutar=False) == 0

    salida = capsys.readouterr().out
    assert "Modo lectura" in salida
    for dato in ("SECRETO", "servidor-privado", "usuario:"):
        assert dato not in salida
    async with sesion_local() as db:
        usuario = await db.get(Usuario, usuario_id)
        assert usuario.password_hash == "hash-de-prueba"
        assert usuario.intentos_fallidos == 5


async def test_con_ejecutar_pide_la_contrasena_aplica_el_cambio_y_no_la_imprime(sesion_local, monkeypatch, entorno_aislado, capsys):
    usuario_id = await preparar_cuenta(sesion_local)
    monkeypatch.setattr(script, "ARCHIVO_ENV", escribir_env(entorno_aislado / ".env.qa"))
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: PASSWORD_NUEVA)
    import app.config

    monkeypatch.setattr(app.config.settings, "APP_ENV", "qa")

    assert await script.principal(ejecutar=True) == 0

    captura = capsys.readouterr()
    assert PASSWORD_NUEVA not in captura.out + captura.err
    async with sesion_local() as db:
        usuario = await db.get(Usuario, usuario_id)
        assert verificar_password(PASSWORD_NUEVA, usuario.password_hash)
        assert usuario.bloqueado_hasta is None


def test_el_unico_argumento_del_script_es_ejecutar():
    """La contraseña nunca se recibe por argumento: solo existe `--ejecutar`."""
    fuente = open(script.__file__, encoding="utf-8").read()
    assert fuente.count("add_argument(") == 1
    assert 'add_argument("--ejecutar"' in fuente

"""Script administrativo `scripts/restablecer_superadmin.py`.

Nivel: unidad (rutas de aborto, sin base de datos) e integración con el PostgreSQL TEMPORAL de las pruebas. Nunca se conecta
a QA: el archivo de entorno y la sesión de base de datos se sustituyen por dobles locales.
"""
import os
from datetime import datetime, timedelta, timezone

import dotenv
import pytest
from sqlalchemy import select

from app.modules.auditoria import service as audit
from app.models import RegistroAuditoria, RolUsuario, Sesion, TipoEventoAuditoria, TokenRecuperacion, Usuario
from app.core.security import verificar_password
from scripts import restablecer_superadmin as script
from tests.ayudantes_ingesta import crear_usuario

CORREO = "superadmin@pruebas.invalid"
PASSWORD_NUEVA = "NuevaClave#2026"
VALORES_QA = {
    "APP_ENV": "qa",
    "DATABASE_URL": "postgresql+asyncpg://usuario:SECRETO@servidor-privado:5432/igualab_qa",
    "JWT_SECRET_KEY": "x",
    "SUPERADMIN_CORREO": "otra-cuenta@pruebas.invalid",  # el script ya no la usa: la cuenta sale de --correo
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
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://otro@otro/igualab")
    monkeypatch.setattr(script, "ARCHIVO_ENV", escribir_env(entorno_aislado / ".env.qa"))
    valores = script.cargar_entorno_qa()
    assert os.environ["DATABASE_URL"] == VALORES_QA["DATABASE_URL"]
    assert valores["APP_ENV"] == "qa"
    assert dotenv.load_dotenv() is False  # `app.core.config` ya no puede cargar `.env` (desarrollo)


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
    """El script abre sus sesiones con `app.core.database.AsyncSessionLocal`: apunta al clúster temporal."""
    import app.core.database

    monkeypatch.setattr(app.core.database, "AsyncSessionLocal", fabrica_pg)
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
        assert sesiones
        assert all(s.revocada for s in sesiones)
        tokens = (await db.execute(select(TokenRecuperacion).where(TokenRecuperacion.usuario_id == usuario_id))).scalars().all()
        assert tokens
        assert all(t.usado for t in tokens)
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
    import app.core.config

    monkeypatch.setattr(app.core.config.settings, "APP_ENV", "qa")

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
    import app.core.config

    monkeypatch.setattr(app.core.config.settings, "APP_ENV", "qa")

    assert await script.principal(ejecutar=True, correo=CORREO) == 0

    captura = capsys.readouterr()
    assert PASSWORD_NUEVA not in captura.out + captura.err
    async with sesion_local() as db:
        usuario = await db.get(Usuario, usuario_id)
        assert verificar_password(PASSWORD_NUEVA, usuario.password_hash)
        assert usuario.bloqueado_hasta is None


def test_los_unicos_argumentos_del_script_son_ejecutar_y_correo():
    """La contraseña nunca se recibe por argumento: solo existen `--ejecutar` y `--correo`."""
    fuente = open(script.__file__, encoding="utf-8").read()
    assert fuente.count("add_argument(") == 2
    assert 'add_argument("--ejecutar"' in fuente
    assert 'add_argument("--correo"' in fuente


# ============================================ Cuenta explícita, un solo SuperAdmin y solo lectura ============================================

CORREO_ADMIN = "oscar.administrador@pruebas.invalid"


@pytest.fixture
def qa_simulado(monkeypatch, entorno_aislado, sesion_local):
    """Entorno QA ficticio (el archivo y la base son locales) y sesión apuntando al clúster temporal."""
    import app.core.config

    monkeypatch.setattr(script, "ARCHIVO_ENV", escribir_env(entorno_aislado / ".env.qa"))
    monkeypatch.setattr(app.core.config.settings, "APP_ENV", "qa")
    return sesion_local


async def crear_administrador(fabrica, habilitado=True):
    async with fabrica() as db:
        admin = await crear_usuario(db, rol=RolUsuario.ADMINISTRADOR, correo=CORREO_ADMIN, habilitado=habilitado)
        db.add_all([
            Sesion(usuario_id=admin.id),
            TokenRecuperacion(usuario_id=admin.id, token_hash="admin", expira_en=datetime.now(timezone.utc) + timedelta(hours=1)),
        ])
        await db.commit()
        return admin.id


async def foto_de(fabrica, usuario_id):
    async with fabrica() as db:
        usuario = await db.get(Usuario, usuario_id)
        sesiones = (await db.execute(select(Sesion.revocada).where(Sesion.usuario_id == usuario_id))).scalars().all()
        tokens = (await db.execute(select(TokenRecuperacion.usado).where(TokenRecuperacion.usuario_id == usuario_id))).scalars().all()
        return (usuario.password_hash, usuario.rol, usuario.habilitado, usuario.intentos_fallidos, usuario.bloqueado_hasta,
                list(sesiones), list(tokens))


async def test_la_cuenta_sale_del_correo_explicito_aunque_el_env_apunte_a_otra(qa_simulado, capsys):
    usuario_id = await preparar_cuenta(qa_simulado)
    await crear_administrador(qa_simulado)
    assert await script.principal(ejecutar=False) == 0  # sin --correo identifica al único SuperAdmin
    salida = capsys.readouterr().out
    assert CORREO in salida
    assert "SuperAdmin" in salida
    assert "otra-cuenta@pruebas.invalid" not in salida
    assert "hash-de-prueba" not in salida
    assert str(usuario_id) not in salida  # solo correo, nombre, rol y habilitado
    assert await script.principal(ejecutar=False, correo=f"  {CORREO.upper()} ") == 0  # el correo se normaliza


async def test_ejecutar_sin_correo_aborta_antes_de_pedir_la_contrasena(qa_simulado, monkeypatch):
    usuario_id = await preparar_cuenta(qa_simulado)
    antes = await foto_de(qa_simulado, usuario_id)
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: pytest.fail("no debe pedir contraseña sin correo explícito"))
    for correo in (None, "", "   "):
        with pytest.raises(script.Abortar):
            await script.principal(ejecutar=True, correo=correo)
    assert await foto_de(qa_simulado, usuario_id) == antes


@pytest.mark.parametrize("correo", ["sin-arroba", "dos@@pruebas.invalid", "con espacio@pruebas.invalid", "@pruebas.invalid", "cuenta@"])
async def test_un_correo_con_formato_invalido_aborta(qa_simulado, correo):
    await preparar_cuenta(qa_simulado)
    with pytest.raises(script.Abortar):
        await script.principal(ejecutar=False, correo=correo)


async def test_si_no_hay_exactamente_un_superadmin_se_informa_y_no_se_modifica_nada(qa_simulado, monkeypatch):
    admin_id = await crear_administrador(qa_simulado)  # no hay ningún SuperAdmin
    antes = await foto_de(qa_simulado, admin_id)
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: pytest.fail("no debe pedir contraseña"))
    with pytest.raises(script.Abortar) as capturado:
        await script.principal(ejecutar=True, correo=CORREO_ADMIN)
    assert "exactamente un SuperAdmin" in str(capturado.value)
    assert await foto_de(qa_simulado, admin_id) == antes
    async with qa_simulado() as db:
        assert (await db.execute(select(RegistroAuditoria))).scalars().all() == []


async def test_con_varios_superadmin_el_script_no_elige_ninguno():
    class Sesion2:
        async def execute(self, _consulta):
            class Resultado:
                def scalars(self):
                    return self

                def all(self):
                    return ["uno", "otro"]

            return Resultado()

    with pytest.raises(script.Abortar) as capturado:
        await script.identificar_superadmin(Sesion2())
    assert "hay 2" in str(capturado.value)


async def test_el_correo_de_otra_cuenta_no_se_acepta_y_nada_cambia(qa_simulado, monkeypatch):
    superadmin_id = await preparar_cuenta(qa_simulado)
    admin_id = await crear_administrador(qa_simulado)
    antes = (await foto_de(qa_simulado, superadmin_id), await foto_de(qa_simulado, admin_id))
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: pytest.fail("no debe pedir contraseña"))
    for ejecutar in (False, True):
        with pytest.raises(script.Abortar):
            await script.principal(ejecutar=ejecutar, correo=CORREO_ADMIN)
    assert (await foto_de(qa_simulado, superadmin_id), await foto_de(qa_simulado, admin_id)) == antes


async def test_restablecer_solo_toca_al_superadmin_y_no_cambia_roles_ni_habilita_a_nadie(qa_simulado, monkeypatch, capsys):
    superadmin_id = await preparar_cuenta(qa_simulado, habilitado=False)  # deshabilitada: el script no la habilita
    admin_id = await crear_administrador(qa_simulado)
    admin_antes = await foto_de(qa_simulado, admin_id)
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: PASSWORD_NUEVA)

    assert await script.principal(ejecutar=True, correo=CORREO) == 0

    captura = capsys.readouterr()
    assert PASSWORD_NUEVA not in captura.out + captura.err
    assert "AVISO" in captura.out
    assert await foto_de(qa_simulado, admin_id) == admin_antes  # contraseña, rol, estado, sesiones y tokens de Oscar intactos
    async with qa_simulado() as db:
        superadmin = await db.get(Usuario, superadmin_id)
        assert verificar_password(PASSWORD_NUEVA, superadmin.password_hash)
        assert superadmin.rol is RolUsuario.SUPERADMIN  # el rol no cambia
        assert superadmin.habilitado is False  # no se habilita automáticamente
        roles = dict((await db.execute(select(Usuario.correo, Usuario.rol))).all())
        assert roles == {CORREO: RolUsuario.SUPERADMIN, CORREO_ADMIN: RolUsuario.ADMINISTRADOR}
        eventos = (await db.execute(select(RegistroAuditoria))).scalars().all()
        assert [(e.tipo_evento, e.usuario_id) for e in eventos] == [(TipoEventoAuditoria.CAMBIO_ROL, superadmin_id)]


async def test_la_revalidacion_dentro_de_la_transaccion_detecta_que_ya_no_es_superadmin(qa_simulado, monkeypatch):
    superadmin_id = await preparar_cuenta(qa_simulado)
    antes = await foto_de(qa_simulado, superadmin_id)
    original = script.buscar_superadmin

    async def degradar_antes_de_bloquear(db, correo, bloquear=False):
        if bloquear:  # otra sesión transfirió el rol entre la verificación y la transacción
            async with qa_simulado() as otra:
                (await otra.get(Usuario, superadmin_id)).rol = RolUsuario.ADMINISTRADOR
                await otra.commit()
        return await original(db, correo, bloquear)

    monkeypatch.setattr(script, "buscar_superadmin", degradar_antes_de_bloquear)
    monkeypatch.setattr(script.getpass, "getpass", lambda *_: PASSWORD_NUEVA)
    with pytest.raises(script.Abortar):
        await script.restablecer(CORREO, PASSWORD_NUEVA)
    despues = await foto_de(qa_simulado, superadmin_id)
    assert despues[0] == antes[0]  # contraseña sin cambios
    assert despues[3:] == antes[3:]  # intentos, bloqueo, sesiones y tokens sin cambios
    async with qa_simulado() as db:
        assert (await db.execute(select(RegistroAuditoria))).scalars().all() == []


async def test_la_verificacion_corre_en_una_transaccion_de_solo_lectura(qa_simulado, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    superadmin_id = await preparar_cuenta(qa_simulado)
    antes = await foto_de(qa_simulado, superadmin_id)

    async def intenta_escribir(db):
        await db.execute(text("UPDATE usuarios SET nombre = 'escritura no permitida'"))

    monkeypatch.setattr(script, "identificar_superadmin", intenta_escribir)
    with pytest.raises(DBAPIError):
        await script.verificar()
    assert await foto_de(qa_simulado, superadmin_id) == antes
    async with qa_simulado() as db:
        assert (await db.get(Usuario, superadmin_id)).nombre != "escritura no permitida"

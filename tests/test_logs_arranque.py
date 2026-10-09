"""Logs INFO de inicio, fin y duración de cada paso del arranque (diagnóstico de «Waiting for application startup»). Sin bases de
datos ni servicios: los pasos se sustituyen por dobles y se comprueba lo que el log dice —y lo que NO debe decir—."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.logging_config import paso_de_arranque
from app.migraciones import catalogo, motor
from app.migraciones.motor import ErrorMigracion
from app.services.ingesta import gestor as modulo_gestor
from app.services.ingesta.gestor import GestorIngesta, OpcionesGestor
from tests.test_gestor_flujo import _AppDoble, _gestor_doble, sesion_nula

SECRETOS = ("CLAVE-SECRETA", "usuario_secreto", "host-secreto", "PRIVATE KEY")


def mensajes(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def sin_secretos(caplog) -> None:
    texto = " ".join(mensajes(caplog))
    for secreto in SECRETOS:
        assert secreto not in texto


# ============================================ El cronometrador ============================================

def test_un_paso_correcto_registra_inicio_y_fin_con_duracion_en_info(caplog):
    logger = logging.getLogger("igualab.startup")
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with paso_de_arranque(logger, "1/6 Prueba"):
            pass
    inicio, fin = caplog.records
    assert (inicio.levelno, fin.levelno) == (logging.INFO, logging.INFO)
    assert inicio.getMessage() == "Arranque | 1/6 Prueba | inicio"
    assert fin.getMessage().startswith("Arranque | 1/6 Prueba | fin en ") and fin.getMessage().endswith(" s")


def test_un_paso_que_falla_registra_solo_la_clase_y_relanza(caplog):
    logger = logging.getLogger("igualab.startup")
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with pytest.raises(OSError):
            with paso_de_arranque(logger, "2/6 Falla"):
                raise OSError("host-secreto:5432 usuario_secreto:CLAVE-SECRETA")
    assert [r.levelno for r in caplog.records] == [logging.INFO, logging.ERROR]
    assert "falló o se interrumpió tras" in caplog.records[1].getMessage() and "(OSError)" in caplog.records[1].getMessage()
    assert caplog.records[1].exc_info is None  # sin traceback: el mensaje de la excepción puede traer hosts o credenciales
    sin_secretos(caplog)


async def test_un_paso_cancelado_se_registra_y_la_cancelacion_se_propaga(caplog):
    logger = logging.getLogger("igualab.startup")

    async def paso():
        with paso_de_arranque(logger, "3/6 Espera"):
            await asyncio.sleep(60)

    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        tarea = asyncio.create_task(paso())
        await asyncio.sleep(0.05)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert "(CancelledError)" in caplog.records[-1].getMessage()


# ============================================ Los seis pasos ============================================

async def test_el_lifespan_registra_los_seis_pasos_en_orden_con_su_duracion(monkeypatch, caplog):
    from app import main

    async def crear_tablas():
        pass

    async def superadmin():
        pass

    async def migrar_doble(url, esquema, **kwargs):
        return motor.ResultadoMigracion(esquema.nombre, previas=(1, 2))

    monkeypatch.setattr(main, "crear_tablas", crear_tablas)
    monkeypatch.setattr(main, "crear_superadmin_inicial", superadmin)
    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", "postgresql+asyncpg://usuario_secreto:CLAVE-SECRETA@host-secreto/bd")
    monkeypatch.setattr(catalogo.settings, "VECTOR_DATABASE_URL", "postgresql+asyncpg://usuario_secreto:CLAVE-SECRETA@host-secreto/vec")
    gestor = _gestor_doble([], monkeypatch)
    gestor.opciones = OpcionesGestor(recuperacion_habilitada=True, intervalo_recuperacion=3600, retraso_primer_barrido=3600)

    with caplog.at_level(logging.INFO):
        async with main.app.router.lifespan_context(main.app):
            assert main.app.state.ingesta is gestor
    registros = [m for m in mensajes(caplog) if m.startswith("Arranque |")]
    esperado = [
        "Arranque | lifespan | inicio", "Arranque | 1/6 Creación de tablas existentes | inicio", "Arranque | 1/6 Creación de tablas existentes | fin en",
        "Arranque | 2/6 Inicialización del SuperAdmin | inicio", "Arranque | 2/6 Inicialización del SuperAdmin | fin en",
        "Arranque | 3/6 Construcción de recursos de ingesta | inicio", "Arranque | 3/6 Construcción de recursos de ingesta | fin en",
        "Arranque | 4/6 Migraciones transaccionales | inicio", "Arranque | 4/6 Migraciones transaccionales | fin en",
        "Arranque | 5/6 Migraciones vectoriales | inicio", "Arranque | 5/6 Migraciones vectoriales | fin en",
        "Arranque | 6/6 Inicio del gestor y recuperación | inicio", "Arranque | 6/6 Inicio del gestor y recuperación | fin en",
        "Arranque | lifespan | fin en",
    ]
    assert len(registros) == len(esperado)
    for real, previsto in zip(registros, esperado):
        assert real.startswith(previsto), (real, previsto)
    assert registros[-1].endswith("(ingesta habilitada)")
    assert any("Recuperación periódica programada (primer barrido en" in m for m in mensajes(caplog))
    sin_secretos(caplog)


async def test_si_un_paso_se_cuelga_el_log_muestra_su_inicio_sin_fin(monkeypatch, caplog):
    from app import main

    parar = asyncio.Event()

    async def crear_tablas():
        await parar.wait()  # simula una espera indefinida en la base de datos

    monkeypatch.setattr(main, "crear_tablas", crear_tablas)
    with caplog.at_level(logging.INFO):
        tarea = asyncio.create_task(main.app.router.lifespan_context(main.app).__aenter__())
        await asyncio.sleep(0.1)
        arranque = [m for m in mensajes(caplog) if m.startswith("Arranque |")]
        assert arranque == ["Arranque | lifespan | inicio (ambiente=%s)" % main.settings.APP_ENV,
                            "Arranque | 1/6 Creación de tablas existentes | inicio"]  # y nada más: ese es el paso atascado
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert "falló o se interrumpió" in mensajes(caplog)[-1]


async def test_un_fallo_de_migracion_se_registra_en_su_paso_y_el_arranque_continua(monkeypatch, caplog):
    app = _AppDoble()
    orden: list = []
    _gestor_doble(orden, monkeypatch)

    async def migrar_doble(url, esquema, **kwargs):
        if esquema.nombre == "vectorial":
            raise ErrorMigracion("PGVECTOR_REQUIRED", "Falta pgvector.", "vectorial", {"sqlstate": "42501"})
        return motor.ResultadoMigracion(esquema.nombre)

    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", "postgresql+asyncpg://u@h/db")
    monkeypatch.setattr(catalogo.settings, "VECTOR_DATABASE_URL", "postgresql+asyncpg://u@h/vec")
    with caplog.at_level(logging.INFO):
        await modulo_gestor.iniciar_ingesta(app)
    texto = mensajes(caplog)
    assert "Arranque | 4/6 Migraciones transaccionales | inicio" in texto
    assert any(m.startswith("Arranque | 4/6 Migraciones transaccionales | fin en") for m in texto)
    assert "Arranque | 5/6 Migraciones vectoriales | inicio" in texto
    assert any(m.startswith("Arranque | 5/6 Migraciones vectoriales | falló o se interrumpió") and "(ErrorMigracion)" in m for m in texto)
    assert not any(m.startswith("Arranque | 6/6") for m in texto)  # no se llegó a iniciar el gestor
    assert app.state.ingesta is None and app.state.ingesta_error[0] == "INGESTION_SCHEMA_NOT_READY"


# ============================================ Subpasos de las migraciones ============================================

class ConexionMinima:
    def __init__(self, ocupada: int = 0):
        self.ocupada = ocupada

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            if self.ocupada > 0:
                self.ocupada -= 1
                return False
            return True
        return 1


async def test_el_bloqueo_registra_su_obtencion_y_avisa_si_la_espera_se_prolonga(monkeypatch, caplog):
    reloj = {"t": 1000.0}
    monkeypatch.setattr(motor.time, "monotonic", lambda: reloj["t"])

    async def dormir(segundos):
        reloj["t"] += 6  # cada vuelta «pasan» 6 s

    monkeypatch.setattr(motor.asyncio, "sleep", dormir)
    with caplog.at_level(logging.INFO, logger="igualab.migraciones"):
        await motor._tomar_candado(ConexionMinima(ocupada=4), "transaccional", espera=60)
    texto = mensajes(caplog)
    assert any("esperando el bloqueo de migraciones" in m for m in texto)  # aviso a los ~10 s sin cambiar el comportamiento
    assert texto[-1].startswith("Base transaccional: bloqueo de migraciones obtenido tras ")


async def test_el_bloqueo_agotado_sigue_fallando_igual_y_sin_credenciales(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="igualab.migraciones"):
        with pytest.raises(ErrorMigracion) as error:
            await motor._tomar_candado(ConexionMinima(ocupada=10**6), "vectorial", espera=0.3)
    assert error.value.codigo == "MIGRATION_LOCK_TIMEOUT"
    sin_secretos(caplog)


async def test_migrar_registra_conexion_y_lectura_del_registro(monkeypatch, caplog):
    from tests.test_motor_migraciones_sin_base import ConexionSimulada

    c = ConexionSimulada(catalogo.ESQUEMA_TRANSACCIONAL)

    async def conectar(url, base):
        return c

    monkeypatch.setattr(motor, "_conectar", conectar)
    with caplog.at_level(logging.INFO, logger="igualab.migraciones"):
        await motor.migrar("postgresql+asyncpg://usuario_secreto:CLAVE-SECRETA@host-secreto/bd", catalogo.ESQUEMA_TRANSACCIONAL)
    texto = mensajes(caplog)
    assert any(m.startswith("Base transaccional: conexión establecida en") for m in texto)
    assert any("bloqueo de migraciones obtenido" in m for m in texto) and any("leyendo el registro" in m for m in texto)
    sin_secretos(caplog)


# ============================================ Recuperación en segundo plano ============================================

async def test_el_primer_barrido_se_registra_con_su_duracion_y_no_bloquea_el_arranque(monkeypatch, caplog):
    import contextlib

    from app.services.ingesta import coordinador
    from app.services.ingesta.coordinador import DependenciasIngesta
    from tests.ayudantes_ingesta import AlmacenEnMemoria

    llamadas = []

    async def recuperar(dependencias, **kwargs):
        llamadas.append(1)
        return "resumen"

    monkeypatch.setattr(coordinador, "ejecutar_recuperacion", recuperar)
    deps = DependenciasIngesta(sesion_nula, sesion_nula, AlmacenEnMemoria("development"), SimpleNamespace())
    gestor = GestorIngesta(deps, opciones=OpcionesGestor(recuperacion_habilitada=True, intervalo_recuperacion=0.05, retraso_primer_barrido=0.01))

    @contextlib.asynccontextmanager
    async def libre():
        yield True

    gestor._candado_de_barrido = libre
    with caplog.at_level(logging.INFO, logger="igualab.ingesta.http"):
        await gestor.iniciar()
        assert "Primer barrido de recuperación" not in " ".join(mensajes(caplog))  # iniciar() volvió sin esperarlo
        async with asyncio.timeout(5):
            while len(llamadas) < 2:
                await asyncio.sleep(0.01)
        await gestor.cerrar()
    texto = mensajes(caplog)
    assert sum(m.startswith("Arranque | Primer barrido de recuperación (en segundo plano) | inicio") for m in texto) == 1  # solo el primero
    assert any(m.startswith("Arranque | Primer barrido de recuperación (en segundo plano) | fin en") for m in texto)
    assert "Primer barrido de recuperación: resumen" in texto


async def test_con_la_recuperacion_deshabilitada_el_paso_6_lo_dice(monkeypatch, caplog):
    deps = SimpleNamespace(almacen=SimpleNamespace(ambiente="development"))
    gestor = GestorIngesta(deps, opciones=OpcionesGestor(recuperacion_habilitada=False))
    with caplog.at_level(logging.INFO, logger="igualab.ingesta.http"):
        await gestor.iniciar()
    assert "Recuperación periódica no programada (deshabilitada o ya iniciada)." in mensajes(caplog)

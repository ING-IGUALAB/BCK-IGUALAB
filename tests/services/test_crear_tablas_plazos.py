"""`crear_tablas` (paso 1/6 del arranque): logs por conexión y por tabla, plazos explícitos y fallo claro cuando se agotan.
Parte con dobles (sin base) y parte contra un PostgreSQL REAL y AISLADO (`url_pg_aislado`; se OMITE si no está disponible)."""
import asyncio
import logging
import time
import uuid

import asyncpg
import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table
from sqlalchemy.ext.asyncio import create_async_engine

from scripts import crear_tablas as modulo

SECRETOS = ("CLAVE-SECRETA", "usuario_secreto", "host-secreto")


def mensajes(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def sin_secretos(caplog) -> None:
    texto = " ".join(mensajes(caplog)) + " " + " ".join(str(r.exc_info) for r in caplog.records)
    for secreto in SECRETOS:
        assert secreto not in texto


# ============================================ Dobles ============================================

class ConexionDoble:
    def __init__(self, comportamiento=None):
        self.comportamiento = comportamiento
        self.sentencias: list[str] = []
        self.eventos: list[str] = []

    async def execute(self, sentencia):
        self.sentencias.append(str(sentencia))

    async def run_sync(self, funcion, tabla):
        self.eventos.append(f"run_sync:{tabla.name}")
        if self.comportamiento is None:
            return False
        return await self.comportamiento(tabla)

    async def commit(self):
        self.eventos.append("commit")

    async def invalidate(self):
        self.eventos.append("invalidate")

    async def close(self):
        self.eventos.append("close")


class MotorDoble:
    def __init__(self, conexion=None, conectar=None):
        self.conexion = conexion
        self._conectar = conectar

    async def _conectar_por_defecto(self):
        return self.conexion

    def connect(self):
        return (self._conectar or self._conectar_por_defecto)()


def tabla_falsa(nombre: str):
    return type("T", (), {"name": nombre, "schema": None})()


@pytest.fixture
def tres_tablas(monkeypatch):
    tablas = [tabla_falsa("usuarios"), tabla_falsa("empresas"), tabla_falsa("auditoria")]
    monkeypatch.setattr(modulo, "tablas_a_crear", lambda: tablas)
    return tablas


# ============================================ Con dobles ============================================

async def test_registra_conexion_y_cada_tabla_antes_y_despues_y_confirma(monkeypatch, caplog, tres_tablas):
    async def comportamiento(tabla):
        return tabla.name != "empresas"  # «empresas» no existía y se crea

    conexion = ConexionDoble(comportamiento)
    monkeypatch.setattr(modulo, "engine", MotorDoble(conexion))
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        nombres = await modulo.crear_tablas()

    assert nombres == ["usuarios", "empresas", "auditoria"]
    log = mensajes(caplog)
    esperado = [
        "Arranque | 1/6 › conexión a PostgreSQL | inicio",
        "Arranque | 1/6 › plazos de sesión | inicio",
        "Arranque | 1/6 › tabla 1/3 usuarios | inicio",
        "Arranque | 1/6 › tabla 1/3 usuarios | ya existía",
        "Arranque | 1/6 › tabla 2/3 empresas | inicio",
        "Arranque | 1/6 › tabla 2/3 empresas | CREADA",
        "Arranque | 1/6 › tabla 3/3 auditoria | inicio",
        "Arranque | 1/6 › confirmación de la transacción | inicio",
    ]
    posiciones = [next(i for i, m in enumerate(log) if m == e) for e in esperado]
    assert posiciones == sorted(posiciones)
    assert any(m.startswith("Arranque | 1/6 › conexión a PostgreSQL | fin en ") for m in log)
    assert any(m.startswith("Arranque | 1/6 › tabla 2/3 empresas | fin en ") for m in log)
    assert "Tablas creadas en este arranque: ['empresas']" in log
    assert conexion.sentencias == ["SET LOCAL statement_timeout = 30000", "SET LOCAL lock_timeout = 15000"]
    assert conexion.eventos == ["run_sync:usuarios", "run_sync:empresas", "run_sync:auditoria", "commit", "close"]
    sin_secretos(caplog)


async def test_un_plazo_de_conexion_agotado_falla_indicando_el_paso(monkeypatch, caplog, tres_tablas):
    async def nunca():
        await asyncio.sleep(60)

    monkeypatch.setattr(modulo, "engine", MotorDoble(conectar=nunca))
    monkeypatch.setattr(modulo, "PLAZO_CONEXION_S", 0.05)
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with pytest.raises(modulo.ErrorInicializacionTablas) as error:
            await modulo.crear_tablas()

    assert error.value.paso == "1/6 › conexión a PostgreSQL"
    assert "plazo de 0 s agotado" in error.value.motivo
    log = mensajes(caplog)
    assert "Arranque | 1/6 › conexión a PostgreSQL | inicio" in log
    assert not any("conexión a PostgreSQL | fin" in m for m in log)
    assert any("conexión a PostgreSQL | plazo de 0 s agotado" in m for m in log)
    assert not any("Tablas verificadas" in m for m in log)  # no continúa como si hubiera terminado
    assert not any("tabla 1/3" in m for m in log)


async def test_una_tabla_que_no_responde_falla_con_su_nombre_y_descarta_la_conexion(monkeypatch, caplog, tres_tablas):
    async def comportamiento(tabla):
        if tabla.name == "empresas":
            await asyncio.sleep(60)
        return True

    conexion = ConexionDoble(comportamiento)
    monkeypatch.setattr(modulo, "engine", MotorDoble(conexion))
    monkeypatch.setattr(modulo, "PLAZO_TABLA_S", 0.05)
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with pytest.raises(modulo.ErrorInicializacionTablas) as error:
            await modulo.crear_tablas()

    assert error.value.paso == "1/6 › tabla 2/3 empresas"
    log = mensajes(caplog)
    assert "Arranque | 1/6 › tabla 2/3 empresas | inicio" in log
    assert not any("tabla 2/3 empresas | fin" in m for m in log)
    assert not any("tabla 3/3" in m for m in log)
    assert "commit" not in conexion.eventos  # nada se confirma
    assert conexion.eventos[-2:] == ["invalidate", "close"]  # se descarta, no se hace rollback contra un servidor mudo
    assert not any("Tablas verificadas" in m for m in log)


async def test_el_error_de_base_de_datos_se_describe_sin_el_mensaje_original(monkeypatch, caplog, tres_tablas):
    class Original(Exception):
        sqlstate = "55P03"

    class ErrorSql(Exception):
        def __init__(self):
            super().__init__("connection to host-secreto failed: usuario_secreto:CLAVE-SECRETA")
            self.orig = Original("host-secreto CLAVE-SECRETA")

    async def comportamiento(tabla):
        raise ErrorSql()

    monkeypatch.setattr(modulo, "engine", MotorDoble(ConexionDoble(comportamiento)))
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with pytest.raises(modulo.ErrorInicializacionTablas) as error:
            await modulo.crear_tablas()

    assert "lock_timeout agotado" in error.value.motivo
    assert "SQLSTATE 55P03" in error.value.motivo
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    for secreto in SECRETOS:
        assert secreto not in str(error.value)
    sin_secretos(caplog)


async def test_un_error_de_conexion_conocido_no_filtra_host_ni_clave(monkeypatch, caplog, tres_tablas):
    async def rechazada():
        raise ConnectionRefusedError("host-secreto:5432 usuario_secreto CLAVE-SECRETA")

    monkeypatch.setattr(modulo, "engine", MotorDoble(conectar=rechazada))
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with pytest.raises(modulo.ErrorInicializacionTablas) as error:
            await modulo.crear_tablas()
    assert error.value.paso == "1/6 › conexión a PostgreSQL"
    assert "ConnectionRefusedError" in error.value.motivo
    sin_secretos(caplog)


async def test_la_cancelacion_del_arranque_se_propaga_y_descarta_la_conexion(monkeypatch, tres_tablas):
    async def comportamiento(tabla):
        await asyncio.sleep(60)

    conexion = ConexionDoble(comportamiento)
    monkeypatch.setattr(modulo, "engine", MotorDoble(conexion))
    tarea = asyncio.create_task(modulo.crear_tablas())
    await asyncio.sleep(0.05)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert conexion.eventos[-2:] == ["invalidate", "close"]


async def test_si_la_liberacion_se_cuelga_el_error_original_prevalece(monkeypatch, caplog, tres_tablas):
    class ConexionMuda(ConexionDoble):
        async def invalidate(self):
            await asyncio.sleep(60)

    async def comportamiento(tabla):
        raise OSError("host-secreto")

    monkeypatch.setattr(modulo, "engine", MotorDoble(ConexionMuda(comportamiento)))
    monkeypatch.setattr(modulo, "PLAZO_CIERRE_S", 0.05)
    with caplog.at_level(logging.INFO, logger="igualab.startup"):
        with pytest.raises(modulo.ErrorInicializacionTablas) as error:
            await modulo.crear_tablas()
    assert error.value.paso == "1/6 › tabla 1/3 usuarios"
    assert any("no se pudo liberar limpiamente" in m for m in mensajes(caplog))


def test_los_plazos_son_explicitos_y_el_del_servidor_cabe_en_el_del_cliente():
    assert 0 < modulo.PLAZO_BLOQUEO_S <= modulo.PLAZO_SENTENCIA_S < modulo.PLAZO_TABLA_S
    assert 0 < modulo.PLAZO_CONEXION_S < 60  # el valor por defecto de asyncpg
    assert modulo.TABLAS_SIN_DDL_AUTOMATICO == {"documentos", "operaciones_ingesta"}


# ============================================ Con PostgreSQL real y aislado ============================================

async def _base_nueva(url_pg_aislado: str) -> str:
    base = f"tablas_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(url_pg_aislado.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await admin.execute(f'CREATE DATABASE "{base}"')
    finally:
        await admin.close()
    return url_pg_aislado.rsplit("/", 1)[0] + f"/{base}"


def _tablas_de_prueba() -> list:
    metadatos = MetaData()
    padre = Table("padre_prueba", metadatos, Column("id", Integer, primary_key=True))
    hija = Table("hija_prueba", metadatos, Column("id", Integer, primary_key=True),
                 Column("padre_id", Integer, ForeignKey("padre_prueba.id")))
    return [padre, hija]


async def test_postgres_real_crea_las_tablas_del_modelo_y_el_segundo_arranque_las_encuentra(url_pg_aislado, monkeypatch, caplog):
    url = await _base_nueva(url_pg_aislado)
    motor = create_async_engine(url)
    monkeypatch.setattr(modulo, "engine", motor)
    try:
        with caplog.at_level(logging.INFO, logger="igualab.startup"):
            primera = await modulo.crear_tablas()
            assert any(m.startswith("Tablas creadas en este arranque:") for m in mensajes(caplog))
            caplog.clear()
            segunda = await modulo.crear_tablas()
    finally:
        await motor.dispose()
    assert primera == segunda
    assert "documentos" not in primera
    assert "operaciones_ingesta" not in primera
    log = mensajes(caplog)
    assert not any("CREADA" in m for m in log)
    assert not any(m.startswith("Tablas creadas") for m in log)
    assert sum(m.endswith("| ya existía") for m in log) == len(segunda)


async def test_postgres_real_un_bloqueo_ajeno_agota_lock_timeout_y_no_deja_nada_a_medias(url_pg_aislado, monkeypatch, caplog):
    url = await _base_nueva(url_pg_aislado)
    padre, hija = tablas = _tablas_de_prueba()
    motor = create_async_engine(url)
    async with motor.begin() as conexion:
        await conexion.run_sync(padre.create)
    # Otra sesión retiene un bloqueo exclusivo sobre el padre: crear la hija (clave foránea) debe esperarlo.
    ajena = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    transaccion = ajena.transaction()
    await transaccion.start()
    await ajena.execute("LOCK TABLE padre_prueba IN ACCESS EXCLUSIVE MODE")
    monkeypatch.setattr(modulo, "engine", motor)
    monkeypatch.setattr(modulo, "tablas_a_crear", lambda: tablas)
    monkeypatch.setattr(modulo, "PLAZO_BLOQUEO_S", 0.5)
    try:
        inicio = time.monotonic()
        with caplog.at_level(logging.INFO, logger="igualab.startup"):
            with pytest.raises(modulo.ErrorInicializacionTablas) as error:
                await modulo.crear_tablas()
        duracion = time.monotonic() - inicio
    finally:
        await transaccion.rollback()
        await ajena.close()
    assert duracion < 10, "el plazo debe cortar la espera en segundos, no minutos"
    assert error.value.paso == "1/6 › tabla 2/2 hija_prueba"
    assert "lock_timeout agotado" in error.value.motivo
    log = mensajes(caplog)
    assert "Arranque | 1/6 › tabla 1/2 padre_prueba | ya existía" in log
    assert "Arranque | 1/6 › tabla 2/2 hija_prueba | inicio" in log
    assert not any(m.startswith("Tablas verificadas") for m in log)
    async with motor.connect() as conexion:  # la hija no se creó y la conexión del arranque no quedó retenida
        assert not await conexion.run_sync(lambda c: c.dialect.has_table(c, "hija_prueba"))
    # Liberado el bloqueo, el siguiente arranque termina bien.
    monkeypatch.setattr(modulo, "PLAZO_BLOQUEO_S", 15.0)
    assert await modulo.crear_tablas() == ["padre_prueba", "hija_prueba"]
    await motor.dispose()

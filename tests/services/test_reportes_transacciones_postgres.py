"""Transacciones, aislamiento y contrato conjunto de los reportes versionados (BUG-15, entrega 3) sobre un PostgreSQL REAL, AISLADO y
TEMPORAL. Comprueba que la lectura coherente (`REPEATABLE READ`) no se filtra a otras operaciones ni a conexiones reutilizadas
(el motor de pruebas usa UNA sola conexión, de modo que la siguiente operación reutiliza la misma), que el parámetro `liberar` del
servicio de análisis GRI conserva su comportamiento y que la auditoría de descarga se confirma o se deshace con la lectura.
Sin PostgreSQL se OMITEN (`pytest -rs`)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.exceptions import ConflictError, NotFoundError
from app.models import TipoDocumento
from app.modules.analisis_gri.application import servicio
from app.modules.reportes import service as servicio_reportes
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    cliente_http,
    comprobar_error,
    crear_administrador,
    crear_empresa,
    sembrar_documento,
)
from tests.services.test_reportes_postgres import construir_app
from tests.services.test_reportes_versionado_postgres import (
    AMBIENTE,
    OK,
    RUTA,
    SUB,
    CompiladorRegistro,
    asignar_estados,
    emitir,
    escenario_v1,
    escenario_validacion_2,
    eventos,
    get,
    params,
    reportes,
    revisar_y_validar,
)

ESPERA = 15
LECTURA_COMMITTED = "read committed"
LECTURA_REPETIBLE = "repeatable read"


@pytest_asyncio.fixture
async def e1(fabrica_pg, url_pg_aislado):
    """Entorno con UNA sola conexión en el pool (`fabrica_pg` solo deja las tablas vacías): lo que una operación deje en la conexión
    lo hereda la siguiente. Las operaciones de cada prueba son secuenciales salvo donde se indica."""
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    motor = create_async_engine(url_pg_aislado, pool_size=1, max_overflow=0, pool_timeout=ESPERA)
    fabrica = async_sessionmaker(motor, expire_on_commit=False)
    async with fabrica() as db:
        admin = await crear_administrador(db, "Ana Admin")
        empresa = await crear_empresa(db, "Minera Andina")
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=2025)
    entorno = SimpleNamespace(
        fabrica=fabrica, admin=admin, empresa=empresa, otra=empresa, compilador=CompiladorRegistro(), url=url_pg_aislado
    )
    yield entorno
    await motor.dispose()


# ------------------------------------------------------------------ Ayudantes ------------------------------------------------------------------

async def estado_de_la_conexion(e) -> tuple[str, str, int]:
    """(aislamiento, solo lectura, pid) que ve la PRÓXIMA operación sobre la conexión del pool."""
    async with e.fabrica() as db:
        fila = (await db.execute(text(
            "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only'), pg_backend_pid()"
        ))).one()
    return fila[0], fila[1], fila[2]


async def transacciones_abiertas_ociosas(e) -> int:
    """Sesiones de la base en `idle in transaction` (no cuenta la de esta consulta, que usa una conexión aparte)."""
    conexion = await asyncpg.connect(e.url.replace("+asyncpg", "", 1))
    try:
        return await conexion.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND state LIKE 'idle in transaction%'"
        )
    finally:
        await conexion.close()


async def comprobar_conexion_limpia(e, pid_inicial: int) -> None:
    aislamiento, solo_lectura, pid = await estado_de_la_conexion(e)
    assert aislamiento == LECTURA_COMMITTED
    assert solo_lectura == "off"
    assert pid == pid_inicial  # es la MISMA conexión reutilizada
    assert await transacciones_abiertas_ociosas(e) == 0


async def consultar_vigente(e):
    async with e.fabrica() as db:
        return await servicio_reportes.obtener_reporte_vigente(db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025)


async def descargar_vigente_servicio(e):
    async with e.fabrica() as db:
        return await servicio_reportes.obtener_pdf_vigente(
            db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025, usuario_id=e.admin.id
        )


class _Pausa:
    def __init__(self):
        self.llegada, self.salida = asyncio.Event(), asyncio.Event()

    async def detener(self):
        self.llegada.set()
        await self.salida.wait()


# ============================================ A · Aislamiento REPEATABLE READ ============================================

async def test_la_lectura_coherente_es_repetible_y_no_es_de_solo_lectura_para_poder_auditar(e1, monkeypatch):
    await escenario_v1(e1)
    visto = {}
    original = servicio_reportes._contextos

    async def observar(db, *args, **kwargs):
        fila = (await db.execute(text(
            "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')"
        ))).one()
        visto["aislamiento"], visto["solo_lectura"] = fila[0], fila[1]
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(servicio_reportes, "_contextos", observar)
    await descargar_vigente_servicio(e1)
    assert visto["aislamiento"] == LECTURA_REPETIBLE  # fijado antes de la primera consulta de la transacción
    assert visto["solo_lectura"] == "off"  # la auditoría de la descarga se inserta en esa misma transacción


async def test_el_aislamiento_no_se_filtra_a_la_conexion_reutilizada_en_ningun_desenlace(e1, monkeypatch):
    await escenario_v1(e1)
    _, _, pid = await estado_de_la_conexion(e1)
    await comprobar_conexion_limpia(e1, pid)

    await consultar_vigente(e1)  # éxito de la consulta
    await comprobar_conexion_limpia(e1, pid)
    async with e1.fabrica() as db:  # disponibilidad
        await servicio_reportes.disponibilidad_de_reportes(db, ambiente=AMBIENTE, empresa_id=e1.empresa.id, anio=2025)
    await comprobar_conexion_limpia(e1, pid)
    await descargar_vigente_servicio(e1)  # descarga exitosa (confirma la auditoría)
    await comprobar_conexion_limpia(e1, pid)

    await asignar_estados(e1, [OK, OK])  # descarga rechazada por cambios sin validar
    with pytest.raises(ConflictError):
        await descargar_vigente_servicio(e1)
    await comprobar_conexion_limpia(e1, pid)

    async with e1.fabrica() as db:  # sin reportes: REPORT_NOT_FOUND
        with pytest.raises(NotFoundError):
            await servicio_reportes.obtener_reporte_vigente(db, ambiente=AMBIENTE, empresa_id=e1.empresa.id, anio=2019)
    await comprobar_conexion_limpia(e1, pid)

    async def falla(*args, **kwargs):
        raise RuntimeError("fallo simulado de la auditoría")

    await revisar_y_validar(e1, [OK, OK])  # nueva validación …
    assert (await emitir(e1)).status_code == 201  # … emitida: otra vez hay un reporte demostrablemente actualizado
    with monkeypatch.context() as m:
        m.setattr(servicio_reportes, "_registrar_descarga", falla)
        with pytest.raises(RuntimeError):
            await descargar_vigente_servicio(e1)
    await comprobar_conexion_limpia(e1, pid)


async def test_la_cancelacion_de_una_lectura_coherente_libera_la_conexion_y_restablece_el_aislamiento(e1, monkeypatch):
    await escenario_v1(e1)
    _, _, pid = await estado_de_la_conexion(e1)
    pausa = _Pausa()
    original = servicio_reportes._contextos

    async def detener_y_seguir(*args, **kwargs):
        await pausa.detener()
        return await original(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(servicio_reportes, "_contextos", detener_y_seguir)
        descarga = asyncio.create_task(descargar_vigente_servicio(e1))
        await asyncio.wait_for(pausa.llegada.wait(), ESPERA)
        descarga.cancel()
        with pytest.raises(asyncio.CancelledError):
            await descarga
    await comprobar_conexion_limpia(e1, pid)
    assert await eventos(e1, "DESCARGA") == []  # la descarga cancelada no dejó auditoría


async def test_un_fallo_de_auditoria_no_se_oculta_ni_produce_exito_falso(e1, monkeypatch):
    await escenario_v1(e1)

    def falla(*args, **kwargs):
        raise RuntimeError("fallo simulado de la auditoría")

    with monkeypatch.context() as m:
        m.setattr(servicio_reportes, "registrar_evento", falla)
        async with cliente_http(construir_app(e1, e1.admin)) as c:
            respuesta = await c.get(f"{RUTA}/vigente/archivo", params=params(e1.empresa))
    assert respuesta.status_code == 500  # nunca un 200 con el archivo si no se pudo auditar
    assert respuesta.headers.get("content-type") != "application/pdf"
    assert await eventos(e1, "DESCARGA") == []


async def test_la_descarga_exitosa_audita_en_la_misma_transaccion_y_un_rechazo_no_registra_exito(e1):
    v1 = await escenario_v1(e1)
    assert (await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa))).status_code == 200
    (evento,) = await eventos(e1, "DESCARGA")
    assert evento[0] == e1.admin.id
    assert evento[1] == "EXITOSO"
    assert "Descarga de reporte" in evento[2]
    assert str(v1["id"]) not in evento[2]  # sin UUID crudo
    await escenario_validacion_2(e1)
    comprobar_error(await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa)), 409, "REPORT_UPDATE_PENDING")
    assert len(await eventos(e1, "DESCARGA")) == 1


# ============================================ B · Parámetro `liberar` del servicio de análisis GRI ============================================

async def test_por_defecto_el_resumen_por_anio_y_las_ultimas_validaciones_terminan_su_transaccion(e1):
    await revisar_y_validar(e1, [OK, SUB])
    async with e1.fabrica() as db:
        await servicio.resumen_por_anio(db, ambiente=AMBIENTE, empresa_id=e1.empresa.id)
        assert not db.in_transaction()
    async with e1.fabrica() as db:
        await servicio.ultimas_validaciones(db, ambiente=AMBIENTE, empresa_id=e1.empresa.id, anios={2025})
        assert not db.in_transaction()
    assert await transacciones_abiertas_ociosas(e1) == 0


async def test_con_liberar_falso_la_transaccion_sigue_abierta_hasta_que_el_llamador_la_termina(e1):
    await revisar_y_validar(e1, [OK, SUB])
    async with e1.fabrica() as db:
        resumen = await servicio.resumen_por_anio(db, ambiente=AMBIENTE, empresa_id=e1.empresa.id, liberar=False)
        assert db.in_transaction()
        ultimas = await servicio.ultimas_validaciones(db, ambiente=AMBIENTE, empresa_id=e1.empresa.id, anios={2025}, liberar=False)
        assert db.in_transaction()
        await db.rollback()
    assert resumen[2025].documentos == 1
    assert ultimas[2025].numero == 1
    assert await transacciones_abiertas_ociosas(e1) == 0


async def test_las_lecturas_encadenadas_conservan_la_instantanea_hasta_el_final(e1):
    await escenario_v1(e1)
    async with e1.fabrica() as db:
        await servicio_reportes._leer_origen(db, AMBIENTE, e1.empresa.id, 2025)
        assert db.in_transaction()  # ninguna lectura interna terminó la transacción de forma prematura
        aislamiento = (await db.execute(text("SELECT current_setting('transaction_isolation')"))).scalar_one()
        assert aislamiento == LECTURA_REPETIBLE
        await db.rollback()
    assert await transacciones_abiertas_ociosas(e1) == 0


# ============================================ D · Vista previa y reutilización ============================================

async def test_la_vista_previa_existente_no_mezcla_validaciones_si_aparece_otra_durante_la_consulta(e1, monkeypatch):
    await escenario_v1(e1)
    pdf1 = (await reportes(e1))[0][6]
    pausa = _Pausa()
    original = servicio_reportes._pdf_almacenado

    async def detener_y_seguir(*args, **kwargs):
        await pausa.detener()  # la validación vigente (#1) y su emisión ya se determinaron
        return await original(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(servicio_reportes, "_pdf_almacenado", detener_y_seguir)
        previa = asyncio.create_task(_vista_previa_servicio(e1))
        await asyncio.wait_for(pausa.llegada.wait(), ESPERA)
        await escenario_validacion_2(e1)  # llega la validación #2 y se emite la versión 2 mientras tanto
        assert (await emitir(e1)).status_code == 201
        pausa.salida.set()
        devuelto = await asyncio.wait_for(previa, ESPERA)
    assert devuelto == pdf1  # los bytes de la emisión de la validación que se evaluó, no los de la versión 2
    assert len(await reportes(e1)) == 2
    assert e1.compilador.llamadas == 2  # solo las dos emisiones compilaron; la vista previa no


async def _vista_previa_servicio(e):
    async with e.fabrica() as db:
        return await servicio_reportes.vista_previa(
            db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025, compilar_pdf=e.compilador
        )


async def test_la_vista_previa_candidata_no_deja_version_reservada_y_la_emision_usa_su_propia_fecha(e1):
    await revisar_y_validar(e1, [OK, SUB])
    await _vista_previa_servicio(e1)
    assert await reportes(e1) == []
    emitida = await emitir(e1)
    assert emitida.status_code == 201
    assert emitida.json()["version"] == 1
    (fila,) = await reportes(e1)
    assert fila[9].isoformat()[:10] in e1.compilador.htmls[-1]  # la fecha de la emisión es la de la emisión, no la de la vista previa


# ============================================ Contrato conjunto ============================================

async def test_ciclo_completo_del_contrato_http(e1):
    anio = {"empresa_id": str(e1.empresa.id), "anio": 2025}
    comprobar_error(await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa)), 404, "REPORT_NOT_FOUND")
    await revisar_y_validar(e1, [OK, SUB])
    primera = await emitir(e1)
    repetida = await emitir(e1)
    assert (primera.status_code, repetida.status_code) == (201, 200)
    assert repetida.json()["id"] == primera.json()["id"]
    pdf1 = (await reportes(e1))[0][6]
    async with cliente_http(construir_app(e1, e1.admin)) as c:
        assert (await c.post(f"{RUTA}/vista-previa", json=anio)).content == pdf1  # emisión existente: sus bytes
    assert (await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa))).content == pdf1

    await escenario_validacion_2(e1)
    pendiente = comprobar_error(await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa)), 409, "REPORT_UPDATE_PENDING")
    assert pendiente["details"]["accion_sugerida"] == "EMITIR"
    segunda = await emitir(e1)
    assert segunda.status_code == 201
    assert segunda.json()["version"] == 2
    pdf2 = (await reportes(e1))[1][6]
    assert (await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa))).content == pdf2
    assert (await get(e1, f"{RUTA}/{primera.json()['id']}/archivo")).content == pdf1  # la histórica conserva sus bytes
    historial = (await get(e1, RUTA)).json()
    assert [(x["version"], x["es_ultima_version"]) for x in historial] == [(2, True), (1, False)]


async def test_cuando_coinciden_varias_condiciones_la_prioridad_y_la_accion_son_las_documentadas(e1):
    await escenario_v1(e1)
    await escenario_validacion_2(e1)
    # Validación posterior sin emitir + filas nuevas por revisar: gana la actualización pendiente, y primero hay que REVISAR.
    async with e1.fabrica() as db:
        await sembrar_documento(db, e1.empresa, e1.admin, texto="# Otro\n\nGRI 302-1.\n", anio=2025, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    error = comprobar_error(await get(e1, f"{RUTA}/vigente/archivo", **params(e1.empresa)), 409, "REPORT_UPDATE_PENDING")
    assert error["details"]["accion_sugerida"] == "REVISAR"  # no VALIDAR: faltan estados por revisar
    assert error["details"]["puede_generar"] is False  # ni EMITIR: no hay validación vigente
    assert error["details"]["motivo_no_generar"] == "PENDIENTE_REVISION"

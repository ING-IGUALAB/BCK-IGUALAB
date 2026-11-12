"""Coordinación transaccional entre ingesta, validación GRI, desactivación de empresas y emisión de reportes (BUG-15, entrega 1)
sobre un PostgreSQL REAL, AISLADO y TEMPORAL, con sesiones independientes y puntos de pausa deterministas.

El documento pasa a ser elegible por el flujo REAL de ingesta (`publicar_en_base_vectorial`, que confirma `vector_publicado_en`
bajo la protección común del origen); solo la base vectorial es un doble (no hay pgvector ni proveedores externos aquí). Las
pausas se colocan DESPUÉS de adquirir los bloqueos y antes del commit, de modo que se observa lo que ocurre con otra operación
mientras la primera los retiene: queda esperando (se comprueba en `pg_stat_activity`) y, al soltarse, ve el resultado confirmado.
Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.exceptions import ConflictError, NotFoundError
from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.application import servicio
from app.modules.analisis_gri.application.coordinacion import proteger_origen_de_analisis
from app.modules.empresas import service as servicio_empresas
from app.modules.ingesta.application.documento_service import publicacion
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores
from app.modules.reportes import service as servicio_reportes
from app.modules.reportes.service import generar_reporte
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    MD_GRI_204_Y_302,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    crear_administrador,
    crear_empresa,
    sembrar_documento,
)
from tests.services.test_reportes_versionado_postgres import CompiladorRegistro, asignar_estados, validar

AMBIENTE = "development"
OK, SUB = EstadoGri.OK, EstadoGri.SUB_REPORTADO
GRI = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
FRAGMENTOS = 2
ESPERA = 15


# ------------------------------------------------------------------ Ayudantes ------------------------------------------------------------------

class Pausa:
    """Punto de sincronización: la tarea llega, avisa y espera a que el test la libere. Si el test falla antes de liberarla, el
    fixture la libera y cancela las tareas pendientes para que ninguna conexión quede retenida y el clúster pueda cerrarse."""

    def __init__(self, e):
        self.llegada, self.salida = asyncio.Event(), asyncio.Event()
        e.pausas.append(self)

    async def detener(self) -> None:
        self.llegada.set()
        await self.salida.wait()

    async def esperar_llegada(self) -> None:
        await asyncio.wait_for(self.llegada.wait(), ESPERA)

    def liberar(self) -> None:
        self.salida.set()


def tarea(e, corrutina) -> asyncio.Task:
    nueva = asyncio.create_task(corrutina)
    e.tareas.append(nueva)
    return nueva


def pausar(monkeypatch, modulo, nombre: str, pausa: Pausa, *, cuando=lambda *a, **k: True, despues: bool = False) -> None:
    original = getattr(modulo, nombre)

    async def envoltorio(*args, **kwargs):
        if not cuando(*args, **kwargs):
            return await original(*args, **kwargs)
        if despues:
            resultado = await original(*args, **kwargs)
            await pausa.detener()
            return resultado
        await pausa.detener()
        return await original(*args, **kwargs)

    monkeypatch.setattr(modulo, nombre, envoltorio)


def confirma_publicacion(_db, _documento_id, _condiciones, valores) -> bool:
    return valores.get("vector_publicado_en") is not None


class VectorialDoble:
    """Sesión de la base vectorial: sin transacción abierta; las operaciones reales están sustituidas en el fixture."""

    def in_transaction(self) -> bool:
        return False


@pytest_asyncio.fixture
async def e(fabrica_pg, url_pg_aislado, monkeypatch):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)

    async def publicar_fragmentos(db, *, ambiente, documento_id):
        return FRAGMENTOS

    async def contar_fragmentos(db, *, ambiente, documento_id):
        return SimpleNamespace(total=FRAGMENTOS, publicados=FRAGMENTOS)

    monkeypatch.setattr(vectores, "publicar_fragmentos", publicar_fragmentos)  # proveedor externo: doble
    monkeypatch.setattr(vectores, "contar_fragmentos", contar_fragmentos)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        a = await crear_empresa(db, "Empresa A")
        b = await crear_empresa(db, "Empresa B")
        for empresa, anio in ((a, 2025), (a, 2024), (b, 2025)):
            await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=anio)
    entorno = SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, empresa=a, otra=b, compilador=CompiladorRegistro(), pausas=[], tareas=[]
    )
    for empresa, anio in ((a, 2025), (a, 2024), (b, 2025)):
        await asignar_estados(entorno, [OK, SUB], empresa, anio)
        await validar(entorno, empresa, anio)
    yield entorno
    for pausa in entorno.pausas:
        pausa.liberar()
    for pendiente in entorno.tareas:
        if not pendiente.done():
            pendiente.cancel()
    await asyncio.gather(*entorno.tareas, return_exceptions=True)


async def documento_pendiente(e, empresa=None, anio: int = 2025, texto: str = MD_GRI_204_Y_302):
    """Documento COMPLETADO cuya publicación vectorial quedó pendiente (aún NO es elegible)."""
    async with e.fabrica() as db:
        documento = await sembrar_documento(db, empresa or e.empresa, e.admin, texto=texto, anio=anio, tipo=GRI, publicado=False)
        await db.execute(text("UPDATE documentos SET fragmentos_total = :n WHERE id = :i"), {"n": FRAGMENTOS, "i": documento.id})
        await db.commit()
    return documento


async def publicar(e, documento) -> bool:
    async with e.fabrica() as db:
        return await publicacion.publicar_en_base_vectorial(db, VectorialDoble(), documento.id)


async def emitir(e, empresa=None, anio: int = 2025):
    async with e.fabrica() as db:
        return await generar_reporte(
            db, ambiente=AMBIENTE, empresa_id=(empresa or e.empresa).id, anio=anio, usuario_id=e.admin.id, compilar_pdf=e.compilador
        )


async def analisis(e, empresa=None, anio: int = 2025):
    async with e.fabrica() as db:
        return await servicio.consultar_analisis(db, ambiente=AMBIENTE, empresa_id=(empresa or e.empresa).id, anio=anio)


async def sql(e, consulta: str, **parametros):
    async with e.fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


async def reportes(e):
    return [tuple(f) for f in await sql(e, "SELECT empresa_id, anio, version, validacion_id FROM reportes_prospeccion ORDER BY anio, version")]


async def publicado_en(e, documento):
    return (await sql(e, "SELECT vector_publicado_en FROM documentos WHERE id = :i", i=documento.id))[0][0]


async def esperando_un_bloqueo(e) -> None:
    """Hay al menos una sesión de la base esperando un bloqueo (evidencia de que la operación NO avanzó por casualidad)."""
    for _ in range(200):
        n = (await sql(e, "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"))[0][0]
        if n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("ninguna sesión quedó esperando un bloqueo")


async def sigue_pendiente(tarea: asyncio.Task) -> bool:
    try:
        await asyncio.wait_for(asyncio.shield(tarea), 0.5)
    except asyncio.TimeoutError:
        return True
    return False


async def lock_libre(e, empresa=None, anio: int = 2025) -> None:
    """Otra sesión adquiere la protección común sin esperar: demuestra que nadie la retiene."""
    async with e.fabrica() as db:
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        await proteger_origen_de_analisis(db, ambiente=AMBIENTE, empresa_id=(empresa or e.empresa).id, anio=anio)
        await db.commit()


# ============================================ La protección común ============================================

async def test_la_proteccion_devuelve_la_version_de_estados_y_crea_la_fila_de_control_una_sola_vez(e):
    async with e.fabrica() as db:
        version = await proteger_origen_de_analisis(db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025)
        await db.commit()
    assert version == (await analisis(e)).version
    async with e.fabrica() as db:
        await proteger_origen_de_analisis(db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025)
        await db.commit()
    assert (await sql(e, "SELECT count(*) FROM analisis_gri_control WHERE empresa_id = :emp AND anio = 2025", emp=e.empresa.id))[0][0] == 1


async def test_la_proteccion_de_una_empresa_inexistente_es_un_error_de_negocio(e):
    async with e.fabrica() as db:
        with pytest.raises(NotFoundError) as capturado:
            await proteger_origen_de_analisis(db, ambiente=AMBIENTE, empresa_id=e.admin.id, anio=2025)
    assert capturado.value.code == "COMPANY_NOT_FOUND"


async def test_la_proteccion_no_confirma_ni_deshace_por_su_cuenta(e):
    async with e.fabrica() as db:
        await proteger_origen_de_analisis(db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2030)
        assert db.in_transaction()  # sigue abierta: quien llama decide
    assert (await sql(e, "SELECT count(*) FROM analisis_gri_control WHERE anio = 2030"))[0][0] == 0  # al cerrar sin commit se deshizo


# ============================================ 1 · El documento se vuelve elegible antes de confirmar el reporte ============================================

async def test_una_publicacion_confirmada_antes_del_commit_final_impide_publicar_una_version_obsoleta(e, monkeypatch):
    nuevo = await documento_pendiente(e)
    original = servicio_reportes._preparar

    async def preparar_y_publicar(*args, **kwargs):
        preparacion = await original(*args, **kwargs)  # el snapshot y el PDF candidato ya existen
        assert await publicar(e, nuevo) is True  # el documento se vuelve elegible "durante" el renderizado
        return preparacion

    monkeypatch.setattr(servicio_reportes, "_preparar", preparar_y_publicar)
    with pytest.raises(ConflictError) as capturado:
        await emitir(e)
    assert capturado.value.code == "REPORT_SOURCE_CHANGED"
    assert await reportes(e) == []
    assert await sql(e, "SELECT 1 FROM auditoria WHERE tipo_evento = 'GENERACION_REPORTE'") == []


async def test_una_publicacion_en_curso_hace_esperar_a_la_emision_y_esta_detecta_el_cambio(e, monkeypatch):
    await emitir(e)  # versión 1 de la validación #1
    await asignar_estados(e, [OK, OK])
    await validar(e)  # validación #2 vigente
    nuevo = await documento_pendiente(e)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, publicacion, "_transicion", pausa, cuando=confirma_publicacion)
        publicando = tarea(e, publicar(e, nuevo))
        await pausa.esperar_llegada()  # la publicación retiene la protección y aún no confirmó
        emitiendo = tarea(e, emitir(e))
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(emitiendo)  # la emisión espera su comprobación final
        pausa.liberar()
        assert await asyncio.wait_for(publicando, ESPERA) is True
        with pytest.raises(ConflictError) as capturado:
            await asyncio.wait_for(emitiendo, ESPERA)
    assert capturado.value.code == "REPORT_SOURCE_CHANGED"
    assert [f[2] for f in await reportes(e)] == [1]  # solo la versión 1: la 2 no se publicó con el PDF candidato


# ============================================ 2 · El reporte se confirma primero ============================================

async def test_si_el_reporte_se_confirma_primero_se_conserva_y_la_incorporacion_posterior_cambia_la_vigencia(e, monkeypatch):
    nuevo = await documento_pendiente(e)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, servicio_reportes, "_tomar_candado_de_numeracion", pausa)
        emitiendo = tarea(e, emitir(e))
        await pausa.esperar_llegada()  # la emisión retiene la protección con su comprobación final ya hecha
        publicando = tarea(e, publicar(e, nuevo))
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(publicando)  # el documento no puede volverse elegible a mitad de la emisión
        assert await publicado_en(e, nuevo) is None
        pausa.liberar()
        emitido = await asyncio.wait_for(emitiendo, ESPERA)
        assert await asyncio.wait_for(publicando, ESPERA) is True
    assert emitido.version == 1
    assert emitido.reutilizado is False
    assert [f[2] for f in await reportes(e)] == [1]  # el reporte confirmado se conserva
    actual = await analisis(e)
    assert actual.validacion.vigente is False  # y la incorporación posterior invalida la validación que lo respaldaba
    assert [m.value for m in actual.validacion.motivos_no_vigente] == ["DOCUMENTOS_CAMBIARON"]
    assert await publicado_en(e, nuevo) is not None


# ============================================ 3 · Publicación de documento y validación GRI compiten ============================================

async def test_una_publicacion_en_curso_hace_esperar_a_la_validacion_que_ya_no_coincide(e, monkeypatch):
    await asignar_estados(e, [OK, OK], e.empresa, 2024)  # invalida la validación vigente de 2024 para volver a validar
    previo = await analisis(e, e.empresa, 2024)
    nuevo = await documento_pendiente(e, e.empresa, 2024)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, publicacion, "_transicion", pausa, cuando=confirma_publicacion)
        publicando = tarea(e, publicar(e, nuevo))
        await pausa.esperar_llegada()

        async def validando():
            async with e.fabrica() as db:
                return await servicio.validar_analisis(
                    db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2024, version_esperada=previo.version,
                    huella_esperada=previo.huella_documentos, actor=e.admin,
                )

        validacion = tarea(e, validando())
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(validacion)
        pausa.liberar()
        assert await asyncio.wait_for(publicando, ESPERA) is True
        with pytest.raises(ConflictError) as capturado:
            await asyncio.wait_for(validacion, ESPERA)
    assert capturado.value.code == "ANALYSIS_VERSION_CONFLICT"
    assert capturado.value.details["documentos_cambiaron"] is True
    assert (await sql(e, "SELECT count(*) FROM analisis_gri_validaciones WHERE empresa_id = :emp AND anio = 2024", emp=e.empresa.id))[0][0] == 1


async def test_una_validacion_en_curso_hace_esperar_a_la_publicacion_y_queda_no_vigente(e, monkeypatch):
    await asignar_estados(e, [OK, OK], e.empresa, 2024)
    previo = await analisis(e, e.empresa, 2024)
    nuevo = await documento_pendiente(e, e.empresa, 2024)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, servicio, "_leer", pausa, despues=True, cuando=lambda *a, **k: k.get("consistente") is False)
        async def validando():
            async with e.fabrica() as db:
                return await servicio.validar_analisis(
                    db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2024, version_esperada=previo.version,
                    huella_esperada=previo.huella_documentos, actor=e.admin,
                )

        validacion = tarea(e, validando())
        await pausa.esperar_llegada()  # la validación ya leyó el origen y retiene la protección
        publicando = tarea(e, publicar(e, nuevo))
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(publicando)
        pausa.liberar()
        await asyncio.wait_for(validacion, ESPERA)
        assert await asyncio.wait_for(publicando, ESPERA) is True
    actual = await analisis(e, e.empresa, 2024)
    assert actual.validacion.numero == 2  # la validación se guardó con el origen que leyó
    assert actual.validacion.vigente is False  # y la publicación posterior la deja no vigente (huella distinta)


# ============================================ 4 · Desactivación de empresa y emisión/publicación ============================================

async def desactivar(e, empresa=None):
    async with e.fabrica() as db:
        return await servicio_empresas.cambiar_estado_empresa(db, (empresa or e.empresa).id, False)


async def test_una_emision_en_curso_hace_esperar_a_la_desactivacion(e, monkeypatch):
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, servicio_reportes, "_tomar_candado_de_numeracion", pausa)
        emitiendo = tarea(e, emitir(e))
        await pausa.esperar_llegada()
        desactivando = tarea(e, desactivar(e))
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(desactivando)
        pausa.liberar()
        emitido = await asyncio.wait_for(emitiendo, ESPERA)
        empresa = await asyncio.wait_for(desactivando, ESPERA)
    assert emitido.version == 1
    assert empresa.activa is False
    assert [f[2] for f in await reportes(e)] == [1]  # la emisión se completó antes de la desactivación


async def test_una_desactivacion_en_curso_hace_esperar_a_la_emision_que_no_publica_nada(e, monkeypatch):
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, servicio_empresas, "_guardar", pausa)
        desactivando = tarea(e, desactivar(e))
        await pausa.esperar_llegada()  # retiene el bloqueo exclusivo de la empresa, aún sin confirmar
        emitiendo = tarea(e, emitir(e))
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(emitiendo)
        pausa.liberar()
        await asyncio.wait_for(desactivando, ESPERA)
        with pytest.raises(ConflictError) as capturado:
            await asyncio.wait_for(emitiendo, ESPERA)
    assert capturado.value.code == "REPORT_SOURCE_CHANGED"
    assert await reportes(e) == []


async def test_la_desactivacion_afecta_a_todos_los_anios_y_espera_a_una_publicacion_de_cualquiera(e, monkeypatch):
    nuevo = await documento_pendiente(e, e.empresa, 2024)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, publicacion, "_transicion", pausa, cuando=confirma_publicacion)
        publicando = tarea(e, publicar(e, nuevo))
        await pausa.esperar_llegada()
        desactivando = tarea(e, desactivar(e))  # empresa completa, no solo el año 2024
        await esperando_un_bloqueo(e)
        assert await sigue_pendiente(desactivando)
        pausa.liberar()
        assert await asyncio.wait_for(publicando, ESPERA) is True
        assert (await asyncio.wait_for(desactivando, ESPERA)).activa is False
    assert (await analisis(e, e.empresa, 2024)).documentos == []  # ya ningún año de la empresa es elegible
    assert (await analisis(e, e.empresa, 2025)).documentos == []


# ============================================ 5 · Solicitudes simultáneas para la misma validación ============================================

async def test_solicitudes_simultaneas_para_la_misma_validacion_emiten_una_sola_vez_y_las_demas_reutilizan(e, monkeypatch):
    barrera = asyncio.Barrier(3)
    original = servicio_reportes._publicar

    async def con_barrera(*args, **kwargs):
        await barrera.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(servicio_reportes, "_publicar", con_barrera)
    resultados = await asyncio.gather(emitir(e), emitir(e), emitir(e))
    assert sorted(r.reutilizado for r in resultados) == [False, True, True]
    assert len({r.id for r in resultados}) == 1
    assert [f[2] for f in await reportes(e)] == [1]


# ============================================ 6 · Otra empresa o año progresa de forma independiente ============================================

async def test_otra_empresa_y_otro_anio_progresan_mientras_una_emision_retiene_su_proteccion(e, monkeypatch):
    nuevo_2024 = await documento_pendiente(e, e.empresa, 2024)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, servicio_reportes, "_tomar_candado_de_numeracion", pausa, cuando=lambda db, empresa_id, anio: anio == 2025 and empresa_id == e.empresa.id)
        retenida = tarea(e, emitir(e))  # A/2025 retiene sus bloqueos
        await pausa.esperar_llegada()
        otra_empresa = await asyncio.wait_for(emitir(e, e.otra, 2025), ESPERA)
        assert otra_empresa.version == 1
        assert await asyncio.wait_for(publicar(e, nuevo_2024), ESPERA) is True  # otro año de la MISMA empresa
        emision_2024 = emitir(e, e.empresa, 2024)
        with pytest.raises(ConflictError) as capturado:  # sin esperar: ese año cambió su origen y su validación ya no es vigente
            await asyncio.wait_for(emision_2024, ESPERA)
        assert capturado.value.code == "ANALYSIS_NOT_VALIDATED"
        assert await sigue_pendiente(retenida)
        pausa.liberar()
        await asyncio.wait_for(retenida, ESPERA)
    assert {(f[0], f[1]) for f in await reportes(e)} >= {(e.otra.id, 2025), (e.empresa.id, 2025)}


# ============================================ 7 · Error, timeout o cancelación liberan la transacción y sus bloqueos ============================================

async def test_un_error_en_la_emision_tras_tomar_los_bloqueos_los_libera_y_no_deja_nada(e, monkeypatch):
    def falla(*args, **kwargs):
        raise RuntimeError("fallo simulado tras tomar los bloqueos")

    with monkeypatch.context() as m:
        m.setattr(servicio_reportes, "registrar_evento", falla)
        with pytest.raises(RuntimeError):
            await emitir(e)
    await asyncio.wait_for(lock_libre(e), ESPERA)
    assert await reportes(e) == []
    assert (await emitir(e)).version == 1  # y la solicitud siguiente funciona


async def test_un_timeout_de_la_emision_libera_los_bloqueos(e, monkeypatch):
    pausa = Pausa(e)  # nunca se libera: la emisión queda retenida con sus bloqueos hasta que vence el plazo
    with monkeypatch.context() as m:
        pausar(m, servicio_reportes, "_tomar_candado_de_numeracion", pausa)
        emision = emitir(e)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(emision, 1.5)
    await asyncio.wait_for(lock_libre(e), ESPERA)
    assert await reportes(e) == []


async def test_la_cancelacion_de_la_emision_libera_los_bloqueos(e, monkeypatch):
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, servicio_reportes, "_tomar_candado_de_numeracion", pausa)
        emitiendo = tarea(e, emitir(e))
        await pausa.esperar_llegada()
        emitiendo.cancel()
        with pytest.raises(asyncio.CancelledError):
            await emitiendo
    await asyncio.wait_for(lock_libre(e), ESPERA)
    assert await reportes(e) == []


async def test_un_error_al_confirmar_la_publicacion_libera_los_bloqueos_y_el_reintento_la_completa(e, monkeypatch):
    nuevo = await documento_pendiente(e)

    async def falla(*args, **kwargs):
        raise RuntimeError("fallo simulado al confirmar")

    with monkeypatch.context() as m:
        m.setattr(publicacion, "_transicion", falla)
        with pytest.raises(RuntimeError):
            await publicar(e, nuevo)
    await asyncio.wait_for(lock_libre(e), ESPERA)
    assert await publicado_en(e, nuevo) is None  # quedó pendiente y recuperable
    assert await publicar(e, nuevo) is True  # el reintento la confirma
    assert await publicado_en(e, nuevo) is not None


async def test_la_cancelacion_de_la_publicacion_libera_los_bloqueos_y_no_confirma_nada(e, monkeypatch):
    nuevo = await documento_pendiente(e)
    pausa = Pausa(e)
    with monkeypatch.context() as m:
        pausar(m, publicacion, "_transicion", pausa, cuando=confirma_publicacion)
        publicando = tarea(e, publicar(e, nuevo))
        await pausa.esperar_llegada()
        publicando.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publicando
    await asyncio.wait_for(lock_libre(e), ESPERA)
    assert await publicado_en(e, nuevo) is None
    assert await publicar(e, nuevo) is True


# ============================================ 8 · Repetir una publicación conserva su idempotencia ============================================

async def eventos_de_publicacion(e, documento) -> int:
    return (await sql(e, "SELECT count(*) FROM auditoria WHERE detalle LIKE :d", d="Publicación vectorial confirmada%"))[0][0]


async def test_repetir_la_publicacion_devuelve_false_sin_cambiar_la_fecha_ni_duplicar_la_auditoria(e):
    nuevo = await documento_pendiente(e)
    assert await publicar(e, nuevo) is True
    primera = await publicado_en(e, nuevo)
    assert await publicar(e, nuevo) is False
    assert await publicar(e, nuevo) is False
    assert await publicado_en(e, nuevo) == primera
    assert await eventos_de_publicacion(e, nuevo) == 1


async def test_dos_publicaciones_simultaneas_del_mismo_documento_registran_una_sola(e, monkeypatch):
    nuevo = await documento_pendiente(e)
    barrera = asyncio.Barrier(2)
    original = publicacion._confirmar_publicacion

    async def con_barrera(*args, **kwargs):
        await barrera.wait()  # ambas pasaron las llamadas externas y compiten por la confirmación
        return await original(*args, **kwargs)

    monkeypatch.setattr(publicacion, "_confirmar_publicacion", con_barrera)
    resultados = await asyncio.gather(publicar(e, nuevo), publicar(e, nuevo))
    assert sorted(resultados) == [False, True]
    assert await eventos_de_publicacion(e, nuevo) == 1


async def test_la_publicacion_no_cambia_el_contador_de_estados_solo_la_huella_de_documentos(e):
    antes = await analisis(e)
    await publicar(e, await documento_pendiente(e))
    despues = await analisis(e)
    assert despues.version == antes.version  # el contador de cambios de estado conserva su significado
    assert despues.huella_documentos != antes.huella_documentos

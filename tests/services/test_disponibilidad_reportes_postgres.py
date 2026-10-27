"""Años disponibles para generar el reporte (`GET /reportes/disponibilidad`) sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

La consulta es INFORMATIVA y de solo lectura. Aquí se comprueba que (1) no basta con que exista un documento, (2) distingue revisión
pendiente, validación pendiente, validación vigente y reporte generado, (3) lo que anuncia coincide con lo que decide la generación
real (`generar_reporte`: 201 solo si el año figura `puede_generar`), (4) conserva el 409 de una segunda generación y la descarga del
reporte emitido aunque cambie el análisis, y (5) no hace una consulta por año. El motor HTML→PDF es un doble; no hay proveedores
externos. Sin PostgreSQL estas pruebas se OMITEN (`pytest -rs`).
"""
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from app.core.exceptions import ConflictError
from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.models.documento_ingesta import EstadoProcesamiento
from app.modules.analisis_gri.application import servicio
from app.modules.reportes.service import disponibilidad_de_reportes, generar_reporte
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    MD_GRI_204_Y_302,
    MD_OBSERVADO,
    MD_SOLO_SANCIONES,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    cliente_http,
    comprobar_error,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
)
from tests.services.test_reportes_postgres import CompiladorControlado, construir_app as app_reportes

RUTA = "/reportes/disponibilidad"
REPORTE_GRI = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI


@pytest_asyncio.fixture
async def m7(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        otra = await crear_empresa(db, "Petrolera Sur")
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, superadmin=superadmin, empresa=empresa, otra=otra, compilador=CompiladorControlado())


async def sembrar(m7, texto: str, anio: int = 2025, tipo=TipoDocumento.MEMORIA_ANUAL, empresa=None, **extra):
    async with m7.fabrica() as db:
        return await sembrar_documento(db, empresa or m7.empresa, m7.admin, texto=texto, anio=anio, tipo=tipo, **extra)


async def consultar(m7, empresa=None, anio=None, usuario=None, **cabeceras):
    parametros = {"empresa_id": str((empresa or m7.empresa).id)}
    if anio is not None:
        parametros["anio"] = anio
    async with cliente_http(app_reportes(m7, usuario or m7.admin)) as c:
        return await c.get(RUTA, params=parametros)


async def por_anio(m7, **kwargs) -> dict[int, dict]:
    r = await consultar(m7, **kwargs)
    assert r.status_code == 200, r.text
    return {x["anio"]: x for x in r.json()["anios"]}


async def asignar_todos(m7, anio: int = 2025, empresa=None, estado=EstadoGri.OK) -> None:
    kwargs = dict(ambiente="development", empresa_id=(empresa or m7.empresa).id, anio=anio)
    async with m7.fabrica() as db:
        analisis = await servicio.consultar_analisis(db, **kwargs)
    for fila in analisis.filas:
        async with m7.fabrica() as db:
            await servicio.asignar_estado(db, identidad=fila.identidad, estado=estado, version_fila_esperada=0, actor=m7.admin, **kwargs)


async def validar(m7, anio: int = 2025, empresa=None) -> None:
    kwargs = dict(ambiente="development", empresa_id=(empresa or m7.empresa).id, anio=anio)
    async with m7.fabrica() as db:
        actual = await servicio.consultar_analisis(db, **kwargs)
    async with m7.fabrica() as db:
        await servicio.validar_analisis(db, version_esperada=actual.version, huella_esperada=actual.huella_documentos, actor=m7.admin, **kwargs)


async def generar(m7, anio: int = 2025, empresa=None):
    async with m7.fabrica() as db:
        return await generar_reporte(
            db, ambiente="development", empresa_id=(empresa or m7.empresa).id, anio=anio, usuario_id=m7.admin.id,
            compilar_pdf=m7.compilador,
        )


async def coincide_con_la_generacion(m7, anio: int, anunciado: dict) -> None:
    """Lo anunciado coincide con la operación real: `puede_generar` ⇔ la generación se acepta. Sin efectos si se rechaza."""
    antes = (await _contar_reportes(m7), m7.compilador.llamadas)
    if anunciado["puede_generar"]:
        assert (await generar(m7, anio)).anio == anio
        return
    with pytest.raises(ConflictError) as rechazo:
        await generar(m7, anio)
    assert rechazo.value.code in {"ANALYSIS_NOT_VALIDATED", "REPORT_ALREADY_EXISTS"}
    assert (await _contar_reportes(m7), m7.compilador.llamadas) == antes


async def _contar_reportes(m7) -> int:
    async with m7.fabrica() as db:
        return (await db.execute(text("SELECT count(*) FROM reportes_prospeccion"))).scalar_one()


# ============================================ Elegibilidad ============================================

async def test_sin_documentos_no_hay_anios_y_un_anio_concreto_se_declara_sin_documentacion(m7):
    assert await por_anio(m7) == {}
    concreto = (await por_anio(m7, anio=2025))[2025]
    assert (concreto["estado"], concreto["puede_generar"], concreto["documentos_elegibles"]) == ("SIN_DOCUMENTACION_ELEGIBLE", False, 0)
    await coincide_con_la_generacion(m7, 2025, concreto)


async def test_la_mera_existencia_de_un_documento_no_hace_elegible_el_anio(m7):
    await sembrar(m7, MD_GRI_204_305, 2022, publicado=False)  # completado, publicación vectorial pendiente
    await sembrar(m7, MD_GRI_204_305, 2023, estado=EstadoProcesamiento.FALLIDO)
    await sembrar(m7, MD_GRI_204_305, 2024, ambiente="qa")  # otro ambiente
    async with m7.fabrica() as db:
        inactiva = await crear_empresa(db, "Empresa Inactiva", activa=False)
        await sembrar_documento(db, inactiva, m7.admin, texto=MD_GRI_204_305, anio=2025)
    assert await por_anio(m7) == {}
    assert await por_anio(m7, empresa=inactiva) == {}  # empresa inactiva: nada es elegible
    for anio in (2022, 2023, 2024):
        assert (await por_anio(m7, anio=anio))[anio]["estado"] == "SIN_DOCUMENTACION_ELEGIBLE"


async def test_los_documentos_de_otra_empresa_no_cuentan(m7):
    await sembrar(m7, MD_GRI_204_305, 2025, empresa=m7.otra)
    assert await por_anio(m7) == {}
    assert list(await por_anio(m7, empresa=m7.otra)) == [2025]


# ============================================ Flujo ============================================

async def test_revision_pendiente_validacion_pendiente_y_validacion_vigente(m7):
    await sembrar(m7, MD_GRI_204_305, 2025, tipo=REPORTE_GRI)

    revision = (await por_anio(m7))[2025]
    assert (revision["estado"], revision["puede_generar"]) == ("PENDIENTE_REVISION", False)
    assert (revision["documentos_elegibles"], revision["filas_gri"], revision["filas_sin_estado"]) == (1, 2, 2)
    await coincide_con_la_generacion(m7, 2025, revision)

    await asignar_todos(m7)
    pendiente = (await por_anio(m7))[2025]
    assert (pendiente["estado"], pendiente["puede_generar"], pendiente["filas_sin_estado"]) == ("PENDIENTE_VALIDACION", False, 0)
    assert pendiente["validacion_anterior_no_vigente"] is False
    await coincide_con_la_generacion(m7, 2025, pendiente)

    await validar(m7)
    vigente = (await por_anio(m7))[2025]
    assert (vigente["estado"], vigente["puede_generar"]) == ("VALIDACION_VIGENTE", True)
    await coincide_con_la_generacion(m7, 2025, vigente)  # la generación real lo acepta


async def test_un_cambio_posterior_a_la_validacion_vuelve_a_dejar_el_anio_pendiente_de_validacion(m7):
    await sembrar(m7, MD_GRI_204_305, 2025)
    await asignar_todos(m7)
    await validar(m7)
    assert (await por_anio(m7))[2025]["estado"] == "VALIDACION_VIGENTE"
    async with m7.fabrica() as db:  # un cambio de estado invalida la validación
        await servicio.asignar_estado(
            db, ambiente="development", empresa_id=m7.empresa.id, anio=2025, identidad="204:2016",
            estado=EstadoGri.SUB_REPORTADO, version_fila_esperada=1, actor=m7.admin,
        )
    cambiado = (await por_anio(m7))[2025]
    assert (cambiado["estado"], cambiado["puede_generar"], cambiado["validacion_anterior_no_vigente"]) == ("PENDIENTE_VALIDACION", False, True)
    await coincide_con_la_generacion(m7, 2025, cambiado)
    await sembrar(m7, MD_GRI_204_Y_302, 2025, tipo=REPORTE_GRI)  # un documento nuevo también reabre la revisión
    assert (await por_anio(m7))[2025]["estado"] == "PENDIENTE_REVISION"


async def test_documentos_sin_codigos_gri_no_habilitan_el_reporte(m7):
    await sembrar(m7, MD_SOLO_SANCIONES, 2024)
    await sembrar(m7, MD_OBSERVADO, 2023)
    anios = await por_anio(m7)
    assert (anios[2024]["estado"], anios[2023]["estado"]) == ("SIN_CODIGOS_GRI", "SIN_CODIGOS_GRI")
    for anio in (2024, 2023):
        assert anios[anio]["puede_generar"] is False
        await coincide_con_la_generacion(m7, anio, anios[anio])


# ============================================ Reporte generado ============================================

async def test_un_reporte_emitido_figura_generado_se_rechaza_una_segunda_generacion_y_la_descarga_sigue(m7):
    await sembrar(m7, MD_GRI_204_305, 2025)
    await asignar_todos(m7)
    await validar(m7)
    resumen = await generar(m7)
    generado = (await por_anio(m7))[2025]
    assert (generado["estado"], generado["puede_generar"], generado["reporte_id"]) == ("REPORTE_GENERADO", False, str(resumen.id))
    assert generado["reporte_creado_en"] is not None
    assert generado["documentos_elegibles"] is None
    await coincide_con_la_generacion(m7, 2025, generado)  # 409 REPORT_ALREADY_EXISTS

    # El análisis cambia después: el reporte emitido sigue figurando y se descarga igual, sin recompilar.
    await sembrar(m7, MD_GRI_204_Y_302, 2025, tipo=REPORTE_GRI)
    assert (await por_anio(m7))[2025]["estado"] == "REPORTE_GENERADO"
    compilaciones = m7.compilador.llamadas
    async with cliente_http(app_reportes(m7, m7.admin)) as c:
        descarga = await c.get(f"/reportes/{resumen.id}/archivo")
    assert descarga.status_code == 200
    assert descarga.content.startswith(b"%PDF")
    assert m7.compilador.llamadas == compilaciones
    # Otro año de la misma empresa no queda bloqueado por ese reporte.
    await sembrar(m7, MD_GRI_204_305, 2024)
    anios = await por_anio(m7)
    assert (anios[2025]["estado"], anios[2024]["estado"]) == ("REPORTE_GENERADO", "PENDIENTE_REVISION")
    assert list(anios) == [2025, 2024]  # del más reciente al más antiguo


async def test_un_reporte_existente_figura_aunque_la_empresa_ya_no_tenga_documentos_elegibles(m7):
    await sembrar(m7, MD_GRI_204_305, 2025)
    await asignar_todos(m7)
    await validar(m7)
    await generar(m7)
    async with m7.fabrica() as db:
        await db.execute(text("UPDATE empresas SET activa = false WHERE id = :i"), {"i": m7.empresa.id})
        await db.commit()
    assert (await por_anio(m7))[2025]["estado"] == "REPORTE_GENERADO"


# ============================================ Permisos, aislamiento y errores ============================================

async def test_permisos_y_errores(m7):
    parametros = {"empresa_id": str(m7.empresa.id)}
    async with cliente_http(app_reportes(m7, None)) as sin_sesion, cliente_http(app_reportes(m7, m7.superadmin)) as superadmin:
        comprobar_error(await sin_sesion.get(RUTA, params=parametros), 401, "INVALID_SESSION")
        comprobar_error(await superadmin.get(RUTA, params=parametros), 403, "FORBIDDEN")
    class _Inexistente:
        id = uuid.uuid4()

    comprobar_error(await consultar(m7, empresa=_Inexistente), 404, "COMPANY_NOT_FOUND")
    for anio in (1999, 10000):
        comprobar_error(await consultar(m7, anio=anio), 422, "REQUEST_VALIDATION_ERROR")


async def test_el_aislamiento_por_ambiente_se_aplica_a_estados_y_validaciones(m7):
    await sembrar(m7, MD_GRI_204_305, 2025, ambiente="qa")
    assert await por_anio(m7) == {}  # la aplicación consulta el ambiente `development`


# ============================================ Solo lectura y costo ============================================

async def _consultas_de_disponibilidad(m7, fabrica_pg) -> list[str]:
    """Sentencias que ejecuta la consulta de disponibilidad (no las de la siembra)."""
    sentencias: list[str] = []

    def contar(conexion, cursor, sentencia, *resto):
        sentencias.append(sentencia)

    motor = fabrica_pg.kw["bind"]
    event.listen(motor.sync_engine, "before_cursor_execute", contar)
    try:
        async with m7.fabrica() as db:
            await disponibilidad_de_reportes(db, ambiente="development", empresa_id=m7.empresa.id)
    finally:
        event.remove(motor.sync_engine, "before_cursor_execute", contar)
    return sentencias


async def test_el_numero_de_consultas_no_crece_con_los_anios_y_no_se_escribe(m7, fabrica_pg):
    await sembrar(m7, MD_GRI_204_305, 2010)
    uno = await _consultas_de_disponibilidad(m7, fabrica_pg)
    for anio in range(2011, 2018):
        await sembrar(m7, MD_GRI_204_305, anio)
    async with m7.fabrica() as db:
        antes = (await db.execute(text("SELECT count(*), md5(string_agg(id::text || estado_procesamiento::text, ',' ORDER BY id)) FROM documentos"))).all()
    ocho = await _consultas_de_disponibilidad(m7, fabrica_pg)
    assert (await por_anio(m7)).keys() == set(range(2010, 2018))  # los 8 años aparecen
    lecturas = lambda lista: [s for s in lista if s.lstrip().upper().startswith("SELECT")]
    assert len(lecturas(uno)) == len(lecturas(ocho)) <= 7  # constante: sin una consulta adicional por año
    assert not [s for s in ocho if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    async with m7.fabrica() as db:
        despues = (await db.execute(text("SELECT count(*), md5(string_agg(id::text || estado_procesamiento::text, ',' ORDER BY id)) FROM documentos"))).all()
    assert despues == antes
    assert await _contar_reportes(m7) == 0  # la consulta no generó ningún reporte

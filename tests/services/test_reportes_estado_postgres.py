"""Vista previa coherente, disponibilidad y estado de actualización de los reportes (BUG-15, entrega 2) sobre un PostgreSQL REAL,
AISLADO y TEMPORAL, con migraciones reales. El motor HTML→PDF es un doble que devuelve bytes distintos en cada llamada y guarda el
HTML (permite detectar cualquier recompilación y comparar contenido). Cada prueba comprueba combinaciones COMPLETAS de campos
(estado, `puede_generar`, `motivo_no_generar`, `validacion_anterior_no_vigente`, `reporte_actualizado`, `actualizacion_pendiente`,
`origen_verificable`), no solo un indicador. Sin PostgreSQL se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import TipoDocumento
from app.modules.reportes import service as servicio_reportes
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    MD_GRI_204_Y_302,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    cliente_http,
    comprobar_error,
    completar_esquema_reportes,
    crear_administrador,
    crear_base_aislada,
    crear_empresa,
    crear_superadmin,
    preparar_esquema_anterior_a_la_0007,
    sembrar_documento,
)
from tests.services.test_reportes_postgres import construir_app
from tests.services.test_reportes_versionado_postgres import (
    OK,
    RUTA,
    SUB,
    CompiladorRegistro,
    analisis,
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

GRI = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
CAMPOS_DE_ESTADO = (
    "estado", "puede_generar", "motivo_no_generar", "validacion_anterior_no_vigente", "reporte_actualizado",
    "actualizacion_pendiente", "origen_verificable",
)


# ------------------------------------------------------------------ Entornos ------------------------------------------------------------------

@pytest_asyncio.fixture
async def ent(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        otra = await crear_empresa(db, "Petrolera Sur")
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=2025)
        await sembrar_documento(db, otra, admin, texto=MD_GRI_204_305, anio=2025)
    return SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, otro=otro, superadmin=superadmin, empresa=empresa, otra=otra, compilador=CompiladorRegistro()
    )


@pytest_asyncio.fixture
async def ent_anterior(url_pg_aislado):
    """Base con el esquema de los ambientes con la 0006 pero SIN la 0007: permite sembrar reportes históricos sin validación."""
    url = await crear_base_aislada(url_pg_aislado)
    await preparar_esquema_anterior_a_la_0007(url)
    motor = create_async_engine(url)
    fabrica = async_sessionmaker(motor, expire_on_commit=False)
    async with fabrica() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=2025)
    yield SimpleNamespace(
        fabrica=fabrica, admin=admin, otro=otro, superadmin=superadmin, empresa=empresa, otra=empresa,
        compilador=CompiladorRegistro(), url=url,
    )
    await motor.dispose()


# ------------------------------------------------------------------ Ayudantes ------------------------------------------------------------------

async def anio_de(e, anio: int = 2025, empresa=None) -> dict:
    r = await get(e, f"{RUTA}/disponibilidad", empresa_id=str((empresa or e.empresa).id), anio=anio)
    assert r.status_code == 200, r.text
    return r.json()["anios"][0]


def comprobar(fila: dict, **esperado) -> None:
    """Cada campo esperado se comprueba por separado, con su nombre en el mensaje."""
    for campo, valor in esperado.items():
        assert fila[campo] == valor, campo


async def vista_previa(e, anio: int = 2025):
    async with cliente_http(construir_app(e, e.admin)) as c:
        return await c.post(f"{RUTA}/vista-previa", json={"empresa_id": str(e.empresa.id), "anio": anio})


async def vigente(e, anio: int = 2025):
    return await get(e, f"{RUTA}/vigente", **params(e.empresa, anio))


async def descargar_vigente(e, anio: int = 2025):
    return await get(e, f"{RUTA}/vigente/archivo", **params(e.empresa, anio))


async def por_id(e, reporte_id: str):
    return await get(e, f"{RUTA}/{reporte_id}/archivo")


async def total_descargas(e) -> int:
    return len(await eventos(e, "DESCARGA"))


async def nuevo_documento_sin_estados(e, anio: int = 2025):
    async with e.fabrica() as db:
        await sembrar_documento(db, e.empresa, e.admin, texto=MD_GRI_204_Y_302, anio=anio, tipo=GRI)


async def sembrar_historico(e, snapshot: dict, version: int = 1) -> None:
    """Reporte con la forma de antes del versionado (sin `validacion_id`); solo posible en el esquema anterior a la 0007."""
    async with e.fabrica() as db:
        await db.execute(
            text(
                "INSERT INTO reportes_prospeccion (id, empresa_id, anio, version, validacion_id, generado_por, pdf, sha256, snapshot)"
                " VALUES (gen_random_uuid(), :e, 2025, :v, NULL, :u, :pdf, :h, CAST(:s AS jsonb))"
            ),
            {"e": e.empresa.id, "v": version, "u": e.admin.id, "pdf": b"%PDF-1.4 historico", "h": "c" * 64, "s": json.dumps(snapshot)},
        )
        await db.commit()


async def snapshot_de_validacion(e) -> dict:
    actual = await analisis(e)
    return {"validacion": {"numero": actual.validacion.numero, "huella_documentos": actual.validacion.huella_documentos}}


# ============================================ Vista previa de una emisión existente ============================================

async def test_la_vista_previa_de_una_emision_existente_conserva_los_bytes_aunque_cambie_el_nombre_de_la_empresa(ent):
    await escenario_v1(ent)
    antes = await reportes(ent)
    compilaciones = ent.compilador.llamadas
    async with ent.fabrica() as db:
        await db.execute(text("UPDATE empresas SET nombre = 'Minera Andina Renombrada S.A.' WHERE id = :i"), {"i": ent.empresa.id})
        await db.commit()
    previa = await vista_previa(ent)
    assert previa.status_code == 200
    assert previa.content == antes[0][6]  # exactamente el PDF almacenado
    assert ent.compilador.llamadas == compilaciones  # no se recompiló ni se reconstruyó con el nombre actual
    assert await reportes(ent) == antes
    assert len(await eventos(ent, "GENERACION_REPORTE")) == 1  # consultar no es emitir


async def test_la_vista_previa_de_una_emision_existente_ignora_otros_metadatos_modificados(ent):
    await escenario_v1(ent)
    antes = await reportes(ent)
    async with ent.fabrica() as db:
        await db.execute(text("UPDATE usuarios SET nombre = 'Ana Renombrada' WHERE id = :i"), {"i": ent.admin.id})
        await db.commit()
    previa = await vista_previa(ent)
    assert previa.content == antes[0][6]
    assert (await get(ent, RUTA)).json()[0]["generado_por_nombre"] == "Ana Renombrada"  # los metadatos son vivos; el PDF no
    assert await reportes(ent) == antes


async def test_no_se_llama_al_compilador_si_la_emision_existe(ent):
    await escenario_v1(ent)

    def no_debe_compilar(_html):
        raise AssertionError("una emisión respaldada no se recompila")

    ent.compilador = no_debe_compilar
    previa = await vista_previa(ent)
    assert previa.status_code == 200
    assert previa.content.startswith(b"%PDF")


async def test_una_validacion_sin_reporte_genera_una_vista_previa_candidata_sin_guardar_ni_reservar_numero(ent):
    await revisar_y_validar(ent, [OK, SUB])
    primera = await vista_previa(ent)
    segunda = await vista_previa(ent)
    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert ent.compilador.llamadas == 2  # candidata: se compila cada vez
    assert "Versión 1 del reporte" in ent.compilador.htmls[0]
    assert await reportes(ent) == []
    assert await eventos(ent, "GENERACION_REPORTE") == []
    emitido = await emitir(ent)
    assert emitido.status_code == 201
    assert emitido.json()["version"] == 1  # las vistas previas no reservaron ningún número


async def test_la_vista_previa_candidata_y_la_emision_usan_el_mismo_constructor_y_los_mismos_datos(ent):
    await revisar_y_validar(ent, [OK, SUB])
    await vista_previa(ent)
    await emitir(ent)
    html_previa, html_emision = ent.compilador.htmls
    assert html_previa == html_emision


async def test_la_vista_previa_exige_validacion_vigente_aunque_haya_un_reporte_anterior(ent):
    await escenario_v1(ent)
    await asignar_estados(ent, [OK, OK])  # cambios sin validar
    antes = await reportes(ent)
    comprobar_error(await vista_previa(ent), 409, "ANALYSIS_NOT_VALIDATED")
    assert await reportes(ent) == antes


async def test_la_vista_previa_de_un_historico_verificable_devuelve_sus_bytes_originales(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, await snapshot_de_validacion(e))
    await completar_esquema_reportes(e.url)
    previa = await vista_previa(e)
    assert previa.status_code == 200
    assert previa.content == b"%PDF-1.4 historico"  # no se agrega visualmente una versión ni se recompila
    assert e.compilador.llamadas == 0


async def test_un_historico_de_origen_no_verificable_no_se_presume_equivalente_a_la_validacion(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, {})  # snapshot sin número ni huella de validación
    await completar_esquema_reportes(e.url)
    previa = await vista_previa(e)
    assert previa.status_code == 200
    assert previa.content != b"%PDF-1.4 historico"
    assert e.compilador.llamadas == 1  # es una vista previa candidata
    assert "Versión 2 del reporte" in e.compilador.htmls[0]  # el histórico conserva la versión 1; la candidata sería la 2


async def test_ninguna_consulta_altera_pdf_snapshot_version_ni_fecha(ent):
    await escenario_v1(ent)
    await escenario_validacion_2(ent)
    antes = await reportes(ent)
    eventos_antes = (len(await eventos(ent, "GENERACION_REPORTE")), len(await eventos(ent, "DESCARGA")))
    await vista_previa(ent)
    await anio_de(ent)
    await vigente(ent)
    await get(ent, RUTA)
    comprobar_error(await descargar_vigente(ent), 409, "REPORT_UPDATE_PENDING")
    assert await reportes(ent) == antes  # id, versión, validación, autor, PDF, SHA-256, snapshot y fecha
    eventos_despues = (len(await eventos(ent, "GENERACION_REPORTE")), len(await eventos(ent, "DESCARGA")))
    assert eventos_despues == eventos_antes


# ============================================ Disponibilidad y estados coherentes ============================================

async def test_sin_reporte_y_sin_validacion(ent):
    fila = await anio_de(ent)
    comprobar(
        fila, estado="PENDIENTE_REVISION", puede_generar=False, motivo_no_generar="PENDIENTE_REVISION",
        validacion_anterior_no_vigente=False, reporte_actualizado=None, actualizacion_pendiente=False, origen_verificable=None,
        reporte_id=None, versiones_emitidas=0,
    )


async def test_un_anio_consultado_sin_documentos_elegibles(ent):
    fila = await anio_de(ent, 2019)
    comprobar(
        fila, estado="SIN_DOCUMENTACION_ELEGIBLE", puede_generar=False, motivo_no_generar="SIN_DOCUMENTACION_ELEGIBLE",
        validacion_anterior_no_vigente=False, reporte_actualizado=None, actualizacion_pendiente=False, origen_verificable=None,
        documentos_elegibles=0,
    )


async def test_sin_reporte_y_con_validacion_vigente(ent):
    await revisar_y_validar(ent, [OK, SUB])
    comprobar(
        await anio_de(ent), estado="VALIDACION_VIGENTE", puede_generar=True, motivo_no_generar=None,
        validacion_anterior_no_vigente=False, reporte_actualizado=None, actualizacion_pendiente=False, origen_verificable=None,
    )


async def test_reporte_actualizado(ent):
    v1 = await escenario_v1(ent)
    fila = await anio_de(ent)
    comprobar(
        fila, estado="REPORTE_GENERADO", puede_generar=False, motivo_no_generar="REPORTE_ACTUALIZADO",
        validacion_anterior_no_vigente=False, reporte_actualizado=True, actualizacion_pendiente=False, origen_verificable=True,
        reporte_id=v1["id"], reporte_version=1, versiones_emitidas=1,
    )
    repetida = await emitir(ent)  # no hay nada que emitir: la solicitud repetida es idempotente
    assert (repetida.status_code, repetida.json()["id"]) == (200, v1["id"])


async def test_reporte_existente_con_estados_modificados_sin_validar(ent):
    v1 = await escenario_v1(ent)
    await asignar_estados(ent, [OK, OK])
    comprobar(
        await anio_de(ent), estado="PENDIENTE_VALIDACION", puede_generar=False, motivo_no_generar="PENDIENTE_VALIDACION",
        validacion_anterior_no_vigente=True, reporte_actualizado=False, actualizacion_pendiente=False, origen_verificable=True,
        reporte_id=v1["id"], reporte_version=1,  # el reporte anterior queda como referencia
    )


async def test_reporte_existente_con_filas_pendientes_de_revision(ent):
    v1 = await escenario_v1(ent)
    await nuevo_documento_sin_estados(ent)
    comprobar(
        await anio_de(ent), estado="PENDIENTE_REVISION", puede_generar=False, motivo_no_generar="PENDIENTE_REVISION",
        validacion_anterior_no_vigente=True, reporte_actualizado=False, actualizacion_pendiente=False, origen_verificable=True,
        reporte_id=v1["id"], reporte_version=1,
    )


async def test_nueva_validacion_vigente_sin_emitir(ent):
    v1 = await escenario_v1(ent)
    await escenario_validacion_2(ent)
    comprobar(
        await anio_de(ent), estado="ACTUALIZACION_PENDIENTE", puede_generar=True, motivo_no_generar=None,
        validacion_anterior_no_vigente=False, reporte_actualizado=False, actualizacion_pendiente=True, origen_verificable=True,
        reporte_id=v1["id"], reporte_version=1, ultima_validacion_numero=2, reporte_validacion_numero=1,
    )


async def test_validacion_posterior_que_despues_deja_de_estar_vigente(ent):
    v1 = await escenario_v1(ent)
    await escenario_validacion_2(ent)
    await asignar_estados(ent, [SUB, OK])  # invalida la validación #2 antes de emitirla
    comprobar(
        await anio_de(ent), estado="PENDIENTE_VALIDACION", puede_generar=False, motivo_no_generar="PENDIENTE_VALIDACION",
        validacion_anterior_no_vigente=True, reporte_actualizado=False, actualizacion_pendiente=True, origen_verificable=True,
        reporte_id=v1["id"], ultima_validacion_numero=2,
    )


async def test_sin_documentos_elegibles_se_conserva_el_historico_sin_anunciarlo_actualizado(ent):
    v1 = await escenario_v1(ent)
    async with ent.fabrica() as db:
        await db.execute(text("UPDATE empresas SET activa = false WHERE id = :i"), {"i": ent.empresa.id})
        await db.commit()
    comprobar(
        await anio_de(ent), estado="SIN_DOCUMENTACION_ELEGIBLE", puede_generar=False, motivo_no_generar="SIN_DOCUMENTACION_ELEGIBLE",
        validacion_anterior_no_vigente=True, reporte_actualizado=False, actualizacion_pendiente=False, origen_verificable=True,
        reporte_id=v1["id"], reporte_version=1,
    )
    assert (await por_id(ent, v1["id"])).status_code == 200  # y sigue descargable por su identificador


async def test_historico_verificable_que_respalda_la_validacion_vigente(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, await snapshot_de_validacion(e))
    await completar_esquema_reportes(e.url)
    comprobar(
        await anio_de(e), estado="REPORTE_GENERADO", puede_generar=False, motivo_no_generar="REPORTE_ACTUALIZADO",
        validacion_anterior_no_vigente=False, reporte_actualizado=True, actualizacion_pendiente=False, origen_verificable=True,
        reporte_version=1, reporte_validacion_numero=1,
    )


async def test_historico_verificable_de_una_validacion_anterior(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, await snapshot_de_validacion(e))
    await completar_esquema_reportes(e.url)
    await escenario_validacion_2(e)
    comprobar(
        await anio_de(e), estado="ACTUALIZACION_PENDIENTE", puede_generar=True, motivo_no_generar=None,
        validacion_anterior_no_vigente=False, reporte_actualizado=False, actualizacion_pendiente=True, origen_verificable=True,
    )


async def test_historico_con_datos_incompletos_no_se_declara_actualizado_y_permite_generar(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, {"validacion": {"numero": 1}})  # falta la huella: no puede verificarse
    await completar_esquema_reportes(e.url)
    comprobar(
        await anio_de(e), estado="VALIDACION_VIGENTE", puede_generar=True, motivo_no_generar=None,
        validacion_anterior_no_vigente=False, reporte_actualizado=False, actualizacion_pendiente=False, origen_verificable=False,
        reporte_version=1,
    )


async def test_historico_sin_validacion_conocida(ent_anterior):
    e = ent_anterior
    await sembrar_historico(e, {})
    await completar_esquema_reportes(e.url)
    comprobar(
        await anio_de(e), estado="PENDIENTE_REVISION", puede_generar=False, motivo_no_generar="PENDIENTE_REVISION",
        validacion_anterior_no_vigente=False, reporte_actualizado=False, actualizacion_pendiente=False, origen_verificable=False,
        ultima_validacion_numero=None,
    )


async def test_un_historico_no_verificable_con_validacion_vigente_deja_emitir_una_version_nueva_y_se_conserva(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, {})
    await completar_esquema_reportes(e.url)
    nueva = await emitir(e)
    assert nueva.status_code == 201
    assert nueva.json()["version"] == 2
    filas = await reportes(e)
    assert [f[3] for f in filas] == [1, 2]
    assert filas[0][6] == b"%PDF-1.4 historico"  # el histórico sigue intacto
    comprobar(
        await anio_de(e), estado="REPORTE_GENERADO", puede_generar=False, motivo_no_generar="REPORTE_ACTUALIZADO",
        validacion_anterior_no_vigente=False, reporte_actualizado=True, actualizacion_pendiente=False, origen_verificable=True,
        reporte_version=2, versiones_emitidas=2,
    )


async def test_la_consulta_de_la_ultima_emision_describe_un_historico_no_verificable_sin_fallar(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, {})
    await completar_esquema_reportes(e.url)
    r = await vigente(e)
    assert r.status_code == 200
    comprobar(
        r.json(), validacion_id=None, validacion_numero=None, origen_verificable=False, reporte_actualizado=False,
        actualizacion_pendiente=False, puede_generar=True, motivo_no_generar=None, validacion_anterior_no_vigente=False,
        version=1, ultima_validacion_numero=1,
    )


# ============================================ Descarga del reporte actualizado ============================================

async def test_la_descarga_actualizada_devuelve_la_version_correcta(ent):
    v1 = await escenario_v1(ent)
    pdf1 = (await reportes(ent))[0][6]
    r1 = await descargar_vigente(ent)
    assert (r1.status_code, r1.content, r1.headers["x-reporte-version"]) == (200, pdf1, "1")
    await escenario_validacion_2(ent)
    v2 = (await emitir(ent)).json()
    pdf2 = (await reportes(ent))[1][6]
    r2 = await descargar_vigente(ent)
    assert (r2.status_code, r2.content, r2.headers["x-reporte-version"]) == (200, pdf2, "2")
    assert (await por_id(ent, v1["id"])).content == pdf1
    assert (await por_id(ent, v2["id"])).content == pdf2


async def test_una_validacion_nueva_sin_emitir_se_rechaza_como_actualizacion_pendiente(ent):
    v1 = await escenario_v1(ent)
    await escenario_validacion_2(ent)
    error = comprobar_error(await descargar_vigente(ent), 409, "REPORT_UPDATE_PENDING")
    comprobar(
        error["details"], reporte_id=v1["id"], version=1, ultima_validacion_numero=2, origen_verificable=True,
        estado="ACTUALIZACION_PENDIENTE", puede_generar=True, motivo_no_generar=None, accion_sugerida="EMITIR",
    )
    assert (await por_id(ent, v1["id"])).content == (await reportes(ent))[0][6]


async def test_los_cambios_sin_validar_no_se_presentan_como_reporte_actualizado(ent):
    v1 = await escenario_v1(ent)
    await asignar_estados(ent, [OK, OK])
    error = comprobar_error(await descargar_vigente(ent), 409, "REPORT_ANALYSIS_NOT_CURRENT")
    comprobar(
        error["details"], reporte_id=v1["id"], version=1, origen_verificable=True, estado="PENDIENTE_VALIDACION",
        puede_generar=False, motivo_no_generar="PENDIENTE_VALIDACION", accion_sugerida="VALIDAR",
    )
    meta = (await vigente(ent)).json()
    comprobar(meta, reporte_actualizado=False, actualizacion_pendiente=False, validacion_anterior_no_vigente=True, id=v1["id"])
    assert (await por_id(ent, v1["id"])).content == (await reportes(ent))[0][6]


async def test_las_filas_pendientes_de_revision_orientan_a_revisar(ent):
    v1 = await escenario_v1(ent)
    await nuevo_documento_sin_estados(ent)
    error = comprobar_error(await descargar_vigente(ent), 409, "REPORT_ANALYSIS_NOT_CURRENT")
    comprobar(error["details"], motivo_no_generar="PENDIENTE_REVISION", accion_sugerida="REVISAR", reporte_id=v1["id"])


async def test_un_historico_no_verificable_se_rechaza_con_su_error_especifico(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, {})
    await completar_esquema_reportes(e.url)
    historico = (await reportes(e))[0]
    error = comprobar_error(await descargar_vigente(e), 409, "REPORT_ORIGIN_UNVERIFIABLE")  # no REPORT_UPDATE_PENDING
    comprobar(
        error["details"], reporte_id=str(historico[0]), version=1, origen_verificable=False, estado="VALIDACION_VIGENTE",
        puede_generar=True, motivo_no_generar=None, accion_sugerida="EMITIR",
    )
    descarga = await por_id(e, str(historico[0]))
    assert (descarga.status_code, descarga.content) == (200, b"%PDF-1.4 historico")  # la descarga explícita sigue disponible


async def test_un_historico_no_verificable_con_cambios_sin_validar_orienta_a_validar(ent_anterior):
    e = ent_anterior
    await revisar_y_validar(e, [OK, SUB])
    await sembrar_historico(e, {})
    await completar_esquema_reportes(e.url)
    await asignar_estados(e, [OK, OK])
    error = comprobar_error(await descargar_vigente(e), 409, "REPORT_ORIGIN_UNVERIFIABLE")
    comprobar(error["details"], puede_generar=False, motivo_no_generar="PENDIENTE_VALIDACION", accion_sugerida="VALIDAR")


async def test_sin_reportes_la_descarga_y_la_consulta_conservan_report_not_found(ent):
    comprobar_error(await descargar_vigente(ent), 404, "REPORT_NOT_FOUND")
    comprobar_error(await vigente(ent), 404, "REPORT_NOT_FOUND")


async def test_la_auditoria_registra_la_descarga_exitosa_y_ningun_exito_en_los_rechazos(ent):
    v1 = await escenario_v1(ent)
    assert await total_descargas(ent) == 0
    assert (await descargar_vigente(ent)).status_code == 200
    (evento,) = await eventos(ent, "DESCARGA")
    assert evento[0] == ent.admin.id
    assert evento[1] == "EXITOSO"
    assert "version=1" in evento[2]
    await escenario_validacion_2(ent)
    comprobar_error(await descargar_vigente(ent), 409, "REPORT_UPDATE_PENDING")
    await asignar_estados(ent, [SUB, OK])
    comprobar_error(await descargar_vigente(ent), 409, "REPORT_UPDATE_PENDING")
    assert await total_descargas(ent) == 1  # los rechazos no dejan ningún evento de éxito
    assert (await por_id(ent, v1["id"])).status_code == 200
    assert await total_descargas(ent) == 2  # la descarga por identificador sí registra la suya


async def test_los_roles_y_la_autenticacion_de_las_rutas_afectadas(ent):
    destinos = [
        ("POST", f"{RUTA}/vista-previa", {"empresa_id": str(ent.empresa.id), "anio": 2025}, None),
        ("GET", f"{RUTA}/vigente", None, params(ent.empresa)),
        ("GET", f"{RUTA}/vigente/archivo", None, params(ent.empresa)),
        ("GET", f"{RUTA}/disponibilidad", None, {"empresa_id": str(ent.empresa.id), "anio": 2025}),
    ]
    async with cliente_http(construir_app(ent, None)) as sin_sesion, cliente_http(construir_app(ent, ent.superadmin)) as super_:
        for metodo, url, cuerpo, consulta in destinos:
            comprobar_error(await sin_sesion.request(metodo, url, json=cuerpo, params=consulta), 401, "INVALID_SESSION")
            comprobar_error(await super_.request(metodo, url, json=cuerpo, params=consulta), 403, "FORBIDDEN")
    assert ent.compilador.llamadas == 0


# ============================================ Lecturas coherentes ============================================

async def _consultar_ultima_emision(e):
    async with e.fabrica() as db:
        return await servicio_reportes.obtener_reporte_vigente(db, ambiente="development", empresa_id=e.empresa.id, anio=2025)


async def _descargar_ultima_emision(e):
    async with e.fabrica() as db:
        return await servicio_reportes.obtener_pdf_vigente(
            db, ambiente="development", empresa_id=e.empresa.id, anio=2025, usuario_id=e.admin.id
        )


class _Pausa:
    def __init__(self):
        self.llegada, self.salida = asyncio.Event(), asyncio.Event()

    async def detener(self):
        self.llegada.set()
        await self.salida.wait()


def _pausar_antes_de_leer_el_analisis(monkeypatch, pausa: _Pausa) -> None:
    """La instantánea ya se tomó (consultas de la empresa y de las emisiones); el análisis y las validaciones se leen DESPUÉS."""
    original = servicio_reportes._contextos

    async def envoltorio(*args, **kwargs):
        await pausa.detener()
        return await original(*args, **kwargs)

    monkeypatch.setattr(servicio_reportes, "_contextos", envoltorio)


async def test_una_emision_y_una_validacion_durante_la_consulta_no_se_mezclan_con_la_instantanea(ent, monkeypatch):
    v1 = await escenario_v1(ent)
    pausa = _Pausa()
    with monkeypatch.context() as m:
        _pausar_antes_de_leer_el_analisis(m, pausa)
        consulta = asyncio.create_task(_consultar_ultima_emision(ent))
        await asyncio.wait_for(pausa.llegada.wait(), 15)
        await escenario_validacion_2(ent)  # validación #2 …
        v2 = (await emitir(ent)).json()  # … y su emisión, ambas confirmadas mientras la consulta sigue abierta
        pausa.salida.set()
        durante = await asyncio.wait_for(consulta, 15)
    comprobar(
        durante.model_dump(), id=uuid.UUID(v1["id"]), version=1, reporte_actualizado=True, actualizacion_pendiente=False,
        puede_generar=False, es_ultima_version=True,
    )  # todo del mismo instante: la versión 1 y la validación #1, aún vigente en ese instante
    despues = (await vigente(ent)).json()
    comprobar(despues, id=v2["id"], version=2, reporte_actualizado=True, actualizacion_pendiente=False)


async def test_una_validacion_nueva_durante_la_descarga_no_mezcla_instantes(ent, monkeypatch):
    v1 = await escenario_v1(ent)
    pdf1 = (await reportes(ent))[0][6]
    pausa = _Pausa()
    with monkeypatch.context() as m:
        _pausar_antes_de_leer_el_analisis(m, pausa)
        descarga = asyncio.create_task(_descargar_ultima_emision(ent))
        await asyncio.wait_for(pausa.llegada.wait(), 15)
        await escenario_validacion_2(ent)  # validación posterior confirmada durante la descarga
        pausa.salida.set()
        pdf, resumen = await asyncio.wait_for(descarga, 15)
    assert pdf == pdf1  # coherente con el instante de la instantánea (la validación #2 aún no existía)
    assert str(resumen.id) == v1["id"]
    comprobar_error(await descargar_vigente(ent), 409, "REPORT_UPDATE_PENDING")  # y una consulta posterior ya la ve

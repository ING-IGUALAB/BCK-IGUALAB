"""Citas agrupadas por HTTP y en el reporte, sobre un PostgreSQL REAL, AISLADO y TEMPORAL (`conftest.py`).

Se comprueba, con el servicio real del Módulo 5 y el de reportes: la respuesta HTTP trae `citas_agrupadas` y
`total_citas_agrupadas` sin alterar los campos existentes; consultar no escribe nada (tampoco con datos del formato anterior); la
validación y el puntaje ESG funcionan igual; el snapshot de un reporte nuevo conserva la forma anterior y un reporte ya emitido no se
modifica. El motor HTML→PDF es un doble que captura el HTML. Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
import json
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import text

from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.application import servicio
from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from app.modules.reportes.service import generar_reporte
from tests.ayudantes_analisis_gri import (
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    cliente_http,
    consultar,
    construir_app,
    crear_administrador,
    crear_empresa,
    fila_de,
    sembrar_documento,
)
from tests.services.test_analisis_gri_citas_legibles_postgres import CompiladorQueCaptura

FRASE = "Cumplimos GRI 204-1 con proveedores locales."
PRIMERO = (
    "# Informe\n\n## Compras\n\n" + FRASE + "\n\n## Contratos\n\n" + FRASE + "\n\n"
    "### Emisiones\n\nRedujimos GRI 305-1, GRI 305-2 y GRI 305-3 en el periodo.\n"
)
SEGUNDO = "# Informe\n\n## Compras\n\n" + FRASE + "\n"
MEMORIA = "memoria.md"
CITAS = "citas_agrupadas"
TOTAL_CITAS = "total_citas_agrupadas"
MENCIONES = "total_menciones"
EVIDENCIAS = "evidencias"
PASAJES = "pasajes"
AMBIENTE = "development"


@pytest_asyncio.fixture
async def m5(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        empresa = await crear_empresa(db, "Minera Andina")
        # Mismo nombre de archivo, documentos distintos (tipos distintos: la base rechaza dos activos del mismo tipo y año).
        uno = await sembrar_documento(
            db, empresa, admin, texto=PRIMERO, tipo=TipoDocumento.MEMORIA_ANUAL, nombre=MEMORIA
        )
        dos = await sembrar_documento(
            db, empresa, admin, texto=SEGUNDO, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI, nombre=MEMORIA
        )
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, empresa=empresa, uno=uno, dos=dos, compilador=CompiladorQueCaptura())


async def sql(m5, consulta: str, **parametros):
    async with m5.fabrica() as db:
        return [tuple(f) for f in (await db.execute(text(consulta), parametros)).all()]


async def volcado(m5) -> dict[str, list]:
    tablas = {
        "documentos": "SELECT id, estado_procesamiento, resultado_analisis, md5(analisis::text), vector_publicado_en FROM documentos",
        "estados": "SELECT identidad, estado FROM analisis_gri_estados",
        "historial": "SELECT identidad FROM analisis_gri_historial",
        "control": "SELECT version FROM analisis_gri_control",
        "reportes": "SELECT id FROM reportes_prospeccion",
    }
    return {nombre: [tuple(map(str, f)) for f in await sql(m5, consulta)] for nombre, consulta in tablas.items()}


async def validar_todo(m5) -> None:
    kwargs = dict(ambiente=AMBIENTE, empresa_id=m5.empresa.id, anio=2025)
    async with m5.fabrica() as db:
        analisis = await servicio.consultar_analisis(db, **kwargs)
    for fila in analisis.filas:
        async with m5.fabrica() as db:
            await servicio.asignar_estado(
                db, identidad=fila.identidad, estado=EstadoGri.OK, version_fila_esperada=0, actor=m5.admin, **kwargs
            )
    async with m5.fabrica() as db:
        actual = await servicio.consultar_analisis(db, **kwargs)
    async with m5.fabrica() as db:
        await servicio.validar_analisis(
            db, version_esperada=actual.version, huella_esperada=actual.huella_documentos, actor=m5.admin, **kwargs
        )


# ============================================ HTTP ============================================

async def test_la_respuesta_trae_las_citas_agrupadas_y_no_altera_los_campos_existentes(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    f204 = fila_de(a, "204:2016")
    assert f204[MENCIONES] == len(f204[EVIDENCIAS]) == 3  # nada se pierde
    assert len(f204[PASAJES]) == 3  # los pasajes posicionales siguen siendo los de antes
    assert f204[TOTAL_CITAS] == len(f204[CITAS]) == 2  # no se deriva de total_menciones
    primera, segunda = f204[CITAS]
    assert (primera["documento_id"], primera["documento_nombre"]) == (str(m5.uno.id), MEMORIA)
    assert (segunda["documento_id"], segunda["documento_nombre"]) == (str(m5.dos.id), MEMORIA)  # mismo nombre, no se fusionan
    assert (primera["cita_legible"], primera[MENCIONES]) == (FRASE, 2)
    assert primera["referencias"] == ["GRI 204-1"]
    assert [(u["seccion_legible"], u[MENCIONES]) for u in primera["ubicaciones"]] == [
        ("Informe > Compras", 1), ("Informe > Contratos", 1)
    ]  # todas las ubicaciones del mismo texto, no solo la primera
    assert (segunda["cita_legible"], segunda[MENCIONES]) == (FRASE, 1)
    assert primera["indices_evidencia"] + segunda["indices_evidencia"] == [0, 1, 2]
    assert sorted(primera["indices_pasaje"] + segunda["indices_pasaje"]) == [0, 1, 2]
    f305 = fila_de(a, "305:2016")
    assert (f305[MENCIONES], f305[TOTAL_CITAS]) == (3, 1)
    assert f305[CITAS][0][MENCIONES] == 3  # tres referencias de un pasaje: una cita, tres menciones
    assert f305[CITAS][0]["referencias"] == ["GRI 305-1", "GRI 305-2", "GRI 305-3"]
    for original in (MENCIONES, "menciones_en_indice", "menciones_en_cuerpo", "documentos", EVIDENCIAS, PASAJES):
        assert original in f204  # los campos existentes siguen en la respuesta


async def test_consultar_no_escribe_y_el_original_queda_intacto(m5):
    antes = await volcado(m5)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        for _ in range(3):
            await consultar(c, m5.empresa)
    assert await volcado(m5) == antes
    assert sorted(await sql(m5, "SELECT sha256 FROM documentos")) == sorted([(m5.uno.sha256,), (m5.dos.sha256,)])


async def test_un_analisis_del_formato_anterior_se_presenta_agrupado_sin_reingesta_ni_escrituras(m5):
    """Documento ingerido con una cita que empieza a mitad de palabra y sin campos legibles guardados: se agrupa al responder."""
    analisis = analizar_texto("# Informe\n\nCumplimos GRI 204-1 con proveedores.\n\nCumplimos GRI 204-1 con proveedores.\n").a_dict()
    for mencion in analisis["gri"][0]["menciones"]:
        mencion["cita"] = "ado **ambiental** y GRI 204-1<br>cierre"
    async with m5.fabrica() as db:
        otra = await crear_empresa(db, "Empresa Antigua")
        await sembrar_documento(db, otra, m5.admin, analisis=analisis)
    antes = await volcado(m5)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = (await c.get(f"/analisis-gri/empresas/{otra.id}/anios/2025")).json()
    f = fila_de(a, "204:2016")
    assert f[EVIDENCIAS][0]["cita"] == "ado **ambiental** y GRI 204-1<br>cierre"  # el original, intacto
    assert f[TOTAL_CITAS] == 1
    assert f[CITAS][0]["cita_legible"] == "ado ambiental y GRI 204-1\ncierre"
    assert f[CITAS][0][MENCIONES] == f[MENCIONES] == 2
    assert await volcado(m5) == antes


async def test_las_citas_agrupadas_no_cambian_la_validacion_ni_el_puntaje(m5):
    await validar_todo(m5)
    async with m5.fabrica() as db:
        a = await servicio.consultar_analisis(db, ambiente=AMBIENTE, empresa_id=m5.empresa.id, anio=2025)
    assert a.habilita_reporte is True
    assert a.puntaje.filas_contadas == len(a.filas) == 2  # una contribución por identidad aunque haya 3 y 3 menciones
    assert (a.puntaje.valor, a.puntaje.suma_puntos, a.puntaje.definitivo) == (100.0, 200, True)
    assert [f.total_menciones for f in a.filas] == [3, 3]
    assert [f.total_citas_agrupadas for f in a.filas] == [2, 1]
    huella = a.huella_documentos
    async with m5.fabrica() as db:
        otra = await servicio.consultar_analisis(db, ambiente=AMBIENTE, empresa_id=m5.empresa.id, anio=2025)
    assert (otra.huella_documentos, otra.version) == (huella, a.version)  # la vigencia no depende de la presentación


# ============================================ Reporte ============================================

async def test_el_snapshot_de_un_reporte_nuevo_conserva_la_forma_anterior_y_el_html_no_cambia(m5):
    await validar_todo(m5)
    async with m5.fabrica() as db:
        resumen = await generar_reporte(
            db, ambiente=AMBIENTE, empresa_id=m5.empresa.id, anio=2025, usuario_id=m5.admin.id, compilar_pdf=m5.compilador
        )
    snapshot = json.loads((await sql(m5, "SELECT snapshot::text FROM reportes_prospeccion WHERE id = :i", i=resumen.id))[0][0])
    for fila in snapshot["filas"]:
        assert CITAS not in fila
        assert TOTAL_CITAS not in fila
        assert {EVIDENCIAS, PASAJES, MENCIONES} <= set(fila)
    fila204 = next(f for f in snapshot["filas"] if f["identidad"] == "204:2016")
    assert len(fila204[PASAJES]) == 3
    assert fila204[MENCIONES] == 3
    html = m5.compilador.html[0]
    assert html.count(FRASE) == 1  # el reporte sigue mostrando el primer pasaje de la fila, como antes
    assert html.count("Redujimos GRI 305-1, GRI 305-2 y GRI 305-3") == 1


async def test_un_reporte_emitido_no_cambia_al_consultar_con_las_citas_agrupadas(m5):
    await validar_todo(m5)
    async with m5.fabrica() as db:
        await generar_reporte(
            db, ambiente=AMBIENTE, empresa_id=m5.empresa.id, anio=2025, usuario_id=m5.admin.id, compilar_pdf=m5.compilador
        )
    columnas = "id, version, validacion_id, pdf, sha256, snapshot::text, creado_en, generado_por"
    emitido = await sql(m5, f"SELECT {columnas} FROM reportes_prospeccion")
    antes = await volcado(m5)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    assert a["habilita_reporte"] is True
    assert await sql(m5, f"SELECT {columnas} FROM reportes_prospeccion") == emitido
    assert await volcado(m5) == antes

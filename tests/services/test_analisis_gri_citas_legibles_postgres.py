"""Citas legibles y pasajes por HTTP y en el reporte, sobre un PostgreSQL REAL, AISLADO y TEMPORAL (`conftest.py`).

Se comprueba, con el servicio real del Módulo 5 y el de reportes: la respuesta HTTP trae los campos nuevos sin alterar los
originales; consultar no escribe ni reanaliza; un análisis con citas del formato anterior sigue siendo legible; el reporte nuevo
usa el texto legible; el ESG no cambia; y un reporte YA emitido (PDF y snapshot) no se modifica. El motor HTML→PDF es un doble que
captura el HTML (no se prueba WeasyPrint aquí). Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
import hashlib
import json
import uuid
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

TEXTO = (
    "# **Gestión** ambiental<br>2025\n\n### Emisiones\n\n"
    "La empresa<br>redujo emisiones según GRI 305-1, GRI 305-2 y GRI 305-3 durante el periodo.\n\n"
    "| Indicador | Referencia |\n|---|---|\n| **Proveedores** | GRI 204-1 |\n"
)


class CompiladorQueCaptura:
    def __init__(self):
        self.html: list[str] = []

    def __call__(self, html: str) -> bytes:
        self.html.append(html)
        return f"%PDF-1.4 compilacion-{len(self.html)}".encode()


@pytest_asyncio.fixture
async def m5(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        empresa = await crear_empresa(db, "Minera Andina")
        documento = await sembrar_documento(db, empresa, admin, texto=TEXTO, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, empresa=empresa, documento=documento, compilador=CompiladorQueCaptura())


async def sql(m5, consulta: str, **parametros):
    async with m5.fabrica() as db:
        return [tuple(f) for f in (await db.execute(text(consulta), parametros)).all()]


async def volcado_de_tablas(m5) -> dict[str, list]:
    tablas = {
        "documentos": "SELECT id, estado_procesamiento, resultado_analisis, md5(analisis::text), vector_publicado_en FROM documentos",
        "estados": "SELECT identidad, estado FROM analisis_gri_estados",
        "historial": "SELECT identidad FROM analisis_gri_historial",
        "control": "SELECT version FROM analisis_gri_control",
        "reportes": "SELECT id FROM reportes_prospeccion",
    }
    return {nombre: [tuple(map(str, f)) for f in await sql(m5, consulta)] for nombre, consulta in tablas.items()}


async def validar_todo(m5) -> None:
    kwargs = dict(ambiente="development", empresa_id=m5.empresa.id, anio=2025)
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

async def test_la_respuesta_trae_los_campos_legibles_y_los_pasajes_sin_alterar_los_originales(m5):
    mencion_original = next(g for g in m5.documento.analisis["gri"] if g["codigo"] == "305")["menciones"][0]
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    f305 = fila_de(a, "305:2016")
    assert f305["total_menciones"] == len(f305["evidencias"]) == 3  # nada se pierde
    ev = f305["evidencias"][0]
    assert "<br>" in ev["cita"]
    assert "**" in ev["seccion"]  # original intacto, con sus marcas
    assert ev["cita"] == mencion_original["cita"]
    assert ev["cita_inicio"] == mencion_original["cita_inicio"]
    assert ev["cita_legible"] == "La empresa\nredujo emisiones según GRI 305-1, GRI 305-2 y GRI 305-3 durante el periodo."
    assert ev["seccion_legible"] == "Gestión ambiental 2025 > Emisiones"
    assert len(f305["pasajes"]) == 1
    assert f305["pasajes"][0]["total_apariciones"] == 3
    assert [x["indice_evidencia"] for x in f305["pasajes"][0]["apariciones"]] == [0, 1, 2]
    f204 = fila_de(a, "204:2016")
    assert f204["evidencias"][0]["cita"] == "| **Proveedores** | GRI 204-1 |"
    assert f204["evidencias"][0]["cita_legible"] == "Proveedores | GRI 204-1"
    assert a["documentos"][0]["nombre_archivo"] == m5.documento.nombre_archivo  # documento de origen
    assert f305["evidencias"][0]["documento_id"] == str(m5.documento.id)


async def test_consultar_no_escribe_ni_reanaliza_ni_toca_el_original(m5):
    antes = await volcado_de_tablas(m5)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        for _ in range(3):
            await consultar(c, m5.empresa)
    assert await volcado_de_tablas(m5) == antes
    assert await sql(m5, "SELECT sha256 FROM documentos") == [(m5.documento.sha256,)]  # el SHA-256 del archivo original


async def test_un_analisis_con_citas_del_formato_anterior_sigue_siendo_legible(m5):
    """Documento ingerido antes de corregir los límites: la cita empieza a mitad de palabra. Se muestra tal cual, sin reescribirla."""
    analisis = analizar_texto("# Informe\n\nCumplimos GRI 204-1 con proveedores.\n").a_dict()
    analisis["gri"][0]["menciones"][0]["cita"] = "ado **ambiental** y GRI 204-1<br>cierre"
    async with m5.fabrica() as db:
        otro = await crear_empresa(db, "Empresa Antigua")
        await sembrar_documento(db, otro, m5.admin, analisis=analisis)
    antes = await volcado_de_tablas(m5)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = (await c.get(f"/analisis-gri/empresas/{otro.id}/anios/2025")).json()
    ev = fila_de(a, "204:2016")["evidencias"][0]
    assert ev["cita"] == "ado **ambiental** y GRI 204-1<br>cierre"
    assert ev["cita_legible"] == "ado ambiental y GRI 204-1\ncierre"
    assert await volcado_de_tablas(m5) == antes


# ============================================ Reporte ============================================

async def test_el_reporte_nuevo_usa_el_texto_legible_y_el_snapshot_conserva_original_y_legible(m5):
    await validar_todo(m5)
    async with m5.fabrica() as db:
        resumen = await generar_reporte(
            db, ambiente="development", empresa_id=m5.empresa.id, anio=2025, usuario_id=m5.admin.id, compilar_pdf=m5.compilador
        )
    html = m5.compilador.html[0]
    assert "redujo emisiones según GRI 305-1" in html
    assert "Gestión ambiental 2025" in html
    assert "&lt;br&gt;" not in html
    assert "###" not in html
    assert "**" not in html  # ni marcas ni etiquetas escapadas en la cita
    assert html.count("redujo emisiones según GRI 305-1") == 1  # el pasaje compartido por tres referencias se muestra una vez
    snapshot = (await sql(m5, "SELECT snapshot::text FROM reportes_prospeccion WHERE id = :i", i=resumen.id))[0][0]
    fila = next(f for f in json.loads(snapshot)["filas"] if f["identidad"] == "305:2016")
    assert "<br>" in fila["evidencias"][0]["cita"]
    assert fila["evidencias"][0]["cita_legible"].startswith("La empresa\nredujo")
    assert fila["total_menciones"] == 3
    assert len(fila["pasajes"]) == 1


async def test_el_puntaje_esg_es_el_mismo_con_y_sin_presentacion(m5):
    await validar_todo(m5)
    async with m5.fabrica() as db:
        a = await servicio.consultar_analisis(db, ambiente="development", empresa_id=m5.empresa.id, anio=2025)
    assert a.puntaje.filas_contadas == len(a.filas) == 2  # una contribución por identidad, con 3 + 1 menciones
    assert (a.puntaje.valor, a.puntaje.suma_puntos, a.puntaje.definitivo) == (100.0, 200, True)


async def test_un_reporte_ya_emitido_no_cambia_aunque_cambie_el_analisis_o_su_presentacion(m5):
    await validar_todo(m5)
    async with m5.fabrica() as db:
        resumen = await generar_reporte(
            db, ambiente="development", empresa_id=m5.empresa.id, anio=2025, usuario_id=m5.admin.id, compilar_pdf=m5.compilador
        )
    columnas = "id, pdf, sha256, snapshot::text, creado_en, generado_por"
    emitido = await sql(m5, f"SELECT {columnas} FROM reportes_prospeccion")
    # El análisis cambia después (un documento nuevo invalida la validación) y se consulta varias veces.
    async with m5.fabrica() as db:
        await sembrar_documento(db, m5.empresa, m5.admin, texto="# Otro\n\nTambién GRI 302-1.\n", tipo=TipoDocumento.MEMORIA_ANUAL)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        assert (await consultar(c, m5.empresa))["habilita_reporte"] is False
    assert await sql(m5, f"SELECT {columnas} FROM reportes_prospeccion") == emitido
    assert hashlib.sha256(emitido[0][1]).hexdigest() == resumen.sha256


async def test_un_reporte_emitido_con_el_formato_anterior_se_descarga_y_lista_sin_cambios(m5):
    """Reporte guardado antes de existir los campos legibles (snapshot sin `cita_legible` ni `pasajes`): se entrega tal cual."""
    pdf = b"%PDF-1.4 emitido-antes"
    snapshot = {"anio": 2025, "filas": [{"identidad": "305:2016", "evidencias": [{"cita": "### Emisiones<br>**x**"}]}]}
    reporte_id = uuid.uuid4()
    async with m5.fabrica() as db:
        await db.execute(
            text(
                "INSERT INTO reportes_prospeccion (id, empresa_id, anio, generado_por, pdf, sha256, snapshot) "
                "VALUES (:i, :e, 2025, :u, :pdf, :h, CAST(:s AS jsonb))"
            ),
            {"i": reporte_id, "e": m5.empresa.id, "u": m5.admin.id, "pdf": pdf, "h": hashlib.sha256(pdf).hexdigest(), "s": json.dumps(snapshot)},
        )
        await db.commit()
    antes = await sql(m5, "SELECT id, pdf, sha256, snapshot::text FROM reportes_prospeccion")
    from tests.services.test_reportes_postgres import construir_app as app_reportes  # app mínima con el router de reportes

    entorno = SimpleNamespace(fabrica=m5.fabrica, compilador=m5.compilador)
    async with cliente_http(app_reportes(entorno, m5.admin)) as c:
        descarga = await c.get(f"/reportes/{reporte_id}/archivo")
        listado = await c.get("/reportes")
    assert descarga.status_code == 200
    assert descarga.content == pdf
    assert [x["id"] for x in listado.json()] == [str(reporte_id)]
    assert m5.compilador.html == []  # nada se recompiló
    assert await sql(m5, "SELECT id, pdf, sha256, snapshot::text FROM reportes_prospeccion") == antes

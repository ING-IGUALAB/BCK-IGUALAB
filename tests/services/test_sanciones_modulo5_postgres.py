"""Sanciones económicas: del detector al análisis persistido y a la consulta del Módulo 5, sobre un PostgreSQL REAL, AISLADO y
TEMPORAL (`conftest.py`). El `analisis` se genera con los detectores reales (sin IA, MinIO, OCI ni red) y se persiste en
`documentos.analisis` (JSONB) con el esquema real (migración 0004); la consulta es la del endpoint HTTP.

Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`) y no cuentan como ejecutadas.

También fija qué ocurre con los análisis YA persistidos con el detector anterior (sin sanciones): no se tocan al leer, se
distinguen por `version_detector_sanciones` y una corrección controlada en su lugar no altera estados, historial ni validaciones.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import TipoDocumento
from app.models.documento_ingesta import ResultadoAnalisis
from app.services.ingesta.analisis_ingesta import MOTIVO_SIN_HALLAZGOS, analizar_texto
from app.services.ingesta.deteccion_sanciones import VERSION_DETECTOR_SANCIONES
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    asegurar_esquema_real,
    asignar,
    cliente_http,
    consultar,
    construir_app,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
    validar,
)

DATOS = Path(__file__).parent / "datos"
REPORTE = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
MEMORIA = TipoDocumento.MEMORIA_ANUAL


def leer(nombre: str) -> str:
    return (DATOS / nombre).read_bytes().decode("utf-8")


@pytest_asyncio.fixture
async def m5(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Petroandes de prueba")
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, superadmin=superadmin, empresa=empresa)


async def sql(m5, consulta: str, **parametros):
    async with m5.fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


async def sembrar(m5, *, texto=None, analisis=None, tipo=REPORTE):
    async with m5.fabrica() as db:
        return await sembrar_documento(db, m5.empresa, m5.admin, texto=texto, analisis=analisis, tipo=tipo)


def analisis_del_detector_anterior(texto: str) -> dict:
    """Lo que dejó persistido el detector anterior: sin la clave de versión y, en estos documentos, SIN sanciones."""
    actual = analizar_texto(texto).a_dict()
    anterior = {**actual, "sanciones": [], "descartes_sanciones": {}}
    anterior["advertencias"] = [a for a in actual["advertencias"] if a["categoria"] != "sanciones"]
    anterior.pop("version_detector_sanciones")
    # Sin sanciones, la clasificación era la de solo GRI: un documento sin estándares catalogados quedaba OBSERVADO.
    anterior["motivos"] = [m for m in actual["motivos"] if m != "sanciones_economicas"] or [MOTIVO_SIN_HALLAZGOS]
    anterior["resultado"] = "CON_HALLAZGOS" if anterior["motivos"] != [MOTIVO_SIN_HALLAZGOS] else "OBSERVADO"
    return anterior


# ============================================ Detector → análisis persistido → consulta ============================================

async def test_las_sanciones_de_los_documentos_de_evidencia_llegan_a_la_consulta_del_modulo_5(m5):
    energia, mixtas = leer("energia_andina_gri_2024.md"), leer("sanciones_mixtas_gri_2024.md")
    doc_energia = await sembrar(m5, texto=energia, tipo=REPORTE)
    doc_mixtas = await sembrar(m5, texto=mixtas, tipo=MEMORIA)
    textos = {str(doc_energia.id): energia, str(doc_mixtas.id): mixtas}

    persistido = (await sql(m5, "SELECT analisis FROM documentos WHERE id = :i", i=doc_mixtas.id))[0].analisis
    assert persistido["version_detector_sanciones"] == VERSION_DETECTOR_SANCIONES
    assert [s["monto"] is None for s in persistido["sanciones"]] == [False, True, False, True]

    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)

    # El conteo es de MENCIONES: dos documentos, tres menciones por suceso descrito en párrafo y tabla.
    assert a["resumen_sanciones"] == {"total": 6, "cuantificadas": 4, "no_cuantificadas": 2, "por_unidad": {"S/": 2, "soles": 2}}
    por_documento = {
        str(doc_energia.id): [s for s in a["sanciones"] if s["documento_id"] == str(doc_energia.id)],
        str(doc_mixtas.id): [s for s in a["sanciones"] if s["documento_id"] == str(doc_mixtas.id)],
    }
    assert [len(v) for v in por_documento.values()] == [2, 4]

    for documento_id, sanciones in por_documento.items():
        texto = textos[documento_id]
        for s in sanciones:  # cada cita y cada posición son un trozo EXACTO del original almacenado
            assert texto[s["inicio"]:s["fin"]] == s["referencia_original"]
            assert texto[s["cita_inicio"]:s["cita_fin"]] == s["cita"]
            if s["monto"] is not None:
                assert texto[s["monto"]["inicio"]:s["monto"]["fin"]] == s["monto"]["texto"]
            if s["entidad"] is not None:
                assert texto[s["entidad"]["inicio"]:s["entidad"]["fin"]] == s["entidad"]["texto"]

    no_cuantificadas = [s for s in a["sanciones"] if not s["cuantificada"]]
    assert len(no_cuantificadas) == 2
    for s in no_cuantificadas:
        assert s["monto"] is None  # nunca cero
        assert "monto_no_especificado_en_el_documento" in s["motivos_revision"]
    assert "no especifica un monto económico" in next(s for s in no_cuantificadas if not s["en_tabla"])["cita"]

    de_tabla = [s for s in a["sanciones"] if s["en_tabla"] and s["monto"] is not None]
    assert len(de_tabla) == 2
    for s in de_tabla:
        assert (s["monto"]["unidad"], s["monto"]["vinculo"], s["entidad"]["texto"]) == ("S/", "encabezado_de_columna", "OEFA")
        texto = textos[s["documento_id"]]
        assert texto[s["monto"]["unidad_inicio"]:s["monto"]["unidad_fin"]] == "S/"
    assert all(s["monto"]["unidad_inicio"] is None for s in a["sanciones"] if s["monto"] and not s["en_tabla"])

    # Las sanciones no son parte del puntaje ESG: con los estados GRI pendientes no hay puntaje.
    assert a["puntaje"]["valor"] is None


async def test_la_consulta_no_recalcula_ni_modifica_el_analisis_persistido(m5):
    doc = await sembrar(m5, analisis=analisis_del_detector_anterior(leer("sanciones_mixtas_gri_2024.md")))
    antes = await sql(m5, "SELECT analisis::text FROM documentos WHERE id = :i", i=doc.id)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    # El análisis vacío ya persistido se muestra tal cual: leer no «arregla» en silencio lo que guardó el detector anterior.
    assert a["sanciones"] == []
    assert a["resumen_sanciones"]["total"] == 0
    assert await sql(m5, "SELECT analisis::text FROM documentos WHERE id = :i", i=doc.id) == antes


# ============================================ Análisis persistidos con el detector anterior ============================================

CONSULTA_CANDIDATOS = (
    "SELECT id FROM documentos WHERE estado_procesamiento = 'COMPLETADO' AND analisis IS NOT NULL "
    "AND coalesce(analisis->>'version_detector_sanciones', '') <> :version "
    "AND jsonb_array_length(analisis->'sanciones') = 0"
)


async def test_los_analisis_del_detector_anterior_se_identifican_sin_modificarlos(m5):
    mixtas = leer("sanciones_mixtas_gri_2024.md")
    viejo = await sembrar(m5, analisis=analisis_del_detector_anterior(mixtas), tipo=REPORTE)
    nuevo = await sembrar(m5, texto=mixtas, tipo=MEMORIA)
    candidatos = {fila.id for fila in await sql(m5, CONSULTA_CANDIDATOS, version=VERSION_DETECTOR_SANCIONES)}
    assert candidatos == {viejo.id}  # solo el persistido sin versión y sin sanciones; el nuevo ya tiene la versión actual
    assert nuevo.id not in candidatos


async def test_corregir_un_analisis_vacio_en_su_lugar_no_toca_estados_historial_ni_validaciones(m5):
    """Documenta el comportamiento ACTUAL: el estado humano vive en tablas aparte (por identidad GRI) y la validación vigente
    depende de los documentos (id + SHA-256) y de la versión de estados, NO del contenido de `analisis`. Por eso una corrección
    controlada en su lugar no invalida la validación: la decisión de exigir revalidación es del equipo y la operación debe
    auditarse. Esta prueba NO ejecuta la corrección: simula con SQL lo que haría una operación autorizada."""
    texto = MD_GRI_204_305 + "\nLa SUNAFIL emitió una sanción administrativa. La resolución no especifica un monto económico.\n"
    doc = await sembrar(m5, analisis=analisis_del_detector_anterior(texto))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        for identidad in ("204:2016", "305:2016"):
            assert (await asignar(c, m5.empresa, identidad, "OK")).status_code == 200
        a = await consultar(c, m5.empresa)
        assert (await validar(c, m5.empresa, a)).status_code == 201
        antes_tablas = {
            tabla: await sql(m5, f"SELECT * FROM {tabla} ORDER BY 1")
            for tabla in ("analisis_gri_estados", "analisis_gri_historial", "analisis_gri_validaciones", "analisis_gri_control")
        }
        assert a["sanciones"] == []

        # Corrección controlada (simulada): vuelve a analizar el MISMO original y reemplaza solo `analisis`.
        nuevo = analizar_texto(texto).a_dict()
        assert nuevo["resultado"] == ResultadoAnalisis.CON_HALLAZGOS.value == doc.resultado_analisis.value
        async with m5.fabrica() as db:
            await db.execute(
                text("UPDATE documentos SET analisis = CAST(:a AS jsonb) WHERE id = :i"),
                {"a": json.dumps(nuevo), "i": doc.id},
            )
            await db.commit()

        despues = await consultar(c, m5.empresa)

    assert [s["monto"] for s in despues["sanciones"]] == [None]
    assert despues["resumen_sanciones"] == {"total": 1, "cuantificadas": 0, "no_cuantificadas": 1, "por_unidad": {}}
    for tabla, filas in antes_tablas.items():
        assert await sql(m5, f"SELECT * FROM {tabla} ORDER BY 1") == filas, tabla
    assert despues["validacion"]["vigente"] is True  # la huella es de id + SHA-256: no cambió
    assert despues["huella_documentos"] == a["huella_documentos"]
    assert despues["version"] == a["version"]


async def test_un_observado_antiguo_pasa_a_con_hallazgos_solo_si_se_actualiza_la_clasificacion_a_la_vez(m5):
    """El CHECK de la base exige que `resultado_analisis` y `analisis->>'resultado'` coincidan: una corrección en su lugar de un
    OBSERVADO que ahora tiene sanciones debe cambiar ambos en la misma sentencia (no se puede dejar uno solo)."""
    texto = "# Informe\n\nLa SUNAFIL emitió una sanción administrativa. La resolución no especifica un monto económico.\n"
    antiguo = analisis_del_detector_anterior(texto)
    assert antiguo["resultado"] == "OBSERVADO"
    doc = await sembrar(m5, analisis=antiguo)
    nuevo = analizar_texto(texto).a_dict()
    assert nuevo["resultado"] == "CON_HALLAZGOS"
    solo_analisis = text("UPDATE documentos SET analisis = CAST(:a AS jsonb) WHERE id = :i")
    parametros = {"a": json.dumps(nuevo), "i": doc.id}
    async with m5.fabrica() as db:
        with pytest.raises(IntegrityError):
            await db.execute(solo_analisis, parametros)
        await db.rollback()
    async with m5.fabrica() as db:
        await db.execute(
            text(
                "UPDATE documentos SET analisis = CAST(:a AS jsonb), resultado_analisis = 'CON_HALLAZGOS' WHERE id = :i"
            ),
            {"a": json.dumps(nuevo), "i": doc.id},
        )
        await db.commit()
    fila = (await sql(m5, "SELECT resultado_analisis::text AS r, analisis->>'resultado' AS a FROM documentos WHERE id = :i", i=doc.id))[0]
    assert (fila.r, fila.a) == ("CON_HALLAZGOS", "CON_HALLAZGOS")

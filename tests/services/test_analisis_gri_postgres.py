"""Módulo 5 (análisis y validación GRI) de extremo a extremo sobre un PostgreSQL REAL, AISLADO y TEMPORAL (`conftest.py`).

El esquema del módulo se crea con la migración 0004 real. Los documentos se siembran con el `analisis` que producen los
detectores reales de la ingesta; no se usa IA, MinIO ni la base vectorial, y la aplicación de prueba NO tiene gestor de ingesta.
Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`) y no cuentan como ejecutadas.
"""
import asyncio
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError
from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.models.documento_ingesta import EstadoProcesamiento
from app.modules.analisis_gri.application import servicio
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    MD_GRI_204_Y_302,
    MD_OBSERVADO,
    MD_SOLO_SANCIONES,
    asegurar_esquema_real,
    asignar,
    cliente_http,
    comprobar_error,
    consultar,
    construir_app,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    fila_de,
    publicar,
    ruta,
    sembrar_documento,
    validar,
)

REPORTE = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI


@pytest_asyncio.fixture
async def m5(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, otro=otro, superadmin=superadmin, empresa=empresa, url=url_pg_aislado)


async def sql(m5, consulta: str, **parametros):
    async with m5.fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


async def con_documentos(m5, *textos_y_tipos):
    """Siembra un documento por cada (texto, tipo) para la empresa de prueba (2025)."""
    creados = []
    async with m5.fabrica() as db:
        for texto, tipo in textos_y_tipos:
            creados.append(await sembrar_documento(db, m5.empresa, m5.admin, texto=texto, tipo=tipo))
    return creados


# ============================================ Permisos y errores ============================================

async def test_sin_sesion_401_y_superadmin_403_en_cada_endpoint(m5):
    destinos = [
        ("GET", ruta(m5.empresa), None),
        ("PUT", ruta(m5.empresa, sufijo="/codigos/204:2016/estado"), {"estado": "OK", "version_fila_esperada": 0}),
        ("GET", ruta(m5.empresa, sufijo="/historial"), None),
        ("POST", ruta(m5.empresa, sufijo="/validacion"), {"version": 0, "huella_documentos": "a" * 64}),
    ]
    async with cliente_http(construir_app(m5.fabrica, None)) as sin_sesion, \
            cliente_http(construir_app(m5.fabrica, m5.superadmin)) as como_superadmin:
        for metodo, url, cuerpo in destinos:
            r = await sin_sesion.request(metodo, url, json=cuerpo)
            comprobar_error(r, 401, "INVALID_SESSION")
            r = await como_superadmin.request(metodo, url, json=cuerpo)
            comprobar_error(r, 403, "FORBIDDEN")
    assert await sql(m5, "SELECT 1 FROM analisis_gri_control") == []  # el 403 no escribió nada


async def test_el_rol_no_reemplaza_la_prioridad_del_401_sobre_la_validacion_de_la_ruta(m5):
    async with cliente_http(construir_app(m5.fabrica, None)) as c:
        r = await c.get("/analisis-gri/empresas/no-es-uuid/anios/1")
        comprobar_error(r, 401, "INVALID_SESSION")


async def test_validaciones_de_ruta_y_cuerpo_usan_el_contrato_422(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        for url in (
            "/analisis-gri/empresas/no-es-uuid/anios/2025",
            ruta(m5.empresa, 1999),
            ruta(m5.empresa, sufijo="/historial?pagina=0"),
            ruta(m5.empresa, sufijo="/historial?identidad=GRI-204"),
        ):
            comprobar_error(await c.get(url), 422, "REQUEST_VALIDATION_ERROR")
        malas = [
            {"estado": "CRITICO", "version_fila_esperada": 0},  # no existe un cuarto estado
            {"estado": "", "version_fila_esperada": 0},
            {"estado": "NO_ASIGNADO", "version_fila_esperada": 0},
            {"estado": "OK"},  # falta la versión
            {"estado": "OK", "version_fila_esperada": -1},
            {"estado": "OK", "version_fila_esperada": 0, "usuario_id": str(uuid.uuid4())},  # el autor no viaja en el payload
        ]
        for cuerpo in malas:
            r = await c.put(ruta(m5.empresa, sufijo="/codigos/204:2016/estado"), json=cuerpo)
            comprobar_error(r, 422, "REQUEST_VALIDATION_ERROR")
        r = await c.put(ruta(m5.empresa, sufijo="/codigos/204-1/estado"), json={"estado": "OK", "version_fila_esperada": 0})
        comprobar_error(r, 422, "REQUEST_VALIDATION_ERROR")
        r = await c.post(ruta(m5.empresa, sufijo="/validacion"), json={"version": 1, "huella_documentos": "corta"})
        comprobar_error(r, 422, "REQUEST_VALIDATION_ERROR")
    assert await sql(m5, "SELECT 1 FROM analisis_gri_historial") == []


async def test_empresa_inexistente_es_404_en_todos_los_endpoints(m5):
    inexistente = SimpleNamespace(id=uuid.uuid4())
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        comprobar_error(await c.get(ruta(inexistente)), 404, "COMPANY_NOT_FOUND")
        comprobar_error(await asignar(c, inexistente, "204:2016", "OK"), 404, "COMPANY_NOT_FOUND")
        comprobar_error(await c.get(ruta(inexistente, sufijo="/historial")), 404, "COMPANY_NOT_FOUND")
        r = await c.post(ruta(inexistente, sufijo="/validacion"), json={"version": 0, "huella_documentos": "a" * 64})
        comprobar_error(r, 404, "COMPANY_NOT_FOUND")


# ============================================ Consulta ============================================

async def test_consulta_sin_documentos_elegibles_es_documentacion_insuficiente(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    assert (a["estado_flujo"], a["habilita_reporte"], a["filas"], a["documentos"], a["version"]) == (
        "DOCUMENTACION_INSUFICIENTE", False, [], [], 0)
    assert a["puntaje"]["estado"] == "NO_DISPONIBLE"
    assert a["puntaje"]["valor"] is None
    assert a["pendientes"][0]["codigo"] == "DOCUMENTACION_INSUFICIENTE"
    assert a["validacion"] is None
    assert a["empresa"]["nombre"] == "Minera Andina"
    assert a["ambiente"] == "development"
    assert a["anio"] == 2025
    assert await sql(m5, "SELECT 1 FROM analisis_gri_control") == []  # consultar no escribe


async def test_consulta_devuelve_filas_con_evidencia_pendientes_y_version(m5):
    reporte, memoria = await con_documentos(m5, (MD_GRI_204_305, REPORTE), (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    assert a["estado_flujo"] == "PENDIENTE_ESTADOS"
    assert a["habilita_reporte"] is False
    assert [f["identidad"] for f in a["filas"]] == ["204:2016", "302:2016", "305:2016"]
    f204 = fila_de(a, "204:2016")
    assert f204["estado"] is None
    assert f204["estado_etiqueta"] is None
    assert f204["version_fila"] == 0
    assert f204["asignado_por"] is None
    assert f204["total_menciones"] == 3
    assert set(f204["documentos"]) == {str(reporte.id), str(memoria.id)}
    assert {e["documento_nombre"] for e in f204["evidencias"]} == {reporte.nombre_archivo, memoria.nombre_archivo}
    assert all({"seccion", "rol", "referencia_original", "cita", "inicio", "fin", "cita_inicio", "cita_fin"} <= set(e) for e in f204["evidencias"])
    assert a["pendientes"] == [{
        "codigo": "ESTADOS_PENDIENTES", "mensaje": "Faltan estados por asignar en 3 de 3 filas.", "cantidad": 3,
        "identidades": ["204:2016", "302:2016", "305:2016"]}]
    assert a["puntaje"]["estado"] == "PENDIENTE_ESTADOS"
    assert a["puntaje"]["valor"] is None
    assert len(a["huella_documentos"]) == 64
    assert a["version"] == 0
    assert len(a["documentos"]) == 2


async def test_la_consulta_no_modifica_documentos_ni_detecta_de_nuevo(m5):
    (doc,) = await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    antes = await sql(m5, "SELECT analisis::text, advertencias::text FROM documentos WHERE id = :i", i=doc.id)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await consultar(c, m5.empresa)
        await asignar(c, m5.empresa, "204:2016", "OK")
    assert await sql(m5, "SELECT analisis::text, advertencias::text FROM documentos WHERE id = :i", i=doc.id) == antes


# ============================================ Asignación de estados e historial ============================================

async def test_asignar_estado_registra_historial_con_autor_de_la_sesion_y_fecha_utc(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        r = await asignar(c, m5.empresa, "204:2016", "BAJA_SUSTANCIA")
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert (cuerpo["cambio"], cuerpo["estado_anterior"], cuerpo["estado"], cuerpo["version_fila"], cuerpo["version_analisis"]) == (
            True, None, "BAJA_SUSTANCIA", 1, 1)
        assert cuerpo["asignado_por"] == {"id": str(m5.admin.id), "nombre": "Ana Admin"}
        a = await consultar(c, m5.empresa)
    fila = fila_de(a, "204:2016")
    assert (fila["estado"], fila["estado_etiqueta"], fila["version_fila"], fila["asignado_por"]["nombre"]) == (
        "BAJA_SUSTANCIA", "Baja sustancia", 1, "Ana Admin")
    assert a["version"] == 1  # solo esa fila cambió
    assert fila_de(a, "305:2016")["estado"] is None
    (h,) = await sql(m5, "SELECT estado_anterior, estado_nuevo, usuario_id, cambiado_en, identidad, version_analisis, codigo, edicion, "
                         "identidad_tipo FROM analisis_gri_historial")
    assert (h.estado_anterior, h.estado_nuevo, h.usuario_id, h.identidad, h.version_analisis, h.codigo, h.edicion, h.identidad_tipo) == (
        None, "BAJA_SUSTANCIA", m5.admin.id, "204:2016", 1, "204", "2016", "EDICION_RESUELTA")
    assert h.cambiado_en.utcoffset().total_seconds() == 0  # UTC


async def test_cambiar_el_estado_conserva_estado_anterior_y_nuevo_y_un_cambio_por_fila(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        assert (await asignar(c, m5.empresa, "204:2016", "OK")).status_code == 200
        r = await asignar(c2, m5.empresa, "204:2016", "SUB_REPORTADO", version_fila=1)
        assert r.status_code == 200
        assert r.json()["estado_anterior"] == "OK"
        assert r.json()["version_fila"] == 2
        assert r.json()["asignado_por"]["nombre"] == "Beto Admin"
        pagina = (await c.get(ruta(m5.empresa, sufijo="/historial"))).json()
    assert pagina["total"] == 2
    nuevo, viejo = pagina["items"]  # recientes primero
    assert (nuevo["estado_anterior"], nuevo["estado_nuevo"], nuevo["usuario"]["nombre"], nuevo["version_analisis"]) == ("OK", "SUB_REPORTADO", "Beto Admin", 2)
    assert (viejo["estado_anterior"], viejo["estado_nuevo"], viejo["usuario"]["nombre"], viejo["version_analisis"]) == (None, "OK", "Ana Admin", 1)
    assert nuevo["identidad"] == "204:2016"
    assert nuevo["codigo"] == "204"
    assert nuevo["edicion"] == "2016"
    assert nuevo["cambiado_en"] >= viejo["cambiado_en"]


async def test_reasignar_el_mismo_estado_no_cambia_nada_ni_invalida(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await asignar(c, m5.empresa, "204:2016", "OK")
        r = await asignar(c, m5.empresa, "204:2016", "OK", version_fila=1)
        assert r.status_code == 200
        assert r.json()["cambio"] is False
        assert r.json()["version_fila"] == 1
        assert r.json()["version_analisis"] == 1
        assert r.json()["estado_anterior"] == "OK"
        assert (await consultar(c, m5.empresa))["version"] == 1
    assert len(await sql(m5, "SELECT 1 FROM analisis_gri_historial")) == 1


async def test_el_historial_se_filtra_por_identidad_y_se_pagina(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await asignar(c, m5.empresa, "204:2016", "OK")
        await asignar(c, m5.empresa, "305:2016", "OK")
        await asignar(c, m5.empresa, "204:2016", "BAJA_SUSTANCIA", version_fila=1)
        todo = (await c.get(ruta(m5.empresa, sufijo="/historial"))).json()
        solo = (await c.get(ruta(m5.empresa, sufijo="/historial?identidad=305:2016"))).json()
        pag = (await c.get(ruta(m5.empresa, sufijo="/historial?tamano=2&pagina=2"))).json()
        vacio = (await c.get(ruta(m5.empresa, 2024, "/historial"))).json()
    assert todo["total"] == 3
    assert [i["version_analisis"] for i in todo["items"]] == [3, 2, 1]
    assert solo["total"] == 1
    assert solo["items"][0]["identidad"] == "305:2016"
    assert (pag["total"], pag["pagina"], pag["tamano"], len(pag["items"])) == (3, 2, 2, 1)
    assert vacio["total"] == 0
    assert vacio["items"] == []


async def test_asignar_a_una_identidad_inexistente_es_404_y_no_escribe(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        error = comprobar_error(await asignar(c, m5.empresa, "999:2016", "OK"), 404, "ANALYSIS_ROW_NOT_FOUND")
        assert error["details"]["identidad"] == "999:2016"
        # Una edición que el texto no menciona tampoco existe: el sistema no busca ausencias ni crea filas.
        comprobar_error(await asignar(c, m5.empresa, "204:2020", "OK"), 404, "ANALYSIS_ROW_NOT_FOUND")
        comprobar_error(await asignar(c, m5.empresa, "204:ambigua", "OK"), 404, "ANALYSIS_ROW_NOT_FOUND")
    assert await sql(m5, "SELECT 1 FROM analisis_gri_estados") == []
    assert await sql(m5, "SELECT 1 FROM analisis_gri_control") == []


async def test_no_se_puede_asignar_a_una_fila_de_un_documento_no_elegible(m5):
    async with m5.fabrica() as db:
        await sembrar_documento(db, m5.empresa, m5.admin, texto=MD_GRI_204_305, publicado=False)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        comprobar_error(await asignar(c, m5.empresa, "204:2016", "OK"), 404, "ANALYSIS_ROW_NOT_FOUND")


async def test_ediciones_ambiguas_y_no_catalogadas_se_asignan_por_separado_de_la_resuelta(m5):
    await con_documentos(m5, ("# X\n\nGRI 102 (2025) y GRI 102 y GRI 102 (2030).\n", REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
        assert [f["identidad"] for f in a["filas"]] == ["102:2025", "102:no-catalogada-2030", "102:ambigua"]  # ediciones primero; la ambigua al final
        assert fila_de(a, "102:ambigua")["requiere_revision"]
        assert fila_de(a, "102:ambigua")["advertencias"][0]["codigo"] == "GRI_EDICION_AMBIGUA"
        assert (await asignar(c, m5.empresa, "102:ambigua", "SUB_REPORTADO")).status_code == 200
        a = await consultar(c, m5.empresa)
    assert fila_de(a, "102:ambigua")["estado"] == "SUB_REPORTADO"
    assert fila_de(a, "102:2025")["estado"] is None
    assert fila_de(a, "102:no-catalogada-2030")["estado"] is None


# ============================================ Validación ============================================

async def completar_y_validar(c, m5, estados: dict[str, str]):
    for identidad, estado in estados.items():
        r = await asignar(c, m5.empresa, identidad, estado)
        assert r.status_code == 200, r.text
    a = await consultar(c, m5.empresa)
    r = await validar(c, m5.empresa, a)
    assert r.status_code == 201, r.text
    return a, r.json()


async def test_los_estados_pendientes_impiden_validar(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await asignar(c, m5.empresa, "204:2016", "OK")
        a = await consultar(c, m5.empresa)
        error = comprobar_error(await validar(c, m5.empresa, a), 409, "ANALYSIS_PENDING_STATES")
    assert error["details"] == {"filas_sin_estado": ["305:2016"], "total_filas": 2}
    assert await sql(m5, "SELECT 1 FROM analisis_gri_validaciones") == []


async def test_sin_documentos_elegibles_no_hay_nada_que_validar(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
        comprobar_error(await validar(c, m5.empresa, a), 409, "ANALYSIS_NO_ELIGIBLE_DOCUMENTS")


async def test_validar_persiste_quien_cuando_version_documentos_y_estados_y_habilita_el_reporte(m5):
    reporte, memoria = await con_documentos(m5, (MD_GRI_204_305, REPORTE), (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        antes, respuesta = await completar_y_validar(
            c, m5, {"204:2016": "OK", "302:2016": "BAJA_SUSTANCIA", "305:2016": "SUB_REPORTADO"})
        assert respuesta["estado_flujo"] == "VALIDADO"
        assert respuesta["habilita_reporte"] is True
        assert respuesta["puntaje"]["estado"] == "VALIDADO"
        assert respuesta["puntaje"]["valor"] == 50.0
        assert respuesta["puntaje"]["definitivo"] is True
        assert respuesta["validacion"]["vigente"] is True
        a = await consultar(c, m5.empresa)
    assert a["estado_flujo"] == "VALIDADO"
    assert a["habilita_reporte"] is True
    assert a["pendientes"] == []
    assert a["puntaje"]["definitivo"] is True
    assert a["puntaje"]["valor"] == 50.0
    assert a["validacion"]["vigente"] is True
    assert a["validacion"]["validado_por"]["nombre"] == "Ana Admin"
    assert a["validacion"]["numero"] == 1
    assert a["version"] == 3 == a["validacion"]["version_estados"]
    assert a["huella_documentos"] == antes["huella_documentos"]
    (v,) = await sql(m5, "SELECT * FROM analisis_gri_validaciones")
    assert (v.validado_por, v.numero, v.version_estados, v.huella_documentos, v.puntaje_suma, v.puntaje_filas) == (
        m5.admin.id, 1, 3, antes["huella_documentos"], 150, 3)
    assert v.validado_en.utcoffset().total_seconds() == 0
    assert {d["documento_id"] for d in v.documentos} == {str(reporte.id), str(memoria.id)}
    assert {e["identidad"]: e["estado"] for e in v.estados} == {"204:2016": "OK", "302:2016": "BAJA_SUSTANCIA", "305:2016": "SUB_REPORTADO"}


async def test_no_se_valida_dos_veces_la_misma_version(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a, _ = await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "OK"})
        error = comprobar_error(await validar(c, m5.empresa, a), 409, "ANALYSIS_ALREADY_VALIDATED")
    assert error["details"] == {"numero": 1}
    assert len(await sql(m5, "SELECT 1 FROM analisis_gri_validaciones")) == 1


async def test_validar_con_datos_viejos_devuelve_409_y_no_valida(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        await asignar(c, m5.empresa, "204:2016", "OK")
        await asignar(c, m5.empresa, "305:2016", "OK")
        consultado = await consultar(c, m5.empresa)  # el primer Administrador consulta...
        await asignar(c2, m5.empresa, "305:2016", "SUB_REPORTADO", version_fila=1)  # ...y otro cambia un estado
        error = comprobar_error(await validar(c, m5.empresa, consultado), 409, "ANALYSIS_VERSION_CONFLICT")
        assert error["details"]["version_esperada"] == 2
        assert error["details"]["version_actual"] == 3
        assert error["details"]["documentos_cambiaron"] is False
        # Con datos frescos sí procede.
        assert (await validar(c, m5.empresa, await consultar(c, m5.empresa))).status_code == 201


async def test_un_documento_incorporado_tras_consultar_impide_validar_los_datos_viejos(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await asignar(c, m5.empresa, "204:2016", "OK")
        await asignar(c, m5.empresa, "305:2016", "OK")
        consultado = await consultar(c, m5.empresa)
        await con_documentos(m5, (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))
        error = comprobar_error(await validar(c, m5.empresa, consultado), 409, "ANALYSIS_VERSION_CONFLICT")
        assert error["details"]["documentos_cambiaron"] is True
    assert await sql(m5, "SELECT 1 FROM analisis_gri_validaciones") == []


async def test_un_nuevo_documento_elegible_invalida_la_validacion_y_conserva_asignaciones_e_historial(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "BAJA_SUSTANCIA"})
        await con_documentos(m5, (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))  # otro documento elegible de la misma empresa/año
        a = await consultar(c, m5.empresa)
        assert a["estado_flujo"] == "PENDIENTE_ESTADOS"
        assert a["habilita_reporte"] is False
        assert a["validacion"]["vigente"] is False
        assert a["validacion"]["motivos_no_vigente"] == ["DOCUMENTOS_CAMBIARON"]
        assert fila_de(a, "204:2016")["estado"] == "OK"  # se conservan
        assert fila_de(a, "305:2016")["estado"] == "BAJA_SUSTANCIA"
        assert fila_de(a, "302:2016")["estado"] is None  # la fila nueva comienza sin estado
        assert a["puntaje"]["estado"] == "PENDIENTE_ESTADOS"
        assert a["puntaje"]["valor"] is None
        assert a["pendientes"][0]["identidades"] == ["302:2016"]
        # Al completar la fila nueva hay que volver a validar: la validación 1 no vuelve a ser vigente.
        await asignar(c, m5.empresa, "302:2016", "OK")
        a = await consultar(c, m5.empresa)
        assert a["estado_flujo"] == "LISTO_PARA_VALIDAR"
        assert a["pendientes"][0]["codigo"] == "VALIDACION_NO_VIGENTE"
        r = await validar(c, m5.empresa, a)
        assert r.status_code == 201
        assert r.json()["validacion"]["numero"] == 2
    assert len(await sql(m5, "SELECT 1 FROM analisis_gri_historial")) == 3  # el historial no se tocó
    assert len(await sql(m5, "SELECT 1 FROM analisis_gri_validaciones")) == 2  # la validación anterior se conserva


async def test_cualquier_cambio_de_estado_invalida_la_validacion_incluso_si_se_revierte(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "OK"})
        r = await asignar(c, m5.empresa, "204:2016", "SUB_REPORTADO", version_fila=1)
        assert r.json()["validacion_previa_invalidada"] is True
        a = await consultar(c, m5.empresa)
        assert a["estado_flujo"] == "LISTO_PARA_VALIDAR"
        assert a["validacion"]["vigente"] is False
        assert a["validacion"]["motivos_no_vigente"] == ["ESTADOS_CAMBIARON"]
        assert a["puntaje"]["estado"] == "CALCULADO_SIN_VALIDAR"
        assert a["puntaje"]["definitivo"] is False
        assert a["puntaje"]["valor"] == 50.0
        # Volver al estado original deja los estados idénticos, pero no revive la validación anterior.
        r = await asignar(c, m5.empresa, "204:2016", "OK", version_fila=2)
        assert r.json()["validacion_previa_invalidada"] is False
        a = await consultar(c, m5.empresa)
        assert a["validacion"]["vigente"] is False
        assert a["habilita_reporte"] is False
        assert (await validar(c, m5.empresa, a)).status_code == 201


async def test_un_documento_que_deja_de_ser_elegible_invalida_la_validacion(m5):
    reporte, _ = await con_documentos(m5, (MD_GRI_204_305, REPORTE), (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await completar_y_validar(c, m5, {"204:2016": "OK", "302:2016": "OK", "305:2016": "OK"})
        async with m5.fabrica() as db:  # la empresa se desactiva: sus documentos dejan de ser elegibles
            await db.execute(text("UPDATE empresas SET activa = false WHERE id = :i"), {"i": m5.empresa.id})
            await db.commit()
        a = await consultar(c, m5.empresa)
    assert a["estado_flujo"] == "DOCUMENTACION_INSUFICIENTE"
    assert a["habilita_reporte"] is False
    assert a["filas"] == []
    assert [p["codigo"] for p in a["pendientes"]] == ["EMPRESA_INACTIVA", "DOCUMENTACION_INSUFICIENTE"]
    assert a["validacion"]["vigente"] is False
    assert a["empresa"]["activa"] is False


# ============================================ Sin códigos GRI, observados y sanciones ============================================

async def test_solo_sanciones_muestra_las_sanciones_pero_no_habilita_el_reporte(m5):
    (doc,) = await con_documentos(m5, (MD_SOLO_SANCIONES, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
        assert a["estado_flujo"] == "SIN_CODIGOS_GRI"
        assert a["habilita_reporte"] is False
        assert a["filas"] == []
        assert a["puntaje"]["estado"] == "NO_DISPONIBLE"
        assert a["puntaje"]["valor"] is None
        assert "no disponible" in a["puntaje"]["mensaje"]
        assert [s["documento_id"] for s in a["sanciones"]] == [str(doc.id)] * 2
        assert a["resumen_sanciones"]["total"] == 2
        assert a["pendientes"][0]["codigo"] == "SIN_CODIGOS_GRI"
        comprobar_error(await validar(c, m5.empresa, a), 409, "ANALYSIS_NO_GRI_CODES")
    assert await sql(m5, "SELECT 1 FROM analisis_gri_validaciones") == []


async def test_sanciones_con_evidencia_monto_ausente_no_cuantificado_y_sin_sumas(m5):
    await con_documentos(m5, (MD_SOLO_SANCIONES, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
    con_monto = next(s for s in a["sanciones"] if s["monto"] is not None)
    sin_monto = next(s for s in a["sanciones"] if s["monto"] is None)
    assert con_monto["cuantificada"] is True
    assert con_monto["monto"]["texto"] == "S/ 120,000"
    assert con_monto["monto"]["separador_ambiguo"] is True
    assert con_monto["entidad"]["texto"] == "OSINERGMIN"
    assert sin_monto["cuantificada"] is False  # nunca cero
    assert sin_monto["monto"] is None
    assert {"cita", "cita_inicio", "cita_fin", "seccion", "documento_nombre", "motivos_revision"} <= set(sin_monto)
    assert a["resumen_sanciones"] == {"total": 2, "cuantificadas": 1, "no_cuantificadas": 1, "por_unidad": {"S/": 1}}
    assert a["puntaje"]["valor"] is None  # las sanciones no forman parte del promedio ESG


async def test_un_observado_no_habilita_por_si_solo_pero_tampoco_bloquea_otro_documento(m5):
    observado, = await con_documentos(m5, (MD_OBSERVADO, TipoDocumento.MEMORIA_ANUAL))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
        assert a["estado_flujo"] == "SIN_CODIGOS_GRI"
        assert a["habilita_reporte"] is False
        assert [(d["documento_id"], d["observado"], d["resultado_analisis"]) for d in a["documentos"]] == [(str(observado.id), True, "OBSERVADO")]
        comprobar_error(await validar(c, m5.empresa, a), 409, "ANALYSIS_NO_GRI_CODES")
        await con_documentos(m5, (MD_GRI_204_305, REPORTE))  # otro documento elegible de la misma empresa/año con GRI
        _, validada = await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "BAJA_SUSTANCIA"})
        assert validada["habilita_reporte"] is True
        assert validada["puntaje"]["valor"] == 75.0
        a = await consultar(c, m5.empresa)
    assert {d["observado"] for d in a["documentos"]} == {True, False}


async def test_referencias_desconocidas_son_advertencias_y_no_puntuan(m5):
    await con_documentos(m5, ("# X\n\nSegún GRI 999 y GRI 204-1.\n", REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
        assert [f["identidad"] for f in a["filas"]] == ["204:2016"]
        (adv,) = [x for x in a["advertencias"] if x["codigo"] == "GRI_REFERENCIA_DESCONOCIDA"]
        assert adv["detalles"]["codigo_estandar"] == "999"
        assert adv["evidencias"][0]["cita"]
        assert (await asignar(c, m5.empresa, "999:2016", "OK")).status_code == 404  # no es evaluable
        _, v = await completar_y_validar(c, m5, {"204:2016": "OK"})
    assert v["puntaje"]["valor"] == 100.0
    assert v["puntaje"]["filas_contadas"] == 1


async def test_todas_sub_reportadas_validan_con_cero_y_no_es_no_disponible(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        _, v = await completar_y_validar(c, m5, {"204:2016": "SUB_REPORTADO", "305:2016": "SUB_REPORTADO"})
    assert v["puntaje"]["valor"] == 0.0
    assert v["puntaje"]["valor_presentacion"] == 0.0
    assert v["puntaje"]["estado"] == "VALIDADO"
    assert v["puntaje"]["definitivo"] is True
    assert v["puntaje"]["suma_puntos"] == 0


# ============================================ Elegibilidad y aislamiento ============================================

async def test_solo_los_documentos_completados_con_analisis_y_publicacion_confirmada_son_elegibles(m5):
    async with m5.fabrica() as db:
        sin_publicar = await sembrar_documento(db, m5.empresa, m5.admin, texto=MD_GRI_204_305, tipo=REPORTE, publicado=False)
        await sembrar_documento(db, m5.empresa, m5.admin, estado=EstadoProcesamiento.EN_PROCESO, anio=2024)
        await sembrar_documento(db, m5.empresa, m5.admin, estado=EstadoProcesamiento.FALLIDO, anio=2023)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        for anio in (2025, 2024, 2023):
            a = await consultar(c, m5.empresa, anio)
            assert a["documentos"] == []
            assert a["filas"] == []
            assert a["estado_flujo"] == "DOCUMENTACION_INSUFICIENTE"
        # La publicación confirmada lo vuelve elegible.
        async with m5.fabrica() as db:
            await publicar(db, sin_publicar)
        a = await consultar(c, m5.empresa)
        assert [d["documento_id"] for d in a["documentos"]] == [str(sin_publicar.id)]
        assert len(a["filas"]) == 2


async def test_aislamiento_por_ambiente_empresa_y_anio(m5):
    async with m5.fabrica() as db:
        otra = await crear_empresa(db, "Otra Minera")
        await sembrar_documento(db, m5.empresa, m5.admin, texto=MD_GRI_204_305, ambiente="qa")
        await sembrar_documento(db, otra, m5.admin, texto=MD_GRI_204_Y_302)
        await sembrar_documento(db, m5.empresa, m5.admin, texto=MD_GRI_204_Y_302, anio=2024)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)  # development, 2025, empresa de prueba: nada propio
        assert a["documentos"] == []
        assert a["filas"] == []
        assert [f["identidad"] for f in (await consultar(c, otra))["filas"]] == ["204:2016", "302:2016"]
        assert [f["identidad"] for f in (await consultar(c, m5.empresa, 2024))["filas"]] == ["204:2016", "302:2016"]
        assert (await asignar(c, otra, "204:2016", "OK")).status_code == 200
        # El estado de otra empresa/año/ambiente no se filtra: cada análisis tiene su versión y sus estados.
        assert (await consultar(c, m5.empresa, 2024))["version"] == 0
        assert fila_de(await consultar(c, m5.empresa, 2024), "204:2016")["estado"] is None
    async with cliente_http(construir_app(m5.fabrica, m5.admin, ambiente="qa")) as cqa:
        a = await consultar(cqa, m5.empresa)
        assert a["ambiente"] == "qa"
        assert [f["identidad"] for f in a["filas"]] == ["204:2016", "305:2016"]


async def test_el_ambiente_sale_de_app_env_y_uno_invalido_es_503(m5, monkeypatch):
    async with m5.fabrica() as db:
        await sembrar_documento(db, m5.empresa, m5.admin, texto=MD_GRI_204_305, ambiente="uat")
    app = construir_app(m5.fabrica, m5.admin, ambiente=None)  # dependencia REAL de ambiente
    async with cliente_http(app) as c:
        monkeypatch.setattr(settings, "APP_ENV", "uat")
        assert len((await consultar(c, m5.empresa))["filas"]) == 2
        monkeypatch.setattr(settings, "APP_ENV", "development")
        assert (await consultar(c, m5.empresa))["filas"] == []
        monkeypatch.setattr(settings, "APP_ENV", "produccion")
        comprobar_error(await c.get(ruta(m5.empresa)), 503, "ANALYSIS_ENVIRONMENT_INVALID")


# ============================================ Atomicidad y concurrencia ============================================

async def test_si_falla_el_historial_no_queda_ni_el_estado_ni_la_version(m5, monkeypatch):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))

    async def falla(*args, **kwargs):
        raise RuntimeError("fallo simulado al insertar el historial")

    monkeypatch.setattr(servicio, "_registrar_historial", falla)
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        r = await asignar(c, m5.empresa, "204:2016", "OK")
        assert r.status_code == 500
        a = await consultar(c, m5.empresa)
    assert fila_de(a, "204:2016")["estado"] is None
    assert a["version"] == 0
    for tabla in ("analisis_gri_estados", "analisis_gri_historial", "analisis_gri_control"):
        assert await sql(m5, f"SELECT 1 FROM {tabla}") == [], tabla


async def test_si_falla_la_validacion_al_confirmar_no_queda_nada(m5, monkeypatch):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await asignar(c, m5.empresa, "204:2016", "OK")
        await asignar(c, m5.empresa, "305:2016", "OK")
        a = await consultar(c, m5.empresa)
        monkeypatch.setattr(servicio, "_ahora_utc", lambda: (_ for _ in ()).throw(RuntimeError("fallo")))
        assert (await validar(c, m5.empresa, a)).status_code == 500
        monkeypatch.undo()
        assert (await consultar(c, m5.empresa))["validacion"] is None
    assert await sql(m5, "SELECT 1 FROM analisis_gri_validaciones") == []


async def test_dos_administradores_asignando_la_misma_fila_a_la_vez_solo_uno_gana(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        # La fila de control ya existe (si no, el propio INSERT la serializaría y la prueba no ejercitaría el bloqueo).
        assert (await asignar(c1, m5.empresa, "305:2016", "OK")).status_code == 200
        respuestas = await asyncio.gather(
            *[asignar(c, m5.empresa, "204:2016", estado) for c, estado in ((c1, "OK"), (c2, "SUB_REPORTADO")) for _ in range(3)]
        )
        codigos = sorted(r.status_code for r in respuestas)
        assert codigos == [200, 409, 409, 409, 409, 409]  # una sola asignación inicial; el resto, conflicto explícito
        for r in respuestas:
            if r.status_code == 409:
                assert r.json()["error"]["code"] == "ANALYSIS_ROW_VERSION_CONFLICT"
        a = await consultar(c1, m5.empresa)
    assert a["version"] == 2
    assert fila_de(a, "204:2016")["version_fila"] == 1
    assert len(await sql(m5, "SELECT 1 FROM analisis_gri_historial WHERE identidad = '204:2016'")) == 1  # sin sobrescritura silenciosa


async def test_asignaciones_concurrentes_a_filas_distintas_se_serializan_sin_perder_versiones(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE), (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        assert (await asignar(c1, m5.empresa, "204:2016", "OK")).status_code == 200  # crea la fila de control
        respuestas = await asyncio.gather(
            asignar(c2, m5.empresa, "302:2016", "OK"), asignar(c1, m5.empresa, "305:2016", "BAJA_SUSTANCIA"),
            asignar(c2, m5.empresa, "204:2016", "SUB_REPORTADO", version_fila=1),
        )
        assert [r.status_code for r in respuestas] == [200, 200, 200]
        assert sorted(r.json()["version_analisis"] for r in respuestas) == [2, 3, 4]  # una versión por cambio, sin duplicados
        assert (await consultar(c1, m5.empresa))["version"] == 4
    versiones = await sql(m5, "SELECT version_analisis FROM analisis_gri_historial ORDER BY version_analisis")
    assert [v[0] for v in versiones] == [1, 2, 3, 4]


async def test_validar_y_cambiar_un_estado_a_la_vez_nunca_validan_datos_obsoletos(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        await asignar(c1, m5.empresa, "204:2016", "OK")
        await asignar(c1, m5.empresa, "305:2016", "OK")
        consultado = await consultar(c1, m5.empresa)
        r_validar, r_cambio = await asyncio.gather(
            validar(c1, m5.empresa, consultado), asignar(c2, m5.empresa, "305:2016", "SUB_REPORTADO", version_fila=1))
        assert r_cambio.status_code == 200
        a = await consultar(c1, m5.empresa)
        if r_validar.status_code == 201:
            # Ganó la validación: se hizo sobre la versión consultada y el cambio posterior la dejó sin vigencia.
            assert a["validacion"]["version_estados"] == consultado["version"]
            assert a["validacion"]["vigente"] is False
        else:
            comprobar_error(r_validar, 409, "ANALYSIS_VERSION_CONFLICT")
            assert a["validacion"] is None
    assert a["habilita_reporte"] is False


# ============================================ Solo inserción ============================================

async def test_el_historial_y_las_validaciones_son_de_solo_insercion(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "OK"})
    conexion = await asyncpg.connect(m5.url.replace("+asyncpg", "", 1))
    try:
        for sentencia in (
            "UPDATE analisis_gri_historial SET estado_nuevo = 'SUB_REPORTADO'",
            "DELETE FROM analisis_gri_historial",
            "UPDATE analisis_gri_validaciones SET puntaje_suma = 0",
            "DELETE FROM analisis_gri_validaciones",
        ):
            with pytest.raises(asyncpg.IntegrityConstraintViolationError, match="solo inserción"):
                await conexion.execute(sentencia)
        assert await conexion.fetchval("SELECT count(*) FROM analisis_gri_historial") == 2
        assert await conexion.fetchval("SELECT count(*) FROM analisis_gri_validaciones") == 1
        # La BD también rechaza lo imposible: un cuarto estado y un puntaje fuera de rango.
        with pytest.raises(asyncpg.CheckViolationError):
            await conexion.execute("UPDATE analisis_gri_estados SET estado = 'CRITICO'")
    finally:
        await conexion.close()


# ============================================ Funcionamiento sin ingesta ni proveedores ============================================

async def test_el_modulo_funciona_sin_gestor_de_ingesta_y_sin_llamar_a_proveedores(m5, monkeypatch):
    import socket

    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    app = construir_app(m5.fabrica, m5.admin)
    assert app.state.ingesta is None  # sin OCI, MinIO ni gestor
    conexiones_externas = []
    real = socket.socket.connect

    def vigilar(self, direccion, *args, **kwargs):
        host = direccion[0] if isinstance(direccion, tuple) else direccion
        if host not in ("127.0.0.1", "localhost", "::1"):
            conexiones_externas.append(direccion)
        return real(self, direccion, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", vigilar)
    async with cliente_http(app) as c:
        await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "OK"})
        assert (await c.get(ruta(m5.empresa, sufijo="/historial"))).status_code == 200
    assert conexiones_externas == []  # solo se habló con la base local aislada


async def test_el_esquema_no_preparado_responde_503_despues_de_autenticar(m5):
    error = ("ANALYSIS_SCHEMA_NOT_READY", "El análisis GRI no está disponible.", {"base": "transaccional", "motivo": "MIGRATION_FAILED"})
    async with cliente_http(construir_app(m5.fabrica, m5.admin, analisis_error=error)) as c, \
            cliente_http(construir_app(m5.fabrica, m5.superadmin, analisis_error=error)) as cs, \
            cliente_http(construir_app(m5.fabrica, None, analisis_error=error)) as sin:
        e = comprobar_error(await c.get(ruta(m5.empresa)), 503, "ANALYSIS_SCHEMA_NOT_READY")
        assert e["details"]["motivo"] == "MIGRATION_FAILED"
        comprobar_error(await cs.get(ruta(m5.empresa)), 403, "FORBIDDEN")  # 403 y 401 tienen prioridad
        comprobar_error(await sin.get(ruta(m5.empresa)), 401, "INVALID_SESSION")


# ============================================ Función reutilizable para el Módulo 7 ============================================

async def test_obtener_analisis_validado_exige_validacion_vigente_en_la_base(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    argumentos = dict(ambiente="development", empresa_id=m5.empresa.id, anio=2025)

    async def pedir():
        async with m5.fabrica() as db:
            return await servicio.obtener_analisis_validado(db, **argumentos)

    with pytest.raises(ConflictError) as error:
        await pedir()
    assert error.value.code == "ANALYSIS_NOT_VALIDATED"
    assert error.value.details["estado_flujo"] == "PENDIENTE_ESTADOS"
    assert error.value.details["pendientes"] == ["ESTADOS_PENDIENTES"]

    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await completar_y_validar(c, m5, {"204:2016": "OK", "305:2016": "BAJA_SUSTANCIA"})
        validado = await pedir()
        assert validado.habilita_reporte is True
        assert validado.estado_flujo.value == "VALIDADO"
        assert validado.puntaje.definitivo
        assert validado.puntaje.valor == 75.0
        assert validado.puntaje.estado.value == "VALIDADO"
        assert validado.validacion.vigente
        assert validado.validacion.validado_por.nombre == "Ana Admin"
        assert [(f.identidad, f.estado) for f in validado.filas] == [("204:2016", EstadoGri.OK), ("305:2016", EstadoGri.BAJA_SUSTANCIA)]
        assert all(f.evidencias for f in validado.filas)
        assert len(validado.huella_documentos) == 64
        assert validado.version == 2 == validado.validacion.version_estados
        # Un cambio posterior deja de entregarlo: no basta nada que venga del frontend.
        await asignar(c, m5.empresa, "305:2016", "OK", version_fila=1)
        with pytest.raises(ConflictError) as error:
            await pedir()
    assert error.value.details["estado_flujo"] == "LISTO_PARA_VALIDAR"


async def test_obtener_analisis_validado_rechaza_solo_sanciones_observados_y_empresa_inexistente(m5):
    async with m5.fabrica() as db:
        otra = await crear_empresa(db, "Solo sanciones")
        await sembrar_documento(db, otra, m5.admin, texto=MD_SOLO_SANCIONES)
        obs = await crear_empresa(db, "Solo observado")
        await sembrar_documento(db, obs, m5.admin, texto=MD_OBSERVADO)
    for empresa, flujo in ((otra, "SIN_CODIGOS_GRI"), (obs, "SIN_CODIGOS_GRI"), (m5.empresa, "DOCUMENTACION_INSUFICIENTE")):
        async with m5.fabrica() as db:
            with pytest.raises(ConflictError) as error:
                await servicio.obtener_analisis_validado(db, ambiente="development", empresa_id=empresa.id, anio=2025)
        assert error.value.details["estado_flujo"] == flujo
    async with m5.fabrica() as db:
        with pytest.raises(NotFoundError):
            await servicio.obtener_analisis_validado(db, ambiente="development", empresa_id=uuid.uuid4(), anio=2025)


# ============================================ Orden del flujo de validación (Swagger) ============================================

async def foto_de_tablas(m5) -> dict:
    """Contenido completo de las tablas del módulo: para comprobar que un 409 no modifica NADA."""
    return {
        t: [tuple(f) for f in await sql(m5, f"SELECT * FROM {t} ORDER BY 1")]
        for t in ("analisis_gri_control", "analisis_gri_estados", "analisis_gri_historial", "analisis_gri_validaciones")
    }


async def test_el_flujo_completo_consultar_asignar_reconsultar_y_validar_con_la_version_actual(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE), (MD_GRI_204_Y_302, TipoDocumento.MEMORIA_ANUAL))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        inicial = await consultar(c, m5.empresa)  # 1. consultar
        assert inicial["version"] == 0
        assert inicial["estado_flujo"] == "PENDIENTE_ESTADOS"
        versiones = []
        for identidad, estado in (("204:2016", "OK"), ("302:2016", "BAJA_SUSTANCIA"), ("305:2016", "SUB_REPORTADO")):  # 2. asignar
            r = await asignar(c, m5.empresa, identidad, estado)
            assert r.status_code == 200
            versiones.append(r.json()["version_analisis"])
        assert versiones == [1, 2, 3]  # asignar SÍ incrementa la versión del análisis, una vez por cambio
        actual = await consultar(c, m5.empresa)  # 3. consultar de nuevo
        assert actual["version"] == 3  # la huella no cambió
        assert actual["huella_documentos"] == inicial["huella_documentos"]
        assert actual["estado_flujo"] == "LISTO_PARA_VALIDAR"
        assert actual["habilita_reporte"] is False
        assert actual["puntaje"]["estado"] == "CALCULADO_SIN_VALIDAR"
        assert actual["puntaje"]["definitivo"] is False
        # 4. validar con la versión y la huella ACTUALES
        r = await validar(c, m5.empresa, actual)
        assert r.status_code == 201
        assert r.json()["validacion"]["version_estados"] == 3
        final = await consultar(c, m5.empresa)
    # 5. VALIDADO y puntaje correcto: (100 + 50 + 0) / 3 = 50
    assert final["estado_flujo"] == "VALIDADO"
    assert final["habilita_reporte"] is True
    assert final["puntaje"]["estado"] == "VALIDADO"
    assert final["puntaje"]["definitivo"] is True
    assert (final["puntaje"]["valor"], final["puntaje"]["valor_presentacion"], final["puntaje"]["suma_puntos"]) == (50.0, 50.0, 150)
    assert final["version"] == 3  # validar no cambia la versión de los estados


async def test_validar_con_la_version_consultada_antes_de_asignar_es_409_y_no_modifica_nada(m5):
    """El error típico en Swagger: anotar `version` en la primera consulta y validar después de asignar."""
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        antigua = await consultar(c, m5.empresa)  # version 0
        await asignar(c, m5.empresa, "204:2016", "OK")
        await asignar(c, m5.empresa, "305:2016", "OK")
        antes = await foto_de_tablas(m5)
        error = comprobar_error(await validar(c, m5.empresa, antigua), 409, "ANALYSIS_VERSION_CONFLICT")
        assert error["details"]["version_esperada"] == 0
        assert error["details"]["version_actual"] == 2
        assert "consulte de nuevo" in error["message"]
        assert await foto_de_tablas(m5) == antes  # el 409 no escribió ni una fila ni cambió la versión
        # Tampoco sirve una versión inventada o futura, ni una huella ajena.
        for cuerpo in ({"version": 3, "huella_documentos": antigua["huella_documentos"]},
                       {"version": 2, "huella_documentos": "0" * 64}):
            comprobar_error(await c.post(ruta(m5.empresa, sufijo="/validacion"), json=cuerpo), 409, "ANALYSIS_VERSION_CONFLICT")
        assert await foto_de_tablas(m5) == antes
        # La consulta actualizada sí valida.
        assert (await validar(c, m5.empresa, await consultar(c, m5.empresa))).status_code == 201


async def test_un_put_con_version_de_fila_vieja_es_409_y_no_modifica_nada(m5):
    await con_documentos(m5, (MD_GRI_204_305, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        await asignar(c, m5.empresa, "204:2016", "OK")
        antes = await foto_de_tablas(m5)
        comprobar_error(await asignar(c, m5.empresa, "204:2016", "SUB_REPORTADO", version_fila=0), 409, "ANALYSIS_ROW_VERSION_CONFLICT")
        assert await foto_de_tablas(m5) == antes


async def test_veinte_menciones_de_un_codigo_cuentan_una_sola_vez_en_el_puntaje_por_http(m5):
    texto = "# Informe\n\n" + "".join(f"Párrafo {n}: cumplimos GRI 204-1 en el periodo.\n\n" for n in range(20)) + "Además GRI 302-1.\n"
    await con_documentos(m5, (texto, REPORTE))
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        a = await consultar(c, m5.empresa)
        assert [f["identidad"] for f in a["filas"]] == ["204:2016", "302:2016"]
        assert fila_de(a, "204:2016")["total_menciones"] == 20 == len(fila_de(a, "204:2016")["evidencias"])
        await asignar(c, m5.empresa, "204:2016", "OK")
        await asignar(c, m5.empresa, "302:2016", "SUB_REPORTADO")
        a = await consultar(c, m5.empresa)
    # Dos identidades: (100 + 0) / 2 = 50. Si las 21 menciones puntuaran por separado el resultado sería distinto.
    assert (a["puntaje"]["filas_contadas"], a["puntaje"]["suma_puntos"], a["puntaje"]["valor"]) == (2, 100, 50.0)

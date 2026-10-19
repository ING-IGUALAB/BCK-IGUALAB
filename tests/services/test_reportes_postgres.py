"""Reportes de prospección (Módulo 7) — inmutabilidad (LTX:RN-041, CU006) sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

REAL: la restricción única `uq_reporte_empresa_anio`, el disparador de solo inserción (migración 0005 real), las transacciones y la
carrera entre dos generaciones, la auditoría, los roles por HTTP y el análisis validado (migración 0004 real y el servicio del
Módulo 5). CONTROLADO: el motor HTML→PDF (un doble que cuenta sus llamadas y devuelve bytes distintos en cada una, para detectar
cualquier recompilación); no se prueba WeasyPrint aquí.
Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import ConflictError
from app.core.request_id import RequestIDMiddleware
from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.application import servicio
from app.modules.reportes import router as reportes
from app.modules.reportes.service import generar_reporte
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    cliente_http,
    comprobar_error,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
)

RUTA = "/reportes"
COLUMNAS = "id, empresa_id, anio, generado_por, pdf, sha256, snapshot::text AS snapshot, creado_en"


class CompiladorControlado:
    """Devuelve un PDF distinto en cada llamada: si el servicio recompilara, los bytes cambiarían."""

    def __init__(self):
        self.llamadas = 0

    def __call__(self, html: str) -> bytes:
        self.llamadas += 1
        return f"%PDF-1.4 compilacion-{self.llamadas}".encode()


@pytest_asyncio.fixture
async def m7(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        sin_validar = await crear_empresa(db, "Minera Sin Validar")
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305)
        await sembrar_documento(db, sin_validar, admin, texto=MD_GRI_204_305)
    entorno = SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, otro=otro, superadmin=superadmin, empresa=empresa, sin_validar=sin_validar,
        compilador=CompiladorControlado(), url=url_pg_aislado,
    )
    await validar_analisis(entorno, empresa, admin)
    return entorno


async def validar_analisis(m7, empresa, actor, anio: int = 2025) -> None:
    """Asigna un estado a cada fila y valida, con el servicio real del Módulo 5 (validación humana previa al reporte)."""
    kwargs = dict(ambiente="development", empresa_id=empresa.id, anio=anio)
    async with m7.fabrica() as db:
        analisis = await servicio.consultar_analisis(db, **kwargs)
    for fila in analisis.filas:
        async with m7.fabrica() as db:
            await servicio.asignar_estado(
                db, identidad=fila.identidad, estado=EstadoGri.OK, version_fila_esperada=0, actor=actor, **kwargs
            )
    async with m7.fabrica() as db:
        actual = await servicio.consultar_analisis(db, **kwargs)  # versión vigente DESPUÉS de asignar
    async with m7.fabrica() as db:
        await servicio.validar_analisis(
            db, version_esperada=actual.version, huella_esperada=actual.huella_documentos, actor=actor, **kwargs
        )


def construir_app(m7, usuario) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(reportes.router)

    async def db_de_prueba():
        async with m7.fabrica() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    app.dependency_overrides[reportes.obtener_ambiente] = lambda: "development"
    app.dependency_overrides[reportes.obtener_compilador_pdf] = lambda: m7.compilador
    if usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    return app


async def sql(m7, consulta: str, **parametros):
    async with m7.fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


async def reportes_guardados(m7):
    return [tuple(f) for f in await sql(m7, f"SELECT {COLUMNAS} FROM reportes_prospeccion ORDER BY creado_en")]


async def eventos(m7, tipo: str):
    return await sql(m7, "SELECT usuario_id, resultado FROM auditoria WHERE tipo_evento = :t", t=tipo)


async def generar(m7, usuario=None, empresa=None, anio: int = 2025):
    async with cliente_http(construir_app(m7, usuario or m7.admin)) as c:
        return await c.post(RUTA, json={"empresa_id": str((empresa or m7.empresa).id), "anio": anio})


# ================================================ Roles ================================================

async def test_sin_sesion_es_401_y_el_superadmin_403_en_todos_los_endpoints(m7):
    destinos = [("POST", RUTA, {"empresa_id": str(m7.empresa.id), "anio": 2025}), ("GET", RUTA, None),
                ("POST", RUTA + "/vista-previa", {"empresa_id": str(m7.empresa.id), "anio": 2025}),
                ("GET", f"{RUTA}/{m7.empresa.id}/archivo", None)]
    async with cliente_http(construir_app(m7, None)) as sin_sesion, cliente_http(construir_app(m7, m7.superadmin)) as super_:
        for metodo, url, cuerpo in destinos:
            comprobar_error(await sin_sesion.request(metodo, url, json=cuerpo), 401, "INVALID_SESSION")
            comprobar_error(await super_.request(metodo, url, json=cuerpo), 403, "FORBIDDEN")
    assert await reportes_guardados(m7) == []
    assert m7.compilador.llamadas == 0


# ================================================ Generación e inmutabilidad ================================================

async def test_primera_generacion_crea_el_reporte_con_su_snapshot_su_hash_y_su_evento(m7):
    r = await generar(m7)
    assert r.status_code == 201, r.text
    (fila,) = await reportes_guardados(m7)
    pdf = fila[4]
    assert r.json()["id"] == str(fila[0])
    assert r.json()["sha256"] == fila[5] == hashlib.sha256(pdf).hexdigest()
    assert fila[3] == m7.admin.id
    assert '"estado_flujo": "VALIDADO"' in fila[6]
    assert [(e[0], e[1]) for e in await eventos(m7, "GENERACION_REPORTE")] == [(m7.admin.id, "EXITOSO")]


async def test_la_segunda_generacion_se_rechaza_con_409_y_deja_intacto_el_reporte_anterior(m7):
    assert (await generar(m7)).status_code == 201
    antes = await reportes_guardados(m7)
    compilaciones = m7.compilador.llamadas

    for usuario in (m7.admin, m7.otro):  # el mismo Administrador y otro distinto
        r = await generar(m7, usuario)
        error = comprobar_error(r, 409, "REPORT_ALREADY_EXISTS")
        assert error["details"]["reporte_id"] == str(antes[0][0])
        assert error["details"]["anio"] == 2025

    assert await reportes_guardados(m7) == antes  # ID, PDF, SHA-256, snapshot, autor y fecha idénticos
    assert m7.compilador.llamadas == compilaciones  # ni siquiera se recompiló
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 1  # el rechazo no registra una generación exitosa


async def test_otro_anio_u_otra_empresa_no_estan_bloqueados_por_el_reporte_existente(m7):
    assert (await generar(m7)).status_code == 201
    # otra empresa: su análisis no está validado → conflicto de negocio distinto (no confundirlo con el de reporte existente)
    comprobar_error(await generar(m7, empresa=m7.sin_validar), 409, "ANALYSIS_NOT_VALIDATED")
    assert len(await reportes_guardados(m7)) == 1


async def test_sin_analisis_validado_no_se_crea_nada_ni_se_registra_evento(m7):
    comprobar_error(await generar(m7, empresa=m7.sin_validar), 409, "ANALYSIS_NOT_VALIDATED")
    assert await reportes_guardados(m7) == []
    assert await eventos(m7, "GENERACION_REPORTE") == []


async def test_dos_generaciones_simultaneas_dejan_un_solo_reporte_y_una_es_conflicto_de_negocio(m7, monkeypatch):
    from app.modules.reportes import service as servicio_reportes

    # Barrera DESPUÉS de la comprobación previa: ambas solicitudes la superan (no hay reporte aún) y recién entonces avanzan juntas
    # hacia la inserción, de modo que la carrera es determinista y la decide la restricción de la base, no la comprobación previa.
    barrera = asyncio.Barrier(2)
    original = servicio_reportes.obtener_analisis_validado

    async def con_barrera(*args, **kwargs):
        await barrera.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(servicio_reportes, "obtener_analisis_validado", con_barrera)

    async def intento(usuario):
        async with m7.fabrica() as db:
            try:
                return await generar_reporte(
                    db, ambiente="development", empresa_id=m7.empresa.id, anio=2025, usuario_id=usuario.id, compilar_pdf=m7.compilador
                )
            except ConflictError as exc:
                return exc

    resultados = await asyncio.gather(intento(m7.admin), intento(m7.otro))
    exitos = [r for r in resultados if not isinstance(r, ConflictError)]
    conflictos = [r for r in resultados if isinstance(r, ConflictError)]
    assert m7.compilador.llamadas == 2, "las dos solicitudes debían pasar la comprobación previa para probar la carrera"
    assert len(exitos) == 1
    assert len(conflictos) == 1
    assert conflictos[0].code == "REPORT_ALREADY_EXISTS"
    assert conflictos[0].details["reporte_id"] == str(exitos[0].id)
    (fila,) = await reportes_guardados(m7)
    assert fila[0] == exitos[0].id
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 1  # el evento de la solicitud perdedora se deshizo con su transacción


# ================================================ Consulta, descarga y vista previa ================================================

async def test_la_descarga_entrega_el_pdf_almacenado_sin_recompilar(m7):
    creado = (await generar(m7)).json()
    compilaciones = m7.compilador.llamadas
    async with cliente_http(construir_app(m7, m7.otro)) as c:
        r = await c.get(f"{RUTA}/{creado['id']}/archivo")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == (await reportes_guardados(m7))[0][4]
    assert hashlib.sha256(r.content).hexdigest() == creado["sha256"]
    assert m7.compilador.llamadas == compilaciones
    assert [e[1] for e in await eventos(m7, "DESCARGA")] == ["EXITOSO"]


async def test_tras_un_rechazo_el_reporte_sigue_consultable_y_descargable(m7):
    creado = (await generar(m7)).json()
    comprobar_error(await generar(m7), 409, "REPORT_ALREADY_EXISTS")
    async with cliente_http(construir_app(m7, m7.admin)) as c:
        listado = (await c.get(RUTA)).json()
        descarga = await c.get(f"{RUTA}/{creado['id']}/archivo")
    assert [x["id"] for x in listado] == [creado["id"]]
    assert listado[0]["sha256"] == creado["sha256"]
    assert listado[0]["empresa_nombre"] == "Minera Andina"
    assert listado[0]["generado_por_nombre"] == "Ana Admin"
    assert descarga.status_code == 200
    assert hashlib.sha256(descarga.content).hexdigest() == creado["sha256"]


async def test_la_vista_previa_no_crea_ni_modifica_ni_sustituye_el_reporte(m7):
    cuerpo = {"empresa_id": str(m7.empresa.id), "anio": 2025}
    async with cliente_http(construir_app(m7, m7.admin)) as c:
        previa = await c.post(RUTA + "/vista-previa", json=cuerpo)
        assert previa.status_code == 200  # sin reporte: no guarda nada
        assert await reportes_guardados(m7) == []
        assert (await c.post(RUTA, json=cuerpo)).status_code == 201
        antes = await reportes_guardados(m7)
        otra = await c.post(RUTA + "/vista-previa", json=cuerpo)
    assert otra.status_code == 200
    assert otra.content.startswith(b"%PDF")
    assert await reportes_guardados(m7) == antes  # con reporte existente: sigue idéntico
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 1


# ================================================ Garantías de la base de datos ================================================

async def test_la_base_impide_un_segundo_reporte_de_la_misma_empresa_y_anio(m7):
    assert (await generar(m7)).status_code == 201
    insertar = text(
        "INSERT INTO reportes_prospeccion (id, empresa_id, anio, generado_por, pdf, sha256, snapshot)"
        " VALUES (gen_random_uuid(), :e, 2025, :u, :pdf, :h, '{}'::jsonb)"
    )
    async with m7.fabrica() as db:
        with pytest.raises(DBAPIError) as e:
            await db.execute(insertar, {"e": m7.empresa.id, "u": m7.admin.id, "pdf": b"otro", "h": "f" * 64})
        assert "uq_reporte_empresa_anio" in str(e.value.orig)


@pytest.mark.parametrize("sentencia", [
    "UPDATE reportes_prospeccion SET pdf = '\\x00'::bytea",
    "UPDATE reportes_prospeccion SET generado_por = generado_por",
    "DELETE FROM reportes_prospeccion",
])
async def test_la_base_rechaza_modificar_o_eliminar_un_reporte(m7, sentencia):
    assert (await generar(m7)).status_code == 201
    antes = await reportes_guardados(m7)
    async with m7.fabrica() as db:
        with pytest.raises(DBAPIError) as e:
            await db.execute(text(sentencia))
        assert "solo inserción" in str(e.value.orig)
    assert await reportes_guardados(m7) == antes

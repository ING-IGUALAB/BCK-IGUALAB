"""Una empresa del sector «Otros» (`OTROS`) recorre el análisis GRI, la validación, la disponibilidad, la emisión y la descarga de
reportes con las MISMAS reglas que una de otro sector, sobre un PostgreSQL REAL, AISLADO y TEMPORAL con migraciones reales. El motor
HTML→PDF es el doble `CompiladorRegistro` (sin WeasyPrint). Cada prueba compara a la empresa `OTROS` con una de CONTROL (minería) que
tiene el mismo documento: no hay excepciones ni ramas para `OTROS`. Sin PostgreSQL se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio

from app.models import SectorEmpresa
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
)
from tests.services.test_reportes_versionado_postgres import (
    OK,
    RUTA,
    SUB,
    CompiladorRegistro,
    analisis,
    emitir,
    get,
    params,
    revisar_y_validar,
)

ANIO = 2025


@pytest_asyncio.fixture
async def ent(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        otros = await crear_empresa(db, "Servicios Varios", sector=SectorEmpresa.OTROS)
        control = await crear_empresa(db, "Minera Control", sector=SectorEmpresa.MINERIA)
        await sembrar_documento(db, otros, admin, texto=MD_GRI_204_305, anio=ANIO)
        await sembrar_documento(db, control, admin, texto=MD_GRI_204_305, anio=ANIO)
    return SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, otro=admin, superadmin=superadmin, empresa=otros, control=control, compilador=CompiladorRegistro()
    )


async def disponibilidad(ent, empresa) -> dict:
    r = await get(ent, f"{RUTA}/disponibilidad", empresa_id=str(empresa.id), anio=ANIO)
    assert r.status_code == 200, r.text
    return r.json()["anios"][0]


def sin_identidad(fila: dict) -> dict:
    """La fila de disponibilidad sin los datos propios de cada empresa."""
    return {k: v for k, v in fila.items() if k not in ("reporte_id", "reporte_creado_en")}


async def test_otros_no_queda_excluido_del_analisis_y_produce_las_mismas_filas_que_otro_sector(ent):
    con_otros = await analisis(ent, ent.empresa)
    de_control = await analisis(ent, ent.control)

    assert con_otros.empresa.sector is SectorEmpresa.OTROS
    assert de_control.empresa.sector is SectorEmpresa.MINERIA
    assert len(con_otros.filas) > 0
    assert [f.identidad for f in con_otros.filas] == [f.identidad for f in de_control.filas]
    assert [f.total_menciones for f in con_otros.filas] == [f.total_menciones for f in de_control.filas]


async def test_otros_llega_a_la_validacion_la_emision_y_la_descarga_como_cualquier_otro_sector(ent):
    for empresa in (ent.empresa, ent.control):
        await revisar_y_validar(ent, [OK, SUB], empresa=empresa)

    emitido_otros = await emitir(ent, empresa=ent.empresa)
    emitido_control = await emitir(ent, empresa=ent.control)

    assert emitido_otros.status_code == 201, emitido_otros.text
    assert emitido_control.status_code == 201, emitido_control.text
    assert emitido_otros.json()["empresa_id"] == str(ent.empresa.id)
    assert emitido_otros.json()["version"] == emitido_control.json()["version"] == 1
    assert emitido_otros.json()["validacion_numero"] == emitido_control.json()["validacion_numero"] == 1
    puntaje_otros, puntaje_control = (await analisis(ent, ent.empresa)).puntaje, (await analisis(ent, ent.control)).puntaje
    assert puntaje_otros.valor == puntaje_control.valor  # el puntaje ESG sale de las mismas reglas
    assert "Sector: OTROS" in ent.compilador.htmls[0]  # el reporte muestra el identificador, como con los demás sectores
    assert "Sector: MINERIA" in ent.compilador.htmls[1]

    vigente = await get(ent, f"{RUTA}/vigente", **params(ent.empresa, ANIO))
    descarga = await get(ent, f"{RUTA}/vigente/archivo", **params(ent.empresa, ANIO))
    assert vigente.status_code == 200, vigente.text
    assert vigente.json()["id"] == emitido_otros.json()["id"]
    assert descarga.status_code == 200
    assert len(descarga.content) > 0

    otros, control = await disponibilidad(ent, ent.empresa), await disponibilidad(ent, ent.control)
    assert otros["reporte_actualizado"] is True
    assert otros["puede_generar"] is control["puede_generar"]
    assert otros["estado"] == control["estado"]


async def test_las_reglas_generales_se_aplican_igual_a_otros_sin_validacion_no_se_emite(ent):
    otros, control = await disponibilidad(ent, ent.empresa), await disponibilidad(ent, ent.control)
    rechazo_otros = await emitir(ent, empresa=ent.empresa)
    rechazo_control = await emitir(ent, empresa=ent.control)

    assert sin_identidad(otros) == sin_identidad(control)
    assert otros["documentos_elegibles"] == 1  # el documento de la empresa OTROS es elegible, no se excluye por su sector
    assert rechazo_otros.status_code != 201
    assert rechazo_otros.status_code == rechazo_control.status_code
    assert rechazo_otros.json()["error"]["code"] == rechazo_control.json()["error"]["code"]
    assert ent.compilador.htmls == []  # nada se compiló ni se guardó

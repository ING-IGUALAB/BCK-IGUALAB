"""Sector «Otros» (`OTROS`) sobre un PostgreSQL REAL, AISLADO y TEMPORAL (clúster de `conftest.py`; sin bases compartidas). Sin
PostgreSQL estas pruebas se OMITEN y no cuentan como ejecutadas.

Cubre la migración 0008 (`ALTER TYPE sector_empresa ADD VALUE`) sobre una base desplegada ANTES de ella, con empresas y documentos
que deben conservarse; la instalación nueva (donde `create_all` ya trae el valor); y el uso de `OTROS` en el servicio de empresas
y en la reserva de documentos de la ingesta (que copia el sector a `documentos.sector`, mismo tipo enumerado). No llama a ningún
proveedor externo.
"""
from __future__ import annotations

import dataclasses
import uuid

import asyncpg
import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessValidationError, ConflictError
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.compatibilidad import CODIGO, elementos_faltantes, verificar_esquema_central
from app.migraciones.motor import ErrorMigracion, migrar
from app.models import Empresa, SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import Documento
from app.modules.empresas import service as empresa_service
from app.modules.ingesta.application import documento_service as servicio
from app.schemas import CrearEmpresaRequest, EditarEmpresaRequest
from tests.ayudantes_ingesta import crear_empresa, crear_usuario, metadatos, sha256_de
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar, url_de_base
from tests.services.test_documento_ddl_postgres import _crear_base_con_prerequisitos

FALTA_OTROS = "valor_de_tipo:sector_empresa.OTROS"
ETIQUETAS_ANTERIORES = ["MINERIA", "PETROLEO", "ENERGIA"]
# Estado de un ambiente desplegado antes de la 0008: el tipo solo tiene los tres valores originales. Solo se usa en la base
# desechable de la prueba, antes de que existan las tablas de ingesta.
SQL_TIPO_ANTERIOR = """
ALTER TABLE empresas ALTER COLUMN sector TYPE text;
DROP TYPE sector_empresa;
CREATE TYPE sector_empresa AS ENUM ('MINERIA', 'PETROLEO', 'ENERGIA');
ALTER TABLE empresas ALTER COLUMN sector TYPE sector_empresa USING sector::sector_empresa;
"""
SQL_EMPRESAS = "SELECT id, nombre, sector::text AS sector, activa, creada_en FROM empresas ORDER BY nombre"
SQL_DOCUMENTOS = "SELECT id, empresa_id, sector::text AS sector, sha256, clave_original FROM documentos ORDER BY id"
SQL_ETIQUETAS = (
    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'sector_empresa' ORDER BY e.enumsortorder"
)
SQL_VALOR_PREDETERMINADO = (
    "SELECT column_default FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'empresas' AND column_name = 'sector'"
)


def esquema_hasta(version: int):
    return dataclasses.replace(ESQUEMA_TRANSACCIONAL, migraciones=ESQUEMA_TRANSACCIONAL.migraciones[:version])


async def faltantes(url: str) -> list[str]:
    conexion = await conectar(url)
    try:
        return await elementos_faltantes(conexion)
    finally:
        await conexion.close()


async def etiquetas(url: str) -> list[str]:
    return [f["enumlabel"] for f in await consultar(url, SQL_ETIQUETAS)]


async def foto(url: str) -> tuple[list[tuple], list[tuple]]:
    empresas = [tuple(f) for f in await consultar(url, SQL_EMPRESAS)]
    documentos = [tuple(f) for f in await consultar(url, SQL_DOCUMENTOS)]
    return empresas, documentos


async def base_desplegada_antes_de_la_0008(url_pg_aislado: str):
    """Base con las migraciones 0001–0007, el tipo con tres valores, ocho empresas de minería, una petrolera y dos documentos."""
    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    url = url_de_base(url_pg_aislado, esquema.base)
    await ejecutar(url, SQL_TIPO_ANTERIOR)
    await migrar(url, esquema_hasta(7))
    petrolera = uuid.uuid4()
    conexion = await conectar(url)
    try:
        await conexion.execute("INSERT INTO empresas (id, nombre, sector, activa) VALUES ($1, 'Petrolera Previa', 'PETROLEO', true)", petrolera)
        await esquema.insertar(conexion, sector="MINERIA")
        await esquema.insertar(conexion, empresa_id=petrolera, sector="PETROLEO")
    finally:
        await conexion.close()
    return esquema, url


async def usar_otros(esquema, url: str) -> uuid.UUID:
    """Una empresa y un documento con el sector nuevo, con SQL directo (el tipo es el de la base y no el del ORM)."""
    empresa = uuid.uuid4()
    conexion = await conectar(url)
    try:
        await conexion.execute("INSERT INTO empresas (id, nombre, sector, activa) VALUES ($1, 'Empresa Otros', 'OTROS', true)", empresa)
        await esquema.insertar(conexion, empresa_id=empresa, sector="OTROS")
    finally:
        await conexion.close()
    return empresa


# ============================================ Migración 0008 ============================================

async def test_la_0008_sobre_el_esquema_anterior_rechaza_otros_hasta_aplicarse(url_pg_aislado):
    _, url = await base_desplegada_antes_de_la_0008(url_pg_aislado)
    assert await etiquetas(url) == ETIQUETAS_ANTERIORES
    assert await faltantes(url) == [FALTA_OTROS]
    with pytest.raises(ErrorMigracion) as capturado:
        await verificar_esquema_central(url)  # el arranque NO debe anunciar disponibilidad con el tipo anterior
    assert capturado.value.codigo == CODIGO
    assert capturado.value.detalles["faltan"] == [FALTA_OTROS]
    identificador = uuid.uuid4()
    with pytest.raises(asyncpg.exceptions.InvalidTextRepresentationError):
        await ejecutar(url, "INSERT INTO empresas (id, nombre, sector) VALUES ($1, 'Sin migrar', 'OTROS')", identificador)


async def test_la_0008_agrega_otros_conserva_los_datos_y_el_arranque_posterior_funciona(url_pg_aislado):
    esquema, url = await base_desplegada_antes_de_la_0008(url_pg_aislado)
    antes = await foto(url)
    assert len(antes[0]) == 9
    assert len(antes[1]) == 2

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)  # el arranque de la versión nueva

    assert (resultado.aplicadas, resultado.adoptadas, resultado.previas) == ((8, 9, 10, 11), (), (1, 2, 3, 4, 5, 6, 7))  # la 8 es la de «Otros»
    assert await faltantes(url) == []
    await verificar_esquema_central(url)
    assert await foto(url) == antes  # empresas y documentos idénticos: nadie se renombra ni se reclasifica
    assert await etiquetas(url) == [*ETIQUETAS_ANTERIORES, "OTROS"]  # los valores anteriores intactos y el nuevo al final
    assert [f["column_default"] for f in await consultar(url, SQL_VALOR_PREDETERMINADO)] == [None]  # «Otros» no es predeterminado

    otros = await usar_otros(esquema, url)
    filas = await consultar(url, "SELECT e.sector::text AS empresa, d.sector::text AS documento FROM empresas e JOIN documentos d ON d.empresa_id = e.id WHERE e.id = $1", otros)
    assert [(f["empresa"], f["documento"]) for f in filas] == [("OTROS", "OTROS")]

    siguiente = await migrar(url, ESQUEMA_TRANSACCIONAL)  # y el arranque siguiente no ejecuta nada
    assert (siguiente.sin_cambios, siguiente.previas) == (True, tuple(range(1, 12)))
    await verificar_esquema_central(url)


async def test_una_instalacion_nueva_ya_trae_otros_y_la_0008_es_idempotente(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)  # create_all con los modelos actuales, como el arranque
    assert await etiquetas(url) == [*ETIQUETAS_ANTERIORES, "OTROS"]
    assert FALTA_OTROS not in await faltantes(url)

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)  # `ADD VALUE IF NOT EXISTS`: el valor ya existe y no falla

    assert resultado.aplicadas == tuple(range(1, 12))
    assert await faltantes(url) == []
    assert await etiquetas(url) == [*ETIQUETAS_ANTERIORES, "OTROS"]


# ============================================ Uso de «Otros» en empresas e ingesta ============================================

async def test_crear_editar_consultar_y_filtrar_empresas_de_sector_otros(fabrica_pg):
    async with fabrica_pg() as db:
        creada = await empresa_service.crear_empresa(db, CrearEmpresaRequest(nombre="Empresa Otros", sector=SectorEmpresa.OTROS))
        minera = await empresa_service.crear_empresa(db, CrearEmpresaRequest(nombre="Minera Previa", sector=SectorEmpresa.MINERIA))
    async with fabrica_pg() as db:
        editada = await empresa_service.editar_empresa(db, minera.id, EditarEmpresaRequest(sector=SectorEmpresa.OTROS))
    async with fabrica_pg() as db:
        guardada = await empresa_service.obtener_empresa(db, creada.id)
        con_otros = await empresa_service.listar_empresas(db, sector=SectorEmpresa.OTROS)
        con_mineria = await empresa_service.listar_empresas(db, sector=SectorEmpresa.MINERIA)

    assert creada.sector is SectorEmpresa.OTROS
    assert editada.sector is SectorEmpresa.OTROS
    assert guardada.sector is SectorEmpresa.OTROS
    assert [e.id for e in con_otros] == [creada.id, minera.id]
    assert con_mineria == []


async def reservar(fabrica, usuario, empresa, numero, sector_declarado=None):
    datos = f"# Documento {numero}\n".encode()
    async with fabrica() as db:
        return await servicio.reservar_documento(
            db,
            metadatos=metadatos(empresa, 2025, TipoDocumento.MEMORIA_ANUAL, sector=sector_declarado),
            nombre_archivo="Memoria anual 2025.md",
            sha256=sha256_de(datos),
            tamano_bytes=len(datos),
            usuario_id=usuario.id,
            ambiente="development",
        )


async def test_la_ingesta_deriva_y_copia_el_sector_otros_de_la_empresa_y_rechaza_uno_distinto(fabrica_pg):
    async with fabrica_pg() as db:
        usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
        empresa = await crear_empresa(db, nombre="Empresa Otros", sector=SectorEmpresa.OTROS)

    sin_declarar = await reservar(fabrica_pg, usuario, empresa, 1)
    assert sin_declarar.sector is SectorEmpresa.OTROS  # el sector sale de la empresa, no del formulario

    with pytest.raises(BusinessValidationError) as capturado:
        await reservar(fabrica_pg, usuario, empresa, 2, sector_declarado=SectorEmpresa.MINERIA)
    assert capturado.value.code == "COMPANY_SECTOR_MISMATCH"

    async with fabrica_pg() as db:
        copiados = (await db.execute(select(Documento.sector).where(Documento.empresa_id == empresa.id))).scalars().all()
    assert list(copiados) == [SectorEmpresa.OTROS]  # una sola fila: el rechazado no creó documento


async def test_con_documentos_otros_sigue_la_regla_de_no_cambiar_de_sector(fabrica_pg):
    async with fabrica_pg() as db:
        usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
        empresa = await crear_empresa(db, nombre="Empresa Otros", sector=SectorEmpresa.OTROS)
    await reservar(fabrica_pg, usuario, empresa, 1)
    peticion = EditarEmpresaRequest(sector=SectorEmpresa.ENERGIA)
    mismo = EditarEmpresaRequest(sector=SectorEmpresa.OTROS)

    async with fabrica_pg() as db:
        with pytest.raises(ConflictError) as capturado:
            await empresa_service.editar_empresa(db, empresa.id, peticion)
    assert capturado.value.code == "COMPANY_SECTOR_CHANGE_BLOCKED"

    async with fabrica_pg() as db:
        sin_cambio = await empresa_service.editar_empresa(db, empresa.id, mismo)
        guardada = (await db.execute(select(Empresa.sector).where(Empresa.id == empresa.id))).scalar_one()
    assert sin_cambio.sector is SectorEmpresa.OTROS
    assert guardada is SectorEmpresa.OTROS

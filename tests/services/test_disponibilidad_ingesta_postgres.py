"""Duplicados por empresa/año/tipo y SHA-256 y consulta de disponibilidad de tipos, sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

`GET /documentos/disponibilidad` es INFORMATIVO: la garantía de no duplicar sigue en la reserva (`reservar_documento`) y en los
índices únicos parciales de la base. Aquí se comprueba (1) que esas garantías siguen firmes —mismo archivo con otro nombre, mismo
espacio con contenido distinto, cargas simultáneas— y (2) que lo que anuncia la consulta coincide con lo que la reserva real decide
y con los estados que ocupan o liberan un espacio. El almacén y los proveedores no intervienen. Sin PostgreSQL estas pruebas se
OMITEN (`pytest -rs`).
"""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, text

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import BusinessValidationError, ConflictError
from app.core.request_id import RequestIDMiddleware
from app.models import TipoDocumento
from app.models.documento_ingesta import EstadoProcesamiento
from app.modules.ingesta import router as ingesta
from app.modules.ingesta.application import documento_service as servicio
from app.modules.ingesta.application import operacion_service
from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from tests.ayudantes_analisis_gri import cliente_http, comprobar_error, crear_administrador, crear_empresa, sembrar_documento
from tests.ayudantes_ingesta import crear_usuario, metadatos, sha256_de

MEMORIA = TipoDocumento.MEMORIA_ANUAL
REPORTE = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
RUTA = "/documentos/disponibilidad"
ANALISIS_MINIMO = analizar_texto("# Informe\n\nTexto sin códigos ni sanciones reconocibles.\n").a_dict()  # OBSERVADO válido


@pytest_asyncio.fixture
async def mundo(fabrica_pg):
    async with fabrica_pg() as db:
        superadmin = await crear_usuario(db, correo="superadmin@pruebas.invalid")
        administrador = await crear_administrador(db, "Ana Admin")
        empresa = await crear_empresa(db, "Minera Andina")
        otra = await crear_empresa(db, "Petrolera Sur")
    return SimpleNamespace(fabrica=fabrica_pg, superadmin=superadmin, administrador=administrador, empresa=empresa, otra=otra)


def construir_app(mundo, usuario, ambiente: str = "development") -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(ingesta.router)

    async def db_de_prueba():
        async with mundo.fabrica() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    app.dependency_overrides[ingesta.obtener_gestor_ingesta] = lambda: SimpleNamespace(ambiente=ambiente)
    if usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    return app


async def consultar(mundo, empresa=None, anio: int = 2025, usuario=None, ambiente: str = "development"):
    async with cliente_http(construir_app(mundo, usuario or mundo.superadmin, ambiente)) as c:
        return await c.get(RUTA, params={"empresa_id": str((empresa or mundo.empresa).id), "anio": anio})


def estados(respuesta) -> dict[str, str]:
    assert respuesta.status_code == 200, respuesta.text
    return {t["tipo"]: t["estado"] for t in respuesta.json()["tipos"]}


async def reservar(mundo, datos: bytes, tipo=MEMORIA, empresa=None, anio: int = 2025, ambiente: str = "development", nombre="Memoria.md"):
    async with mundo.fabrica() as db:
        return await servicio.reservar_documento(
            db, metadatos=metadatos(empresa or mundo.empresa, anio, tipo), nombre_archivo=nombre, sha256=sha256_de(datos),
            tamano_bytes=len(datos), usuario_id=mundo.superadmin.id, ambiente=ambiente,
        )


async def sql(mundo, consulta: str, **parametros):
    async with mundo.fabrica() as db:
        return [tuple(f) for f in (await db.execute(text(consulta), parametros)).all()]


# ============================================ Disponibilidad ============================================

async def test_sin_documentos_todos_los_tipos_estan_disponibles(mundo):
    r = await consultar(mundo)
    assert r.status_code == 200
    cuerpo = r.json()
    assert {t["tipo"] for t in cuerpo["tipos"]} == {t.value for t in TipoDocumento}  # un elemento por cada tipo admitido
    assert all(t["estado"] == "DISPONIBLE" and t["cargable"] and t["documento_id"] is None for t in cuerpo["tipos"])
    assert (cuerpo["ambiente"], cuerpo["empresa_activa"], cuerpo["anio"]) == ("development", True, 2025)


@pytest.mark.parametrize(
    ("construccion", "esperado"),
    [
        pytest.param(dict(estado=EstadoProcesamiento.EN_PROCESO), "EN_PROCESO", id="en-proceso"),
        pytest.param(dict(estado=EstadoProcesamiento.COMPLETADO, publicado=False), "PUBLICACION_PENDIENTE", id="publicacion-pendiente"),
        pytest.param(dict(estado=EstadoProcesamiento.COMPLETADO, publicado=True), "OCUPADO", id="completado-publicado"),
    ],
)
async def test_los_estados_con_reserva_activa_ocupan_el_espacio(mundo, construccion, esperado):
    async with mundo.fabrica() as db:
        documento = await sembrar_documento(db, mundo.empresa, mundo.superadmin, analisis=ANALISIS_MINIMO, tipo=MEMORIA, **construccion)
    cuerpo = (await consultar(mundo)).json()
    ocupado = next(t for t in cuerpo["tipos"] if t["tipo"] == "MEMORIA_ANUAL")
    assert (ocupado["estado"], ocupado["cargable"], ocupado["documento_id"]) == (esperado, False, str(documento.id))
    assert ocupado["nombre_archivo"] == documento.nombre_archivo
    assert estados_resto(cuerpo) == {"DISPONIBLE"}  # los demás tipos siguen libres
    # Coherencia con la carga real: el espacio anunciado como ocupado se rechaza, con el código de conflicto existente.
    with pytest.raises(ConflictError) as conflicto:
        await reservar(mundo, b"# otro contenido\n", MEMORIA)
    assert conflicto.value.code.startswith("DOCUMENT_")


def estados_resto(cuerpo) -> set[str]:
    return {t["estado"] for t in cuerpo["tipos"] if t["tipo"] != "MEMORIA_ANUAL"}


async def test_un_fallido_con_la_limpieza_terminada_libera_el_espacio_y_uno_con_limpieza_pendiente_lo_retiene(mundo):
    async with mundo.fabrica() as db:
        liberado = await sembrar_documento(db, mundo.empresa, mundo.superadmin, estado=EstadoProcesamiento.FALLIDO, tipo=MEMORIA)
        retenido = await sembrar_documento(db, mundo.empresa, mundo.superadmin, estado=EstadoProcesamiento.FALLIDO, tipo=REPORTE)
        await db.execute(
            text("UPDATE documentos SET estado_compensacion = 'PENDIENTE', reserva_activa = true WHERE id = :i"), {"i": retenido.id}
        )
        await db.commit()
    assert (await sql(mundo, "SELECT reserva_activa FROM documentos WHERE id = :i", i=liberado.id)) == [(False,)]
    por_tipo = estados(await consultar(mundo))
    assert por_tipo["MEMORIA_ANUAL"] == "DISPONIBLE"  # no se declara ocupado un espacio que las reglas permiten reutilizar
    assert por_tipo["REPORTE_SOSTENIBILIDAD_GRI"] == "LIMPIEZA_PENDIENTE"
    reintento = await reservar(mundo, b"# reintento\n", MEMORIA)  # la carga real acepta lo anunciado como disponible
    assert reintento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    with pytest.raises(ConflictError) as bloqueado:
        await reservar(mundo, b"# otro\n", REPORTE)
    assert bloqueado.value.code == "DOCUMENT_CLEANUP_PENDING"


async def test_la_reserva_real_se_refleja_en_la_consulta(mundo):
    assert estados(await consultar(mundo))["MEMORIA_ANUAL"] == "DISPONIBLE"
    documento = await reservar(mundo, b"# memoria\n", MEMORIA)
    cuerpo = (await consultar(mundo)).json()
    ocupado = next(t for t in cuerpo["tipos"] if t["tipo"] == "MEMORIA_ANUAL")
    assert (ocupado["estado"], ocupado["documento_id"]) == ("EN_PROCESO", str(documento.id))
    assert estados_resto(cuerpo) == {"DISPONIBLE"}
    otro_tipo = await reservar(mundo, b"# reporte\n", REPORTE)  # un tipo distinto del mismo año sigue aceptándose
    assert otro_tipo.id != documento.id
    assert estados(await consultar(mundo))["REPORTE_SOSTENIBILIDAD_GRI"] == "EN_PROCESO"


# ============================================ Duplicados ============================================

async def test_el_mismo_archivo_con_otro_nombre_se_rechaza_por_sha256_aunque_la_consulta_lo_anuncie_libre(mundo):
    """El SHA-256 no puede anticiparse sin el archivo: la consulta dice DISPONIBLE (empresa/año/tipo libres) y la carga igual rechaza."""
    datos = b"# contenido repetido\n"
    await reservar(mundo, datos, MEMORIA, nombre="original.md")
    assert estados(await consultar(mundo))["REPORTE_SOSTENIBILIDAD_GRI"] == "DISPONIBLE"
    with pytest.raises(ConflictError) as duplicado:
        await reservar(mundo, datos, REPORTE, nombre="otro-nombre.md")
    assert duplicado.value.code == "DOCUMENT_UPLOAD_IN_PROGRESS"  # el original sigue EN_PROCESO; completado sería DOCUMENT_ALREADY_EXISTS
    assert len(await sql(mundo, "SELECT 1 FROM documentos WHERE reserva_activa")) == 1


async def test_el_sha256_ocupa_tambien_entre_empresas_y_anios(mundo):
    datos = b"# contenido compartido\n"
    await reservar(mundo, datos, MEMORIA)
    with pytest.raises(ConflictError):
        await reservar(mundo, datos, MEMORIA, empresa=mundo.otra, anio=2024)
    assert len(await sql(mundo, "SELECT 1 FROM documentos WHERE reserva_activa")) == 1


async def test_el_mismo_espacio_con_contenido_distinto_se_rechaza_por_empresa_anio_tipo(mundo):
    await reservar(mundo, b"# version A\n", MEMORIA)
    with pytest.raises(ConflictError) as conflicto:
        await reservar(mundo, b"# version B, distinta\n", MEMORIA)
    assert conflicto.value.code.startswith("DOCUMENT_")
    assert estados(await consultar(mundo))["MEMORIA_ANUAL"] == "EN_PROCESO"
    assert len(await sql(mundo, "SELECT 1 FROM documentos WHERE reserva_activa")) == 1


async def test_cargas_simultaneas_para_el_mismo_espacio_dejan_una_sola_reserva(mundo):
    resultados = await asyncio.gather(
        *[reservar(mundo, f"# carga {n}\n".encode(), MEMORIA) for n in range(8)], return_exceptions=True
    )
    ganadoras = [r for r in resultados if not isinstance(r, Exception)]
    assert len(ganadoras) == 1
    assert all(isinstance(r, ConflictError) for r in resultados if isinstance(r, Exception))  # conflicto informado, nunca otro error
    assert len(await sql(mundo, "SELECT 1 FROM documentos WHERE reserva_activa AND tipo = 'MEMORIA_ANUAL'")) == 1
    assert estados(await consultar(mundo))["MEMORIA_ANUAL"] == "EN_PROCESO"


async def test_la_base_rechaza_dos_reservas_activas_del_mismo_espacio_aunque_se_omita_la_consulta_previa(mundo):
    """La garantía no depende de la consulta previa: se inserta directamente y los índices únicos parciales deciden."""
    from sqlalchemy.exc import IntegrityError

    async with mundo.fabrica() as db:
        await sembrar_documento(db, mundo.empresa, mundo.superadmin, analisis=ANALISIS_MINIMO, tipo=MEMORIA)
    with pytest.raises(IntegrityError):
        async with mundo.fabrica() as db:
            await sembrar_documento(db, mundo.empresa, mundo.superadmin, analisis=ANALISIS_MINIMO, tipo=MEMORIA)


# ============================================ Ambiente, permisos y empresa ============================================

async def test_los_documentos_de_otro_ambiente_no_ocupan_el_espacio(mundo):
    async with mundo.fabrica() as db:
        await sembrar_documento(db, mundo.empresa, mundo.superadmin, analisis=ANALISIS_MINIMO, tipo=MEMORIA, ambiente="qa")
    assert estados(await consultar(mundo, ambiente="development"))["MEMORIA_ANUAL"] == "DISPONIBLE"
    assert estados(await consultar(mundo, ambiente="qa"))["MEMORIA_ANUAL"] == "OCUPADO"
    reservado = await reservar(mundo, b"# memoria development\n", MEMORIA, ambiente="development")  # la carga coincide
    assert reservado.ambiente == "development"


async def test_permisos_solo_superadmin_y_sin_sesion_401(mundo):
    parametros = {"empresa_id": str(mundo.empresa.id), "anio": 2025}
    async with cliente_http(construir_app(mundo, None)) as sin_sesion, \
            cliente_http(construir_app(mundo, mundo.administrador)) as como_administrador:
        comprobar_error(await sin_sesion.get(RUTA, params=parametros), 401, "INVALID_SESSION")
        comprobar_error(await como_administrador.get(RUTA, params=parametros), 403, "FORBIDDEN")


async def test_empresa_inexistente_inactiva_y_anio_invalido(mundo):
    import uuid

    comprobar_error(await consultar(mundo, empresa=SimpleNamespace(id=uuid.uuid4())), 404, "COMPANY_NOT_FOUND")
    inactiva = None
    async with mundo.fabrica() as db:
        inactiva = await crear_empresa(db, "Empresa Inactiva", activa=False)
    cuerpo = (await consultar(mundo, empresa=inactiva)).json()
    assert cuerpo["empresa_activa"] is False
    assert all(t["estado"] == "DISPONIBLE" and t["cargable"] is False for t in cuerpo["tipos"])  # libre, pero no cargable
    with pytest.raises(BusinessValidationError) as rechazo:  # la carga real lo confirma
        await reservar(mundo, b"# x\n", MEMORIA, empresa=inactiva)
    assert rechazo.value.code == "COMPANY_INACTIVE"
    for anio in (1999, 9999, "dos mil"):
        comprobar_error(await consultar(mundo, anio=anio), 422, "REQUEST_VALIDATION_ERROR")


# ============================================ Solo lectura y costo ============================================

async def test_la_consulta_es_de_solo_lectura_y_usa_pocas_consultas(mundo, fabrica_pg):
    async with mundo.fabrica() as db:
        await sembrar_documento(db, mundo.empresa, mundo.superadmin, analisis=ANALISIS_MINIMO, tipo=MEMORIA)
        await sembrar_documento(db, mundo.empresa, mundo.superadmin, analisis=ANALISIS_MINIMO, tipo=REPORTE)
    antes = await sql(mundo, "SELECT id, estado_procesamiento, reserva_activa, md5(analisis::text) FROM documentos ORDER BY id")
    sentencias: list[str] = []

    def contar(conexion, cursor, sentencia, *resto):
        sentencias.append(sentencia)

    motor = fabrica_pg.kw["bind"]
    event.listen(motor.sync_engine, "before_cursor_execute", contar)
    try:
        async with mundo.fabrica() as db:
            await operacion_service.disponibilidad_de_tipos(db, ambiente="development", empresa_id=mundo.empresa.id, anio=2025)
    finally:
        event.remove(motor.sync_engine, "before_cursor_execute", contar)
    lecturas = [s for s in sentencias if s.lstrip().upper().startswith("SELECT")]
    assert len(lecturas) == 2
    assert len(lecturas) == len(sentencias) - sum(s.strip().upper() in {"BEGIN", "ROLLBACK"} for s in sentencias)
    assert await sql(mundo, "SELECT id, estado_procesamiento, reserva_activa, md5(analisis::text) FROM documentos ORDER BY id") == antes

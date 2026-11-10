"""Deshabilitación de documentos (ACT-02) sobre un PostgreSQL REAL, AISLADO y TEMPORAL (el clúster `initdb` de `conftest.py`: 127.0.0.1,
sin `DATABASE_URL` ni bases compartidas; se destruye al terminar).

REAL: HTTP con roles y ambiente, la protección común del origen del análisis (`proteger_origen_de_analisis`: bloqueos de empresa y de la
fila de control), los índices únicos parciales de reserva (`uq_documentos_sha256_activo`, `uq_documentos_empresa_anio_tipo_activo`), las
restricciones CHECK, la auditoría, el Análisis GRI con su validación, la emisión de reportes y la recuperación de pendientes, y la
migración 0011 sobre una base con datos existentes. CONTROLADOS (dobles): el motor HTML→PDF (`CompiladorRegistro`), la publicación
vectorial de la recuperación y la búsqueda vectorial del chat (`AlmacenVectorialSimulado`, que emula el filtro previo a `ORDER BY`/`LIMIT`).

NO DEMUESTRA pgvector: el filtro SQL real de `buscar_similares` y el desplazamiento por similitud se prueban con pgvector en
`test_documento_deshabilitacion_pgvector.py` (necesita Docker). Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exception_handlers import register_exception_handlers
from app.core.exceptions import ConflictError
from app.core.request_id import RequestIDMiddleware
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.motor import migrar
from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.models.documento_ingesta import Documento, EstadoProcesamiento
from app.modules.analisis_gri.schemas import MotivoNoVigente
from app.modules.asistente import service as servicio_chat
from app.modules.asistente.domain.citas import MOTIVO_SIN_FRAGMENTOS
from app.modules.ingesta import router as ingesta
from app.modules.ingesta.application import documento_service as servicio
from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado
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
from tests.ayudantes_ingesta import metadatos, sha256_de
from tests.ayudantes_migraciones import url_de_base
from tests.services.test_asistente import afirmacion, respuesta
from tests.services.test_asistente_postgres import EmbeddingsControlado, LlmControlado
from tests.services.test_documento_coordinador_postgres import completado
from tests.services.test_documento_ddl_postgres import _crear_base_con_prerequisitos
from tests.services.test_reportes_versionado_postgres import (
    CompiladorRegistro,
    analisis,
    emitir,
    get,
    reportes,
    revisar_y_validar,
)

AMBIENTE = "development"
MEMORIA, REPORTE = TipoDocumento.MEMORIA_ANUAL, TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
RUTA = "/documentos"
MOTIVO = "Se cargó con el año equivocado."
OK, SUB = EstadoGri.OK, EstadoGri.SUB_REPORTADO
VECTOR_CONSULTA = [0.1] * 1536


@pytest_asyncio.fixture
async def m(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        otra = await crear_empresa(db, "Petrolera Sur")
        memoria = await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, tipo=MEMORIA, nombre="memoria-2025.md")
        reporte = await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, tipo=REPORTE, nombre="reporte-2025.md")
    return SimpleNamespace(
        fabrica=fabrica_pg, url=url_pg_aislado, admin=admin, otro=admin, superadmin=superadmin, empresa=empresa, otra=otra,
        memoria=memoria, reporte=reporte, compilador=CompiladorRegistro(),
    )


# ------------------------------------------------------------------ Ayudantes ------------------------------------------------------------------

def app_ingesta(m, usuario, ambiente: str = AMBIENTE) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(ingesta.router)

    async def db_de_prueba():
        async with m.fabrica() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    app.dependency_overrides[ingesta.obtener_gestor_ingesta] = lambda: SimpleNamespace(ambiente=ambiente)
    if usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    return app


async def deshabilitar_http(m, documento_id, cuerpo=None, usuario=None, ambiente: str = AMBIENTE):
    async with cliente_http(app_ingesta(m, m.superadmin if usuario is None else usuario, ambiente)) as c:
        return await c.post(f"{RUTA}/{documento_id}/deshabilitar", json={"motivo": MOTIVO} if cuerpo is None else cuerpo)


async def get_ingesta(m, ruta: str, usuario=None, ambiente: str = AMBIENTE, **params):
    async with cliente_http(app_ingesta(m, m.superadmin if usuario is None else usuario, ambiente)) as c:
        return await c.get(ruta, params=params)


async def tipos(m, empresa=None, anio: int = 2025, ambiente: str = AMBIENTE) -> dict[str, dict]:
    r = await get_ingesta(m, f"{RUTA}/disponibilidad", ambiente=ambiente, empresa_id=str((empresa or m.empresa).id), anio=anio)
    assert r.status_code == 200, r.text
    return {t["tipo"]: t for t in r.json()["tipos"]}


async def sql(m, consulta: str, **parametros):
    async with m.fabrica() as db:
        return [tuple(f) for f in (await db.execute(text(consulta), parametros)).all()]


async def fila(m, documento_id) -> dict:
    async with m.fabrica() as db:
        resultado = await db.execute(text("SELECT * FROM documentos WHERE id = :i"), {"i": documento_id})
        return dict(resultado.mappings().one())


async def auditoria_de_deshabilitacion(m) -> list[tuple]:
    return await sql(m, "SELECT usuario_id, resultado, detalle FROM auditoria WHERE detalle LIKE 'Documento deshabilitado%' ORDER BY fecha_hora_utc")


async def reservar(m, datos: bytes, tipo=MEMORIA, anio: int = 2025, empresa=None):
    async with m.fabrica() as db:
        return await servicio.reservar_documento(
            db, metadatos=metadatos(empresa or m.empresa, anio, tipo), nombre_archivo="Reemplazo.md", sha256=sha256_de(datos),
            tamano_bytes=len(datos), usuario_id=m.superadmin.id, ambiente=AMBIENTE,
        )


async def completar(m, documento) -> None:
    """Lleva una reserva EN_PROCESO a COMPLETADO y publicada (lo que hace el coordinador al terminar)."""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.modules.ingesta.application.analisis_ingesta import analizar_texto

    analisis_ = analizar_texto(MD_GRI_204_305).a_dict()
    ahora = datetime.now(timezone.utc)
    async with m.fabrica() as db:
        await db.execute(update(Documento).where(Documento.id == documento.id).values(
            estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=analisis_["resultado"], analisis=analisis_,
            completado_en=ahora, original_almacenado_en=ahora, vector_escritura_intentada_en=ahora, vector_publicado_en=ahora,
            etapa_actual="FINALIZADO",
        ))
        await db.commit()


# ============================================ Acceptance: memoria y reporte de la misma empresa y año ============================================

async def test_memoria_y_reporte_ocupan_sus_tipos_y_al_deshabilitar_la_memoria_solo_se_libera_su_cupo(m):
    antes = await tipos(m)
    assert [(antes[t.value]["estado"], antes[t.value]["cargable"]) for t in (MEMORIA, REPORTE)] == [("OCUPADO", False)] * 2

    r = await deshabilitar_http(m, m.memoria.id)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert (cuerpo["documento_id"], cuerpo["estado"], cuerpo["deshabilitado"], cuerpo["ya_estaba_deshabilitado"]) == (
        str(m.memoria.id), "COMPLETADO", True, False,
    )
    assert (cuerpo["motivo_deshabilitacion"], cuerpo["deshabilitado_por"]) == (MOTIVO, str(m.superadmin.id))  # el actor sale de la sesión
    assert (cuerpo["empresa_id"], cuerpo["anio"], cuerpo["tipo"]) == (str(m.empresa.id), 2025, "MEMORIA_ANUAL")

    despues = await tipos(m)
    assert (despues["MEMORIA_ANUAL"]["estado"], despues["MEMORIA_ANUAL"]["cargable"], despues["MEMORIA_ANUAL"]["documento_id"]) == (
        "DISPONIBLE", True, None,
    )
    reporte = despues["REPORTE_SOSTENIBILIDAD_GRI"]
    assert (reporte["estado"], reporte["cargable"], reporte["documento_id"]) == ("OCUPADO", False, str(m.reporte.id))
    # El retirado conserva todo lo demás: sigue COMPLETADO, publicado y con su original y su análisis.
    retirado, vigente = await fila(m, m.memoria.id), await fila(m, m.reporte.id)
    assert (retirado["reserva_activa"], retirado["estado_procesamiento"], retirado["deshabilitado_por"], retirado["motivo_deshabilitacion"]) == (
        False, "COMPLETADO", m.superadmin.id, MOTIVO,
    )
    assert None not in (retirado["deshabilitado_en"], retirado["vector_publicado_en"], retirado["original_almacenado_en"], retirado["analisis"])
    assert retirado["clave_original"] == m.memoria.clave_original
    assert (vigente["reserva_activa"], vigente["deshabilitado_en"], vigente["motivo_deshabilitacion"]) == (True, None, None)


async def test_se_audita_una_sola_vez_con_el_actor_y_el_listado_y_el_detalle_distinguen_al_retirado(m):
    await deshabilitar_http(m, m.memoria.id)

    ((usuario_id, resultado, detalle),) = await auditoria_de_deshabilitacion(m)
    assert (usuario_id, resultado) == (m.superadmin.id, "EXITOSO")
    assert f"archivo={m.memoria.nombre_archivo}" in detalle
    assert str(m.memoria.id) not in detalle  # sin UUID crudo
    assert MOTIVO in detalle

    lista = await get_ingesta(m, RUTA, empresa_id=str(m.empresa.id), anio=2025)
    assert lista.status_code == 200, lista.text
    por_id = {i["id"]: i for i in lista.json()["items"]}
    retirado, vigente = por_id[str(m.memoria.id)], por_id[str(m.reporte.id)]
    assert (retirado["estado"], retirado["deshabilitado"], retirado["disponible_para_rag"], retirado["motivo_deshabilitacion"]) == (
        "COMPLETADO", True, False, MOTIVO,
    )
    assert (retirado["deshabilitado_por"], retirado["deshabilitado_en"] is not None) == (str(m.superadmin.id), True)
    assert (vigente["estado"], vigente["deshabilitado"], vigente["disponible_para_rag"]) == ("COMPLETADO", False, True)
    assert (vigente["deshabilitado_en"], vigente["deshabilitado_por"], vigente["motivo_deshabilitacion"]) == (None, None, None)

    detalle_http = await get_ingesta(m, f"{RUTA}/{m.memoria.id}")
    assert detalle_http.status_code == 200
    assert (detalle_http.json()["deshabilitado"], detalle_http.json()["estado"], detalle_http.json()["analisis"] is not None) == (True, "COMPLETADO", True)


async def test_una_segunda_carga_simultanea_del_mismo_tipo_no_puede_ganar_la_misma_reserva(m):
    await deshabilitar_http(m, m.memoria.id)

    resultados = await asyncio.gather(
        *(reservar(m, f"# Memoria corregida, intento {n}\n".encode()) for n in range(5)), return_exceptions=True
    )

    ganadoras = [r for r in resultados if isinstance(r, Documento)]
    perdedoras = [r for r in resultados if isinstance(r, ConflictError)]
    assert (len(ganadoras), len(perdedoras)) == (1, 4), resultados
    assert {p.code for p in perdedoras} <= {"DOCUMENT_UPLOAD_IN_PROGRESS", "DOCUMENT_RESERVATION_CONFLICT"}
    activas = await sql(m, "SELECT id FROM documentos WHERE empresa_id = :e AND anio = 2025 AND tipo = 'MEMORIA_ANUAL' AND reserva_activa", e=m.empresa.id)
    assert activas == [(ganadoras[0].id,)]  # lo decide el índice único parcial, no una consulta previa
    cupo = (await tipos(m))["MEMORIA_ANUAL"]
    assert (cupo["estado"], cupo["cargable"], cupo["documento_id"]) == ("EN_PROCESO", False, str(ganadoras[0].id))  # el cupo vuelve a quedar ocupado
    assert (await tipos(m))["REPORTE_SOSTENIBILIDAD_GRI"]["estado"] == "OCUPADO"


async def test_dos_cargas_simultaneas_con_los_mismos_bytes_tampoco_ganan_las_dos(m):
    await deshabilitar_http(m, m.memoria.id)
    datos = b"# Mismo contenido en las dos cargas\n"

    resultados = await asyncio.gather(reservar(m, datos), reservar(m, datos), return_exceptions=True)

    assert sorted(type(r).__name__ for r in resultados) == ["ConflictError", "Documento"], resultados


# ============================================ Los mismos bytes bajo el índice de SHA-256 ============================================

async def test_se_puede_corregir_una_carga_equivocada_volviendo_a_subir_los_mismos_bytes(m):
    datos = b"# Memoria 2024 con el anio mal puesto\n"
    equivocado = await reservar(m, datos, anio=2024, empresa=m.otra)
    await completar(m, equivocado)
    with pytest.raises(ConflictError) as repetido:  # mientras está activo, el SHA-256 lo protege
        await reservar(m, datos, anio=2025, empresa=m.otra)
    assert repetido.value.code == "DOCUMENT_ALREADY_INGESTED"
    assert repetido.value.details["criterio"] == ["sha256"]

    assert (await deshabilitar_http(m, equivocado.id, {"motivo": "El año es 2025, no 2024."})).status_code == 200
    corregido = await reservar(m, datos, anio=2025, empresa=m.otra)

    assert corregido.id != equivocado.id
    assert corregido.sha256 == equivocado.sha256
    assert [(f[0], f[1]) for f in await sql(m, "SELECT id, reserva_activa FROM documentos WHERE sha256 = :s ORDER BY creado_en", s=sha256_de(datos))] == [
        (equivocado.id, False), (corregido.id, True),
    ]


async def test_los_mismos_bytes_siguen_bloqueados_si_otra_reserva_activa_los_tiene(m):
    datos = b"# Contenido que ya ocupa otro tipo\n"
    primero = await reservar(m, datos, anio=2024, empresa=m.otra)
    await completar(m, primero)
    assert (await deshabilitar_http(m, primero.id)).status_code == 200
    ocupa_otro_tipo = await reservar(m, datos, tipo=REPORTE, anio=2024, empresa=m.otra)  # liberado el SHA-256, otra reserva lo toma

    with pytest.raises(ConflictError) as capturado:
        await reservar(m, datos, tipo=MEMORIA, anio=2025, empresa=m.otra)

    assert capturado.value.details["criterio"] == ["sha256"]
    assert ocupa_otro_tipo.reserva_activa is True
    # y la base misma lo impone: dos reservas activas con el mismo SHA-256 en un ambiente no caben.
    with pytest.raises(asyncpg.UniqueViolationError, match="uq_documentos_sha256_activo"):
        await ejecutar_sql(m, "UPDATE documentos SET sha256 = (SELECT sha256 FROM documentos WHERE id = $1) WHERE id = $2", ocupa_otro_tipo.id, m.memoria.id)


async def ejecutar_sql(m, consulta: str, *argumentos):
    conexion = await asyncpg.connect(m.url.replace("+asyncpg", "", 1))
    try:
        return await conexion.execute(consulta, *argumentos)
    finally:
        await conexion.close()


# ============================================ Permisos, ambiente, motivo y estados ============================================

async def test_solo_el_superadmin_puede_deshabilitar(m):
    async with cliente_http(app_ingesta(m, None)) as c:  # sin override de la sesión: autenticación real
        sin_sesion = await c.post(f"{RUTA}/{m.memoria.id}/deshabilitar", json={"motivo": MOTIVO})
    como_administrador = await deshabilitar_http(m, m.memoria.id, usuario=m.admin)

    assert (sin_sesion.status_code, como_administrador.status_code) == (401, 403)
    assert (await fila(m, m.memoria.id))["deshabilitado_en"] is None
    assert await auditoria_de_deshabilitacion(m) == []


async def test_un_documento_de_otro_ambiente_es_inexistente_y_no_se_modifica(m):
    r = await deshabilitar_http(m, m.memoria.id, ambiente="qa")

    comprobar_error(r, 404, "DOCUMENT_NOT_FOUND")
    assert (await fila(m, m.memoria.id))["reserva_activa"] is True
    inexistente = await deshabilitar_http(m, "00000000-0000-4000-8000-000000000000")
    comprobar_error(inexistente, 404, "DOCUMENT_NOT_FOUND")


@pytest.mark.parametrize(
    "cuerpo",
    [
        pytest.param({}, id="sin-motivo"),
        pytest.param({"motivo": ""}, id="vacio"),
        pytest.param({"motivo": "  \t\n "}, id="solo-espacios"),
        pytest.param({"motivo": "x" * 501}, id="demasiado-largo"),
        pytest.param({"motivo": None}, id="nulo"),
        pytest.param({"motivo": MOTIVO, "usuario_id": "00000000-0000-4000-8000-000000000000"}, id="actor-en-el-payload"),
    ],
)
async def test_un_motivo_vacio_o_un_cuerpo_invalido_es_422_y_no_cambia_nada(m, cuerpo):
    r = await deshabilitar_http(m, m.memoria.id, cuerpo)

    comprobar_error(r, 422, "REQUEST_VALIDATION_ERROR")
    assert (await fila(m, m.memoria.id))["reserva_activa"] is True
    assert await auditoria_de_deshabilitacion(m) == []
    assert (await tipos(m))["MEMORIA_ANUAL"]["estado"] == "OCUPADO"


@pytest.mark.parametrize(
    ("construccion", "estado"),
    [
        pytest.param(dict(estado=EstadoProcesamiento.EN_PROCESO), "EN_PROCESO", id="en-proceso"),
        pytest.param(dict(estado=EstadoProcesamiento.FALLIDO), "FALLIDO", id="fallido"),
        pytest.param(dict(estado=EstadoProcesamiento.COMPLETADO, publicado=False), "PUBLICACION_PENDIENTE", id="publicacion-pendiente"),
    ],
)
async def test_una_carga_no_finalizada_no_se_deshabilita(m, construccion, estado):
    async with m.fabrica() as db:
        pendiente = await sembrar_documento(db, m.otra, m.admin, texto=MD_GRI_204_305, **construccion)

    r = await deshabilitar_http(m, pendiente.id)

    error = comprobar_error(r, 409, "DOCUMENT_NOT_DISABLEABLE")
    assert error["details"] == {"estado": estado}
    actual = await fila(m, pendiente.id)
    assert (actual["reserva_activa"], actual["estado_procesamiento"], actual["deshabilitado_en"]) == (
        pendiente.reserva_activa, pendiente.estado_procesamiento.value, None,
    )
    assert await auditoria_de_deshabilitacion(m) == []


async def test_repetir_la_deshabilitacion_devuelve_el_registro_original_sin_otro_evento(m):
    primera = await deshabilitar_http(m, m.memoria.id, {"motivo": "Motivo original"})
    antes = await fila(m, m.memoria.id)

    repetida = await deshabilitar_http(m, m.memoria.id, {"motivo": "Otro motivo distinto"})

    assert (primera.status_code, repetida.status_code) == (200, 200)
    assert (primera.json()["ya_estaba_deshabilitado"], repetida.json()["ya_estaba_deshabilitado"]) == (False, True)
    assert repetida.json()["motivo_deshabilitacion"] == "Motivo original"
    assert {k: v for k, v in primera.json().items() if k != "ya_estaba_deshabilitado"} == {
        k: v for k, v in repetida.json().items() if k != "ya_estaba_deshabilitado"
    }
    assert await fila(m, m.memoria.id) == antes  # nada cambió, ni siquiera el instante
    assert len(await auditoria_de_deshabilitacion(m)) == 1


async def test_dos_solicitudes_simultaneas_deshabilitan_una_sola_vez(m):
    a, b = await asyncio.gather(
        deshabilitar_http(m, m.memoria.id, {"motivo": "Motivo A"}), deshabilitar_http(m, m.memoria.id, {"motivo": "Motivo B"})
    )

    assert (a.status_code, b.status_code) == (200, 200)
    assert sorted((a.json()["ya_estaba_deshabilitado"], b.json()["ya_estaba_deshabilitado"])) == [False, True]
    ganador = a if not a.json()["ya_estaba_deshabilitado"] else b
    assert a.json()["motivo_deshabilitacion"] == b.json()["motivo_deshabilitacion"] == ganador.json()["motivo_deshabilitacion"]
    assert (await fila(m, m.memoria.id))["motivo_deshabilitacion"] == ganador.json()["motivo_deshabilitacion"]
    assert len(await auditoria_de_deshabilitacion(m)) == 1


# ============================================ Validación vigente y reportes ============================================

async def test_la_validacion_anterior_pierde_vigencia_y_el_pdf_ya_emitido_no_cambia(m):
    await revisar_y_validar(m, [OK, SUB])
    emitido = await emitir(m)
    assert emitido.status_code == 201, emitido.text
    antes = await analisis(m)
    assert antes.validacion.vigente is True
    assert {d.documento_id for d in antes.documentos} == {m.memoria.id, m.reporte.id}
    reportes_antes = await reportes(m)
    validaciones_antes = await sql(m, "SELECT * FROM analisis_gri_validaciones ORDER BY numero")
    pdf_antes = await get(m, f"/reportes/{emitido.json()['id']}/archivo")
    assert pdf_antes.status_code == 200

    assert (await deshabilitar_http(m, m.memoria.id)).status_code == 200

    despues = await analisis(m)
    assert [d.documento_id for d in despues.documentos] == [m.reporte.id]  # el retirado dejó de ser elegible
    assert despues.validacion.vigente is False
    assert despues.validacion.motivos_no_vigente == [MotivoNoVigente.DOCUMENTOS_CAMBIARON]
    assert despues.habilita_reporte is False
    assert despues.huella_documentos != antes.huella_documentos
    # No se emite un reporte nuevo con una validación sin vigencia, y no se toca el emitido.
    comprobar_error(await emitir(m), 409, "ANALYSIS_NOT_VALIDATED")
    assert m.compilador.llamadas == 1
    assert await reportes(m) == reportes_antes
    assert await sql(m, "SELECT * FROM analisis_gri_validaciones ORDER BY numero") == validaciones_antes  # la validación no se borra
    pdf_despues = await get(m, f"/reportes/{emitido.json()['id']}/archivo")
    assert (pdf_despues.status_code, pdf_despues.content) == (200, pdf_antes.content)


async def test_tras_deshabilitar_se_puede_validar_con_lo_que_queda_y_el_reporte_nuevo_no_usa_al_retirado(m):
    await revisar_y_validar(m, [OK, SUB])
    primero = await emitir(m)
    assert primero.status_code == 201, primero.text
    await deshabilitar_http(m, m.memoria.id)

    await revisar_y_validar(m, [OK, SUB])  # nueva validación sobre el conjunto elegible actual
    segundo = await emitir(m)

    assert segundo.status_code == 201, segundo.text
    assert (primero.json()["version"], segundo.json()["version"]) == (1, 2)
    instantaneas = dict(await sql(m, "SELECT version, snapshot::text FROM reportes_prospeccion ORDER BY version"))
    assert str(m.memoria.id) in instantaneas[1]  # el reporte histórico conserva su origen
    assert str(m.memoria.id) not in instantaneas[2]
    assert str(m.reporte.id) in instantaneas[2]


async def esperar_bloqueo(m, segundos: float = 10.0) -> None:
    """Espera a que alguna sesión del clúster aislado esté detenida en un bloqueo (comprobado en `pg_stat_activity`, no por tiempo)."""
    limite = asyncio.get_running_loop().time() + segundos
    while asyncio.get_running_loop().time() < limite:
        if (await sql(m, "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"))[0][0] >= 1:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("Ninguna sesión llegó a esperar un bloqueo.")


async def test_la_disponibilidad_de_reportes_deja_de_contar_a_los_retirados(m):
    await revisar_y_validar(m, [OK, SUB])
    assert (await emitir(m)).status_code == 201

    async def anio_2025() -> dict:
        r = await get(m, "/reportes/disponibilidad", empresa_id=str(m.empresa.id), anio=2025)
        assert r.status_code == 200, r.text
        return r.json()["anios"][0]

    antes = await anio_2025()
    assert (antes["documentos_elegibles"], antes["reporte_actualizado"]) == (2, True)

    await deshabilitar_http(m, m.memoria.id)
    uno = await anio_2025()
    assert (uno["documentos_elegibles"], uno["reporte_actualizado"], uno["puede_generar"]) == (1, False, False)  # validación sin vigencia
    assert uno["motivo_no_generar"] == "PENDIENTE_VALIDACION"

    await deshabilitar_http(m, m.reporte.id)
    ninguno = await anio_2025()
    # `documentos_elegibles = null` es el contrato existente de «el año ya no tiene documentos elegibles».
    assert (ninguno["documentos_elegibles"], ninguno["puede_generar"], ninguno["reporte_actualizado"]) == (None, False, False)
    assert ninguno["motivo_no_generar"] == "SIN_DOCUMENTACION_ELEGIBLE"
    assert ninguno["reporte_id"] == antes["reporte_id"]  # el reporte ya emitido sigue siendo el de ese año
    comprobar_error(await emitir(m), 409, "ANALYSIS_NOT_VALIDATED")


async def test_el_retiro_espera_a_una_validacion_en_curso_del_mismo_anio(m):
    """Orden de bloqueos empresa → control → documento: mientras otra transacción mantiene la protección común del origen, la
    deshabilitación espera (no se cuela) y, al soltarse, ve el resultado confirmado."""
    from app.modules.analisis_gri.application.coordinacion import proteger_origen_de_analisis

    async with m.fabrica() as bloqueadora:
        await proteger_origen_de_analisis(bloqueadora, ambiente=AMBIENTE, empresa_id=m.empresa.id, anio=2025)
        pendiente = asyncio.create_task(deshabilitar_http(m, m.memoria.id))
        await esperar_bloqueo(m)
        assert not pendiente.done()  # espera al bloqueo de la fila de control
        assert (await fila(m, m.memoria.id))["deshabilitado_en"] is None
        await bloqueadora.commit()
    respuesta_http = await asyncio.wait_for(pendiente, 15)

    assert respuesta_http.status_code == 200
    assert (await fila(m, m.memoria.id))["deshabilitado_en"] is not None


async def test_una_validacion_de_otro_anio_o_empresa_no_pierde_vigencia(m):
    async with m.fabrica() as db:
        await sembrar_documento(db, m.empresa, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, anio=2024)
        await sembrar_documento(db, m.otra, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, anio=2025)
    await revisar_y_validar(m, [OK, SUB], empresa=m.empresa, anio=2024)
    await revisar_y_validar(m, [OK, SUB], empresa=m.otra, anio=2025)

    await deshabilitar_http(m, m.memoria.id)  # empresa A, 2025

    assert (await analisis(m, m.empresa, 2024)).validacion.vigente is True
    assert (await analisis(m, m.otra, 2025)).validacion.vigente is True


# ============================================ Recuperación e inmutabilidad ============================================

class VectorialDoble:
    def in_transaction(self) -> bool:
        return False


async def test_ni_la_recuperacion_ni_un_reintento_vuelven_a_habilitar_al_retirado_ni_a_ocupar_su_cupo(m, monkeypatch):
    publicadas: list = []

    async def publicar_fragmentos(db, *, ambiente, documento_id):
        publicadas.append(documento_id)
        return 2

    async def contar_fragmentos(db, *, ambiente, documento_id):
        return SimpleNamespace(total=2, publicados=2)

    monkeypatch.setattr(vectores, "publicar_fragmentos", publicar_fragmentos)
    monkeypatch.setattr(vectores, "contar_fragmentos", contar_fragmentos)
    async with m.fabrica() as db:  # un COMPLETADO con la publicación pendiente: la tarea SÍ debe procesarlo
        pendiente = await sembrar_documento(db, m.otra, m.admin, texto=MD_GRI_204_305, publicado=False)
        await db.execute(text("UPDATE documentos SET fragmentos_total = 2 WHERE id = :i"), {"i": pendiente.id})
        await db.commit()
    await deshabilitar_http(m, m.memoria.id)
    antes = await fila(m, m.memoria.id)

    async with m.fabrica() as db:
        resumen = await servicio.recuperar_documentos_pendientes(
            db, SimpleNamespace(ambiente=AMBIENTE), limite=10, vectorial=VectorialDoble()
        )
    async with m.fabrica() as db:
        repetida = await servicio.publicar_en_base_vectorial(db, VectorialDoble(), m.memoria.id)

    assert (resumen.abandonados, resumen.compensados, resumen.pendientes, resumen.publicados) == (0, 0, 0, 1)  # solo el pendiente ajeno
    assert publicadas == [pendiente.id]
    assert repetida is False
    assert await fila(m, m.memoria.id) == antes
    assert (await tipos(m))["MEMORIA_ANUAL"]["estado"] == "DISPONIBLE"
    assert len(await auditoria_de_deshabilitacion(m)) == 1


@pytest.mark.parametrize(
    ("sentencia", "restriccion"),
    [
        pytest.param("UPDATE documentos SET reserva_activa = true WHERE id = $1", "ck_documentos_deshabilitado_sin_reserva", id="reactivar-el-cupo"),
        pytest.param(
            "UPDATE documentos SET deshabilitado_en = NULL, deshabilitado_por = NULL, motivo_deshabilitacion = NULL WHERE id = $1",
            "ck_documentos_reserva_solo_liberada_si_limpio", id="borrar-la-deshabilitacion-sin-reserva",
        ),
        pytest.param("UPDATE documentos SET motivo_deshabilitacion = '   ' WHERE id = $1", "ck_documentos_motivo_deshabilitacion_no_vacio", id="motivo-en-blanco"),
        pytest.param("UPDATE documentos SET deshabilitado_por = NULL WHERE id = $1", "ck_documentos_deshabilitacion_completa", id="sin-actor"),
    ],
)
async def test_la_base_no_deja_reactivar_ni_mutilar_el_registro_de_un_retirado(m, sentencia, restriccion):
    await deshabilitar_http(m, m.memoria.id)

    with pytest.raises(asyncpg.CheckViolationError) as capturado:
        await ejecutar_sql(m, sentencia, m.memoria.id)

    assert capturado.value.constraint_name == restriccion
    assert (await fila(m, m.memoria.id))["motivo_deshabilitacion"] == MOTIVO


# ============================================ Chat: el filtro va ANTES de ordenar y limitar ============================================

class AlmacenVectorialSimulado:
    """Emula `buscar_similares`: filtra por ambiente, empresa, año, sector y —si se pide— por `documento_ids` ANTES de ordenar por
    similitud y limitar, como hace el SQL. Con `respetar_documentos=False` ignora ese filtro (el comportamiento anterior)."""

    def __init__(self, fragmentos, *, respetar_documentos: bool = True, antes_de_responder=None):
        self.fragmentos, self.respetar, self.busquedas = fragmentos, respetar_documentos, []
        self.antes_de_responder = antes_de_responder

    async def buscar(self, db_vectorial, **kwargs):
        self.busquedas.append(kwargs)
        if self.antes_de_responder is not None:
            await self.antes_de_responder()
        candidatos = [f for f in self.fragmentos if f.empresa_id in kwargs["empresa_ids"] and f.anio == kwargs["anio"]]
        if self.respetar and kwargs.get("documento_ids") is not None:
            candidatos = [f for f in candidatos if f.documento_id in kwargs["documento_ids"]]
        return sorted(candidatos, key=lambda f: -f.similitud)[: kwargs["limite"]]


def fragmento_de(documento, empresa, texto: str, similitud: float, indice: int = 0) -> FragmentoRecuperado:
    return FragmentoRecuperado(
        documento_id=documento.id, indice=indice, texto_literal=texto, contexto=texto, inicio=0, fin=len(texto), continuacion=False,
        ruta_encabezados=("Agua",), empresa_id=empresa.id, anio=documento.anio, tipo=documento.tipo.value, sector=empresa.sector.value,
        embedding_proveedor="oci", embedding_modelo="cohere.embed-v4.0", similitud=similitud,
    )


TEXTO_RETIRADO = "Texto del documento retirado: la empresa redujo el consumo de agua en 12 % durante el periodo."
TEXTO_REEMPLAZO = "Texto del reemplazo: la empresa redujo el consumo de agua en 12 % tras la corrección."
TEXTO_REPORTE = "Texto del reporte vigente: la empresa gestionó el agua con indicadores propios."


@pytest_asyncio.fixture
async def chat(m, monkeypatch):
    """Memoria retirada con 6 fragmentos MUY similares a la consulta, memoria de reemplazo y reporte vigente con fragmentos menos similares."""
    await deshabilitar_http(m, m.memoria.id)
    async with m.fabrica() as db:
        reemplazo = await sembrar_documento(db, m.empresa, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, nombre="memoria-corregida-2025.md")
    fragmentos = [fragmento_de(m.memoria, m.empresa, f"{TEXTO_RETIRADO} ({n})", 0.99 - n / 100, indice=n) for n in range(6)]
    fragmentos += [fragmento_de(reemplazo, m.empresa, TEXTO_REEMPLAZO, 0.50), fragmento_de(m.reporte, m.empresa, TEXTO_REPORTE, 0.40)]
    almacen = AlmacenVectorialSimulado(fragmentos)
    monkeypatch.setattr(servicio_chat, "buscar_similares", almacen.buscar)
    return SimpleNamespace(m=m, reemplazo=reemplazo, almacen=almacen, embeddings=EmbeddingsControlado(), llm=LlmControlado())


async def preguntar(chat, *, empresa=None, anio: int = 2025):
    m = chat.m
    async with m.fabrica() as db:
        return await servicio_chat.responder_consulta(
            db, None, pregunta="¿Cómo evolucionó el consumo de agua?", ambiente=AMBIENTE, proveedor_embeddings=chat.embeddings,
            proveedor_llm=chat.llm, sector=(empresa or m.empresa).sector, empresa_id=(empresa or m.empresa).id, anio=anio,
        )


async def test_el_chat_recupera_la_evidencia_del_reemplazo_aunque_los_fragmentos_del_retirado_sean_los_mas_similares(chat):
    chat.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    resultado = await preguntar(chat)

    # La búsqueda recibió SOLO los elegibles (reemplazo y reporte), no el retirado: sus 6 fragmentos no ocupan los 4 primeros lugares.
    (busqueda,) = chat.almacen.busquedas
    assert set(busqueda["documento_ids"]) == {chat.reemplazo.id, chat.m.reporte.id}
    assert chat.m.memoria.id not in busqueda["documento_ids"]
    assert resultado.sin_evidencia is False
    assert [c.documento_id for c in resultado.citas] == [chat.reemplazo.id]
    assert TEXTO_REEMPLAZO in chat.llm.usuario
    assert TEXTO_REPORTE in chat.llm.usuario
    assert TEXTO_RETIRADO not in chat.llm.usuario


async def test_sin_el_filtro_previo_los_fragmentos_del_retirado_desplazarian_al_reemplazo(chat):
    """Control negativo: con un almacén que IGNORA `documento_ids` (el comportamiento anterior a ACT-02), los 4 primeros lugares son del
    retirado; la comprobación transaccional posterior los descarta y no queda evidencia. El filtro previo es lo que lo evita."""
    chat.almacen.respetar = False
    chat.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    resultado = await preguntar(chat)

    assert resultado.sin_evidencia is True
    assert resultado.motivo == MOTIVO_SIN_FRAGMENTOS
    assert chat.llm.llamadas == 0
    assert TEXTO_RETIRADO not in (chat.llm.usuario or "")  # la comprobación posterior sigue activa


async def test_un_documento_retirado_entre_la_lectura_de_elegibles_y_la_busqueda_tampoco_llega_al_modelo(m, monkeypatch):
    """Carrera: los elegibles se leyeron con el documento vigente, la deshabilitación llega justo antes de que la búsqueda responda y
    la comprobación transaccional posterior (`documentos_recuperables`) lo descarta."""
    texto = "Texto del documento vigente que se retira durante la consulta: redujo el consumo de agua en 12 %."
    fragmentos = [fragmento_de(m.memoria, m.empresa, texto, 0.9)]

    async def retirar():
        assert (await deshabilitar_http(m, m.memoria.id)).status_code == 200

    almacen = AlmacenVectorialSimulado(fragmentos, antes_de_responder=retirar)
    monkeypatch.setattr(servicio_chat, "buscar_similares", almacen.buscar)
    caso = SimpleNamespace(m=m, almacen=almacen, embeddings=EmbeddingsControlado(), llm=LlmControlado())
    caso.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    resultado = await preguntar(caso)

    assert m.memoria.id in almacen.busquedas[0]["documento_ids"]  # todavía era elegible al buscar
    assert resultado.sin_evidencia is True
    assert caso.llm.llamadas == 0


async def test_si_no_queda_ningun_documento_elegible_no_se_consulta_la_base_vectorial(chat):
    for documento in (chat.reemplazo, chat.m.reporte):
        assert (await deshabilitar_http(chat.m, documento.id)).status_code == 200

    resultado = await preguntar(chat)

    assert (resultado.sin_evidencia, resultado.motivo) == (True, MOTIVO_SIN_FRAGMENTOS)
    assert chat.almacen.busquedas == []
    assert chat.llm.llamadas == 0
    assert chat.embeddings.llamadas == 1


async def test_los_elegibles_del_chat_no_incluyen_otro_ano_otra_empresa_ni_cargas_no_finalizadas(m):
    async with m.fabrica() as db:
        await sembrar_documento(db, m.empresa, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, anio=2024)
        await sembrar_documento(db, m.otra, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA)
        await sembrar_documento(db, m.empresa, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, anio=2023, estado=EstadoProcesamiento.EN_PROCESO)
        await sembrar_documento(db, m.empresa, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, anio=2022, estado=EstadoProcesamiento.FALLIDO)
        await sembrar_documento(db, m.empresa, m.admin, texto=MD_GRI_204_305, tipo=MEMORIA, ambiente="qa")

    async with m.fabrica() as db:
        elegibles = await servicio.documentos_elegibles_para_consulta(db, ambiente=AMBIENTE, empresa_id=m.empresa.id, anio=2025)

    assert set(elegibles) == {m.memoria.id, m.reporte.id}


# ============================================ Migración 0011 con datos existentes ============================================

COLUMNAS_NUEVAS = {"deshabilitado_en", "deshabilitado_por", "motivo_deshabilitacion", "confirmacion_pendiente_en"}
NUEVAS_RESTRICCIONES = {
    "documentos_deshabilitado_por_fkey", "ck_documentos_deshabilitacion_completa", "ck_documentos_motivo_deshabilitacion_no_vacio",
    "ck_documentos_deshabilitado_solo_completado_publicado", "ck_documentos_deshabilitado_sin_reserva",
}


async def instantanea(esquema) -> SimpleNamespace:
    conexion = await esquema.conectar()
    try:
        filas = [dict(f) for f in await conexion.fetch("SELECT * FROM documentos ORDER BY id")]
        indices = [tuple(f) for f in await conexion.fetch("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'documentos' ORDER BY indexname")]
        restricciones = {f["conname"]: f["def"] for f in await conexion.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = 'documentos'::regclass")}
    finally:
        await conexion.close()
    return SimpleNamespace(filas=filas, indices=indices, restricciones=restricciones)


async def test_la_0011_sobre_datos_existentes_no_deshabilita_nada_conserva_los_indices_y_amplia_la_reserva(url_pg_aislado):
    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    url = url_de_base(url_pg_aislado, esquema.base)
    anterior = replace(ESQUEMA_TRANSACCIONAL, migraciones=ESQUEMA_TRANSACCIONAL.migraciones[:10])
    assert (await migrar(url, anterior)).aplicadas == tuple(range(1, 11))
    conexion = await esquema.conectar()
    try:
        ahora = completado()["completado_en"]
        publicado = await esquema.insertar(conexion, **completado(vector_publicado_en=ahora, tipo="MEMORIA_ANUAL"))
        sin_publicar = await esquema.insertar(conexion, **completado(tipo="REPORTE_SOSTENIBILIDAD_GRI"))
        en_proceso = await esquema.insertar(conexion, anio=2024)
        fallido_limpio = await esquema.insertar(
            conexion, anio=2023, estado_procesamiento="FALLIDO", motivo_fallo="X_ERROR", fallido_en=ahora, reserva_activa=False,
        )
        fallido_pendiente = await esquema.insertar(
            conexion, anio=2023, tipo="REPORTE_SOSTENIBILIDAD_GRI", estado_procesamiento="FALLIDO", motivo_fallo="X_ERROR",
            fallido_en=ahora, estado_compensacion="PENDIENTE",
        )
    finally:
        await conexion.close()
    antes = await instantanea(esquema)
    assert COLUMNAS_NUEVAS.isdisjoint(antes.filas[0])

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert (resultado.aplicadas, resultado.adoptadas, resultado.previas) == ((11, 12), (), tuple(range(1, 11)))
    despues = await instantanea(esquema)
    assert [{k: v for k, v in f.items() if k not in COLUMNAS_NUEVAS} for f in despues.filas] == antes.filas  # nada cambió
    assert all(f[c] is None for f in despues.filas for c in COLUMNAS_NUEVAS)  # NINGÚN documento existente quedó deshabilitado
    assert despues.indices == antes.indices  # los dos índices únicos parciales (y los demás) siguen igual
    assert set(despues.restricciones) - set(antes.restricciones) == NUEVAS_RESTRICCIONES
    assert set(antes.restricciones) - set(despues.restricciones) == set()  # la de reserva se redefine con el MISMO nombre
    assert despues.restricciones["ck_documentos_reserva_solo_liberada_si_limpio"] != antes.restricciones["ck_documentos_reserva_solo_liberada_si_limpio"]
    assert (await migrar(url, ESQUEMA_TRANSACCIONAL)).sin_cambios is True

    # --- la regla ampliada: SOLO un COMPLETADO publicado deshabilitado puede liberar su reserva; la regla de cargas fallidas sigue igual ---
    liberar = "UPDATE documentos SET reserva_activa = false WHERE id = $1"
    retiro = (
        "UPDATE documentos SET reserva_activa = false, deshabilitado_en = now(), deshabilitado_por = usuario_id, "
        "motivo_deshabilitacion = 'Motivo' WHERE id = $1"
    )

    async def ejecutar(sentencia, documento):
        conexion = await esquema.conectar()
        try:
            return await conexion.execute(sentencia, documento)
        finally:
            await conexion.close()

    for documento, sentencia, restriccion in (
        (publicado, liberar, "ck_documentos_reserva_solo_liberada_si_limpio"),  # liberar sin deshabilitar: no
        (en_proceso, liberar, "ck_documentos_reserva_solo_liberada_si_limpio"),
        (fallido_pendiente, liberar, "ck_documentos_reserva_solo_liberada_si_limpio"),  # limpieza pendiente: no (regla original intacta)
        (en_proceso, retiro, "ck_documentos_deshabilitado_solo_completado_publicado"),
        (sin_publicar, retiro, "ck_documentos_deshabilitado_solo_completado_publicado"),
        (fallido_limpio, retiro, "ck_documentos_deshabilitado_solo_completado_publicado"),
    ):
        with pytest.raises(asyncpg.CheckViolationError) as capturado:
            await ejecutar(sentencia, documento)
        assert capturado.value.constraint_name == restriccion, (documento, sentencia)
    assert await ejecutar(retiro, publicado) == "UPDATE 1"  # el único caso admitido

    # --- liberar la reserva libera el cupo y el SHA-256 en los índices parciales, y solo eso ---
    (datos_publicado,) = [f for f in (await instantanea(esquema)).filas if f["id"] == publicado]
    conexion = await esquema.conectar()
    try:
        nuevo = await esquema.insertar(conexion, sha256=datos_publicado["sha256"], tipo="MEMORIA_ANUAL")  # mismo cupo y mismos bytes: caben
        with pytest.raises(asyncpg.UniqueViolationError) as mismo_cupo:
            await esquema.insertar(conexion, tipo="MEMORIA_ANUAL")
        with pytest.raises(asyncpg.UniqueViolationError) as mismos_bytes:
            await esquema.insertar(conexion, anio=2010, sha256=datos_publicado["sha256"])
    finally:
        await conexion.close()
    assert nuevo != publicado
    assert mismo_cupo.value.constraint_name == "uq_documentos_empresa_anio_tipo_activo"
    assert mismos_bytes.value.constraint_name == "uq_documentos_sha256_activo"

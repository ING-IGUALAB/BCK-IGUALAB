"""Gestor de ingesta: desconexión del cliente, apagado, recuperación periódica, varias instancias y configuración.

Bases REALES y aisladas (PostgreSQL `initdb` y pgvector en Docker); OCI y MinIO son DOBLES. Se OMITEN con motivo visible
si PostgreSQL local o Docker/pgvector no están disponibles. No hay evidencia aquí de varios PROCESOS ni de servicios
reales: las «dos instancias» son dos gestores en el mismo proceso sobre las mismas bases, que es lo que prueba el candado
asesor de PostgreSQL (los candados son por conexión, así que dos conexiones se excluyen igual que dos procesos).
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.exceptions import ServiceUnavailableError
from app.models.documento_ingesta import EstadoCompensacion, EstadoProcesamiento
from app.services.ingesta import coordinador
from app.services.ingesta import gestor as modulo_gestor
from app.services.ingesta.gestor import GestorIngesta
from app.services.ingesta.parametros import PARAMETROS_INGESTA, ParametrosIngesta
from tests.ayudantes_coordinador import TEXTO_CON_HALLAZGOS, TEXTO_OBSERVADO
from tests.ayudantes_http_ingesta import (
    comprobar_error,
    construir_app,
    crear_operacion,
    cliente_http,
    ingerir_por_http,
    opciones_de_prueba,
    subir,
)


def futuro(dias: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=dias)


async def esperar(condicion, segundos: float = 15.0):
    async with asyncio.timeout(segundos):
        while True:
            resultado = condicion()
            if asyncio.iscoroutine(resultado):
                resultado = await resultado
            if resultado:
                return
            await asyncio.sleep(0.02)


async def vista(cliente, operacion_id: str) -> dict:
    return (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()


async def en_indexacion(cliente, operacion_id: str) -> bool:
    v = await vista(cliente, operacion_id)
    return v["etapa"] == "INDEXANDO" and v["fragmentos_procesados"] > 0


async def etapa_es(cliente, operacion_id: str, etapa: str) -> bool:
    return (await vista(cliente, operacion_id))["etapa"] == etapa


# ============================================ Desconexión del cliente ============================================

async def test_si_el_cliente_se_desconecta_la_ingesta_continua_y_su_desenlace_se_consulta(entorno):
    liberar = asyncio.Event()
    entorno.proveedor.antes_del_lote[1] = liberar.wait
    gestor = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
    async with cliente_http(construir_app(entorno, gestor=gestor)) as cliente:
        operacion_id = await crear_operacion(cliente)
        carga = asyncio.create_task(subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS))
        await esperar(lambda: en_indexacion(cliente, operacion_id))

        carga.cancel()  # el cliente se fue: la petición se cancela
        with pytest.raises(asyncio.CancelledError):
            await carga

        # La ingesta NO se canceló: sigue en curso y ninguna reserva se liberó ni se anunció éxito alguno.
        assert gestor.tareas_en_curso == 1
        parcial = await vista(cliente, operacion_id)
        assert parcial["estado"] == "EN_PROCESO" and parcial["exitosa"] is False and parcial["resultado_analisis"] is None
        (documento,) = await entorno.documentos()
        assert documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO and documento.reserva_activa

        liberar.set()
        await esperar(lambda: gestor.tareas_en_curso == 0)
        final = await vista(cliente, operacion_id)
        assert final["estado"] == "COMPLETADO" and final["exitosa"] is True and final["resultado_analisis"] == "CON_HALLAZGOS"
    documento = (await entorno.documentos())[0]
    assert documento.vector_publicado_en is not None
    conteo = await entorno.conteo_vectorial(documento.id)
    assert conteo.total == conteo.publicados == documento.fragmentos_total


async def test_si_el_cliente_se_desconecta_y_la_ingesta_falla_queda_limpia_y_consultable(entorno):
    liberar = asyncio.Event()
    entorno.proveedor.antes_del_lote[1] = liberar.wait
    entorno.proveedor.fallar_en_lote = 1
    gestor = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
    async with cliente_http(construir_app(entorno, gestor=gestor)) as cliente:
        operacion_id = await crear_operacion(cliente)
        carga = asyncio.create_task(subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS))
        await esperar(lambda: en_indexacion(cliente, operacion_id))
        carga.cancel()
        with pytest.raises(asyncio.CancelledError):
            await carga
        liberar.set()
        await esperar(lambda: gestor.tareas_en_curso == 0)
        final = await vista(cliente, operacion_id)
        assert final["estado"] == "FALLIDO" and final["error"]["code"] == "EMBEDDING_PROVIDER_ERROR"
    assert entorno.almacen.objetos == {}  # compensado: nada incorporado al corpus
    assert await entorno.sql_vectorial("SELECT 1 FROM fragmentos_documento") == []
    (documento,) = await entorno.documentos()
    assert documento.estado_compensacion is EstadoCompensacion.COMPLETADA and documento.reserva_activa is False


async def test_si_el_apagado_cancela_la_ingesta_nada_se_libera_y_la_recuperacion_decide(entorno):
    entorno.proveedor.antes_del_lote[1] = lambda: asyncio.sleep(60)
    gestor = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba(espera_cierre=0.05))
    async with cliente_http(construir_app(entorno, gestor=gestor)) as cliente:
        operacion_id = await crear_operacion(cliente)
        carga = asyncio.create_task(subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS))
        await esperar(lambda: en_indexacion(cliente, operacion_id))

        await gestor.cerrar()  # agota la espera y CANCELA la ingesta en curso
        resultado = (await asyncio.gather(carga, return_exceptions=True))[0]
        assert not (hasattr(resultado, "status_code") and resultado.status_code == 201)  # nunca éxito

        # Estado incierto → se conserva todo: EN_PROCESO, reserva activa, vectores y original sin tocar.
        (documento,) = await entorno.documentos()
        assert documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO and documento.reserva_activa
        assert documento.estado_compensacion is EstadoCompensacion.NINGUNA
        parcial = await vista(cliente, operacion_id)
        assert parcial["estado"] == "EN_PROCESO" and parcial["exitosa"] is False
        assert (documento.clave_original, None) in entorno.almacen.objetos

        # Mientras tanto el mismo archivo sigue bloqueado: la reserva no se liberó.
        nueva = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
        async with cliente_http(construir_app(entorno, gestor=nueva)) as otro:
            segunda, dup = await ingerir_por_http(otro, entorno, TEXTO_CON_HALLAZGOS)
            comprobar_error(dup, 409, "DOCUMENT_UPLOAD_IN_PROGRESS")

            # La recuperación (otra instancia o el siguiente arranque) la toma al vencer su vigencia y limpia.
            resumen = await coordinador.ejecutar_recuperacion(entorno.deps, ahora=futuro())
            assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (1, 1, 0)
            final = await vista(otro, operacion_id)
            assert final["estado"] == "FALLIDO" and final["error"]["code"] == "RESERVA_ABANDONADA" and final["terminal"]
            assert entorno.almacen.objetos == {}
            # Con la limpieza confirmada se puede volver a ingerir.
            _, otra = await ingerir_por_http(otro, entorno, TEXTO_CON_HALLAZGOS)
            assert otra.status_code == 201, otra.text


async def test_cancelar_antes_de_reservar_deja_la_operacion_en_carga_y_se_muestra_interrumpida_al_vencer(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        async with entorno.fabrica_pg() as db:
            await db.execute(text(
                "UPDATE operaciones_ingesta SET estado = 'EN_CARGA', carga_iniciada_en = now(), "
                "carga_vigente_hasta = now() + interval '10 minutes' WHERE id = :i"), {"i": uuid.UUID(operacion_id)})
            await db.commit()
        v = await vista(cliente, operacion_id)
        assert v["estado"] == "VALIDANDO" and v["terminal"] is False and v["error"] is None
        async with entorno.fabrica_pg() as db:
            await db.execute(text("UPDATE operaciones_ingesta SET carga_vigente_hasta = now() - interval '1 minute' WHERE id = :i"),
                             {"i": uuid.UUID(operacion_id)})
            await db.commit()
        v = await vista(cliente, operacion_id)
        assert v["estado"] == "INTERRUMPIDA" and v["exitosa"] is False and v["documento_id"] is None
        assert v["error"]["code"] == "UPLOAD_INTERRUPTED"
        # No se inventó ningún documento ni se tocó el almacén.
        assert await entorno.documentos() == [] and entorno.almacen.objetos == {}


# ============================================ Cierre ordenado ============================================

async def test_el_cierre_espera_las_ingestas_en_curso_libera_recursos_y_rechaza_trabajo_nuevo(entorno):
    entorno.proveedor.antes_del_lote[1] = lambda: asyncio.sleep(0.3)
    liberados = []

    async def liberar():
        liberados.append(1)

    gestor = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba(espera_cierre=10), liberar_recursos=liberar)
    async with cliente_http(construir_app(entorno, gestor=gestor)) as cliente:
        operacion_id = await crear_operacion(cliente)
        carga = asyncio.create_task(subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS))
        await esperar(lambda: en_indexacion(cliente, operacion_id))

        await gestor.cerrar()
        assert gestor.tareas_en_curso == 0 and liberados == [1]
        respuesta = await carga
        assert respuesta.status_code == 201  # terminó antes de cerrar: el cierre no cortó la ingesta
        assert (await vista(cliente, operacion_id))["estado"] == "COMPLETADO"

        await gestor.cerrar()  # idempotente
        assert liberados == [1]
        comprobar_error(await cliente.post("/documentos/operaciones"), 503, "INGESTION_SHUTTING_DOWN")
        otra = await crear_operacion_directa(entorno)
        comprobar_error(await subir(cliente, otra, entorno, TEXTO_OBSERVADO), 503, "INGESTION_SHUTTING_DOWN")


async def crear_operacion_directa(entorno) -> str:
    identificador = uuid.uuid4()
    async with entorno.fabrica_pg() as db:
        await db.execute(text("INSERT INTO operaciones_ingesta (id, ambiente, usuario_id) VALUES (:i, 'development', :u)"),
                         {"i": identificador, "u": entorno.usuario.id})
        await db.commit()
    return str(identificador)


# ============================================ Recuperación periódica ============================================

async def test_el_bucle_periodico_publica_pendientes_y_abandona_vencidas_y_se_detiene_al_cerrar(entorno):
    gestor = GestorIngesta(
        entorno.deps, entorno.config, opciones_de_prueba(recuperacion_habilitada=True, intervalo_recuperacion=0.05)
    )
    async with cliente_http(construir_app(entorno, gestor=gestor)) as cliente:
        # 1) Un COMPLETADO con la publicación vectorial pendiente.
        entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado", veces=1)
        pendiente, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS)
        entorno.vectorial.quitar()
        assert respuesta.status_code == 502
        # 2) Una operación EN_PROCESO abandonada (su vigencia venció). El lote se numera sobre todas las llamadas.
        entorno.proveedor.antes_del_lote[entorno.proveedor.llamadas] = lambda: asyncio.sleep(60)
        otra_gestion = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
        async with cliente_http(construir_app(entorno, gestor=otra_gestion)) as cliente2:
            abandonada = await crear_operacion(cliente2)
            carga = asyncio.create_task(subir(cliente2, abandonada, entorno, TEXTO_OBSERVADO, anio="2024"))
            await esperar(lambda: etapa_es(cliente2, abandonada, "INDEXANDO"))
            await otra_gestion.cerrar()  # la cancelación deja el documento EN_PROCESO
            await asyncio.gather(carga, return_exceptions=True)
        async with entorno.fabrica_pg() as db:
            await db.execute(text("UPDATE documentos SET ejecucion_vigente_hasta = now() - interval '1 minute' "
                                  "WHERE estado_procesamiento = 'EN_PROCESO'"))
            await db.commit()

        await gestor.iniciar()
        await esperar(lambda: gestor.barridos >= 1)

        async def resuelto():
            return (
                (await vista(cliente, pendiente))["estado"] == "COMPLETADO"
                and (await vista(cliente, abandonada))["estado"] == "FALLIDO"
            )

        await esperar(resuelto)
        assert (await vista(cliente, abandonada))["error"]["code"] == "RESERVA_ABANDONADA"
        assert entorno.almacen.objetos.keys() == {(d.clave_original, None) for d in await entorno.documentos()
                                                   if d.estado_procesamiento is EstadoProcesamiento.COMPLETADO}

        await gestor.cerrar()
        assert gestor._bucle.done() and gestor.tareas_en_curso == 0
        barridos = gestor.barridos
        await asyncio.sleep(0.2)
        assert gestor.barridos == barridos  # cerrado: no sigue barriendo


async def test_un_barrido_que_falla_no_mata_el_bucle(entorno, monkeypatch):
    llamadas = []

    async def falla_la_primera(dependencias, **kwargs):
        llamadas.append(1)
        if len(llamadas) == 1:
            raise RuntimeError("fallo transitorio con texto del documento")
        return "ok"

    monkeypatch.setattr(coordinador, "ejecutar_recuperacion", falla_la_primera)
    gestor = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba(recuperacion_habilitada=True, intervalo_recuperacion=0.03))
    await gestor.iniciar()
    await esperar(lambda: len(llamadas) >= 3)
    assert not gestor._bucle.done()
    await gestor.cerrar()


# ============================================ Varias instancias ============================================

async def test_con_dos_instancias_solo_una_barre_a_la_vez_y_el_candado_se_suelta(entorno):
    a = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
    b = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
    async with a._candado_de_barrido() as obtenido_a:
        assert obtenido_a is True
        assert await b.barrido() is None and b.barridos_omitidos == 1 and b.barridos == 0
    resumen = await b.barrido()
    assert resumen is not None and b.barridos == 1  # soltado: ahora sí

    with pytest.raises(RuntimeError):
        async with a._candado_de_barrido() as obtenido:
            assert obtenido
            raise RuntimeError("falla dentro del barrido")
    assert await b.barrido() is not None  # el candado se soltó aunque el barrido fallara


async def test_dos_barridos_simultaneos_nunca_se_solapan(entorno, monkeypatch):
    activos, maximo, ejecutados = 0, 0, 0

    async def lento(dependencias, **kwargs):
        nonlocal activos, maximo, ejecutados
        activos += 1
        ejecutados += 1
        maximo = max(maximo, activos)
        await asyncio.sleep(0.2)
        activos -= 1
        return "ok"

    monkeypatch.setattr(coordinador, "ejecutar_recuperacion", lento)
    gestores = [GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba()) for _ in range(3)]
    resultados = await asyncio.gather(*(g.barrido() for g in gestores))
    assert maximo == 1 and ejecutados == 1
    assert sorted(r is None for r in resultados) == [False, True, True]


async def test_el_candado_es_por_ambiente(entorno):
    from tests.ayudantes_ingesta import AlmacenEnMemoria
    from app.services.ingesta.coordinador import DependenciasIngesta

    qa = GestorIngesta(
        DependenciasIngesta(entorno.transaccional, entorno.vectorial, AlmacenEnMemoria("qa"), entorno.proveedor),
        entorno.config, opciones_de_prueba(),
    )
    dev = GestorIngesta(entorno.deps, entorno.config, opciones_de_prueba())
    async with dev._candado_de_barrido() as en_dev:
        async with qa._candado_de_barrido() as en_qa:
            assert en_dev is True and en_qa is True


# ============================================ Configuración y arranque ============================================

CONFIG_VALIDA = dict(
    MINIO_ENDPOINT_URL="https://minio.ejemplo.invalid", MINIO_BUCKET="bucket-prueba", MINIO_REGION=None,
    MINIO_ACCESS_KEY="clave-de-acceso-ficticia", MINIO_SECRET_KEY="clave-secreta-ficticia", APP_ENV="development",
)
# Parámetros de código SUSTITUIDOS directamente en la prueba: no se edita ningún .env.
PARAMETROS_PROPIOS = ParametrosIngesta(
    tamano_lote=8, timeout_embeddings_segundos=30, vigencia=timedelta(minutes=5), intervalo_recuperacion_segundos=45,
    espera_cierre_segundos=7, minio_read_timeout_segundos=11,
)


async def test_sin_configuracion_el_arranque_no_falla_y_la_ingesta_informa_un_503_controlado(entorno, monkeypatch):
    configuracion = SimpleNamespace(**{**CONFIG_VALIDA, "MINIO_ENDPOINT_URL": "http://sin-https.invalid/SECRETO",
                                      "MINIO_SECRET_KEY": "SK-SECRETO-NO-FILTRAR", "MINIO_ACCESS_KEY": "AK-SECRETO-NO-FILTRAR"})
    monkeypatch.setattr(modulo_gestor, "settings", configuracion)
    monkeypatch.setattr("app.config.settings.OCI_COMPARTMENT_ID", None)
    monkeypatch.setattr("app.config.settings.VECTOR_DATABASE_URL", None)

    with pytest.raises(ServiceUnavailableError) as error:
        modulo_gestor.construir_gestor()
    assert error.value.code == "INGESTION_NOT_CONFIGURED"
    componentes = {c["componente"] for c in error.value.details["componentes"]}
    assert {"almacenamiento", "embeddings", "base_vectorial"} <= componentes
    assert "SECRETO" not in json.dumps(error.value.details) and "SECRETO" not in error.value.message

    app = FastAPI()
    await modulo_gestor.iniciar_ingesta(app)  # NO lanza: la aplicación arranca igual
    assert app.state.ingesta is None and app.state.ingesta_error[0] == "INGESTION_NOT_CONFIGURED"
    await modulo_gestor.detener_ingesta(app)  # sin gestor: no hace nada

    app = construir_app(entorno, con_gestor=False)
    app.state.ingesta_error = app.state.ingesta_error[:2] + ({"componentes": error.value.details["componentes"]},)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    async with cliente_http(app) as cliente:
        assert (await cliente.get("/health")).json() == {"status": "ok"}  # el resto sigue disponible
        for metodo, ruta in (("post", "/documentos/operaciones"), ("get", "/documentos"),
                             ("get", f"/documentos/operaciones/{uuid.uuid4()}")):
            respuesta = await getattr(cliente, metodo)(ruta)
            detalle = comprobar_error(respuesta, 503, "INGESTION_NOT_CONFIGURED")
            assert {c["componente"] for c in detalle["details"]["componentes"]} >= {"almacenamiento"}
            assert "SECRETO" not in respuesta.text
    assert await entorno.sql("SELECT 1 FROM operaciones_ingesta") == []


async def test_el_503_no_oculta_la_autenticacion(entorno):
    async with cliente_http(construir_app(entorno, usuario=None, con_gestor=False)) as cliente:
        comprobar_error(await cliente.post("/documentos/operaciones"), 401, "INVALID_SESSION")
    async with cliente_http(construir_app(entorno, usuario="administrador", con_gestor=False)) as cliente:
        comprobar_error(await cliente.post("/documentos/operaciones"), 403, "FORBIDDEN")


async def test_la_aplicacion_real_arranca_sin_configuracion_de_ingesta(monkeypatch):
    from unittest.mock import AsyncMock

    from app import main

    monkeypatch.setattr(main, "crear_tablas", AsyncMock())
    monkeypatch.setattr(main, "crear_superadmin_inicial", AsyncMock())
    monkeypatch.setattr("app.config.settings.OCI_COMPARTMENT_ID", None)
    monkeypatch.setattr("app.config.settings.VECTOR_DATABASE_URL", None)
    monkeypatch.setattr("app.config.settings.MINIO_ENDPOINT_URL", None)
    async with main.app.router.lifespan_context(main.app):
        assert main.app.state.ingesta is None
        assert main.app.state.ingesta_error[0] == "INGESTION_NOT_CONFIGURED"
    main.crear_tablas.assert_awaited_once()


async def test_con_configuracion_valida_se_construyen_los_recursos_una_vez_y_se_cierran_en_orden(entorno, monkeypatch):
    cierres, recibidos = [], []

    class OciFalso:
        identidad = entorno.proveedor.identidad

        def __init__(self, parametros=None):
            recibidos.append(parametros)

        def cerrar(self):
            cierres.append("oci")

    async def cerrar_motor():
        cierres.append("vectorial")

    monkeypatch.setattr("app.services.ingesta.proveedor_oci.ProveedorEmbeddingsOCI", OciFalso)
    monkeypatch.setattr("app.database_vectorial.fabrica_sesiones_vectoriales", lambda: entorno.fabrica_vectorial)
    monkeypatch.setattr("app.database_vectorial.cerrar_motor_vectorial", cerrar_motor)
    gestor = modulo_gestor.construir_gestor(SimpleNamespace(**CONFIG_VALIDA), PARAMETROS_PROPIOS)
    assert gestor.ambiente == "development" and recibidos == [PARAMETROS_PROPIOS]
    assert gestor.config.tamano_lote == 8 and gestor.config.timeout_embeddings_segundos == 30
    assert gestor.config.vigencia == timedelta(minutes=5)
    assert gestor.opciones.intervalo_recuperacion == 45 and gestor.opciones.espera_cierre == 7
    assert gestor.opciones.recuperacion_habilitada is True
    assert gestor.dependencias.sesiones_vectoriales is entorno.fabrica_vectorial
    assert gestor.dependencias.almacen._config.read_timeout == 11  # los plazos de MinIO también salen de los parámetros
    assert "clave-secreta-ficticia" not in repr(gestor.dependencias.almacen)

    await gestor.iniciar()
    await gestor.cerrar()
    assert cierres == ["oci", "vectorial"] and gestor._bucle.done()


async def test_sin_parametros_propios_rige_la_configuracion_de_codigo_y_el_entorno_no_la_cambia(entorno, monkeypatch):
    monkeypatch.setattr("app.services.ingesta.proveedor_oci.ProveedorEmbeddingsOCI", lambda parametros=None: SimpleNamespace(identidad=entorno.proveedor.identidad))
    monkeypatch.setattr("app.database_vectorial.fabrica_sesiones_vectoriales", lambda: entorno.fabrica_vectorial)
    # Variables viejas en los ajustes (p. ej. un secreto de Jenkins no retirado): se ignoran por completo.
    viejas = dict(INGESTA_TAMANO_LOTE="999", INGESTA_VIGENCIA_MINUTOS="1", MINIO_PREFIX="otro", MINIO_READ_TIMEOUT_SECONDS="1")
    gestor = modulo_gestor.construir_gestor(SimpleNamespace(**CONFIG_VALIDA, **viejas))
    assert gestor.config.tamano_lote == 16 and gestor.config.timeout_embeddings_segundos == 120
    assert gestor.config.vigencia == timedelta(minutes=15)
    assert (gestor.opciones.recuperacion_habilitada, gestor.opciones.intervalo_recuperacion, gestor.opciones.espera_cierre) == (True, 60, 30)
    assert gestor.ambiente == "development"  # derivado de APP_ENV, no de MINIO_PREFIX
    almacen = gestor.dependencias.almacen._config
    assert (almacen.connect_timeout, almacen.read_timeout, almacen.operation_timeout) == (10, 60, 300)
    assert PARAMETROS_INGESTA.vigencia == timedelta(minutes=15)


@pytest.mark.parametrize("app_env", ["prod", "production", "QA", "", None])
async def test_un_app_env_invalido_deja_la_ingesta_sin_servicio_y_no_se_repite(entorno, monkeypatch, app_env):
    monkeypatch.setattr("app.services.ingesta.proveedor_oci.ProveedorEmbeddingsOCI", lambda parametros=None: SimpleNamespace(identidad=entorno.proveedor.identidad))
    monkeypatch.setattr("app.database_vectorial.fabrica_sesiones_vectoriales", lambda: entorno.fabrica_vectorial)
    with pytest.raises(ServiceUnavailableError) as error:
        modulo_gestor.construir_gestor(SimpleNamespace(**{**CONFIG_VALIDA, "APP_ENV": app_env}))
    assert error.value.details["componentes"] == [
        {"componente": "almacenamiento", "motivo": "APP_ENV debe ser exactamente development, qa o uat."}
    ]
    assert "prod" not in json.dumps(error.value.details)


@pytest.mark.parametrize("app_env", ["development", "qa", "uat"])
async def test_el_ambiente_del_gestor_es_app_env(entorno, monkeypatch, app_env):
    monkeypatch.setattr("app.services.ingesta.proveedor_oci.ProveedorEmbeddingsOCI", lambda parametros=None: SimpleNamespace(identidad=entorno.proveedor.identidad))
    monkeypatch.setattr("app.database_vectorial.fabrica_sesiones_vectoriales", lambda: entorno.fabrica_vectorial)
    gestor = modulo_gestor.construir_gestor(SimpleNamespace(**{**CONFIG_VALIDA, "APP_ENV": app_env}))
    assert gestor.ambiente == app_env == gestor.dependencias.almacen.ambiente

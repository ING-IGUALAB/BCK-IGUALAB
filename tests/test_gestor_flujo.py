"""Flujo del gestor de ingesta con DOBLES del coordinador y del servicio de operaciones (sin bases de datos).

Prueba la mecánica asíncrona propia del gestor —tareas registradas, `shield` ante la cancelación de la petición,
cierre ordenado, marca de rechazo, reintento de publicación y bucle de recuperación— y corre también en CI. NO
demuestra transacciones ni concurrencia entre bases: eso lo hacen `tests/services/test_gestor_ingesta.py` y
`test_documentos_http.py` contra PostgreSQL y pgvector reales.
"""
import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.exceptions import ConflictError, ExternalServiceError, ServiceUnavailableError
from app.schemas import MetadatosIngestaRequest
from app.schemas_ingesta import EstadoOperacionPublico, OperacionResponse
from app.services.ingesta import coordinador, operacion_service
from app.services.ingesta.coordinador import DependenciasIngesta, PublicacionVectorialPendiente
from app.services.ingesta.gestor import FuenteEnMemoria, GestorIngesta, OpcionesGestor
from tests.ayudantes_ingesta import AlmacenEnMemoria

OPERACION = uuid.uuid4()
DOCUMENTO = uuid.uuid4()
USUARIO = uuid.uuid4()
METADATOS = MetadatosIngestaRequest(empresa_id=uuid.uuid4(), anio=2025, tipo="MEMORIA_ANUAL")


@contextlib.asynccontextmanager
async def sesion_nula():
    yield SimpleNamespace()


def vista(estado=EstadoOperacionPublico.COMPLETADO, *, documento=True, **cambios) -> OperacionResponse:
    ahora = datetime.now(timezone.utc)
    datos = dict(
        operacion_id=OPERACION, estado=estado, terminal=estado is EstadoOperacionPublico.COMPLETADO,
        exitosa=estado is EstadoOperacionPublico.COMPLETADO, documento_id=DOCUMENTO if documento else None,
        creada_en=ahora, actualizada_en=ahora, resultado_analisis="OBSERVADO" if estado is EstadoOperacionPublico.COMPLETADO else None,
    )
    datos.update(cambios)
    return OperacionResponse(**datos)


class Entorno:
    def __init__(self, monkeypatch, **opciones) -> None:
        self.llamadas: dict[str, list] = {"tomar": [], "rechazar": [], "ingerir": [], "reintentar": [], "recuperar": []}
        self.vista = vista()
        self.resultado = SimpleNamespace(motivos=("m1", "m2"), fragmentos=3)
        self.ingestion = None  # corrutina opcional que sustituye al coordinador
        self.liberados = 0

        async def tomar_carga(db, operacion_id, **kwargs):
            self.llamadas["tomar"].append((operacion_id, kwargs))

        async def marcar_rechazada(db, operacion_id, ambiente, **kwargs):
            self.llamadas["rechazar"].append((operacion_id, ambiente, kwargs))
            return True

        async def obtener_vista(db, operacion_id, ambiente):
            return self.vista

        async def crear_operacion(db, **kwargs):
            return SimpleNamespace(**kwargs)

        async def ingerir_documento(dependencias, config, **kwargs):
            self.llamadas["ingerir"].append(kwargs)
            if self.ingestion is not None:
                return await self.ingestion(kwargs)
            await kwargs["leer"](10)  # lee algo del archivo, como el validador
            return self.resultado

        async def reintentar_publicacion(dependencias, documento_id):
            self.llamadas["reintentar"].append(documento_id)
            return True

        async def ejecutar_recuperacion(dependencias, **kwargs):
            self.llamadas["recuperar"].append(kwargs)
            return "resumen"

        for nombre, valor in dict(tomar_carga=tomar_carga, marcar_rechazada=marcar_rechazada, obtener_vista=obtener_vista,
                                  crear_operacion=crear_operacion).items():
            monkeypatch.setattr(operacion_service, nombre, valor)
        monkeypatch.setattr(coordinador, "ingerir_documento", ingerir_documento)
        monkeypatch.setattr(coordinador, "reintentar_publicacion", reintentar_publicacion)
        monkeypatch.setattr(coordinador, "ejecutar_recuperacion", ejecutar_recuperacion)

        async def liberar():
            self.liberados += 1

        deps = DependenciasIngesta(sesion_nula, sesion_nula, AlmacenEnMemoria("development"), SimpleNamespace())
        base = dict(recuperacion_habilitada=False, intervalo_recuperacion=0.02, espera_cierre=1.0, retraso_primer_barrido=0.0)
        base.update(opciones)
        self.gestor = GestorIngesta(deps, opciones=OpcionesGestor(**base), liberar_recursos=liberar)

    async def lector(self, datos: bytes = b"# Hola\n"):
        posicion = 0

        async def leer(cantidad):
            nonlocal posicion
            trozo = datos[posicion:posicion + cantidad]
            posicion += len(trozo)
            return trozo

        return leer

    async def ingerir(self, **cambios):
        argumentos = dict(operacion_id=OPERACION, usuario_id=USUARIO, nombre_archivo="a.md", leer=await self.lector(), metadatos=METADATOS)
        argumentos.update(cambios)
        return await self.gestor.ingerir(**argumentos)


@pytest.fixture
def f(monkeypatch):
    return Entorno(monkeypatch)


# ============================================ Ingesta ============================================

async def test_ingesta_exitosa_toma_la_carga_ejecuta_y_devuelve_la_vista_confirmada(f):
    respuesta = await f.ingerir()
    (operacion, kwargs) = f.llamadas["tomar"][0]
    assert operacion == OPERACION
    assert kwargs["usuario_id"] == USUARIO
    assert kwargs["ambiente"] == "development"
    assert kwargs["vigencia"] == f.gestor.config.vigencia
    (llamada,) = f.llamadas["ingerir"]
    assert llamada["operacion_id"] == OPERACION
    assert llamada["usuario_id"] == USUARIO
    assert llamada["nombre_archivo"] == "a.md"
    assert respuesta.exitosa
    assert respuesta.motivos == ["m1", "m2"]
    assert respuesta.fragmentos == 3
    assert f.llamadas["rechazar"] == []
    assert f.gestor.tareas_en_curso == 0


async def test_si_la_base_no_confirma_el_exito_no_se_anuncia(f):
    f.vista = vista(EstadoOperacionPublico.EN_PROCESO, exitosa=False, terminal=False)
    with pytest.raises(ExternalServiceError) as error:
        await f.ingerir()
    assert error.value.code == "INGESTION_OUTCOME_NOT_CONFIRMED"
    assert error.value.details["estado"] == "EN_PROCESO"


@pytest.mark.parametrize(
    "error,codigo,estado_http",
    [
        (ConflictError("DOCUMENT_ALREADY_INGESTED", "Duplicado."), "DOCUMENT_ALREADY_INGESTED", 409),
        (ExternalServiceError("EMBEDDING_PROVIDER_ERROR", "Falló."), "EMBEDDING_PROVIDER_ERROR", 502),
        (RuntimeError("secreto: texto del documento"), "INTERNAL_ERROR", 500),
    ],
)
async def test_un_error_antes_de_reservar_marca_la_operacion_rechazada_con_su_codigo(f, error, codigo, estado_http):
    async def falla(kwargs):
        raise error

    f.ingestion = falla
    with pytest.raises(type(error)):
        await f.ingerir()
    ((operacion, ambiente, kwargs),) = f.llamadas["rechazar"]
    assert (operacion, ambiente, kwargs["codigo"], kwargs["estado_http"]) == (OPERACION, "development", codigo, estado_http)
    assert "secreto" not in kwargs["mensaje"]


async def test_si_no_se_puede_marcar_el_rechazo_el_error_original_se_conserva(f, monkeypatch, caplog):
    async def falla(kwargs):
        raise ConflictError("DOCUMENT_ALREADY_INGESTED", "Duplicado.")

    async def marcar_falla(*args, **kwargs):
        raise ConnectionError("base caída")

    f.ingestion = falla
    monkeypatch.setattr(operacion_service, "marcar_rechazada", marcar_falla)
    with caplog.at_level(logging.WARNING, logger="igualab.ingesta.http"):
        with pytest.raises(ConflictError) as error:
            await f.ingerir()
    assert error.value.code == "DOCUMENT_ALREADY_INGESTED"
    assert any("No se pudo marcar la operación" in r.message for r in caplog.records)


async def test_la_publicacion_pendiente_lleva_la_operacion_y_no_marca_rechazo(f):
    async def pendiente(kwargs):
        raise PublicacionVectorialPendiente(DOCUMENTO, "VECTOR_PUBLICATION_FAILED")

    f.ingestion = pendiente
    with pytest.raises(PublicacionVectorialPendiente) as error:
        await f.ingerir()
    assert error.value.details["operacion_id"] == str(OPERACION)
    assert error.value.details["documento_id"] == str(DOCUMENTO)
    assert error.value.details["reintentar_url"].endswith("/reintentar-publicacion")
    assert f.llamadas["rechazar"] == []  # el documento existe: manda el documento


async def test_una_segunda_carga_sobre_la_misma_operacion_falla_antes_de_leer_el_archivo(f, monkeypatch):
    async def ya_tomada(db, operacion_id, **kwargs):
        raise ConflictError("OPERATION_ALREADY_STARTED", "Ya recibió una carga.")

    monkeypatch.setattr(operacion_service, "tomar_carga", ya_tomada)
    leido = []

    async def leer(n):
        leido.append(n)
        return b""

    with pytest.raises(ConflictError):
        await f.gestor.ingerir(operacion_id=OPERACION, usuario_id=USUARIO, nombre_archivo="a.md", leer=leer, metadatos=METADATOS)
    assert leido == []
    assert f.llamadas["ingerir"] == []


async def test_un_fallo_al_copiar_el_archivo_marca_el_rechazo_y_una_cancelacion_no(f):
    async def lectura_rota(n):
        raise OSError("disco")

    with pytest.raises(OSError):
        await f.ingerir(leer=lectura_rota)
    assert f.llamadas["rechazar"][0][2]["codigo"] == "INTERNAL_ERROR"
    assert f.llamadas["ingerir"] == []

    async def lectura_cancelada(n):
        raise asyncio.CancelledError

    antes = len(f.llamadas["rechazar"])
    with pytest.raises(asyncio.CancelledError):
        await f.ingerir(leer=lectura_cancelada)
    assert len(f.llamadas["rechazar"]) == antes  # queda EN_CARGA: no hay certeza, se muestra interrumpida al vencer


# ============================================ Desconexión y cierre ============================================

async def test_cancelar_la_peticion_no_cancela_la_ingesta_y_su_resultado_no_se_pierde(f):
    avanzar, entro = asyncio.Event(), asyncio.Event()

    async def lenta(kwargs):
        entro.set()
        await avanzar.wait()
        return f.resultado

    f.ingestion = lenta
    manejador = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, contexto: manejador.append(contexto))
    peticion = asyncio.create_task(f.ingerir())
    await entro.wait()
    peticion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await peticion
    assert f.gestor.tareas_en_curso == 1  # la ingesta sigue
    avanzar.set()
    async with asyncio.timeout(5):
        while f.gestor.tareas_en_curso:
            await asyncio.sleep(0.01)
    assert f.llamadas["rechazar"] == []
    assert manejador == []


async def test_el_error_de_una_ingesta_sin_cliente_se_registra_y_no_queda_sin_leer(f, caplog):
    entro, avanzar = asyncio.Event(), asyncio.Event()

    async def falla_despues(kwargs):
        entro.set()
        await avanzar.wait()
        raise ExternalServiceError("EMBEDDING_PROVIDER_ERROR", "Falló.")

    f.ingestion = falla_despues
    manejador = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, contexto: manejador.append(contexto))
    with caplog.at_level(logging.WARNING, logger="igualab.ingesta.http"):
        peticion = asyncio.create_task(f.ingerir())
        await entro.wait()
        peticion.cancel()
        await asyncio.gather(peticion, return_exceptions=True)
        avanzar.set()
        async with asyncio.timeout(5):
            while f.gestor.tareas_en_curso:
                await asyncio.sleep(0.01)
    assert manejador == []
    assert any("codigo=EMBEDDING_PROVIDER_ERROR" in r.message for r in caplog.records)


async def test_el_cierre_cancela_lo_que_no_termina_y_libera_recursos(monkeypatch):
    f = Entorno(monkeypatch, espera_cierre=0.05)
    entro = asyncio.Event()

    async def eterna(kwargs):
        entro.set()
        await asyncio.sleep(60)

    f.ingestion = eterna
    peticion = asyncio.create_task(f.ingerir())
    await entro.wait()
    await f.gestor.cerrar()
    resultados = await asyncio.gather(peticion, return_exceptions=True)
    assert isinstance(resultados[0], asyncio.CancelledError)
    assert f.gestor.tareas_en_curso == 0
    assert f.liberados == 1
    assert f.llamadas["rechazar"] == []  # cancelada: no se toca el estado
    await f.gestor.cerrar()
    assert f.liberados == 1  # idempotente


async def test_el_cierre_espera_a_las_que_terminan_a_tiempo(monkeypatch):
    f = Entorno(monkeypatch, espera_cierre=5.0)
    entro = asyncio.Event()

    async def corta(kwargs):
        entro.set()
        await asyncio.sleep(0.1)
        return f.resultado

    f.ingestion = corta
    peticion = asyncio.create_task(f.ingerir())
    await entro.wait()
    await f.gestor.cerrar()
    assert (await peticion).exitosa is True
    assert f.liberados == 1


async def test_un_gestor_cerrado_rechaza_trabajo_nuevo(f):
    await f.gestor.cerrar()
    for corrutina in (f.ingerir(), f.gestor.crear_operacion(SimpleNamespace(), usuario_id=USUARIO), f.gestor.reintentar_publicacion(OPERACION)):
        with pytest.raises(ServiceUnavailableError) as error:
            await corrutina
        assert error.value.code == "INGESTION_SHUTTING_DOWN"
    assert f.llamadas["tomar"] == []
    assert f.llamadas["ingerir"] == []


async def test_crear_operacion_delega_con_el_ambiente_del_gestor(f):
    creada = await f.gestor.crear_operacion(SimpleNamespace(), usuario_id=USUARIO)
    assert creada.usuario_id == USUARIO
    assert creada.ambiente == "development"


# ============================================ Reintento de publicación ============================================

async def test_reintentar_exige_documento_y_publicacion_pendiente(f):
    f.vista = vista(EstadoOperacionPublico.CREADA, documento=False)
    with pytest.raises(ConflictError) as sin_documento:
        await f.gestor.reintentar_publicacion(OPERACION)
    assert sin_documento.value.code == "OPERATION_WITHOUT_DOCUMENT"
    assert sin_documento.value.details == {"estado": "CREADA"}

    f.vista = vista()
    with pytest.raises(ConflictError) as no_pendiente:
        await f.gestor.reintentar_publicacion(OPERACION)
    assert no_pendiente.value.code == "VECTOR_PUBLICATION_NOT_PENDING"
    assert f.llamadas["reintentar"] == []


async def test_reintentar_publica_y_devuelve_la_operacion_actualizada(f, monkeypatch):
    pendiente = vista(EstadoOperacionPublico.PUBLICACION_PENDIENTE, exitosa=False, terminal=False)
    consultas = []

    async def obtener_vista(db, operacion_id, ambiente):
        consultas.append(1)
        return pendiente if len(consultas) == 1 else vista()  # tras publicar, la operación ya está completada

    monkeypatch.setattr(operacion_service, "obtener_vista", obtener_vista)
    resultado = await f.gestor.reintentar_publicacion(OPERACION)
    assert f.llamadas["reintentar"] == [DOCUMENTO]
    assert resultado.exitosa
    assert len(consultas) == 2


async def test_si_el_reintento_falla_de_nuevo_lleva_la_operacion(f, monkeypatch):
    f.vista = vista(EstadoOperacionPublico.PUBLICACION_PENDIENTE, exitosa=False, terminal=False)

    async def vuelve_a_fallar(dependencias, documento_id):
        raise PublicacionVectorialPendiente(documento_id, "VECTOR_PUBLICATION_FAILED")

    monkeypatch.setattr(coordinador, "reintentar_publicacion", vuelve_a_fallar)
    with pytest.raises(PublicacionVectorialPendiente) as error:
        await f.gestor.reintentar_publicacion(OPERACION)
    assert error.value.details["operacion_id"] == str(OPERACION)


# ============================================ Recuperación periódica ============================================

async def test_el_bucle_barre_periodicamente_sobrevive_a_los_errores_y_se_detiene_al_cerrar(monkeypatch, caplog):
    f = Entorno(monkeypatch, recuperacion_habilitada=True)
    intentos = []

    async def recuperar(dependencias, **kwargs):
        intentos.append(1)
        if len(intentos) == 1:
            raise RuntimeError("fallo transitorio")
        return "resumen"

    monkeypatch.setattr(coordinador, "ejecutar_recuperacion", recuperar)

    @contextlib.asynccontextmanager
    async def sin_candado():
        yield True

    f.gestor._candado_de_barrido = sin_candado
    with caplog.at_level(logging.WARNING, logger="igualab.ingesta.http"):
        f.gestor.iniciar()
        f.gestor.iniciar()  # idempotente: un solo bucle
        async with asyncio.timeout(5):
            while len(intentos) < 3:
                await asyncio.sleep(0.01)
        await f.gestor.cerrar()
    assert f.gestor._bucle.done()
    assert any("barrido de recuperación" in r.message for r in caplog.records)
    cantidad = len(intentos)
    await asyncio.sleep(0.1)
    assert len(intentos) == cantidad


async def test_un_barrido_sin_candado_se_omite(f):
    @contextlib.asynccontextmanager
    async def ocupado():
        yield False

    f.gestor._candado_de_barrido = ocupado
    assert await f.gestor.barrido() is None
    assert f.gestor.barridos_omitidos == 1
    assert f.llamadas["recuperar"] == []


async def test_el_barrido_pasa_los_argumentos_al_coordinador(f):
    @contextlib.asynccontextmanager
    async def libre():
        yield True

    f.gestor._candado_de_barrido = libre
    assert await f.gestor.barrido(limite=7) == "resumen"
    assert f.llamadas["recuperar"] == [{"limite": 7}]
    assert f.gestor.barridos == 1


# ============================================ Fuente en memoria ============================================

async def test_la_fuente_en_memoria_entrega_los_bytes_en_orden_y_se_vacia():
    fuente = FuenteEnMemoria(bytearray(b"abcdefghij"))
    assert fuente.tamano == 10
    assert [await fuente.leer(4), await fuente.leer(4), await fuente.leer(4), await fuente.leer(4)] == [b"abcd", b"efgh", b"ij", b""]
    assert fuente.tamano == 0
    assert await fuente.leer(4) == b""


# ============================================ Preparación del esquema en el arranque ============================================

class _AppDoble:
    def __init__(self):
        self.state = SimpleNamespace()


def _gestor_doble(registro: list, monkeypatch):
    from app.services.ingesta import gestor as modulo

    deps = DependenciasIngesta(sesion_nula, sesion_nula, AlmacenEnMemoria("development"), SimpleNamespace())

    async def liberar():
        registro.append("liberado")

    gestor = GestorIngesta(deps, opciones=OpcionesGestor(recuperacion_habilitada=False), liberar_recursos=liberar)

    def construir(*args, **kwargs):
        registro.append("construido")
        return gestor

    monkeypatch.setattr(modulo, "construir_gestor", construir)
    return gestor


async def test_el_esquema_se_prepara_despues_de_construir_y_antes_de_habilitar_la_ingesta(monkeypatch):
    from app.services.ingesta.gestor import iniciar_ingesta

    orden = []
    gestor = _gestor_doble(orden, monkeypatch)

    async def preparar():
        orden.append("esquema")
        assert gestor._bucle is None
        assert _app.state.ingesta is None  # todavía no habilitada

    _app = _AppDoble()
    await iniciar_ingesta(_app, preparar_esquema=preparar)
    assert orden == ["construido", "esquema"]
    assert _app.state.ingesta is gestor
    assert _app.state.ingesta_error is None


async def test_si_falla_la_preparacion_del_esquema_la_ingesta_queda_en_503_y_se_liberan_los_recursos(monkeypatch, caplog):
    from app.migraciones.motor import ErrorMigracion
    from app.services.ingesta.gestor import iniciar_ingesta

    orden = []
    gestor = _gestor_doble(orden, monkeypatch)

    async def preparar():
        raise ErrorMigracion("PGVECTOR_REQUIRED", "Falta la extensión pgvector. Requisito: instalarla.", "vectorial", {"sqlstate": "42501"})

    app = _AppDoble()
    with caplog.at_level(logging.ERROR, logger="igualab.ingesta.http"):
        await iniciar_ingesta(app, preparar_esquema=preparar)  # NO lanza: el arranque de la aplicación continúa
    assert app.state.ingesta is None
    assert orden == ["construido", "liberado"]
    assert gestor._cerrado
    codigo, mensaje, detalles = app.state.ingesta_error
    assert codigo == "INGESTION_SCHEMA_NOT_READY"
    assert "Requisito: instalarla" in mensaje
    assert detalles == {"base": "vectorial", "motivo": "PGVECTOR_REQUIRED", "sqlstate": "42501"}
    assert any("PGVECTOR_REQUIRED" in r.message for r in caplog.records)


async def test_un_error_inesperado_al_preparar_el_esquema_tampoco_impide_el_arranque(monkeypatch):
    from app.services.ingesta.gestor import iniciar_ingesta

    orden = []
    _gestor_doble(orden, monkeypatch)

    async def preparar():
        raise RuntimeError("detalle interno con credenciales")

    app = _AppDoble()
    await iniciar_ingesta(app, preparar_esquema=preparar)
    assert app.state.ingesta is None
    assert orden == ["construido", "liberado"]
    assert app.state.ingesta_error[0] == "INGESTION_NOT_CONFIGURED"
    assert "credenciales" not in str(app.state.ingesta_error)


async def test_con_configuracion_incompleta_no_se_intenta_migrar(monkeypatch):
    from app.services.ingesta import gestor as modulo

    llamadas = []

    def sin_config(*args, **kwargs):
        raise ServiceUnavailableError("INGESTION_NOT_CONFIGURED", "Falta configuración.", details={"componentes": []})

    async def preparar():
        llamadas.append(1)

    monkeypatch.setattr(modulo, "construir_gestor", sin_config)
    app = _AppDoble()
    await modulo.iniciar_ingesta(app, preparar_esquema=preparar)
    assert llamadas == []
    assert app.state.ingesta_error[0] == "INGESTION_NOT_CONFIGURED"


async def test_con_el_esquema_sin_preparar_solo_la_ingesta_responde_503_y_el_resto_sigue(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from app.exception_handlers import register_exception_handlers
    from app.migraciones.motor import ErrorMigracion
    from app.request_id import RequestIDMiddleware
    from app.routers import documentos
    from app.services.ingesta.gestor import iniciar_ingesta
    from app.database import get_db
    from app.dependencies import get_current_user
    from app.models import RolUsuario

    _gestor_doble([], monkeypatch)

    async def preparar():
        raise ErrorMigracion("MIGRATION_SCHEMA_MISMATCH", "El objeto 'documentos' no coincide.", "transaccional", {"objeto": "documentos"})

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(documentos.router)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.SUPERADMIN)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    await iniciar_ingesta(app, preparar_esquema=preparar)
    transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://prueba") as cliente:
        assert (await cliente.get("/health")).json() == {"status": "ok"}
        for metodo, ruta in (("post", "/documentos/operaciones"), ("get", "/documentos")):
            respuesta = await getattr(cliente, metodo)(ruta)
            assert respuesta.status_code == 503
            error = respuesta.json()["error"]
            assert error["code"] == "INGESTION_SCHEMA_NOT_READY"
            assert error["details"] == {"base": "transaccional", "motivo": "MIGRATION_SCHEMA_MISMATCH", "objeto": "documentos"}
            assert "postgresql" not in respuesta.text.lower()


async def test_el_lifespan_real_prepara_el_esquema_tras_los_modulos_existentes_y_sobrevive_a_su_fallo(monkeypatch):
    from unittest.mock import AsyncMock

    from app import main
    from app.migraciones.motor import ErrorMigracion

    orden = []

    async def crear_tablas():
        orden.append("crear_tablas")

    async def superadmin():
        orden.append("superadmin")

    async def preparar():
        orden.append("esquema")
        raise ErrorMigracion("MIGRATION_DB_UNAVAILABLE", "No se pudo conectar a la base vectorial.", "vectorial", {"causa": "OSError"})

    monkeypatch.setattr(main, "crear_tablas", crear_tablas)
    monkeypatch.setattr(main, "crear_superadmin_inicial", superadmin)
    monkeypatch.setattr("app.migraciones.catalogo.asegurar_esquema_ingesta", preparar)
    _gestor_doble(orden, monkeypatch)
    async with main.app.router.lifespan_context(main.app):
        assert orden == ["crear_tablas", "superadmin", "construido", "esquema", "liberado"]
        assert main.app.state.ingesta is None
        assert main.app.state.ingesta_error[0] == "INGESTION_SCHEMA_NOT_READY"

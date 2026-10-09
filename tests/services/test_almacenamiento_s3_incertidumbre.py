"""Etapa 4A: adaptador S3 ante subidas que terminan DESPUÉS del timeout o de la cancelación,
versionado y reintentos del SDK.

Nivel: adaptador real (`AlmacenOriginalesS3`) sobre un cliente boto3 FALSO con hilos reales
(`tests.ayudantes_ingesta.ClienteS3Versionado`). Sin red ni credenciales: NO demuestra el
comportamiento del servidor MinIO real.
"""
import asyncio
import hashlib
import uuid

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from app.exceptions import ConflictError, ExternalServiceError, ExternalServiceTimeoutError
from app.services.ingesta import almacenamiento_s3
from app.services.ingesta.almacenamiento import (
    ConfigAlmacenamiento,
    EstadoSubida,
    ReferenciaOriginal,
    generar_clave_original,
)
from app.services.ingesta.almacenamiento_s3 import AlmacenOriginalesS3, crear_cliente_s3, listar_versiones_exactas
from tests.ayudantes_ingesta import ClienteS3Versionado, error_cliente

DATOS = b"\xef\xbb\xbf# Memoria\n"
SHA = hashlib.sha256(DATOS).hexdigest()
BUCKET = "bucket-prueba"


def configuracion(**cambios) -> ConfigAlmacenamiento:
    base = dict(
        endpoint_url="https://almacen.ejemplo.invalid", bucket=BUCKET, ambiente="development",
        access_key="ACCESO-FICTICIO", secret_key="SECRETO-FICTICIO",
        connect_timeout=5, read_timeout=30, operation_timeout=5,
    )
    base.update(cambios)
    return ConfigAlmacenamiento(**base)


def clave_nueva() -> str:
    return generar_clave_original("development", uuid.uuid4())


def preparar(cliente=None, **cambios):
    cliente = cliente or ClienteS3Versionado()
    return AlmacenOriginalesS3(configuracion(**cambios), cliente), cliente


async def esperar_fin_de_subida(almacen, clave: str) -> EstadoSubida:
    """Espera una CONDICIÓN (el hilo anotó su desenlace), no un tiempo fijo."""
    async with asyncio.timeout(10):
        while (estado := (await almacen.estado_subida(clave)).estado) is EstadoSubida.EN_CURSO:
            await asyncio.sleep(0.005)
    return estado


# --- 1. Reintentos del SDK: configuración efectiva -----------------------------------------------------------

def test_la_configuracion_efectiva_del_cliente_es_una_sola_peticion():
    cliente = crear_cliente_s3(configuracion())
    reintentos = cliente.meta.config.retries
    assert reintentos["total_max_attempts"] == 1
    assert "max_attempts" not in reintentos
    assert reintentos["mode"] == "standard"


def test_un_error_5xx_produce_exactamente_una_peticion_sin_reintentos_automaticos():
    """Prueba de comportamiento: un servidor que responde 503 recibe UNA sola petición.
    Con `max_attempts=1` (el valor anterior) botocore enviaría dos."""
    cliente = crear_cliente_s3(configuracion())
    peticiones: list[str] = []

    class Crudo:
        def stream(self):
            yield b"<Error><Code>ServiceUnavailable</Code></Error>"

    def responder(request, **kwargs):
        peticiones.append(request.method)
        return AWSResponse(request.url, 503, {"Content-Type": "application/xml"}, Crudo())

    cliente.meta.events.register("before-send.s3.PutObject", responder)
    with pytest.raises(ClientError) as capturado:
        cliente.put_object(Bucket=BUCKET, Key="k", Body=b"x")
    assert capturado.value.response["ResponseMetadata"]["HTTPStatusCode"] == 503
    assert capturado.value.response["Error"]["Code"] == "ServiceUnavailable"
    assert peticiones == ["PUT"]


# --- 2. Desenlace real de la subida ----------------------------------------------------------------------------

async def test_una_subida_que_termina_despues_del_timeout_deja_su_desenlace_y_su_version():
    almacen, cliente = preparar(operation_timeout=0.05)
    cliente.compuerta = threading_event()
    clave = clave_nueva()

    with pytest.raises(ExternalServiceTimeoutError) as capturado:
        await almacen.guardar(clave, DATOS, SHA)
    assert capturado.value.details["resultado_incierto"] is True
    assert (await almacen.estado_subida(clave)).estado is EstadoSubida.EN_CURSO  # el hilo sigue vivo
    assert cliente.versiones_de(clave) == []  # el objeto aún no existe: aparecerá DESPUÉS

    cliente.compuerta.set()
    assert await esperar_fin_de_subida(almacen, clave) is EstadoSubida.CREADA
    conocida = await almacen.estado_subida(clave)
    assert conocida.version_id == cliente.versiones_de(clave)[0]["VersionId"]  # versión concreta conocida


async def test_una_subida_cancelada_tambien_termina_despues_y_se_registra():
    almacen, cliente = preparar()
    cliente.compuerta = threading_event()
    clave = clave_nueva()
    tarea = asyncio.create_task(almacen.guardar(clave, DATOS, SHA))
    await asyncio.to_thread(cliente.put_iniciado.wait, 5)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert (await almacen.estado_subida(clave)).estado is EstadoSubida.EN_CURSO

    cliente.compuerta.set()
    assert await esperar_fin_de_subida(almacen, clave) is EstadoSubida.CREADA
    assert len(cliente.versiones_de(clave)) == 1


async def test_sin_versionado_la_subida_creada_no_tiene_version():
    almacen, cliente = preparar(ClienteS3Versionado(versionado=False))
    clave = clave_nueva()
    referencia = await almacen.guardar(clave, DATOS, SHA)
    assert referencia.version_id is None
    conocida = await almacen.estado_subida(clave)
    assert (conocida.estado, conocida.version_id) == (EstadoSubida.CREADA, None)


@pytest.mark.parametrize("error, esperado", [
    (error_cliente("AccessDenied", 403), EstadoSubida.NO_CREADA),
    (error_cliente("BadDigest", 400), EstadoSubida.NO_CREADA),
    (error_cliente("RequestTimeout", 408), EstadoSubida.INCIERTA),
    (error_cliente("SlowDown", 429), EstadoSubida.INCIERTA),
    (error_cliente("InternalError", 500), EstadoSubida.INCIERTA),
    (error_cliente("ServiceUnavailable", 503), EstadoSubida.INCIERTA),
    (ConnectTimeoutError(endpoint_url="https://x.invalid"), EstadoSubida.NO_CREADA),
    (EndpointConnectionError(endpoint_url="https://x.invalid"), EstadoSubida.NO_CREADA),
    (ReadTimeoutError(endpoint_url="https://x.invalid"), EstadoSubida.INCIERTA),
    (RuntimeError("cualquier otro"), EstadoSubida.INCIERTA),
])
async def test_el_desenlace_distingue_lo_que_nunca_llego_de_lo_que_pudo_crearse(error, esperado):
    almacen, cliente = preparar()
    cliente.errores["put_object"] = error
    clave = clave_nueva()
    with pytest.raises(ExternalServiceError):
        await almacen.guardar(clave, DATOS, SHA)
    assert (await almacen.estado_subida(clave)).estado is esperado


async def test_una_clave_nunca_subida_desde_este_proceso_no_tiene_registro():
    almacen, _ = preparar()
    conocida = await almacen.estado_subida(clave_nueva())
    assert (conocida.estado, conocida.version_id) == (EstadoSubida.SIN_REGISTRO, None)


async def test_estado_subida_y_listado_rechazan_claves_ajenas():
    almacen, cliente = preparar()
    for operacion in (almacen.estado_subida, almacen.listar_versiones):
        with pytest.raises(ExternalServiceError) as capturado:
            await operacion("development/pruebas/ajena.md")
        assert capturado.value.code == "STORAGE_INVALID_KEY"
    assert cliente.llamadas == []


# --- 3. No sobrescritura ----------------------------------------------------------------------------------------

async def test_el_adaptador_rechaza_una_segunda_subida_de_la_misma_clave_sin_llamar_al_servidor():
    almacen, cliente = preparar()
    clave = clave_nueva()
    await almacen.guardar(clave, DATOS, SHA)
    with pytest.raises(ConflictError) as capturado:
        await almacen.guardar(clave, DATOS, SHA)
    assert capturado.value.code == "STORAGE_UPLOAD_ALREADY_ATTEMPTED"
    assert cliente.metodos().count("put_object") == 1
    assert len(cliente.versiones_de(clave)) == 1


async def test_dos_subidas_simultaneas_de_la_misma_clave_solo_envian_una_peticion():
    almacen, cliente = preparar()
    cliente.compuerta = threading_event()
    clave = clave_nueva()
    primera = asyncio.create_task(almacen.guardar(clave, DATOS, SHA))
    await asyncio.to_thread(cliente.put_iniciado.wait, 5)
    with pytest.raises(ConflictError):
        await almacen.guardar(clave, DATOS, SHA)
    cliente.compuerta.set()
    await primera
    assert cliente.metodos().count("put_object") == 1


async def test_el_registro_de_subidas_esta_acotado_y_nunca_descarta_las_que_siguen_en_curso(monkeypatch):
    monkeypatch.setattr(almacenamiento_s3, "_MAXIMO_REGISTROS_SUBIDA", 2)
    almacen, cliente = preparar()
    claves = [clave_nueva() for _ in range(4)]
    for clave in claves[:3]:
        await almacen.guardar(clave, DATOS, SHA)
    assert len(almacen._subidas) == 2
    cliente.compuerta = threading_event()
    cliente.put_iniciado.clear()  # las subidas anteriores ya lo activaron: se espera la de esta clave
    en_curso = asyncio.create_task(almacen.guardar(claves[3], DATOS, SHA))
    await asyncio.to_thread(cliente.put_iniciado.wait, 5)
    assert (await almacen.estado_subida(claves[3])).estado is EstadoSubida.EN_CURSO
    cliente.compuerta.set()
    await en_curso


# --- 4. Listado de versiones ----------------------------------------------------------------------------------------

async def test_listar_versiones_solo_devuelve_la_clave_exacta_con_versiones_y_marcas():
    almacen, cliente = preparar()
    clave = clave_nueva()
    v1 = cliente.poner(clave, b"uno")
    cliente.poner(clave + ".bak", b"otra clave con el mismo prefijo")
    await almacen.eliminar(ReferenciaOriginal(clave))  # crea una marca de borrado encima de v1
    versiones = await almacen.listar_versiones(clave)
    assert [(v.es_marca_de_borrado, v.tamano) for v in versiones] == [(False, 3), (True, None)]
    assert versiones[0].version_id == v1
    assert all(clave + ".bak" not in str(v) for v in versiones)


async def test_listar_versiones_recorre_todas_las_paginas():
    almacen, cliente = preparar(ClienteS3Versionado(tamano_pagina=2))
    clave = clave_nueva()
    for n in range(5):
        cliente.poner(clave, b"x" * (n + 1))
    versiones = await almacen.listar_versiones(clave)
    assert len(versiones) == 5
    assert cliente.metodos().count("list_object_versions") == 3


async def test_un_listado_sin_permiso_es_un_error_y_nunca_una_lista_vacia():
    almacen, cliente = preparar(ClienteS3Versionado(permitir_listado=False))
    valor_clave_nueva = clave_nueva()
    with pytest.raises(ExternalServiceError) as capturado:
        await almacen.listar_versiones(valor_clave_nueva)
    assert capturado.value.code == "STORAGE_ERROR"
    assert capturado.value.details["codigo_s3"] == "AccessDenied"
    assert "bucket-prueba" not in str(capturado.value.details)


def test_un_listado_truncado_sin_marcador_no_se_toma_por_completo():
    class Truncado:
        def list_object_versions(self, **kw):
            return {"IsTruncated": True, "Versions": []}

    valor_truncado = Truncado()
    with pytest.raises(RuntimeError):
        listar_versiones_exactas(valor_truncado, BUCKET, "k")


def test_un_listado_que_nunca_termina_se_corta_en_lugar_de_dar_por_vacio():
    class Infinito:
        def list_object_versions(self, **kw):
            return {"IsTruncated": True, "NextKeyMarker": "k", "NextVersionIdMarker": "v", "Versions": []}

    valor_infinito = Infinito()
    with pytest.raises(RuntimeError):
        listar_versiones_exactas(valor_infinito, BUCKET, "k")


# --- 5. Eliminación por versión ------------------------------------------------------------------------------------------

async def test_eliminar_con_version_borra_solo_esa_version_y_no_crea_marca():
    almacen, cliente = preparar()
    clave = clave_nueva()
    antigua = cliente.poner(clave, b"antigua")
    nueva = cliente.poner(clave, b"nueva")
    await almacen.eliminar(ReferenciaOriginal(clave, nueva))
    assert [e["VersionId"] for e in cliente.versiones_de(clave)] == [antigua]
    assert cliente.llamadas[-1][1]["VersionId"] == nueva


def threading_event():
    import threading

    return threading.Event()

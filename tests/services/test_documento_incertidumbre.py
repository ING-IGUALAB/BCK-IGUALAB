"""Etapa 4A: subidas con resultado incierto, versionado, vigencia de la operación y
ejecutores desplazados.

Nivel: servicio real + adaptador S3 REAL sobre un cliente boto3 falso con hilos reales
(`ClienteS3Versionado`) + SQLite aislado. Sin red ni credenciales: demuestra la lógica de
reconciliación, NO el comportamiento de MinIO (versionado y permisos de listado reales
siguen sin comprobar). La concurrencia entre procesos se prueba en
`test_documento_postgres.py`.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from sqlalchemy import text, update

from app.exceptions import ConflictError, ExternalServiceError, ExternalServiceTimeoutError
from app.models import TipoDocumento
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from app.services.ingesta import documento_service as servicio
from app.services.ingesta.almacenamiento import ConfigAlmacenamiento, EstadoSubida
from app.services.ingesta.almacenamiento_s3 import AlmacenOriginalesS3
from tests.ayudantes_ingesta import (
    BOM,
    ClienteS3Versionado,
    SesionSQLite,
    crear_empresa,
    crear_usuario,
    error_cliente,
    metadatos,
    sha256_de,
)

CONTENIDO = BOM + "# Memoria anual\r\nÑandú, acción y pingüino.\r\n".encode("utf-8")


def nuevo_almacen(cliente, **cambios) -> AlmacenOriginalesS3:
    """Cada adaptador nuevo es un «proceso reiniciado»: no conoce las subidas anteriores."""
    base = dict(
        endpoint_url="https://almacen.ejemplo.invalid", bucket="bucket-prueba", ambiente="development",
        access_key="ACCESO-FICTICIO", secret_key="SECRETO-FICTICIO",
        connect_timeout=5, read_timeout=30, operation_timeout=5,
    )
    base.update(cambios)
    return AlmacenOriginalesS3(ConfigAlmacenamiento(**base), cliente)


@pytest.fixture
async def e():
    db = SesionSQLite()
    usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
    empresa = await crear_empresa(db)
    cliente = ClienteS3Versionado()
    entorno = SimpleNamespace(db=db, usuario=usuario, empresa=empresa, cliente=cliente,
                              almacen=nuevo_almacen(cliente))
    yield entorno
    db.cerrar()


async def reservar(e, datos=CONTENIDO, *, anio=2025, tipo=TipoDocumento.MEMORIA_ANUAL):
    documento = await servicio.reservar_documento(
        e.db,
        metadatos=metadatos(e.empresa, anio, tipo),
        nombre_archivo="Memoria anual 2025.md",
        sha256=sha256_de(datos),
        tamano_bytes=len(datos),
        usuario_id=e.usuario.id,
        ambiente="development",
    )
    return documento, datos


def recargar(e, documento: Documento) -> Documento:
    e.db.sync.expire_all()
    return e.db.sync.get(Documento, documento.id)


async def conflicto(e, datos=CONTENIDO, **kwargs) -> ConflictError:
    with pytest.raises(ConflictError) as capturado:
        await reservar(e, datos, **kwargs)
    return capturado.value


async def fallido_con_intento(e, documento):
    """Deja el documento FALLIDO con un intento de subida registrado y compensación PENDIENTE,
    sin que el almacén haya intervenido (para probar la reconciliación de forma aislada)."""
    token = documento.ejecucion_token
    e.db.sync.execute(
        update(Documento).where(Documento.id == documento.id).values(almacenamiento_intentado_en=datetime.now(timezone.utc))
    )
    e.db.sync.commit()
    return await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", requiere_compensacion=True, token=token)


async def esperar_fin_de_subida(almacen, clave: str) -> EstadoSubida:
    async with asyncio.timeout(10):
        while (estado := (await almacen.estado_subida(clave)).estado) is EstadoSubida.EN_CURSO:
            await asyncio.sleep(0.005)
    return estado


def borrados(cliente) -> list[dict]:
    return [kw for metodo, kw in cliente.llamadas if metodo == "delete_object"]


# ============ Subida que termina DESPUÉS del timeout o de la cancelación ============

async def test_subida_que_termina_despues_del_timeout_mantiene_la_compensacion_pendiente_hasta_conocer_su_version(e):
    documento, datos = await reservar(e)
    token, clave = documento.ejecucion_token, documento.clave_original
    e.cliente.compuerta = threading.Event()
    almacen = nuevo_almacen(e.cliente, operation_timeout=0.05)

    with pytest.raises(ExternalServiceTimeoutError):
        await servicio.almacenar_original(e.db, almacen, documento.id, datos, token=token)

    # La corrutina terminó, pero el hilo del SDK sigue y el objeto AÚN no existe: no hay limpieza posible.
    pendiente = recargar(e, documento)
    assert pendiente.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert pendiente.estado_compensacion is EstadoCompensacion.PENDIENTE
    assert pendiente.reserva_activa is True
    assert pendiente.ultimo_error_compensacion == "STORAGE_UPLOAD_IN_FLIGHT"
    assert pendiente.compensacion_intentos == 1
    assert (await conflicto(e, datos)).code == "DOCUMENT_CLEANUP_PENDING"  # no se reintenta como si estuviera limpio
    assert e.cliente.versiones_de(clave) == []
    assert borrados(e.cliente) == []
    assert "head_object" not in e.cliente.metodos()  # ni DELETE ni HEAD «de prueba»

    # Un compensador que insista mientras sigue en curso no puede concluir nada.
    assert await servicio.compensar_documento(e.db, almacen, documento.id) is EstadoCompensacion.PENDIENTE
    assert recargar(e, documento).compensacion_intentos == 2

    # No es publicable: ni con el token viejo ni con ninguno.
    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO
        )
    assert capturado.value.code == "DOCUMENT_NOT_PUBLISHABLE"

    # La subida termina DESPUÉS: ahora existe, con una versión concreta que se conoce.
    e.cliente.compuerta.set()
    assert await esperar_fin_de_subida(almacen, clave) is EstadoSubida.CREADA
    creada = e.cliente.versiones_de(clave)
    assert len(creada) == 1

    assert await servicio.compensar_documento(e.db, almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert e.cliente.versiones_de(clave) == []  # sin versión residual ni marca de borrado
    assert [kw.get("VersionId") for kw in borrados(e.cliente)] == [creada[0]["VersionId"]]  # versión concreta
    limpio = recargar(e, documento)
    assert limpio.reserva_activa is False
    assert limpio.version_id_original == creada[0]["VersionId"]
    await reservar(e, datos)  # solo ahora se puede reintentar


async def test_subida_cancelada_que_termina_despues_se_limpia_cuando_la_recuperacion_conoce_su_desenlace(e):
    documento, datos = await reservar(e)
    clave = documento.clave_original
    e.cliente.compuerta = threading.Event()
    tarea = asyncio.create_task(
        servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    )
    await asyncio.to_thread(e.cliente.put_iniciado.wait, 5)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO

    ahora = datetime.now(timezone.utc)
    # Aún vigente: no se recupera aunque la tarea fue cancelada.
    assert (await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10, ahora=ahora)).abandonados == 0
    vencida = ahora + timedelta(days=1)
    resumen = await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10, ahora=vencida)
    assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (1, 0, 1)
    assert recargar(e, documento).ultimo_error_compensacion == "STORAGE_UPLOAD_IN_FLIGHT"
    assert (await conflicto(e, datos)).code == "DOCUMENT_CLEANUP_PENDING"

    e.cliente.compuerta.set()
    await esperar_fin_de_subida(e.almacen, clave)
    resumen = await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10, ahora=vencida)
    assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (0, 1, 0)
    assert e.cliente.versiones_de(clave) == []
    await reservar(e, datos)


# ============ Versionado: versión conocida y desconocida ============

async def test_con_la_version_conocida_se_elimina_y_se_verifica_esa_version_sin_listar_el_bucket(e):
    documento, datos = await reservar(e)
    token = documento.ejecucion_token
    guardado = await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token)
    version = guardado.version_id_original
    assert version is not None
    otra = e.cliente.poner(documento.clave_original, b"version ajena posterior")  # no debe tocarse
    e.cliente.llamadas.clear()

    await servicio.fallar_documento(e.db, documento.id, "VALIDACION_POSTERIOR", requiere_compensacion=True, token=token)
    assert recargar(e, documento).estado_compensacion is EstadoCompensacion.PENDIENTE
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA

    assert [kw["VersionId"] for kw in borrados(e.cliente)] == [version]
    comprobaciones = [kw for m, kw in e.cliente.llamadas if m == "head_object"]
    assert comprobaciones
    assert all(kw["VersionId"] == version for kw in comprobaciones)  # se verifica ESA versión
    assert "list_object_versions" not in e.cliente.metodos()
    assert [v["VersionId"] for v in e.cliente.versiones_de(documento.clave_original)] == [otra]  # la ajena sigue


async def test_con_version_desconocida_un_head_404_no_basta_hay_que_listar_y_borrar_por_version(e):
    """El objeto existe detrás de una marca de borrado: HEAD devuelve 404 aunque la versión siga."""
    documento, datos = await reservar(e)
    clave = documento.clave_original
    version = e.cliente.poner(clave, datos)
    e.cliente.delete_object(Bucket="b", Key=clave)  # marca de borrado encima de la versión
    with pytest.raises(ClientError) as ausente:
        e.cliente.head_object(Bucket="b", Key=clave)  # 404 «prueba» ausencia, pero es falso
    assert ausente.value.response["Error"]["Code"] == "404"
    assert len(e.cliente.versiones_de(clave)) == 2
    await fallido_con_intento(e, documento)
    e.cliente.llamadas.clear()

    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert e.cliente.versiones_de(clave) == []  # versión Y marca eliminadas por su id
    assert all("VersionId" in kw for kw in borrados(e.cliente))
    assert version in [kw["VersionId"] for kw in borrados(e.cliente)]
    assert recargar(e, documento).original_almacenado_en is not None  # constancia de que existió


async def test_tras_un_reinicio_sin_permiso_de_listado_la_compensacion_sigue_pendiente_y_no_toca_nada(e):
    documento, datos = await reservar(e)
    clave = documento.clave_original
    e.cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")
    e.cliente.permitir_listado = False
    with pytest.raises(ExternalServiceTimeoutError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert (await e.almacen.estado_subida(clave)).estado is EstadoSubida.INCIERTA
    assert len(e.cliente.versiones_de(clave)) == 1  # el servidor sí guardó el objeto

    reiniciado = nuevo_almacen(e.cliente)  # sin registro de la subida
    for intento in (2, 3):
        assert await servicio.compensar_documento(e.db, reiniciado, documento.id) is EstadoCompensacion.PENDIENTE
        pendiente = recargar(e, documento)
        assert pendiente.ultimo_error_compensacion == "STORAGE_RECONCILIATION_UNAVAILABLE"
        assert pendiente.compensacion_intentos == intento
        assert pendiente.reserva_activa is True
    assert borrados(e.cliente) == []
    assert len(e.cliente.versiones_de(clave)) == 1  # no se borró nada a ciegas
    assert (await conflicto(e, datos)).code == "DOCUMENT_CLEANUP_PENDING"

    e.cliente.permitir_listado = True  # se concede el permiso: la reconciliación ya puede probar
    assert await servicio.compensar_documento(e.db, reiniciado, documento.id) is EstadoCompensacion.COMPLETADA
    assert e.cliente.versiones_de(clave) == []
    assert all("VersionId" in kw for kw in borrados(e.cliente))  # nunca una eliminación sin versión
    await reservar(e, datos)


async def test_resultado_incierto_sin_hallazgos_no_se_declara_limpio_y_exige_intervencion_documentada(e):
    """El servidor pudo no haber recibido nada... o estar procesándolo aún. La ausencia no es prueba."""
    documento, datos = await reservar(e)
    clave = documento.clave_original
    e.cliente.errores["put_object"] = ReadTimeoutError(endpoint_url="https://x.invalid")  # antes de crear nada
    with pytest.raises(ExternalServiceTimeoutError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    for almacen in (e.almacen, nuevo_almacen(e.cliente)):  # con y sin registro del proceso
        assert await servicio.compensar_documento(e.db, almacen, documento.id) is EstadoCompensacion.PENDIENTE
        pendiente = recargar(e, documento)
        assert pendiente.ultimo_error_compensacion == "STORAGE_OUTCOME_UNCERTAIN"
        assert pendiente.reserva_activa is True
    assert "head_object" not in e.cliente.metodos()
    assert borrados(e.cliente) == []
    assert (await conflicto(e, datos)).code == "DOCUMENT_CLEANUP_PENDING"

    # Intervención documentada (07-almacenamiento-minio.md §8): tras comprobar a mano en MinIO que no hay
    # versiones ni marcas de esa clave, el operador libera la reserva. Debe respetar los CHECK de la tabla.
    e.db.sync.execute(
        text(
            "UPDATE documentos SET estado_compensacion = 'COMPLETADA', reserva_activa = 0, "
            "compensada_en = CURRENT_TIMESTAMP, ultimo_error_compensacion = NULL "
            "WHERE id = :id AND estado_procesamiento = 'FALLIDO' AND estado_compensacion = 'PENDIENTE'"
        ),
        {"id": documento.id.hex},
    )
    e.db.sync.commit()
    assert recargar(e, documento).reserva_activa is False
    await reservar(e, datos)
    assert clave  # la clave del intento anterior no se reutiliza: la nueva reserva tiene otra


async def test_una_marca_de_borrado_sola_no_prueba_que_el_objeto_existio(e):
    documento, datos = await reservar(e)
    clave = documento.clave_original
    e.cliente.entradas.append({"Key": clave, "VersionId": "m-previa", "Body": b"", "Marca": True})
    await fallido_con_intento(e, documento)
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.PENDIENTE
    assert recargar(e, documento).ultimo_error_compensacion == "STORAGE_OUTCOME_UNCERTAIN"
    assert borrados(e.cliente) == []
    assert len(e.cliente.versiones_de(clave)) == 1


async def test_si_se_interrumpe_tras_borrar_el_reintento_sabe_que_la_ausencia_es_limpieza(e):
    """Constancia durable de existencia ANTES de borrar: si falla la verificación, el reintento concluye."""

    class FallaElSegundoListado(ClienteS3Versionado):
        listados = 0

        def list_object_versions(self, **kw):
            type(self).listados += 1
            if type(self).listados == 2:
                raise error_cliente("InternalError", 500)
            return super().list_object_versions(**kw)

    FallaElSegundoListado.listados = 0
    e.cliente = FallaElSegundoListado()
    documento, datos = await reservar(e)
    clave = documento.clave_original
    e.cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")
    valor_nuevo_almacen = nuevo_almacen(e.cliente)
    with pytest.raises(ExternalServiceTimeoutError):
        await servicio.almacenar_original(
            e.db, valor_nuevo_almacen, documento.id, datos, token=documento.ejecucion_token
        )
    intermedio = recargar(e, documento)
    assert intermedio.estado_compensacion is EstadoCompensacion.PENDIENTE
    assert intermedio.reserva_activa is True
    assert intermedio.original_almacenado_en is not None
    assert e.cliente.versiones_de(clave) == []

    assert await servicio.compensar_documento(e.db, nuevo_almacen(e.cliente), documento.id) is EstadoCompensacion.COMPLETADA
    assert recargar(e, documento).reserva_activa is False


# ============ Objetos inesperados y ajenos ============

async def test_un_objeto_de_otro_tamano_o_mas_de_uno_no_se_borra(e):
    documento, datos = await reservar(e)
    clave = documento.clave_original
    ajena = e.cliente.poner(clave, b"contenido de otro tamano")
    await fallido_con_intento(e, documento)
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.PENDIENTE
    assert recargar(e, documento).ultimo_error_compensacion == "STORAGE_UNEXPECTED_OBJECT"
    assert [v["VersionId"] for v in e.cliente.versiones_de(clave)] == [ajena]
    assert borrados(e.cliente) == []

    e.cliente.poner(clave, datos)  # ahora dos versiones: ninguna se borra
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.PENDIENTE
    assert len(e.cliente.versiones_de(clave)) == 2
    assert borrados(e.cliente) == []


async def test_la_compensacion_no_toca_claves_vecinas_con_el_mismo_prefijo_ni_otros_documentos(e):
    documento, datos = await reservar(e)
    otro, otros_datos = await reservar(e, b"# Otro documento\n", anio=2024)
    e.almacen = nuevo_almacen(e.cliente)
    vecina = e.cliente.poner(documento.clave_original + ".bak", b"vecina")
    de_otro = e.cliente.poner(otro.clave_original, otros_datos)
    e.cliente.poner(documento.clave_original, datos)
    await fallido_con_intento(e, documento)

    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert e.cliente.versiones_de(documento.clave_original) == []
    assert [v["VersionId"] for v in e.cliente.versiones_de(documento.clave_original + ".bak")] == [vecina]
    assert [v["VersionId"] for v in e.cliente.versiones_de(otro.clave_original)] == [de_otro]


async def test_un_rechazo_definitivo_se_limpia_sin_borrar_y_un_objeto_inesperado_lo_impide(e):
    documento, datos = await reservar(e)
    e.cliente.errores["put_object"] = error_cliente("AccessDenied", 403)
    with pytest.raises(ExternalServiceError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    limpio = recargar(e, documento)  # compensación inmediata: NO_CREADA + comprobación de ausencia
    assert limpio.estado_compensacion is EstadoCompensacion.COMPLETADA
    assert limpio.reserva_activa is False
    assert borrados(e.cliente) == []
    assert "list_object_versions" not in e.cliente.metodos()

    segundo, datos2 = await reservar(e, b"# Segundo\n", anio=2024)
    e.cliente.poner(segundo.clave_original, b"aparecio algo")
    with pytest.raises(ExternalServiceError):
        await servicio.almacenar_original(e.db, e.almacen, segundo.id, datos2, token=segundo.ejecucion_token)
    assert recargar(e, segundo).ultimo_error_compensacion == "STORAGE_UNEXPECTED_OBJECT"
    assert recargar(e, segundo).reserva_activa is True
    assert len(e.cliente.versiones_de(segundo.clave_original)) == 1


async def test_sin_versionado_un_reinicio_reconcilia_borrando_la_unica_version_nula(e):
    e.cliente = ClienteS3Versionado(versionado=False)
    documento, datos = await reservar(e)
    token, clave = documento.ejecucion_token, documento.clave_original
    guardado = await servicio.almacenar_original(e.db, nuevo_almacen(e.cliente), documento.id, datos, token=token)
    assert guardado.version_id_original is None
    await servicio.fallar_documento(e.db, documento.id, "VALIDACION_POSTERIOR", requiere_compensacion=True, token=token)
    assert await servicio.compensar_documento(e.db, nuevo_almacen(e.cliente), documento.id) is EstadoCompensacion.COMPLETADA
    assert e.cliente.versiones_de(clave) == []


# ============ Vigencia: una operación lenta no se recupera ============

async def test_una_operacion_antigua_pero_vigente_no_se_considera_abandonada(e):
    documento, _ = await reservar(e)
    hace_tres_dias = datetime.now(timezone.utc) - timedelta(days=3)
    e.db.sync.execute(update(Documento).values(creado_en=hace_tres_dias))
    e.db.sync.commit()
    resumen = await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10)
    assert resumen.abandonados == 0
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO


async def test_el_ejecutor_que_renueva_su_vigencia_no_es_recuperado_y_el_que_no_renueva_si(e):
    documento, datos = await reservar(e)
    token = documento.ejecucion_token
    pasado = datetime.now(timezone.utc) - timedelta(minutes=1)
    e.db.sync.execute(update(Documento).values(ejecucion_vigente_hasta=pasado))
    e.db.sync.commit()

    nueva = await servicio.renovar_vigencia(e.db, documento.id, token=token, duracion=timedelta(minutes=5))
    assert nueva > datetime.now(timezone.utc)
    assert (await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10)).abandonados == 0

    e.db.sync.execute(update(Documento).values(ejecucion_vigente_hasta=pasado))
    e.db.sync.commit()
    assert (await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10)).abandonados == 1
    with pytest.raises(ConflictError):  # y el ejecutor desplazado ya no puede renovar
        await servicio.renovar_vigencia(e.db, documento.id, token=token)


async def test_el_ejecutor_anterior_no_puede_publicar_ni_registrar_nada_tras_una_recuperacion(e):
    documento, datos = await reservar(e)
    viejo = documento.ejecucion_token
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=viejo)
    clave = documento.clave_original
    # El ejecutor se queda colgado: su vigencia vence y otra instancia recupera la operación.
    e.db.sync.execute(update(Documento).values(ejecucion_vigente_hasta=datetime.now(timezone.utc) - timedelta(seconds=1)))
    e.db.sync.commit()
    resumen = await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10)
    assert (resumen.abandonados, resumen.compensados) == (1, 1)
    recuperado = recargar(e, documento)
    assert recuperado.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert recuperado.ejecucion_token != viejo

    # El ejecutor anterior despierta e intenta terminar su trabajo.
    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=viejo, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO
        )
    assert capturado.value.code == "DOCUMENT_NOT_PUBLISHABLE"
    for operacion in (
        servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=viejo),
        servicio.renovar_vigencia(e.db, documento.id, token=viejo),
    ):
        with pytest.raises(ConflictError):
            await operacion
    final = recargar(e, documento)
    assert final.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert final.disponible_para_rag is False
    assert e.cliente.versiones_de(clave) == []

    # Aunque el token anterior se reutilice con una reserva nueva, el nuevo ejecutor es otro.
    nuevo, _ = await reservar(e, datos)
    assert nuevo.ejecucion_token not in (viejo, final.ejecucion_token)
    with pytest.raises(ConflictError):
        await servicio.publicar_documento(
            e.db, nuevo.id, token=viejo, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO
        )


async def test_el_ejecutor_desplazado_durante_la_subida_no_registra_el_original_y_el_objeto_se_limpia(e):
    """La recuperación ocurre MIENTRAS el ejecutor viejo sube; cuando su subida termina, ya no es suyo."""
    documento, datos = await reservar(e)
    token, clave = documento.ejecucion_token, documento.clave_original
    e.cliente.compuerta = threading.Event()
    tarea = asyncio.create_task(servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token))
    await asyncio.to_thread(e.cliente.put_iniciado.wait, 5)

    vencida = datetime.now(timezone.utc) + timedelta(days=1)
    resumen = await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10, ahora=vencida)
    assert (resumen.abandonados, resumen.pendientes) == (1, 1)  # la subida sigue en curso: pendiente

    e.cliente.compuerta.set()
    with pytest.raises(ConflictError):  # el ejecutor viejo no puede registrar su original
        await tarea
    assert recargar(e, documento).original_almacenado_en is None
    assert e.cliente.versiones_de(clave) != []  # el objeto sí se creó

    resumen = await servicio.recuperar_documentos_pendientes(e.db, e.almacen, limite=10, ahora=vencida)
    assert resumen.compensados == 1
    assert e.cliente.versiones_de(clave) == []


async def test_una_operacion_vencida_no_empieza_a_subir(e):
    documento, datos = await reservar(e)
    e.db.sync.execute(update(Documento).values(ejecucion_vigente_hasta=datetime.now(timezone.utc) - timedelta(seconds=1)))
    e.db.sync.commit()
    with pytest.raises(ConflictError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert "put_object" not in e.cliente.metodos()
    assert recargar(e, documento).almacenamiento_intentado_en is None


async def test_el_intento_de_subida_extiende_la_vigencia_para_cubrir_la_subida(e):
    documento, datos = await reservar(e)
    antes = recargar(e, documento).ejecucion_vigente_hasta
    await servicio.almacenar_original(
        e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token, duracion_vigencia=timedelta(hours=2)
    )
    despues = recargar(e, documento).ejecucion_vigente_hasta
    assert despues - antes > timedelta(hours=1)


# ============ No sobrescritura: dos intentos del mismo documento ============

async def test_dos_intentos_simultaneos_de_almacenar_el_mismo_documento_solo_llegan_una_vez_al_almacen(e):
    documento, datos = await reservar(e)
    token = documento.ejecucion_token
    e.cliente.compuerta = threading.Event()
    primero = asyncio.create_task(servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token))
    await asyncio.to_thread(e.cliente.put_iniciado.wait, 5)

    with pytest.raises(ConflictError) as capturado:  # el segundo llega con la primera subida en vuelo
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token)
    assert capturado.value.code == "DOCUMENT_STATE_CONFLICT"
    e.cliente.compuerta.set()
    await primero

    assert e.cliente.metodos().count("put_object") == 1
    assert len(e.cliente.versiones_de(documento.clave_original)) == 1
    with pytest.raises(ConflictError):  # y tampoco uno tardío, con el original ya registrado
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token)
    assert e.cliente.metodos().count("put_object") == 1


# ============ Validaciones de parámetros ============

async def test_tokens_y_duraciones_invalidos(e):
    documento, datos = await reservar(e)
    token = documento.ejecucion_token
    with pytest.raises(TypeError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token="no-uuid")
    with pytest.raises(TypeError):
        await servicio.renovar_vigencia(e.db, documento.id, token=None)
    with pytest.raises(TypeError):
        await servicio.publicar_documento(
            e.db, documento.id, token=123, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO
        )
    for duracion in (timedelta(0), timedelta(seconds=-5), 60, None):
        with pytest.raises(ValueError):
            await servicio.renovar_vigencia(e.db, documento.id, token=token, duracion=duracion)
        with pytest.raises(ValueError):
            await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token, duracion_vigencia=duracion)
    token_ajeno = uuid.uuid4()
    with pytest.raises(ConflictError):
        await servicio.renovar_vigencia(e.db, documento.id, token=token_ajeno)  # token ajeno


async def test_fallar_exige_demostrar_propiedad_o_vigencia_vencida(e):
    documento, _ = await reservar(e)
    ahora = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA")  # sin ninguna de las dos
    with pytest.raises(ValueError):
        await servicio.fallar_documento(
            e.db, documento.id, "CARGA_CANCELADA", token=documento.ejecucion_token, vencido_antes_de=ahora
        )
    valor_datetime = datetime(2025, 1, 1)
    with pytest.raises(ValueError):
        await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", vencido_antes_de=valor_datetime)
    with pytest.raises(ConflictError):  # vigente: la recuperación no puede fallarlo
        await servicio.fallar_documento(e.db, documento.id, "RESERVA_ABANDONADA", vencido_antes_de=ahora)
    token_ajeno = uuid.uuid4()
    with pytest.raises(ConflictError):  # token ajeno
        await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", token=token_ajeno)
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO


async def test_un_token_ajeno_no_publica_ni_almacena_un_documento_en_proceso(e):
    """Aunque el documento siga EN_PROCESO, solo quien posee el token puede actuar sobre él."""
    documento, datos = await reservar(e)
    propio, ajeno = documento.ejecucion_token, uuid.uuid4()
    with pytest.raises(ConflictError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=ajeno)
    assert "put_object" not in e.cliente.metodos()
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=propio)

    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=ajeno, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO
        )
    assert capturado.value.details == {"faltantes": ["propiedad_de_la_operacion"]}
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO

    publicado = await servicio.publicar_documento(
        e.db, documento.id, token=propio, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO
    )
    assert publicado.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert publicado.disponible_para_rag


async def test_con_version_conocida_si_la_version_sigue_existiendo_tras_eliminar_no_se_declara_limpio(e):
    documento, datos = await reservar(e)
    token = documento.ejecucion_token
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token)
    e.cliente.delete_object = lambda **kw: e.cliente.llamadas.append(("delete_object", kw)) or {}  # sin efecto
    await servicio.fallar_documento(e.db, documento.id, "VALIDACION_POSTERIOR", requiere_compensacion=True, token=token)
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.PENDIENTE
    pendiente = recargar(e, documento)
    assert pendiente.ultimo_error_compensacion == "STORAGE_CLEANUP_NOT_CONFIRMED"
    assert pendiente.reserva_activa is True
    assert (await conflicto(e, datos)).code == "DOCUMENT_CLEANUP_PENDING"


async def test_aunque_el_adaptador_olvide_la_clave_el_servicio_sigue_impidiendo_la_segunda_subida(e):
    """La protección del adaptador depende de su registro en memoria (acotado, volátil). La duradera es
    el UPDATE condicional de la BD: se comprueba descartando el registro."""
    documento, datos = await reservar(e)
    token, clave = documento.ejecucion_token, documento.clave_original
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=token)
    e.almacen._subidas.clear()  # el adaptador «olvida» la clave (poda o reinicio)
    assert (await e.almacen.estado_subida(clave)).estado is EstadoSubida.SIN_REGISTRO

    for almacen in (e.almacen, nuevo_almacen(e.cliente)):  # mismo adaptador sin registro, y uno reiniciado
        with pytest.raises(ConflictError) as capturado:
            await servicio.almacenar_original(e.db, almacen, documento.id, datos, token=token)
        assert capturado.value.code == "DOCUMENT_STATE_CONFLICT"
    assert e.cliente.metodos().count("put_object") == 1
    assert len(e.cliente.versiones_de(clave)) == 1

    # Y se documenta el alcance real del adaptador: sin registro, él solo no lo impediría.
    await e.almacen.guardar(clave, datos, sha256_de(datos))
    assert e.cliente.metodos().count("put_object") == 2

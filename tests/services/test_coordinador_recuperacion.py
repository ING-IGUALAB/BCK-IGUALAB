"""Coordinador de ingesta: publicación pendiente, cancelación, pérdida de propiedad, escrituras tardías y
compensación CONJUNTA (base vectorial + MinIO) con su recuperación.

Bases REALES y aisladas (PostgreSQL `initdb` y pgvector en Docker); OCI y MinIO son dobles. Las garantías de
concurrencia entre la inserción y la limpieza vectorial se prueban contra pgvector real (bloqueo asesor y cierre).
Se OMITEN con motivo visible si el PostgreSQL local o Docker/pgvector no están disponibles.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.exceptions import ConflictError, ExternalServiceError
from app.models.documento_ingesta import EstadoCompensacion, EstadoProcesamiento
from app.services.ingesta import coordinador
from app.services.ingesta import documento_service as servicio
from app.services.ingesta import fragmentos_vectoriales as fv
from app.services.ingesta.coordinador import PublicacionVectorialPendiente, ingerir_documento
from tests.ayudantes_coordinador import (
    TEXTO_CON_HALLAZGOS,
    TEXTO_OBSERVADO,
    config_prueba,
    lector,
    lote_sintetico,
)
from tests.ayudantes_ingesta import AlmacenEnMemoria


def futuro(dias: int = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=dias)


async def ingerir(entorno, texto: str = TEXTO_CON_HALLAZGOS, **cambios):
    argumentos = dict(
        usuario_id=entorno.usuario.id, nombre_archivo="informe.md", leer=lector(texto.encode("utf-8")),
        metadatos=entorno.metadatos,
    )
    argumentos.update(cambios)
    return await ingerir_documento(entorno.deps, entorno.config, **argumentos)


async def esperar(condicion, segundos: float = 10.0):
    async with asyncio.timeout(segundos):
        while not condicion():
            await asyncio.sleep(0.01)


async def id_del_documento(entorno) -> uuid.UUID:
    (documento,) = await entorno.documentos()
    return documento.id


# ============================================ Publicación pendiente y su recuperación ============================================

async def publicacion_pendiente(entorno, **kwargs):
    """Deja un documento COMPLETADO cuya primera publicación vectorial falla."""
    entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado", veces=1, **kwargs)
    with pytest.raises(PublicacionVectorialPendiente) as capturado:
        await ingerir(entorno)
    entorno.vectorial.quitar()
    return capturado.value


async def test_fallo_al_publicar_deja_el_documento_completado_pero_pendiente_no_exitoso(entorno):
    error = await publicacion_pendiente(entorno)
    assert error.code == "VECTOR_PUBLICATION_PENDING" and error.details["causa"] == "VECTOR_PUBLICATION_FAILED"

    documento = await entorno.documento(error.documento_id)
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO and documento.resultado_analisis is not None
    assert documento.vector_publicado_en is None and documento.publicacion_vectorial_pendiente
    assert documento.vector_publicacion_intentos == 1 and documento.vector_ultimo_error == "VECTOR_PUBLICATION_FAILED"
    assert documento.etapa_actual == "PUBLICANDO"  # NUNCA FINALIZADO mientras esté pendiente
    # Los fragmentos existen pero siguen invisibles para la recuperación.
    conteo = await entorno.conteo_vectorial(documento.id)
    assert conteo.total == documento.fragmentos_total and conteo.publicados == 0
    assert await entorno.sql_vectorial("SELECT 1 FROM fragmentos_consultables") == []
    # El progreso no anuncia éxito ni resultado.
    async with entorno.fabrica_pg() as db:
        progreso = await servicio.obtener_progreso(db, documento.id)
    assert progreso.estado is servicio.EstadoProgreso.PUBLICACION_PENDIENTE
    assert not progreso.finalizado and progreso.resultado_analisis is None
    assert progreso.codigo_error == "VECTOR_PUBLICATION_FAILED" and progreso.etapa == "PUBLICANDO"
    # La reserva se conserva: un COMPLETADO sigue bloqueando duplicados.
    with pytest.raises(ConflictError) as duplicado:
        await ingerir(entorno)
    assert duplicado.value.code == "DOCUMENT_ALREADY_INGESTED"


async def test_reintentar_la_publicacion_es_idempotente_y_no_regenera_embeddings_ni_reanaliza(entorno, monkeypatch):
    error = await publicacion_pendiente(entorno)
    llamadas_proveedor = entorno.proveedor.llamadas
    reanalisis = []
    monkeypatch.setattr(coordinador, "analizar_documento", lambda doc: reanalisis.append(1))
    original_sube = list(entorno.almacen.llamadas)

    assert await coordinador.reintentar_publicacion(entorno.deps, error.documento_id) is True
    documento = await entorno.documento(error.documento_id)
    assert documento.vector_publicado_en is not None and documento.etapa_actual == "FINALIZADO"
    assert documento.vector_ultimo_error is None and not documento.publicacion_vectorial_pendiente
    conteo = await entorno.conteo_vectorial(documento.id)
    assert conteo.total == conteo.publicados == documento.fragmentos_total
    assert entorno.proveedor.llamadas == llamadas_proveedor and reanalisis == []
    assert entorno.almacen.llamadas == original_sube  # ni siquiera tocó MinIO

    publicado_en = documento.vector_publicado_en
    assert await coordinador.reintentar_publicacion(entorno.deps, error.documento_id) is False  # idempotente
    assert (await entorno.documento(error.documento_id)).vector_publicado_en == publicado_en
    async with entorno.fabrica_pg() as db:
        progreso = await servicio.obtener_progreso(db, documento.id)
    assert progreso.finalizado and progreso.estado is servicio.EstadoProgreso.COMPLETADO
    # Recuperable ahora (COMPLETADO + publicado) y con auditoría de la publicación confirmada.
    assert len(await entorno.sql_vectorial("SELECT 1 FROM fragmentos_consultables")) == documento.fragmentos_total
    detalles = [e.detalle for e in await entorno.auditoria()]
    assert sum("Publicación vectorial confirmada" in d for d in detalles) == 1


async def test_la_recuperacion_repite_las_publicaciones_pendientes_sin_regenerar_nada(entorno):
    error = await publicacion_pendiente(entorno)
    llamadas = entorno.proveedor.llamadas
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert (resumen.publicados, resumen.publicaciones_pendientes) == (1, 0)
    assert (await entorno.documento(error.documento_id)).vector_publicado_en is not None
    assert entorno.proveedor.llamadas == llamadas
    assert (await coordinador.ejecutar_recuperacion(entorno.deps)).publicados == 0  # ya no hay nada pendiente


async def test_si_la_publicacion_sigue_fallando_el_documento_sigue_recuperable(entorno):
    error = await publicacion_pendiente(entorno)
    entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado")
    with pytest.raises(PublicacionVectorialPendiente):
        await coordinador.reintentar_publicacion(entorno.deps, error.documento_id)
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert (resumen.publicados, resumen.publicaciones_pendientes) == (0, 1)
    documento = await entorno.documento(error.documento_id)
    assert documento.vector_publicacion_intentos == 3 and documento.publicacion_vectorial_pendiente
    entorno.vectorial.quitar()
    assert await coordinador.reintentar_publicacion(entorno.deps, error.documento_id) is True


async def test_publicar_sin_poder_confirmar_el_conteo_no_registra_la_publicacion(entorno):
    error = await publicacion_pendiente(entorno)
    await entorno.sql_vectorial("SELECT 1")
    async with entorno.fabrica_vectorial() as v:  # se pierde un fragmento: la publicación no puede confirmarse
        await v.execute(text("DELETE FROM fragmentos_documento WHERE indice = 0"))
        await v.commit()
    with pytest.raises(PublicacionVectorialPendiente) as capturado:
        await coordinador.reintentar_publicacion(entorno.deps, error.documento_id)
    assert capturado.value.details["causa"] == "VECTOR_PUBLICATION_NOT_CONFIRMED"
    documento = await entorno.documento(error.documento_id)
    assert documento.vector_publicado_en is None and documento.vector_ultimo_error == "VECTOR_PUBLICATION_NOT_CONFIRMED"


async def test_cancelar_durante_la_publicacion_deja_el_documento_completado_y_recuperable(entorno):
    entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado", accion="esperar", segundos=30)
    tarea = asyncio.create_task(ingerir(entorno))
    # La publicación empezó cuando la sentencia interceptada está en espera (el documento ya es COMPLETADO).
    await esperar(lambda: any(e.intervenciones >= 1 for e in entorno.vectorial.envolturas))
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    entorno.vectorial.quitar()
    documento = (await entorno.documentos())[0]
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO and documento.publicacion_vectorial_pendiente
    assert documento.reserva_activa and documento.motivo_fallo is None  # cancelar NO lo marca fallido
    assert (await entorno.conteo_vectorial(documento.id)).publicados == 0
    llamadas = entorno.proveedor.llamadas
    assert (await coordinador.ejecutar_recuperacion(entorno.deps)).publicados == 1
    assert entorno.proveedor.llamadas == llamadas


# ============================================ Cancelación y escrituras tardías ============================================

async def test_cancelar_durante_los_embeddings_no_toca_la_bd_y_la_recuperacion_limpia_todo(entorno):
    entorno.config = config_prueba(vigencia=timedelta(seconds=30), intervalo_latido=timedelta(milliseconds=100))
    entorno.proveedor.antes_del_lote[1] = lambda: asyncio.sleep(30)  # el lote 0 ya se guardó; se cancela dentro del 1
    tarea = asyncio.create_task(ingerir(entorno))
    await esperar(lambda: entorno.proveedor.llamadas >= 2)
    await asyncio.sleep(0.25)  # el latido renueva al menos una vez antes de cancelar
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    documento = (await entorno.documentos())[0]
    assert documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO  # la cancelación no la marca
    assert documento.reserva_activa and documento.vector_escritura_intentada_en is not None
    assert (await entorno.conteo_vectorial(documento.id)).total == 2  # un lote ya guardado, sin publicar

    # El latido se detuvo con la cancelación: la vigencia ya no se renueva (con latido vivo cambiaría cada 100 ms).
    vigente = (await entorno.documento(documento.id)).ejecucion_vigente_hasta
    await asyncio.sleep(0.4)
    assert (await entorno.documento(documento.id)).ejecucion_vigente_hasta == vigente

    # Mientras la vigencia no venza, la recuperación NO la toca (la antigüedad no es criterio).
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert (resumen.abandonados, resumen.compensados) == (0, 0)
    assert (await entorno.documento(documento.id)).estado_procesamiento is EstadoProcesamiento.EN_PROCESO

    resumen = await coordinador.ejecutar_recuperacion(entorno.deps, ahora=futuro())
    assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (1, 1, 0)
    documento = await entorno.documento(documento.id)
    assert documento.motivo_fallo == "RESERVA_ABANDONADA" and documento.analisis is None
    assert documento.estado_compensacion is EstadoCompensacion.COMPLETADA and documento.reserva_activa is False
    assert (await entorno.conteo_vectorial(documento.id)).total == 0
    assert (documento.clave_original, None) not in entorno.almacen.objetos
    # El fallo detectado por la recuperación se audita una vez, con el dueño de la ingesta como actor.
    (evento,) = [e for e in await entorno.auditoria() if "Ingesta fallida" in e.detalle]
    assert "motivo=RESERVA_ABANDONADA" in evento.detalle and "origen=recuperacion" in evento.detalle
    assert evento.usuario_id == entorno.usuario.id
    # Escritura tardía de un ejecutor anterior tras la limpieza: rechazada, no reintroduce contenido.
    async with entorno.fabrica_vectorial() as v:
        with pytest.raises(ConflictError) as tardia:
            await fv.guardar_lote(
                v, ambiente="development", documento_id=documento.id, empresa_id=entorno.empresa.id,
                anio=2025, tipo="MEMORIA_ANUAL", sector="MINERIA", lote=lote_sintetico(2, desde=2),
            )
    assert tardia.value.code == "FRAGMENTS_DOCUMENT_CLOSED"
    assert (await entorno.conteo_vectorial(documento.id)).total == 0


async def recuperacion_toma_la_operacion(entorno, completa: bool):
    """Simula que la recuperación tomó la operación (token rotado) mientras el ejecutor trabajaba; con `completa`,
    además terminó la compensación (limpieza + cierre)."""
    documento_id = await id_del_documento(entorno)
    async with entorno.fabrica_pg() as db:
        await servicio.fallar_documento(db, documento_id, "RESERVA_ABANDONADA", vencido_antes_de=futuro())
    if completa:
        async with entorno.fabrica_pg() as db, entorno.fabrica_vectorial() as v:
            assert await servicio.compensar_documento(db, entorno.almacen, documento_id, vectorial=v) is EstadoCompensacion.COMPLETADA


async def test_ejecutor_anterior_con_la_limpieza_ya_terminada_no_puede_reinsertar(entorno):
    entorno.proveedor.antes_del_lote[1] = lambda: recuperacion_toma_la_operacion(entorno, completa=True)
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "INGESTION_OWNERSHIP_LOST"
    documento = (await entorno.documentos())[0]
    # El ejecutor no tocó el estado: lo decidió quien recuperó. Y su escritura tardía no dejó nada.
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO and documento.motivo_fallo == "RESERVA_ABANDONADA"
    assert documento.estado_compensacion is EstadoCompensacion.COMPLETADA and documento.reserva_activa is False
    assert (await entorno.conteo_vectorial(documento.id)).total == 0
    assert documento.vector_publicado_en is None and documento.resultado_analisis is None
    assert entorno.proveedor.llamadas == 2  # no pidió más embeddings tras perder la propiedad


async def test_ejecutor_anterior_con_la_limpieza_aun_pendiente_la_deja_para_la_recuperacion(entorno):
    entorno.proveedor.antes_del_lote[1] = lambda: recuperacion_toma_la_operacion(entorno, completa=False)
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "INGESTION_OWNERSHIP_LOST"
    documento = (await entorno.documentos())[0]
    # El lote tardío pudo insertarse ANTES de la limpieza; esta lo elimina y cierra el documento.
    assert documento.estado_compensacion is EstadoCompensacion.PENDIENTE and documento.reserva_activa is True
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert resumen.compensados == 1
    assert (await entorno.conteo_vectorial(documento.id)).total == 0
    assert len(await entorno.sql_vectorial("SELECT 1 FROM cierres_documento WHERE documento_id = :d", d=documento.id)) == 1


async def test_el_latido_detecta_la_perdida_de_propiedad_y_detiene_el_trabajo_sin_insertar(entorno):
    entorno.config = config_prueba(vigencia=timedelta(seconds=5), intervalo_latido=timedelta(milliseconds=100))

    async def tomada_y_lenta():
        await recuperacion_toma_la_operacion(entorno, completa=False)
        await asyncio.sleep(0.6)  # el latido (cada 100 ms) detecta el token rotado mientras el proveedor «responde»

    entorno.proveedor.antes_del_lote[1] = tomada_y_lenta
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "INGESTION_OWNERSHIP_LOST"
    documento = (await entorno.documentos())[0]
    # Solo el lote 0 llegó a guardarse antes de perder la propiedad: el 1 NO se insertó (detenido por el latido).
    assert (await entorno.conteo_vectorial(documento.id)).total == 2
    assert entorno.proveedor.llamadas == 2
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO and documento.motivo_fallo == "RESERVA_ABANDONADA"


async def test_si_el_latido_no_puede_renovar_durante_una_vigencia_entera_se_detiene(entorno):
    entorno.config = config_prueba(vigencia=timedelta(milliseconds=500), intervalo_latido=timedelta(milliseconds=100))

    async def base_caida_y_lenta():
        entorno.transaccional.fallar_en_latido = True
        await asyncio.sleep(1.0)

    entorno.proveedor.antes_del_lote[1] = base_caida_y_lenta
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "INGESTION_OWNERSHIP_LOST"
    documento = (await entorno.documentos())[0]
    assert documento.estado_procesamiento.value == "EN_PROCESO"  # no se tocó el estado: lo decide la recuperación
    assert entorno.proveedor.llamadas == 2
    assert (await entorno.conteo_vectorial(documento.id)).total == 2


async def test_un_fallo_transitorio_del_latido_no_hace_perder_la_operacion(entorno):
    entorno.config = config_prueba(vigencia=timedelta(seconds=10), intervalo_latido=timedelta(milliseconds=100))

    async def fallo_breve():
        entorno.transaccional.fallar_en_latido = True
        await asyncio.sleep(0.4)
        entorno.transaccional.fallar_en_latido = False
        await asyncio.sleep(0.3)

    entorno.proveedor.antes_del_lote[1] = fallo_breve
    resultado = await ingerir(entorno)
    assert resultado.progreso.finalizado


async def recuperar_en_bucle(entorno, detener: asyncio.Event) -> dict:
    """Una recuperación concurrente que barre cada 50 ms: toma cualquier operación cuya vigencia venza."""
    totales = {"abandonados": 0, "barridos": 0}
    while not detener.is_set():
        resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
        totales["abandonados"] += resumen.abandonados
        totales["barridos"] += 1
        await asyncio.sleep(0.05)
    return totales


async def test_la_vigencia_no_es_una_duracion_maxima_un_trabajo_largo_que_renueva_termina(entorno):
    entorno.config = config_prueba(vigencia=timedelta(milliseconds=600), intervalo_latido=timedelta(milliseconds=100))
    entorno.proveedor.espera = 0.5  # varios lotes de 0,5 s: el total supera con creces la vigencia de 0,6 s
    detener = asyncio.Event()
    vigilante = asyncio.create_task(recuperar_en_bucle(entorno, detener))
    inicio = asyncio.get_running_loop().time()
    try:
        resultado = await ingerir(entorno)
    finally:
        detener.set()
        totales = await vigilante
    assert asyncio.get_running_loop().time() - inicio > 1.2 and resultado.progreso.finalizado
    assert totales["barridos"] >= 10 and totales["abandonados"] == 0  # la recuperación nunca la tomó
    assert entorno.transaccional.aperturas_del_latido >= 5  # renovó repetidamente, en sesiones propias del latido


async def test_sin_latido_la_misma_operacion_larga_pierde_la_vigencia_y_la_recuperacion_la_toma(entorno, monkeypatch):
    async def latido_inerte(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(coordinador._Latido, "_bucle", latido_inerte)
    entorno.config = config_prueba(vigencia=timedelta(milliseconds=600), intervalo_latido=timedelta(milliseconds=100))
    entorno.proveedor.espera = 0.5
    detener = asyncio.Event()
    vigilante = asyncio.create_task(recuperar_en_bucle(entorno, detener))
    try:
        with pytest.raises(ConflictError) as capturado:
            await ingerir(entorno)
    finally:
        detener.set()
        totales = await vigilante
    assert capturado.value.code == "INGESTION_OWNERSHIP_LOST" and totales["abandonados"] == 1
    documento = (await entorno.documentos())[0]
    assert documento.motivo_fallo == "RESERVA_ABANDONADA" and documento.vector_publicado_en is None
    assert documento.resultado_analisis is None  # la operación perdida jamás se completó


async def test_una_operacion_cancelada_pierde_su_vigencia_y_la_recuperacion_la_toma(entorno):
    entorno.config = config_prueba(vigencia=timedelta(milliseconds=300), intervalo_latido=timedelta(milliseconds=250))
    entorno.proveedor.antes_del_lote[1] = lambda: asyncio.sleep(30)
    tarea = asyncio.create_task(ingerir(entorno))
    await esperar(lambda: entorno.proveedor.llamadas >= 2)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    await asyncio.sleep(0.6)  # vence la vigencia (el latido ya no existe)
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert resumen.abandonados == 1 and resumen.compensados == 1


# ============================================ Compensación conjunta ============================================

async def fallido_con_vectores(entorno):
    """FALLIDO tras indexar parcialmente, con la limpieza vectorial bloqueada para que quede PENDIENTE."""
    entorno.proveedor.fallar_en_lote = 1
    entorno.vectorial.inyectar("DELETE FROM fragmentos_documento")
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    entorno.vectorial.quitar()
    entorno.proveedor.fallar_en_lote = None
    documento = (await entorno.documentos())[0]
    assert documento.estado_compensacion is EstadoCompensacion.PENDIENTE and documento.reserva_activa
    assert (await entorno.conteo_vectorial(documento.id)).total == 2 and (documento.clave_original, None) in entorno.almacen.objetos
    return documento.id


async def compensar(entorno, documento_id, *, con_vectorial: bool = True, fabrica=None):
    async with entorno.fabrica_pg() as db:
        if not con_vectorial:
            return await servicio.compensar_documento(db, entorno.almacen, documento_id)
        async with (fabrica or entorno.fabrica_vectorial)() as v:
            return await servicio.compensar_documento(db, entorno.almacen, documento_id, vectorial=v)


async def test_compensacion_conjunta_exitosa_limpia_ambas_y_libera_la_reserva(entorno):
    documento_id = await fallido_con_vectores(entorno)
    assert await compensar(entorno, documento_id) is EstadoCompensacion.COMPLETADA
    documento = await entorno.documento(documento_id)
    assert documento.reserva_activa is False and documento.estado_compensacion is EstadoCompensacion.COMPLETADA
    assert documento.compensada_en is not None and documento.ultimo_error_compensacion is None
    assert (await entorno.conteo_vectorial(documento_id)).total == 0
    assert (documento.clave_original, None) not in entorno.almacen.objetos
    assert await compensar(entorno, documento_id) is EstadoCompensacion.COMPLETADA  # idempotente


async def test_sin_la_base_vectorial_la_compensacion_no_puede_darse_por_terminada(entorno):
    documento_id = await fallido_con_vectores(entorno)
    assert await compensar(entorno, documento_id, con_vectorial=False) is EstadoCompensacion.PENDIENTE
    documento = await entorno.documento(documento_id)
    assert documento.ultimo_error_compensacion == "VECTOR_CLEANUP_UNAVAILABLE" and documento.reserva_activa
    assert (await entorno.conteo_vectorial(documento_id)).total == 2  # nada se limpió a medias sin poder confirmarlo
    assert (documento.clave_original, None) in entorno.almacen.objetos


async def test_si_la_limpieza_vectorial_falla_la_reserva_se_conserva_y_minio_no_se_toca(entorno):
    documento_id = await fallido_con_vectores(entorno)
    entorno.vectorial.inyectar("DELETE FROM fragmentos_documento")
    assert await compensar(entorno, documento_id, fabrica=entorno.vectorial) is EstadoCompensacion.PENDIENTE
    documento = await entorno.documento(documento_id)
    assert documento.ultimo_error_compensacion == "VECTOR_CLEANUP_FAILED" and documento.reserva_activa
    assert documento.compensacion_intentos >= 2 and (documento.clave_original, None) in entorno.almacen.objetos
    assert [c for c in entorno.almacen.llamadas if c[0] == "eliminar"] == []


async def test_si_la_limpieza_vectorial_no_borra_nada_no_se_confirma(entorno):
    documento_id = await fallido_con_vectores(entorno)
    entorno.vectorial.inyectar("DELETE FROM fragmentos_documento", accion="omitir")
    assert await compensar(entorno, documento_id, fabrica=entorno.vectorial) is EstadoCompensacion.PENDIENTE
    documento = await entorno.documento(documento_id)
    assert documento.ultimo_error_compensacion == "VECTOR_CLEANUP_NOT_CONFIRMED" and documento.reserva_activa
    assert (await entorno.conteo_vectorial(documento_id)).total == 2


async def test_si_no_se_puede_contar_tras_limpiar_no_se_confirma(entorno):
    documento_id = await fallido_con_vectores(entorno)
    entorno.vectorial.inyectar("count(*)")
    assert await compensar(entorno, documento_id, fabrica=entorno.vectorial) is EstadoCompensacion.PENDIENTE
    assert (await entorno.documento(documento_id)).ultimo_error_compensacion == "VECTOR_CLEANUP_FAILED"
    assert (await entorno.documento(documento_id)).reserva_activa


async def test_vectorial_limpio_pero_minio_incierto_conserva_pendiente_y_reserva(entorno):
    documento_id = await fallido_con_vectores(entorno)
    entorno.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "no disponible")]
    assert await compensar(entorno, documento_id) is EstadoCompensacion.PENDIENTE
    documento = await entorno.documento(documento_id)
    assert documento.reserva_activa and documento.estado_compensacion is EstadoCompensacion.PENDIENTE
    assert (await entorno.conteo_vectorial(documento_id)).total == 0  # lo vectorial sí quedó limpio y cerrado
    assert (documento.clave_original, None) in entorno.almacen.objetos  # el original sigue: nada declarado limpio
    # Reintento: ahora sí.
    assert await compensar(entorno, documento_id) is EstadoCompensacion.COMPLETADA
    assert (await entorno.documento(documento_id)).reserva_activa is False


async def test_subida_incierta_respeta_las_garantias_de_minio_al_compensar_en_conjunto(entorno):
    from app.services.ingesta.almacenamiento import EstadoSubida, SubidaConocida

    documento_id = await fallido_con_vectores(entorno)
    clave = (await entorno.documento(documento_id)).clave_original
    entorno.almacen.subidas[clave] = SubidaConocida(EstadoSubida.EN_CURSO)  # el objeto aún puede aparecer
    assert await compensar(entorno, documento_id) is EstadoCompensacion.PENDIENTE
    documento = await entorno.documento(documento_id)
    assert documento.ultimo_error_compensacion == "STORAGE_UPLOAD_IN_FLIGHT" and documento.reserva_activa


async def test_la_recuperacion_aplica_la_misma_regla_conjunta(entorno):
    documento_id = await fallido_con_vectores(entorno)
    async with entorno.fabrica_pg() as db:
        sin_vectorial = await servicio.recuperar_documentos_pendientes(db, entorno.almacen, limite=10)
    assert (sin_vectorial.compensados, sin_vectorial.pendientes) == (0, 1)
    assert (await entorno.documento(documento_id)).reserva_activa
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert (resumen.compensados, resumen.pendientes) == (1, 0)
    assert (await entorno.documento(documento_id)).reserva_activa is False


async def test_un_documento_que_nunca_escribio_vectores_no_necesita_la_base_vectorial(entorno):
    entorno.almacen.guardar_y_fallar = True  # la subida falla pero el objeto pudo crearse: resultado incierto
    entorno.almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_ERROR", "x")]
    # Dos fallos: el intento inmediato de `almacenar_original` y el reintento del coordinador.
    entorno.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "x")] * 2
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    documento = (await entorno.documentos())[0]
    assert documento.vector_escritura_intentada_en is None
    assert documento.estado_compensacion is EstadoCompensacion.PENDIENTE and documento.reserva_activa
    async with entorno.fabrica_pg() as db:
        resumen = await servicio.recuperar_documentos_pendientes(db, entorno.almacen, limite=10)  # SIN vectorial
    assert (resumen.compensados, resumen.pendientes) == (1, 0)
    assert (await entorno.documento(documento.id)).reserva_activa is False


async def test_el_reintento_se_permite_solo_despues_de_confirmar_la_limpieza_conjunta(entorno):
    documento_id = await fallido_con_vectores(entorno)
    with pytest.raises(ConflictError) as bloqueado:
        await ingerir(entorno)
    assert bloqueado.value.code == "DOCUMENT_CLEANUP_PENDING"
    assert await compensar(entorno, documento_id) is EstadoCompensacion.COMPLETADA
    resultado = await ingerir(entorno)
    assert resultado.documento_id != documento_id and resultado.progreso.finalizado
    # El documento nuevo es independiente del cerrado: sus fragmentos se publicaron.
    assert (await entorno.conteo_vectorial(resultado.documento_id)).publicados == resultado.fragmentos
    assert (await entorno.conteo_vectorial(documento_id)).total == 0


# ============================================ Cierre vectorial y escrituras tardías ============================================

async def test_guardar_tras_cerrar_se_rechaza_y_el_cierre_es_idempotente_y_por_ambiente(entorno):
    documento = uuid.uuid4()
    argumentos = dict(documento_id=documento, empresa_id=entorno.empresa.id, anio=2025, tipo="MEMORIA_ANUAL", sector="MINERIA")
    async with entorno.fabrica_vectorial() as v:
        await fv.guardar_lote(v, ambiente="development", lote=lote_sintetico(2), **argumentos)
        await fv.guardar_lote(v, ambiente="qa", lote=lote_sintetico(2), **argumentos)
        assert await fv.cerrar_y_eliminar_fragmentos(v, ambiente="development", documento_id=documento) == 2
        assert await fv.cerrar_y_eliminar_fragmentos(v, ambiente="development", documento_id=documento) == 0
        with pytest.raises(ConflictError) as capturado:
            await fv.guardar_lote(v, ambiente="development", lote=lote_sintetico(2, desde=2), **argumentos)
        assert capturado.value.code == "FRAGMENTS_DOCUMENT_CLOSED"
        # El cierre es por ambiente: «qa» no se ve afectado y puede seguir escribiendo.
        assert (await fv.contar_fragmentos(v, ambiente="qa", documento_id=documento)).total == 2
        await fv.guardar_lote(v, ambiente="qa", lote=lote_sintetico(1, desde=2), **argumentos)
        assert (await fv.contar_fragmentos(v, ambiente="qa", documento_id=documento)).total == 3
        assert (await fv.contar_fragmentos(v, ambiente="development", documento_id=documento)).total == 0


async def test_un_documento_cerrado_no_se_publica_aunque_queden_filas(entorno):
    documento = uuid.uuid4()
    argumentos = dict(documento_id=documento, empresa_id=entorno.empresa.id, anio=2025, tipo="MEMORIA_ANUAL", sector="MINERIA")
    async with entorno.fabrica_vectorial() as v:
        await fv.guardar_lote(v, ambiente="development", lote=lote_sintetico(2), **argumentos)
        await v.execute(text("INSERT INTO cierres_documento (ambiente, documento_id) VALUES ('development', :d)"), {"d": documento})
        await v.commit()
        assert await fv.publicar_fragmentos(v, ambiente="development", documento_id=documento) == 0
        assert (await fv.contar_fragmentos(v, ambiente="development", documento_id=documento)).publicados == 0


async def test_la_insercion_y_la_limpieza_concurrentes_nunca_dejan_filas_tardias(entorno):
    """30 carreras reales contra pgvector: o el lote termina antes y la limpieza lo borra, o ve el cierre y se
    rechaza. En ningún caso queda contenido después de la limpieza (bloqueo asesor + cierre)."""
    rechazadas = aceptadas = 0
    for i in range(30):
        documento = uuid.uuid4()
        argumentos = dict(
            ambiente="development", documento_id=documento, empresa_id=entorno.empresa.id, anio=2025,
            tipo="MEMORIA_ANUAL", sector="MINERIA",
        )
        async with entorno.fabrica_vectorial() as v1, entorno.fabrica_vectorial() as v2:
            if i % 2:
                await asyncio.sleep(0)  # alterna quién arranca primero
            resultados = await asyncio.gather(
                fv.guardar_lote(v1, lote=lote_sintetico(3), **argumentos),
                fv.cerrar_y_eliminar_fragmentos(v2, ambiente="development", documento_id=documento),
                return_exceptions=True,
            )
        insercion, limpieza = resultados
        assert limpieza == (limpieza if isinstance(limpieza, int) else None) and not isinstance(limpieza, Exception)
        if isinstance(insercion, ConflictError):
            assert insercion.code == "FRAGMENTS_DOCUMENT_CLOSED"
            rechazadas += 1
        else:
            assert insercion == 3
            aceptadas += 1
        async with entorno.fabrica_vectorial() as v:
            assert (await fv.contar_fragmentos(v, ambiente="development", documento_id=documento)).total == 0
            # Y nada puede reintroducirse después.
            with pytest.raises(ConflictError):
                await fv.guardar_lote(v, lote=lote_sintetico(1), **argumentos)
    assert rechazadas + aceptadas == 30


async def test_el_latido_renueva_con_sesiones_propias_y_la_ingesta_termina(entorno):
    entorno.config = config_prueba(vigencia=timedelta(seconds=2), intervalo_latido=timedelta(milliseconds=100))
    entorno.proveedor.espera = 0.15
    resultado = await ingerir(entorno)
    assert resultado.progreso.finalizado
    aperturas = entorno.transaccional.aperturas
    # Cada renovación abre y cierra SU sesión dentro de la tarea del latido; el ejecutor usa la suya.
    assert aperturas.count("latido-ingesta") >= 3
    assert aperturas.count("latido-ingesta") < len(aperturas) and aperturas[0] != "latido-ingesta"

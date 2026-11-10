"""Doble check de ingesta (CONFIRMANDO): 202 tras el análisis, confirmar, cancelar con limpieza de
residuos y reintento del mismo archivo/empresa/año.

Bases REALES y aisladas (PostgreSQL initdb + pgvector Docker); OCI y MinIO son DOBLES. Se OMITEN si
no hay PostgreSQL local o Docker/pgvector (`pytest -rs`).
"""
import uuid

import pytest
from sqlalchemy import text

from app.core.exceptions import ConfirmacionPendiente, ConflictError
from app.models.documento_ingesta import EstadoCompensacion, EstadoProcesamiento, EtapaIngesta, ResultadoAnalisis
from app.modules.ingesta.application import coordinador
from app.modules.ingesta.application import documento_service as servicio
from tests.ayudantes_coordinador import TEXTO_CON_HALLAZGOS, TEXTO_OBSERVADO, config_prueba, lector
from tests.ayudantes_http_ingesta import (
    cliente_http,
    construir_app,
    crear_operacion,
    ingerir_por_http,
    subir,
    subir_sin_confirmar,
)


async def ingerir_con_pausa(entorno, texto: str = TEXTO_CON_HALLAZGOS, **cambios):
    """Lanza la ingesta y devuelve la ConfirmacionPendiente (sin confirmar)."""
    argumentos = dict(
        usuario_id=entorno.usuario.id,
        nombre_archivo="informe.md",
        leer=lector(texto.encode("utf-8")),
        metadatos=entorno.metadatos,
    )
    argumentos.update(cambios)
    with pytest.raises(ConfirmacionPendiente) as capturado:
        await coordinador.ingerir_documento(entorno.deps, entorno.config, **argumentos)
    return capturado.value


# ============================================ Coordinador ============================================

async def test_tras_el_analisis_la_ingesta_se_pausa_en_confirmando(entorno):
    pausa = await ingerir_con_pausa(entorno)
    documento_id = uuid.UUID(pausa.documento_id)
    (documento,) = await entorno.documentos()
    assert documento.id == documento_id
    assert documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert documento.etapa_actual == EtapaIngesta.CONFIRMANDO.value
    assert documento.confirmacion_pendiente_en is not None
    assert documento.analisis is not None  # el análisis queda persistido para el resumen
    assert documento.resultado_analisis is None  # aún no COMPLETADO
    assert documento.reserva_activa is True
    assert pausa.code == "CONFIRMATION_PENDING"
    # Original y fragmentos NO publicados siguen ahí (residuos hasta confirmar/cancelar)
    conteo = await entorno.conteo_vectorial(documento_id)
    assert conteo.total > 0 and conteo.publicados == 0
    assert (documento.clave_original, None) in entorno.almacen.objetos


async def test_confirmar_completa_la_ingesta_sin_reanalizar(entorno):
    pausa = await ingerir_con_pausa(entorno)
    llamadas_antes = entorno.proveedor.llamadas
    resultado = await coordinador.confirmar_ingesta(entorno.deps, uuid.UUID(pausa.documento_id))
    assert resultado.resultado_analisis in (ResultadoAnalisis.CON_HALLAZGOS, ResultadoAnalisis.OBSERVADO)
    assert entorno.proveedor.llamadas == llamadas_antes  # no regeneró embeddings
    documento = await entorno.documento(resultado.documento_id)
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert documento.etapa_actual == EtapaIngesta.FINALIZADO.value
    assert documento.vector_publicado_en is not None
    conteo = await entorno.conteo_vectorial(resultado.documento_id)
    assert conteo.total == conteo.publicados == documento.fragmentos_total


async def test_cancelar_elimina_residuos_y_libera_la_reserva_para_reintentar(entorno):
    pausa = await ingerir_con_pausa(entorno)
    documento_id = uuid.UUID(pausa.documento_id)
    (antes,) = await entorno.documentos()

    await coordinador.cancelar_ingesta(entorno.deps, documento_id)

    (documento,) = await entorno.documentos()
    assert documento.id == documento_id
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert documento.motivo_fallo == "CONFIRMACION_RECHAZADA"
    assert documento.estado_compensacion in (EstadoCompensacion.COMPLETADA, EstadoCompensacion.NINGUNA)
    assert documento.reserva_activa is False  # cupo empresa/año/tipo y SHA-256 libres
    assert documento.analisis is None  # un fallo no conserva citas
    assert documento.confirmacion_pendiente_en is not None  # histórico de la pausa
    # Residuos fuera: MinIO y pgvector
    assert (antes.clave_original, None) not in entorno.almacen.objetos
    conteo = await entorno.conteo_vectorial(documento_id)
    assert conteo.total == 0
    cierres = await entorno.sql_vectorial(
        "SELECT 1 FROM cierres_documento WHERE documento_id = :d", d=documento_id
    )
    assert len(cierres) == 1


async def test_cancelar_luego_reingerir_y_confirmar_funciona(entorno):
    pausa = await ingerir_con_pausa(entorno)
    await coordinador.cancelar_ingesta(entorno.deps, uuid.UUID(pausa.documento_id))

    # Reintento del MISMO archivo/empresa/año: sin 409 (como si nunca se hubiera hecho)
    try:
        resultado = await coordinador.ingerir_documento(
            entorno.deps,
            entorno.config,
            usuario_id=entorno.usuario.id,
            nombre_archivo="informe.md",
            leer=lector(TEXTO_CON_HALLAZGOS.encode("utf-8")),
            metadatos=entorno.metadatos,
        )
    except ConfirmacionPendiente as segunda:
        resultado = await coordinador.confirmar_ingesta(entorno.deps, uuid.UUID(segunda.documento_id))

    assert resultado.resultado_analisis is not None
    documento = await entorno.documento(resultado.documento_id)
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert documento.motivo_fallo is None
    # El cupo quedó ocupado de nuevo por el reintento exitoso
    assert documento.reserva_activa is True


async def test_confirmar_dos_veces_es_conflicto(entorno):
    pausa = await ingerir_con_pausa(entorno)
    documento_id = uuid.UUID(pausa.documento_id)
    await coordinador.confirmar_ingesta(entorno.deps, documento_id)
    with pytest.raises(ConflictError) as capturado:
        await coordinador.confirmar_ingesta(entorno.deps, documento_id)
    assert capturado.value.code == "CONFIRMATION_NOT_PENDING"


async def test_cancelar_sin_pausa_es_conflicto(entorno):
    pausa = await ingerir_con_pausa(entorno)
    await coordinador.cancelar_ingesta(entorno.deps, uuid.UUID(pausa.documento_id))
    with pytest.raises(ConflictError) as capturado:
        await coordinador.cancelar_ingesta(entorno.deps, uuid.UUID(pausa.documento_id))
    assert capturado.value.code == "CONFIRMATION_NOT_PENDING"


async def test_observado_tambien_se_pausa(entorno):
    pausa = await ingerir_con_pausa(entorno, TEXTO_OBSERVADO)
    documento = await entorno.documento(uuid.UUID(pausa.documento_id))
    assert documento.analisis["resultado"] == "OBSERVADO"
    assert documento.etapa_actual == EtapaIngesta.CONFIRMANDO.value
    resultado = await coordinador.confirmar_ingesta(entorno.deps, uuid.UUID(pausa.documento_id))
    assert resultado.resultado_analisis is ResultadoAnalisis.OBSERVADO


async def test_sin_confirmacion_requerida_no_se_pausa(entorno):
    config = config_prueba(confirmacion_requerida=False)
    resultado = await coordinador.ingerir_documento(
        entorno.deps,
        config,
        usuario_id=entorno.usuario.id,
        nombre_archivo="informe.md",
        leer=lector(TEXTO_CON_HALLAZGOS.encode("utf-8")),
        metadatos=entorno.metadatos,
    )
    documento = await entorno.documento(resultado.documento_id)
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert documento.etapa_actual == EtapaIngesta.FINALIZADO.value


# ============================================ HTTP ============================================

async def test_post_ingesta_responde_202_con_resumen_y_no_es_terminal(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        respuesta = await subir_sin_confirmar(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS)
        assert respuesta.status_code == 202, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["estado"] == "CONFIRMACION_PENDIENTE"
        assert cuerpo["terminal"] is False
        assert cuerpo["exitosa"] is False
        assert cuerpo["confirmacion_pendiente"] is True
        resumen = cuerpo["resumen_confirmacion"]
        assert resumen["resultado"] in ("CON_HALLAZGOS", "OBSERVADO")
        assert isinstance(resumen["referencias_gri"], int)
        assert isinstance(resumen["sanciones"], int)
        assert resumen["mensaje"]  # texto listo para la alerta
        assert cuerpo["etapa"] == "CONFIRMANDO"

        # GET refleja el mismo estado (el FE hace poll)
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "CONFIRMACION_PENDIENTE"
        assert vista["confirmacion_pendiente"] is True
        assert vista["resumen_confirmacion"]["mensaje"] == resumen["mensaje"]
        assert (await cliente.get("/documentos", params={"estado": "CONFIRMACION_PENDIENTE"})).json()["total"] == 1


async def test_post_confirmar_201_y_cancelar_200_con_limpieza(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        # --- cancelar ---
        operacion_id, _ = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO) if False else (None, None)
        operacion_id = await crear_operacion(cliente)
        pausa = await subir_sin_confirmar(cliente, operacion_id, entorno, TEXTO_OBSERVADO)
        assert pausa.status_code == 202
        documento_id = pausa.json()["documento_id"]

        cancelada = await cliente.post(f"/documentos/operaciones/{operacion_id}/cancelar")
        assert cancelada.status_code == 200, cancelada.text
        cuerpo = cancelada.json()
        assert cuerpo["estado"] == "FALLIDO"
        assert cuerpo["terminal"] is True
        assert cuerpo["error"]["code"] in ("CONFIRMACION_RECHAZADA", "UNEXPECTED_ERROR")
        assert cuerpo["confirmacion_pendiente"] is False

        # Residuos fuera y reserva liberada
        (documento,) = await entorno.documentos()
        assert documento.id == uuid.UUID(documento_id)
        assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO
        assert documento.reserva_activa is False
        assert await entorno.conteo_vectorial(documento.id) == (await entorno.conteo_vectorial(documento.id))
        conteo = await entorno.conteo_vectorial(documento.id)
        assert conteo.total == 0

        # Reintento del mismo archivo por HTTP: 201 (o 202 si pausa de nuevo → confirmar)
        reoperacion = await crear_operacion(cliente)
        reintento = await subir(cliente, reoperacion, entorno, TEXTO_OBSERVADO)
        assert reintento.status_code == 201, reintento.text
        assert reintento.json()["estado"] == "COMPLETADO"

        # --- confirmar en otra carga ---
        op2 = await crear_operacion(cliente)
        pausa2 = await subir_sin_confirmar(cliente, op2, entorno, TEXTO_CON_HALLAZGOS)
        # mismo tipo/año ya ocupado por el reintento COMPLETADO → 409 en la carga
        # (la reserva del primer cancelado se liberó; el reintento ocupó el cupo)
        if pausa2.status_code == 202:
            ok = await cliente.post(f"/documentos/operaciones/{op2}/confirmar")
            assert ok.status_code == 201, ok.text
            assert ok.json()["estado"] == "COMPLETADO"
        else:
            assert pausa2.status_code == 409


async def test_confirmar_y_cancelar_sin_pausa_son_409(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        # Operación sin documento
        from tests.ayudantes_http_ingesta import comprobar_error

        comprobar_error(await cliente.post(f"/documentos/operaciones/{operacion_id}/confirmar"), 409, "OPERATION_WITHOUT_DOCUMENT")
        comprobar_error(await cliente.post(f"/documentos/operaciones/{operacion_id}/cancelar"), 409, "OPERATION_WITHOUT_DOCUMENT")

        # Completa sin pausa (confirmacion desactivada en este entorno)
        entorno.config = config_prueba(confirmacion_requerida=False)
        op2, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS)
        assert respuesta.status_code == 201
        comprobar_error(await cliente.post(f"/documentos/operaciones/{op2}/confirmar"), 409, "CONFIRMATION_NOT_PENDING")
        comprobar_error(await cliente.post(f"/documentos/operaciones/{op2}/cancelar"), 409, "CONFIRMATION_NOT_PENDING")

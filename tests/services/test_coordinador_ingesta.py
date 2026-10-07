"""Coordinador síncrono de ingesta: flujo completo, rechazos, fallos, progreso y auditoría.

Bases REALES y aisladas (PostgreSQL transaccional con `initdb` y pgvector en un contenedor desechable); OCI y
MinIO son DOBLES. Si el PostgreSQL local o Docker/pgvector no están disponibles, estas pruebas se OMITEN con
motivo visible (`pytest -rs`) y no cuentan como ejecutadas. Nada toca bases compartidas.
"""
import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.exceptions import BusinessValidationError, ConflictError, ExternalServiceError
from app.models import SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import (
    EstadoCompensacion,
    EstadoProcesamiento,
    EtapaIngesta,
    ResultadoAnalisis,
)
from app.services.ingesta import coordinador
from app.services.ingesta import documento_service as servicio
from app.services.ingesta.analisis_ingesta import AnalisisIngestaError
from app.services.ingesta.coordinador import PublicacionVectorialPendiente, ingerir_documento
from app.services.ingesta.embeddings import IdentidadEmbeddings
from tests.ayudantes_coordinador import (
    TABLA_DETERIORADA,
    TEXTO_CON_HALLAZGOS,
    TEXTO_OBSERVADO,
    ProveedorDoble,
    config_prueba,
    lector,
)
from tests.ayudantes_ingesta import BOM, crear_empresa, metadatos, sha256_de


async def ingerir(entorno, texto: str | bytes = TEXTO_CON_HALLAZGOS, nombre: str = "informe.md", **cambios):
    datos = texto if isinstance(texto, bytes) else texto.encode("utf-8")
    argumentos = dict(
        usuario_id=entorno.usuario.id, nombre_archivo=nombre, leer=lector(datos), metadatos=entorno.metadatos
    )
    argumentos.update(cambios)
    return await ingerir_documento(entorno.deps, entorno.config, **argumentos)


# ============================================ Flujo completo ============================================

async def test_documento_con_hallazgos_completa_publica_y_persiste_el_analisis(entorno):
    resultado = await ingerir(entorno)

    assert resultado.resultado_analisis is ResultadoAnalisis.CON_HALLAZGOS
    assert resultado.motivos == ("referencias_gri_catalogadas", "sanciones_economicas")
    documento = await entorno.documento(resultado.documento_id)
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert documento.resultado_analisis is ResultadoAnalisis.CON_HALLAZGOS
    assert documento.etapa_actual == "FINALIZADO" and documento.vector_publicado_en is not None
    assert documento.reserva_activa and documento.estado_compensacion is EstadoCompensacion.NINGUNA
    assert documento.vector_escritura_intentada_en is not None and documento.vector_publicacion_intentos == 0
    assert documento.disponible_para_rag and not documento.publicacion_vectorial_pendiente

    # Original conservado en el almacén (bytes exactos).
    assert entorno.almacen.objetos[(documento.clave_original, None)] == TEXTO_CON_HALLAZGOS.encode("utf-8")
    # Fragmentos: todos persistidos y PUBLICADOS en la base vectorial.
    assert documento.fragmentos_total and documento.fragmentos_total >= 3
    conteo = await entorno.conteo_vectorial(documento.id)
    assert (conteo.total, conteo.publicados) == (documento.fragmentos_total, documento.fragmentos_total)
    assert documento.fragmentos_procesados == documento.fragmentos_total == resultado.fragmentos
    # Análisis completo y consistente con la clasificación.
    analisis = documento.analisis
    assert analisis["resultado"] == documento.resultado_analisis.value
    assert analisis["version_catalogo"] == "2026-10-07.2"
    assert {g["codigo"] for g in analisis["gri"]} == {"305"}
    (sancion,) = analisis["sanciones"]
    assert sancion["monto"]["texto"] == "S/ 12,500" and sancion["entidad"]["texto"] == "OEFA"
    assert TEXTO_CON_HALLAZGOS[sancion["cita_inicio"]:sancion["cita_fin"]] == sancion["cita"]
    assert [a["codigo"] for a in documento.advertencias] == [a["codigo"] for a in analisis["advertencias"]]
    # Recuperable: COMPLETADO + empresa activa (transaccional) y publicado (vectorial).
    async with entorno.fabrica_pg() as db:
        assert await servicio.documentos_recuperables(db, ambiente="development", documento_ids=[documento.id]) == {documento.id}


async def test_documento_observado_es_una_ingesta_valida_disponible_para_rag(entorno):
    resultado = await ingerir(entorno, TEXTO_OBSERVADO)

    assert resultado.resultado_analisis is ResultadoAnalisis.OBSERVADO
    assert resultado.motivos == ("sin_referencias_gri_catalogadas_ni_sanciones_economicas",)
    documento = await entorno.documento(resultado.documento_id)
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert documento.resultado_analisis is ResultadoAnalisis.OBSERVADO and documento.disponible_para_rag
    assert documento.analisis["resultado"] == "OBSERVADO" and documento.analisis["gri"] == [] == documento.analisis["sanciones"]
    assert documento.clave_original and (documento.clave_original, None) in entorno.almacen.objetos
    conteo = await entorno.conteo_vectorial(documento.id)
    assert conteo.total == conteo.publicados == documento.fragmentos_total > 0
    async with entorno.fabrica_pg() as db:
        assert documento.id in await servicio.documentos_recuperables(db, ambiente="development", documento_ids=[documento.id])
    # No hay estados de cumplimiento ni puntaje ESG.
    assert not any(k in documento.analisis for k in ("puntaje", "esg", "estado_gri"))


async def test_tabla_deteriorada_se_acepta_y_su_advertencia_se_persiste(entorno):
    texto = TEXTO_OBSERVADO + TABLA_DETERIORADA
    resultado = await ingerir(entorno, texto)

    documento = await entorno.documento(resultado.documento_id)
    # La advertencia de calidad NO cambia la clasificación...
    assert resultado.resultado_analisis is ResultadoAnalisis.OBSERVADO
    # ... pero queda persistida en las dos representaciones y en el progreso.
    codigos = [a["codigo"] for a in documento.advertencias]
    assert codigos == ["MARKDOWN_TABLE_INCONSISTENT"]
    assert [a["codigo"] for a in documento.analisis["advertencias"]] == codigos
    assert documento.advertencias[0]["categoria"] == "calidad_documento"
    assert documento.advertencias[0]["detalles"]["total_inconsistencias"] == 3
    assert [a["codigo"] for a in resultado.progreso.advertencias] == codigos
    # El original conserva los bytes de la tabla deteriorada y los fragmentos todo el texto literal.
    assert entorno.almacen.objetos[(documento.clave_original, None)] == texto.encode("utf-8")
    filas = await entorno.sql_vectorial(
        "SELECT texto_literal, inicio, fin FROM fragmentos_documento WHERE documento_id = :d ORDER BY indice", d=documento.id
    )
    assert "".join(f.texto_literal for f in filas) == texto
    assert all(texto[f.inicio:f.fin] == f.texto_literal for f in filas)


async def test_bom_y_bytes_originales_se_conservan_y_los_offsets_son_sobre_el_texto_sin_bom(entorno):
    datos = BOM + TEXTO_OBSERVADO.encode("utf-8")
    resultado = await ingerir(entorno, datos)
    documento = await entorno.documento(resultado.documento_id)
    assert entorno.almacen.objetos[(documento.clave_original, None)] == datos
    assert documento.sha256 == sha256_de(datos) and documento.tamano_bytes == len(datos)
    filas = await entorno.sql_vectorial("SELECT texto_literal FROM fragmentos_documento WHERE documento_id = :d ORDER BY indice", d=documento.id)
    assert "".join(f.texto_literal for f in filas) == TEXTO_OBSERVADO


async def test_el_orden_de_las_etapas_es_el_acordado(entorno, monkeypatch):
    """Original → fragmentos no publicados → análisis → COMPLETADO → publicación → éxito."""
    eventos: list[str] = []
    documento_id: list[uuid.UUID] = []

    async def estado_en_este_momento(etiqueta: str):
        async with entorno.fabrica_pg() as db:
            fila = (await db.execute(text("SELECT id, estado_procesamiento::text AS e, original_almacenado_en IS NOT NULL AS o FROM documentos"))).one()
        documento_id[:] = [fila.id]
        conteo = await entorno.conteo_vectorial(fila.id)
        eventos.append((etiqueta, fila.e, fila.o, conteo.total, conteo.publicados))

    original_analizar = coordinador.analizar_documento
    def analizar_vigilado(documento):
        eventos.append("analisis")
        return original_analizar(documento)
    monkeypatch.setattr(coordinador, "analizar_documento", analizar_vigilado)

    original_publicar = servicio.publicar_en_base_vectorial
    async def publicar_vigilado(db, vectorial, documento_id_):
        await estado_en_este_momento("antes_de_publicar_vectores")
        return await original_publicar(db, vectorial, documento_id_)
    monkeypatch.setattr(servicio, "publicar_en_base_vectorial", publicar_vigilado)

    original_publicar_doc = servicio.publicar_documento
    async def completar_vigilado(*args, **kwargs):
        await estado_en_este_momento("antes_de_completar")
        return await original_publicar_doc(*args, **kwargs)
    monkeypatch.setattr(servicio, "publicar_documento", completar_vigilado)

    await ingerir(entorno)

    nombres = [e if isinstance(e, str) else e[0] for e in eventos]
    assert nombres == ["analisis", "antes_de_completar", "antes_de_publicar_vectores"]
    _, estado, original, total, publicados = eventos[1]
    # Al completar: documento aún EN_PROCESO, original ya guardado, todos los fragmentos presentes y NO publicados.
    assert (estado, original, publicados) == ("EN_PROCESO", True, 0) and total > 0
    _, estado, original, total2, publicados = eventos[2]
    # Al publicar vectores: el documento YA está COMPLETADO y los fragmentos siguen sin publicar.
    assert (estado, publicados, total2) == ("COMPLETADO", 0, total)
    # El análisis corrió con la indexación terminada y el original ya guardado.
    assert entorno.proveedor.llamadas >= 2


# ============================================ Rechazos ============================================

@pytest.mark.parametrize(
    ("datos", "nombre", "codigo"),
    [
        (b"", "vacio.md", "EMPTY_DOCUMENT"),
        (b"\xff\xfe\x00bytes invalidos", "x.md", "INVALID_ENCODING"),
        (b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n", "x.md", "INVALID_FILE_CONTENT"),
        (b"# Texto", "x.pdf", "INVALID_FILE_TYPE"),
    ],
)
async def test_un_rechazo_no_deja_reserva_original_ni_fragmentos_y_se_audita(entorno, datos, nombre, codigo):
    with pytest.raises(BusinessValidationError) as capturado:
        await ingerir(entorno, datos, nombre)
    assert capturado.value.code == codigo
    assert await entorno.documentos() == []
    assert entorno.almacen.objetos == {} and entorno.proveedor.llamadas == 0
    assert (await entorno.sql_vectorial("SELECT count(*) AS n FROM fragmentos_documento"))[0].n == 0
    (evento,) = await entorno.auditoria()
    assert evento.tipo_evento == "RECHAZO_DOCUMENTO" or "Rechazo" in str(evento.tipo_evento)
    assert f"codigo={codigo}" in evento.detalle and evento.usuario_id == entorno.usuario.id
    assert evento.fecha_hora_utc.utcoffset() == timedelta(0)  # UTC


async def test_empresa_inactiva_antes_de_empezar_se_rechaza_y_se_audita(entorno):
    async with entorno.fabrica_pg() as db:
        await db.execute(text("UPDATE empresas SET activa = false"))
        await db.commit()
    with pytest.raises(BusinessValidationError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "COMPANY_INACTIVE"
    assert await entorno.documentos() == [] and entorno.almacen.objetos == {}
    (evento,) = await entorno.auditoria()
    assert "codigo=COMPANY_INACTIVE" in evento.detalle


async def test_sector_declarado_distinto_al_de_la_empresa_se_rechaza(entorno):
    entorno.metadatos = metadatos(entorno.empresa, sector=SectorEmpresa.ENERGIA)
    with pytest.raises(BusinessValidationError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "COMPANY_SECTOR_MISMATCH"
    assert await entorno.documentos() == []


async def test_el_actor_viene_de_la_sesion_y_debe_ser_un_superadmin_habilitado(entorno):
    with pytest.raises(Exception) as capturado:
        await ingerir(entorno, usuario_id=uuid.uuid4())
    assert getattr(capturado.value, "code", None) == "INGESTION_FORBIDDEN"
    assert await entorno.documentos() == []


async def test_duplicado_por_contenido_o_por_empresa_anio_tipo_se_rechaza_sin_efectos(entorno):
    primero = await ingerir(entorno)
    antes_llamadas = entorno.proveedor.llamadas

    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)  # mismos bytes
    assert capturado.value.code == "DOCUMENT_ALREADY_INGESTED"
    assert "sha256" in capturado.value.details["criterio"]

    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno, TEXTO_OBSERVADO)  # otro contenido, misma empresa/año/tipo
    assert capturado.value.code == "DOCUMENT_ALREADY_INGESTED"
    assert capturado.value.details["criterio"] == ["empresa_anio_tipo"]

    assert [d.id for d in await entorno.documentos()] == [primero.documento_id]
    assert entorno.proveedor.llamadas == antes_llamadas  # el duplicado no llegó al proveedor
    eventos = await entorno.auditoria()
    assert sum("Ingesta rechazada" in e.detalle and "codigo=DOCUMENT_ALREADY_INGESTED" in e.detalle for e in eventos) == 2


async def test_un_observado_completado_tambien_bloquea_recargas_duplicadas(entorno):
    await ingerir(entorno, TEXTO_OBSERVADO)
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno, TEXTO_OBSERVADO)
    assert capturado.value.code == "DOCUMENT_ALREADY_INGESTED"


async def test_dos_cargas_identicas_concurrentes_dejan_una_sola_ingesta_valida(entorno):
    proveedor = entorno.proveedor
    proveedor.espera = 0.05
    resultados = await asyncio.gather(ingerir(entorno), ingerir(entorno), return_exceptions=True)
    exitos = [r for r in resultados if not isinstance(r, Exception)]
    fallos = [r for r in resultados if isinstance(r, Exception)]
    assert len(exitos) == 1 and len(fallos) == 1
    assert isinstance(fallos[0], ConflictError)
    assert fallos[0].code in ("DOCUMENT_UPLOAD_IN_PROGRESS", "DOCUMENT_ALREADY_INGESTED", "DOCUMENT_RESERVATION_CONFLICT")
    documentos = await entorno.documentos()
    assert len(documentos) == 1 and documentos[0].estado_procesamiento is EstadoProcesamiento.COMPLETADO
    conteo = await entorno.conteo_vectorial(documentos[0].id)
    assert conteo.total == conteo.publicados == documentos[0].fragmentos_total


async def test_dos_cargas_concurrentes_para_la_misma_empresa_anio_tipo_con_distinto_contenido(entorno):
    entorno.proveedor.espera = 0.05
    resultados = await asyncio.gather(
        ingerir(entorno, TEXTO_CON_HALLAZGOS), ingerir(entorno, TEXTO_OBSERVADO), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in resultados) == 1
    assert sum(isinstance(r, ConflictError) for r in resultados) == 1
    assert len(await entorno.documentos()) == 1


# ============================================ Fallos con compensación conjunta ============================================

async def verificar_intento_limpio(entorno, documento, *, motivo: str, con_vectores: bool):
    """Estado esperado tras un fallo ya compensado: FALLIDO, reserva liberada, nada externo, sin análisis."""
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert documento.motivo_fallo == motivo
    assert documento.estado_compensacion in (EstadoCompensacion.COMPLETADA, EstadoCompensacion.NINGUNA)
    assert documento.reserva_activa is False
    assert documento.analisis is None and documento.resultado_analisis is None
    conteo = await entorno.conteo_vectorial(documento.id)
    assert conteo.total == 0
    assert (documento.clave_original, None) not in entorno.almacen.objetos
    if con_vectores:
        # El documento quedó CERRADO en la base vectorial: ninguna escritura tardía puede reintroducir contenido.
        cierres = await entorno.sql_vectorial("SELECT 1 FROM cierres_documento WHERE documento_id = :d", d=documento.id)
        assert len(cierres) == 1


async def test_fallo_en_minio_marca_fallido_no_toca_la_base_vectorial_y_libera_la_reserva(entorno):
    entorno.almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_ERROR", "fallo del almacén")]
    with pytest.raises(ExternalServiceError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "STORAGE_ERROR"
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="STORAGE_ERROR", con_vectores=False)
    assert documento.vector_escritura_intentada_en is None and entorno.proveedor.llamadas == 0
    assert (await entorno.sql_vectorial("SELECT count(*) AS n FROM cierres_documento"))[0].n == 0
    # Reintento permitido tras confirmar la limpieza.
    resultado = await ingerir(entorno)
    assert resultado.documento_id != documento.id


async def test_fallo_de_embeddings_en_un_lote_posterior_elimina_los_lotes_anteriores(entorno):
    entorno.proveedor.fallar_en_lote = 1
    with pytest.raises(ExternalServiceError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "EMBEDDING_PROVIDER_ERROR"
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="EMBEDDING_PROVIDER_ERROR", con_vectores=True)
    assert documento.vector_escritura_intentada_en is not None and entorno.proveedor.llamadas == 2
    # Sin texto del proveedor ni del documento en el error ni en la fila.
    assert "texto del documento" not in str(capturado.value.details) and "texto del documento" not in capturado.value.message


async def test_fallo_al_guardar_un_lote_vectorial_deja_el_intento_limpio(entorno):
    entorno.vectorial.inyectar("INSERT INTO fragmentos_documento", desde=2, veces=1)
    with pytest.raises(ExternalServiceError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "VECTOR_STORE_ERROR"
    (documento,) = await entorno.documentos()
    entorno.vectorial.quitar()
    await verificar_intento_limpio(entorno, documento, motivo="VECTOR_STORE_ERROR", con_vectores=True)


async def test_un_error_del_detector_nunca_se_convierte_en_observado(entorno, monkeypatch):
    def detector_roto(texto, catalogo):
        raise RuntimeError("fallo con contenido del documento")

    from app.services.ingesta import analisis_ingesta

    original = analisis_ingesta.analizar_texto
    monkeypatch.setattr(
        coordinador, "analizar_documento", lambda doc: original(doc.texto, advertencias_calidad=doc.advertencias, detector_gri=detector_roto)
    )
    with pytest.raises(AnalisisIngestaError) as capturado:
        await ingerir(entorno, TEXTO_OBSERVADO)  # un texto que SERÍA observado
    assert capturado.value.code == "INGESTION_ANALYSIS_FAILED"
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="INGESTION_ANALYSIS_FAILED", con_vectores=True)
    assert documento.resultado_analisis is None  # análisis pendiente, nunca OBSERVADO


async def test_fallo_de_un_detector_de_sanciones_tampoco_es_observado(entorno, monkeypatch):
    from app.services.ingesta import analisis_ingesta

    original = analisis_ingesta.analizar_texto

    def sanciones_roto(texto):
        raise RuntimeError("x")

    monkeypatch.setattr(
        coordinador, "analizar_documento", lambda doc: original(doc.texto, detector_sanciones=sanciones_roto)
    )
    with pytest.raises(AnalisisIngestaError) as capturado:
        await ingerir(entorno)
    assert capturado.value.details["detector"] == "sanciones"
    (documento,) = await entorno.documentos()
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO and documento.resultado_analisis is None


async def test_fallo_al_persistir_el_analisis_compensa_y_retira_resultados_parciales(entorno, monkeypatch):
    original = servicio.persistir_analisis

    async def persistir_roto(db, documento_id, **kwargs):
        # El análisis llega a guardarse y luego falla algo: lo parcial no debe sobrevivir.
        await original(db, documento_id, **kwargs)
        raise ExternalServiceError("ANALYSIS_PERSISTENCE_ERROR", "fallo simulado")

    monkeypatch.setattr(servicio, "persistir_analisis", persistir_roto)
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="ANALYSIS_PERSISTENCE_ERROR", con_vectores=True)
    assert documento.advertencias is not None  # el progreso se conserva (códigos), el análisis no


async def test_un_analisis_con_estructura_invalida_no_se_persiste(entorno, monkeypatch):
    class Resultado:
        resultado = ResultadoAnalisis.OBSERVADO
        motivos = ()

    monkeypatch.setattr(coordinador, "_analizar", lambda doc: (Resultado(), {"resultado": "OBSERVADO"}))
    with pytest.raises(BusinessValidationError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "INVALID_ANALYSIS"
    (documento,) = await entorno.documentos()
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO and documento.analisis is None


async def test_empresa_desactivada_durante_el_proceso_no_completa_y_compensa(entorno):
    async def desactivar():
        async with entorno.fabrica_pg() as db:
            await db.execute(text("UPDATE empresas SET activa = false"))
            await db.commit()

    entorno.proveedor.antes_del_lote[1] = desactivar
    with pytest.raises(BusinessValidationError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "COMPANY_INACTIVE"
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="COMPANY_INACTIVE", con_vectores=True)
    assert documento.completado_en is None and documento.vector_publicado_en is None


async def test_dimension_del_proveedor_distinta_de_la_base_vectorial_se_rechaza_antes_de_reservar(entorno):
    entorno.deps.embeddings.identidad = IdentidadEmbeddings("doble", "doble-1024", 1024)
    with pytest.raises(ExternalServiceError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "EMBEDDING_DIMENSION_MISMATCH"
    assert await entorno.documentos() == []


async def test_reintento_tras_fallo_con_limpieza_pendiente_se_bloquea_hasta_confirmarla(entorno):
    entorno.proveedor.fallar_en_lote = 1
    entorno.vectorial.inyectar("DELETE FROM fragmentos_documento")  # la limpieza vectorial no puede ejecutarse
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    (documento,) = await entorno.documentos()
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert documento.estado_compensacion is EstadoCompensacion.PENDIENTE and documento.reserva_activa is True
    assert documento.ultimo_error_compensacion == "VECTOR_CLEANUP_FAILED"

    entorno.proveedor.fallar_en_lote = None
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "DOCUMENT_CLEANUP_PENDING"  # reserva conservada

    entorno.vectorial.quitar()
    resumen = await coordinador.ejecutar_recuperacion(entorno.deps)
    assert resumen.compensados == 1 and resumen.pendientes == 0
    documento = await entorno.documento(documento.id)
    assert documento.estado_compensacion is EstadoCompensacion.COMPLETADA and documento.reserva_activa is False
    resultado = await ingerir(entorno)  # ahora sí
    assert resultado.documento_id != documento.id


async def test_un_conflicto_que_no_es_de_propiedad_tambien_compensa(entorno, monkeypatch):
    async def lote_ya_persistido(*args, **kwargs):
        raise ConflictError("FRAGMENTS_ALREADY_PERSISTED", "ya existen")

    monkeypatch.setattr(coordinador, "_guardar_lote", lote_ya_persistido)
    with pytest.raises(ConflictError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "FRAGMENTS_ALREADY_PERSISTED"
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="FRAGMENTS_ALREADY_PERSISTED", con_vectores=True)


async def test_si_el_conteo_vectorial_no_coincide_la_indexacion_no_se_confirma_y_se_compensa(entorno, monkeypatch):
    original = coordinador._contar

    async def conteo_menor(vectorial, documento):
        real = await original(vectorial, documento)
        return type(real)(total=real.total - 1, publicados=0)

    monkeypatch.setattr(coordinador, "_contar", conteo_menor)
    with pytest.raises(ExternalServiceError) as capturado:
        await ingerir(entorno)
    assert capturado.value.code == "VECTOR_INDEX_NOT_CONFIRMED"
    (documento,) = await entorno.documentos()
    await verificar_intento_limpio(entorno, documento, motivo="VECTOR_INDEX_NOT_CONFIRMED", con_vectores=True)
    assert documento.completado_en is None and documento.resultado_analisis is None


# ============================================ Progreso y auditoría ============================================

async def test_el_progreso_es_persistente_real_y_sin_porcentajes(entorno, monkeypatch):
    capturas = []

    original = servicio.registrar_progreso

    async def vigilado(db, documento_id, **kwargs):
        await original(db, documento_id, **kwargs)
        async with entorno.fabrica_pg() as otra:  # lectura desde OTRA sesión: está persistido, no en memoria
            p = await servicio.obtener_progreso(otra, documento_id)
        capturas.append((p.estado.value, p.etapa, p.fragmentos_procesados, p.fragmentos_total, p.finalizado))

    monkeypatch.setattr(servicio, "registrar_progreso", vigilado)
    resultado = await ingerir(entorno)

    etapas = [c[1] for c in capturas]
    assert etapas[0] == "ALMACENANDO_ORIGINAL" and "INDEXANDO" in etapas and "ANALIZANDO" in etapas and "COMPLETANDO" in etapas
    assert all(c[0] == "EN_PROCESO" and c[4] is False for c in capturas)  # nada es "final" antes de publicar
    contadores = [c[2] for c in capturas if c[1] == "INDEXANDO"]
    assert contadores == sorted(contadores) and contadores[-1] == resultado.fragmentos  # sube con cada lote
    assert capturas[0][3] is None and all(c[3] == resultado.fragmentos for c in capturas if c[1] == "INDEXANDO")
    final = resultado.progreso
    assert (final.estado.value, final.etapa, final.finalizado) == ("COMPLETADO", "FINALIZADO", True)
    assert final.resultado_analisis == "CON_HALLAZGOS" and final.codigo_error is None
    assert not any("porcentaje" in campo or "percent" in campo for campo in final.__dataclass_fields__)


async def test_progreso_de_un_fallo_expone_un_codigo_seguro_y_no_finaliza(entorno):
    entorno.proveedor.fallar_en_lote = 0
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    (documento,) = await entorno.documentos()
    async with entorno.fabrica_pg() as db:
        progreso = await servicio.obtener_progreso(db, documento.id)
    assert progreso.estado is servicio.EstadoProgreso.FALLIDO and not progreso.finalizado
    assert progreso.codigo_error == "EMBEDDING_PROVIDER_ERROR" and progreso.resultado_analisis is None
    assert progreso.etapa == "INDEXANDO" and "texto" not in json.dumps(progreso.advertencias)


async def test_progreso_de_una_operacion_inexistente(entorno):
    from app.exceptions import NotFoundError

    async with entorno.fabrica_pg() as db:
        with pytest.raises(NotFoundError):
            await servicio.obtener_progreso(db, uuid.uuid4())


async def test_auditoria_de_exito_con_actor_y_fecha_utc_sin_contenido(entorno):
    resultado = await ingerir(entorno)
    eventos = await entorno.auditoria()
    (exito,) = [e for e in eventos if "Ingesta completada" in e.detalle]
    assert exito.usuario_id == entorno.usuario.id and exito.fecha_hora_utc.utcoffset() == timedelta(0)
    assert str(resultado.documento_id) in exito.detalle and "resultado=CON_HALLAZGOS" in exito.detalle
    for evento in eventos:
        assert "alcance 1" not in evento.detalle and "S/ 12,500" not in evento.detalle  # nada del contenido
        assert len(evento.detalle) <= 500


async def test_auditoria_de_fallo_una_sola_vez_con_actor_y_motivo(entorno):
    entorno.proveedor.fallar_en_lote = 1
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    eventos = [e for e in await entorno.auditoria() if "Ingesta fallida" in e.detalle]
    assert len(eventos) == 1
    assert "motivo=EMBEDDING_PROVIDER_ERROR" in eventos[0].detalle and "origen=ejecutor" in eventos[0].detalle
    assert eventos[0].usuario_id == entorno.usuario.id
    assert "texto del documento" not in eventos[0].detalle


async def test_si_la_auditoria_no_puede_escribirse_el_fallo_igual_queda_registrado(entorno, monkeypatch):
    def auditoria_rota(*args, **kwargs):
        raise RuntimeError("tabla de auditoría no disponible")

    monkeypatch.setattr(servicio, "registrar_evento", auditoria_rota)
    entorno.almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_ERROR", "x")]
    with pytest.raises(ExternalServiceError):
        await ingerir(entorno)
    (documento,) = await entorno.documentos()
    assert documento.estado_procesamiento is EstadoProcesamiento.FALLIDO and documento.reserva_activa is False


# ============================================ Aislamiento por ambiente ============================================

async def test_el_mismo_documento_en_dos_ambientes_no_se_mezcla(entorno):
    primero = await ingerir(entorno)
    from tests.ayudantes_ingesta import AlmacenEnMemoria

    qa = AlmacenEnMemoria("qa")
    deps_qa = coordinador.DependenciasIngesta(entorno.deps.sesiones, entorno.deps.sesiones_vectoriales, qa, entorno.proveedor)
    segundo = await ingerir_documento(
        deps_qa, entorno.config, usuario_id=entorno.usuario.id, nombre_archivo="informe.md",
        leer=lector(TEXTO_CON_HALLAZGOS.encode()), metadatos=entorno.metadatos,
    )
    assert segundo.documento_id != primero.documento_id
    docs = {d.id: d for d in await entorno.documentos()}
    assert {docs[primero.documento_id].ambiente, docs[segundo.documento_id].ambiente} == {"development", "qa"}
    assert (await entorno.conteo_vectorial(primero.documento_id, "development")).publicados > 0
    assert (await entorno.conteo_vectorial(primero.documento_id, "qa")).total == 0
    assert (await entorno.conteo_vectorial(segundo.documento_id, "qa")).publicados > 0
    assert (await entorno.conteo_vectorial(segundo.documento_id, "development")).total == 0


# ============================================ Configuración y códigos seguros (sin bases) ============================================

@pytest.mark.parametrize(
    "cambios",
    [
        {"tamano_lote": 0}, {"tamano_lote": True}, {"tamano_lote": 1.5},
        {"vigencia": timedelta(0)}, {"vigencia": 30},
        {"intervalo_latido": timedelta(0)},
        {"vigencia": timedelta(seconds=10), "intervalo_latido": timedelta(seconds=10)},
        {"vigencia": timedelta(seconds=10), "intervalo_latido": timedelta(seconds=11)},
    ],
)
def test_configuracion_invalida_del_coordinador(cambios):
    with pytest.raises(ValueError):
        coordinador.ConfigCoordinador(**cambios)


def test_el_intervalo_del_latido_por_defecto_es_un_tercio_de_la_vigencia():
    assert coordinador.ConfigCoordinador(vigencia=timedelta(minutes=15)).intervalo == timedelta(minutes=5)
    assert coordinador.ConfigCoordinador(vigencia=timedelta(seconds=9), intervalo_latido=timedelta(seconds=2)).intervalo == timedelta(seconds=2)


def test_solo_los_codigos_propios_y_bien_formados_se_usan_como_motivo_de_fallo():
    assert coordinador._codigo(ExternalServiceError("EMBEDDING_PROVIDER_ERROR", "x")) == "EMBEDDING_PROVIDER_ERROR"
    assert coordinador._codigo(ConflictError("DOCUMENT_STATE_CONFLICT", "x")) == "DOCUMENT_STATE_CONFLICT"
    assert coordinador._codigo(ExternalServiceError("codigo en minusculas con texto del documento", "x")) == "UNEXPECTED_ERROR"
    assert coordinador._codigo(RuntimeError("texto del documento")) == "UNEXPECTED_ERROR"

    class Ajena(Exception):
        code = "CODIGO_DE_UN_TERCERO"

    assert coordinador._codigo(Ajena()) == "UNEXPECTED_ERROR"  # no es una AppException propia


def test_publicacion_pendiente_no_es_un_exito_y_lleva_solo_identificadores():
    documento_id = uuid.uuid4()
    error = PublicacionVectorialPendiente(documento_id, "VECTOR_PUBLICATION_FAILED")
    assert isinstance(error, ExternalServiceError) and error.code == "VECTOR_PUBLICATION_PENDING"
    assert error.details == {"documento_id": str(documento_id), "causa": "VECTOR_PUBLICATION_FAILED"}

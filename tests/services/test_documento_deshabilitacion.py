"""Deshabilitación de documentos (ACT-02): reglas del servicio sobre SQLite aislado. Corre también donde no hay PostgreSQL.

QUÉ DEMUESTRA. Que la deshabilitación registra actor, instante y motivo, libera la reserva en la misma sentencia, escribe UN evento
de auditoría, es idempotente (un reintento no sobrescribe el motivo ni audita otra vez), respeta el ambiente y el estado del documento,
deja libre el cupo anunciado por `GET /documentos/disponibilidad` y los excluye de las consultas de elegibilidad.

QUÉ NO DEMUESTRA. SQLite no demuestra la exclusión mutua de PostgreSQL: la protección común del origen (`proteger_origen_de_analisis`)
usa construcciones propias de PostgreSQL y aquí se sustituye por un doble que registra la llamada. La coordinación real con las
validaciones y reportes, las carreras entre cargas, la migración y los índices parciales se prueban con PostgreSQL real en
`test_documento_deshabilitacion_postgres.py`.
"""
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessValidationError, ConflictError, NotFoundError
from app.models import RegistroAuditoria, ResultadoAuditoria, TipoDocumento, TipoEventoAuditoria
from app.models.documento_ingesta import MAXIMO_MOTIVO_DESHABILITACION, Documento, EstadoProcesamiento
from app.modules.ingesta.application import documento_service as servicio
from app.modules.ingesta.application import operacion_service
from app.modules.ingesta.application.analisis_ingesta import analizar_texto
from app.modules.ingesta.application.documento_service import deshabilitacion as modulo
from app.modules.ingesta.schemas import (
    DeshabilitarDocumentoRequest,
    DisponibilidadTipo,
    EstadoEspacio,
    EstadoProgreso,
)
from tests.ayudantes_analisis_gri import sembrar_documento
from tests.ayudantes_ingesta import SesionSQLite, crear_empresa, crear_usuario, metadatos, sha256_de

MEMORIA = TipoDocumento.MEMORIA_ANUAL
REPORTE = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI
ANALISIS = analizar_texto("# Informe\n\nTexto sin códigos ni sanciones reconocibles.\n").a_dict()
AMBIENTE = "development"
MOTIVO = "Se cargó con el año equivocado."


@pytest.fixture
async def e(monkeypatch):
    db = SesionSQLite()
    usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
    empresa = await crear_empresa(db)
    protecciones: list[dict] = []

    async def proteger(_db, *, ambiente, empresa_id, anio):
        protecciones.append({"ambiente": ambiente, "empresa_id": empresa_id, "anio": anio})
        return 0

    monkeypatch.setattr(modulo, "proteger_origen_de_analisis", proteger)  # PostgreSQL: ver el archivo *_postgres
    yield SimpleNamespace(db=db, usuario=usuario, empresa=empresa, protecciones=protecciones)
    db.cerrar()


async def documento(e, tipo=MEMORIA, **cambios) -> Documento:
    return await sembrar_documento(e.db, e.empresa, e.usuario, analisis=ANALISIS, tipo=tipo, **cambios)


async def fila(e, doc) -> Documento:
    return (await e.db.execute(select(Documento).where(Documento.id == doc.id).execution_options(populate_existing=True))).scalar_one()


async def auditoria(e) -> list[RegistroAuditoria]:
    return list((await e.db.execute(select(RegistroAuditoria))).scalars().all())


async def deshabilitar(e, doc, motivo=MOTIVO, *, ambiente=AMBIENTE, usuario=None):
    return await servicio.deshabilitar_documento(
        e.db, doc.id, ambiente=ambiente, usuario_id=(usuario or e.usuario).id, motivo=motivo
    )


# ============================================ Efecto de deshabilitar ============================================

async def test_deshabilitar_registra_actor_fecha_y_motivo_libera_la_reserva_y_conserva_el_documento(e):
    doc = await documento(e)
    antes = {c: getattr(doc, c) for c in ("clave_original", "version_id_original", "fragmentos_total", "sha256",
                                         "resultado_analisis", "analisis", "etapa_actual")}

    resultado = await deshabilitar(e, doc, "  " + MOTIVO + "  ")

    assert resultado.ya_estaba_deshabilitado is False
    actual = await fila(e, doc)
    assert (actual.reserva_activa, actual.deshabilitado) == (False, True)
    assert (actual.deshabilitado_por, actual.motivo_deshabilitacion) == (e.usuario.id, MOTIVO)  # sin espacios sobrantes
    assert actual.deshabilitado_en is not None
    # NO es un borrado ni un estado de procesamiento: sigue COMPLETADO, con su original, su análisis y su publicación.
    assert actual.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert {c: getattr(actual, c) for c in antes} == antes
    assert None not in (actual.original_almacenado_en, actual.completado_en, actual.vector_publicado_en)
    assert actual.disponible_para_rag is False
    assert e.protecciones == [{"ambiente": AMBIENTE, "empresa_id": e.empresa.id, "anio": 2025}]  # la protección común, ANTES de escribir


async def test_se_escribe_un_solo_evento_de_auditoria_con_el_actor_de_la_sesion(e):
    doc = await documento(e)
    await deshabilitar(e, doc)

    (evento,) = await auditoria(e)
    assert (evento.tipo_evento, evento.resultado, evento.usuario_id) == (
        TipoEventoAuditoria.INGESTA_DATOS, ResultadoAuditoria.EXITOSO.value, e.usuario.id,
    )
    assert f"archivo={doc.nombre_archivo}" in evento.detalle
    assert str(doc.id) not in evento.detalle  # sin UUID crudo
    assert MOTIVO in evento.detalle
    assert "deshabilitado" in evento.detalle.lower()
    assert len(evento.detalle) <= 500


async def test_un_motivo_muy_largo_no_rompe_el_detalle_de_auditoria(e):
    doc = await documento(e)
    await deshabilitar(e, doc, "x" * MAXIMO_MOTIVO_DESHABILITACION)

    (evento,) = await auditoria(e)
    assert len(evento.detalle) == 500  # la columna es VARCHAR(500)
    assert (await fila(e, doc)).motivo_deshabilitacion == "x" * MAXIMO_MOTIVO_DESHABILITACION  # el motivo completo queda en el documento


# ============================================ Idempotencia ============================================

async def test_un_reintento_no_sobrescribe_el_motivo_ni_genera_otro_evento(e):
    doc = await documento(e)
    primero = await deshabilitar(e, doc, "Motivo original")
    instante = (await fila(e, doc)).deshabilitado_en

    otro_actor = await crear_usuario(e.db, correo="otro@pruebas.invalid", rol=servicio.RolUsuario.ADMINISTRADOR)
    segundo = await deshabilitar(e, doc, "Un motivo distinto", usuario=otro_actor)

    assert (primero.ya_estaba_deshabilitado, segundo.ya_estaba_deshabilitado) == (False, True)
    actual = await fila(e, doc)
    assert (actual.motivo_deshabilitacion, actual.deshabilitado_por, actual.deshabilitado_en) == ("Motivo original", e.usuario.id, instante)
    assert len(await auditoria(e)) == 1
    assert len(e.protecciones) == 1  # el reintento ni siquiera toma el bloqueo del origen


async def test_si_otra_solicitud_gana_la_carrera_se_devuelve_su_registro_sin_sobrescribir_ni_auditar(e, monkeypatch):
    """La solicitud pasa la lectura previa (aún no deshabilitado), pero mientras toma la protección otra la deshabilita y confirma: el
    UPDATE condicional (`deshabilitado_en IS NULL`) ya no encuentra la fila."""
    doc = await documento(e)

    async def protege_y_gana_otra(db, *, ambiente, empresa_id, anio):
        await db.execute(
            update(Documento).where(Documento.id == doc.id).values(
                deshabilitado_en=doc.creado_en, deshabilitado_por=e.usuario.id, motivo_deshabilitacion="Motivo del ganador",
                reserva_activa=False,
            )
        )
        await db.commit()
        return 0

    monkeypatch.setattr(modulo, "proteger_origen_de_analisis", protege_y_gana_otra)

    resultado = await deshabilitar(e, doc, "Motivo del perdedor")

    assert resultado.ya_estaba_deshabilitado is True
    assert resultado.documento.motivo_deshabilitacion == "Motivo del ganador"
    assert (await fila(e, doc)).motivo_deshabilitacion == "Motivo del ganador"
    assert await auditoria(e) == []  # quien perdió no audita


# ============================================ Rechazos ============================================

@pytest.mark.parametrize(
    ("construccion", "estado"),
    [
        pytest.param(dict(estado=EstadoProcesamiento.EN_PROCESO), "EN_PROCESO", id="en-proceso"),
        pytest.param(dict(estado=EstadoProcesamiento.FALLIDO), "FALLIDO", id="fallido"),
        pytest.param(dict(estado=EstadoProcesamiento.COMPLETADO, publicado=False), "PUBLICACION_PENDIENTE", id="publicacion-pendiente"),
    ],
)
async def test_una_carga_no_finalizada_no_se_deshabilita_ni_se_cancela(e, construccion, estado):
    doc = await documento(e, **construccion)
    instantanea = (doc.reserva_activa, doc.estado_procesamiento, doc.vector_publicado_en)

    with pytest.raises(ConflictError) as capturado:
        await deshabilitar(e, doc)

    assert capturado.value.code == "DOCUMENT_NOT_DISABLEABLE"
    assert capturado.value.details == {"estado": estado}
    actual = await fila(e, doc)
    assert (actual.reserva_activa, actual.estado_procesamiento, actual.vector_publicado_en) == instantanea
    assert (actual.deshabilitado, actual.motivo_deshabilitacion) == (False, None)
    assert await auditoria(e) == []
    assert e.protecciones == []


async def test_un_documento_de_otro_ambiente_o_inexistente_es_404_y_no_se_toca(e):
    doc = await documento(e, ambiente="qa")

    for objetivo in (doc.id, uuid.uuid4()):
        with pytest.raises(NotFoundError) as capturado:
            await servicio.deshabilitar_documento(e.db, objetivo, ambiente=AMBIENTE, usuario_id=e.usuario.id, motivo=MOTIVO)
        assert capturado.value.code == "DOCUMENT_NOT_FOUND"

    actual = await fila(e, doc)
    assert (actual.reserva_activa, actual.deshabilitado) == (True, False)
    assert await auditoria(e) == []


@pytest.mark.parametrize("motivo", ["", "   \n\t ", None, 7, "x" * (MAXIMO_MOTIVO_DESHABILITACION + 1), "con\x00nulo", "con\x1bescape"])
async def test_un_motivo_vacio_o_invalido_se_rechaza_antes_de_tocar_nada(e, motivo):
    doc = await documento(e)

    with pytest.raises(BusinessValidationError) as capturado:
        await deshabilitar(e, doc, motivo)

    assert capturado.value.code == "INVALID_DISABLE_REASON"
    assert (await fila(e, doc)).deshabilitado is False
    assert await auditoria(e) == []
    assert e.protecciones == []


def test_el_motivo_admite_saltos_de_linea_y_tabulaciones_y_se_recorta():
    assert servicio.validar_motivo("  Línea 1\n\tLínea 2  ") == "Línea 1\n\tLínea 2"


# ============================================ Cupo, reemplazo y SHA-256 ============================================

async def estados_de(e, anio=2025) -> dict[str, DisponibilidadTipo]:
    r = await operacion_service.disponibilidad_de_tipos(e.db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=anio)
    return {t.tipo.value: t for t in r.tipos}


async def test_deshabilitar_la_memoria_libera_solo_su_cupo_y_el_reporte_sigue_ocupado(e):
    memoria, reporte = await documento(e, MEMORIA), await documento(e, REPORTE)
    antes = await estados_de(e)
    assert (antes[MEMORIA.value].estado, antes[REPORTE.value].estado) == (EstadoEspacio.OCUPADO, EstadoEspacio.OCUPADO)
    assert not antes[MEMORIA.value].cargable
    assert not antes[REPORTE.value].cargable

    await deshabilitar(e, memoria)

    despues = await estados_de(e)
    assert (despues[MEMORIA.value].estado, despues[MEMORIA.value].cargable, despues[MEMORIA.value].documento_id) == (
        EstadoEspacio.DISPONIBLE, True, None,
    )
    assert (despues[REPORTE.value].estado, despues[REPORTE.value].cargable, despues[REPORTE.value].documento_id) == (
        EstadoEspacio.OCUPADO, False, reporte.id,
    )
    assert (await fila(e, reporte)).deshabilitado is False
    assert (await fila(e, reporte)).reserva_activa is True


def test_el_contrato_de_disponibilidad_no_cambia():
    assert set(DisponibilidadTipo.model_fields) == {"tipo", "estado", "cargable", "documento_id", "nombre_archivo", "reservado_en"}
    assert {m.value for m in EstadoEspacio} == {"DISPONIBLE", "EN_PROCESO", "PUBLICACION_PENDIENTE", "OCUPADO", "LIMPIEZA_PENDIENTE"}


async def reservar(e, datos: bytes, tipo=MEMORIA, anio=2025):
    return await servicio.reservar_documento(
        e.db, metadatos=metadatos(e.empresa, anio, tipo), nombre_archivo="Reemplazo.md", sha256=sha256_de(datos),
        tamano_bytes=len(datos), usuario_id=e.usuario.id, ambiente=AMBIENTE,
    )


async def test_el_reemplazo_vuelve_a_ocupar_el_cupo_y_una_segunda_carga_del_mismo_tipo_se_rechaza(e):
    memoria = await documento(e, MEMORIA)
    await deshabilitar(e, memoria)

    reemplazo = await reservar(e, b"# Memoria corregida\n")

    cupo = (await estados_de(e))[MEMORIA.value]
    assert (cupo.estado, cupo.cargable, cupo.documento_id) == (EstadoEspacio.EN_PROCESO, False, reemplazo.id)
    with pytest.raises(ConflictError) as capturado:
        await reservar(e, b"# Otra memoria\n")
    assert capturado.value.code == "DOCUMENT_UPLOAD_IN_PROGRESS"
    assert (await fila(e, memoria)).reserva_activa is False  # el retirado no recupera el cupo


async def test_se_puede_corregir_una_carga_equivocada_volviendo_a_subir_los_mismos_bytes(e):
    datos = b"# Memoria con metadatos equivocados\n"
    equivocado = await reservar(e, datos, anio=2024)
    await e.db.execute(update(Documento).where(Documento.id == equivocado.id).values(
        estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ANALISIS["resultado"], analisis=ANALISIS,
        completado_en=equivocado.creado_en, original_almacenado_en=equivocado.creado_en, vector_escritura_intentada_en=equivocado.creado_en,
        vector_publicado_en=equivocado.creado_en, etapa_actual="FINALIZADO",
    ))
    await e.db.commit()
    with pytest.raises(ConflictError):  # mientras el equivocado sigue activo, el SHA-256 lo protege
        await reservar(e, datos, anio=2025)

    await servicio.deshabilitar_documento(e.db, equivocado.id, ambiente=AMBIENTE, usuario_id=e.usuario.id, motivo="Año equivocado.")
    corregido = await reservar(e, datos, anio=2025)

    assert corregido.id != equivocado.id
    assert corregido.sha256 == equivocado.sha256
    assert corregido.reserva_activa is True
    assert (await fila(e, equivocado)).deshabilitado is True  # el historial conserva ambos


async def test_los_mismos_bytes_siguen_bloqueados_si_otra_reserva_activa_los_tiene(e):
    """Liberar la reserva solo libera lo del documento retirado: si el mismo contenido está activo como otro tipo, el índice de SHA-256
    sigue rechazando la carga."""
    datos = b"# Contenido compartido\n"
    memoria, reporte = await documento(e, MEMORIA), await documento(e, REPORTE)
    await e.db.execute(update(Documento).where(Documento.id == memoria.id).values(sha256=sha256_de(datos)))
    await e.db.commit()
    await deshabilitar(e, memoria)  # libera el SHA-256 y el cupo de la memoria
    await e.db.execute(update(Documento).where(Documento.id == reporte.id).values(sha256=sha256_de(datos)))  # el reporte ACTIVO lo tiene
    await e.db.commit()

    with pytest.raises(ConflictError) as capturado:
        await reservar(e, datos, MEMORIA)

    assert capturado.value.code == "DOCUMENT_ALREADY_INGESTED"
    assert capturado.value.details["criterio"] == ["sha256"]


# ============================================ Restricciones de la base ============================================

@pytest.mark.parametrize(
    ("cambios", "descripcion"),
    [
        pytest.param(dict(reserva_activa=False), "liberar la reserva de un completado sin deshabilitarlo", id="liberar-sin-deshabilitar"),
        pytest.param(dict(deshabilitado_en="ahora"), "deshabilitado sin actor ni motivo", id="incompleto"),
        pytest.param(dict(deshabilitado_en="ahora", deshabilitado_por="actor", motivo_deshabilitacion="   "), "motivo en blanco", id="motivo-en-blanco"),
        pytest.param(dict(deshabilitado_en="ahora", deshabilitado_por="actor", motivo_deshabilitacion="x"), "deshabilitado conservando la reserva", id="con-reserva"),
    ],
)
async def test_la_base_rechaza_estados_de_deshabilitacion_incoherentes(e, cambios, descripcion):
    doc = await documento(e)
    valores = {k: (doc.creado_en if v == "ahora" else e.usuario.id if v == "actor" else v) for k, v in cambios.items()}
    sentencia = update(Documento).where(Documento.id == doc.id).values(**valores)
    with pytest.raises(IntegrityError):
        await e.db.execute(sentencia)
    await e.db.rollback()
    assert (await fila(e, doc)).deshabilitado is False, descripcion


async def test_la_base_impide_reactivar_el_cupo_de_un_documento_deshabilitado(e):
    doc = await documento(e)
    await deshabilitar(e, doc)

    reactivar = update(Documento).where(Documento.id == doc.id).values(reserva_activa=True)
    with pytest.raises(IntegrityError):
        await e.db.execute(reactivar)
    await e.db.rollback()
    assert (await fila(e, doc)).reserva_activa is False


async def test_la_base_no_deshabilita_cargas_en_proceso_fallidas_o_sin_publicar(e):
    for anio, construccion in enumerate((
        dict(estado=EstadoProcesamiento.EN_PROCESO), dict(estado=EstadoProcesamiento.FALLIDO),
        dict(estado=EstadoProcesamiento.COMPLETADO, publicado=False),
    ), start=2020):
        doc = await documento(e, tipo=REPORTE, anio=anio, **construccion)
        retiro = update(Documento).where(Documento.id == doc.id).values(
            deshabilitado_en=doc.creado_en, deshabilitado_por=e.usuario.id, motivo_deshabilitacion="x", reserva_activa=False,
        )
        with pytest.raises(IntegrityError):
            await e.db.execute(retiro)
        await e.db.rollback()
        assert (await fila(e, doc)).deshabilitado is False


# ============================================ Elegibilidad y consultas ============================================

async def test_los_consultas_de_elegibilidad_excluyen_al_deshabilitado(e):
    memoria, reporte = await documento(e, MEMORIA), await documento(e, REPORTE)
    otro_anio = await documento(e, MEMORIA, anio=2024)
    en_qa = await documento(e, MEMORIA, ambiente="qa")
    fallido = await documento(e, REPORTE, estado=EstadoProcesamiento.FALLIDO, anio=2023)

    elegibles = await servicio.documentos_elegibles_para_consulta(e.db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025)
    assert set(elegibles) == {memoria.id, reporte.id}

    await deshabilitar(e, memoria)

    assert await servicio.documentos_elegibles_para_consulta(e.db, ambiente=AMBIENTE, empresa_id=e.empresa.id, anio=2025) == [reporte.id]
    todos = [memoria.id, reporte.id, otro_anio.id, en_qa.id, fallido.id]
    # La comprobación posterior (defensa en profundidad) aplica la MISMA regla: el retirado no es recuperable.
    assert await servicio.documentos_recuperables(e.db, ambiente=AMBIENTE, documento_ids=todos) == {reporte.id, otro_anio.id}


async def test_una_empresa_inactiva_no_tiene_documentos_elegibles_para_consulta(e):
    await documento(e)
    empresa_inactiva = await crear_empresa(e.db, nombre="Minera Cerrada", activa=False)
    e.empresa = empresa_inactiva
    await documento(e)
    assert await servicio.documentos_elegibles_para_consulta(e.db, ambiente=AMBIENTE, empresa_id=empresa_inactiva.id, anio=2025) == []


async def test_la_consulta_de_elegibles_valida_sus_argumentos(e):
    with pytest.raises(ValueError):
        await servicio.documentos_elegibles_para_consulta(e.db, ambiente=AMBIENTE, empresa_id="no-es-uuid", anio=2025)


# ============================================ Listado y detalle (aditivo) ============================================

async def test_el_listado_y_el_detalle_distinguen_al_documento_retirado_sin_cambiar_su_estado(e):
    memoria, reporte = await documento(e, MEMORIA), await documento(e, REPORTE)
    await deshabilitar(e, memoria)

    pagina = await operacion_service.listar_documentos(e.db, ambiente=AMBIENTE, empresa_id=e.empresa.id)
    por_id = {i.id: i for i in pagina.items}

    retirado, vigente = por_id[memoria.id], por_id[reporte.id]
    assert (retirado.estado, retirado.deshabilitado, retirado.disponible_para_rag) == (EstadoProgreso.COMPLETADO, True, False)
    assert (retirado.deshabilitado_por, retirado.motivo_deshabilitacion) == (e.usuario.id, MOTIVO)
    assert retirado.deshabilitado_en is not None
    assert retirado.resultado_analisis is not None
    assert (vigente.estado, vigente.deshabilitado, vigente.disponible_para_rag) == (EstadoProgreso.COMPLETADO, False, True)
    assert (vigente.deshabilitado_en, vigente.deshabilitado_por, vigente.motivo_deshabilitacion) == (None, None, None)

    detalle = await operacion_service.obtener_detalle(e.db, memoria.id, AMBIENTE)
    assert (detalle.deshabilitado, detalle.motivo_deshabilitacion, detalle.disponible_para_rag, detalle.estado) == (
        True, MOTIVO, False, EstadoProgreso.COMPLETADO,
    )
    assert detalle.analisis is not None  # el análisis histórico se conserva


async def test_la_respuesta_de_la_operacion_describe_el_registro_original_tambien_en_el_reintento(e):
    doc = await documento(e)
    primera = await operacion_service.deshabilitar_documento(e.db, doc.id, ambiente=AMBIENTE, usuario_id=e.usuario.id, motivo=MOTIVO)
    repetida = await operacion_service.deshabilitar_documento(e.db, doc.id, ambiente=AMBIENTE, usuario_id=e.usuario.id, motivo="Otro")

    assert (primera.ya_estaba_deshabilitado, repetida.ya_estaba_deshabilitado) == (False, True)
    assert primera.model_dump(exclude={"ya_estaba_deshabilitado"}) == repetida.model_dump(exclude={"ya_estaba_deshabilitado"})
    assert (primera.documento_id, primera.empresa_id, primera.anio, primera.tipo) == (doc.id, e.empresa.id, 2025, MEMORIA)
    assert (primera.estado, primera.deshabilitado, primera.motivo_deshabilitacion) == (EstadoProgreso.COMPLETADO, True, MOTIVO)


# ============================================ Cuerpo de la solicitud ============================================

def test_el_cuerpo_exige_un_motivo_no_vacio_y_acotado_y_no_admite_otros_campos():
    assert DeshabilitarDocumentoRequest(motivo="  Año equivocado.  ").motivo == "Año equivocado."
    assert DeshabilitarDocumentoRequest(motivo="x" * MAXIMO_MOTIVO_DESHABILITACION).motivo
    for invalido in ({}, {"motivo": ""}, {"motivo": "   "}, {"motivo": "x" * (MAXIMO_MOTIVO_DESHABILITACION + 1)},
                     {"motivo": "ok", "usuario_id": str(uuid.uuid4())}):
        with pytest.raises(ValidationError):
            DeshabilitarDocumentoRequest(**invalido)


# ============================================ buscar_similares: el filtro por documento va antes de ordenar y limitar ============================================

class SesionVectorialGrabadora:
    """Sesión de la base vectorial que solo registra el SQL y los parámetros (no demuestra pgvector: ver el archivo `*_pgvector`)."""

    def __init__(self):
        self.consultas: list[tuple[str, dict]] = []

    def in_transaction(self) -> bool:
        return False

    async def execute(self, consulta, parametros=None):
        self.consultas.append((str(consulta), dict(parametros or {})))
        return SimpleNamespace(all=lambda: [])

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


async def buscar(documento_ids="omitido", **extra):
    from app.modules.ingesta.domain.embeddings import IdentidadEmbeddings
    from app.modules.ingesta.infrastructure import fragmentos_vectoriales as vectores

    sesion = SesionVectorialGrabadora()
    argumentos = dict(
        ambiente=AMBIENTE, vector_consulta=[0.1] * 1536, identidad=IdentidadEmbeddings("oci", "cohere.embed-v4.0", 1536),
        empresa_ids=[uuid.uuid4()], limite=4, anio=2025,
    )
    if documento_ids != "omitido":
        argumentos["documento_ids"] = documento_ids
    resultado = await vectores.buscar_similares(sesion, **argumentos, **extra)
    return sesion, resultado


async def test_buscar_similares_filtra_por_documento_en_el_where_antes_de_ordenar_y_limitar():
    elegibles = [uuid.uuid4(), uuid.uuid4()]

    sesion, _ = await buscar(elegibles)

    ((sql, parametros),) = sesion.consultas
    filtro = "documento_id = ANY(CAST(:documentos AS uuid[]))"
    assert filtro in sql
    assert parametros["documentos"] == elegibles
    assert sql.index("WHERE") < sql.index(filtro) < sql.index("ORDER BY") < sql.index("LIMIT")  # en SQL, antes del orden y del límite


async def test_buscar_similares_sin_documento_ids_no_filtra_por_documento_y_con_una_lista_vacia_no_consulta():
    sesion, _ = await buscar()
    assert "documento_id = ANY" not in sesion.consultas[0][0]
    assert "documentos" not in sesion.consultas[0][1]

    vacia, resultado = await buscar([])
    assert resultado == []
    assert vacia.consultas == []


@pytest.mark.parametrize("invalido", ["no-es-una-lista", b"bytes", ["no-es-uuid"], [uuid.uuid4(), 7]])
async def test_buscar_similares_valida_los_documento_ids(invalido):
    with pytest.raises(ValueError):
        await buscar(invalido)

"""API HTTP de ingesta (`/documentos`): permisos, contrato de errores, resultados, advertencias, duplicados, progreso,
reintento de publicación, listado e historial y aislamiento por ambiente.

Bases REALES y aisladas (PostgreSQL `initdb` y pgvector en Docker); OCI y MinIO son DOBLES. Si PostgreSQL local o
Docker/pgvector no están disponibles estas pruebas se OMITEN con motivo visible (`pytest -rs`) y no cuentan como
ejecutadas. Los dobles no demuestran nada sobre OCI ni MinIO reales.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.models import SectorEmpresa
from app.models.documento_ingesta import EstadoProcesamiento
from app.services.ingesta import coordinador
from app.services.ingesta.coordinador import DependenciasIngesta, ingerir_documento
from tests.ayudantes_coordinador import (
    TABLA_DETERIORADA,
    TEXTO_CON_HALLAZGOS,
    TEXTO_OBSERVADO,
    ProveedorDoble,
    lector,
)
from tests.ayudantes_http_ingesta import (
    comprobar_error,
    construir_app,
    crear_operacion,
    cliente_http,
    ingerir_por_http,
    subir,
)
from tests.ayudantes_ingesta import AlmacenEnMemoria, crear_empresa

RUTAS = [
    ("post", "/documentos/operaciones"),
    ("post", f"/documentos/operaciones/{uuid.uuid4()}/ingesta"),
    ("get", f"/documentos/operaciones/{uuid.uuid4()}"),
    ("post", f"/documentos/operaciones/{uuid.uuid4()}/reintentar-publicacion"),
    ("get", "/documentos"),
    ("get", f"/documentos/{uuid.uuid4()}"),
]


async def esperar(condicion, segundos: float = 15.0):
    async with asyncio.timeout(segundos):
        while not await condicion():
            await asyncio.sleep(0.02)


# ============================================ Permisos ============================================

@pytest.mark.parametrize("metodo,ruta", RUTAS)
async def test_sin_sesion_todo_es_401(entorno, metodo, ruta):
    async with cliente_http(construir_app(entorno, usuario=None)) as cliente:
        comprobar_error(await getattr(cliente, metodo)(ruta), 401, "INVALID_SESSION")
    assert await entorno.documentos() == []


@pytest.mark.parametrize("metodo,ruta", RUTAS)
async def test_el_administrador_recibe_403_en_todo(entorno, metodo, ruta):
    async with cliente_http(construir_app(entorno, usuario="administrador")) as cliente:
        comprobar_error(await getattr(cliente, metodo)(ruta), 403, "FORBIDDEN")
    assert await entorno.documentos() == []
    assert await entorno.sql("SELECT 1 FROM operaciones_ingesta") == []


async def test_el_administrador_no_puede_ingerir_aunque_envie_un_archivo_valido(entorno):
    app = construir_app(entorno)
    async with cliente_http(app) as cliente:
        operacion_id = await crear_operacion(cliente)
    app.dependency_overrides.clear()
    app2 = construir_app(entorno, usuario="administrador")
    async with cliente_http(app2) as cliente:
        comprobar_error(await subir(cliente, operacion_id, entorno, TEXTO_OBSERVADO), 403, "FORBIDDEN")
    assert await entorno.documentos() == []
    (fila,) = await entorno.sql("SELECT estado FROM operaciones_ingesta")
    assert fila.estado == "CREADA"


# ============================================ Operaciones ============================================

async def test_crear_operacion_devuelve_el_identificador_sin_crear_documentos(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        respuesta = await cliente.post("/documentos/operaciones")
        assert respuesta.status_code == 201
        cuerpo = respuesta.json()
        operacion_id = cuerpo["operacion_id"]
        assert uuid.UUID(operacion_id) and cuerpo["estado"] == "CREADA"
        assert cuerpo["ingesta_url"] == f"/documentos/operaciones/{operacion_id}/ingesta"
        assert cuerpo["progreso_url"] == f"/documentos/operaciones/{operacion_id}"

        # Sin documentos ficticios: ni hash, ni metadatos inventados, ni original.
        assert await entorno.documentos() == []
        assert entorno.almacen.objetos == {}
        (fila,) = await entorno.sql(
            "SELECT ambiente, usuario_id, estado, documento_id FROM operaciones_ingesta WHERE id = :i", i=uuid.UUID(operacion_id)
        )
        assert (fila.ambiente, fila.usuario_id, fila.estado, fila.documento_id) == (
            "development", entorno.usuario.id, "CREADA", None
        )
        detalles = [e.detalle for e in await entorno.auditoria()]
        assert any(f"Operación de ingesta creada; operacion_id={operacion_id}" in d for d in detalles)

        estado = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert estado["estado"] == "CREADA" and estado["terminal"] is False and estado["exitosa"] is False
        assert estado["documento_id"] is None and estado["etapa"] is None and estado["error"] is None


async def test_el_actor_sale_de_la_sesion_y_no_del_payload(entorno):
    otro = uuid.uuid4()
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(
            cliente, entorno, TEXTO_OBSERVADO, usuario_id=str(otro), cargado_por=str(otro)
        )
    assert respuesta.status_code == 201, respuesta.text
    (documento,) = await entorno.documentos()
    assert documento.usuario_id == entorno.usuario.id


# ============================================ Ingesta: ambos resultados, advertencias, duplicados ============================================

async def test_ingesta_con_hallazgos_responde_201_y_el_detalle_trae_el_analisis(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS)
        assert respuesta.status_code == 201, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["estado"] == "COMPLETADO" and cuerpo["exitosa"] is True and cuerpo["terminal"] is True
        assert cuerpo["resultado_analisis"] == "CON_HALLAZGOS" and cuerpo["etapa"] == "FINALIZADO"
        assert cuerpo["motivos"] == ["referencias_gri_catalogadas", "sanciones_economicas"]
        assert cuerpo["fragmentos"] == cuerpo["fragmentos_total"] == cuerpo["fragmentos_procesados"] >= 3
        assert cuerpo["error"] is None and cuerpo["publicacion_reintentable"] is False
        documento_id = cuerpo["documento_id"]

        # El progreso consultado después es el mismo desenlace.
        consulta = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert consulta["estado"] == "COMPLETADO" and consulta["documento_id"] == documento_id

        detalle = (await cliente.get(f"/documentos/{documento_id}")).json()
        assert detalle["estado"] == "COMPLETADO" and detalle["disponible_para_rag"] is True
        assert detalle["operacion_id"] == operacion_id and detalle["sector"] == "MINERIA"
        assert detalle["tipo"] == "MEMORIA_ANUAL" and detalle["anio"] == 2025 and detalle["empresa_nombre"]
        analisis = detalle["analisis"]
        assert analisis["resultado"] == "CON_HALLAZGOS" and {g["codigo"] for g in analisis["gri"]} == {"305"}
        (sancion,) = analisis["sanciones"]
        assert sancion["entidad"]["texto"] == "OEFA" and sancion["cita"] in TEXTO_CON_HALLAZGOS
    (documento,) = await entorno.documentos()
    assert documento.estado_procesamiento is EstadoProcesamiento.COMPLETADO and documento.vector_publicado_en is not None
    assert (documento.clave_original, None) in entorno.almacen.objetos


async def test_ingesta_observada_es_valida_indexada_y_disponible_para_rag(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        _, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        assert respuesta.status_code == 201, respuesta.text
        cuerpo = respuesta.json()
        assert cuerpo["resultado_analisis"] == "OBSERVADO" and cuerpo["exitosa"] is True
        assert cuerpo["motivos"] == ["sin_referencias_gri_catalogadas_ni_sanciones_economicas"]
        detalle = (await cliente.get(f"/documentos/{cuerpo['documento_id']}")).json()
        assert detalle["disponible_para_rag"] is True and detalle["analisis"]["gri"] == detalle["analisis"]["sanciones"] == []
    documento = (await entorno.documentos())[0]
    conteo = await entorno.conteo_vectorial(documento.id)
    assert conteo.total == conteo.publicados == documento.fragmentos_total > 0


async def test_las_advertencias_de_tabla_llegan_en_la_respuesta_la_consulta_y_el_detalle(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO + TABLA_DETERIORADA)
        assert respuesta.status_code == 201, respuesta.text
        codigos = [a["codigo"] for a in respuesta.json()["advertencias"]]
        assert codigos == ["MARKDOWN_TABLE_INCONSISTENT"]
        assert respuesta.json()["resultado_analisis"] == "OBSERVADO"  # la advertencia no cambia la clasificación
        consulta = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert [a["codigo"] for a in consulta["advertencias"]] == codigos
        detalle = (await cliente.get(f"/documentos/{respuesta.json()['documento_id']}")).json()
        assert [a["codigo"] for a in detalle["advertencias"]] == codigos
        assert detalle["cantidad_advertencias"] == 1


async def test_duplicado_por_contenido_y_por_empresa_anio_tipo(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        _, primera = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        assert primera.status_code == 201
        documento_id = primera.json()["documento_id"]

        # Mismo contenido (otro año): SHA-256.
        op2, dup = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO, anio="2024")
        error = comprobar_error(dup, 409, "DOCUMENT_ALREADY_INGESTED")
        assert error["details"]["criterio"] == ["sha256"] and error["details"]["documento_id"] == documento_id
        # Mismo empresa/año/tipo con otro contenido.
        op3, dup2 = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS)
        assert comprobar_error(dup2, 409, "DOCUMENT_ALREADY_INGESTED")["details"]["criterio"] == ["empresa_anio_tipo"]

        # Las operaciones rechazadas quedan RECHAZADAS con el código; no tienen documento.
        for operacion in (op2, op3):
            vista = (await cliente.get(f"/documentos/operaciones/{operacion}")).json()
            assert vista["estado"] == "RECHAZADA" and vista["terminal"] and not vista["exitosa"]
            assert vista["documento_id"] is None and vista["error"]["code"] == "DOCUMENT_ALREADY_INGESTED"
    assert len(await entorno.documentos()) == 1


async def test_empresa_inactiva_se_rechaza_sin_dejar_nada(entorno):
    async with entorno.fabrica_pg() as db:
        inactiva = await crear_empresa(db, "Inactiva SA", activa=False)
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO, empresa_id=str(inactiva.id))
        comprobar_error(respuesta, 400, "COMPANY_INACTIVE")
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "RECHAZADA" and vista["error"]["code"] == "COMPANY_INACTIVE"
    assert await entorno.documentos() == [] and entorno.almacen.objetos == {}
    assert await entorno.sql_vectorial("SELECT 1 FROM fragmentos_documento") == []
    assert any("Rechazo" in str(e.tipo_evento) or "RECHAZO" in str(e.tipo_evento).upper() for e in await entorno.auditoria())


async def test_empresa_inexistente_y_sector_incompatible(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        _, r1 = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO, empresa_id=str(uuid.uuid4()))
        assert r1.status_code == 404
        # El sector NO se acepta del formulario: un campo extra se ignora y el sector sale de la empresa.
        _, r2 = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO, sector="PETROLEO")
        assert r2.status_code == 201
        detalle = (await cliente.get(f"/documentos/{r2.json()['documento_id']}")).json()
        assert detalle["sector"] == "MINERIA"


@pytest.mark.parametrize(
    "cambios",
    [
        {"anio": "1999"}, {"anio": "abc"}, {"anio": "2025.5"}, {"anio": "9999"}, {"anio": ""},
        {"tipo_documento": "OTRO"}, {"empresa_id": "no-es-uuid"},
    ],
)
async def test_metadatos_invalidos_son_422_y_la_operacion_sigue_disponible(entorno, cambios):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        error = comprobar_error(await subir(cliente, operacion_id, entorno, TEXTO_OBSERVADO, **cambios), 422, "REQUEST_VALIDATION_ERROR")
        assert error["details"] and "input" not in str(error["details"])
        # La operación no se consumió: se puede reenviar con metadatos correctos.
        assert (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()["estado"] == "CREADA"
        assert (await subir(cliente, operacion_id, entorno, TEXTO_OBSERVADO)).status_code == 201
    assert len(await entorno.documentos()) == 1


async def test_faltan_el_archivo_o_los_campos_obligatorios_es_422(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        ruta = f"/documentos/operaciones/{operacion_id}/ingesta"
        comprobar_error(await cliente.post(ruta, data={"empresa_id": str(entorno.empresa.id)}), 422, "REQUEST_VALIDATION_ERROR")
        comprobar_error(
            await cliente.post(ruta, files={"archivo": ("x.md", b"# hola", "text/markdown")}), 422, "REQUEST_VALIDATION_ERROR"
        )


@pytest.mark.parametrize(
    "nombre,contenido,estado,codigo",
    [
        ("informe.pdf", b"# hola mundo", 400, "INVALID_FILE_TYPE"),
        ("../x.md", b"# hola mundo", 400, "INVALID_FILE_NAME"),
        ("informe.md", b"\xff\xfe no es utf8", 400, "INVALID_ENCODING"),
        ("informe.md", b"   \n\t  ", 400, "EMPTY_DOCUMENT"),
    ],
)
async def test_archivos_invalidos_se_rechazan_y_la_operacion_queda_rechazada(entorno, nombre, contenido, estado, codigo):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, contenido, nombre)
        comprobar_error(respuesta, estado, codigo)
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "RECHAZADA" and vista["error"]["code"] == codigo and vista["terminal"] is True
    assert await entorno.documentos() == [] and entorno.almacen.objetos == {}


async def test_una_operacion_es_de_un_solo_uso(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, primera = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        assert primera.status_code == 201
        error = comprobar_error(
            await subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS, anio="2024"), 409, "OPERATION_ALREADY_STARTED"
        )
        assert error["details"] == {"estado": "CON_DOCUMENTO"}
    assert len(await entorno.documentos()) == 1


async def test_dos_cargas_simultaneas_sobre_la_misma_operacion_solo_una_procede(entorno):
    entorno.proveedor.espera = 0.3  # la primera sigue en curso cuando llega la segunda
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        respuestas = await asyncio.gather(
            subir(cliente, operacion_id, entorno, TEXTO_OBSERVADO),
            subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS, anio="2024"),
        )
    estados = sorted(r.status_code for r in respuestas)
    assert estados == [201, 409]
    perdedora = next(r for r in respuestas if r.status_code == 409)
    assert perdedora.json()["error"]["code"] == "OPERATION_ALREADY_STARTED"
    assert len(await entorno.documentos()) == 1
    assert len(await entorno.sql("SELECT 1 FROM operaciones_ingesta WHERE documento_id IS NOT NULL")) == 1


async def test_operacion_inexistente_o_ajena_es_404(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        comprobar_error(await cliente.get(f"/documentos/operaciones/{uuid.uuid4()}"), 404, "OPERATION_NOT_FOUND")
        comprobar_error(await subir(cliente, str(uuid.uuid4()), entorno, TEXTO_OBSERVADO), 404, "OPERATION_NOT_FOUND")
        # Una operación creada por otro actor no se puede cargar.
        operacion_id = await crear_operacion(cliente)
    async with entorno.fabrica_pg() as db:
        # Solo puede haber un SuperAdmin: se simula la transferencia del rol a otra cuenta.
        from tests.ayudantes_ingesta import crear_usuario
        await db.execute(text("UPDATE usuarios SET rol = 'ADMINISTRADOR' WHERE id = :i"), {"i": entorno.usuario.id})
        await db.commit()
        otro = await crear_usuario(db)
    async with cliente_http(construir_app(entorno, usuario=otro)) as cliente:
        comprobar_error(await subir(cliente, operacion_id, entorno, TEXTO_OBSERVADO), 404, "OPERATION_NOT_FOUND")
    (fila,) = await entorno.sql("SELECT estado FROM operaciones_ingesta")
    assert fila.estado == "CREADA"
    # Los UUID mal formados son 422 (validación de la ruta).
    async with cliente_http(construir_app(entorno)) as cliente:
        comprobar_error(await cliente.get("/documentos/operaciones/no-es-uuid"), 422, "REQUEST_VALIDATION_ERROR")


# ============================================ Progreso durante la petición ============================================

async def test_el_progreso_es_real_mientras_la_peticion_sigue_en_curso(entorno):
    liberar = asyncio.Event()
    entorno.proveedor.antes_del_lote[1] = liberar.wait  # el lote 0 ya se guardó; el 1 espera
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id = await crear_operacion(cliente)
        carga = asyncio.create_task(subir(cliente, operacion_id, entorno, TEXTO_CON_HALLAZGOS))

        async def en_indexacion():
            vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
            return vista["etapa"] == "INDEXANDO" and vista["fragmentos_procesados"] > 0

        await esperar(en_indexacion)
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "EN_PROCESO" and vista["terminal"] is False and vista["exitosa"] is False
        assert vista["documento_id"] and vista["resultado_analisis"] is None  # no se anuncia nada antes de tiempo
        assert 0 < vista["fragmentos_procesados"] < vista["fragmentos_total"]
        assert not carga.done()

        liberar.set()
        final = await carga
        assert final.status_code == 201
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "COMPLETADO" and vista["fragmentos_procesados"] == vista["fragmentos_total"]
        assert vista["resultado_analisis"] == "CON_HALLAZGOS"


# ============================================ Publicación pendiente y reintento ============================================

async def publicacion_pendiente_por_http(cliente, entorno):
    entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado", veces=1)
    operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS)
    entorno.vectorial.quitar()
    return operacion_id, respuesta


async def test_publicacion_pendiente_es_502_no_es_exito_y_se_reintenta_sin_regenerar(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await publicacion_pendiente_por_http(cliente, entorno)
        error = comprobar_error(respuesta, 502, "VECTOR_PUBLICATION_PENDING")
        assert error["details"]["operacion_id"] == operacion_id and error["details"]["causa"] == "VECTOR_PUBLICATION_FAILED"
        assert error["details"]["reintentar_url"] == f"/documentos/operaciones/{operacion_id}/reintentar-publicacion"
        documento_id = error["details"]["documento_id"]

        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "PUBLICACION_PENDIENTE" and vista["exitosa"] is False and vista["terminal"] is False
        assert vista["resultado_analisis"] is None and vista["publicacion_reintentable"] is True
        assert vista["error"]["code"] == "VECTOR_PUBLICATION_FAILED" and vista["etapa"] == "PUBLICANDO"

        # En el historial tampoco figura como éxito ni disponible para RAG.
        lista = (await cliente.get("/documentos")).json()
        assert [d["estado"] for d in lista["items"]] == ["PUBLICACION_PENDIENTE"]
        assert lista["items"][0]["disponible_para_rag"] is False and lista["items"][0]["resultado_analisis"] is None
        assert (await cliente.get("/documentos", params={"estado": "COMPLETADO"})).json()["total"] == 0
        assert (await cliente.get("/documentos", params={"estado": "PUBLICACION_PENDIENTE"})).json()["total"] == 1
        detalle = (await cliente.get(f"/documentos/{documento_id}")).json()
        assert detalle["publicacion_reintentable"] is True and detalle["disponible_para_rag"] is False
        assert await entorno.sql_vectorial("SELECT 1 FROM fragmentos_consultables") == []

        llamadas = entorno.proveedor.llamadas
        reintento = await cliente.post(f"/documentos/operaciones/{operacion_id}/reintentar-publicacion")
        assert reintento.status_code == 200, reintento.text
        cuerpo = reintento.json()
        assert cuerpo["estado"] == "COMPLETADO" and cuerpo["exitosa"] is True and cuerpo["resultado_analisis"] == "CON_HALLAZGOS"
        assert entorno.proveedor.llamadas == llamadas  # no regeneró embeddings
        assert len(await entorno.sql_vectorial("SELECT 1 FROM fragmentos_consultables")) > 0

        # Ya no hay nada que reintentar.
        error = comprobar_error(
            await cliente.post(f"/documentos/operaciones/{operacion_id}/reintentar-publicacion"), 409, "VECTOR_PUBLICATION_NOT_PENDING"
        )
        assert error["details"] == {"estado": "COMPLETADO"}


async def test_si_el_reintento_vuelve_a_fallar_sigue_pendiente_y_reintentable(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, _ = await publicacion_pendiente_por_http(cliente, entorno)
        entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado")
        error = comprobar_error(
            await cliente.post(f"/documentos/operaciones/{operacion_id}/reintentar-publicacion"), 502, "VECTOR_PUBLICATION_PENDING"
        )
        assert error["details"]["operacion_id"] == operacion_id
        entorno.vectorial.quitar()
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] == "PUBLICACION_PENDIENTE" and vista["publicacion_reintentable"] is True
        assert (await cliente.post(f"/documentos/operaciones/{operacion_id}/reintentar-publicacion")).status_code == 200


async def test_reintentar_solo_aplica_a_una_publicacion_pendiente(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        # Operación sin documento.
        nueva = await crear_operacion(cliente)
        error = comprobar_error(
            await cliente.post(f"/documentos/operaciones/{nueva}/reintentar-publicacion"), 409, "OPERATION_WITHOUT_DOCUMENT"
        )
        assert error["details"] == {"estado": "CREADA"}
        # Documento fallido: no se reintenta nada (nada se publica de un intento fallido).
        entorno.proveedor.fallar_en_lote = 0
        operacion_id, falla = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        assert falla.status_code == 502
        error = comprobar_error(
            await cliente.post(f"/documentos/operaciones/{operacion_id}/reintentar-publicacion"), 409, "VECTOR_PUBLICATION_NOT_PENDING"
        )
        assert error["details"]["estado"] == "FALLIDO"
        comprobar_error(
            await cliente.post(f"/documentos/operaciones/{uuid.uuid4()}/reintentar-publicacion"), 404, "OPERATION_NOT_FOUND"
        )


# ============================================ Contrato de errores del flujo ============================================

async def test_fallo_del_proveedor_de_embeddings_es_502_sin_filtrar_detalles_y_el_historial_lo_muestra(entorno):
    entorno.proveedor.fallar_en_lote = 1
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS)
        error = comprobar_error(respuesta, 502, "EMBEDDING_PROVIDER_ERROR")
        assert "detalle interno del proveedor" not in respuesta.text and "OEFA" not in respuesta.text
        assert "RuntimeError" in str(error["details"]) or error["details"] is not None  # solo la clase, nunca el texto

        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["estado"] in ("FALLIDO", "FALLIDO_LIMPIEZA_PENDIENTE")
        assert vista["error"]["code"] == "EMBEDDING_PROVIDER_ERROR" and vista["exitosa"] is False
        assert "detalle interno" not in str(vista)
        # Historial: aparece como fallido, sin análisis; el original y los fragmentos se limpiaron.
        lista = (await cliente.get("/documentos", params={"estado": vista["estado"]})).json()
        assert lista["total"] == 1 and lista["items"][0]["resultado_analisis"] is None
        detalle = (await cliente.get(f"/documentos/{vista['documento_id']}")).json()
        assert detalle["analisis"] is None and detalle["disponible_para_rag"] is False
        assert detalle["error"]["code"] == "EMBEDDING_PROVIDER_ERROR"
    assert entorno.almacen.objetos == {}
    assert await entorno.sql_vectorial("SELECT 1 FROM fragmentos_documento") == []


async def test_timeout_del_proveedor_es_504(entorno):
    entorno.proveedor.fallar_en_lote = 0
    entorno.proveedor.error = TimeoutError("el proveedor tardó demasiado con texto del documento")
    async with cliente_http(construir_app(entorno)) as cliente:
        _, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        comprobar_error(respuesta, 504, "EMBEDDING_PROVIDER_TIMEOUT")
        assert "texto del documento" not in respuesta.text


async def test_fallo_interno_del_analisis_es_500_con_su_codigo_y_no_es_observado(entorno, monkeypatch):
    from app.services.ingesta.analisis_ingesta import AnalisisIngestaError

    def detector_roto(_documento):
        raise AnalisisIngestaError(
            "INGESTION_ANALYSIS_FAILED", "El análisis del documento no pudo completarse.", details={"detector": "gri"}
        )

    monkeypatch.setattr(coordinador, "analizar_documento", detector_roto)
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        error = comprobar_error(respuesta, 500, "INGESTION_ANALYSIS_FAILED")
        assert error["details"] == {"detector": "gri"}
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["resultado_analisis"] is None and vista["exitosa"] is False
        assert vista["error"]["code"] == "INGESTION_ANALYSIS_FAILED"
    assert entorno.almacen.objetos == {}  # no se incorpora al corpus


async def test_un_error_inesperado_es_500_generico_sin_detalles(entorno, monkeypatch):
    def explota(_documento):
        raise RuntimeError("secreto: contenido del documento y credenciales")

    monkeypatch.setattr(coordinador, "analizar_documento", explota)
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        comprobar_error(respuesta, 500, "INTERNAL_ERROR")
        assert "secreto" not in respuesta.text and "credenciales" not in respuesta.text
        vista = (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()
        assert vista["error"]["code"] == "UNEXPECTED_ERROR" and "secreto" not in str(vista)


async def test_dimension_de_embeddings_incompatible_es_502_y_no_deja_nada(entorno):
    from app.services.ingesta.embeddings import IdentidadEmbeddings

    entorno.proveedor.identidad = IdentidadEmbeddings("doble", "doble-512", 512)
    async with cliente_http(construir_app(entorno)) as cliente:
        operacion_id, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        comprobar_error(respuesta, 502, "EMBEDDING_DIMENSION_MISMATCH")
        assert (await cliente.get(f"/documentos/operaciones/{operacion_id}")).json()["estado"] == "RECHAZADA"
    assert await entorno.documentos() == []


# ============================================ Listado e historial ============================================

async def poblar(entorno, cliente) -> dict:
    async with entorno.fabrica_pg() as db:
        otra = await crear_empresa(db, "Petrolera Norte", sector=SectorEmpresa.PETROLEO)
    casos = [
        (entorno.empresa, "2023", "MEMORIA_ANUAL", "# Memoria 2023\n\nTexto del año 2023 de la empresa uno.\n"),
        (entorno.empresa, "2024", "REPORTE_SOSTENIBILIDAD_GRI", "# Reporte 2024\n\nSegún GRI 305-1 se reporta alcance 1.\n"),
        (otra, "2024", "MEMORIA_ANUAL", "# Memoria 2024\n\nTexto del año 2024 de la empresa dos.\n"),
        (entorno.empresa, "2025", "MEMORIA_ANUAL", TEXTO_OBSERVADO),
    ]
    ids = {}
    for empresa, anio, tipo, contenido in casos:
        _, r = await ingerir_por_http(cliente, entorno, contenido, empresa_id=str(empresa.id), anio=anio, tipo_documento=tipo)
        assert r.status_code == 201, r.text
        ids[(str(empresa.id), anio, tipo)] = r.json()["documento_id"]
    return {"otra": otra, "ids": ids}


async def test_listado_paginado_filtrable_y_ordenado(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        datos = await poblar(entorno, cliente)
        lista = (await cliente.get("/documentos")).json()
        assert lista["total"] == 4 and lista["pagina"] == 1 and lista["tamano"] == 20 and lista["paginas"] == 1
        # Más recientes primero.
        assert [d["anio"] for d in lista["items"]] == [2025, 2024, 2024, 2023]
        primero = lista["items"][0]
        assert set(primero) >= {"id", "operacion_id", "empresa_id", "empresa_nombre", "sector", "anio", "tipo", "estado",
                                 "resultado_analisis", "disponible_para_rag", "nombre_archivo", "sha256", "creado_en"}
        assert "analisis" not in primero  # el resumen no carga el análisis completo

        # Paginación.
        p1 = (await cliente.get("/documentos", params={"tamano": 3})).json()
        p2 = (await cliente.get("/documentos", params={"tamano": 3, "pagina": 2})).json()
        assert (p1["paginas"], len(p1["items"]), len(p2["items"])) == (2, 3, 1)
        assert {d["id"] for d in p1["items"]}.isdisjoint({d["id"] for d in p2["items"]})
        assert (await cliente.get("/documentos", params={"pagina": 3, "tamano": 3})).json()["items"] == []

        # Filtros.
        por_empresa = (await cliente.get("/documentos", params={"empresa_id": str(datos["otra"].id)})).json()
        assert por_empresa["total"] == 1 and por_empresa["items"][0]["empresa_nombre"] == "Petrolera Norte"
        assert por_empresa["items"][0]["sector"] == "PETROLEO"
        assert (await cliente.get("/documentos", params={"anio": 2024})).json()["total"] == 2
        assert (await cliente.get("/documentos", params={"tipo": "REPORTE_SOSTENIBILIDAD_GRI"})).json()["total"] == 1
        assert (await cliente.get("/documentos", params={"estado": "COMPLETADO"})).json()["total"] == 4
        assert (await cliente.get("/documentos", params={"estado": "FALLIDO"})).json()["total"] == 0
        combinado = (await cliente.get("/documentos", params={"anio": 2024, "tipo": "MEMORIA_ANUAL", "empresa_id": str(datos["otra"].id)})).json()
        assert combinado["total"] == 1

        for parametros in ({"tamano": 0}, {"tamano": 101}, {"pagina": 0}, {"estado": "XYZ"}, {"tipo": "OTRO"}, {"anio": 1999}, {"empresa_id": "x"}):
            comprobar_error(await cliente.get("/documentos", params=parametros), 422, "REQUEST_VALIDATION_ERROR")


async def test_el_historial_incluye_los_intentos_fallidos_y_los_completados(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        entorno.proveedor.fallar_en_lote = 0
        _, falla = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO, anio="2023")
        assert falla.status_code == 502
        entorno.proveedor.fallar_en_lote = None
        _, bien = await ingerir_por_http(cliente, entorno, TEXTO_CON_HALLAZGOS, anio="2024")
        assert bien.status_code == 201
        lista = (await cliente.get("/documentos")).json()
        assert sorted(d["estado"] for d in lista["items"]) == ["COMPLETADO", "FALLIDO"]
        fallido = next(d for d in lista["items"] if d["estado"] == "FALLIDO")
        assert fallido["disponible_para_rag"] is False and fallido["completado_en"] is None
        # Un intento fallido y limpio libera la reserva: se puede volver a ingerir el mismo archivo.
        _, reintento = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO, anio="2023")
        assert reintento.status_code == 201


async def test_detalle_de_documento_inexistente_o_con_id_invalido(entorno):
    async with cliente_http(construir_app(entorno)) as cliente:
        comprobar_error(await cliente.get(f"/documentos/{uuid.uuid4()}"), 404, "DOCUMENT_NOT_FOUND")
        comprobar_error(await cliente.get("/documentos/no-es-uuid"), 422, "REQUEST_VALIDATION_ERROR")


# ============================================ Aislamiento por ambiente ============================================

async def documento_en_otro_ambiente(entorno, *, pendiente: bool = False):
    """Ingiere un documento en el ambiente `qa` (mismas bases, otro prefijo) y devuelve su id."""
    deps_qa = DependenciasIngesta(entorno.transaccional, entorno.vectorial, AlmacenEnMemoria("qa"), ProveedorDoble())
    if pendiente:
        entorno.vectorial.inyectar("UPDATE fragmentos_documento SET publicado", veces=1)
    try:
        resultado = await ingerir_documento(
            deps_qa, entorno.config, usuario_id=entorno.usuario.id, nombre_archivo="qa.md",
            leer=lector(TEXTO_OBSERVADO.encode("utf-8")), metadatos=entorno.metadatos,
        )
        return resultado.documento_id
    except coordinador.PublicacionVectorialPendiente as error:
        return error.documento_id
    finally:
        entorno.vectorial.quitar()


async def test_un_uuid_de_otro_ambiente_es_404_y_no_se_lee_ni_se_modifica(entorno):
    qa_documento = await documento_en_otro_ambiente(entorno, pendiente=True)
    qa_operacion = uuid.uuid4()
    async with entorno.fabrica_pg() as db:
        await db.execute(text(
            "INSERT INTO operaciones_ingesta (id, ambiente, usuario_id, estado, documento_id, carga_iniciada_en, carga_vigente_hasta) "
            "VALUES (:o, 'qa', :u, 'CON_DOCUMENTO', :d, now(), now())"), {"o": qa_operacion, "u": entorno.usuario.id, "d": qa_documento})
        await db.commit()
    antes_doc = await entorno.sql("SELECT * FROM documentos WHERE id = :i", i=qa_documento)
    antes_op = await entorno.sql("SELECT * FROM operaciones_ingesta WHERE id = :i", i=qa_operacion)
    llamadas_proveedor = entorno.proveedor.llamadas

    async with cliente_http(construir_app(entorno)) as cliente:
        comprobar_error(await cliente.get(f"/documentos/operaciones/{qa_operacion}"), 404, "OPERATION_NOT_FOUND")
        comprobar_error(await cliente.get(f"/documentos/{qa_documento}"), 404, "DOCUMENT_NOT_FOUND")
        comprobar_error(
            await cliente.post(f"/documentos/operaciones/{qa_operacion}/reintentar-publicacion"), 404, "OPERATION_NOT_FOUND"
        )
        comprobar_error(await subir(cliente, str(qa_operacion), entorno, TEXTO_CON_HALLAZGOS), 404, "OPERATION_NOT_FOUND")
        assert (await cliente.get("/documentos")).json()["total"] == 0
        assert (await cliente.get("/documentos", params={"estado": "PUBLICACION_PENDIENTE"})).json()["total"] == 0

    # Nada cambió en las filas del otro ambiente (ni siquiera los intentos de publicación).
    assert await entorno.sql("SELECT * FROM documentos WHERE id = :i", i=qa_documento) == antes_doc
    assert await entorno.sql("SELECT * FROM operaciones_ingesta WHERE id = :i", i=qa_operacion) == antes_op
    assert entorno.proveedor.llamadas == llamadas_proveedor


async def test_el_reintento_del_coordinador_tambien_comprueba_el_ambiente(entorno):
    from app.exceptions import NotFoundError

    qa_documento = await documento_en_otro_ambiente(entorno, pendiente=True)
    antes = await entorno.sql("SELECT vector_publicacion_intentos, vector_publicado_en FROM documentos WHERE id = :i", i=qa_documento)
    with pytest.raises(NotFoundError) as error:
        await coordinador.reintentar_publicacion(entorno.deps, qa_documento)
    assert error.value.code == "DOCUMENT_NOT_FOUND"
    assert await entorno.sql("SELECT vector_publicacion_intentos, vector_publicado_en FROM documentos WHERE id = :i", i=qa_documento) == antes
    async with entorno.fabrica_pg() as db:
        from app.services.ingesta import documento_service as servicio
        with pytest.raises(NotFoundError):
            await servicio.obtener_progreso(db, qa_documento, ambiente="development")
        assert (await servicio.obtener_progreso(db, qa_documento, ambiente="qa")).documento_id == qa_documento


async def test_los_duplicados_se_evaluan_por_ambiente(entorno):
    await documento_en_otro_ambiente(entorno)  # mismo archivo y metadatos en qa
    async with cliente_http(construir_app(entorno)) as cliente:
        _, respuesta = await ingerir_por_http(cliente, entorno, TEXTO_OBSERVADO)
        assert respuesta.status_code == 201
        assert (await cliente.get("/documentos")).json()["total"] == 1

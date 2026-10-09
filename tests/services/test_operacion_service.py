"""Operaciones de ingesta, vista de progreso, listado e historial y detalle (`operacion_service`).

Nivel: SQLite aislado (sin PostgreSQL, Docker ni servicios externos): corre también en CI. Las restricciones y los índices se
ejecutan de verdad; lo que SQLite NO demuestra (concurrencia, CHECK sobre JSONB, bloqueos) se prueba contra PostgreSQL en
`test_documentos_http.py`, `test_gestor_ingesta.py` y `test_operaciones_ingesta_sql.py`.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

from app.exceptions import ConflictError, NotFoundError
from app.models import SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoOperacion,
    EstadoProcesamiento,
    OperacionIngesta,
    ResultadoAnalisis,
)
from app.schemas_ingesta import EstadoOperacionPublico
from app.services.ingesta import documento_service as servicio
from app.services.ingesta import operacion_service as ops
from app.services.ingesta.documento_service import EstadoProgreso
from tests.ayudantes_ingesta import SesionSQLite, crear_empresa, crear_usuario, metadatos, sha256_de

VIGENCIA = timedelta(minutes=5)
ANALISIS = {"resultado": "CON_HALLAZGOS", "version_catalogo": "v", "motivos": ["m"], "gri": [{"codigo": "305"}],
            "sanciones": [], "advertencias": []}


@pytest.fixture
async def e():
    db = SesionSQLite()
    usuario = await crear_usuario(db)
    empresa = await crear_empresa(db)
    yield SimpleNamespace(db=db, usuario=usuario, empresa=empresa)
    db.cerrar()


async def operacion_en_carga(e, ambiente="development") -> uuid.UUID:
    creada = await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente=ambiente)
    await ops.tomar_carga(e.db, creada.operacion_id, usuario_id=e.usuario.id, ambiente=ambiente, vigencia=VIGENCIA)
    return creada.operacion_id


async def documento_con_operacion(e, n=1, *, anio=2025, tipo=TipoDocumento.MEMORIA_ANUAL, empresa=None):
    operacion_id = await operacion_en_carga(e)
    empresa = empresa or e.empresa
    documento = await servicio.reservar_documento(
        e.db, metadatos=metadatos(empresa, anio=anio, tipo=tipo), nombre_archivo=f"doc{n}.md",
        sha256=sha256_de(f"contenido {n}".encode()), tamano_bytes=100 + n, usuario_id=e.usuario.id,
        ambiente="development", operacion_id=operacion_id,
    )
    return operacion_id, documento


async def completar(e, documento_id, *, publicado=True, resultado=ResultadoAnalisis.CON_HALLAZGOS, analisis=ANALISIS):
    ahora = datetime.now(timezone.utc)
    valores = dict(
        estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=resultado, completado_en=ahora,
        original_almacenado_en=ahora, analisis=analisis, etapa_actual="FINALIZADO" if publicado else "PUBLICANDO",
        fragmentos_total=4, fragmentos_procesados=4, vector_escritura_intentada_en=ahora,
        vector_publicado_en=ahora if publicado else None,
        advertencias=[{"codigo": "A", "categoria": "c", "mensaje": "m", "detalles": {}}],
    )
    if not publicado:
        valores["vector_ultimo_error"] = "VECTOR_PUBLICATION_FAILED"
    await e.db.execute(update(Documento).where(Documento.id == documento_id).values(**valores))
    await e.db.commit()


async def fallar(e, documento_id, *, limpieza_pendiente=False):
    ahora = datetime.now(timezone.utc)
    valores = dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="EMBEDDING_PROVIDER_ERROR", fallido_en=ahora)
    if limpieza_pendiente:
        valores["estado_compensacion"] = EstadoCompensacion.PENDIENTE
    else:
        valores.update(reserva_activa=False)
    await e.db.execute(update(Documento).where(Documento.id == documento_id).values(**valores))
    await e.db.commit()


# ============================================ Crear y tomar la carga ============================================

async def test_crear_operacion_registra_ambiente_actor_y_auditoria_sin_documento(e):
    creada = await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente="qa")
    assert creada.estado is EstadoOperacionPublico.CREADA
    assert creada.ingesta_url == f"/documentos/operaciones/{creada.operacion_id}/ingesta"
    fila = (await e.db.execute(select(OperacionIngesta))).scalar_one()
    assert (fila.ambiente, fila.usuario_id, fila.estado, fila.documento_id) == ("qa", e.usuario.id, "CREADA", None)
    assert (await e.db.execute(select(Documento))).scalars().all() == []
    auditoria = (await e.db.execute(text("SELECT detalle FROM auditoria"))).scalars().all()
    assert auditoria == [f"Operación de ingesta creada; operacion_id={creada.operacion_id}"]


async def test_un_ambiente_invalido_no_crea_nada(e):
    with pytest.raises(ValueError):
        await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente="Producción!")
    assert (await e.db.execute(select(OperacionIngesta))).scalars().all() == []


async def test_solo_una_carga_toma_la_operacion(e):
    creada = await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente="development")
    argumentos = dict(usuario_id=e.usuario.id, ambiente="development", vigencia=VIGENCIA)
    await ops.tomar_carga(e.db, creada.operacion_id, **argumentos)
    fila = (await e.db.execute(select(OperacionIngesta))).scalar_one()
    assert fila.estado == "EN_CARGA"
    assert fila.carga_iniciada_en
    assert fila.carga_vigente_hasta > fila.carga_iniciada_en
    with pytest.raises(ConflictError) as segunda:
        await ops.tomar_carga(e.db, creada.operacion_id, **argumentos)
    assert segunda.value.code == "OPERATION_ALREADY_STARTED"
    assert segunda.value.details == {"estado": "EN_CARGA"}


async def test_tomar_la_carga_exige_ambiente_y_actor_propios(e):
    creada = await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente="development")
    otro = SimpleNamespace(id=uuid.uuid4())
    for argumentos in (
        dict(usuario_id=e.usuario.id, ambiente="qa"),  # otro ambiente
        dict(usuario_id=otro.id, ambiente="development"),  # otro actor
    ):
        with pytest.raises(NotFoundError) as error:
            await ops.tomar_carga(e.db, creada.operacion_id, vigencia=VIGENCIA, **argumentos)
        assert error.value.code == "OPERATION_NOT_FOUND"
    valor_uuid_uuid4 = uuid.uuid4()
    with pytest.raises(NotFoundError):
        await ops.tomar_carga(e.db, valor_uuid_uuid4, usuario_id=e.usuario.id, ambiente="development", vigencia=VIGENCIA)
    assert (await e.db.execute(select(OperacionIngesta.estado))).scalar_one() == "CREADA"  # nada cambió


# ============================================ Enlace con el documento ============================================

async def test_reservar_enlaza_la_operacion_en_la_misma_transaccion(e):
    operacion_id, documento = await documento_con_operacion(e)
    fila = (await e.db.execute(select(OperacionIngesta))).scalar_one()
    assert (fila.estado, fila.documento_id) == ("CON_DOCUMENTO", documento.id)
    vista = await ops.obtener_vista(e.db, operacion_id, "development")
    assert vista.estado is EstadoOperacionPublico.EN_PROCESO
    assert vista.documento_id == documento.id


@pytest.mark.parametrize("variante", ["sin_cargar", "otro_ambiente", "ya_enlazada", "otro_actor"])
async def test_si_la_operacion_no_esta_en_carga_no_se_reserva_ningun_documento(e, variante):
    if variante == "otro_ambiente":
        operacion_id = await operacion_en_carga(e, ambiente="qa")
    elif variante == "otro_actor":
        operacion_id = await operacion_en_carga(e)
        await e.db.execute(update(OperacionIngesta).values(usuario_id=uuid.uuid4()))
        await e.db.commit()
    elif variante == "ya_enlazada":
        operacion_id, _ = await documento_con_operacion(e, 1)
    else:
        operacion_id = (await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente="development")).operacion_id
    antes = len((await e.db.execute(select(Documento))).scalars().all())
    valor_metadatos = metadatos(e.empresa, anio=2024)
    valor_sha256_de = sha256_de(b"otro")
    with pytest.raises(ConflictError) as error:
        await servicio.reservar_documento(
            e.db, metadatos=valor_metadatos, nombre_archivo="otro.md", sha256=valor_sha256_de,
            tamano_bytes=10, usuario_id=e.usuario.id, ambiente="development", operacion_id=operacion_id,
        )
    assert getattr(error.value, "code", "") == "OPERATION_STATE_CONFLICT"
    assert len((await e.db.execute(select(Documento))).scalars().all()) == antes  # el documento no quedó reservado


async def test_un_duplicado_no_enlaza_la_operacion(e):
    await documento_con_operacion(e, 1)
    operacion_id = await operacion_en_carga(e)
    valor_metadatos_2 = metadatos(e.empresa, anio=2025)
    valor_sha256_de_2 = sha256_de(b"contenido 1")
    with pytest.raises(ConflictError) as error:
        await servicio.reservar_documento(
            e.db, metadatos=valor_metadatos_2, nombre_archivo="dup.md", sha256=valor_sha256_de_2,
            tamano_bytes=10, usuario_id=e.usuario.id, ambiente="development", operacion_id=operacion_id,
        )
    assert error.value.code == "DOCUMENT_ALREADY_INGESTED" or error.value.code == "DOCUMENT_UPLOAD_IN_PROGRESS"
    fila = (await e.db.execute(select(OperacionIngesta).where(OperacionIngesta.id == operacion_id))).scalar_one()
    assert (fila.estado, fila.documento_id) == ("EN_CARGA", None)


async def test_marcar_rechazada_solo_aplica_a_una_carga_sin_documento(e):
    sin_documento = await operacion_en_carga(e)
    assert await ops.marcar_rechazada(
        e.db, sin_documento, "development", codigo="FILE_TOO_LARGE", mensaje="x" * 500, estado_http=413
    ) is True
    fila = (await e.db.execute(select(OperacionIngesta).where(OperacionIngesta.id == sin_documento))).scalar_one()
    assert (fila.estado, fila.codigo_error, fila.estado_http, len(fila.mensaje_error)) == ("RECHAZADA", "FILE_TOO_LARGE", 413, 300)
    # Una segunda marca no cambia nada, y el código inseguro se normaliza.
    assert await ops.marcar_rechazada(e.db, sin_documento, "development", codigo="otro", mensaje="m", estado_http=400) is False

    con_documento, _ = await documento_con_operacion(e, 2)
    assert await ops.marcar_rechazada(e.db, con_documento, "development", codigo="X", mensaje="m", estado_http=400) is False
    fila = (await e.db.execute(select(OperacionIngesta).where(OperacionIngesta.id == con_documento))).scalar_one()
    assert fila.estado == "CON_DOCUMENTO"
    assert fila.codigo_error is None

    otra = await operacion_en_carga(e)
    assert await ops.marcar_rechazada(e.db, otra, "qa", codigo="X", mensaje="m", estado_http=400) is False  # otro ambiente
    assert await ops.marcar_rechazada(e.db, otra, "development", codigo="codigo malo!", mensaje="m", estado_http=500) is True
    assert (await e.db.execute(select(OperacionIngesta.codigo_error).where(OperacionIngesta.id == otra))).scalar_one() == "UNEXPECTED_ERROR"


# ============================================ Vista derivada ============================================

async def test_estados_sin_documento(e):
    creada = (await ops.crear_operacion(e.db, usuario_id=e.usuario.id, ambiente="development")).operacion_id
    v = await ops.obtener_vista(e.db, creada, "development")
    assert (v.estado, v.terminal, v.exitosa, v.error, v.documento_id, v.etapa) == (EstadoOperacionPublico.CREADA, False, False, None, None, None)

    en_carga = await operacion_en_carga(e)
    v = await ops.obtener_vista(e.db, en_carga, "development")
    assert v.estado is EstadoOperacionPublico.VALIDANDO
    assert v.terminal is False
    assert v.error is None

    await e.db.execute(update(OperacionIngesta).where(OperacionIngesta.id == en_carga).values(
        carga_vigente_hasta=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await e.db.commit()
    v = await ops.obtener_vista(e.db, en_carga, "development")
    assert v.estado is EstadoOperacionPublico.INTERRUMPIDA
    assert v.error.code == "UPLOAD_INTERRUPTED"
    assert not v.terminal

    await ops.marcar_rechazada(e.db, en_carga, "development", codigo="COMPANY_INACTIVE", mensaje="La empresa está inactiva.", estado_http=400)
    v = await ops.obtener_vista(e.db, en_carga, "development")
    assert v.estado is EstadoOperacionPublico.RECHAZADA
    assert v.terminal
    assert not v.exitosa
    assert (v.error.code, v.error.message) == ("COMPANY_INACTIVE", "La empresa está inactiva.")

    with pytest.raises(NotFoundError):
        await ops.obtener_vista(e.db, en_carga, "qa")  # otro ambiente: inexistente
    valor_uuid_uuid4_2 = uuid.uuid4()
    with pytest.raises(NotFoundError):
        await ops.obtener_vista(e.db, valor_uuid_uuid4_2, "development")


async def test_estados_con_documento_y_su_exito(e):
    casos = {}
    for n, (nombre, preparar) in enumerate({
        "proceso": None,
        "completado": lambda d: completar(e, d),
        "observado": lambda d: completar(e, d, resultado=ResultadoAnalisis.OBSERVADO, analisis={**ANALISIS, "resultado": "OBSERVADO"}),
        "pendiente": lambda d: completar(e, d, publicado=False),
        "fallido": lambda d: fallar(e, d),
        "limpieza": lambda d: fallar(e, d, limpieza_pendiente=True),
    }.items(), start=1):
        operacion_id, documento = await documento_con_operacion(e, n, anio=2000 + n)
        if preparar is not None:
            await preparar(documento.id)
        casos[nombre] = await ops.obtener_vista(e.db, operacion_id, "development")

    esperado = {
        "proceso": (EstadoOperacionPublico.EN_PROCESO, False, False, None, None),
        "completado": (EstadoOperacionPublico.COMPLETADO, True, True, None, ResultadoAnalisis.CON_HALLAZGOS),
        "observado": (EstadoOperacionPublico.COMPLETADO, True, True, None, ResultadoAnalisis.OBSERVADO),
        "pendiente": (EstadoOperacionPublico.PUBLICACION_PENDIENTE, False, False, "VECTOR_PUBLICATION_FAILED", None),
        "fallido": (EstadoOperacionPublico.FALLIDO, True, False, "EMBEDDING_PROVIDER_ERROR", None),
        "limpieza": (EstadoOperacionPublico.FALLIDO_LIMPIEZA_PENDIENTE, False, False, "EMBEDDING_PROVIDER_ERROR", None),
    }
    for nombre, (estado, terminal, exitosa, codigo, resultado) in esperado.items():
        v = casos[nombre]
        assert (v.estado, v.terminal, v.exitosa) == (estado, terminal, exitosa), nombre
        assert (v.error.code if v.error else None) == codigo, nombre
        assert (v.resultado_analisis.value if v.resultado_analisis else None) == (resultado.value if resultado else None), nombre
    # Un COMPLETADO transaccional con la publicación pendiente no anuncia resultado y es reintentable.
    assert casos["pendiente"].publicacion_reintentable
    assert casos["pendiente"].resultado_analisis is None
    assert casos["pendiente"].etapa == "PUBLICANDO"
    assert casos["completado"].etapa == "FINALIZADO"
    assert casos["completado"].fragmentos_procesados == casos["completado"].fragmentos_total == 4
    assert [a["codigo"] for a in casos["completado"].advertencias] == ["A"]
    assert casos["fallido"].error.message.startswith("La ingesta no pudo completarse")


# ============================================ Listado e historial ============================================

async def poblar(e):
    otra = await crear_empresa(e.db, "Petrolera Norte", sector=SectorEmpresa.PETROLEO)
    _, d1 = await documento_con_operacion(e, 1, anio=2023)
    _, d2 = await documento_con_operacion(e, 2, anio=2024, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    _, d3 = await documento_con_operacion(e, 3, anio=2024, empresa=otra)
    _, d4 = await documento_con_operacion(e, 4, anio=2025)
    await completar(e, d1.id)
    await completar(e, d2.id, publicado=False)
    await fallar(e, d3.id)
    return SimpleNamespace(otra=otra, ids=[d1.id, d2.id, d3.id, d4.id])


async def listar(e, **filtros):
    return await ops.listar_documentos(e.db, ambiente="development", **filtros)


async def test_listado_ordenado_filtrado_y_paginado(e):
    datos = await poblar(e)
    todos = await listar(e)
    assert todos.total == 4
    assert todos.paginas == 1
    assert [d.id for d in todos.items] == datos.ids[::-1]  # recientes primero
    assert {d.id: d.estado for d in todos.items} == {
        datos.ids[0]: EstadoProgreso.COMPLETADO, datos.ids[1]: EstadoProgreso.PUBLICACION_PENDIENTE,
        datos.ids[2]: EstadoProgreso.FALLIDO, datos.ids[3]: EstadoProgreso.EN_PROCESO,
    }
    rag = {d.id: d.disponible_para_rag for d in todos.items}
    assert rag == {datos.ids[0]: True, datos.ids[1]: False, datos.ids[2]: False, datos.ids[3]: False}
    assert all(d.operacion_id for d in todos.items)

    assert (await listar(e, empresa_id=datos.otra.id)).total == 1
    assert (await listar(e, anio=2024)).total == 2
    assert (await listar(e, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)).total == 1
    for estado, cantidad in ((EstadoProgreso.COMPLETADO, 1), (EstadoProgreso.PUBLICACION_PENDIENTE, 1), (EstadoProgreso.FALLIDO, 1),
                             (EstadoProgreso.EN_PROCESO, 1), (EstadoProgreso.FALLIDO_LIMPIEZA_PENDIENTE, 0)):
        assert (await listar(e, estado=estado)).total == cantidad, estado
    assert (await listar(e, anio=2024, empresa_id=datos.otra.id, estado=EstadoProgreso.FALLIDO)).total == 1
    assert (await listar(e, anio=2024, empresa_id=datos.otra.id, estado=EstadoProgreso.COMPLETADO)).total == 0

    pagina1, pagina2 = await listar(e, tamano=3), await listar(e, tamano=3, pagina=2)
    assert (pagina1.paginas, len(pagina1.items), len(pagina2.items)) == (2, 3, 1)
    assert (await listar(e, tamano=3, pagina=3)).items == []
    assert (await ops.listar_documentos(e.db, ambiente="qa")).total == 0  # otro ambiente: vacío


@pytest.mark.parametrize("argumentos", [dict(pagina=0), dict(tamano=0), dict(tamano=101), dict(pagina=-1)])
async def test_parametros_de_pagina_invalidos(e, argumentos):
    with pytest.raises(ValueError):
        await listar(e, **argumentos)


async def test_el_detalle_trae_el_analisis_y_respeta_el_ambiente(e):
    datos = await poblar(e)
    completo = await ops.obtener_detalle(e.db, datos.ids[0], "development")
    assert completo.estado is EstadoProgreso.COMPLETADO
    assert completo.analisis["gri"] == [{"codigo": "305"}]
    assert completo.error is None
    assert completo.disponible_para_rag
    assert completo.empresa_nombre == e.empresa.nombre
    pendiente = await ops.obtener_detalle(e.db, datos.ids[1], "development")
    assert pendiente.publicacion_reintentable
    assert pendiente.error.code == "VECTOR_PUBLICATION_FAILED"
    assert pendiente.resultado_analisis is None
    assert not pendiente.disponible_para_rag
    fallido = await ops.obtener_detalle(e.db, datos.ids[2], "development")
    assert fallido.error.code == "EMBEDDING_PROVIDER_ERROR"
    assert fallido.sector is SectorEmpresa.PETROLEO
    en_proceso = await ops.obtener_detalle(e.db, datos.ids[3], "development")
    assert en_proceso.estado is EstadoProgreso.EN_PROCESO
    assert en_proceso.error is None
    assert en_proceso.analisis is None
    with pytest.raises(NotFoundError) as error:
        await ops.obtener_detalle(e.db, datos.ids[0], "qa")
    assert error.value.code == "DOCUMENT_NOT_FOUND"
    valor_uuid_uuid4_3 = uuid.uuid4()
    with pytest.raises(NotFoundError):
        await ops.obtener_detalle(e.db, valor_uuid_uuid4_3, "development")

"""Historial del Asistente IA, ENTREGA 1 (guardar, listar y recuperar conversaciones) sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

REAL: las migraciones (hasta la 0009), las restricciones únicas y de verificación, las transacciones, las sesiones concurrentes, la
autenticación y los roles de los endpoints y las reglas de empresa/sector. CONTROLADOS (dobles, sin red, sin OCI ni DeepSeek): los
proveedores de embeddings y de chat de `POST /asistente/consultas`, solo para probar que esa ruta no cambió y que las rutas del
historial no los usan. Cada prueba usa una base NUEVA clonada de una plantilla ya migrada. Sin PostgreSQL se OMITEN (`pytest -rs`).

Ninguna prueba toca QA, UAT ni producción, ni la base configurada en `DATABASE_URL`."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_db
from app.core.database_vectorial import get_vector_db
from app.core.dependencies import get_current_user
from app.core.exceptions import ConflictError
from app.core.exception_handlers import register_exception_handlers
from app.core.request_id import RequestIDMiddleware
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.compatibilidad import verificar_esquema_central
from app.migraciones.motor import migrar
from app.models import RolUsuario, SectorEmpresa
from app.modules.asistente import historial_router, historial_service
from app.modules.asistente import router as asistente
from app.modules.asistente import service as servicio_consultas
from app.modules.asistente.domain.citas import MOTIVO_SIN_FRAGMENTOS
from app.modules.asistente.historial_schemas import GuardarConversacionRequest
from app.modules.asistente.schemas import Afirmacion, Cita, ConsultaResponse
from tests.ayudantes_analisis_gri import cliente_http, comprobar_error, crear_administrador, crear_empresa
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, ejecutar, tablas, url_de_base
from tests.services.test_asistente_postgres import EmbeddingsControlado, LlmControlado

ANIO = 2025
RUTA = "/asistente/conversaciones"
TEXTO = "En 2025 la empresa redujo el consumo de agua en 12 % respecto de 2024."
_PLANTILLAS: dict[str, str] = {}


# ============================================ Entorno ============================================

async def _plantilla(url_pg_aislado: str) -> str:
    """Base con el esquema transaccional COMPLETO (migraciones 0001–0009 reales), creada una vez por clúster."""
    if url_pg_aislado not in _PLANTILLAS:
        url = await base_transaccional_nueva(url_pg_aislado)
        await migrar(url, ESQUEMA_TRANSACCIONAL)
        _PLANTILLAS[url_pg_aislado] = url
    return _PLANTILLAS[url_pg_aislado]


@pytest_asyncio.fixture
async def h(url_pg_aislado):
    plantilla = await _plantilla(url_pg_aislado)
    nombre = f"hist_{uuid.uuid4().hex[:12]}"
    servidor = await conectar(url_pg_aislado)
    try:
        await servidor.execute(f'CREATE DATABASE "{nombre}" TEMPLATE "{plantilla.rsplit("/", 1)[1]}"')
    finally:
        await servidor.close()
    url = url_de_base(url_pg_aislado, nombre)
    motor = create_async_engine(url, pool_size=12, max_overflow=0)
    fabrica = async_sessionmaker(motor, expire_on_commit=False)
    async with fabrica() as db:
        ana = await crear_administrador(db, "Ana Admin")
        beto = await crear_administrador(db, "Beto Admin")
        mineria = await crear_empresa(db, "Minera Andina")
        otros = await crear_empresa(db, "Servicios Varios", sector=SectorEmpresa.OTROS)
        cerrada = await crear_empresa(db, "Minera Cerrada", activa=False)
    superadmin = SimpleNamespace(id=uuid.uuid4(), rol=RolUsuario.SUPERADMIN)  # no se persiste: la sesión se sustituye en la prueba
    yield SimpleNamespace(
        fabrica=fabrica, url=url, ana=ana, beto=beto, superadmin=superadmin, mineria=mineria, otros=otros, cerrada=cerrada,
        embeddings=EmbeddingsControlado(), llm=LlmControlado(),
    )
    await motor.dispose()


def construir_app(h, usuario, ambiente: str = "development", *, con_consultas: bool = False) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(historial_router.router)

    async def db_de_prueba():
        async with h.fabrica() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    app.dependency_overrides[asistente.obtener_ambiente] = lambda: ambiente
    if usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    if con_consultas:
        async def vectorial_de_prueba():
            yield None

        app.include_router(asistente.router)
        app.dependency_overrides[get_vector_db] = vectorial_de_prueba
        app.dependency_overrides[asistente.esquema_vectorial_listo] = lambda: None
        app.dependency_overrides[asistente.get_proveedor_embeddings] = lambda: h.embeddings
        app.dependency_overrides[asistente.get_proveedor_llm] = lambda: h.llm
    return app


# ============================================ Contenido de prueba ============================================

def cita(empresa_id, referencia: int = 1, anio: int = ANIO, seccion: str = "Agua") -> Cita:
    return Cita(
        referencia=referencia, documento_id=uuid.uuid4(), documento_nombre="memoria-2025.md", empresa_id=empresa_id,
        empresa_nombre="Minera Andina", anio=anio, seccion=seccion, extracto=TEXTO,
    )


def con_evidencia(empresa_id, citas: int = 2, anio: int = ANIO) -> ConsultaResponse:
    return ConsultaResponse(
        respuesta="".join(f"Afirmación {n}. [{n}]\n" for n in range(1, citas + 1)).rstrip(),
        afirmaciones=[
            Afirmacion(texto=f"Afirmación {n}.", referencias=[n], cita_textual="redujo el consumo de agua en 12 %") for n in range(1, citas + 1)
        ],
        citas=[cita(empresa_id, n, anio=anio, seccion=f"Sección {n}") for n in range(1, citas + 1)],
        afirmaciones_descartadas=1,
    )


def sin_evidencia() -> ConsultaResponse:
    return ConsultaResponse(
        respuesta="", sin_evidencia=True, motivo=MOTIVO_SIN_FRAGMENTOS,
        mensaje="No hay documentos publicados de esa empresa, sector y año que permitan responder.",
    )


def pregunta(texto: str) -> dict:
    return {"rol": "usuario", "contenido": texto}


def respuesta(r: ConsultaResponse) -> dict:
    return {"rol": "asistente", "respuesta": r.model_dump(mode="json")}


def cuerpo(h, *, empresa=None, sector: str | None = None, clave: uuid.UUID | None = None, mensajes=None, anio: int = ANIO) -> dict:
    empresa = empresa or h.mineria
    return {
        "clave_idempotencia": str(clave or uuid.uuid4()),
        "empresa_id": str(empresa.id),
        "sector": sector or empresa.sector.value,
        "anio": anio,
        "mensajes": mensajes
        if mensajes is not None
        else [pregunta("¿Cómo evolucionó el consumo de agua?"), respuesta(con_evidencia(empresa.id)), pregunta("¿Y las emisiones?"), respuesta(sin_evidencia())],
    }


async def guardar(h, datos: dict, usuario=None, ambiente: str = "development"):
    async with cliente_http(construir_app(h, usuario or h.ana, ambiente)) as c:
        return await c.post(RUTA, json=datos)


async def listar(h, usuario=None, ambiente: str = "development", **parametros):
    async with cliente_http(construir_app(h, usuario or h.ana, ambiente)) as c:
        return await c.get(RUTA, params=parametros)


async def recuperar(h, conversacion_id, usuario=None, ambiente: str = "development"):
    async with cliente_http(construir_app(h, usuario or h.ana, ambiente)) as c:
        return await c.get(f"{RUTA}/{conversacion_id}")


async def contar(h, tabla: str) -> int:
    filas = await consultar(h.url, f"SELECT count(*) AS n FROM {tabla}")
    return filas[0]["n"]


async def contar_todo(h) -> tuple[int, int]:
    return await contar(h, "conversaciones_asistente"), await contar(h, "mensajes_asistente")


# ============================================ 1. Guardado y recuperación completa ============================================

async def test_guardar_y_recuperar_conserva_orden_citas_afirmaciones_y_contexto(h):
    datos = cuerpo(h)
    guardada = await guardar(h, datos)
    assert guardada.status_code == 201, guardada.text
    g = guardada.json()
    assert g["reutilizada"] is False
    assert g["titulo"] == "¿Cómo evolucionó el consumo de agua?"
    assert g["total_mensajes"] == 4

    recuperada = await recuperar(h, g["id"])
    assert recuperada.status_code == 200, recuperada.text
    r = recuperada.json()
    assert r["contexto"] == {
        "empresa_id": str(h.mineria.id), "empresa_nombre": "Minera Andina", "empresa_activa": True, "sector": "MINERIA", "anio": ANIO,
    }
    assert [m["orden"] for m in r["mensajes"]] == [1, 2, 3, 4]
    assert [m["rol"] for m in r["mensajes"]] == ["usuario", "asistente", "usuario", "asistente"]
    assert [m["contenido"] for m in r["mensajes"]][::2] == ["¿Cómo evolucionó el consumo de agua?", "¿Y las emisiones?"]
    assert r["mensajes"][0]["respuesta"] is None
    assert r["mensajes"][1]["respuesta"] == datos["mensajes"][1]["respuesta"]  # citas y afirmaciones idénticas a las enviadas
    assert [c["referencia"] for c in r["mensajes"][1]["respuesta"]["citas"]] == [1, 2]
    assert r["mensajes"][1]["contenido"] == datos["mensajes"][1]["respuesta"]["respuesta"]
    assert r["mensajes"][3]["respuesta"]["sin_evidencia"] is True
    assert r["mensajes"][3]["respuesta"]["motivo"] == MOTIVO_SIN_FRAGMENTOS
    assert r["mensajes"][3]["contenido"] == datos["mensajes"][3]["respuesta"]["mensaje"]  # sin evidencia: el contenido es la aclaración
    assert {m["procedencia"] for m in r["mensajes"]} == {"APORTADO_POR_CLIENTE"}
    assert r["id"] == g["id"]
    assert r["creada_en"] == g["creada_en"] == r["actualizada_en"]


@pytest.mark.parametrize("motivo", sorted(servicio_consultas._MENSAJES))
async def test_respuesta_sin_evidencia_persiste_la_aclaracion_como_contenido_y_la_estructura_integra(h, motivo):
    """`respuesta` vacía no viola `ck_mensajes_asistente_contenido_no_vacio`: el contenido legible es la aclaración (`mensaje`) y la
    respuesta estructurada (incluida `respuesta: ""`) se conserva íntegra en `datos_respuesta`. Se usa la construcción real del servicio."""
    real = servicio_consultas._sin_evidencia(motivo, descartadas=2)
    datos = cuerpo(h, mensajes=[pregunta("¿Hay datos de agua?"), respuesta(real)])
    guardada = await guardar(h, datos)
    assert guardada.status_code == 201, guardada.text

    filas = await consultar(h.url, "SELECT contenido, datos_respuesta FROM mensajes_asistente WHERE rol = 'asistente'")
    assert len(filas) == 1
    assert filas[0]["contenido"] == real.mensaje
    persistido = filas[0]["datos_respuesta"]
    persistido = json.loads(persistido) if isinstance(persistido, str) else persistido
    assert persistido == real.model_dump(mode="json")
    assert persistido["respuesta"] == ""

    recuperada = (await recuperar(h, guardada.json()["id"])).json()
    assert recuperada["mensajes"][1]["contenido"] == real.mensaje
    assert recuperada["mensajes"][1]["respuesta"] == real.model_dump(mode="json")


async def test_el_servidor_fija_propietario_ambiente_y_procedencia(h):
    guardada = await guardar(h, cuerpo(h), usuario=h.ana, ambiente="qa")
    assert guardada.status_code == 201, guardada.text
    fila = (await consultar(h.url, "SELECT ambiente, usuario_id FROM conversaciones_asistente"))[0]
    assert (fila["ambiente"], fila["usuario_id"]) == ("qa", h.ana.id)
    procedencias = {f["procedencia"] for f in await consultar(h.url, "SELECT procedencia FROM mensajes_asistente")}
    assert procedencias == {"APORTADO_POR_CLIENTE"}


async def test_una_conversacion_del_tamano_maximo_se_guarda_y_se_recupera_completa(h):
    pares = []
    for n in range(20):
        pares += [pregunta(f"Pregunta {n} " + "x" * 1900), respuesta(con_evidencia(h.mineria.id))]
    guardada = await guardar(h, cuerpo(h, mensajes=pares))
    assert guardada.status_code == 201, guardada.text
    recuperada = await recuperar(h, guardada.json()["id"])
    assert recuperada.status_code == 200
    assert [m["orden"] for m in recuperada.json()["mensajes"]] == list(range(1, 41))


async def test_una_conversacion_de_una_empresa_otros_funciona_igual(h):
    guardada = await guardar(h, cuerpo(h, empresa=h.otros))
    assert guardada.status_code == 201, guardada.text
    assert guardada.json()["contexto"]["sector"] == "OTROS"
    assert (await recuperar(h, guardada.json()["id"])).json()["contexto"]["sector"] == "OTROS"


# ============================================ 2. Listado paginado ============================================

async def test_el_listado_esta_paginado_ordenado_y_no_incluye_mensajes(h):
    ids = []
    for n in range(5):
        r = await guardar(h, cuerpo(h, mensajes=[pregunta(f"Pregunta número {n}"), respuesta(con_evidencia(h.mineria.id, 1))]))
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    primera = (await listar(h, pagina=1, tamano=2)).json()
    segunda = (await listar(h, pagina=2, tamano=2)).json()
    tercera = (await listar(h, pagina=3, tamano=2)).json()
    cuarta = (await listar(h, pagina=4, tamano=2)).json()

    assert (primera["pagina"], primera["tamano"], primera["total"]) == (1, 2, 5)
    assert [len(p["items"]) for p in (primera, segunda, tercera, cuarta)] == [2, 2, 1, 0]
    recibidas = [i["id"] for p in (primera, segunda, tercera) for i in p["items"]]
    assert recibidas == list(reversed(ids))  # de la más reciente a la más antigua, sin repetir ni omitir
    item = primera["items"][0]
    assert "mensajes" not in item
    assert set(item) == {"id", "titulo", "contexto", "total_mensajes", "creada_en", "actualizada_en"}
    assert item["titulo"] == "Pregunta número 4"
    assert item["total_mensajes"] == 2
    assert item["contexto"]["empresa_nombre"] == "Minera Andina"


@pytest.mark.parametrize("parametros", [{"pagina": 0}, {"tamano": 0}, {"tamano": 101}, {"pagina": "x"}])
async def test_los_parametros_de_paginacion_invalidos_se_rechazan(h, parametros):
    comprobar_error(await listar(h, **parametros), 422, "REQUEST_VALIDATION_ERROR")


async def test_el_listado_esta_limitado_al_propietario_y_al_ambiente(h):
    mia = await guardar(h, cuerpo(h), usuario=h.ana)
    de_beto = await guardar(h, cuerpo(h, empresa=h.otros), usuario=h.beto)
    en_qa = await guardar(h, cuerpo(h), usuario=h.ana, ambiente="qa")
    assert {mia.status_code, de_beto.status_code, en_qa.status_code} == {201}

    de_ana = (await listar(h, usuario=h.ana)).json()
    de_ana_qa = (await listar(h, usuario=h.ana, ambiente="qa")).json()
    de_beto_dev = (await listar(h, usuario=h.beto)).json()
    sin_nada = (await listar(h, usuario=h.beto, ambiente="qa")).json()

    assert [i["id"] for i in de_ana["items"]] == [mia.json()["id"]]
    assert de_ana["total"] == 1
    assert [i["id"] for i in de_ana_qa["items"]] == [en_qa.json()["id"]]
    assert de_ana_qa["total"] == 1
    assert [i["id"] for i in de_beto_dev["items"]] == [de_beto.json()["id"]]
    assert sin_nada == {"items": [], "pagina": 1, "tamano": 20, "total": 0}


# ============================================ 3. Acceso a conversaciones ajenas ============================================

async def test_inexistente_ajena_y_de_otro_ambiente_responden_exactamente_igual(h):
    propia = (await guardar(h, cuerpo(h), usuario=h.ana)).json()["id"]
    inexistente = await recuperar(h, uuid.uuid4(), usuario=h.ana)
    ajena = await recuperar(h, propia, usuario=h.beto)
    otro_ambiente = await recuperar(h, propia, usuario=h.ana, ambiente="qa")

    for r in (inexistente, ajena, otro_ambiente):
        comprobar_error(r, 404, "CONVERSATION_NOT_FOUND")

    def sin_id(r):
        error = dict(r.json()["error"])
        error.pop("request_id")
        return error

    assert sin_id(inexistente) == sin_id(ajena) == sin_id(otro_ambiente)
    assert propia not in ajena.text
    assert (await recuperar(h, propia, usuario=h.ana)).status_code == 200  # la propietaria sí la ve


async def test_sin_sesion_es_401_y_el_superadmin_403_en_las_tres_rutas(h):
    datos = cuerpo(h)
    conversacion = (await guardar(h, datos)).json()["id"]
    async with cliente_http(construir_app(h, None)) as sin_sesion:
        for r in (await sin_sesion.get(RUTA), await sin_sesion.get(f"{RUTA}/{conversacion}"), await sin_sesion.post(RUTA, json=cuerpo(h))):
            assert r.status_code == 401, r.text
    async with cliente_http(construir_app(h, h.superadmin)) as como_superadmin:
        for r in (await como_superadmin.get(RUTA), await como_superadmin.get(f"{RUTA}/{conversacion}"), await como_superadmin.post(RUTA, json=cuerpo(h))):
            comprobar_error(r, 403, "FORBIDDEN")
    assert (await contar_todo(h))[0] == 1  # nada nuevo se guardó


# ============================================ 4. Rechazos de entrada ============================================

async def test_las_conversaciones_invalidas_se_rechazan_y_no_se_guarda_nada(h):
    secreto = "CONTENIDO-CONFIDENCIAL-98765"
    casos = {
        "vacia": cuerpo(h, mensajes=[]),
        "sin-respuesta": cuerpo(h, mensajes=[pregunta("¿Hola?")]),
        "rol-system": cuerpo(h, mensajes=[pregunta("¿Hola?"), {"rol": "system", "contenido": secreto}]),
        "rol-tool": cuerpo(h, mensajes=[{"rol": "tool", "contenido": secreto}, respuesta(sin_evidencia())]),
        "cita-de-otra-empresa": cuerpo(h, mensajes=[pregunta(secreto), respuesta(con_evidencia(h.otros.id))]),
        "cita-de-otro-anio": cuerpo(h, mensajes=[pregunta(secreto), respuesta(con_evidencia(h.mineria.id, anio=2024))]),
        "demasiados-mensajes": cuerpo(h, mensajes=[pregunta("p"), respuesta(sin_evidencia())] * 21),
        "pregunta-larga": cuerpo(h, mensajes=[pregunta(secreto + "x" * 2000), respuesta(sin_evidencia())]),
    }
    for nombre, datos in casos.items():
        r = await guardar(h, datos)
        assert r.status_code == 422, (nombre, r.text)
        assert r.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR", nombre
        assert secreto not in r.text, nombre  # el contenido recibido no se repite en el error
    assert await contar_todo(h) == (0, 0)


async def test_una_conversacion_que_supera_el_tamano_total_responde_413(h):
    pesada = ConsultaResponse(
        respuesta="r" * 20_000,
        afirmaciones=[Afirmacion(texto="a" * 2000, referencias=[1], cita_textual="c" * 2000) for _ in range(10)],
        citas=[cita(h.mineria.id)],
    )
    mensajes = [pregunta("p" * 2000), respuesta(pesada)] * 20
    r = await guardar(h, cuerpo(h, mensajes=mensajes))
    comprobar_error(r, 413, "CONVERSATION_TOO_LARGE")
    assert await contar_todo(h) == (0, 0)


async def test_el_contexto_se_valida_en_el_servidor(h):
    inconsistente = await guardar(h, cuerpo(h, empresa=h.mineria, sector="ENERGIA"))
    comprobar_error(inconsistente, 400, "COMPANY_SECTOR_MISMATCH")
    otros_como_mineria = await guardar(h, cuerpo(h, empresa=h.otros, sector="MINERIA"))
    comprobar_error(otros_como_mineria, 400, "COMPANY_SECTOR_MISMATCH")
    inactiva = await guardar(h, cuerpo(h, empresa=h.cerrada))
    comprobar_error(inactiva, 400, "COMPANY_INACTIVE")
    neutros = [pregunta("¿Hay documentos publicados?"), respuesta(sin_evidencia())]  # sin citas: el fallo es el de la empresa
    inexistente = await guardar(h, {**cuerpo(h, mensajes=neutros), "empresa_id": str(uuid.uuid4())})
    comprobar_error(inexistente, 404, "COMPANY_NOT_FOUND")
    assert await contar_todo(h) == (0, 0)


# ============================================ 6. Empresa desactivada ============================================

async def test_una_conversacion_de_una_empresa_desactivada_se_recupera_pero_no_habilita_nuevas_consultas(h):
    guardada = (await guardar(h, cuerpo(h))).json()
    await ejecutar(h.url, "UPDATE empresas SET activa = false WHERE id = $1", h.mineria.id)

    recuperada = await recuperar(h, guardada["id"])
    listado = await listar(h)
    assert recuperada.status_code == 200, recuperada.text
    assert recuperada.json()["contexto"]["empresa_activa"] is False
    assert recuperada.json()["mensajes"][1]["respuesta"]["citas"]  # el historial sigue completo
    assert listado.json()["items"][0]["contexto"]["empresa_activa"] is False

    async with cliente_http(construir_app(h, h.ana, con_consultas=True)) as c:
        nueva = await c.post(
            "/asistente/consultas",
            json={"pregunta": "¿Cómo evolucionó el consumo de agua?", "sector": "MINERIA", "empresa_id": str(h.mineria.id), "anio": ANIO},
        )
    comprobar_error(nueva, 400, "COMPANY_INACTIVE")  # recuperar historial no autoriza consultar de nuevo
    assert (h.embeddings.llamadas, h.llm.llamadas) == (0, 0)


# ============================================ 5/7. Atomicidad e idempotencia ============================================

async def test_un_fallo_intermedio_no_deja_una_conversacion_parcial(h, monkeypatch):
    solicitud = GuardarConversacionRequest.model_validate(cuerpo(h))
    original = historial_service.MensajeAsistente

    def con_orden_repetido(**campos):
        campos["orden"] = 1  # el segundo mensaje viola uq_mensajes_asistente_orden DESPUÉS de insertarse la conversación
        return original(**campos)

    monkeypatch.setattr(historial_service, "MensajeAsistente", con_orden_repetido)
    async with h.fabrica() as db:
        with pytest.raises(IntegrityError):
            await historial_service.guardar_conversacion(db, usuario_id=h.ana.id, ambiente="development", payload=solicitud)
    assert await contar_todo(h) == (0, 0)  # ni la conversación ni el primer mensaje quedaron guardados

    monkeypatch.undo()  # sin el fallo inyectado la misma solicitud se guarda completa
    async with h.fabrica() as db:
        guardada = await historial_service.guardar_conversacion(db, usuario_id=h.ana.id, ambiente="development", payload=solicitud)
    assert guardada.reutilizada is False
    assert await contar_todo(h) == (1, 4)


async def test_repetir_la_misma_solicitud_no_duplica_y_devuelve_lo_guardado(h):
    datos = cuerpo(h)
    primera = await guardar(h, datos)
    segunda = await guardar(h, datos)
    tercera = await guardar(h, datos)

    assert (primera.status_code, segunda.status_code, tercera.status_code) == (201, 200, 200)
    assert (primera.json()["reutilizada"], segunda.json()["reutilizada"]) == (False, True)
    assert segunda.json()["id"] == tercera.json()["id"] == primera.json()["id"]
    assert [m["id"] for m in segunda.json()["mensajes"]] == [m["id"] for m in primera.json()["mensajes"]]
    assert await contar_todo(h) == (1, 4)


async def test_la_misma_clave_con_contenido_distinto_es_un_conflicto_explicito(h):
    clave = uuid.uuid4()
    neutros = [pregunta("¿Hay documentos publicados?"), respuesta(sin_evidencia())]  # válidos para cualquier año
    original = cuerpo(h, clave=clave, mensajes=neutros)
    await guardar(h, original)
    distinto_en_la_pregunta = cuerpo(h, clave=clave, mensajes=[pregunta("Otra pregunta"), respuesta(sin_evidencia())])
    otro_anio = {**original, "anio": 2024}
    otra_empresa_y_sector = cuerpo(h, empresa=h.otros, clave=clave, mensajes=neutros)

    for variante in (distinto_en_la_pregunta, otro_anio, otra_empresa_y_sector):
        comprobar_error(await guardar(h, variante), 409, "CONVERSATION_IDEMPOTENCY_CONFLICT")
    assert await contar_todo(h) == (1, 2)  # y lo guardado no cambió
    assert (await guardar(h, original)).status_code == 200


async def test_la_clave_esta_delimitada_por_propietario_y_ambiente(h):
    clave = uuid.uuid4()
    datos = cuerpo(h, clave=clave)
    resultados = [
        await guardar(h, datos, usuario=h.ana, ambiente="development"),
        await guardar(h, datos, usuario=h.beto, ambiente="development"),
        await guardar(h, datos, usuario=h.ana, ambiente="qa"),
    ]
    assert [r.status_code for r in resultados] == [201, 201, 201]
    assert len({r.json()["id"] for r in resultados}) == 3
    assert await contar_todo(h) == (3, 12)


async def test_reintentar_tras_desactivar_la_empresa_devuelve_lo_guardado(h):
    datos = cuerpo(h)
    primera = await guardar(h, datos)
    await ejecutar(h.url, "UPDATE empresas SET activa = false WHERE id = $1", h.mineria.id)
    repetida = await guardar(h, datos)  # es una repetición, no una operación nueva
    assert repetida.status_code == 200
    assert repetida.json()["id"] == primera.json()["id"]
    comprobar_error(await guardar(h, cuerpo(h)), 400, "COMPANY_INACTIVE")  # una conversación nueva sí exige empresa activa


# Las dos pruebas «simultáneas» siguientes comprueban el resultado de punta a punta (HTTP + sesiones reales), pero NO garantizan que las
# solicitudes se solapen dentro de la ventana de la carrera: lo que prueba que decide la restricción única de la base son las pruebas con
# barrera (`test_la_restriccion_unica_resuelve_la_carrera_*`), más abajo.
async def test_solicitudes_simultaneas_con_la_misma_clave_y_contenido_crean_una_sola_conversacion(h):
    datos = cuerpo(h)
    respuestas = await asyncio.gather(*[guardar(h, datos) for _ in range(8)])

    assert sorted(r.status_code for r in respuestas) == [200] * 7 + [201]
    assert len({r.json()["id"] for r in respuestas}) == 1
    assert [r.json()["reutilizada"] for r in respuestas].count(False) == 1
    assert await contar_todo(h) == (1, 4)  # una conversación, sus 4 mensajes, sin duplicados ni parciales


async def test_solicitudes_simultaneas_con_la_misma_clave_y_contenido_distinto_dejan_un_ganador_y_conflictos(h):
    clave = uuid.uuid4()
    a = cuerpo(h, clave=clave, mensajes=[pregunta("Pregunta A"), respuesta(sin_evidencia())])
    b = cuerpo(h, clave=clave, mensajes=[pregunta("Pregunta B"), respuesta(sin_evidencia())])
    respuestas = await asyncio.gather(*[guardar(h, a if n % 2 == 0 else b) for n in range(8)])

    estados = sorted(r.status_code for r in respuestas)
    assert estados.count(201) == 1
    assert set(estados) <= {200, 201, 409}
    assert estados.count(409) == 4  # las 4 del contenido que perdió la carrera
    assert {r.json()["error"]["code"] for r in respuestas if r.status_code == 409} == {"CONVERSATION_IDEMPOTENCY_CONFLICT"}
    assert len({r.json()["id"] for r in respuestas if r.status_code != 409}) == 1
    assert await contar_todo(h) == (1, 2)


def forzar_carrera(monkeypatch, participantes: int = 2) -> None:
    """Obliga a que `participantes` solicitudes pasen la comprobación previa de la clave (que aún no existe) ANTES de que alguna inserte:
    así decide la restricción única de la base, no la comprobación previa. Si una solicitud viera la fila ya guardada no llegaría a la
    barrera y la prueba fallaría por tiempo de espera, en vez de pasar sin ejercitar la carrera."""
    barrera = asyncio.Barrier(participantes)
    original = historial_service._empresa_consultable

    async def con_barrera(*args, **kwargs):
        resultado = await original(*args, **kwargs)
        await asyncio.wait_for(barrera.wait(), 15)
        return resultado

    monkeypatch.setattr(historial_service, "_empresa_consultable", con_barrera)


async def guardar_en_servicio(h, solicitud: GuardarConversacionRequest):
    async with h.fabrica() as db:
        return await historial_service.guardar_conversacion(db, usuario_id=h.ana.id, ambiente="development", payload=solicitud)


async def test_la_restriccion_unica_resuelve_la_carrera_con_el_mismo_contenido(h, monkeypatch):
    solicitud = GuardarConversacionRequest.model_validate(cuerpo(h))
    forzar_carrera(monkeypatch)

    primera, segunda = await asyncio.gather(guardar_en_servicio(h, solicitud), guardar_en_servicio(h, solicitud))

    assert sorted([primera.reutilizada, segunda.reutilizada]) == [False, True]  # una creó y la otra recuperó tras la violación
    assert primera.id == segunda.id
    assert [m.id for m in primera.mensajes] == [m.id for m in segunda.mensajes]
    assert await contar_todo(h) == (1, 4)


async def test_la_restriccion_unica_resuelve_la_carrera_con_contenido_distinto_como_conflicto(h, monkeypatch):
    clave = uuid.uuid4()
    a = GuardarConversacionRequest.model_validate(cuerpo(h, clave=clave, mensajes=[pregunta("Pregunta A"), respuesta(sin_evidencia())]))
    b = GuardarConversacionRequest.model_validate(cuerpo(h, clave=clave, mensajes=[pregunta("Pregunta B"), respuesta(sin_evidencia())]))
    forzar_carrera(monkeypatch)

    resultados = await asyncio.gather(guardar_en_servicio(h, a), guardar_en_servicio(h, b), return_exceptions=True)

    guardadas = [r for r in resultados if not isinstance(r, BaseException)]
    conflictos = [r for r in resultados if isinstance(r, ConflictError)]
    assert len(guardadas) == 1
    assert len(conflictos) == 1
    assert conflictos[0].code == "CONVERSATION_IDEMPOTENCY_CONFLICT"
    assert guardadas[0].reutilizada is False
    assert await contar_todo(h) == (1, 2)


# ============================================ 10/11. Sin IA y consultas intactas ============================================

async def test_guardar_listar_y_recuperar_no_invocan_ia_embeddings_ni_la_base_vectorial(h, monkeypatch):
    llamadas: list[str] = []

    async def prohibida(*args, **kwargs):
        llamadas.append("proveedor")
        raise AssertionError("el historial no puede invocar IA, embeddings ni la base vectorial")

    monkeypatch.setattr(servicio_consultas, "buscar_similares", prohibida)
    monkeypatch.setattr(servicio_consultas, "embeber_consulta", prohibida)
    monkeypatch.setattr(h.llm, "completar", prohibida)
    monkeypatch.setattr(h.embeddings, "generar_embeddings_consulta", prohibida)

    guardada = await guardar(h, cuerpo(h))
    assert guardada.status_code == 201, guardada.text
    assert (await listar(h)).status_code == 200
    assert (await recuperar(h, guardada.json()["id"])).status_code == 200
    assert llamadas == []
    assert (h.embeddings.llamadas, h.llm.llamadas) == (0, 0)


async def test_post_consultas_conserva_su_comportamiento_y_no_guarda_nada_en_el_historial(h, monkeypatch):
    async def sin_fragmentos(*args, **kwargs):
        return []

    monkeypatch.setattr(servicio_consultas, "buscar_similares", sin_fragmentos)
    async with cliente_http(construir_app(h, h.ana, con_consultas=True)) as c:
        r = await c.post(
            "/asistente/consultas",
            json={"pregunta": "¿Cómo evolucionó el consumo de agua?", "sector": "MINERIA", "empresa_id": str(h.mineria.id), "anio": ANIO},
        )
    assert r.status_code == 200, r.text
    cuerpo_consulta = r.json()
    assert set(cuerpo_consulta) == {"respuesta", "afirmaciones", "citas", "sin_evidencia", "motivo", "mensaje", "afirmaciones_descartadas"}
    assert (cuerpo_consulta["sin_evidencia"], cuerpo_consulta["motivo"]) == (True, MOTIVO_SIN_FRAGMENTOS)
    assert h.embeddings.llamadas == 1  # la consulta SÍ usa embeddings: la ruta no cambió
    assert await contar_todo(h) == (0, 0)  # y no guarda nada automáticamente
    assert (await listar(h)).json()["total"] == 0


# ============================================ 9. Migración 0009 ============================================

def esquema_hasta(version: int):
    return dataclasses.replace(ESQUEMA_TRANSACCIONAL, migraciones=ESQUEMA_TRANSACCIONAL.migraciones[:version])


TABLAS_NUEVAS = {"conversaciones_asistente", "mensajes_asistente"}
SQL_CONVERSACION = (
    "INSERT INTO conversaciones_asistente (id, ambiente, usuario_id, empresa_id, sector, anio, titulo, clave_idempotencia, huella_contenido)"
    " VALUES ($1, 'development', $2, $3, 'MINERIA', 2025, 'titulo', $4, $5)"
)
SQL_MENSAJE = (
    "INSERT INTO mensajes_asistente (id, conversacion_id, orden, rol, contenido, datos_respuesta, procedencia)"
    " VALUES ($1, $2, $3, $4, 'contenido', $5::jsonb, $6)"
)


async def test_la_0009_sobre_el_esquema_anterior_conserva_los_datos_y_no_se_repite(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, esquema_hasta(8))
    assert TABLAS_NUEVAS.isdisjoint(await tablas(url))
    antes = {
        "empresas": [tuple(f) for f in await consultar(url, "SELECT * FROM empresas ORDER BY id")],
        "usuarios": [tuple(f) for f in await consultar(url, "SELECT id, correo, rol::text FROM usuarios ORDER BY id")],
        "migraciones": [tuple(f) for f in await consultar(url, "SELECT version, nombre, checksum FROM igualab_migraciones ORDER BY version")],
    }
    assert len(antes["migraciones"]) == 8

    resultado = await migrar(url, esquema_hasta(9))  # el arranque de la versión con la 0009 (la 0010 es de la entrega 2)

    assert (resultado.aplicadas, resultado.adoptadas, resultado.previas) == ((9,), (), (1, 2, 3, 4, 5, 6, 7, 8))
    assert TABLAS_NUEVAS <= await tablas(url)
    assert [tuple(f) for f in await consultar(url, "SELECT * FROM empresas ORDER BY id")] == antes["empresas"]
    assert [tuple(f) for f in await consultar(url, "SELECT id, correo, rol::text FROM usuarios ORDER BY id")] == antes["usuarios"]
    despues = [tuple(f) for f in await consultar(url, "SELECT version, nombre, checksum FROM igualab_migraciones ORDER BY version")]
    assert despues[:8] == antes["migraciones"]  # las migraciones 0001–0008 y sus checksums no cambiaron
    assert despues[8][:2] == (9, "historial_asistente")
    await verificar_esquema_central(url)

    siguiente = await migrar(url, esquema_hasta(9))  # el arranque siguiente no vuelve a aplicarla
    assert (siguiente.sin_cambios, siguiente.previas) == (True, tuple(range(1, 10)))


async def test_una_instalacion_nueva_aplica_la_0009_con_las_demas(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    assert TABLAS_NUEVAS.isdisjoint(await tablas(url))  # `create_all` no crea estas tablas: son de la migración
    resultado = await migrar(url, esquema_hasta(9))
    assert resultado.aplicadas == tuple(range(1, 10))
    assert TABLAS_NUEVAS <= await tablas(url)


async def test_las_restricciones_de_la_base_protegen_la_integridad_aunque_la_aplicacion_falle(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, esquema_hasta(9))  # las restricciones de la 0009; las de la 0010 las prueba `test_continuacion_asistente_postgres.py`
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    conversacion, clave, huella = uuid.uuid4(), uuid.uuid4(), "a" * 64
    conexion = await conectar(url)
    try:
        await conexion.execute(SQL_CONVERSACION, conversacion, usuario, empresa, clave, huella)
        await conexion.execute(SQL_MENSAJE, uuid.uuid4(), conversacion, 1, "usuario", None, "APORTADO_POR_CLIENTE")

        async def viola(sql, *args) -> str:
            with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)) as capturado:
                await conexion.execute(sql, *args)
            return capturado.value.constraint_name

        assert await viola(SQL_CONVERSACION, uuid.uuid4(), usuario, empresa, clave, huella) == "uq_conversaciones_asistente_idempotencia"
        assert await viola(SQL_CONVERSACION, uuid.uuid4(), usuario, empresa, uuid.uuid4(), "malformada") == "ck_conversaciones_asistente_huella"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 1, "usuario", None, "APORTADO_POR_CLIENTE") == "uq_mensajes_asistente_orden"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 0, "usuario", None, "APORTADO_POR_CLIENTE") == "ck_mensajes_asistente_orden_positivo"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 2, "system", None, "APORTADO_POR_CLIENTE") == "ck_mensajes_asistente_rol"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 3, "usuario", "{}", "APORTADO_POR_CLIENTE") == "ck_mensajes_asistente_datos_solo_asistente"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 4, "asistente", None, "APORTADO_POR_CLIENTE") == "ck_mensajes_asistente_datos_solo_asistente"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 5, "asistente", "[]", "APORTADO_POR_CLIENTE") == "ck_mensajes_asistente_datos_objeto"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion, 6, "usuario", None, "SERVIDOR") == "ck_mensajes_asistente_procedencia"
    finally:
        await conexion.execute("DELETE FROM mensajes_asistente; DELETE FROM conversaciones_asistente;")  # la plantilla queda limpia
        await conexion.close()

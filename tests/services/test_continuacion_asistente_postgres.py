"""Historial del Asistente IA, ENTREGA 2 (continuar una conversación guardada y persistir los nuevos intercambios) sobre un
PostgreSQL REAL, AISLADO y TEMPORAL.

REAL: las migraciones (hasta la 0010), las restricciones únicas y de verificación, las transacciones, los bloqueos, las sesiones
concurrentes, la autenticación y los roles, y el pipeline del RAG (`responder_consulta`) con su verificación de afirmaciones y citas.
CONTROLADOS (dobles, sin red, sin OCI ni DeepSeek): los proveedores de embeddings y de chat y la búsqueda vectorial (`buscar_similares`),
que devuelve fragmentos fijos; así se cuentan las llamadas y se sondea la base DURANTE ellas. Sin PostgreSQL se OMITEN (`pytest -rs`).

La concurrencia NO se declara probada por lanzar solicitudes «a la vez»: las carreras se FUERZAN con `asyncio.Barrier` (todas pasan la
lectura previa antes de que alguna escriba) y con eventos que mantienen una ejecución dentro del proveedor; además se cuenta qué
restricción de la base decidió. Ninguna prueba toca QA, UAT ni producción, ni la base configurada en `DATABASE_URL`.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import ConflictError, ExternalServiceError, ExternalServiceTimeoutError, ServiceUnavailableError
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.migraciones.compatibilidad import verificar_esquema_central
from app.migraciones.motor import migrar
from app.models.asistente import ConversacionAsistente
from app.modules.asistente import continuacion_router, continuacion_service, historial_service
from app.modules.asistente.continuacion_service import RecursosRag
from app.modules.asistente import service as servicio_consultas
from app.modules.asistente.domain.citas import MOTIVO_MODELO_SIN_EVIDENCIA, MOTIVO_SIN_FRAGMENTOS
from app.modules.asistente.domain.historial import contenido_canonico
from app.modules.asistente.domain.interpretacion import SISTEMA_INTERPRETACION
from app.modules.asistente.historial_schemas import GuardarConversacionRequest
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado
from tests.ayudantes_analisis_gri import cliente_http, comprobar_error
from tests.ayudantes_migraciones import base_transaccional_nueva, conectar, consultar, tablas
from tests.services.test_asistente_postgres import EmbeddingsControlado, LlmControlado
from tests.services.test_historial_asistente_postgres import (  # noqa: F401  (`h` es un fixture)
    ANIO,
    RUTA,
    TEXTO,
    construir_app,
    con_evidencia,
    contar,
    cuerpo,
    esquema_hasta,
    guardar,
    h,
    listar,
    pregunta,
    recuperar,
    respuesta,
    sin_evidencia,
)

PREGUNTA = "¿Cuánto se redujo el consumo de agua?"
DOCUMENTO = uuid.UUID("7e2f4c1a-6b0d-4e57-8c1f-2d9a5b3e7f40")
RESPUESTA_LLM = json.dumps(
    {
        "afirmaciones": [
            {"texto": "La empresa redujo su consumo de agua en 12 %.", "referencias": [1], "cita_textual": "redujo el consumo de agua en 12 %"}
        ]
    }
)


# ============================================ Entorno ============================================

class EmbeddingsRegistrados(EmbeddingsControlado):
    def __init__(self):
        super().__init__()
        self.textos: list[str] = []
        self.al_entrar = None
        self.falla: Exception | None = None

    async def generar_embeddings_consulta(self, textos):
        self.llamadas += 1  # se cuenta al ENTRAR: así una ejecución que espera dentro del proveedor ya cuenta como llamada
        self.textos.extend(textos)
        if self.al_entrar:
            await self.al_entrar()
        if self.falla:
            raise self.falla
        return [[0.1] * 1536 for _ in textos]


def pregunta_actual_del_prompt(prompt: str) -> str:
    """La cadena JSON del bloque «PREGUNTA ACTUAL» del prompt de interpretación."""
    return json.loads(re.search(r"<<<PREGUNTA ACTUAL \w+>>>\n(.*)\n<<<FIN PREGUNTA ACTUAL", prompt, re.S).group(1))


def decision_consultar(pregunta: str | None = None) -> str:
    return json.dumps({"decision": "CONSULTAR", "pregunta_autonoma": pregunta})


class LlmRegistrado(LlmControlado):
    """Doble del LLM con DOS usos: la interpretación (sistema = `SISTEMA_INTERPRETACION`) y la respuesta del RAG. `llamadas` cuenta SOLO
    las del RAG (como antes de la entrega 3); `interpretaciones` cuenta las de interpretación. Por defecto la interpretación devuelve
    CONSULTAR con la misma pregunta (una pregunta autosuficiente conserva su intención)."""

    def __init__(self, contenido=""):
        super().__init__(contenido)
        self.al_entrar = None  # compuerta del RAG
        self.al_interpretar = None  # compuerta de la interpretación
        self.interpretaciones = 0
        self.interpretacion = None  # str | Exception | callable(prompt) -> str; None = eco de la pregunta actual
        self.interp_sistema = None
        self.interp_usuario = None

    async def _interpretar(self, sistema: str, usuario: str) -> str:
        self.interpretaciones += 1
        self.interp_sistema, self.interp_usuario = sistema, usuario
        if self.al_interpretar:
            await self.al_interpretar()
        valor = self.interpretacion
        if isinstance(valor, Exception):
            raise valor
        if callable(valor):
            return valor(usuario)
        return valor if valor is not None else decision_consultar(pregunta_actual_del_prompt(usuario))

    async def completar(self, sistema: str, usuario: str) -> str:
        if sistema == SISTEMA_INTERPRETACION:
            return await self._interpretar(sistema, usuario)
        self.llamadas += 1  # se cuenta al ENTRAR (ver los embeddings)
        self.sistema, self.usuario = sistema, usuario
        if self.al_entrar:
            await self.al_entrar()
        if isinstance(self.contenido, Exception):
            raise self.contenido
        return self.contenido


class Proveedores:
    """Dobles del RAG y de su fábrica diferida; cuenta cuántas veces se construyeron los proveedores."""

    def __init__(self):
        self.embeddings = EmbeddingsRegistrados()
        self.llm = LlmRegistrado(RESPUESTA_LLM)
        self.construidos = 0
        self.sesiones_abiertas = 0
        self.sesiones_cerradas = 0
        self.fallo_al_abrir_sesion: Exception | None = None
        self.fallo_al_construir: Exception | None = None
        self.fragmentos: list[FragmentoRecuperado] = []
        self.busquedas: list[dict] = []

    def construir(self):
        self.construidos += 1
        if self.fallo_al_construir:
            raise self.fallo_al_construir
        return self.embeddings, self.llm

    @asynccontextmanager
    async def sesion_vectorial(self):
        """Doble de la sesión vectorial: cuenta aperturas y cierres (el cierre debe ocurrir también ante un fallo)."""
        if self.fallo_al_abrir_sesion:
            raise self.fallo_al_abrir_sesion
        self.sesiones_abiertas += 1
        try:
            yield None
        finally:
            self.sesiones_cerradas += 1

    @property
    def recursos(self) -> RecursosRag:
        return RecursosRag(preparar=self.construir, sesion_vectorial=self.sesion_vectorial)

    @property
    def llamadas(self) -> tuple[int, int, int]:
        return self.construidos, self.embeddings.llamadas, self.llm.llamadas


def fragmento(empresa) -> FragmentoRecuperado:
    return FragmentoRecuperado(
        documento_id=DOCUMENTO, indice=0, texto_literal=TEXTO, contexto=TEXTO, inicio=0, fin=len(TEXTO), continuacion=False,
        ruta_encabezados=("Agua",), empresa_id=empresa.id, anio=ANIO, tipo="MEMORIA_ANUAL", sector=empresa.sector.value,
        embedding_proveedor="oci", embedding_modelo="cohere.embed-v4.0", similitud=0.9,
    )


@pytest.fixture
def p(h, monkeypatch):
    prov = Proveedores()
    prov.fragmentos = [fragmento(h.mineria)]

    async def buscar(*args, **kwargs):
        prov.busquedas.append(kwargs)
        return prov.fragmentos

    async def recuperables(db, *, ambiente, documento_ids):
        return set(documento_ids)

    async def elegibles(db, *, ambiente, empresa_id, anio):
        return [DOCUMENTO]  # el documento doble es elegible: el filtro previo a la búsqueda (ACT-02) no es lo que se prueba aquí

    async def nombres(db, ids):
        return {i: "memoria-2025.md" for i in ids}

    monkeypatch.setattr(servicio_consultas, "buscar_similares", buscar)
    monkeypatch.setattr(servicio_consultas, "documentos_recuperables", recuperables)
    monkeypatch.setattr(servicio_consultas, "documentos_elegibles_para_consulta", elegibles)
    monkeypatch.setattr(servicio_consultas, "_nombres_documentos", nombres)
    return prov


def app_con_continuacion(h, p, usuario, ambiente: str = "development"):
    app = construir_app(h, usuario, ambiente)
    app.include_router(continuacion_router.router)
    # Solo se sustituyen los RECURSOS DIFERIDOS (dobles de proveedores y de la sesión vectorial). La ruta ya no declara el esquema
    # vectorial ni la sesión vectorial como dependencias: `test_..._con_las_dependencias_reales` prueba la ruta SIN este reemplazo.
    app.dependency_overrides[continuacion_router.get_recursos_rag] = lambda: p.recursos
    return app


async def continuar(h, p, conversacion_id, texto: str = PREGUNTA, *, clave=None, usuario=None, ambiente: str = "development", extra=None):
    cuerpo_http = {"clave_idempotencia": str(clave or uuid.uuid4()), "pregunta": texto, **(extra or {})}
    async with cliente_http(app_con_continuacion(h, p, usuario if usuario is not None else h.ana, ambiente)) as c:
        return await c.post(f"{RUTA}/{conversacion_id}/consultas", json=cuerpo_http)


async def conversacion(h, *, ambiente: str = "development", usuario=None, mensajes=None) -> dict:
    guardada = await guardar(h, cuerpo(h, mensajes=mensajes), usuario=usuario, ambiente=ambiente)
    assert guardada.status_code == 201, guardada.text
    return guardada.json()


async def intercambios(h) -> list:
    return await consultar(
        h.url, "SELECT estado, intentos, codigo_error, token_ejecucion, vence_en FROM intercambios_asistente ORDER BY creado_en, id"
    )


async def mensajes_en_base(h) -> list:
    return await consultar(h.url, "SELECT orden, rol, contenido, datos_respuesta, procedencia FROM mensajes_asistente ORDER BY conversacion_id, orden")


async def vencer(h) -> None:
    conexion = await conectar(h.url)
    try:
        await conexion.execute("UPDATE intercambios_asistente SET vence_en = now() - interval '1 minute' WHERE estado = 'EN_CURSO'")
    finally:
        await conexion.close()


# ============================================ 1-3. Continuación, procedencia y contexto ============================================

async def test_continuar_agrega_pregunta_y_respuesta_completa_conservando_lo_anterior_y_sin_usar_el_historial(h, p):
    c = await conversacion(h)
    antes = (await recuperar(h, c["id"])).json()

    r = await continuar(h, p, c["id"])

    assert r.status_code == 201, r.text
    j = r.json()
    assert j["reutilizado"] is False
    assert j["conversacion_id"] == c["id"]
    assert (j["pregunta"]["orden"], j["respuesta"]["orden"]) == (5, 6)
    assert (j["pregunta"]["rol"], j["respuesta"]["rol"]) == ("usuario", "asistente")
    assert (j["pregunta"]["procedencia"], j["respuesta"]["procedencia"]) == ("ENVIADO_POR_USUARIO", "GENERADO_POR_SERVIDOR")
    assert j["pregunta"]["contenido"] == PREGUNTA
    assert j["pregunta"]["respuesta"] is None
    assert j["respuesta"]["contenido"] == "La empresa redujo su consumo de agua en 12 %. [1]"
    estructurada = j["respuesta"]["respuesta"]
    assert estructurada["sin_evidencia"] is False
    assert estructurada["afirmaciones"] == [
        {"texto": "La empresa redujo su consumo de agua en 12 %.", "referencias": [1], "cita_textual": "redujo el consumo de agua en 12 %"}
    ]
    assert estructurada["citas"] == [
        {
            "referencia": 1, "documento_id": str(DOCUMENTO), "documento_nombre": "memoria-2025.md", "empresa_id": str(h.mineria.id),
            "empresa_nombre": "Minera Andina", "anio": ANIO, "seccion": "Agua", "extracto": TEXTO,
        }
    ]
    assert j["total_mensajes"] == 6

    despues = (await recuperar(h, c["id"])).json()
    assert despues["mensajes"][:4] == antes["mensajes"]  # lo anterior, idéntico: mismos ids, orden, contenido y procedencia
    assert {m["procedencia"] for m in despues["mensajes"][:4]} == {"APORTADO_POR_CLIENTE"}  # no se elevó retroactivamente
    assert [m["orden"] for m in despues["mensajes"]] == [1, 2, 3, 4, 5, 6]
    assert despues["mensajes"][4:] == [j["pregunta"], j["respuesta"]]
    assert despues["actualizada_en"] == j["actualizada_en"]
    assert despues["actualizada_en"] > antes["actualizada_en"]
    assert despues["creada_en"] == antes["creada_en"]
    listado = (await listar(h)).json()["items"][0]
    assert (listado["total_mensajes"], listado["actualizada_en"]) == (6, j["actualizada_en"])

    # Al RAG solo llega la pregunta (la autónoma, aquí igual a la original): el historial va únicamente a la INTERPRETACIÓN (entrega 3).
    assert p.embeddings.textos == [PREGUNTA]
    assert p.llm.llamadas == 1
    historial_previo = [m["contenido"] for m in antes["mensajes"]]
    assert all(previo not in p.llm.usuario and previo not in p.llm.sistema for previo in historial_previo)
    assert PREGUNTA in p.llm.usuario
    busqueda = p.busquedas[0]
    assert (busqueda["empresa_ids"], busqueda["anio"], busqueda["ambiente"]) == ([h.mineria.id], ANIO, "development")


async def test_el_servidor_fija_propietario_ambiente_y_contexto(h, p):
    c = await conversacion(h, ambiente="qa")
    r = await continuar(h, p, c["id"], ambiente="qa")
    assert r.status_code == 201, r.text
    assert p.busquedas[0]["ambiente"] == "qa"
    assert p.busquedas[0]["empresa_ids"] == [h.mineria.id]
    fila = (await consultar(h.url, "SELECT ambiente, usuario_id, empresa_id, sector::text AS sector, anio FROM conversaciones_asistente"))[0]
    assert (fila["ambiente"], fila["usuario_id"], fila["empresa_id"], fila["sector"], fila["anio"]) == ("qa", h.ana.id, h.mineria.id, "MINERIA", ANIO)


@pytest.mark.parametrize(
    "extra",
    [
        {"usuario_id": str(uuid.uuid4())}, {"ambiente": "qa"}, {"empresa_id": str(uuid.uuid4())}, {"sector": "ENERGIA"}, {"anio": 2020},
        {"respuesta": {"respuesta": "x"}}, {"citas": []}, {"procedencia": "GENERADO_POR_SERVIDOR"}, {"orden": 7},
        {"mensajes": [{"rol": "usuario", "contenido": "hola"}]}, {"historial": ["a"]},
    ],
)
async def test_la_peticion_no_acepta_contexto_respuestas_ni_historial_del_cliente(h, p, extra):
    c = await conversacion(h)
    r = await continuar(h, p, c["id"], extra=extra)
    comprobar_error(r, 422, "REQUEST_VALIDATION_ERROR")
    assert p.llamadas == (0, 0, 0)
    assert await intercambios(h) == []
    assert await contar(h, "mensajes_asistente") == 4


@pytest.mark.parametrize("texto", ["", "   \n\t", "x" * 2001, "con nul \x00 dentro"])
async def test_la_pregunta_vacia_demasiado_larga_o_con_nul_se_rechaza_sin_repetir_su_contenido(h, p, texto):
    c = await conversacion(h)
    r = await continuar(h, p, c["id"], texto)
    comprobar_error(r, 422, "REQUEST_VALIDATION_ERROR")
    assert "x" * 50 not in r.text
    assert p.llamadas == (0, 0, 0)
    assert await intercambios(h) == []


# ============================================ 4. Autorización antes de los proveedores ============================================

async def test_una_conversacion_ajena_inexistente_u_de_otro_ambiente_responde_el_mismo_404_sin_tocar_proveedores(h, p):
    ajena = await conversacion(h, usuario=h.beto)
    de_qa = await conversacion(h, ambiente="qa")
    respuestas = [
        await continuar(h, p, ajena["id"]),  # de otro usuario
        await continuar(h, p, uuid.uuid4()),  # inexistente
        await continuar(h, p, de_qa["id"]),  # de otro ambiente
    ]
    errores = [comprobar_error(r, 404, "CONVERSATION_NOT_FOUND") for r in respuestas]
    assert len({e["message"] for e in errores}) == 1
    assert p.llamadas == (0, 0, 0)  # ni siquiera se construyeron los proveedores
    assert await intercambios(h) == []
    assert await contar(h, "mensajes_asistente") == 8


async def test_sin_sesion_401_y_superadmin_403_antes_de_cualquier_proveedor(h, p):
    c = await conversacion(h)
    async with cliente_http(app_con_continuacion(h, p, None)) as cliente:  # sin sustituir la sesión: autenticación real
        r401 = await cliente.post(f"{RUTA}/{c['id']}/consultas", json={"clave_idempotencia": str(uuid.uuid4()), "pregunta": PREGUNTA})
    assert r401.status_code == 401
    r403 = await continuar(h, p, c["id"], usuario=h.superadmin)
    assert r403.status_code == 403
    assert p.llamadas == (0, 0, 0)
    assert await intercambios(h) == []


# ============================================ 5. Empresa inactiva o sector cambiado ============================================

@pytest.mark.parametrize(
    "cambio,codigo",
    [
        ("UPDATE empresas SET activa = false WHERE id = $1", "COMPANY_INACTIVE"),
        ("UPDATE empresas SET sector = 'ENERGIA' WHERE id = $1", "COMPANY_SECTOR_MISMATCH"),
    ],
)
async def test_una_consulta_nueva_exige_empresa_activa_y_del_sector_guardado(h, p, cambio, codigo):
    c = await conversacion(h)
    conexion = await conectar(h.url)
    try:
        await conexion.execute(cambio, h.mineria.id)
    finally:
        await conexion.close()

    r = await continuar(h, p, c["id"])

    comprobar_error(r, 400, codigo)
    assert p.llamadas == (0, 0, 0)
    assert await intercambios(h) == []
    assert (await recuperar(h, c["id"])).status_code == 200  # recuperar sigue siendo historial
    assert await contar(h, "mensajes_asistente") == 4


# ============================================ 6. Respuesta sin evidencia ============================================

async def test_una_respuesta_sin_evidencia_se_persiste_con_la_aclaracion_visible_y_la_estructura_integra(h, p):
    c = await conversacion(h)
    p.fragmentos = []  # SIN_FRAGMENTOS
    r = await continuar(h, p, c["id"])
    assert r.status_code == 201, r.text
    j = r.json()
    estructurada = j["respuesta"]["respuesta"]
    assert (estructurada["sin_evidencia"], estructurada["motivo"], estructurada["respuesta"]) == (True, MOTIVO_SIN_FRAGMENTOS, "")
    assert estructurada["afirmaciones"] == []
    assert estructurada["citas"] == []
    assert j["respuesta"]["contenido"] == estructurada["mensaje"]
    assert j["respuesta"]["procedencia"] == "GENERADO_POR_SERVIDOR"
    fila = (await mensajes_en_base(h))[-1]
    persistido = json.loads(fila["datos_respuesta"]) if isinstance(fila["datos_respuesta"], str) else fila["datos_respuesta"]
    assert persistido == estructurada
    assert fila["contenido"] == estructurada["mensaje"]


async def test_una_respuesta_sin_evidencia_del_modelo_conserva_motivo_y_descartes(h, p):
    c = await conversacion(h)
    p.llm.contenido = json.dumps({"afirmaciones": []})
    j = (await continuar(h, p, c["id"])).json()
    estructurada = j["respuesta"]["respuesta"]
    assert (estructurada["sin_evidencia"], estructurada["motivo"]) == (True, MOTIVO_MODELO_SIN_EVIDENCIA)
    assert j["respuesta"]["contenido"] == estructurada["mensaje"]
    assert [m["orden"] for m in (await recuperar(h, c["id"])).json()["mensajes"]] == [1, 2, 3, 4, 5, 6]


# ============================================ 7-8. Idempotencia de cada consulta ============================================

async def test_un_reintento_completado_devuelve_el_mismo_intercambio_sin_llamadas_ni_revalidar_la_empresa(h, p):
    c = await conversacion(h)
    clave = uuid.uuid4()
    primera = await continuar(h, p, c["id"], clave=clave)
    assert primera.status_code == 201, primera.text
    llamadas = p.llamadas

    repeticion = await continuar(h, p, c["id"], clave=clave)

    assert repeticion.status_code == 200, repeticion.text
    j = repeticion.json()
    assert j["reutilizado"] is True
    assert (j["pregunta"], j["respuesta"]) == (primera.json()["pregunta"], primera.json()["respuesta"])  # exactamente lo almacenado
    assert p.llamadas == llamadas  # ninguna llamada nueva, ni siquiera se construyeron los proveedores
    assert await contar(h, "mensajes_asistente") == 6

    # Una repetición NO es una consulta nueva: sigue funcionando con la empresa desactivada y con los proveedores sin configurar.
    conexion = await conectar(h.url)
    try:
        await conexion.execute("UPDATE empresas SET activa = false WHERE id = $1", h.mineria.id)
    finally:
        await conexion.close()
    p.fallo_al_construir = ServiceUnavailableError("LLM_NOT_CONFIGURED", "sin configurar")
    otra_vez = await continuar(h, p, c["id"], clave=clave)
    assert otra_vez.status_code == 200
    assert otra_vez.json()["pregunta"] == primera.json()["pregunta"]
    assert p.llamadas == llamadas
    # ...pero una pregunta NUEVA (clave nueva) sobre esa empresa inactiva se rechaza.
    comprobar_error(await continuar(h, p, c["id"]), 400, "COMPANY_INACTIVE")


async def test_el_reintento_de_un_intercambio_anterior_devuelve_ese_intercambio_con_el_estado_actual_de_la_conversacion(h, p):
    c = await conversacion(h)
    clave_a = uuid.uuid4()
    a = (await continuar(h, p, c["id"], "Pregunta A", clave=clave_a)).json()
    b = (await continuar(h, p, c["id"], "Pregunta B")).json()
    assert (b["pregunta"]["orden"], b["total_mensajes"]) == (7, 8)

    repeticion = await continuar(h, p, c["id"], "Pregunta A", clave=clave_a)

    assert repeticion.status_code == 200
    j = repeticion.json()
    assert (j["pregunta"]["orden"], j["respuesta"]["orden"]) == (5, 6)  # el intercambio A, no el último
    assert (j["total_mensajes"], j["actualizada_en"]) == (8, b["actualizada_en"])  # el estado ACTUAL de la conversación
    assert j["pregunta"] == a["pregunta"]


async def test_la_misma_clave_con_otra_pregunta_es_un_conflicto_sin_llamadas(h, p):
    c = await conversacion(h)
    clave = uuid.uuid4()
    assert (await continuar(h, p, c["id"], "Pregunta A", clave=clave)).status_code == 201
    llamadas = p.llamadas
    r = await continuar(h, p, c["id"], "Pregunta B", clave=clave)
    comprobar_error(r, 409, "QUERY_IDEMPOTENCY_CONFLICT")
    assert p.llamadas == llamadas
    assert await contar(h, "mensajes_asistente") == 6
    # La clave se delimita por conversación: la misma clave en otra conversación es otra consulta.
    otra = await conversacion(h)
    assert (await continuar(h, p, otra["id"], "Pregunta B", clave=clave)).status_code == 201


# ============================================ 9-12. Concurrencia ============================================

def forzar_lectura_simultanea(monkeypatch, participantes: int = 2) -> None:
    """Obliga a que `participantes` solicitudes terminen TODAS sus lecturas previas (la clave aún no existe, no hay otra consulta en
    curso) ANTES de que alguna escriba: así deciden las restricciones únicas de la base, no la lectura previa. Si una solicitud viera
    ya la fila de la otra no llegaría a la barrera y la prueba fallaría por tiempo de espera en vez de pasar sin ejercitar la carrera."""
    barrera = asyncio.Barrier(participantes)
    original = continuacion_service._empresa_consultable
    esperaron = 0

    async def con_barrera(*args, **kwargs):
        nonlocal esperaron
        resultado = await original(*args, **kwargs)
        if esperaron < participantes:  # solo los participantes de la carrera esperan; las solicitudes posteriores pasan de largo
            esperaron += 1
            await asyncio.wait_for(barrera.wait(), 15)
        return resultado

    monkeypatch.setattr(continuacion_service, "_empresa_consultable", con_barrera)


def espiar_restricciones(monkeypatch) -> list:
    """Registra qué restricción de la base decidió cada carrera (el nombre que llega al servicio al capturar la violación)."""
    vistas: list = []
    original = continuacion_service._nombre_de_restriccion

    def espia(exc):
        nombre = original(exc)
        vistas.append(nombre)
        return nombre

    monkeypatch.setattr(continuacion_service, "_nombre_de_restriccion", espia)
    return vistas


def mantener_en_el_llm(p):
    """El LLM queda esperando: `entro` avisa que una ejecución llegó al proveedor y `liberar` la deja seguir."""
    entro, liberar = asyncio.Event(), asyncio.Event()

    async def al_entrar():
        entro.set()
        await asyncio.wait_for(liberar.wait(), 15)

    p.llm.al_entrar = al_entrar
    return entro, liberar


async def test_solicitudes_simultaneas_con_la_misma_clave_resuelve_la_restriccion_unica_y_solo_una_ejecuta_el_rag(h, p, monkeypatch):
    c = await conversacion(h)
    forzar_lectura_simultanea(monkeypatch)
    vistas = espiar_restricciones(monkeypatch)
    entro, liberar = mantener_en_el_llm(p)
    clave = uuid.uuid4()

    tareas = [asyncio.create_task(continuar(h, p, c["id"], clave=clave)) for _ in range(2)]
    hechas, pendientes = await asyncio.wait(tareas, timeout=20, return_when=asyncio.FIRST_COMPLETED)
    perdedora = hechas.pop().result()  # la ganadora sigue dentro del LLM: la primera en terminar es la que perdió la carrera
    comprobar_error(perdedora, 409, "CONVERSATION_QUERY_IN_PROGRESS")
    assert perdedora.headers["retry-after"] == str(continuacion_service.REINTENTO_SUGERIDO_SEGUNDOS)
    await asyncio.wait_for(entro.wait(), 15)  # la ganadora llega al proveedor
    assert p.llm.llamadas == 1  # la perdedora NO inició otra ejecución
    liberar.set()
    ganadora = (await asyncio.gather(*pendientes))[0]

    assert ganadora.status_code == 201, ganadora.text
    assert vistas == [continuacion_service.RESTRICCION_CLAVE]  # decidió la restricción única de la clave
    assert (p.embeddings.llamadas, p.llm.llamadas) == (1, 1)
    assert [f["estado"] for f in await intercambios(h)] == ["COMPLETADO"]
    assert await contar(h, "mensajes_asistente") == 6
    # Cuando termina, el reintento de la perdedora recupera lo almacenado.
    final = await continuar(h, p, c["id"], clave=clave)
    assert (final.status_code, final.json()["pregunta"]) == (200, ganadora.json()["pregunta"])
    assert p.llm.llamadas == 1


async def test_consultas_distintas_de_una_conversacion_decide_el_indice_parcial_y_solo_una_esta_en_curso(h, p, monkeypatch):
    c = await conversacion(h)
    forzar_lectura_simultanea(monkeypatch)
    vistas = espiar_restricciones(monkeypatch)
    entro, liberar = mantener_en_el_llm(p)

    tareas = [asyncio.create_task(continuar(h, p, c["id"], f"Pregunta {n}")) for n in (1, 2)]
    hechas, pendientes = await asyncio.wait(tareas, timeout=20, return_when=asyncio.FIRST_COMPLETED)
    perdedora = hechas.pop().result()
    comprobar_error(perdedora, 409, "CONVERSATION_BUSY")
    assert perdedora.headers["retry-after"] == str(continuacion_service.REINTENTO_SUGERIDO_SEGUNDOS)
    await asyncio.wait_for(entro.wait(), 15)  # la ganadora llega al proveedor
    assert p.llm.llamadas == 1  # el conflicto es explícito y no invoca a la IA
    liberar.set()
    ganadora = (await asyncio.gather(*pendientes))[0]

    assert ganadora.status_code == 201, ganadora.text
    assert vistas == [continuacion_service.RESTRICCION_EN_CURSO]
    assert [f["estado"] for f in await intercambios(h)] == ["COMPLETADO"]
    assert await contar(h, "mensajes_asistente") == 6
    # No se encola: quien perdió reintenta con una clave nueva cuando la conversación queda libre.
    assert (await continuar(h, p, c["id"], "Pregunta 3")).status_code == 201


async def test_una_segunda_consulta_distinta_mientras_hay_una_en_curso_recibe_un_conflicto_sin_invocar_a_la_ia(h, p):
    c = await conversacion(h)
    entro, liberar = mantener_en_el_llm(p)
    primera = asyncio.create_task(continuar(h, p, c["id"], "Pregunta A"))
    await asyncio.wait_for(entro.wait(), 15)

    segunda = await continuar(h, p, c["id"], "Pregunta B")
    misma_clave = await continuar(h, p, c["id"], "Pregunta A", clave=uuid.UUID((await intercambios_clave(h))))

    comprobar_error(segunda, 409, "CONVERSATION_BUSY")
    comprobar_error(misma_clave, 409, "CONVERSATION_QUERY_IN_PROGRESS")
    assert (p.embeddings.llamadas, p.llm.llamadas, p.construidos) == (1, 1, 1)
    liberar.set()
    assert (await primera).status_code == 201
    assert await contar(h, "mensajes_asistente") == 6


async def intercambios_clave(h) -> str:
    return str((await consultar(h.url, "SELECT clave_idempotencia FROM intercambios_asistente"))[0]["clave_idempotencia"])


async def test_conversaciones_distintas_se_procesan_a_la_vez_sin_serializacion_global(h, p):
    una, otra = await conversacion(h), await conversacion(h)
    barrera = asyncio.Barrier(2)  # las dos ejecuciones deben estar DENTRO del LLM al mismo tiempo; si se serializaran, expira

    async def al_entrar():
        await asyncio.wait_for(barrera.wait(), 15)

    p.llm.al_entrar = al_entrar
    respuestas = await asyncio.gather(continuar(h, p, una["id"], "Pregunta A"), continuar(h, p, otra["id"], "Pregunta B"))
    assert [r.status_code for r in respuestas] == [201, 201]
    assert p.llm.llamadas == 2
    assert sorted(f["estado"] for f in await intercambios(h)) == ["COMPLETADO", "COMPLETADO"]


async def test_los_proveedores_se_ejecutan_sin_transacciones_ni_bloqueos_del_historial(h, p):
    c = await conversacion(h)
    sondeos: list[dict] = []

    async def sondear():
        inactivas = (
            await consultar(
                h.url,
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND state LIKE 'idle in transaction%'",
            )
        )[0]["n"]
        conexion = await conectar(h.url)
        bloqueado = False
        try:
            async with conexion.transaction():
                for tabla in ("conversaciones_asistente", "intercambios_asistente", "mensajes_asistente"):
                    try:
                        await conexion.fetch(f"SELECT id FROM {tabla} FOR UPDATE NOWAIT")
                    except asyncpg.LockNotAvailableError:
                        bloqueado = True
        finally:
            await conexion.close()
        estados = [f["estado"] for f in await intercambios(h)]
        sondeos.append({"inactivas": inactivas, "bloqueado": bloqueado, "estados": estados})

    p.embeddings.al_entrar = sondear
    p.llm.al_entrar = sondear

    assert (await continuar(h, p, c["id"])).status_code == 201

    assert len(sondeos) == 2  # una durante el embedding y otra durante el LLM
    assert all(s["inactivas"] == 0 for s in sondeos)  # ninguna transacción abierta
    assert not any(s["bloqueado"] for s in sondeos)  # ninguna fila bloqueada
    assert all(s["estados"] == ["EN_CURSO"] for s in sondeos)  # y la reserva ya era DURADERA (confirmada)


# ============================================ Fallos de proveedores y de persistencia ============================================

async def test_un_proveedor_no_configurado_no_deja_reserva_ni_mensajes(h, p):
    c = await conversacion(h)
    p.fallo_al_construir = ServiceUnavailableError("LLM_NOT_CONFIGURED", "El asistente no está disponible: falta configurar el proveedor.")
    comprobar_error(await continuar(h, p, c["id"]), 503, "LLM_NOT_CONFIGURED")
    assert await intercambios(h) == []
    assert await contar(h, "mensajes_asistente") == 4
    p.fallo_al_construir = None
    assert (await continuar(h, p, c["id"])).status_code == 201  # sin nada que limpiar


@pytest.mark.parametrize(
    "donde,falla,estado,codigo",
    [
        ("embeddings", RuntimeError("caída"), 502, "EMBEDDING_PROVIDER_ERROR"),
        ("llm", ExternalServiceError("LLM_PROVIDER_ERROR", "error del proveedor"), 502, "LLM_PROVIDER_ERROR"),
        ("llm", ExternalServiceTimeoutError("LLM_PROVIDER_TIMEOUT", "tiempo agotado"), 504, "LLM_PROVIDER_TIMEOUT"),
    ],
)
async def test_un_error_o_timeout_del_proveedor_no_deja_mensajes_y_la_misma_clave_se_reintenta(h, p, donde, falla, estado, codigo):
    c = await conversacion(h)
    antes = (await recuperar(h, c["id"])).json()
    clave = uuid.uuid4()
    if donde == "embeddings":
        p.embeddings.falla = falla
    else:
        p.llm.contenido = falla  # LlmControlado lanza la excepción

    r = await continuar(h, p, c["id"], clave=clave)

    comprobar_error(r, estado, codigo)
    assert (await recuperar(h, c["id"])).json() == antes  # nada cambió, tampoco actualizada_en
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["codigo_error"], fila["intentos"]) == ("FALLIDO", codigo, 1)

    p.embeddings.falla = None
    p.llm.contenido = RESPUESTA_LLM
    reintento = await continuar(h, p, c["id"], clave=clave)  # la conversación no quedó bloqueada y la clave se reintenta
    assert reintento.status_code == 201, reintento.text
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["codigo_error"], fila["intentos"]) == ("COMPLETADO", None, 2)
    assert await contar(h, "mensajes_asistente") == 6


async def test_un_fallo_al_persistir_despues_de_obtener_la_respuesta_no_deja_un_par_parcial(h, p, monkeypatch):
    c = await conversacion(h)
    antes = (await recuperar(h, c["id"])).json()
    original = continuacion_service.MensajeAsistente

    class ConOrdenRepetido:
        """Construye los mensajes con el `orden` del primero repetido (el segundo viola `uq_mensajes_asistente_orden` DESPUÉS de
        añadirse el primero) y delega todo lo demás (columnas para consultar) en el modelo real."""

        def __init__(self):
            self.ordenes: list[int] = []

        def __call__(self, **campos):
            if self.ordenes:
                campos["orden"] = self.ordenes[0]
            self.ordenes.append(campos["orden"])
            return original(**campos)

        def __getattr__(self, nombre):
            return getattr(original, nombre)

    monkeypatch.setattr(continuacion_service, "MensajeAsistente", ConOrdenRepetido())
    clave = uuid.uuid4()

    r = await continuar(h, p, c["id"], clave=clave)

    comprobar_error(r, 503, "CONVERSATION_PERSISTENCE_FAILED")
    assert (await recuperar(h, c["id"])).json() == antes  # ningún mensaje del par y la misma fecha de actualización
    assert (await intercambios(h))[0]["estado"] == "FALLIDO"
    assert p.llm.llamadas == 1

    monkeypatch.setattr(continuacion_service, "MensajeAsistente", original)  # sin el fallo inyectado (los dobles del RAG siguen)
    reintento = await continuar(h, p, c["id"], clave=clave)
    assert reintento.status_code == 201, reintento.text
    assert await contar(h, "mensajes_asistente") == 6
    # LIMITACIÓN documentada: el reintento volvió a consultar al RAG (la respuesta anterior no se había publicado). Se garantiza como
    # máximo UN intercambio guardado por clave, no exactamente una llamada al proveedor.
    assert p.llm.llamadas == 2


async def test_una_respuesta_valida_del_rag_que_postgresql_no_puede_guardar_no_se_altera_ni_deja_mensajes(h, p):
    """El carácter NUL no cabe en TEXT ni en JSONB. No se elimina en silencio (se perdería información) ni se inventa otra respuesta."""
    c = await conversacion(h)
    antes = (await recuperar(h, c["id"])).json()
    p.llm.contenido = json.dumps(
        {"afirmaciones": [{"texto": "La empresa redujo su consumo de agua.\x00", "referencias": [1], "cita_textual": "redujo el consumo de agua en 12 %"}]}
    )

    r = await continuar(h, p, c["id"])

    comprobar_error(r, 500, "ASSISTANT_RESPONSE_NOT_PERSISTABLE")
    assert (await recuperar(h, c["id"])).json() == antes
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["codigo_error"]) == ("FALLIDO", "ASSISTANT_RESPONSE_NOT_PERSISTABLE")


# ============================================ 14. Reserva vencida ============================================

def parametros_de_reserva(h, conversacion_id, clave, texto: str = PREGUNTA) -> dict:
    return dict(
        usuario_id=h.ana.id, ambiente="development", conversacion_id=uuid.UUID(conversacion_id), pregunta=texto, clave=clave,
        preparar=lambda: (EmbeddingsControlado(), LlmControlado()),
    )


async def test_un_proceso_detenido_deja_una_reserva_que_bloquea_hasta_vencer_y_luego_se_recupera(h, p):
    c = await conversacion(h)
    clave = uuid.uuid4()
    async with h.fabrica() as db:
        reserva = await continuacion_service._reservar(db, **parametros_de_reserva(h, c["id"], clave))  # fase 1 y el proceso «cae»
    assert (await intercambios(h))[0]["estado"] == "EN_CURSO"

    comprobar_error(await continuar(h, p, c["id"], clave=clave), 409, "CONVERSATION_QUERY_IN_PROGRESS")
    comprobar_error(await continuar(h, p, c["id"], "Otra"), 409, "CONVERSATION_BUSY")
    assert p.llamadas == (0, 0, 0)

    await vencer(h)
    recuperada = await continuar(h, p, c["id"], clave=clave)

    assert recuperada.status_code == 201, recuperada.text
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["intentos"]) == ("COMPLETADO", 2)
    assert fila["token_ejecucion"] != reserva.token  # la propiedad cambió de manos
    assert await contar(h, "mensajes_asistente") == 6


async def test_una_ejecucion_tardia_tras_recuperar_la_misma_clave_no_puede_publicar(h, p):
    c = await conversacion(h)
    clave = uuid.uuid4()
    async with h.fabrica() as db:
        vieja = await continuacion_service._reservar(db, **parametros_de_reserva(h, c["id"], clave))
    await vencer(h)
    assert (await continuar(h, p, c["id"], clave=clave)).status_code == 201

    async with h.fabrica() as db:
        with pytest.raises(ConflictError) as tarde:
            await continuacion_service._publicar(db, vieja, PREGUNTA, con_evidencia(h.mineria.id, citas=1))
    assert tarde.value.code == "CONVERSATION_QUERY_SUPERSEDED"
    assert await contar(h, "mensajes_asistente") == 6  # no se añadió nada
    assert [f["estado"] for f in await intercambios(h)] == ["COMPLETADO"]


async def test_una_ejecucion_tardia_tras_ser_sustituida_por_otra_consulta_no_puede_publicar(h, p):
    c = await conversacion(h)
    async with h.fabrica() as db:
        vieja = await continuacion_service._reservar(db, **parametros_de_reserva(h, c["id"], uuid.uuid4(), "Pregunta A"))
    await vencer(h)
    nueva = await continuar(h, p, c["id"], "Pregunta B")  # otra clave: la reserva vencida pasa a VENCIDO
    assert nueva.status_code == 201, nueva.text
    assert sorted(f["estado"] for f in await intercambios(h)) == ["COMPLETADO", "VENCIDO"]

    async with h.fabrica() as db:
        with pytest.raises(ConflictError) as tarde:
            await continuacion_service._publicar(db, vieja, "Pregunta A", con_evidencia(h.mineria.id, citas=1))
    assert tarde.value.code == "CONVERSATION_QUERY_SUPERSEDED"
    assert await contar(h, "mensajes_asistente") == 6
    # La clave sustituida sigue siendo reintentable cuando la conversación está libre.
    assert (await continuar(h, p, c["id"], "Pregunta A", clave=uuid.UUID(await _clave_vencida(h)))).status_code == 201
    assert await contar(h, "mensajes_asistente") == 8


async def _clave_vencida(h) -> str:
    return str((await consultar(h.url, "SELECT clave_idempotencia FROM intercambios_asistente WHERE estado = 'VENCIDO'"))[0]["clave_idempotencia"])


async def test_una_reserva_vencida_que_nadie_recupero_si_puede_publicar_su_resultado(h, p):
    """Sin heartbeat: el token decide, no el reloj. Si nadie sustituyó la reserva, el resultado válido se publica."""
    c = await conversacion(h)
    async with h.fabrica() as db:
        vieja = await continuacion_service._reservar(db, **parametros_de_reserva(h, c["id"], uuid.uuid4()))
    await vencer(h)
    async with h.fabrica() as db:
        publicado = await continuacion_service._publicar(db, vieja, PREGUNTA, con_evidencia(h.mineria.id, citas=1))
    assert publicado.reutilizado is False
    assert await contar(h, "mensajes_asistente") == 6


# ============================================ Ajuste 2.1: recursos vectoriales diferidos ============================================

async def completar_un_intercambio(h, p) -> tuple[dict, uuid.UUID, dict]:
    """Una conversación con un intercambio ya COMPLETADO; devuelve (conversación, clave, respuesta 201)."""
    c = await conversacion(h)
    clave = uuid.uuid4()
    primera = await continuar(h, p, c["id"], clave=clave)
    assert primera.status_code == 201, primera.text
    return c, clave, primera.json()


class EspiaVectorial:
    """Registra cualquier intento de preparar, validar, abrir o usar lo vectorial y de IA a través de las fábricas REALES de la ruta."""

    def __init__(self, monkeypatch, h):
        self.llamadas: list[str] = []
        self.sesiones_abiertas = 0
        self.sesiones_cerradas = 0
        for nombre, objetivo in (
            ("esquema_vectorial_listo", continuacion_router),
            ("url_vectorial", continuacion_router),
            ("fabrica_sesiones_vectoriales", continuacion_router),
        ):
            monkeypatch.setattr(objetivo, nombre, self._espiar(nombre, getattr(objetivo, nombre)))
        monkeypatch.setattr(
            continuacion_router.router_consultas, "get_proveedor_embeddings", self._espiar("embeddings", self._falla("EMBEDDINGS_NOT_CONFIGURED"))
        )
        monkeypatch.setattr(continuacion_router.router_consultas, "get_proveedor_llm", self._espiar("llm", self._falla("LLM_NOT_CONFIGURED")))

    @staticmethod
    def _falla(codigo: str):
        def construir():
            raise ServiceUnavailableError(codigo, "sin configurar")

        return construir

    def _espiar(self, nombre, original):
        def espia(*args, **kwargs):
            self.llamadas.append(nombre)
            return original(*args, **kwargs)

        return espia


def app_con_recursos_reales(h, usuario, ambiente: str = "development"):
    """La ruta con su dependencia REAL de recursos (`get_recursos_rag`) y sin sustituir nada vectorial: si la ruta declarara el esquema
    vectorial o la sesión vectorial, esta app responderia 503 (no hay `app.state.ingesta`) o intentaría abrir la base vectorial."""
    app = construir_app(h, usuario, ambiente)
    app.include_router(continuacion_router.router)
    assert continuacion_router.get_recursos_rag not in app.dependency_overrides
    return app


async def post_real(h, conversacion_id, clave, texto: str = PREGUNTA, usuario=None):
    async with cliente_http(app_con_recursos_reales(h, usuario if usuario is not None else h.ana)) as c:
        return await c.post(f"{RUTA}/{conversacion_id}/consultas", json={"clave_idempotencia": str(clave), "pregunta": texto})


@pytest.mark.parametrize(
    "situacion",
    ["esquema_vectorial_no_preparado", "configuracion_vectorial_ausente", "configuracion_vectorial_invalida", "proveedores_sin_configurar"],
)
async def test_un_reintento_completado_no_prepara_ni_usa_recursos_vectoriales_ni_de_ia(h, p, monkeypatch, situacion):
    c, clave, primera = await completar_un_intercambio(h, p)
    llamadas_previas = p.llamadas
    espia = EspiaVectorial(monkeypatch, h)
    if situacion == "configuracion_vectorial_ausente":
        monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", None, raising=False)
    if situacion == "configuracion_vectorial_invalida":
        monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", "mysql://x", raising=False)
    # En esta app NO existe `app.state.ingesta`: el esquema vectorial figura como NO preparado en todos los casos.

    r = await post_real(h, c["id"], clave)

    assert r.status_code == 200, r.text
    j = r.json()
    assert j["reutilizado"] is True
    assert (j["pregunta"], j["respuesta"]) == (primera["pregunta"], primera["respuesta"])  # el mismo intercambio, el almacenado
    assert espia.llamadas == []  # ni esquema vectorial, ni URL, ni sesión vectorial, ni proveedores
    assert p.llamadas == llamadas_previas  # ninguna construcción ni llamada de proveedores
    assert await contar(h, "mensajes_asistente") == 6


async def test_con_el_esquema_vectorial_no_preparado_una_consulta_nueva_falla_con_error_controlado_y_sin_mensajes_parciales(h, p, monkeypatch):
    c = await conversacion(h)
    espia = EspiaVectorial(monkeypatch, h)

    r = await post_real(h, c["id"], uuid.uuid4())

    comprobar_error(r, 503, "INGESTION_NOT_CONFIGURED")  # el mismo motivo que POST /asistente/consultas
    assert espia.llamadas == ["esquema_vectorial_listo"]  # se preparó SOLO ahora y se detuvo en el primer recurso que falta
    assert await intercambios(h) == []  # falló antes de reservar: nada que recuperar ni limpiar
    assert await contar(h, "mensajes_asistente") == 4


@pytest.mark.parametrize(
    "valor,codigo",
    [(None, "VECTOR_DATABASE_NOT_CONFIGURED"), ("mysql://x", "VECTOR_DATABASE_URL_INVALID")],
)
async def test_con_la_configuracion_vectorial_ausente_o_invalida_una_consulta_nueva_falla_controlada_antes_de_reservar(h, p, monkeypatch, valor, codigo):
    c = await conversacion(h)
    EspiaVectorial(monkeypatch, h)
    monkeypatch.setattr(continuacion_router, "esquema_vectorial_listo", lambda request: None)  # el esquema sí está listo
    monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", valor, raising=False)

    r = await post_real(h, c["id"], uuid.uuid4())

    comprobar_error(r, 502, codigo)  # el mismo error controlado que POST /asistente/consultas
    assert await intercambios(h) == []
    assert await contar(h, "mensajes_asistente") == 4


async def test_una_conversacion_ajena_inexistente_o_de_otro_ambiente_responde_404_antes_de_preparar_recursos_externos(h, p, monkeypatch):
    ajena = await conversacion(h, usuario=h.beto)
    de_qa = await conversacion(h, ambiente="qa")
    espia = EspiaVectorial(monkeypatch, h)

    respuestas = [await post_real(h, i, uuid.uuid4()) for i in (ajena["id"], uuid.uuid4(), de_qa["id"])]

    errores = [comprobar_error(r, 404, "CONVERSATION_NOT_FOUND") for r in respuestas]
    assert len({e["message"] for e in errores}) == 1
    assert espia.llamadas == []  # con el esquema y la configuración vectoriales AUSENTES sigue siendo el mismo 404
    assert await intercambios(h) == []


async def test_los_recursos_vectoriales_se_cierran_tanto_en_exito_como_en_fallo(h, p):
    c = await conversacion(h)
    assert (await continuar(h, p, c["id"])).status_code == 201
    assert (p.sesiones_abiertas, p.sesiones_cerradas) == (1, 1)

    p.llm.contenido = ExternalServiceError("LLM_PROVIDER_ERROR", "error del proveedor")
    comprobar_error(await continuar(h, p, c["id"], "Otra pregunta"), 502, "LLM_PROVIDER_ERROR")
    assert (p.sesiones_abiertas, p.sesiones_cerradas) == (2, 2)  # también tras el fallo

    # Una repetición completada ni siquiera abre una sesión vectorial.
    clave_ok = uuid.UUID(str((await consultar(h.url, "SELECT clave_idempotencia FROM intercambios_asistente WHERE estado = 'COMPLETADO'"))[0]["clave_idempotencia"]))
    assert (await continuar(h, p, c["id"], clave=clave_ok)).status_code == 200
    assert (p.sesiones_abiertas, p.sesiones_cerradas) == (2, 2)


async def test_si_falla_la_apertura_de_la_sesion_vectorial_tras_reservar_la_reserva_queda_recuperable(h, p):
    c = await conversacion(h)
    clave = uuid.uuid4()
    p.fallo_al_abrir_sesion = ExternalServiceError("VECTOR_DATABASE_NOT_CONFIGURED", "sin base vectorial")

    comprobar_error(await continuar(h, p, c["id"], clave=clave), 502, "VECTOR_DATABASE_NOT_CONFIGURED")

    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["codigo_error"]) == ("FALLIDO", "VECTOR_DATABASE_NOT_CONFIGURED")  # no queda bloqueada
    assert await contar(h, "mensajes_asistente") == 4
    p.fallo_al_abrir_sesion = None
    assert (await continuar(h, p, c["id"], clave=clave)).status_code == 201  # la misma clave se recupera


# ============================================ Ajuste 2.1: presupuesto global de la generación ============================================

def test_el_presupuesto_global_de_la_generacion_es_menor_que_la_reserva_con_margen():
    assert continuacion_service.PRESUPUESTO_GENERACION_SEGUNDOS + 30 <= continuacion_service.PLAZO_RESERVA_SEGUNDOS
    assert continuacion_service.PRESUPUESTO_GENERACION_SEGUNDOS > 0


async def test_al_agotarse_el_presupuesto_global_hay_un_error_controlado_sin_mensajes_y_la_misma_clave_se_recupera(h, p, monkeypatch):
    c = await conversacion(h)
    antes = (await recuperar(h, c["id"])).json()
    monkeypatch.setattr(continuacion_service, "PRESUPUESTO_GENERACION_SEGUNDOS", 0.3)
    entro, _liberar = mantener_en_el_llm(p)  # el LLM no responde nunca: solo el presupuesto global puede cortar la espera
    clave = uuid.uuid4()

    r = await continuar(h, p, c["id"], clave=clave)

    error = comprobar_error(r, 504, "ASSISTANT_GENERATION_TIMEOUT")
    assert error["details"] == {"presupuesto_segundos": 0.3}
    assert entro.is_set()
    assert (await recuperar(h, c["id"])).json() == antes  # ningún mensaje parcial, `actualizada_en` intacta
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["codigo_error"]) == ("FALLIDO", "ASSISTANT_GENERATION_TIMEOUT")
    assert (p.sesiones_abiertas, p.sesiones_cerradas) == (1, 1)  # la sesión vectorial se cerró aunque se canceló la espera

    p.llm.al_entrar = None
    monkeypatch.setattr(continuacion_service, "PRESUPUESTO_GENERACION_SEGUNDOS", 240)
    reintento = await continuar(h, p, c["id"], clave=clave)  # reintento seguro con la misma clave
    assert reintento.status_code == 201, reintento.text
    assert (await intercambios(h))[0]["intentos"] == 2
    assert await contar(h, "mensajes_asistente") == 6


async def test_el_presupuesto_global_cubre_tambien_los_embeddings_y_no_confunde_un_timeout_ajeno(h, p, monkeypatch):
    c = await conversacion(h)
    monkeypatch.setattr(continuacion_service, "PRESUPUESTO_GENERACION_SEGUNDOS", 0.3)
    bloqueo = asyncio.Event()

    async def sin_responder():
        await asyncio.wait_for(bloqueo.wait(), 15)

    p.embeddings.al_entrar = sin_responder
    comprobar_error(await continuar(h, p, c["id"]), 504, "ASSISTANT_GENERATION_TIMEOUT")
    assert p.llm.llamadas == 0  # se cortó en la búsqueda: el LLM no llegó a llamarse

    # Un TimeoutError que NO viene del presupuesto no se reinterpreta como agotamiento del presupuesto.
    monkeypatch.setattr(continuacion_service, "PRESUPUESTO_GENERACION_SEGUNDOS", 240)
    p.embeddings.al_entrar = None
    p.llm.contenido = TimeoutError("socket")
    r = await continuar(h, p, c["id"], "Otra")
    assert r.json()["error"]["code"] != "ASSISTANT_GENERATION_TIMEOUT"


async def test_un_ejecutor_desplazado_continua_pero_no_puede_publicar(h, p):
    """A queda dentro del LLM, su reserva vence y la misma clave la recupera B, que completa. Al liberar a A su respuesta llega tarde:
    no publica nada (el token ya no es suyo) y no estropea el intercambio de B."""
    c = await conversacion(h)
    clave = uuid.uuid4()
    entro, liberar = asyncio.Event(), asyncio.Event()
    llamadas_al_llm = 0

    async def solo_la_primera_espera():
        nonlocal llamadas_al_llm
        llamadas_al_llm += 1
        if llamadas_al_llm == 1:
            entro.set()
            await asyncio.wait_for(liberar.wait(), 15)

    p.llm.al_entrar = solo_la_primera_espera
    ejecutor_a = asyncio.create_task(continuar(h, p, c["id"], clave=clave))
    await asyncio.wait_for(entro.wait(), 15)
    await vencer(h)

    b = await continuar(h, p, c["id"], clave=clave)  # recupera la reserva con otro token y completa
    assert b.status_code == 201, b.text
    liberar.set()
    a = await ejecutor_a

    comprobar_error(a, 409, "CONVERSATION_QUERY_SUPERSEDED")
    assert await contar(h, "mensajes_asistente") == 6  # un solo par: el de B
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["intentos"]) == ("COMPLETADO", 2)
    assert (await continuar(h, p, c["id"], clave=clave)).json()["pregunta"] == b.json()["pregunta"]


async def test_la_respuesta_no_persistible_es_un_error_especifico_distinto_de_los_fallos_temporales(h, p):
    c = await conversacion(h)
    p.llm.contenido = json.dumps(
        {"afirmaciones": [{"texto": "La empresa redujo su consumo de agua.\x00", "referencias": [1], "cita_textual": "redujo el consumo de agua en 12 %"}]}
    )
    clave = uuid.uuid4()
    r = await continuar(h, p, c["id"], clave=clave)
    comprobar_error(r, 500, "ASSISTANT_RESPONSE_NOT_PERSISTABLE")
    assert "\x00" not in r.text
    # Distinto de un fallo temporal de proveedor (502) o de persistencia (503): no los comparte.
    assert r.status_code not in (502, 503, 504)
    assert (await intercambios(h))[0]["codigo_error"] == "ASSISTANT_RESPONSE_NOT_PERSISTABLE"
    descripcion = continuacion_router.router.routes[0].responses[500]["description"]
    assert "NO reintentar" in descripcion  # documentado en OpenAPI para el frontend


# ============================================ 15. Límites ============================================

def pares(n: int) -> list[dict]:
    return [m for i in range(n) for m in (pregunta(f"Pregunta {i}"), respuesta(sin_evidencia()))]


async def test_una_conversacion_de_40_mensajes_se_lee_pero_no_admite_otro_par(h, p):
    c = await conversacion(h, mensajes=pares(20))
    assert c["total_mensajes"] == 40
    r = await continuar(h, p, c["id"])
    comprobar_error(r, 409, "CONVERSATION_MESSAGE_LIMIT_REACHED")
    assert p.llamadas == (0, 0, 0)  # se decide antes de llamar a los proveedores
    assert await intercambios(h) == []
    leida = (await recuperar(h, c["id"])).json()
    assert [m["orden"] for m in leida["mensajes"]] == list(range(1, 41))


async def test_el_ultimo_par_que_cabe_se_guarda_y_el_siguiente_se_rechaza(h, p):
    c = await conversacion(h, mensajes=pares(19))
    ultimo = await continuar(h, p, c["id"], "Último")
    assert ultimo.status_code == 201, ultimo.text
    assert ultimo.json()["total_mensajes"] == 40
    comprobar_error(await continuar(h, p, c["id"], "Uno más"), 409, "CONVERSATION_MESSAGE_LIMIT_REACHED")
    assert p.llm.llamadas == 1
    assert await contar(h, "mensajes_asistente") == 40


async def _bytes_actuales(h) -> int:
    async with h.fabrica() as db:
        conv = (await db.execute(select(ConversacionAsistente))).scalar_one()
        mensajes, _ = await continuacion_service._almacenados(db, conv.id)
        return continuacion_service._bytes_de_conversacion(conv, mensajes)


async def test_el_tamano_se_mide_como_en_el_guardado_inicial(h):
    datos = cuerpo(h)
    guardada = await guardar(h, datos)
    assert guardada.status_code == 201
    solicitud = GuardarConversacionRequest.model_validate(datos)
    canonico = contenido_canonico(
        solicitud.empresa_id, solicitud.sector, solicitud.anio, historial_service._mensajes_normalizados(solicitud)
    )
    assert await _bytes_actuales(h) == len(canonico.encode("utf-8"))  # lo recuperado de la base mide igual que lo recibido


async def test_si_la_pregunta_ya_no_cabe_se_rechaza_antes_de_llamar_a_los_proveedores(h, p, monkeypatch):
    c = await conversacion(h)
    monkeypatch.setattr(continuacion_service, "MAX_BYTES_CONVERSACION", await _bytes_actuales(h) + 10)
    comprobar_error(await continuar(h, p, c["id"]), 409, "CONVERSATION_SIZE_LIMIT_REACHED")
    assert p.llamadas == (0, 0, 0)
    assert await intercambios(h) == []


async def test_si_el_par_no_cabe_con_el_tamano_real_no_se_publica_ninguno_y_nada_se_trunca(h, p, monkeypatch):
    c = await conversacion(h)
    antes = (await recuperar(h, c["id"])).json()
    original = continuacion_service.MAX_BYTES_CONVERSACION
    monkeypatch.setattr(continuacion_service, "MAX_BYTES_CONVERSACION", await _bytes_actuales(h) + 300)  # cabe la pregunta, no la respuesta
    clave = uuid.uuid4()

    r = await continuar(h, p, c["id"], clave=clave)

    comprobar_error(r, 409, "CONVERSATION_SIZE_LIMIT_REACHED")
    assert p.llm.llamadas == 1  # la respuesta existía pero no se pudo publicar
    assert (await recuperar(h, c["id"])).json() == antes
    fila = (await intercambios(h))[0]
    assert (fila["estado"], fila["codigo_error"]) == ("FALLIDO", "CONVERSATION_SIZE_LIMIT_REACHED")

    monkeypatch.setattr(continuacion_service, "MAX_BYTES_CONVERSACION", original)
    completo = await continuar(h, p, c["id"], clave=clave)
    assert completo.status_code == 201
    persistido = (await mensajes_en_base(h))[-1]["datos_respuesta"]
    persistido = json.loads(persistido) if isinstance(persistido, str) else persistido
    assert persistido == completo.json()["respuesta"]["respuesta"]  # la respuesta se guardó entera


# ============================================ 16. Guardado inicial de la entrega 1 ============================================

async def test_reintentar_el_guardado_inicial_despues_de_continuar_sigue_siendo_idempotente_y_coherente(h, p):
    datos = cuerpo(h)
    inicial = await guardar(h, datos)
    assert inicial.status_code == 201
    c = inicial.json()
    antes = await consultar(h.url, "SELECT clave_idempotencia, huella_contenido FROM conversaciones_asistente")
    assert (await continuar(h, p, c["id"])).status_code == 201

    reintento = await guardar(h, datos)  # el mismo cuerpo y la misma clave del guardado inicial

    assert reintento.status_code == 200, reintento.text
    j = reintento.json()
    assert (j["id"], j["reutilizada"]) == (c["id"], True)
    assert j["total_mensajes"] == 6  # describe la conversación que existe AHORA
    assert [m["orden"] for m in j["mensajes"]] == [1, 2, 3, 4, 5, 6]
    assert [m["procedencia"] for m in j["mensajes"]] == ["APORTADO_POR_CLIENTE"] * 4 + ["ENVIADO_POR_USUARIO", "GENERADO_POR_SERVIDOR"]
    assert j["mensajes"][:4] == c["mensajes"]  # no restaura ni sobrescribe lo antiguo...
    assert await contar(h, "mensajes_asistente") == 6  # ...ni elimina lo nuevo
    despues = await consultar(h.url, "SELECT clave_idempotencia, huella_contenido FROM conversaciones_asistente")
    assert [tuple(f) for f in despues] == [tuple(f) for f in antes]  # la huella y la clave iniciales no cambian al añadir mensajes
    assert await contar(h, "conversaciones_asistente") == 1
    comprobar_error(await guardar(h, {**datos, "mensajes": pares(1)}), 409, "CONVERSATION_IDEMPOTENCY_CONFLICT")


# ============================================ 17. Migración 0010 ============================================

SQL_CONVERSACION = (
    "INSERT INTO conversaciones_asistente (id, ambiente, usuario_id, empresa_id, sector, anio, titulo, clave_idempotencia, huella_contenido)"
    " VALUES ($1, 'development', $2, $3, 'MINERIA', 2025, 'titulo', $4, $5)"
)
SQL_MENSAJE = (
    "INSERT INTO mensajes_asistente (id, conversacion_id, orden, rol, contenido, datos_respuesta, procedencia)"
    " VALUES ($1, $2, $3, $4, 'contenido', $5::jsonb, $6)"
)
SQL_INTERCAMBIO = (
    "INSERT INTO intercambios_asistente (id, conversacion_id, clave_idempotencia, huella_pregunta, estado, token_ejecucion, vence_en,"
    " codigo_error, mensaje_pregunta_id, mensaje_respuesta_id, completado_en)"
    " VALUES ($1, $2, $3, $4, $5, $6, now() + interval '5 minutes', $7, $8, $9, $10)"
)


async def test_la_0010_sobre_datos_de_la_entrega_1_los_conserva_con_su_procedencia_y_no_se_repite(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, esquema_hasta(9))
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    conversacion_id, pregunta_id, respuesta_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conexion = await conectar(url)
    try:
        await conexion.execute(SQL_CONVERSACION, conversacion_id, usuario, empresa, uuid.uuid4(), "a" * 64)
        await conexion.execute(SQL_MENSAJE, pregunta_id, conversacion_id, 1, "usuario", None, "APORTADO_POR_CLIENTE")
        await conexion.execute(SQL_MENSAJE, respuesta_id, conversacion_id, 2, "asistente", "{}", "APORTADO_POR_CLIENTE")
    finally:
        await conexion.close()
    antes = [tuple(f) for f in await consultar(url, "SELECT * FROM mensajes_asistente ORDER BY orden")]
    conversacion_antes = [tuple(f) for f in await consultar(url, "SELECT * FROM conversaciones_asistente")]
    migraciones_antes = [tuple(f) for f in await consultar(url, "SELECT version, nombre, checksum FROM igualab_migraciones ORDER BY version")]
    assert "intercambios_asistente" not in await tablas(url)

    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)

    assert (resultado.aplicadas, resultado.adoptadas, resultado.previas) == ((10, 11), (), tuple(range(1, 10)))
    assert "intercambios_asistente" in await tablas(url)
    assert [tuple(f) for f in await consultar(url, "SELECT * FROM mensajes_asistente ORDER BY orden")] == antes  # intactos, y APORTADO_POR_CLIENTE
    assert [tuple(f) for f in await consultar(url, "SELECT * FROM conversaciones_asistente")] == conversacion_antes
    despues = [tuple(f) for f in await consultar(url, "SELECT version, nombre, checksum FROM igualab_migraciones ORDER BY version")]
    assert despues[:9] == migraciones_antes  # la 0009 y las anteriores conservan su checksum: ninguna migración histórica se editó
    assert despues[9][:2] == (10, "continuacion_asistente")
    await verificar_esquema_central(url)
    siguiente = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert (siguiente.sin_cambios, siguiente.previas) == (True, tuple(range(1, 12)))  # 11 = deshabilitación de documentos (ACT-02)


async def test_una_instalacion_nueva_aplica_la_0009_y_la_0010_con_las_demas(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    resultado = await migrar(url, ESQUEMA_TRANSACCIONAL)
    assert resultado.aplicadas == tuple(range(1, 12))
    assert {"conversaciones_asistente", "mensajes_asistente", "intercambios_asistente"} <= await tablas(url)


async def test_las_restricciones_de_la_0010_protegen_procedencia_y_reservas_aunque_la_aplicacion_falle(url_pg_aislado):
    url = await base_transaccional_nueva(url_pg_aislado)
    await migrar(url, ESQUEMA_TRANSACCIONAL)
    usuario = (await consultar(url, "SELECT id FROM usuarios LIMIT 1"))[0]["id"]
    empresa = (await consultar(url, "SELECT id FROM empresas LIMIT 1"))[0]["id"]
    conversacion_id, huella = uuid.uuid4(), "b" * 64
    mensaje_p, mensaje_r, clave = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conexion = await conectar(url)
    try:
        await conexion.execute(SQL_CONVERSACION, conversacion_id, usuario, empresa, uuid.uuid4(), huella)

        async def viola(sql, *args) -> str:
            with pytest.raises((asyncpg.UniqueViolationError, asyncpg.CheckViolationError)) as capturado:
                await conexion.execute(sql, *args)
            return capturado.value.constraint_name

        # Procedencia: cada valor, solo con su rol.
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion_id, 1, "usuario", None, "GENERADO_POR_SERVIDOR") == "ck_mensajes_asistente_procedencia"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion_id, 1, "asistente", "{}", "ENVIADO_POR_USUARIO") == "ck_mensajes_asistente_procedencia"
        assert await viola(SQL_MENSAJE, uuid.uuid4(), conversacion_id, 1, "usuario", None, "INVENTADA") == "ck_mensajes_asistente_procedencia"
        await conexion.execute(SQL_MENSAJE, mensaje_p, conversacion_id, 1, "usuario", None, "ENVIADO_POR_USUARIO")
        await conexion.execute(SQL_MENSAJE, mensaje_r, conversacion_id, 2, "asistente", "{}", "GENERADO_POR_SERVIDOR")
        await conexion.execute(SQL_MENSAJE, uuid.uuid4(), conversacion_id, 3, "usuario", None, "APORTADO_POR_CLIENTE")  # la importada sigue admitida

        def intercambio(estado, *, clave_i=None, huella_i=huella, error=None, ids=(None, None), completado=None):
            return (SQL_INTERCAMBIO, uuid.uuid4(), conversacion_id, clave_i or uuid.uuid4(), huella_i, estado, uuid.uuid4(), error, ids[0], ids[1], completado)

        await conexion.execute(*intercambio("EN_CURSO", clave_i=clave))
        assert await viola(*intercambio("EN_CURSO")) == "uq_intercambios_asistente_en_curso"  # una sola consulta en curso por conversación
        assert await viola(*intercambio("FALLIDO", clave_i=clave)) == "uq_intercambios_asistente_clave"  # un intercambio por clave
        await conexion.execute(*intercambio("FALLIDO", error="LLM_PROVIDER_ERROR"))  # el índice parcial solo limita EN_CURSO
        await conexion.execute(*intercambio("VENCIDO"))
        assert await viola(*intercambio("OTRO")) == "ck_intercambios_asistente_estado"
        assert await viola(*intercambio("FALLIDO", huella_i="mala")) == "ck_intercambios_asistente_huella"
        assert await viola(*intercambio("COMPLETADO")) in {"ck_intercambios_asistente_mensajes", "ck_intercambios_asistente_completado_en"}
        assert await viola(*intercambio("FALLIDO", ids=(mensaje_p, mensaje_r))) == "ck_intercambios_asistente_mensajes"
        assert await viola(*intercambio("VENCIDO", error="X")) == "ck_intercambios_asistente_error"
        await conexion.execute("UPDATE intercambios_asistente SET estado = 'VENCIDO' WHERE estado = 'EN_CURSO'")
        ahora = (await conexion.fetchval("SELECT now()"))
        await conexion.execute(*intercambio("COMPLETADO", ids=(mensaje_p, mensaje_r), completado=ahora))
        assert await viola(*intercambio("COMPLETADO", ids=(mensaje_p, mensaje_r), completado=ahora)) == "uq_intercambios_asistente_mensaje_pregunta"
    finally:
        await conexion.close()


# ============================================ 18. POST /asistente/consultas intacto ============================================

async def test_post_consultas_conserva_su_contrato_y_no_guarda_mensajes_ni_reservas(h, p):
    p.fragmentos = []
    async with cliente_http(construir_app(h, h.ana, con_consultas=True)) as c:
        r = await c.post(
            "/asistente/consultas",
            json={"pregunta": PREGUNTA, "sector": "MINERIA", "empresa_id": str(h.mineria.id), "anio": ANIO},
        )
    assert r.status_code == 200, r.text
    # Las claves anteriores se conservan; el contrato solo suma dos opcionales (null / REALIZADA por omisión).
    assert set(r.json()) == {"respuesta", "afirmaciones", "citas", "sin_evidencia", "motivo", "mensaje", "afirmaciones_descartadas", "estado_registrado", "busqueda_documental"}
    assert (r.json()["estado_registrado"], r.json()["busqueda_documental"]) == (None, "REALIZADA")
    assert r.json()["motivo"] == MOTIVO_SIN_FRAGMENTOS
    assert h.embeddings.llamadas == 1
    assert (await contar(h, "conversaciones_asistente"), await contar(h, "mensajes_asistente"), await contar(h, "intercambios_asistente")) == (0, 0, 0)

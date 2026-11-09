"""Estado REGISTRADO en Análisis GRI dentro del Asistente IA, por `POST /asistente/consultas`, sobre un PostgreSQL REAL, AISLADO y TEMPORAL.
(La continuación de conversaciones está en `test_seguimiento_asistente_postgres.py`; las reglas puras, en `test_estado_gri_asistente.py`.)

Origen: el incidente de «Camposol S.A.», 2025, donde GRI 1 figuraba en Análisis GRI y en el reporte pero el chat respondió «No se encontró
evidencia…». Los datos son SINTÉTICOS («Camposol S.A. (SINTETICA)» solo reproduce el contexto): NO es el incidente real.

REAL: el detector GRI, el fragmentador, el servicio de Análisis GRI (estados, validación, vigencia), el router del asistente, la comprobación
de empresa/sector/ambiente y `documentos_recuperables`. CONTROLADOS (dobles): la búsqueda vectorial (`buscar_similares`), los embeddings y el
modelo de chat. Con ellos se demuestra QUÉ se informa y desde DÓNDE; NO se demuestra qué fragmentos devuelve pgvector/OCI para una pregunta ni
cómo responde DeepSeek, que es lo que falta comprobar con Camposol 2025."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine

import pytest

from app.core.exceptions import ConflictError, ExternalServiceError
from app.models import SectorEmpresa
from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.application import servicio as servicio_analisis
from app.modules.asistente import router as router_asistente
from app.modules.asistente import service as servicio
from app.modules.asistente.domain.estado_gri import codigos_para_estado, solo_estado
from app.modules.asistente.domain.historial import problemas_de_respuesta
from app.modules.asistente.schemas import ConsultaResponse
from app.modules.ingesta.domain.fragmentacion import fragmentar
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado
from tests.ayudantes_analisis_gri import (
    asegurar_esquema_real,
    cliente_http,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    sembrar_documento,
)
from tests.services.test_asistente import afirmacion, respuesta
from tests.services.test_asistente_postgres import EmbeddingsControlado, LlmControlado, RUTA, construir_app, cuerpo

NOMBRE = "Camposol S.A. (SINTETICA)"
PARRAFO = (
    "La compañía continuó fortaleciendo su gestión durante el periodo, con programas de capacitación, monitoreo ambiental y "
    "relación con comunidades vecinas. Los resultados se presentan por frente de trabajo y se comparan con las metas internas. "
)


def _seccion(titulo: str, repeticiones: int) -> str:
    return f"## {titulo}\n\n" + PARRAFO * repeticiones + "\n\n"


TEXTO = (
    "# Reporte de sostenibilidad 2025\n\n"
    + _seccion("Mensaje de la gerencia", 3)
    + "## Perfil de la organización\n\n"
    + PARRAFO * 3
    + "El sector de referencia es GRI 13: Agricultura, acuicultura y pesca 2022. Los datos generales siguen GRI 102-1.\n\n"
    + "## Acerca de este reporte\n\n"
    "Este reporte se elaboró con referencia a los Estándares GRI. La organización utilizó GRI 1: Fundamentos 2021 como base de "
    "elaboración.\n\n"
    + _seccion("Gestión del agua", 6)
    + _seccion("Emisiones y cambio climático", 6)
    + "## Índice de contenidos GRI\n\n"
    "| Estándar GRI | Descripción | Ubicación |\n|---|---|---|\n"
    "| GRI 1 | Fundamentos 2021 | Acerca de este reporte |\n"
    "| GRI 13 | Agricultura, acuicultura y pesca 2022 | Perfil |\n"
    "| GRI 305-1 | Emisiones directas | Emisiones |\n\n"
)
TEXTO_2024 = "# Reporte 2024\n\n## Acerca de este reporte\n\nLa organización utilizó GRI 1: Fundamentos 2021 como base de elaboración.\n"

PREGUNTA_ESTADO = "¿Qué estado tiene GRI 1?"
PREGUNTA_BRECHA = "Sobre el GRI 1, ¿qué me puedes comentar? ¿Hay una brecha?"
PREGUNTA_MIXTA = "¿Qué dice el documento sobre GRI 1 y hay una brecha?"
CODIGO_GRI_1 = re.compile(r"GRI 1(?![\d\-])")
IDENTIDADES = ("1:2021", "13:2022", "102:ambigua", "305:2016")


class LlmFiel(LlmControlado):
    """Doble DETERMINISTA y fiel a las reglas del prompt: solo afirma lo que está en los fragmentos entregados (y devuelve
    `afirmaciones: []` si ninguno trae «GRI 1»). Registra lo que RECIBE: el estado registrado no debe aparecer ahí."""

    async def completar(self, sistema: str, usuario: str) -> str:
        await super().completar(sistema, usuario)
        for numero, literal in re.findall(r"<<<FRAGMENTO (\d+) \w+>>>\n[^\n]*\n(.*?)\n<<<FIN FRAGMENTO", usuario, flags=re.DOTALL):
            if CODIGO_GRI_1.search(literal):
                cita = "La organización utilizó GRI 1: Fundamentos 2021 como base de elaboración"
                if cita not in literal:
                    cita = "| GRI 1 | Fundamentos 2021 | Acerca de este reporte |"
                return respuesta(afirmacion(texto="El reporte menciona GRI 1: Fundamentos 2021.", referencias=(int(numero),), cita=cita))
        return respuesta()


def _recuperados(documento, empresa, texto: str) -> list[FragmentoRecuperado]:
    return [
        FragmentoRecuperado(
            documento_id=documento.id, indice=f.indice, texto_literal=f.texto_literal, contexto=f.contexto, inicio=f.inicio, fin=f.fin,
            continuacion=f.continuacion, ruta_encabezados=f.ruta_encabezados, empresa_id=empresa.id, anio=documento.anio,
            tipo="MEMORIA_ANUAL", sector="ENERGIA", embedding_proveedor="oci", embedding_modelo="cohere.embed-v4.0", similitud=0.5,
        )
        for f in fragmentar(texto)
    ]


@pytest_asyncio.fixture
async def caso(fabrica_pg, url_pg_aislado, monkeypatch):
    await asegurar_esquema_real(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, NOMBRE, sector=SectorEmpresa.ENERGIA)
        otra = await crear_empresa(db, "Otra Energia S.A. (SINTETICA)", sector=SectorEmpresa.ENERGIA)
        doc = await sembrar_documento(db, empresa, admin, texto=TEXTO, nombre="reporte-2025-sintetico.md")
        doc_2024 = await sembrar_documento(db, empresa, admin, texto=TEXTO_2024, anio=2024, nombre="reporte-2024-sintetico.md")
        doc_qa = await sembrar_documento(db, empresa, admin, texto=TEXTO, ambiente="qa", nombre="reporte-2025-qa.md")
        doc_otra = await sembrar_documento(db, otra, admin, texto=TEXTO, nombre="otra-2025.md")
    fragmentos = _recuperados(doc, empresa, TEXTO)
    busquedas: list[dict] = []
    entorno = SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, superadmin=superadmin, empresa=empresa, otra=otra, doc=doc, doc_2024=doc_2024, doc_qa=doc_qa,
        doc_otra=doc_otra, fragmentos=fragmentos, busquedas=busquedas, devolver=[], embeddings=EmbeddingsControlado(), llm=LlmFiel(),
    )

    async def buscar_controlado(db_vectorial, **kwargs):
        busquedas.append(kwargs)
        return list(entorno.devolver)

    monkeypatch.setattr(servicio, "buscar_similares", buscar_controlado)
    return entorno


# ---------------------------------------------------- preparación del Análisis GRI (servicio real) ----------------------------------------------------

async def asignar(caso, estados: dict, *, empresa=None, ambiente="development", anio=2025):
    empresa = empresa or caso.empresa
    for identidad, estado in estados.items():
        async with caso.fabrica() as db:
            await servicio_analisis.asignar_estado(
                db, ambiente=ambiente, empresa_id=empresa.id, anio=anio, identidad=identidad, estado=EstadoGri[estado],
                version_fila_esperada=0, actor=caso.admin,
            )


async def validar(caso, *, empresa=None, ambiente="development", anio=2025):
    empresa = empresa or caso.empresa
    async with caso.fabrica() as db:
        actual = await servicio_analisis.consultar_analisis(db, ambiente=ambiente, empresa_id=empresa.id, anio=anio)
    async with caso.fabrica() as db:
        await servicio_analisis.validar_analisis(
            db, ambiente=ambiente, empresa_id=empresa.id, anio=anio, version_esperada=actual.version,
            huella_esperada=actual.huella_documentos, actor=caso.admin,
        )


ESTADOS = {"1:2021": "BAJA_SUSTANCIA", "13:2022": "OK", "102:ambigua": "SUB_REPORTADO", "305:2016": "OK"}


async def validado(caso):
    await asignar(caso, ESTADOS)
    await validar(caso)


# ---------------------------------------------------- consulta HTTP ----------------------------------------------------

async def preguntar(caso, pregunta, **cambios):
    async with cliente_http(construir_app(caso, caso.admin)) as c:
        return await c.post(RUTA, json=cuerpo(caso, **{"pregunta": pregunta, "sector": "ENERGIA", **cambios}))


def con_gri_1(caso):
    return [f for f in caso.fragmentos if CODIGO_GRI_1.search(f.texto_literal)]


def sin_gri_1(caso):
    return [f for f in caso.fragmentos if not CODIGO_GRI_1.search(f.texto_literal)]


def guardable(caso, datos: dict) -> list[str]:
    """Las reglas con que el historial valida una respuesta aportada: el estado en `mensaje` no las viola."""
    return problemas_de_respuesta(ConsultaResponse(**datos), caso.empresa.id, 2025)


def bloque_de(datos: dict) -> str:
    """El texto del estado registrado: viene en `estado_registrado.texto` tanto en las respuestas de solo estado como en las mixtas."""
    estado = datos["estado_registrado"]
    assert estado is not None, datos
    return estado["texto"]


# ============================== 1. Estado con validación vigente (el caso del incidente) ==============================

async def test_la_pregunta_de_estado_informa_el_estado_y_la_vigencia_aunque_la_busqueda_no_traiga_el_fragmento_de_gri_1(caso):
    await validado(caso)
    caso.devolver = sin_gri_1(caso)[:4]  # el buscador no trajo el fragmento de GRI 1 (hipótesis del incidente, aquí fijada)

    r = await preguntar(caso, PREGUNTA_BRECHA)

    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["sin_evidencia"], d["motivo"]) == (True, "MODELO_SIN_EVIDENCIA")  # la parte documental queda como estaba
    assert d["respuesta"] == "" and d["afirmaciones"] == [] and d["citas"] == []  # el estado NO es una cita ni una afirmación
    assert d["mensaje"].startswith(f"Estado registrado en Análisis GRI ({NOMBRE}, 2025):")
    assert "- GRI 1 (2021): Baja sustancia. Validación vigente (n.º 1, validada el " in d["mensaje"]
    assert d["busqueda_documental"] == "REALIZADA"  # sí hubo búsqueda: la ausencia de evidencia es de la búsqueda, no de la pregunta
    assert d["estado_registrado"]["texto"] in d["mensaje"] and d["estado_registrado"]["validacion_vigente"] is True
    assert "\n\nEvidencia documental: No se encontró evidencia en los documentos de la empresa y el año seleccionados" in d["mensaje"]
    assert "No hay una regla registrada que convierta el estado en «hay» o «no hay» brecha" in d["mensaje"]
    assert guardable(caso, d) == []  # cumple las invariantes del historial: se puede guardar tal cual


async def test_el_estado_lo_decide_el_servidor_y_nunca_llega_al_modelo(caso):
    await validado(caso)
    caso.devolver = con_gri_1(caso)

    r = await preguntar(caso, PREGUNTA_BRECHA)

    assert r.status_code == 200, r.text
    enviado = caso.llm.sistema + caso.llm.usuario
    for texto in ("Baja sustancia", "BAJA_SUSTANCIA", "Estado registrado", "Validación vigente", "validación"):
        assert texto not in enviado.replace(PREGUNTA_BRECHA, "")
    assert caso.llm.llamadas == 1  # el flujo documental es el de siempre: una llamada, con los fragmentos


async def test_las_etiquetas_exactas_de_los_tres_estados_se_informan_tal_cual(caso):
    await asignar(caso, {"1:2021": "SUB_REPORTADO", "13:2022": "OK", "102:ambigua": "BAJA_SUSTANCIA", "305:2016": "OK"})
    await validar(caso)
    d1 = (await preguntar(caso, PREGUNTA_ESTADO)).json()
    d13 = (await preguntar(caso, "¿Qué estado tiene GRI 13?")).json()
    d102 = (await preguntar(caso, "¿Qué estado tiene GRI 102?")).json()
    assert "GRI 1 (2021): Sub-reportado." in bloque_de(d1)
    assert "GRI 13 (2022): OK." in bloque_de(d13)
    assert "GRI 102 (edición sin resolver): Baja sustancia." in bloque_de(d102)
    assert [f["estado"] for f in d1["estado_registrado"]["filas"]] == ["SUB_REPORTADO"]  # el valor estable, además de la etiqueta


# ============================== 2. Pregunta mixta: dos fuentes separadas ==============================

async def test_pregunta_mixta_con_evidencia_documental_mantiene_las_citas_y_agrega_el_estado_aparte(caso):
    await validado(caso)
    caso.devolver = con_gri_1(caso) + sin_gri_1(caso)[:2]

    r = await preguntar(caso, PREGUNTA_MIXTA)

    d = r.json()
    assert d["sin_evidencia"] is False and d["motivo"] is None
    assert d["citas"] and all(CODIGO_GRI_1.search(c["extracto"]) for c in d["citas"])  # evidencia documental: citas del documento
    assert re.search(r"\[\d+\]", d["respuesta"]) and "Baja sustancia" not in d["respuesta"]  # el texto de la evidencia no lleva el estado
    for a in d["afirmaciones"]:
        assert "Baja sustancia" not in a["texto"] + a["cita_textual"]
    assert "GRI 1 (2021): Baja sustancia. Validación vigente" in d["mensaje"]  # el estado va aparte, sin cita
    assert "Evidencia documental" not in d["mensaje"]  # la evidencia ya está en `respuesta` y `citas`
    assert d["busqueda_documental"] == "REALIZADA" and d["estado_registrado"]["filas"][0]["estado"] == "BAJA_SUSTANCIA"
    assert d["estado_registrado"]["fuente"] == "ANALISIS_GRI" and "[" not in d["estado_registrado"]["texto"]  # nunca un marcador de cita
    assert guardable(caso, d) == []


async def test_pregunta_mixta_sin_evidencia_documental_deja_visible_el_estado_registrado(caso):
    await validado(caso)
    caso.devolver = []  # ningún fragmento

    d = (await preguntar(caso, PREGUNTA_MIXTA)).json()

    assert (d["sin_evidencia"], d["motivo"], d["citas"], d["afirmaciones"]) == (True, "SIN_FRAGMENTOS", [], [])
    assert "GRI 1 (2021): Baja sustancia. Validación vigente" in d["mensaje"]
    assert "Evidencia documental: No hay documentos publicados de esa empresa, sector y año" in d["mensaje"]
    assert caso.llm.llamadas == 0 and guardable(caso, d) == []


async def test_si_las_afirmaciones_del_modelo_se_descartan_el_estado_sigue_visible_y_los_descartes_se_conservan(caso):
    await validado(caso)
    caso.devolver = caso.fragmentos[:4]
    caso.llm = LlmControlado(respuesta(afirmacion(texto="GRI 1 está en brecha.", referencias=(1,), cita="la empresa tiene una brecha en GRI 1")))

    d = (await preguntar(caso, PREGUNTA_BRECHA)).json()

    assert (d["motivo"], d["afirmaciones_descartadas"]) == ("AFIRMACIONES_NO_VERIFICABLES", 1)  # la verificación de citas no se debilitó
    assert "GRI 1 (2021): Baja sustancia" in d["mensaje"] and "El asistente no pudo respaldar ninguna afirmación" in d["mensaje"]


# ============================== 3. Vigencia y estados incompletos ==============================

async def test_si_la_validacion_perdio_vigencia_informa_el_estado_actual_y_aclara_que_no_esta_vigente(caso):
    await validado(caso)
    await asignar_nuevo_estado(caso, "1:2021", "OK")

    d = (await preguntar(caso, PREGUNTA_ESTADO)).json()

    bloque = bloque_de(d)
    assert "GRI 1 (2021): OK. No cuenta con validación vigente: la última validación (n.º 1) perdió vigencia" in bloque
    assert "cambiaron los estados" in bloque and "Validación vigente" not in bloque


async def asignar_nuevo_estado(caso, identidad: str, estado: str, version_fila: int = 1):
    async with caso.fabrica() as db:
        await servicio_analisis.asignar_estado(
            db, ambiente="development", empresa_id=caso.empresa.id, anio=2025, identidad=identidad, estado=EstadoGri[estado],
            version_fila_esperada=version_fila, actor=caso.admin,
        )


async def test_con_los_estados_asignados_pero_sin_validar_dice_que_el_analisis_aun_no_se_valido(caso):
    await asignar(caso, ESTADOS)

    d = (await preguntar(caso, PREGUNTA_ESTADO)).json()

    assert "GRI 1 (2021): Baja sustancia. No cuenta con validación vigente: el análisis aún no se ha validado." in bloque_de(d)
    assert (d["estado_registrado"]["validacion_vigente"], d["estado_registrado"]["validacion"]) == (False, None)


async def test_un_codigo_detectado_sin_estado_asignado_lo_dice(caso):
    await asignar(caso, {"13:2022": "OK"})  # GRI 1 está en el análisis pero nadie le asignó estado

    d = (await preguntar(caso, PREGUNTA_ESTADO)).json()

    assert "GRI 1 (2021): figura en el análisis, pero no tiene estado asignado." in bloque_de(d)
    assert "Baja sustancia" not in bloque_de(d) and "Sub-reportado" not in bloque_de(d)
    assert [(f["estandar"], f["estado"], f["estado_etiqueta"]) for f in d["estado_registrado"]["filas"]] == [("GRI 1 (2021)", None, None)]


async def test_un_codigo_que_no_figura_en_el_analisis_no_concluye_ausencia_en_el_documento_ni_brecha(caso):
    await validado(caso)

    d = (await preguntar(caso, "¿Hay una brecha en GRI 55?")).json()

    assert "GRI 55: no figura en el Análisis GRI de esta empresa y año (no se concluye nada sobre el documento)." in d["mensaje"]
    for conclusion in ("ausente", "hay una brecha en gri 55", "existe una brecha"):
        assert conclusion not in d["mensaje"].casefold()


# ============================== 4. GRI 1 no es GRI 13, GRI 102 ni sus sufijos ==============================

async def test_gri_1_gri_13_y_gri_102_se_resuelven_cada_uno_con_su_propia_fila_real(caso):
    await validado(caso)
    uno = bloque_de((await preguntar(caso, PREGUNTA_ESTADO)).json())
    trece = bloque_de((await preguntar(caso, "¿Qué estado tiene GRI 13?")).json())
    ciento_dos = bloque_de((await preguntar(caso, "¿Qué estado tiene GRI 102?")).json())

    assert "GRI 1 (2021): Baja sustancia" in uno and "GRI 13" not in uno and "GRI 102" not in uno
    assert "GRI 13 (2022): OK" in trece and "GRI 1 (2021)" not in trece and "Baja sustancia" not in trece
    assert "GRI 102 (edición sin resolver): Sub-reportado" in ciento_dos and "GRI 1 (2021)" not in ciento_dos
    # La identidad ambigua no se resuelve arbitrariamente: si se pide una edición, no se le atribuye ese estado.
    con_edicion = bloque_de((await preguntar(caso, "¿Qué estado tiene GRI 102 (2016)?")).json())
    assert "no hay una fila de esa edición" in con_edicion and "Sub-reportado" not in con_edicion


async def test_una_revelacion_con_sufijo_se_informa_como_el_estado_de_su_estandar_y_no_como_gri_1(caso):
    await validado(caso)

    d = (await preguntar(caso, "¿Cuál es el estado de GRI 305-1?")).json()

    assert "GRI 305-1: el estado se registra por estándar (GRI 305), no por revelación." in d["mensaje"]
    assert "GRI 305 (2016): OK" in d["mensaje"] and "GRI 1 (2021)" not in d["mensaje"]


# ============================== 5. Aislamiento por empresa, año y ambiente ==============================

async def test_el_estado_es_el_de_la_misma_empresa_anio_y_ambiente_de_la_consulta(caso):
    await validado(caso)  # development, 2025, Camposol: GRI 1 = Baja sustancia, validado
    await asignar(caso, {"1:2021": "SUB_REPORTADO"}, empresa=caso.otra)  # otra empresa del mismo sector
    await asignar(caso, {"1:2021": "OK"}, anio=2024)  # otro año de la misma empresa
    await asignar(caso, {"1:2021": "OK"}, ambiente="qa")  # otro ambiente

    propia = bloque_de((await preguntar(caso, PREGUNTA_ESTADO)).json())
    otra = bloque_de((await preguntar(caso, PREGUNTA_ESTADO, empresa_id=str(caso.otra.id))).json())
    anterior = bloque_de((await preguntar(caso, PREGUNTA_ESTADO, anio=2024)).json())

    assert "Camposol" in propia and "GRI 1 (2021): Baja sustancia. Validación vigente" in propia
    assert "OK" not in propia.replace("Sub-reportado", "")  # ni el del otro año ni el de qa
    assert "Otra Energia" in otra and "GRI 1 (2021): Sub-reportado. No cuenta con validación vigente" in otra and "Baja sustancia" not in otra
    assert ", 2024):" in anterior and "GRI 1 (2021): OK. No cuenta con validación vigente" in anterior and "Baja sustancia" not in anterior


async def test_el_permiso_el_sector_y_la_empresa_se_comprueban_antes_de_leer_el_estado(caso, monkeypatch):
    lecturas = []
    original = servicio.consultar_analisis

    async def espiar(*args, **kwargs):
        lecturas.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(servicio, "consultar_analisis", espiar)

    mismatch = await preguntar(caso, PREGUNTA_ESTADO, sector="MINERIA")  # la empresa es ENERGIA
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == "COMPANY_SECTOR_MISMATCH"
    async with cliente_http(construir_app(caso, caso.superadmin)) as c:
        assert (await c.post(RUTA, json=cuerpo(caso, pregunta=PREGUNTA_ESTADO, sector="ENERGIA"))).status_code == 403
    async with cliente_http(construir_app(caso, None)) as c:
        assert (await c.post(RUTA, json=cuerpo(caso, pregunta=PREGUNTA_ESTADO, sector="ENERGIA"))).status_code == 401
    assert lecturas == [] and caso.llm.llamadas == 0 and caso.embeddings.llamadas == 0

    caso.devolver = []
    await preguntar(caso, PREGUNTA_ESTADO)
    assert [(k["ambiente"], k["empresa_id"], k["anio"]) for k in lecturas] == [("development", caso.empresa.id, 2025)]


# ============================== 6. Sin petición de estado todo sigue como antes ==============================

async def test_una_consulta_documental_sin_pedir_estado_no_lee_el_analisis_y_responde_como_antes(caso):
    await validado(caso)
    caso.devolver = con_gri_1(caso)
    sentencias: list[str] = []

    def registrar(conn, cursor, statement, parameters, context, executemany):
        sentencias.append(statement)

    event.listen(Engine, "before_cursor_execute", registrar)
    try:
        d = (await preguntar(caso, "Sobre el GRI 1, ¿qué me puedes comentar?")).json()
    finally:
        event.remove(Engine, "before_cursor_execute", registrar)

    assert d["sin_evidencia"] is False and d["citas"] and d["mensaje"] is None  # idéntico a la respuesta anterior: sin bloque de estado
    assert {t for s in sentencias for t in re.findall(r"\b(analisis_gri_\w+|reportes_\w+)\b", s)} == set()
    assert any("documentos" in s for s in sentencias)


async def test_pedir_estado_sin_nombrar_un_codigo_no_consulta_el_analisis(caso):
    await validado(caso)
    caso.devolver = []

    d = (await preguntar(caso, "¿Hay alguna brecha en la empresa?")).json()

    assert d["motivo"] == "SIN_FRAGMENTOS" and "Estado registrado" not in d["mensaje"]


async def test_los_filtros_k_y_el_embedding_de_la_recuperacion_documental_no_cambian(caso):
    await validado(caso)
    caso.devolver = sin_gri_1(caso)[:4]

    await preguntar(caso, PREGUNTA_BRECHA)

    (busqueda,) = caso.busquedas
    assert (busqueda["ambiente"], busqueda["empresa_ids"], busqueda["anio"], busqueda["sector"], busqueda["limite"]) == (
        "development", [caso.empresa.id], 2025, SectorEmpresa.ENERGIA, 4,
    )
    assert set(busqueda) == {"ambiente", "vector_consulta", "identidad", "empresa_ids", "limite", "anio", "sector"}
    assert caso.embeddings.llamadas == 1


async def test_los_motivos_de_la_parte_documental_siguen_distinguiendose(caso):
    documental = "¿Qué dice el documento sobre GRI 1?"
    caso.devolver = []
    sin_fragmentos = (await preguntar(caso, documental)).json()
    caso.devolver = caso.fragmentos[:4]
    caso.llm = LlmControlado(respuesta(afirmacion(texto="GRI 1 está en brecha.", referencias=(1,), cita="la empresa tiene una brecha en GRI 1")))
    descartadas = (await preguntar(caso, documental)).json()

    assert (sin_fragmentos["motivo"], sin_fragmentos["afirmaciones_descartadas"]) == ("SIN_FRAGMENTOS", 0)
    assert (descartadas["motivo"], descartadas["afirmaciones_descartadas"]) == ("AFIRMACIONES_NO_VERIFICABLES", 1)


# ============================== 7. Robustez ==============================

async def test_si_el_analisis_esta_cambiando_no_se_pierde_la_parte_documental(caso, monkeypatch):
    caso.devolver = con_gri_1(caso)

    async def cambiando(*args, **kwargs):
        raise ConflictError("ANALYSIS_CHANGING", "El análisis está cambiando; vuelva a consultarlo.")

    monkeypatch.setattr(servicio, "consultar_analisis", cambiando)

    r = await preguntar(caso, PREGUNTA_MIXTA)

    d = r.json()
    assert r.status_code == 200 and d["sin_evidencia"] is False and d["citas"]
    assert "no se pudo leer en este momento porque el análisis está cambiando" in d["mensaje"] and guardable(caso, d) == []


async def test_con_muchos_codigos_el_mensaje_cabe_en_el_limite_del_historial_y_sigue_siendo_guardable(caso):
    await validado(caso)
    caso.devolver = []

    d = (await preguntar(caso, "¿Estado de GRI 1? ¿Estado de GRI 13? ¿Estado de GRI 102? ¿Estado de GRI 305? ¿Hay brecha?")).json()

    assert len(d["mensaje"]) <= 1_000 and "se omiten 1 códigos más" in d["mensaje"]
    assert d["mensaje"].count("\n- GRI ") == 3  # tres códigos con su estado; el cuarto se avisa, no se recorta
    assert "Evidencia documental: No hay documentos publicados" in d["mensaje"] and guardable(caso, d) == []


# ============================== 8. Solo estado: se responde desde Análisis GRI SIN recursos documentales ==============================
# Regresión de la limitación anterior (antes, cualquiera de estos recursos caídos bloqueaba «¿Qué estado tiene GRI 1?» con un 503/502).
# La app usa el `get_recursos_consulta` REAL (la fábrica de recursos diferidos de la continuación) con UN recurso no disponible y todos los
# puntos de preparación espiados: lo que se prueba es que una pregunta de solo estado ni los prepara ni los llama.

SOLO_ESTADO = "¿Qué estado tiene GRI 1?"
DOCUMENTAL = "¿Qué dice el documento sobre GRI 1?"
RECURSO_CAIDO = {  # recurso -> (estado HTTP, código) que produce una consulta DOCUMENTAL, igual que antes de este cambio
    "esquema_vectorial": (503, "INGESTION_NOT_CONFIGURED"),
    "base_vectorial": (502, "VECTOR_DATABASE_NOT_CONFIGURED"),
    "embeddings": (503, "EMBEDDINGS_NOT_CONFIGURED"),
    "modelo": (503, "LLM_NOT_CONFIGURED"),
}


def app_con_el_recurso_caido(caso, monkeypatch, caido: str | None):
    """(app, contadores): los recursos reales de la ruta, con `caido` no disponible y todos los pasos de preparación contados."""
    from contextlib import asynccontextmanager

    from app.core.exceptions import ServiceUnavailableError
    from app.modules.asistente import continuacion_router

    pasos = SimpleNamespace(esquema=0, url=0, embeddings=0, modelo=0, sesiones=0)

    def esquema(request):
        pasos.esquema += 1
        if caido == "esquema_vectorial":
            raise ServiceUnavailableError("INGESTION_NOT_CONFIGURED", "El esquema vectorial no está preparado.")

    def url():
        pasos.url += 1
        if caido == "base_vectorial":
            raise ExternalServiceError("VECTOR_DATABASE_NOT_CONFIGURED", "La base de datos vectorial no está configurada.")
        return "postgresql+asyncpg://prueba"

    def embeddings():
        pasos.embeddings += 1
        if caido == "embeddings":
            raise ServiceUnavailableError("EMBEDDINGS_NOT_CONFIGURED", "Falta configurar el proveedor de embeddings.")
        return caso.embeddings

    def modelo():
        pasos.modelo += 1
        if caido == "modelo":
            raise ServiceUnavailableError("LLM_NOT_CONFIGURED", "Falta configurar el proveedor de IA.")
        return caso.llm

    def fabrica():
        @asynccontextmanager
        async def sesion():
            pasos.sesiones += 1
            yield None

        return sesion

    monkeypatch.setattr(continuacion_router, "esquema_vectorial_listo", esquema)
    monkeypatch.setattr(continuacion_router, "url_vectorial", url)
    monkeypatch.setattr(continuacion_router, "fabrica_sesiones_vectoriales", lambda: fabrica())
    monkeypatch.setattr(router_asistente, "get_proveedor_embeddings", embeddings)
    monkeypatch.setattr(router_asistente, "get_proveedor_llm", modelo)
    app = construir_app(caso, caso.admin)
    del app.dependency_overrides[router_asistente.get_recursos_consulta]  # vuelve a ser la fábrica REAL
    return app, pasos


async def preguntar_en(app, caso, pregunta, **cambios):
    async with cliente_http(app) as c:
        return await c.post(RUTA, json=cuerpo(caso, **{"pregunta": pregunta, "sector": "ENERGIA", **cambios}))


def sin_llamadas_documentales(caso, pasos) -> bool:
    return (
        (pasos.esquema, pasos.url, pasos.embeddings, pasos.modelo, pasos.sesiones) == (0, 0, 0, 0, 0)
        and (caso.embeddings.llamadas, caso.llm.llamadas, caso.busquedas) == (0, 0, [])
    )


@pytest.mark.parametrize("caido", [*RECURSO_CAIDO, None], ids=[*RECURSO_CAIDO, "ninguno"])
async def test_una_pregunta_de_solo_estado_se_responde_con_los_recursos_documentales_caidos_y_sin_tocarlos(caso, monkeypatch, caido):
    await validado(caso)
    app, pasos = app_con_el_recurso_caido(caso, monkeypatch, caido)

    r = await preguntar_en(app, caso, SOLO_ESTADO)

    assert r.status_code == 200, r.text
    d = r.json()
    assert sin_llamadas_documentales(caso, pasos)  # ni se prepararon recursos, ni embeddings, ni búsqueda, ni modelo
    assert (d["sin_evidencia"], d["motivo"], d["busqueda_documental"]) == (False, None, "NO_REALIZADA")  # NO anuncia «no se encontró evidencia»
    assert (d["afirmaciones"], d["citas"], d["afirmaciones_descartadas"], d["mensaje"]) == ([], [], 0, None)  # el estado no es una cita
    assert d["respuesta"] == d["estado_registrado"]["texto"]  # un cliente antiguo lo muestra como respuesta
    assert "GRI 1 (2021): Baja sustancia. Validación vigente (n.º 1" in d["respuesta"] and "[" not in d["respuesta"]
    assert guardable(caso, d) == []  # y es una respuesta válida para el historial


async def test_el_estado_de_solo_estado_conserva_empresa_anio_ambiente_sector_y_permisos(caso, monkeypatch):
    await validado(caso)
    await asignar(caso, {"1:2021": "SUB_REPORTADO"}, empresa=caso.otra)
    await asignar(caso, {"1:2021": "OK"}, anio=2024)
    await asignar(caso, {"1:2021": "OK"}, ambiente="qa")
    app, pasos = app_con_el_recurso_caido(caso, monkeypatch, "esquema_vectorial")

    propia = (await preguntar_en(app, caso, SOLO_ESTADO)).json()
    otra = (await preguntar_en(app, caso, SOLO_ESTADO, empresa_id=str(caso.otra.id))).json()
    anterior = (await preguntar_en(app, caso, SOLO_ESTADO, anio=2024)).json()
    inexistente = await preguntar_en(app, caso, SOLO_ESTADO, empresa_id="00000000-0000-4000-8000-000000000000")
    otro_sector = await preguntar_en(app, caso, SOLO_ESTADO, sector="MINERIA")
    superadmin = construir_app(caso, caso.superadmin)
    del superadmin.dependency_overrides[router_asistente.get_recursos_consulta]
    sesion_ausente = construir_app(caso, None)

    assert "GRI 1 (2021): Baja sustancia. Validación vigente" in propia["respuesta"] and "OK" not in propia["respuesta"]
    assert "Otra Energia" in otra["respuesta"] and "Sub-reportado" in otra["respuesta"]
    assert ", 2024):" in anterior["respuesta"] and "GRI 1 (2021): OK" in anterior["respuesta"]
    assert (inexistente.status_code, inexistente.json()["error"]["code"]) == (404, "COMPANY_NOT_FOUND")
    assert (otro_sector.status_code, otro_sector.json()["error"]["code"]) == (400, "COMPANY_SECTOR_MISMATCH")
    async with cliente_http(superadmin) as c:
        assert (await c.post(RUTA, json=cuerpo(caso, pregunta=SOLO_ESTADO, sector="ENERGIA"))).status_code == 403
    async with cliente_http(sesion_ausente) as c:
        assert (await c.post(RUTA, json=cuerpo(caso, pregunta=SOLO_ESTADO, sector="ENERGIA"))).status_code == 401
    assert sin_llamadas_documentales(caso, pasos)


async def test_una_empresa_inactiva_no_se_consulta_tampoco_en_solo_estado(caso, fabrica_pg):
    from sqlalchemy import update

    from app.models import Empresa

    async with fabrica_pg() as db:
        await db.execute(update(Empresa).where(Empresa.id == caso.empresa.id).values(activa=False))
        await db.commit()

    r = await preguntar(caso, SOLO_ESTADO)

    assert (r.status_code, r.json()["error"]["code"]) == (400, "COMPANY_INACTIVE")
    assert caso.embeddings.llamadas == 0


@pytest.mark.parametrize(("caido", "esperado"), list(RECURSO_CAIDO.items()), ids=list(RECURSO_CAIDO))
@pytest.mark.parametrize("pregunta", [DOCUMENTAL, PREGUNTA_MIXTA], ids=["documental", "mixta"])
async def test_la_misma_caida_sigue_bloqueando_las_preguntas_documentales_y_mixtas_con_su_error_http_de_siempre(
    caso, monkeypatch, caido, esperado, pregunta
):
    await validado(caso)
    app, pasos = app_con_el_recurso_caido(caso, monkeypatch, caido)

    r = await preguntar_en(app, caso, pregunta)

    assert (r.status_code, r.json()["error"]["code"]) == esperado  # sin respuesta parcial y sin convertir el error en un 200
    assert "Estado registrado" not in r.text and "estado_registrado" not in r.text
    assert (caso.embeddings.llamadas, caso.llm.llamadas, caso.busquedas) == (0, 0, [])


@pytest.mark.parametrize(
    "pregunta",
    [
        "¿Hay una brecha en GRI 1?",  # sobre brechas: sigue el flujo documental
        "¿Qué estado tienen GRI 1 y GRI 13?",  # varios códigos
        "¿Qué estado tiene GRI 305-1?",  # una revelación
        "¿Qué estado tiene GRI 1 y qué dice el documento?",  # mixta
        "¿Qué estado tiene GRI 1 según el reporte?",  # algo más que el estado
        "¿Y qué estado tiene ese?",  # necesita contexto: sin código no hay forma de leer el estado
    ],
)
async def test_no_se_amplia_la_clasificacion_estas_preguntas_conservan_el_flujo_documental(caso, pregunta):
    await validado(caso)
    caso.devolver = sin_gri_1(caso)[:4]

    d = (await preguntar(caso, pregunta)).json()

    assert caso.embeddings.llamadas == 1 and len(caso.busquedas) == 1 and caso.llm.llamadas == 1  # la búsqueda y el modelo SÍ corren
    assert d["busqueda_documental"] == "REALIZADA" and d["sin_evidencia"] is True  # su resultado sigue siendo el documental
    if "GRI" in pregunta:
        assert d["estado_registrado"] is not None and d["respuesta"] == ""  # el estado acompaña, no sustituye
    else:
        assert d["estado_registrado"] is None  # «¿Y qué estado tiene ese?»: sin código no se lee ningún estado


@pytest.mark.parametrize(
    ("falla", "codigo"), [("embeddings", "EMBEDDING_PROVIDER_ERROR"), ("modelo", "LLM_PROVIDER_ERROR")], ids=["embeddings", "modelo"]
)
async def test_un_fallo_del_proveedor_en_una_pregunta_mixta_conserva_su_error_y_no_devuelve_respuesta_parcial(caso, falla, codigo):
    await validado(caso)
    caso.devolver = sin_gri_1(caso)[:4]
    if falla == "embeddings":
        async def error(textos):
            raise ExternalServiceError(codigo, "El proveedor de embeddings devolvió un error.")

        caso.embeddings.generar_embeddings_consulta = error
    else:
        caso.llm = LlmControlado(ExternalServiceError(codigo, "El proveedor de chat falló."))

    r = await preguntar(caso, PREGUNTA_MIXTA)

    assert (r.status_code, r.json()["error"]["code"]) == (502, codigo)  # no se convierte en un 200 con solo el estado
    assert "Estado registrado" not in r.text


async def test_solo_estado_no_depende_de_que_los_proveedores_funcionen(caso):
    """El mismo fallo que rompe una pregunta mixta no alcanza a una de solo estado: nunca se llama al proveedor."""
    await validado(caso)

    async def error(textos):
        raise ExternalServiceError("EMBEDDING_PROVIDER_ERROR", "El proveedor de embeddings devolvió un error.")

    caso.embeddings.generar_embeddings_consulta = error
    caso.llm = LlmControlado(ExternalServiceError("LLM_PROVIDER_ERROR", "El proveedor de chat falló."))

    r = await preguntar(caso, SOLO_ESTADO)

    assert r.status_code == 200 and r.json()["busqueda_documental"] == "NO_REALIZADA" and caso.llm.llamadas == 0


async def test_si_el_analisis_esta_cambiando_una_pregunta_de_solo_estado_propaga_el_409_sin_otra_fuente(caso, monkeypatch):
    async def cambiando(*args, **kwargs):
        raise ConflictError("ANALYSIS_CHANGING", "El análisis está cambiando; vuelva a consultarlo.")

    monkeypatch.setattr(servicio, "consultar_analisis", cambiando)

    r = await preguntar(caso, SOLO_ESTADO)

    assert (r.status_code, r.json()["error"]["code"]) == (409, "ANALYSIS_CHANGING")  # no hay documentos que mostrar en su lugar


async def test_el_estado_de_solo_estado_nunca_es_una_cita_ni_una_conclusion_sobre_una_brecha(caso):
    await validado(caso)

    d = (await preguntar(caso, SOLO_ESTADO)).json()

    e = d["estado_registrado"]
    assert e["fuente"] == "ANALISIS_GRI" and e["codigos_solicitados"] == ["GRI 1"] and d["citas"] == []
    assert [(f["identidad"], f["estado"], f["estado_etiqueta"]) for f in e["filas"]] == [("1:2021", "BAJA_SUSTANCIA", "Baja sustancia")]
    assert (e["validacion_vigente"], e["validacion"]["numero"], e["validacion"]["motivos_no_vigente"]) == (True, 1, [])
    for conclusion in ("hay una brecha", "no hay brecha", "existe una brecha", "sin brecha", "cumple"):
        assert conclusion not in d["respuesta"].casefold()


async def test_una_pregunta_que_necesita_contexto_no_es_solo_estado_y_la_consulta_directa_no_tiene_como_resolverla(caso):
    """«¿Y qué estado tiene ese?» no nombra ningún código. `POST /asistente/consultas` no interpreta ni conoce el historial: el texto
    se usa literalmente, no se identifica ningún código y no se lee el estado (en la continuación sí hay interpretación: ver seguimiento)."""
    seguimiento = "¿Y qué estado tiene ese?"
    await validado(caso)
    caso.devolver = []

    d = (await preguntar(caso, seguimiento)).json()

    assert solo_estado(seguimiento, 2025) is None and codigos_para_estado(seguimiento) == []
    assert caso.embeddings.llamadas == 1  # la pregunta literal se envió al RAG tal cual
    assert d["motivo"] == "SIN_FRAGMENTOS" and d["estado_registrado"] is None and d["mensaje"] is not None and "Estado registrado" not in d["mensaje"]

"""Tests del Asistente IA — RAG (Módulo 6): dominio (prompt/citas) y servicio con dobles."""
from __future__ import annotations

import uuid

import pytest

from app.modules.asistente.domain.citas import extraer_referencias, procesar_citas
from app.modules.asistente.domain.prompt import construir_contexto, construir_prompt_usuario, validar_pregunta
from app.modules.asistente.infrastructure.llm import DeepSeekLlm, ProveedorLlm
from app.modules.asistente.schemas import ConsultaResponse
from app.modules.asistente.service import responder_consulta
from app.modules.ingesta.domain.embeddings import IdentidadEmbeddings
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado

IDENTIDAD = IdentidadEmbeddings(proveedor="oci", modelo="cohere.embed-v4.0", dimension=1536)


def frag(texto: str, *, indice: int = 0, anio: int = 2025, seccion=("3.6", "Personal")) -> FragmentoRecuperado:
    return FragmentoRecuperado(
        documento_id=uuid.uuid4(),
        indice=indice,
        texto_literal=texto,
        contexto="ctx",
        inicio=0,
        fin=len(texto),
        continuacion=False,
        ruta_encabezados=seccion,
        empresa_id=uuid.uuid4(),
        anio=anio,
        tipo="MEMORIA_ANUAL",
        sector="MINERIA",
        embedding_proveedor="oci",
        embedding_modelo="cohere.embed-v4.0",
        similitud=0.9,
    )


# --- Dominio: prompt ---------------------------------------------------------------------------

def test_contexto_numera_los_fragmentos():
    c = construir_contexto([frag("hola", seccion=("A",)), frag("mundo", seccion=("B",))])
    assert "[1]" in c and "[2]" in c and "hola" in c and "mundo" in c


def test_prompt_valida_pregunta_vacia():
    with pytest.raises(ValueError):
        validar_pregunta("   ")


def test_prompt_incluye_contexto_y_pregunta():
    p = construir_prompt_usuario("¿agua?", [frag("consumo de agua")])
    assert "consumo de agua" in p and "¿agua?" in p


# --- Dominio: citas (RN-034) -------------------------------------------------------------------

def test_extraer_referencias():
    assert extraer_referencias("x [1] y [2][3] z [1]") == {1, 2, 3}


def test_procesar_citas_conserva_las_validas_y_quita_las_invalidas():
    fs = [frag("uno"), frag("dos")]
    texto = "Afirmo [1] y [9] (fuera de rango) y [2]."
    limpio, citados = procesar_citas(texto, fs)
    assert "[9]" not in limpio
    assert "[1]" in limpio and "[2]" in limpio
    assert citados == [fs[0], fs[1]]


# --- Servicio: flujo RAG con dobles ------------------------------------------------------------

class EmbeddingsFake:
    identidad = IDENTIDAD

    async def generar_embeddings_consulta(self, textos):
        return [[0.0] * 1536 for _ in textos]


class LlmFake:
    def __init__(self, respuesta: str):
        self.respuesta = respuesta
        self.llamado = False

    async def completar(self, sistema: str, usuario: str) -> str:
        self.llamado = True
        return self.respuesta


class LlmNunca:
    async def completar(self, sistema: str, usuario: str) -> str:  # pragma: no cover
        raise AssertionError("no debe llamar al LLM sin evidencia")


async def _sin_empresa(_db, _empresa_id):
    return [uuid.uuid4()]


def _parchear(monkeypatch, fragmentos, recuperables, *, llm):
    import app.modules.asistente.service as svc

    async def _buscar(*a, **k):
        return fragmentos

    async def _recuperables(_db, _ambiente, ids):
        return recuperables

    async def _empresas(_db, _emp):
        return [uuid.uuid4()]

    monkeypatch.setattr(svc, "buscar_similares", _buscar)
    monkeypatch.setattr(svc, "documentos_recuperables", _recuperables)
    monkeypatch.setattr(svc, "_empresa_ids", _empresas)
    return llm


async def test_sin_evidencia_no_llama_al_llm(monkeypatch):
    llm = _parchear(monkeypatch, [], set(), llm=LlmNunca())
    r = await responder_consulta(
        None, None, pregunta="¿x?", ambiente="development",
        proveedor_embeddings=EmbeddingsFake(), proveedor_llm=llm,
    )
    assert r.sin_evidencia is True and r.citas == [] and "No tengo evidencia" in r.mensaje


async def test_responde_con_citas_verificadas(monkeypatch):
    f1, f2 = frag("texto uno", seccion=("A",)), frag("texto dos", seccion=("B",))
    llm = _parchear(monkeypatch, [f1, f2], {f1.documento_id, f2.documento_id}, llm=LlmFake("Según [1] y [2]."))
    r = await responder_consulta(
        None, None, pregunta="¿x?", ambiente="development",
        proveedor_embeddings=EmbeddingsFake(), proveedor_llm=llm,
    )
    assert r.sin_evidencia is False
    assert len(r.citas) == 2
    assert r.citas[0].documento_id == f1.documento_id


async def test_fragmentos_no_recuperables_no_se_citan(monkeypatch):
    f1, f2 = frag("uno"), frag("dos")
    llm = _parchear(monkeypatch, [f1, f2], {f1.documento_id}, llm=LlmFake("Dice [1] y [2] (inexistente)."))
    r = await responder_consulta(
        None, None, pregunta="¿x?", ambiente="development",
        proveedor_embeddings=EmbeddingsFake(), proveedor_llm=llm,
    )
    # f2 no recuperable -> queda fuera del contexto, [2] se retira
    assert "[2]" not in r.respuesta
    assert len(r.citas) == 1


# --- Cliente LLM (DeepSeek) --------------------------------------------------------------------

def test_proveedor_llm_error_de_red(monkeypatch):
    import httpx

    llm = DeepSeekLlm(api_key="k", base_url="http://localhost:0", modelo="m", timeout_segundos=0.001)

    async def _no_red(*a, **k):
        raise httpx.ConnectError("no")

    class C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return await _no_red()

    monkeypatch.setattr("app.modules.asistente.infrastructure.llm.httpx.AsyncClient", lambda **k: C())
    with pytest.raises(Exception) as e:
        import anyio
        anyio.run(llm.completar, "s", "u")
    assert getattr(e.value, "code", "") in ("LLM_PROVIDER_ERROR",)

"""Adaptador de embeddings de OCI Generative AI (`proveedor_oci`) frente al contrato de
`embeddings.py`.

Nivel: SDK de OCI SUSTITUIDO por dobles. No se llama a Oracle, no se leen credenciales ni el
archivo de configuración de OCI y no se consumen créditos: estas pruebas NO demuestran que el
servicio real, el modelo, la dimensión ni los permisos funcionen (eso lo cubre el smoke manual
`scripts/smoke_embeddings_oci.py`, que NO se ejecutó en esta integración).
"""
import threading
from types import SimpleNamespace

import pytest

from app.exceptions import ExternalServiceError
from app.services.ingesta import proveedor_oci
from app.services.ingesta.embeddings import IdentidadEmbeddings, embeber_fragmentos
from app.services.ingesta.fragmentacion import ParametrosFragmentacion, iterar_fragmentos

SECRETO = "SECRETO-OCI-FICTICIO"


def ajustes(**cambios):
    base = dict(
        OCI_REGION="us-chicago-1", OCI_COMPARTMENT_ID="ocid1.compartment.oc1..ficticio",
        OCI_EMBED_MODEL="cohere.embed-multilingual-v3.0", OCI_EMBED_DIMENSIONS=3,
        OCI_CONFIG_FILE="~/.oci/config-ficticio", OCI_CONFIG_PROFILE="perfil-ficticio",
    )
    base.update(cambios)
    return SimpleNamespace(**base)


class ClienteFalso:
    def __init__(self, respuesta=None, error=None):
        self.peticiones = []
        self.hilos = []
        self.respuesta, self.error = respuesta, error

    def embed_text(self, detalles):
        self.hilos.append(threading.get_ident())
        self.peticiones.append(detalles)
        if self.error is not None:
            raise self.error
        vectores = self.respuesta if self.respuesta is not None else [[float(i), 0.5, 0.25] for i, _ in enumerate(detalles.inputs)]
        return SimpleNamespace(data=SimpleNamespace(embeddings=vectores))


@pytest.fixture
def oci_falso(monkeypatch):
    estado = SimpleNamespace(cliente=ClienteFalso(), config_leida=None, construido=None)

    def desde_archivo(archivo, perfil):
        estado.config_leida = (archivo, perfil)
        return {"region": "ficticia"}

    def construir(config, service_endpoint):
        estado.construido = (config, service_endpoint)
        return estado.cliente

    monkeypatch.setattr(proveedor_oci, "settings", ajustes())
    monkeypatch.setattr(proveedor_oci.oci.config, "from_file", desde_archivo)
    monkeypatch.setattr(proveedor_oci, "GenerativeAiInferenceClient", construir)
    return estado


def test_sin_compartimento_no_se_construye_ni_se_lee_la_configuracion(oci_falso, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(OCI_COMPARTMENT_ID=None))
    with pytest.raises(ValueError, match="OCI_COMPARTMENT_ID"):
        proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.config_leida is None and oci_falso.construido is None


def test_identidad_endpoint_y_perfil_salen_de_la_configuracion(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    assert proveedor.identidad == IdentidadEmbeddings("oci-cohere", "cohere.embed-multilingual-v3.0", 3)
    archivo, perfil = oci_falso.config_leida
    assert archivo.endswith("config-ficticio") and "~" not in archivo and perfil == "perfil-ficticio"
    assert oci_falso.construido[1] == "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"


async def test_la_peticion_conserva_orden_modelo_compartimento_y_tipo_de_entrada(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    vectores = await proveedor.generar_embeddings(["uno", "dos", "tres"])
    [detalles] = oci_falso.cliente.peticiones
    assert detalles.inputs == ["uno", "dos", "tres"] and detalles.input_type == "SEARCH_DOCUMENT"
    assert detalles.compartment_id == "ocid1.compartment.oc1..ficticio"
    assert detalles.serving_mode.model_id == "cohere.embed-multilingual-v3.0"
    assert [v[0] for v in vectores] == [0.0, 1.0, 2.0]


async def test_la_llamada_sincrona_del_sdk_no_corre_en_el_hilo_del_bucle(oci_falso):
    await proveedor_oci.ProveedorEmbeddingsOCI().generar_embeddings(["x"])
    assert oci_falso.cliente.hilos and oci_falso.cliente.hilos[0] != threading.get_ident()


async def test_funciona_a_traves_del_contrato_de_embeddings_con_fragmentos_reales(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    texto = "# Memoria\n\nIntroducción.\n\n## Agua\n\nConsumo de agua.\n\n## Energía\n\nConsumo energético.\n"
    parametros = ParametrosFragmentacion(max_caracteres=40, max_caracteres_contexto=80)
    lotes = [
        lote async for lote in embeber_fragmentos(
            iterar_fragmentos(texto, parametros), proveedor, tamano_lote=2, timeout_segundos=5
        )
    ]
    elementos = [e for lote in lotes for e in lote.elementos]
    assert len(lotes) >= 2 and elementos
    assert all(e.vector and len(e.vector) == 3 and all(isinstance(c, float) for c in e.vector) for e in elementos)
    assert [e.fragmento.indice for e in elementos] == sorted(e.fragmento.indice for e in elementos)
    # Se envía `texto_embedding` (contexto + literal) en el mismo orden.
    enviados = [t for p in oci_falso.cliente.peticiones for t in p.inputs]
    assert enviados == [e.fragmento.texto_embedding for e in elementos]


async def test_un_error_del_sdk_se_convierte_en_error_controlado_sin_filtrar_el_mensaje(oci_falso):
    oci_falso.cliente.error = RuntimeError(f"401 con clave {SECRETO} y texto del documento")
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    with pytest.raises(ExternalServiceError) as capturado:
        async for _ in embeber_fragmentos(iterar_fragmentos("# T\n\nCuerpo.\n"), proveedor, tamano_lote=2, timeout_segundos=5):
            pass
    assert capturado.value.code == "EMBEDDING_PROVIDER_ERROR"
    assert SECRETO not in f"{capturado.value} {capturado.value.details} {capturado.value.message}"
    assert capturado.value.__cause__ is None


async def test_una_dimension_distinta_a_la_configurada_se_rechaza(oci_falso):
    oci_falso.cliente.respuesta = [[0.1, 0.2]]  # 2 componentes; la identidad declara 3
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    with pytest.raises(ExternalServiceError) as capturado:
        async for _ in embeber_fragmentos(iterar_fragmentos("# T\n\nCuerpo.\n"), proveedor, tamano_lote=1, timeout_segundos=5):
            pass
    assert capturado.value.code == "EMBEDDING_INVALID_RESPONSE"

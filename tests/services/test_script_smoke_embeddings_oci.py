"""Prueba manual `scripts/smoke_embeddings_oci.py`: solo su lógica, con el SDK de OCI
SUSTITUIDO por dobles. No hay llamadas a Oracle, credenciales ni consumo de créditos; el script
real lo ejecuta una persona con `--ejecutar`."""
import math
from types import SimpleNamespace

import pytest

from app.services.ingesta import proveedor_oci
from scripts import smoke_embeddings_oci as smoke
from tests.services.test_ingesta_proveedor_oci import ClienteFalso, ajustes, vector

SECRETO = "SECRETO-OCI-FICTICIO"


@pytest.fixture
def entorno(monkeypatch):
    estado = SimpleNamespace(cliente=ClienteFalso(), cierres=0)
    estado.cliente.base_client.session.close = lambda: setattr(estado, "cierres", estado.cierres + 1)
    monkeypatch.setattr(proveedor_oci, "settings", ajustes())
    monkeypatch.setattr(proveedor_oci.oci.config, "from_file", lambda archivo, perfil: {})
    monkeypatch.setattr(proveedor_oci, "GenerativeAiInferenceClient", lambda **kw: estado.cliente)
    return estado


def ejecutar(argv=("--ejecutar",), crear=None):
    salida: list[str] = []
    codigo = smoke.main(list(argv), crear_proveedor=crear or proveedor_oci.ProveedorEmbeddingsOCI, imprimir=salida.append)
    return codigo, "\n".join(salida)


def test_sin_ejecutar_no_hace_nada_ni_crea_el_proveedor():
    creados = []
    codigo, texto = ejecutar([], crear=lambda: creados.append(1))
    assert codigo == smoke.EXIT_NO_EJECUTADO and creados == [] and "NO ejecutada" in texto


def test_todo_correcto_devuelve_0_y_muestra_solo_resumenes(entorno):
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_OK
    assert "cohere.embed-v4.0" in texto and "dimensión solicitada=1536" in texto
    assert "dimensiones recibidas=[1536]" in texto and "OK:" in texto
    assert "0.25" not in texto and "[0." not in texto  # nunca vectores
    peticiones = entorno.cliente.peticiones
    assert len(peticiones) >= 2
    assert all(p.output_dimensions == 1536 and p.truncate == "NONE" for p in peticiones)
    assert [p.input_type for p in peticiones][:-1] == ["SEARCH_DOCUMENT"] * (len(peticiones) - 1)
    assert peticiones[-1].input_type == "SEARCH_QUERY"
    textos = [t for p in peticiones[:-1] for t in p.inputs]
    assert 1 < len(textos) <= 12 and all("ficticia" in t or "inventado" in t or t for t in textos)
    assert entorno.cierres == 1  # recursos cerrados


@pytest.mark.parametrize("respuesta", [
    [vector(1024)], [vector(1537)], [vector(valor=math.nan)], [vector(valor=math.inf)], [],
])
def test_una_respuesta_invalida_devuelve_distinto_de_0(entorno, respuesta):
    entorno.cliente.respuesta = respuesta
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_FALLO and "FALLO" in texto and "OK:" not in texto
    assert entorno.cierres == 1


def test_una_identidad_distinta_de_embed_v4_1536_no_hace_llamadas_remotas(entorno, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(OCI_EMBED_MODEL="cohere.embed-multilingual-v3.0", OCI_EMBED_DIMENSIONS=1024))
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_FALLO and "la identidad debe ser cohere.embed-v4.0 con 1536" in texto
    assert entorno.cliente.peticiones == [] and entorno.cierres == 1


def test_si_la_peticion_real_no_lleva_output_dimensions_el_script_falla(entorno, monkeypatch):
    original = proveedor_oci.ProveedorEmbeddingsOCI._detalles

    def sin_dimension(self, textos, input_type):
        detalles = original(self, textos, input_type)
        detalles.output_dimensions = None  # la identidad dice 1536, pero la petición no lo envía
        return detalles

    monkeypatch.setattr(proveedor_oci.ProveedorEmbeddingsOCI, "_detalles", sin_dimension)
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_FALLO and "output_dimensions=1536" in texto


def test_si_la_peticion_real_no_lleva_truncate_none_el_script_falla(entorno, monkeypatch):
    original = proveedor_oci.ProveedorEmbeddingsOCI._detalles

    def con_truncate_end(self, textos, input_type):
        detalles = original(self, textos, input_type)
        detalles.truncate = "END"
        return detalles

    monkeypatch.setattr(proveedor_oci.ProveedorEmbeddingsOCI, "_detalles", con_truncate_end)
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_FALLO and 'truncate="NONE"' in texto


def test_un_error_remoto_se_informa_sin_secretos_ni_textos(entorno):
    entorno.cliente.error = RuntimeError(f"401 clave {SECRETO} con texto del documento")
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_FALLO and SECRETO not in texto and "documento" not in texto
    assert "EMBEDDING_PROVIDER_ERROR" in texto and entorno.cierres == 1


def test_configuracion_invalida_devuelve_2_sin_filtrar_el_mensaje(entorno):
    def crear():
        raise OSError(f"no se pudo leer la llave {SECRETO}")

    codigo, texto = ejecutar(crear=crear)
    assert codigo == smoke.EXIT_NO_EJECUTADO and SECRETO not in texto and "OSError" in texto
    assert entorno.cliente.peticiones == []


def test_un_error_al_cerrar_no_cambia_el_resultado_ni_filtra(entorno):
    def falla():
        raise RuntimeError(SECRETO)

    entorno.cliente.base_client.session.close = falla
    codigo, texto = ejecutar()
    assert codigo == smoke.EXIT_OK and SECRETO not in texto and "No se pudo cerrar el cliente de OCI" in texto

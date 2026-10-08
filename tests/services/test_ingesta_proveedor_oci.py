"""Adaptador de embeddings de OCI Generative AI (Cohere Embed 4, 1536) frente al contrato de
`embeddings.py`.

Nivel: SDK de OCI SUSTITUIDO por dobles. No se llama a Oracle, no se leen credenciales ni el
archivo de configuración de OCI y no se consumen créditos: estas pruebas NO demuestran que el
servicio real, el modelo, la dimensión de 1536 ni los permisos funcionen (eso lo comprueba la
prueba manual `scripts/smoke_embeddings_oci.py`, que ejecuta una persona).
"""
import math
import threading
from types import SimpleNamespace

import oci
import pytest

from app.exceptions import ExternalServiceError, ExternalServiceTimeoutError
from app.services.ingesta import proveedor_oci
from app.services.ingesta.embeddings import (
    IdentidadEmbeddings,
    embeber_consulta,
    embeber_fragmentos,
)
from app.services.ingesta.fragmentacion import ParametrosFragmentacion, iterar_fragmentos

SECRETO = "SECRETO-OCI-FICTICIO"
TEXTO_DOCUMENTAL = "TEXTO-DOCUMENTAL-CONFIDENCIAL"
DIM = 1536


def ajustes(**cambios):
    base = dict(
        OCI_REGION="us-chicago-1", OCI_COMPARTMENT_ID="ocid1.compartment.oc1..ficticio",
        OCI_EMBED_MODEL="cohere.embed-v4.0", OCI_EMBED_DIMENSIONS=DIM,
        OCI_CONFIG_FILE="~/.oci/config-ficticio", OCI_CONFIG_PROFILE="perfil-ficticio",
    )
    base.update(cambios)
    return SimpleNamespace(**base)


def vector(n: int = DIM, valor: float = 0.25) -> list[float]:
    return [valor] * n


class ClienteFalso:
    def __init__(self, respuesta=None, error=None):
        self.peticiones, self.hilos = [], []
        self.respuesta, self.error = respuesta, error
        self.compuerta: threading.Event | None = None
        self.base_client = SimpleNamespace(session=SimpleNamespace(cerrada=0, close=lambda: None))

    def embed_text(self, detalles):
        self.hilos.append(threading.get_ident())
        self.peticiones.append(detalles)
        if self.compuerta is not None:
            self.compuerta.wait(10)
        if self.error is not None:
            raise self.error
        vectores = self.respuesta if self.respuesta is not None else [vector(valor=float(i)) for i, _ in enumerate(detalles.inputs)]
        return SimpleNamespace(data=SimpleNamespace(embeddings=vectores))


@pytest.fixture
def oci_falso(monkeypatch):
    estado = SimpleNamespace(cliente=ClienteFalso(), config_leida=None, argumentos=None)

    def desde_archivo(archivo, perfil):
        estado.config_leida = (archivo, perfil)
        return {"region": "ficticia"}

    def construir(**kwargs):
        estado.argumentos = kwargs
        return estado.cliente

    monkeypatch.setattr(proveedor_oci, "settings", ajustes())
    monkeypatch.setattr(proveedor_oci.oci.config, "from_file", desde_archivo)
    monkeypatch.setattr(proveedor_oci, "GenerativeAiInferenceClient", construir)
    return estado


async def consumir(proveedor, texto="# T\n\nCuerpo.\n", tamano_lote=2):
    return [lote async for lote in embeber_fragmentos(
        iterar_fragmentos(texto), proveedor, tamano_lote=tamano_lote, timeout_segundos=5)]


# --- Construcción y configuración ---------------------------------------------------------------------

def test_sin_compartimento_no_se_construye_ni_se_lee_la_configuracion(oci_falso, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(OCI_COMPARTMENT_ID=None))
    with pytest.raises(ValueError, match="OCI_COMPARTMENT_ID"):
        proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.config_leida is None and oci_falso.argumentos is None


@pytest.mark.parametrize("cambios, mensaje", [
    (dict(OCI_EMBED_DIMENSIONS=1000), "OCI_EMBED_DIMENSIONS"),
    (dict(OCI_EMBED_DIMENSIONS=1024.0 + 1), "OCI_EMBED_DIMENSIONS"),
])
def test_configuracion_invalida_se_rechaza_antes_de_construir_el_cliente(oci_falso, monkeypatch, cambios, mensaje):
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(**cambios))
    with pytest.raises(ValueError, match=mensaje):
        proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.argumentos is None


def test_los_plazos_del_sdk_salen_de_los_parametros_de_codigo_y_se_pueden_sustituir(oci_falso):
    from app.services.ingesta.parametros import ParametrosIngesta

    proveedor_oci.ProveedorEmbeddingsOCI(ParametrosIngesta(oci_connect_timeout_segundos=3, oci_read_timeout_segundos=4.5))
    assert oci_falso.argumentos["timeout"] == (3.0, 4.5)
    proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.argumentos["timeout"] == (10.0, 60.0)


def test_el_proveedor_ya_no_lee_plazos_del_entorno(oci_falso, monkeypatch):
    # Aunque el entorno (o los ajustes) trajeran valores, los plazos del SDK son los de código.
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(OCI_CONNECT_TIMEOUT_SECONDS="99", OCI_READ_TIMEOUT_SECONDS="abc"))
    proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.argumentos["timeout"] == (10.0, 60.0)


def test_identidad_endpoint_plazos_y_sin_reintentos(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    assert proveedor.identidad == IdentidadEmbeddings("oci-cohere", "cohere.embed-v4.0", DIM)
    archivo, perfil = oci_falso.config_leida
    assert archivo.endswith("config-ficticio") and "~" not in archivo and perfil == "perfil-ficticio"
    argumentos = oci_falso.argumentos
    assert argumentos["service_endpoint"] == "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"
    assert argumentos["timeout"] == (10.0, 60.0)  # conexión y lectura explícitas
    assert isinstance(argumentos["retry_strategy"], oci.retry.NoneRetryStrategy)  # política explícita: ninguna


# --- Petición efectiva -------------------------------------------------------------------------------------

async def test_la_peticion_envia_modelo_v4_dimension_tipo_y_truncate_none(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    vectores = await proveedor.generar_embeddings(["uno", "dos", "tres"])
    [detalles] = oci_falso.cliente.peticiones
    assert detalles.serving_mode.model_id == "cohere.embed-v4.0"
    assert detalles.output_dimensions == 1536  # ENVIADA en la petición, no solo declarada
    assert detalles.truncate == "NONE" and detalles.input_type == "SEARCH_DOCUMENT"
    assert detalles.inputs == ["uno", "dos", "tres"]
    assert detalles.compartment_id == "ocid1.compartment.oc1..ficticio"
    assert [v[0] for v in vectores] == [0.0, 1.0, 2.0]  # orden y cantidad


async def test_la_consulta_usa_search_query_con_la_misma_dimension_y_truncate(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    consulta = await embeber_consulta("¿Cuánta agua se consumió?", proveedor, timeout_segundos=5)
    [detalles] = oci_falso.cliente.peticiones
    assert detalles.input_type == "SEARCH_QUERY" and detalles.output_dimensions == 1536
    assert detalles.truncate == "NONE" and len(consulta) == DIM


async def test_la_dimension_configurada_distinta_se_envia_y_se_exige(oci_falso, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(OCI_EMBED_DIMENSIONS=1024))
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    oci_falso.cliente.respuesta = [vector(1024)]
    lotes = await consumir(proveedor)
    assert oci_falso.cliente.peticiones[0].output_dimensions == 1024
    assert len(lotes[0].elementos[0].vector) == 1024


async def test_la_llamada_sincrona_del_sdk_no_corre_en_el_hilo_del_bucle(oci_falso):
    await proveedor_oci.ProveedorEmbeddingsOCI().generar_embeddings(["x"])
    assert oci_falso.cliente.hilos and oci_falso.cliente.hilos[0] != threading.get_ident()


# --- Contrato: aceptación y rechazo de respuestas --------------------------------------------------------------

async def test_acepta_vectores_numericos_finitos_de_1536_a_traves_del_contrato(oci_falso):
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    texto = "# Memoria\n\nIntroducción.\n\n## Agua\n\nConsumo de agua.\n\n## Energía\n\nConsumo energético.\n"
    parametros = ParametrosFragmentacion(max_caracteres=40, max_caracteres_contexto=80)
    lotes = [lote async for lote in embeber_fragmentos(
        iterar_fragmentos(texto, parametros), proveedor, tamano_lote=2, timeout_segundos=5)]
    elementos = [e for lote in lotes for e in lote.elementos]
    assert len(lotes) >= 2 and elementos
    assert all(len(e.vector) == DIM and all(math.isfinite(c) for c in e.vector) for e in elementos)
    assert [e.fragmento.indice for e in elementos] == sorted(e.fragmento.indice for e in elementos)
    enviados = [t for p in oci_falso.cliente.peticiones for t in p.inputs]
    assert enviados == [e.fragmento.texto_embedding for e in elementos]  # contexto + literal, en orden


@pytest.mark.parametrize("respuesta", [
    [vector(1024)],                      # dimensión distinta
    [vector(1537)],                      # una componente de más
    [vector(1535)],                      # una de menos
    [vector(), vector()],                # más vectores que textos
    [],                                  # ninguno
    [vector(valor=math.nan)],            # NaN
    [vector(valor=math.inf)],            # infinito
    [vector(valor=-math.inf)],
    [[True] * DIM],                      # booleanos no son números
    [["0.5"] * DIM],                     # texto
])
async def test_rechaza_respuestas_invalidas_con_error_controlado(oci_falso, respuesta):
    oci_falso.cliente.respuesta = respuesta
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    with pytest.raises(ExternalServiceError) as capturado:
        await consumir(proveedor, texto="# T\n\nUn único fragmento.\n", tamano_lote=1)
    assert capturado.value.code == "EMBEDDING_INVALID_RESPONSE"
    assert TEXTO_DOCUMENTAL not in f"{capturado.value} {capturado.value.details}"


# --- Errores y timeout controlados ------------------------------------------------------------------------------

async def test_un_error_del_sdk_no_filtra_secretos_ni_texto(oci_falso):
    oci_falso.cliente.error = RuntimeError(f"401 con clave {SECRETO} y {TEXTO_DOCUMENTAL}")
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    with pytest.raises(ExternalServiceError) as capturado:
        await consumir(proveedor, texto=f"# T\n\n{TEXTO_DOCUMENTAL}\n")
    assert capturado.value.code == "EMBEDDING_PROVIDER_ERROR"
    visible = f"{capturado.value} {capturado.value.details} {capturado.value.message}"
    assert SECRETO not in visible and TEXTO_DOCUMENTAL not in visible and capturado.value.__cause__ is None


async def test_un_timeout_es_un_error_controlado_aunque_el_hilo_siga(oci_falso):
    oci_falso.cliente.compuerta = threading.Event()
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    try:
        with pytest.raises(ExternalServiceTimeoutError) as capturado:
            async for _ in embeber_fragmentos(
                iterar_fragmentos(f"# T\n\n{TEXTO_DOCUMENTAL}\n"), proveedor, tamano_lote=1, timeout_segundos=0.05
            ):
                pass
        assert capturado.value.code == "EMBEDDING_PROVIDER_TIMEOUT"
        assert TEXTO_DOCUMENTAL not in f"{capturado.value} {capturado.value.details}"
        # Cancelar la espera NO detuvo la petición: el hilo del SDK sigue bloqueado dentro de `embed_text`.
        assert len(oci_falso.cliente.peticiones) == 1
    finally:
        oci_falso.cliente.compuerta.set()


# --- Recursos ---------------------------------------------------------------------------------------------------------

def test_cerrar_libera_la_sesion_y_tolera_clientes_sin_ella(oci_falso):
    cierres = []
    oci_falso.cliente.base_client.session.close = lambda: cierres.append(1)
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    proveedor.cerrar()
    proveedor.cerrar()
    assert cierres == [1, 1]  # idempotente: delega en el cierre de la sesión
    proveedor._client = SimpleNamespace()  # un cliente sin sesión no falla
    proveedor.cerrar()


# --- Identidad OCI por variables de entorno (contenedores sin ~/.oci/config) ------------------------------------

def _llave_pem_ficticia() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return llave.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode("ascii")


def identidad_por_variables(pem: str | None = None, **cambios):
    import base64

    base = dict(
        OCI_USER_OCID="ocid1.user.oc1..ficticio", OCI_TENANCY_OCID="ocid1.tenancy.oc1..ficticio",
        OCI_FINGERPRINT=":".join(["aa"] * 16),
        OCI_KEY_PEM_B64=base64.b64encode((pem or _llave_pem_ficticia()).encode("ascii")).decode("ascii"),
        OCI_CONFIG_FILE="~/.oci/no-existe-en-el-contenedor",
    )
    base.update(cambios)
    return ajustes(**base)


def test_sin_archivo_la_identidad_se_arma_en_memoria_con_las_variables_y_el_archivo_no_se_lee(oci_falso, monkeypatch):
    pem = _llave_pem_ficticia()
    monkeypatch.setattr(proveedor_oci, "settings", identidad_por_variables(pem))
    proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.config_leida is None  # no se consultó ningún archivo
    config = oci_falso.argumentos["config"]
    assert config["key_content"] == pem and config["region"] == "us-chicago-1"
    assert (config["user"], config["tenancy"]) == ("ocid1.user.oc1..ficticio", "ocid1.tenancy.oc1..ficticio")
    assert config["fingerprint"] == ":".join(["aa"] * 16) and "key_file" not in config


def test_con_variables_la_llave_no_se_escribe_en_disco_ni_en_el_repr(oci_falso, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(proveedor_oci, "settings", identidad_por_variables())
    proveedor = proveedor_oci.ProveedorEmbeddingsOCI()
    assert list(tmp_path.iterdir()) == []
    assert "PRIVATE KEY" not in repr(proveedor) and "PRIVATE KEY" not in repr(proveedor.identidad)


def test_si_el_archivo_existe_manda_el_archivo(oci_falso, monkeypatch, tmp_path):
    archivo = tmp_path / "config"
    archivo.write_text("[perfil-ficticio]\n", encoding="utf-8")
    monkeypatch.setattr(proveedor_oci, "settings", identidad_por_variables(OCI_CONFIG_FILE=str(archivo)))
    proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.config_leida == (str(archivo), "perfil-ficticio")
    assert oci_falso.argumentos["config"] == {"region": "ficticia"}  # lo que devolvió el archivo, no las variables


@pytest.mark.parametrize("faltante", ["OCI_USER_OCID", "OCI_FINGERPRINT", "OCI_TENANCY_OCID", "OCI_KEY_PEM_B64"])
def test_con_variables_incompletas_se_intenta_el_archivo_como_antes(oci_falso, monkeypatch, faltante):
    monkeypatch.setattr(proveedor_oci, "settings", identidad_por_variables(**{faltante: None}))
    proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.config_leida is not None and oci_falso.argumentos["config"] == {"region": "ficticia"}


@pytest.mark.parametrize("llave", ["esto no es base64!!", "####", "bm8tZXMtdW4tcGVtñ"])
def test_una_llave_invalida_falla_sin_repetir_su_valor(oci_falso, monkeypatch, llave):
    monkeypatch.setattr(proveedor_oci, "settings", identidad_por_variables(OCI_KEY_PEM_B64=llave))
    with pytest.raises(ValueError, match="OCI_KEY_PEM_B64") as error:
        proveedor_oci.ProveedorEmbeddingsOCI()
    assert llave not in str(error.value) and oci_falso.argumentos is None


def test_una_huella_o_un_ocid_con_formato_invalido_lo_rechaza_el_sdk(oci_falso, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "settings", identidad_por_variables(OCI_FINGERPRINT="no-es-una-huella"))
    with pytest.raises(oci.exceptions.InvalidConfig):
        proveedor_oci.ProveedorEmbeddingsOCI()
    assert oci_falso.argumentos is None

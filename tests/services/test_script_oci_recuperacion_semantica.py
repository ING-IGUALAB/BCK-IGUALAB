"""Prueba manual `scripts/pruebas_manuales/oci_recuperacion_semantica.py`: solo su lógica, con el
SDK de OCI SUSTITUIDO por un doble que asigna vectores por tema. No hay llamadas a Oracle ni
créditos; la ejecución real la hace una persona con `--ejecutar`."""
import pytest

from app.services.ingesta import proveedor_oci
from scripts.pruebas_manuales import oci_recuperacion_semantica as prueba
from tests.services.test_ingesta_proveedor_oci import ClienteFalso, ajustes, vector
from tests.services.test_script_smoke_embeddings_oci import entorno  # noqa: F401  (fixture)

SECRETO = "SECRETO-OCI-FICTICIO"


def tema(texto: str) -> int:
    t = texto.lower()
    if any(p in t for p in ("agua", "hídric", "residuales")):
        return 0
    if any(p in t for p in ("electric", "eléctric", "solares", "renovable")):
        return 1
    return 2


def base(indice: int, mezcla: float = 0.0, otro: int | None = None) -> list[float]:
    v = vector(1536, 0.001)
    v[indice] = 1.0
    if otro is not None:
        v[otro] = mezcla
    return v


class ClienteTemas(ClienteFalso):
    def __init__(self, modificar=None):
        super().__init__()
        self.modificar = modificar

    def embed_text(self, detalles):
        self.peticiones.append(detalles)
        vectores = [base(tema(t)) for t in detalles.inputs]
        if self.modificar:
            vectores = self.modificar(detalles, vectores)
        from types import SimpleNamespace as N
        return N(data=N(embeddings=vectores))


def preparar(entorno, cliente):
    entorno.cliente = cliente
    cliente.base_client.session.close = lambda: setattr(entorno, "cierres", entorno.cierres + 1)


def ejecutar(argv=("--ejecutar",), crear=None):
    salida = []
    codigo = prueba.main(list(argv), crear_proveedor=crear or proveedor_oci.ProveedorEmbeddingsOCI, imprimir=salida.append)
    return codigo, "\n".join(salida)


@pytest.fixture
def e(entorno, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "GenerativeAiInferenceClient", lambda **kw: entorno.cliente)
    return entorno


def test_sin_ejecutar_no_hace_nada():
    creados = []
    codigo, texto = ejecutar([], crear=lambda: creados.append(1))
    assert codigo == 2 and creados == [] and "NO ejecutada" in texto


def test_pasa_con_recuperacion_correcta_muestra_la_tabla_y_cuenta_las_llamadas(e):
    preparar(e, ClienteTemas())
    codigo, texto = ejecutar()
    assert codigo == 0 and "OK:" in texto and "llamadas a OCI=4" in texto
    assert "documentos=3 consultas=3" in texto and "dimensiones] recibidas=[1536]" in texto
    filas = [l for l in texto.splitlines() if l.startswith("P")]
    assert len(filas) == 3 and filas[0].split()[-2:] == ["agua", "agua"]
    assert "0.0010" not in texto.split("Similitud")[0]  # el resumen previo no imprime vectores
    tipos = [p.input_type for p in e.cliente.peticiones]
    assert tipos == ["SEARCH_DOCUMENT", "SEARCH_QUERY", "SEARCH_QUERY", "SEARCH_QUERY"]
    assert [len(p.inputs) for p in e.cliente.peticiones] == [3, 1, 1, 1]
    assert all(p.output_dimensions == 1536 and p.truncate == "NONE" for p in e.cliente.peticiones)
    assert e.cierres == 1


def test_una_pregunta_que_recupera_otro_documento_falla(e):
    def cruza(detalles, vectores):
        if detalles.input_type == "SEARCH_QUERY" and tema(detalles.inputs[0]) == 0:
            return [base(2)]  # la pregunta hídrica apunta a «personal»
        return vectores

    preparar(e, ClienteTemas(cruza))
    codigo, texto = ejecutar()
    assert codigo == 1 and "encontró primero «personal»" in texto and "OK:" not in texto
    assert e.cierres == 1


def test_un_empate_en_el_primer_puesto_falla(e):
    def empata(detalles, vectores):
        if detalles.input_type == "SEARCH_QUERY" and tema(detalles.inputs[0]) == 1:
            v = vector(1536, 0.001)
            v[0] = v[1] = 1.0  # equidistante entre «agua» y «energia»
            return [v]
        return vectores

    preparar(e, ClienteTemas(empata))
    codigo, texto = ejecutar()
    assert codigo == 1 and "empate" in texto


def test_documentos_iguales_norma_cero_o_dimension_incorrecta_fallan(e):
    casos = {
        "iguales": lambda d, v: [base(0)] * 3 if d.input_type == "SEARCH_DOCUMENT" else v,
        "norma cero": lambda d, v: [[0.0] * 1536] + v[1:] if d.input_type == "SEARCH_DOCUMENT" else v,
        "dimensión": lambda d, v: [vector(1024)] * len(v) if d.input_type == "SEARCH_QUERY" else v,
    }
    for nombre, modificar in casos.items():
        e.cierres = 0
        preparar(e, ClienteTemas(modificar))
        codigo, texto = ejecutar()
        assert codigo == 1 and "OK:" not in texto, nombre
        assert e.cierres == 1, nombre


def test_una_peticion_sin_la_configuracion_acordada_falla(e, monkeypatch):
    original = proveedor_oci.ProveedorEmbeddingsOCI._detalles

    def sin_truncate_none(self, textos, input_type):
        detalles = original(self, textos, input_type)
        detalles.truncate = "END"
        return detalles

    monkeypatch.setattr(proveedor_oci.ProveedorEmbeddingsOCI, "_detalles", sin_truncate_none)
    preparar(e, ClienteTemas())
    codigo, texto = ejecutar()
    assert codigo == 1 and "truncate=NONE" in texto


def test_una_identidad_distinta_no_hace_llamadas(e, monkeypatch):
    monkeypatch.setattr(proveedor_oci, "settings", ajustes(OCI_EMBED_DIMENSIONS=1024))
    preparar(e, ClienteTemas())
    codigo, texto = ejecutar()
    assert codigo == 1 and "la identidad debe ser" in texto and e.cliente.peticiones == []


def test_un_error_remoto_no_filtra_secretos_ni_textos(e):
    class Falla(ClienteTemas):
        def embed_text(self, detalles):
            raise RuntimeError(f"401 {SECRETO} {detalles.inputs[0]}")

    preparar(e, Falla())
    codigo, texto = ejecutar()
    assert codigo == 1 and SECRETO not in texto and "empresa" not in texto.lower() and e.cierres == 1


def test_configuracion_invalida_devuelve_2_sin_filtrar():
    def crear():
        raise OSError(SECRETO)

    codigo, texto = ejecutar(crear=crear)
    assert codigo == 2 and SECRETO not in texto and "OSError" in texto


def test_coseno_y_clasificacion():
    assert prueba.coseno([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert prueba.coseno([1.0, 0.0], [0.0, 3.0]) == pytest.approx(0.0)
    assert prueba.clasificar({"a": 0.2, "b": 0.5, "c": 0.1}) == ("b", False)
    assert prueba.clasificar({"a": 0.5, "b": 0.5, "c": 0.1})[1] is True

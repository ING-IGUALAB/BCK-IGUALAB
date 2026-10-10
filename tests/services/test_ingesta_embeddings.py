"""Etapa 3A de ingesta: contrato de embeddings y procesamiento incremental por lotes.

LTX:RF-016 (solo la parte de preparar lotes de embeddings), RNF-018 (timeout por
llamada), RNF-027 (interfaz de proveedor), T17 y T18 con proveedor SIMULADO.
Nivel: unidad. Los proveedores simulados viven solo aquí: el código de
producción no tiene ningún proveedor de reserva. No hay llamadas externas.

NO demuestra integración con un proveedor real, persistencia ni indexación.
"""
import asyncio
import gc
import inspect
import logging
import math
import weakref
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.exception_handlers import register_exception_handlers
from app.exceptions import (
    AppException,
    AuthenticationError,
    BusinessValidationError,
    ExternalServiceError,
    ExternalServiceTimeoutError,
)
from app.request_id import RequestIDMiddleware
from app.services.ingesta import embeddings, validacion
from app.services.ingesta.embeddings import (
    FragmentoEmbebido,
    IdentidadEmbeddings,
    LoteEmbebido,
    ProveedorEmbeddings,
    embeber_consulta,
    embeber_fragmentos,
    validar_respuesta,
)
from app.services.ingesta.fragmentacion import (
    Fragmento,
    ParametrosFragmentacion,
    fragmentar,
    iterar_fragmentos,
)

DIMENSION = 4
IDENTIDAD = IdentidadEmbeddings("proveedor-SIMULADO", "modelo-SIMULADO-de-prueba", DIMENSION)
SECRETO_DOCUMENTO = "TEXTO-CONFIDENCIAL-DEL-DOCUMENTO"
SECRETO_CLAVE = "sk-CLAVE-SECRETA-123"


def vector_de(texto: str) -> list[float]:
    """Embedding simulado: determinista y dependiente del texto COMPLETO recibido."""
    return [float(len(texto)), float(sum(map(ord, texto)) % 997), float(texto.count(" ")), 0.5]


def respuesta_simulada(textos):
    return [vector_de(t) for t in textos]


def proveedor_con(generar, identidad: IdentidadEmbeddings = IDENTIDAD):
    return SimpleNamespace(identidad=identidad, generar_embeddings=generar)


class ProveedorSimulado:
    """Proveedor simulado que registra los lotes recibidos."""

    identidad = IDENTIDAD

    def __init__(self):
        self.lotes: list[tuple[str, ...]] = []

    async def generar_embeddings(self, textos):
        self.lotes.append(tuple(textos))
        return respuesta_simulada(textos)


def fragmento(indice: int, literal: str | None = None, contexto: str = "Sección: S\n\n") -> Fragmento:
    literal = literal if literal is not None else f"contenido {indice:03d}"
    return Fragmento(
        indice=indice,
        inicio=indice * 100,
        fin=indice * 100 + len(literal),
        texto_literal=literal,
        ruta_encabezados=("S",),
        contexto=contexto,
        continuacion=False,
    )


def fragmentos(cantidad: int) -> list[Fragmento]:
    return [fragmento(i) for i in range(cantidad)]


async def recolectar(iterador) -> list[LoteEmbebido]:
    return [lote async for lote in iterador]


async def fallo_de(iterador) -> AppException:
    with pytest.raises(AppException) as capturado:
        await recolectar(iterador)
    return capturado.value


def sin_filtraciones(error: AppException):
    visible = f"{error} {error.message} {error.details!r} {error.code}"
    assert SECRETO_DOCUMENTO not in visible
    assert SECRETO_CLAVE not in visible
    # Sin cadena de excepciones visible: ni causa explícita ni contexto sin suprimir.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__ is True


# --- Identidad del proveedor: sin valores inventados ------------------------------

def test_identidad_explicita_de_proveedor_modelo_y_dimension():
    assert (IDENTIDAD.proveedor, IDENTIDAD.modelo, IDENTIDAD.dimension) == (
        "proveedor-SIMULADO", "modelo-SIMULADO-de-prueba", 4,
    )


def test_la_identidad_no_tiene_valores_predeterminados():
    with pytest.raises(TypeError):
        IdentidadEmbeddings()


@pytest.mark.parametrize("valor", ["", "   ", None, 5])
def test_proveedor_y_modelo_deben_ser_textos_no_vacios(valor):
    with pytest.raises(ValueError, match="proveedor debe ser un texto no vacío"):
        IdentidadEmbeddings(valor, "m", 3)
    with pytest.raises(ValueError, match="modelo debe ser un texto no vacío"):
        IdentidadEmbeddings("p", valor, 3)


@pytest.mark.parametrize("valor", [0, -1, True, False, 3.0, "3", None])
def test_dimension_invalida(valor):
    with pytest.raises(ValueError, match="dimension"):
        IdentidadEmbeddings("p", "m", valor)


def test_no_existe_un_proveedor_de_reserva_ni_configuracion_por_entorno_en_produccion():
    with pytest.raises(TypeError):
        ProveedorEmbeddings()  # un Protocol no se instancia: no hay implementación por defecto
    fuente = inspect.getsource(embeddings)
    assert "environ" not in fuente
    assert "getenv" not in fuente
    nombres = [n.lower() for n in dir(embeddings)]
    assert not any(marca in n for n in nombres for marca in ("simulad", "fake", "mock", "dummy"))


# --- Orden, asociación y lotes ----------------------------------------------------------

async def test_cada_fragmento_conserva_su_vector_y_el_orden():
    origen = fragmentos(7)
    proveedor = ProveedorSimulado()
    lotes = await recolectar(embeber_fragmentos(origen, proveedor, tamano_lote=3, timeout_segundos=5))

    elementos = [e for lote in lotes for e in lote.elementos]
    assert [e.fragmento.indice for e in elementos] == list(range(7))
    for esperado, e in zip(origen, elementos):
        assert e.fragmento is esperado  # el original, sin copiar ni alterar
        assert isinstance(e, FragmentoEmbebido)
        assert e.vector == tuple(vector_de(esperado.texto_embedding))
        assert all(type(c) is float for c in e.vector)


async def test_varios_lotes_con_ultimo_incompleto_y_numeracion():
    proveedor = ProveedorSimulado()
    lotes = await recolectar(embeber_fragmentos(fragmentos(7), proveedor, tamano_lote=3, timeout_segundos=5))
    assert [lote.numero for lote in lotes] == [0, 1, 2]
    assert [len(lote.elementos) for lote in lotes] == [3, 3, 1]
    assert [len(textos) for textos in proveedor.lotes] == [3, 3, 1]
    assert all(lote.identidad == IDENTIDAD for lote in lotes)


@pytest.mark.parametrize(
    ("cantidad", "tamano", "esperado"),
    [(6, 3, [3, 3]), (2, 10, [2]), (5, 1, [1] * 5), (1, 1, [1])],
)
async def test_tamanos_de_lote_sin_llamadas_vacias(cantidad, tamano, esperado):
    proveedor = ProveedorSimulado()
    lotes = await recolectar(embeber_fragmentos(fragmentos(cantidad), proveedor, tamano_lote=tamano, timeout_segundos=5))
    assert [len(lote.elementos) for lote in lotes] == esperado
    assert len(proveedor.lotes) == len(esperado)  # ninguna llamada extra al agotar la entrada


async def test_el_texto_enviado_es_el_de_embeddings_y_la_evidencia_sigue_siendo_el_literal():
    f = fragmento(0, literal="cita literal exacta", contexto="Sección: A > B\n\n")
    proveedor = ProveedorSimulado()
    [lote] = await recolectar(embeber_fragmentos([f], proveedor, tamano_lote=5, timeout_segundos=5))
    assert proveedor.lotes == [("Sección: A > B\n\ncita literal exacta",)]
    [elemento] = lote.elementos
    assert elemento.fragmento.texto_literal == "cita literal exacta"
    assert elemento.fragmento.contexto == "Sección: A > B\n\n"
    assert elemento.vector == tuple(vector_de("Sección: A > B\n\ncita literal exacta"))
    assert elemento.vector != tuple(vector_de("cita literal exacta"))


async def test_los_vectores_entregados_son_copias_inmutables_de_la_respuesta():
    respuesta = [[1, 2, 3, 4]]  # enteros: se convierten a float
    proveedor = proveedor_con(AsyncMock(return_value=respuesta))
    [lote] = await recolectar(embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=5))
    respuesta[0][0] = 999.0
    assert lote.elementos[0].vector == (1.0, 2.0, 3.0, 4.0)
    assert isinstance(lote.elementos[0].vector, tuple)
    assert all(type(c) is float for c in lote.elementos[0].vector)


# --- Consumo incremental --------------------------------------------------------------------

async def test_consume_el_origen_lote_a_lote_sin_leer_por_adelantado():
    consumidos: list[int] = []

    def origen():
        for i in range(10):
            consumidos.append(i)
            yield fragmento(i)

    proveedor = ProveedorSimulado()
    iterador = embeber_fragmentos(origen(), proveedor, tamano_lote=3, timeout_segundos=5)
    assert consumidos == []
    assert proveedor.lotes == []  # llamar no consume ni llama al proveedor

    primero = await anext(iterador)
    assert (len(consumidos), len(proveedor.lotes), primero.numero) == (3, 1, 0)
    segundo = await anext(iterador)
    assert (len(consumidos), len(proveedor.lotes), segundo.numero) == (6, 2, 1)
    resto = [lote async for lote in iterador]
    assert [len(lote.elementos) for lote in resto] == [3, 1]
    assert len(consumidos) == 10
    assert len(proveedor.lotes) == 4


async def test_no_retiene_lotes_ya_entregados_ni_acumula_vectores_del_documento():
    proveedor = ProveedorSimulado()
    iterador = embeber_fragmentos(fragmentos(9), proveedor, tamano_lote=3, timeout_segundos=5)
    primero = await anext(iterador)
    referencia = weakref.ref(primero)
    del primero
    await anext(iterador)
    gc.collect()
    assert referencia() is None  # el generador no guarda lotes previos
    await recolectar(iterador)


async def test_se_integra_con_el_iterador_incremental_de_fragmentacion():
    texto = "# T\n\n" + "palabra " * 400
    iterador_fuente = iterar_fragmentos(texto, ParametrosFragmentacion(100, 50))
    proveedor = ProveedorSimulado()
    lotes = [lote async for lote in embeber_fragmentos(iterador_fuente, proveedor, tamano_lote=4, timeout_segundos=5)]
    assert sum(len(lote.elementos) for lote in lotes) == len(fragmentar(texto, ParametrosFragmentacion(100, 50)))
    assert next(iterador_fuente, None) is None  # el origen quedó agotado, no duplicado


# --- Entrada vacía ------------------------------------------------------------------------------

@pytest.mark.parametrize("vacio", [lambda: [], lambda: iter(()), lambda: (f for f in ()), lambda: iterar_fragmentos("")])
async def test_entrada_vacia_no_llama_al_proveedor(vacio):
    generar = AsyncMock()
    lotes = await recolectar(embeber_fragmentos(vacio(), proveedor_con(generar), tamano_lote=3, timeout_segundos=5))
    assert lotes == []
    generar.assert_not_called()


# --- Parámetros y tipos inválidos -------------------------------------------------------------

@pytest.mark.parametrize("valor", [0, -1, -100, True, False, 1.5, 3.0, "10", None])
def test_tamano_de_lote_invalido(valor):
    valor_proveedorsimulado = ProveedorSimulado()
    with pytest.raises(ValueError, match="tamano_lote"):
        embeber_fragmentos([], valor_proveedorsimulado, tamano_lote=valor, timeout_segundos=5)


@pytest.mark.parametrize("valor", [0, 0.0, -1, -0.5, math.nan, math.inf, -math.inf, True, "5", None])
def test_timeout_invalido(valor):
    valor_proveedorsimulado_2 = ProveedorSimulado()
    with pytest.raises(ValueError, match="timeout_segundos"):
        embeber_fragmentos([], valor_proveedorsimulado_2, tamano_lote=1, timeout_segundos=valor)


@pytest.mark.parametrize("plazo", [1, 0.001, 60.5])
def test_timeouts_validos(plazo):
    embeber_fragmentos([], ProveedorSimulado(), tamano_lote=1, timeout_segundos=plazo)


def test_los_parametros_son_obligatorios_y_se_validan_al_llamar_no_al_consumir():
    valor_proveedorsimulado_3 = ProveedorSimulado()
    with pytest.raises(TypeError):
        embeber_fragmentos([], valor_proveedorsimulado_3)
    proveedor = ProveedorSimulado()
    with pytest.raises(TypeError):
        embeber_fragmentos([], proveedor, 3, 5)  # solo por nombre
    dos_fragmentos = fragmentos(2)
    with pytest.raises(ValueError):  # sin necesidad de iterar
        embeber_fragmentos(dos_fragmentos, proveedor, tamano_lote=0, timeout_segundos=5)


@pytest.mark.parametrize(
    "proveedor",
    [
        SimpleNamespace(generar_embeddings=AsyncMock()),                                # sin identidad
        SimpleNamespace(identidad="modelo", generar_embeddings=AsyncMock()),            # identidad mal tipada
        SimpleNamespace(identidad=IDENTIDAD),                                           # sin método
        SimpleNamespace(identidad=IDENTIDAD, generar_embeddings="no invocable"),
        None,
    ],
)
def test_proveedor_que_no_cumple_el_contrato(proveedor):
    with pytest.raises(TypeError):
        embeber_fragmentos([], proveedor, tamano_lote=1, timeout_segundos=5)


@pytest.mark.parametrize("origen", ["texto", b"bytes", None, 5])
def test_los_fragmentos_deben_ser_un_iterable(origen):
    valor_proveedorsimulado_4 = ProveedorSimulado()
    with pytest.raises(TypeError, match="iterable"):
        embeber_fragmentos(origen, valor_proveedorsimulado_4, tamano_lote=1, timeout_segundos=5)


async def test_elementos_que_no_son_fragmentos_se_rechazan_sin_llamar_al_proveedor():
    generar = AsyncMock()
    iterador = embeber_fragmentos(["texto suelto"], proveedor_con(generar), tamano_lote=2, timeout_segundos=5)
    with pytest.raises(TypeError, match="Fragmento"):
        await recolectar(iterador)
    generar.assert_not_called()


# --- Validación de las respuestas -------------------------------------------------------------------

async def procesar_con(respuesta, cantidad: int = 2):
    return embeber_fragmentos(
        fragmentos(cantidad), proveedor_con(AsyncMock(return_value=respuesta)), tamano_lote=cantidad, timeout_segundos=5
    )


def vectores_buenos(n: int):
    return [[1.0, 2.0, 3.0, 4.0] for _ in range(n)]


@pytest.mark.parametrize(
    ("respuesta", "esperada", "recibida"),
    [
        (vectores_buenos(1), 2, 1),
        (vectores_buenos(3), 2, 3),
        ([], 2, 0),
    ],
)
async def test_cantidad_de_vectores_incorrecta(respuesta, esperada, recibida):
    error = await fallo_de(await procesar_con(respuesta))
    assert isinstance(error, ExternalServiceError)
    assert not isinstance(error, ExternalServiceTimeoutError)
    assert error.code == "EMBEDDING_INVALID_RESPONSE"
    assert error.details == {
        "proveedor": "proveedor-SIMULADO", "modelo": "modelo-SIMULADO-de-prueba", "lote": 0,
        "motivo": "cantidad_incorrecta", "esperada": esperada, "recibida": recibida,
    }


@pytest.mark.parametrize(
    ("respuesta", "posicion", "recibida"),
    [
        ([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]], 0, 3),         # corto el primero
        ([[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0, 5.0]], 1, 5),  # largo el segundo
        ([[1.0, 2.0, 3.0, 4.0], []], 1, 0),
    ],
)
async def test_dimension_incorrecta_en_algun_vector(respuesta, posicion, recibida):
    error = await fallo_de(await procesar_con(respuesta))
    assert error.code == "EMBEDDING_INVALID_RESPONSE"
    assert error.details["motivo"] == "dimension_incorrecta"
    assert (error.details["vector"], error.details["esperada"], error.details["recibida"]) == (posicion, 4, recibida)


@pytest.mark.parametrize(
    ("valor", "motivo"),
    [
        (True, "componente_no_numerico"),
        (False, "componente_no_numerico"),
        ("1.0", "componente_no_numerico"),
        (None, "componente_no_numerico"),
        (1 + 2j, "componente_no_numerico"),
        (Decimal("1.5"), "componente_no_numerico"),
        ([1.0], "componente_no_numerico"),
        (b"1", "componente_no_numerico"),
        (math.nan, "componente_no_finito"),
        (math.inf, "componente_no_finito"),
        (-math.inf, "componente_no_finito"),
        (10**400, "componente_no_finito"),  # entero que no cabe en un float
    ],
)
async def test_componentes_invalidos_se_rechazan_con_su_posicion(valor, motivo):
    respuesta = [[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, valor, 4.0]]
    error = await fallo_de(await procesar_con(respuesta))
    assert error.code == "EMBEDDING_INVALID_RESPONSE"
    assert error.details["motivo"] == motivo
    assert (error.details["vector"], error.details["componente"]) == (1, 2)
    assert "nan" not in repr(error.details).lower()
    assert "inf" not in repr(error.details).lower()


@pytest.mark.parametrize(
    ("respuesta", "motivo", "vector"),
    [
        (None, "respuesta_no_es_secuencia", None),
        ({"datos": []}, "respuesta_no_es_secuencia", None),
        ("vectores", "respuesta_no_es_secuencia", None),
        (b"vectores", "respuesta_no_es_secuencia", None),
        (5, "respuesta_no_es_secuencia", None),
        ((v for v in vectores_buenos(2)), "respuesta_no_es_secuencia", None),
        ({1, 2}, "respuesta_no_es_secuencia", None),
        ([[1.0, 2.0, 3.0, 4.0], "1234"], "vector_mal_formado", 1),
        ([None, [1.0, 2.0, 3.0, 4.0]], "vector_mal_formado", 0),
        ([[1.0, 2.0, 3.0, 4.0], 7], "vector_mal_formado", 1),
        ([b"abcd", [1.0, 2.0, 3.0, 4.0]], "vector_mal_formado", 0),
        ([{"a": 1}, [1.0, 2.0, 3.0, 4.0]], "vector_mal_formado", 0),
    ],
)
async def test_respuestas_mal_formadas(respuesta, motivo, vector):
    error = await fallo_de(await procesar_con(respuesta))
    assert error.code == "EMBEDDING_INVALID_RESPONSE"
    assert error.details["motivo"] == motivo
    assert error.details.get("vector") == vector


async def test_componentes_numericos_validos_enteros_y_flotantes_y_tuplas():
    respuesta = [(1, 2.5, -3, 0), [0.0, -0.0, 1e300, 5e-324]]
    [lote] = await recolectar(await procesar_con(respuesta))
    assert [e.vector for e in lote.elementos] == [(1.0, 2.5, -3.0, 0.0), (0.0, -0.0, 1e300, 5e-324)]


async def test_una_respuesta_invalida_no_entrega_el_lote():
    entregados = []
    iterador = await procesar_con([[1.0, 2.0, 3.0, 4.0], [1.0, 2.0, math.nan, 4.0]])
    with pytest.raises(ExternalServiceError):
        async for lote in iterador:
            entregados.append(lote)
    assert entregados == []


def test_validar_respuesta_es_utilizable_por_separado():
    vectores = validar_respuesta([[1, 2, 3, 4]], 1, IDENTIDAD, 7)
    assert vectores == ((1.0, 2.0, 3.0, 4.0),)
    with pytest.raises(ExternalServiceError) as capturado:
        validar_respuesta([[1, 2, 3]], 1, IDENTIDAD, 7)
    assert capturado.value.details["lote"] == 7


# --- Fallos del proveedor, timeout y cancelación -------------------------------------------------------

async def proveedor_colgado():
    async def colgar(textos):
        await asyncio.sleep(30)
    return proveedor_con(AsyncMock(side_effect=colgar))


async def test_timeout_en_una_llamada_produce_error_controlado_504():
    proveedor = await proveedor_colgado()
    error = await fallo_de(embeber_fragmentos(fragmentos(2), proveedor, tamano_lote=2, timeout_segundos=0.05))
    assert isinstance(error, ExternalServiceTimeoutError)
    assert error.code == "EMBEDDING_PROVIDER_TIMEOUT"
    assert error.details == {
        "proveedor": "proveedor-SIMULADO", "modelo": "modelo-SIMULADO-de-prueba", "lote": 0, "timeout_segundos": 0.05,
    }
    assert "0.05" in error.message
    sin_filtraciones(error)


async def test_un_timeout_del_propio_proveedor_tambien_es_timeout():
    proveedor = proveedor_con(AsyncMock(side_effect=TimeoutError(f"tiempo agotado {SECRETO_CLAVE}")))
    error = await fallo_de(embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=5))
    assert error.code == "EMBEDDING_PROVIDER_TIMEOUT"
    sin_filtraciones(error)


async def test_el_timeout_se_aplica_a_cada_llamada_no_al_total():
    async def lenta(textos):
        await asyncio.sleep(0.2)
        return respuesta_simulada(textos)

    # 4 llamadas de 0,2 s = 0,8 s en total, por encima del plazo de 0,5 s por llamada.
    proveedor = proveedor_con(AsyncMock(side_effect=lenta))
    lotes = await recolectar(embeber_fragmentos(fragmentos(4), proveedor, tamano_lote=1, timeout_segundos=0.5))
    assert len(lotes) == 4


async def test_fallo_del_proveedor_no_expone_contenido_ni_credenciales():
    f = fragmento(0, literal=SECRETO_DOCUMENTO)
    excepcion = RuntimeError(f"401 clave {SECRETO_CLAVE} texto {SECRETO_DOCUMENTO}")
    proveedor = proveedor_con(AsyncMock(side_effect=excepcion))
    error = await fallo_de(embeber_fragmentos([f], proveedor, tamano_lote=1, timeout_segundos=5))
    assert isinstance(error, ExternalServiceError)
    assert not isinstance(error, ExternalServiceTimeoutError)
    assert error.code == "EMBEDDING_PROVIDER_ERROR"
    assert error.details == {
        "proveedor": "proveedor-SIMULADO", "modelo": "modelo-SIMULADO-de-prueba", "lote": 0, "tipo_error": "RuntimeError",
    }
    sin_filtraciones(error)


async def test_una_respuesta_invalida_tampoco_expone_el_contenido():
    f = fragmento(0, literal=SECRETO_DOCUMENTO)
    proveedor = proveedor_con(AsyncMock(return_value=[[SECRETO_CLAVE, 1, 2, 3]]))
    error = await fallo_de(embeber_fragmentos([f], proveedor, tamano_lote=1, timeout_segundos=5))
    assert error.code == "EMBEDDING_INVALID_RESPONSE"
    sin_filtraciones(error)


SENSIBLES = (
    "CLAVE-sk-ultrasecreta-999",
    "TEXTO-DOCUMENTAL-CONFIDENCIAL",
    "RESPUESTA-COMPLETA-DEL-PROVEEDOR",
    "Bearer-TOKEN-OCULTO",
)


def excepciones_de_proveedor_con_informacion_sensible() -> list[AppException]:
    return [
        BusinessValidationError(
            "CODIGO-" + SENSIBLES[0],
            f"El documento {SENSIBLES[1]} fue rechazado",
            details={"respuesta": SENSIBLES[2], "autorizacion": SENSIBLES[3], "anidado": [{"clave": SENSIBLES[0]}]},
        ),
        ExternalServiceError(
            "EMBEDDING_INVALID_RESPONSE",  # intenta hacerse pasar por un error propio
            f"respuesta cruda: {SENSIBLES[2]}",
            details={"texto": SENSIBLES[1]},
            headers={"Authorization": SENSIBLES[3]},
        ),
        ExternalServiceTimeoutError(
            "EMBEDDING_PROVIDER_TIMEOUT",
            f"timeout con {SENSIBLES[0]}",
            details=SENSIBLES[2],
        ),
        AuthenticationError("AUTH", SENSIBLES[3], details=[SENSIBLES[0]], headers={"WWW-Authenticate": SENSIBLES[0]}),
        AppException(SENSIBLES[0], SENSIBLES[1]),
    ]


def sin_informacion_sensible(*textos: str):
    for texto in textos:
        for sensible in SENSIBLES:
            assert sensible not in texto, f"se filtró {sensible!r}"


@pytest.mark.parametrize("original", excepciones_de_proveedor_con_informacion_sensible(), ids=lambda e: type(e).__name__)
async def test_una_app_exception_del_proveedor_se_convierte_en_error_controlado_seguro(original):
    proveedor = proveedor_con(AsyncMock(side_effect=original))
    error = await fallo_de(embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=5))

    assert error is not original
    # El código lo fija este módulo: un proveedor no puede hacerse pasar por un error propio.
    assert isinstance(error, ExternalServiceError)
    assert not isinstance(error, ExternalServiceTimeoutError)
    assert error.code == "EMBEDDING_PROVIDER_ERROR"
    assert error.message == "El proveedor de embeddings devolvió un error."
    assert error.details == {
        "proveedor": "proveedor-SIMULADO", "modelo": "modelo-SIMULADO-de-prueba", "lote": 0,
        "tipo_error": type(original).__name__,
    }
    assert error.headers is None
    sin_informacion_sensible(str(error), error.message, error.code, repr(error.details), repr(error.headers))
    sin_filtraciones(error)


async def test_la_app_exception_del_proveedor_no_llega_a_la_respuesta_http_ni_a_los_logs(caplog):
    original = BusinessValidationError(
        "CODIGO-" + SENSIBLES[0],
        f"El documento {SENSIBLES[1]} fue rechazado",
        details={"respuesta": SENSIBLES[2], "autorizacion": SENSIBLES[3]},
        headers={"X-Secreto": SENSIBLES[0]},
    )
    proveedor = proveedor_con(AsyncMock(side_effect=original))

    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    # Ruta solo de prueba: el endpoint real de ingesta pertenece a la Etapa 6.
    @app.post("/embeddings")
    async def embeber():
        async for _ in embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=5):
            pass

    with caplog.at_level(logging.DEBUG):
        with TestClient(app) as client:
            response = client.post("/embeddings")

    assert response.status_code == 502  # fallo de un servicio externo, no un 400 del cliente
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "EMBEDDING_PROVIDER_ERROR"
    assert error["details"] == {
        "proveedor": "proveedor-SIMULADO", "modelo": "modelo-SIMULADO-de-prueba", "lote": 0,
        "tipo_error": "BusinessValidationError",
    }
    cabeceras = " ".join(f"{k}: {v}" for k, v in response.headers.items())
    sin_informacion_sensible(response.text, cabeceras, caplog.text)
    assert "x-secreto" not in response.headers


async def test_las_excepciones_de_la_validacion_propia_no_se_alteran():
    # Lo que genera este módulo al validar la respuesta sigue siendo exactamente eso.
    proveedor = proveedor_con(AsyncMock(return_value=[[1.0, 2.0, math.nan, 4.0]]))
    error = await fallo_de(embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=5))
    assert error.code == "EMBEDDING_INVALID_RESPONSE"
    assert error.message == "El proveedor de embeddings devolvió una respuesta inválida."
    assert error.details["motivo"] == "componente_no_finito"
    assert type(error) is ExternalServiceError


async def test_el_timeout_propio_conserva_su_tratamiento_especifico():
    proveedor = await proveedor_colgado()
    error = await fallo_de(embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=0.05))
    assert type(error) is ExternalServiceTimeoutError
    assert error.code == "EMBEDDING_PROVIDER_TIMEOUT"


async def test_la_cancelacion_sigue_propagandose_aunque_el_proveedor_lance_app_exception_antes():
    llamadas = 0

    async def generar(textos):
        nonlocal llamadas
        llamadas += 1
        raise asyncio.CancelledError()

    proveedor = proveedor_con(AsyncMock(side_effect=generar))
    valor_embeber_fragmentos = embeber_fragmentos(fragmentos(2), proveedor, tamano_lote=1, timeout_segundos=5)
    with pytest.raises(asyncio.CancelledError):
        await recolectar(valor_embeber_fragmentos)
    assert llamadas == 1  # no se convirtió en error ni se pasó al lote siguiente


async def test_la_cancelacion_se_propaga_y_cancela_la_llamada_al_proveedor():
    iniciada = asyncio.Event()
    cancelada = []

    async def colgar(textos):
        iniciada.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelada.append(True)
            raise

    proveedor = proveedor_con(AsyncMock(side_effect=colgar))

    async def consumir():
        async for _ in embeber_fragmentos(fragmentos(3), proveedor, tamano_lote=1, timeout_segundos=60):
            pass

    tarea = asyncio.create_task(consumir())
    await iniciada.wait()
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    assert tarea.cancelled()
    assert cancelada == [True]


async def test_una_cancelacion_lanzada_por_el_proveedor_no_se_convierte_en_exito_ni_en_error_de_servicio():
    proveedor = proveedor_con(AsyncMock(side_effect=asyncio.CancelledError()))
    valor_embeber_fragmentos_2 = embeber_fragmentos(fragmentos(1), proveedor, tamano_lote=1, timeout_segundos=5)
    with pytest.raises(asyncio.CancelledError):
        await recolectar(valor_embeber_fragmentos_2)


async def test_la_cancelacion_durante_el_consumo_no_deja_lotes_como_entregados_de_mas():
    proveedor = ProveedorSimulado()
    iterador = embeber_fragmentos(fragmentos(9), proveedor, tamano_lote=3, timeout_segundos=5)
    await anext(iterador)
    await iterador.aclose()  # el consumidor abandona: no se piden más lotes
    assert len(proveedor.lotes) == 1
    with pytest.raises(StopAsyncIteration):
        await anext(iterador)


# --- Fallo después de un lote exitoso ----------------------------------------------------------------------

async def test_si_falla_un_lote_posterior_los_anteriores_ya_se_entregaron_y_no_se_lee_mas():
    consumidos = []

    def origen():
        for i in range(9):
            consumidos.append(i)
            yield fragmento(i)

    llamadas = 0

    async def generar(textos):
        nonlocal llamadas
        llamadas += 1
        if llamadas == 2:
            raise ConnectionError(SECRETO_CLAVE)
        return respuesta_simulada(textos)

    iterador = embeber_fragmentos(origen(), proveedor_con(AsyncMock(side_effect=generar)), tamano_lote=3, timeout_segundos=5)
    primero = await anext(iterador)  # entregado: el coordinador ya podría haberlo persistido
    assert [e.fragmento.indice for e in primero.elementos] == [0, 1, 2]

    with pytest.raises(ExternalServiceError) as capturado:
        await anext(iterador)
    assert capturado.value.code == "EMBEDDING_PROVIDER_ERROR"
    assert capturado.value.details["lote"] == 1
    sin_filtraciones(capturado.value)
    assert consumidos == list(range(6))  # no se leyó el tercer lote
    with pytest.raises(StopAsyncIteration):  # terminado: no hay un «completado» implícito
        await anext(iterador)


async def test_respuesta_invalida_en_un_lote_posterior_tambien_deja_entregados_los_anteriores():
    respuestas = iter([respuesta_simulada(["a", "b"]), [[1.0, 2.0, 3.0, 4.0], [1.0, math.inf, 3.0, 4.0]]])

    async def generar(textos):
        return next(respuestas)

    iterador = embeber_fragmentos(fragmentos(6), proveedor_con(AsyncMock(side_effect=generar)), tamano_lote=2, timeout_segundos=5)
    entregados = []
    with pytest.raises(ExternalServiceError) as capturado:
        async for lote in iterador:
            entregados.append(lote)
    assert [lote.numero for lote in entregados] == [0]
    assert capturado.value.code == "EMBEDDING_INVALID_RESPONSE"
    assert (capturado.value.details["lote"], capturado.value.details["vector"]) == (1, 1)


async def test_timeout_en_un_lote_posterior():
    llamadas = 0

    async def generar(textos):
        nonlocal llamadas
        llamadas += 1
        if llamadas == 2:
            await asyncio.sleep(30)
        return respuesta_simulada(textos)

    iterador = embeber_fragmentos(fragmentos(4), proveedor_con(AsyncMock(side_effect=generar)), tamano_lote=2, timeout_segundos=0.05)
    await anext(iterador)
    with pytest.raises(ExternalServiceTimeoutError) as capturado:
        await anext(iterador)
    assert capturado.value.details["lote"] == 1


# --- Integración con fragmentos reales de la Etapa 2 -------------------------------------------------------------

TEXTO_REAL = (
    "# Memoria 2025\r\n\r\nIntroducción con ñandú y acción.\r\n\r\n"
    "## Agua\r\n\r\nConsumo total de agua.\r\n\r\n"
    "| Indicador | Valor |\r\n|---|---|\r\n| Agua captada | 1200 |\r\n| Agua reutilizada | 300 |\r\n| Agua vertida | 80 |\r\n"
)


async def test_integracion_con_fragmentos_reales_y_contexto_enviado():
    parametros = ParametrosFragmentacion(max_caracteres=80, max_caracteres_contexto=120)
    esperados = fragmentar(TEXTO_REAL, parametros)
    assert any(f.contexto for f in esperados)  # el caso ejercita contexto real

    proveedor = ProveedorSimulado()
    lotes = await recolectar(
        embeber_fragmentos(iterar_fragmentos(TEXTO_REAL, parametros), proveedor, tamano_lote=2, timeout_segundos=5)
    )
    elementos = [e for lote in lotes for e in lote.elementos]

    # Se envió exactamente el texto de embeddings (contexto + literal), en orden.
    enviados = [t for lote in proveedor.lotes for t in lote]
    assert enviados == [f.texto_embedding for f in esperados]
    assert any(t.startswith("Sección:") for t in enviados)
    assert any("Encabezado de tabla: | Indicador | Valor |" in t for t in enviados)

    # El fragmento original llega intacto: índice, offsets, literal, ruta, contexto.
    assert [e.fragmento for e in elementos] == esperados
    assert [e.fragmento.indice for e in elementos] == list(range(len(esperados)))
    for e in elementos:
        f = e.fragmento
        assert TEXTO_REAL[f.inicio:f.fin] == f.texto_literal  # la cita literal sigue siendo exacta
        assert e.vector == tuple(vector_de(f.texto_embedding))
        if f.contexto:
            assert e.vector != tuple(vector_de(f.texto_literal))  # el vector sale del texto con contexto
    assert "".join(e.fragmento.texto_literal for e in elementos) == TEXTO_REAL
    assert elementos[0].fragmento.inicio == 0
    assert elementos[-1].fragmento.fin == len(TEXTO_REAL)


async def test_integracion_con_el_documento_validado_de_la_etapa_1_con_bom():
    import io
    from starlette.datastructures import UploadFile

    original = b"\xef\xbb\xbf" + TEXTO_REAL.encode("utf-8")
    documento = await validacion.validar_archivo(
        "memoria.md", UploadFile(file=io.BytesIO(original), filename="memoria.md").read
    )
    parametros = ParametrosFragmentacion(max_caracteres=80, max_caracteres_contexto=120)
    proveedor = ProveedorSimulado()
    lotes = await recolectar(
        embeber_fragmentos(iterar_fragmentos(documento.texto, parametros), proveedor, tamano_lote=3, timeout_segundos=5)
    )
    elementos = [e for lote in lotes for e in lote.elementos]
    # Offsets sobre el texto interpretado (sin BOM); los bytes originales y el hash no se tocan.
    assert all(documento.texto[e.fragmento.inicio:e.fragmento.fin] == e.fragmento.texto_literal for e in elementos)
    assert "".join(e.fragmento.texto_literal for e in elementos) == documento.texto
    assert documento.contenido == original


# --- Contrato HTTP de los nuevos errores ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("excepcion", "estado", "codigo"),
    [
        (ExternalServiceError("EMBEDDING_PROVIDER_ERROR", "Error.", details={"lote": 0}), 502, "EMBEDDING_PROVIDER_ERROR"),
        (ExternalServiceError("EMBEDDING_INVALID_RESPONSE", "Inválida."), 502, "EMBEDDING_INVALID_RESPONSE"),
        (ExternalServiceTimeoutError("EMBEDDING_PROVIDER_TIMEOUT", "Plazo."), 504, "EMBEDDING_PROVIDER_TIMEOUT"),
    ],
)
def test_errores_de_servicio_externo_usan_el_contrato_uniforme(excepcion, estado, codigo):
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    # Ruta solo de prueba: el endpoint real de ingesta pertenece a la Etapa 6.
    @app.get("/fallo")
    async def fallo():
        raise excepcion

    with TestClient(app) as client:
        response = client.get("/fallo")

    assert response.status_code == estado
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == codigo
    assert error["request_id"] == response.headers["x-request-id"]


# --- Consulta (SEARCH_QUERY): asimétrica respecto a la ingesta ----------------------

class ProveedorConsultaSimulado:
    """Proveedor simulado con la capacidad de embeber CONSULTAS."""

    identidad = IDENTIDAD

    def __init__(self, respuesta=None, error=None, demora: float = 0.0):
        self.consultas: list[tuple[str, ...]] = []
        self._respuesta = respuesta
        self._error = error
        self._demora = demora

    async def generar_embeddings_consulta(self, textos):
        self.consultas.append(tuple(textos))
        if self._demora:
            await asyncio.sleep(self._demora)
        if self._error is not None:
            raise self._error
        if self._respuesta is not None:
            return self._respuesta
        return respuesta_simulada(textos)


async def test_embeber_consulta_devuelve_el_vector_de_una_consulta():
    proveedor = ProveedorConsultaSimulado()
    texto = "¿cuánta agua se consumió en 2025?"
    vector = await embeber_consulta(texto, proveedor, timeout_segundos=5)
    assert vector == tuple(vector_de(texto))
    assert proveedor.consultas == [(texto,)]  # se envía la consulta tal cual, sin contexto


async def test_embeber_consulta_exige_proveedor_con_capacidad_de_consulta():
    proveedor = proveedor_con(lambda textos: respuesta_simulada(textos))  # solo ingesta
    with pytest.raises(TypeError):
        await embeber_consulta("x", proveedor, timeout_segundos=5)


@pytest.mark.parametrize("texto", ["", "   ", None, 5])
async def test_embeber_consulta_exige_texto_no_vacio(texto):
    valor_proveedorconsultasimulado = ProveedorConsultaSimulado()
    with pytest.raises(ValueError):
        await embeber_consulta(texto, valor_proveedorconsultasimulado, timeout_segundos=5)


@pytest.mark.parametrize("plazo", [0, -1, float("inf"), "5"])
async def test_embeber_consulta_exige_plazo_valido(plazo):
    valor_proveedorconsultasimulado_2 = ProveedorConsultaSimulado()
    with pytest.raises(ValueError):
        await embeber_consulta("x", valor_proveedorconsultasimulado_2, timeout_segundos=plazo)


async def test_embeber_consulta_envuelve_error_del_proveedor_sin_filtrar():
    proveedor = ProveedorConsultaSimulado(error=RuntimeError(SECRETO_CLAVE))
    with pytest.raises(ExternalServiceError) as capturado:
        await embeber_consulta(SECRETO_DOCUMENTO, proveedor, timeout_segundos=5)
    error = capturado.value
    assert error.code == "EMBEDDING_PROVIDER_ERROR"
    sin_filtraciones(error)


async def test_embeber_consulta_respeta_el_timeout():
    proveedor = ProveedorConsultaSimulado(demora=0.2)
    with pytest.raises(ExternalServiceTimeoutError) as capturado:
        await embeber_consulta("x", proveedor, timeout_segundos=0.01)
    assert capturado.value.code == "EMBEDDING_PROVIDER_TIMEOUT"


async def test_embeber_consulta_rechaza_dimension_incorrecta():
    proveedor = ProveedorConsultaSimulado(respuesta=[[0.1, 0.2, 0.3]])  # 3 != DIMENSION
    with pytest.raises(ExternalServiceError) as capturado:
        await embeber_consulta("x", proveedor, timeout_segundos=5)
    assert capturado.value.code == "EMBEDDING_INVALID_RESPONSE"


async def test_embeber_consulta_exige_una_identidad_valida():
    class SinIdentidad:
        identidad = "no es una IdentidadEmbeddings"

        async def generar_embeddings_consulta(self, textos):
            raise AssertionError("no debe llamarse")

    valor_sinidentidad = SinIdentidad()
    with pytest.raises(TypeError, match="identidad"):
        await embeber_consulta("x", valor_sinidentidad, timeout_segundos=5)

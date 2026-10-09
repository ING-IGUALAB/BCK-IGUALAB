"""Contrato de embeddings y procesamiento incremental por lotes (LTX:RF-016 parcial,
RNF-018 parcial, RNF-027).

Etapa 3A: NO hay proveedor real. Proveedor, modelo, dimensión y credenciales
siguen pendientes (D12). Este módulo no define ningún valor de producción ni
ningún proveedor de reserva: sin un `ProveedorEmbeddings` entregado por quien lo
llama no se genera ningún vector, y los proveedores simulados viven solo en las
pruebas.

CONTRATO DEL PROVEEDOR (`ProveedorEmbeddings`)
- `identidad`: `IdentidadEmbeddings(proveedor, modelo, dimension)`. Declara
  explícitamente el espacio vectorial; no se mezclan modelos ni dimensiones.
- `generar_embeddings(textos)`: asíncrona; recibe un lote de textos y devuelve
  una secuencia con un vector por texto, EN EL MISMO ORDEN. Cada vector es una
  secuencia de números reales (no `str`, `bytes` ni arrays de NumPy: el adaptador
  debe convertirlos). Un adaptador real debe respetar los límites de lote y
  longitud de su proveedor, que hoy se desconocen.

PROCESAMIENTO (`embeber_fragmentos`)
- Consume el iterador de fragmentos de forma incremental: lee del origen solo los
  fragmentos del lote en curso (nunca adelanta el siguiente ni lo convierte en
  lista) y no acumula vectores del documento. Entrega un `LoteEmbebido` por lote
  para que el coordinador lo persista antes de pedir el siguiente.
- Se envía a embeddings `Fragmento.texto_embedding` (contexto + literal). El
  fragmento original (índice, offsets, texto literal, ruta, contexto) se entrega
  intacto junto a su vector; la cita literal sigue siendo `texto_literal`.
- Un lote cada vez, sin concurrencia: no hay estado compartido entre llamadas.
- Parámetros explícitos y validados: `tamano_lote` y `timeout_segundos`. Sin
  variables de entorno. No se fija un máximo de lote porque el proveedor es
  desconocido. Entrada vacía: no se llama al proveedor.
- La fragmentación se ejecuta en el hilo del bucle de eventos entre llamadas al
  proveedor; su costo con documentos de 50 MB no está medido (T27).

VALIDACIÓN: antes de entregar cada lote se comprueba cantidad, dimensión,
componentes reales y finitos (se rechazan booleanos, NaN, ±inf, textos y
cualquier no-número) y que la respuesta sea una secuencia de secuencias. Se
entrega una copia inmutable (`tuple[float, ...]`).

FALLOS
- Cada llamada tiene `asyncio.timeout(timeout_segundos)`. Sin reintentos ni
  políticas específicas de un proveedor aún desconocido.
- Errores (`ExternalServiceError`, 502; `ExternalServiceTimeoutError`, 504):
  `EMBEDDING_PROVIDER_TIMEOUT`, `EMBEDDING_PROVIDER_ERROR`,
  `EMBEDDING_INVALID_RESPONSE`. Sus mensajes y detalles solo llevan
  identificadores (proveedor, modelo, número de lote, motivo, posiciones y
  conteos): nunca texto documental, vectores, credenciales, la respuesta ni
  `str(excepción)` del proveedor, y se corta la cadena de excepciones. Este módulo
  no escribe logs. Toda excepción que atraviese la llamada al proveedor, también
  una `AppException` (su message, details, code o headers no son confiables), se
  convierte en `EMBEDDING_PROVIDER_ERROR` con solo el nombre de su clase. Los
  errores de la validación de respuestas los genera este módulo y no se tocan.
- `asyncio.CancelledError` y demás `BaseException` se propagan: la cancelación
  nunca se convierte en éxito.
- Si falla un lote posterior, los anteriores YA se entregaron. Agotar el iterador
  sin error NO significa documento completado ni indexado: falta persistir y
  publicar. El coordinador futuro debe compensar lo entregado y evitar que
  contenido parcial sea consultable. Esta etapa no garantiza atomicidad entre
  bases de datos.
"""
import asyncio
import math
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from itertools import islice
from numbers import Real
from typing import Protocol

from app.exceptions import ExternalServiceError, ExternalServiceTimeoutError
from app.services.ingesta.fragmentacion import Fragmento


def _exigir_texto_no_vacio(nombre: str, valor: object) -> None:
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError(f"{nombre} debe ser un texto no vacío.")


@dataclass(frozen=True)
class IdentidadEmbeddings:
    """Espacio vectorial que produce un proveedor. Sin valores predeterminados:
    nada se asume hasta que el equipo apruebe proveedor, modelo y dimensión."""

    proveedor: str
    modelo: str
    dimension: int

    def __post_init__(self) -> None:
        _exigir_texto_no_vacio("proveedor", self.proveedor)
        _exigir_texto_no_vacio("modelo", self.modelo)
        if isinstance(self.dimension, bool) or not isinstance(self.dimension, int):
            raise ValueError("dimension debe ser un entero.")
        if self.dimension < 1:
            raise ValueError(f"dimension debe ser al menos 1; se recibió {self.dimension}.")


class ProveedorEmbeddings(Protocol):
    @property
    def identidad(self) -> IdentidadEmbeddings: ...

    async def generar_embeddings(self, textos: Sequence[str]) -> Sequence[Sequence[float]]:
        """Un vector por texto, en el mismo orden."""
        ...


@dataclass(frozen=True, slots=True)
class FragmentoEmbebido:
    """El fragmento original, sin alterar, y su vector."""

    fragmento: Fragmento
    vector: tuple[float, ...]


@dataclass(frozen=True)
class LoteEmbebido:
    numero: int  # desde 0
    identidad: IdentidadEmbeddings
    elementos: tuple[FragmentoEmbebido, ...]


def _error_respuesta(identidad: IdentidadEmbeddings, lote: int, motivo: str, **datos: int) -> ExternalServiceError:
    return ExternalServiceError(
        "EMBEDDING_INVALID_RESPONSE",
        "El proveedor de embeddings devolvió una respuesta inválida.",
        details={
            "proveedor": identidad.proveedor,
            "modelo": identidad.modelo,
            "lote": lote,
            "motivo": motivo,
            **datos,
        },
    )


def _es_secuencia(valor: object) -> bool:
    return isinstance(valor, Sequence) and not isinstance(valor, (str, bytes, bytearray))


def validar_respuesta(
    respuesta: object, cantidad: int, identidad: IdentidadEmbeddings, lote: int
) -> tuple[tuple[float, ...], ...]:
    """Comprueba la respuesta de un lote y devuelve una copia inmutable.
    Lanza `ExternalServiceError` (`EMBEDDING_INVALID_RESPONSE`) sin incluir la respuesta."""
    if not _es_secuencia(respuesta):
        raise _error_respuesta(identidad, lote, "respuesta_no_es_secuencia")
    if len(respuesta) != cantidad:
        raise _error_respuesta(
            identidad, lote, "cantidad_incorrecta", esperada=cantidad, recibida=len(respuesta)
        )
    vectores = []
    for posicion, vector in enumerate(respuesta):
        if not _es_secuencia(vector):
            raise _error_respuesta(identidad, lote, "vector_mal_formado", vector=posicion)
        if len(vector) != identidad.dimension:
            raise _error_respuesta(
                identidad, lote, "dimension_incorrecta",
                vector=posicion, esperada=identidad.dimension, recibida=len(vector),
            )
        componentes = []
        for indice, valor in enumerate(vector):
            # bool es subclase de int: debe descartarse antes de Real.
            if isinstance(valor, bool) or not isinstance(valor, Real):
                raise _error_respuesta(
                    identidad, lote, "componente_no_numerico", vector=posicion, componente=indice
                )
            try:
                numero = float(valor)
            except (OverflowError, ValueError, TypeError):
                numero = math.nan
            if not math.isfinite(numero):
                raise _error_respuesta(
                    identidad, lote, "componente_no_finito", vector=posicion, componente=indice
                )
            componentes.append(numero)
        vectores.append(tuple(componentes))
    return tuple(vectores)


def _exigir_entero_positivo(nombre: str, valor: object) -> None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ValueError(f"{nombre} debe ser un entero, no {type(valor).__name__}.")
    if valor < 1:
        raise ValueError(f"{nombre} debe ser al menos 1; se recibió {valor}.")


def _exigir_plazo(nombre: str, valor: object) -> None:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ValueError(f"{nombre} debe ser un número de segundos, no {type(valor).__name__}.")
    if not math.isfinite(valor) or valor <= 0:
        raise ValueError(f"{nombre} debe ser un número finito mayor que 0; se recibió {valor}.")


async def _llamar_metodo(
    metodo,
    identidad: IdentidadEmbeddings,
    textos: tuple[str, ...],
    lote: int,
    timeout_segundos: float,
) -> object:
    datos = {"proveedor": identidad.proveedor, "modelo": identidad.modelo, "lote": lote}
    try:
        async with asyncio.timeout(timeout_segundos):
            return await metodo(textos)
    except TimeoutError:
        raise ExternalServiceTimeoutError(
            "EMBEDDING_PROVIDER_TIMEOUT",
            f"El proveedor de embeddings no respondió en {timeout_segundos:g} segundos.",
            details={**datos, "timeout_segundos": timeout_segundos},
        ) from None
    except Exception as exc:
        # Cualquier excepción que atraviese la llamada, incluida una AppException, es
        # NO CONFIABLE: su message, details, code y headers pueden contener texto del
        # documento, credenciales o la respuesta del proveedor. Solo se conserva el
        # nombre de su clase; el código del error resultante lo fija este módulo.
        raise ExternalServiceError(
            "EMBEDDING_PROVIDER_ERROR",
            "El proveedor de embeddings devolvió un error.",
            details={**datos, "tipo_error": type(exc).__name__},
        ) from None


async def _llamar(
    proveedor: ProveedorEmbeddings,
    identidad: IdentidadEmbeddings,
    textos: tuple[str, ...],
    lote: int,
    timeout_segundos: float,
) -> object:
    return await _llamar_metodo(
        proveedor.generar_embeddings, identidad, textos, lote, timeout_segundos
    )


async def _procesar(
    fragmentos: Iterable[Fragmento],
    proveedor: ProveedorEmbeddings,
    identidad: IdentidadEmbeddings,
    tamano_lote: int,
    timeout_segundos: float,
) -> AsyncIterator[LoteEmbebido]:
    origen = iter(fragmentos)
    numero = 0
    while True:
        lote = tuple(islice(origen, tamano_lote))  # solo lo necesario, sin adelantar
        if not lote:
            return
        if not all(isinstance(f, Fragmento) for f in lote):
            raise TypeError("Solo se pueden procesar objetos Fragmento.")
        textos = tuple(f.texto_embedding for f in lote)
        respuesta = await _llamar(proveedor, identidad, textos, numero, timeout_segundos)
        vectores = validar_respuesta(respuesta, len(lote), identidad, numero)
        yield LoteEmbebido(
            numero=numero,
            identidad=identidad,
            elementos=tuple(FragmentoEmbebido(f, v) for f, v in zip(lote, vectores)),
        )
        numero += 1


def embeber_fragmentos(
    fragmentos: Iterable[Fragmento],
    proveedor: ProveedorEmbeddings,
    *,
    tamano_lote: int,
    timeout_segundos: float,
) -> AsyncIterator[LoteEmbebido]:
    """Entrega lotes de fragmentos con sus vectores, de forma incremental.

    Los parámetros se validan al llamar, no al consumir. Un fallo en un lote
    posterior se lanza al pedir ese lote; los anteriores ya se entregaron.
    """
    _exigir_entero_positivo("tamano_lote", tamano_lote)
    _exigir_plazo("timeout_segundos", timeout_segundos)
    if isinstance(fragmentos, (str, bytes)) or not isinstance(fragmentos, Iterable):
        raise TypeError("Los fragmentos deben ser un iterable de Fragmento.")
    identidad = getattr(proveedor, "identidad", None)
    if not isinstance(identidad, IdentidadEmbeddings):
        raise TypeError("El proveedor debe exponer `identidad` como IdentidadEmbeddings.")
    if not callable(getattr(proveedor, "generar_embeddings", None)):
        raise TypeError("El proveedor debe implementar `generar_embeddings`.")
    return _procesar(fragmentos, proveedor, identidad, tamano_lote, timeout_segundos)


class ProveedorEmbeddingsConsulta(Protocol):
    """Capacidad de embeber CONSULTAS. La ingesta indexa con `SEARCH_DOCUMENT`
    y la consulta busca con `SEARCH_QUERY`: son representaciones asimétricas del
    mismo modelo y no se deben intercambiar."""

    @property
    def identidad(self) -> IdentidadEmbeddings: ...

    async def generar_embeddings_consulta(self, textos: Sequence[str]) -> Sequence[Sequence[float]]:
        """Un vector por texto, en el mismo orden, con `input_type` de consulta."""
        ...


async def embeber_consulta(
    texto: str,
    proveedor: ProveedorEmbeddingsConsulta,
    *,
    timeout_segundos: float,
) -> tuple[float, ...]:
    """Embebe UNA consulta (input_type `SEARCH_QUERY`) y devuelve su vector.

    Reutiliza la misma validación y el mismo manejo de errores que la ingesta
    (`EMBEDDING_PROVIDER_TIMEOUT`, `EMBEDDING_PROVIDER_ERROR`,
    `EMBEDDING_INVALID_RESPONSE`). El parámetro se valida al llamar, no al
    consumir. No se envía texto documental en los mensajes de error.
    """
    _exigir_texto_no_vacio("texto", texto)
    _exigir_plazo("timeout_segundos", timeout_segundos)
    identidad = getattr(proveedor, "identidad", None)
    if not isinstance(identidad, IdentidadEmbeddings):
        raise TypeError("El proveedor debe exponer `identidad` como IdentidadEmbeddings.")
    metodo = getattr(proveedor, "generar_embeddings_consulta", None)
    if not callable(metodo):
        raise TypeError("El proveedor debe implementar `generar_embeddings_consulta`.")
    respuesta = await _llamar_metodo(metodo, identidad, (texto,), 0, timeout_segundos)
    return validar_respuesta(respuesta, 1, identidad, 0)[0]

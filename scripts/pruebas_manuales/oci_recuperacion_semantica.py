"""PRUEBA MANUAL de recuperación semántica REAL con OCI Embed 4 (cohere.embed-v4.0, 1536).

NO forma parte de pytest ni del arranque. Hace llamadas REMOTAS pequeñas a OCI Generative AI
(consumen créditos): solo se ejecuta con `--ejecutar`. Usa tres textos y tres preguntas
SINTÉTICOS en español: sin documentos reales, sin bases de datos, sin pgvector, sin MinIO.

Uso (PowerShell, desde la raíz del repositorio):
    .venv\\Scripts\\python.exe -m scripts.pruebas_manuales.oci_recuperacion_semantica --ejecutar

Qué hace (proveedor y contratos EXISTENTES, sin mocks y sin tocar el flujo productivo):
 1. Embebe los tres textos con `embeber_fragmentos` (SEARCH_DOCUMENT), en UNA llamada.
 2. Embebe cada pregunta con `embeber_consulta` (SEARCH_QUERY): una llamada por pregunta.
 3. Verifica la respuesta real: modelo `cohere.embed-v4.0`; 3 vectores de documentos y 3 de
    consultas; cada vector con 1536 componentes numéricas finitas; ninguno con norma cero;
    los tres vectores de documentos distintos; y que las peticiones reales llevaran
    `output_dimensions=1536`, `truncate="NONE"` y el `input_type` correcto.
 4. Calcula localmente la similitud coseno de cada pregunta con cada documento y muestra las nueve.

Aprueba si cada pregunta encuentra primero su documento (agua→agua, electricidad→energia,
accidentes→personal) sin empate en el primer puesto. NO se impone un umbral de similitud ni se
espera similitud 1: documentos y preguntas usan tipos de entrada distintos (asimétricos).

Código de salida: 0 todo verificado; 1 alguna verificación o la llamada remota falló; 2 no se
ejecutó (falta `--ejecutar`) o la configuración de OCI es inválida. La salida solo lleva
resúmenes y la tabla de similitudes: nunca credenciales, vectores ni respuestas del SDK. Los
recursos se cierran siempre. Esta prueba demuestra generación y recuperación semántica SOLO sobre
estos ejemplos; no evalúa la calidad con documentos GRI reales ni la persistencia vectorial.
"""
import argparse
import asyncio
import math
import sys
from collections.abc import Callable
from typing import Any

from scripts.smoke_embeddings_oci import (  # fija JWT_SECRET_KEY ficticia antes de importar `app`
    DIMENSION_ESPERADA,
    MODELO_ESPERADO,
    TIMEOUT_SEGUNDOS,
    _Observador,
    _seguro,
    _vector_valido,
)
from app.services.ingesta.embeddings import embeber_consulta, embeber_fragmentos  # noqa: E402
from app.services.ingesta.fragmentacion import iterar_fragmentos  # noqa: E402

DOCUMENTOS = {
    "agua": "La empresa redujo el consumo de agua mediante la recirculación y reutilización de aguas residuales.",
    "energia": "La empresa instaló paneles solares para generar electricidad renovable y disminuir el consumo eléctrico de la red.",
    "personal": "La empresa capacitó a sus trabajadores en prevención de accidentes y seguridad ocupacional.",
}
PREGUNTAS = (
    ("¿Qué acciones permitieron ahorrar recursos hídricos?", "agua"),
    ("¿Cómo produce la compañía electricidad mediante fuentes renovables?", "energia"),
    ("¿Qué formación recibieron los empleados para evitar accidentes laborales?", "personal"),
)

EXIT_OK, EXIT_FALLO, EXIT_NO_EJECUTADO = 0, 1, 2


def norma(vector) -> float:
    return math.sqrt(sum(c * c for c in vector))


def coseno(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) / (norma(a) * norma(b))


def clasificar(similitudes: dict[str, float]) -> tuple[str, bool]:
    """(mejor documento, ¿hay empate exacto en el primer puesto?)."""
    orden = sorted(similitudes.items(), key=lambda par: par[1], reverse=True)
    return orden[0][0], orden[0][1] == orden[1][1]


async def _ejecutar(proveedor: Any, imprimir: Callable[[str], None]) -> tuple[list[str], int]:
    fallos: list[str] = []
    identidad = proveedor.identidad
    imprimir(f"[identidad] modelo={identidad.modelo} dimensión={identidad.dimension}")
    if (identidad.modelo, identidad.dimension) != (MODELO_ESPERADO, DIMENSION_ESPERADA):
        return [f"la identidad debe ser {MODELO_ESPERADO} con {DIMENSION_ESPERADA}"], 0

    observador = _Observador(proveedor._client)

    # 1. Documentos (SEARCH_DOCUMENT) a través del contrato, una sola llamada
    nombres = list(DOCUMENTOS)
    fragmentos = []
    for nombre in nombres:
        propios = list(iterar_fragmentos(DOCUMENTOS[nombre]))
        if len(propios) != 1 or propios[0].texto_embedding != DOCUMENTOS[nombre]:
            return [f"el texto «{nombre}» no produjo exactamente un fragmento sin contexto añadido"], 0
        fragmentos.append(propios[0])
    vectores_doc = []
    async for lote in embeber_fragmentos(iter(fragmentos), proveedor, tamano_lote=len(fragmentos), timeout_segundos=TIMEOUT_SEGUNDOS):
        vectores_doc.extend(e.vector for e in lote.elementos)

    # 2. Preguntas (SEARCH_QUERY), una llamada por pregunta
    vectores_q = [await embeber_consulta(texto, proveedor, timeout_segundos=TIMEOUT_SEGUNDOS) for texto, _ in PREGUNTAS]
    llamadas = len(observador.peticiones)

    # 3. Verificaciones de la respuesta real
    imprimir(f"[vectores] documentos={len(vectores_doc)} consultas={len(vectores_q)} llamadas a OCI={llamadas}")
    if len(vectores_doc) != 3:
        fallos.append(f"se esperaban 3 vectores de documentos y llegaron {len(vectores_doc)}")
    if len(vectores_q) != 3:
        fallos.append(f"se esperaban 3 vectores de consultas y llegaron {len(vectores_q)}")
    todos = list(vectores_doc) + list(vectores_q)
    imprimir(f"[dimensiones] recibidas={sorted({len(v) for v in todos})} (esperada {DIMENSION_ESPERADA})")
    if not all(_vector_valido(v) for v in todos):
        fallos.append("algún vector no tiene exactamente 1536 componentes numéricas finitas")
    elif any(norma(v) == 0 for v in todos):
        fallos.append("algún vector tiene norma cero")
    if len({tuple(v) for v in vectores_doc}) != len(vectores_doc):
        fallos.append("los vectores de los documentos no son todos distintos")
    modelos = {p["modelo"] for p in observador.peticiones}
    dims = {p["output_dimensions"] for p in observador.peticiones}
    truncs = {p["truncate"] for p in observador.peticiones}
    tipos = [p["input_type"] for p in observador.peticiones]
    imprimir(f"[peticiones] modelo={sorted(map(str, modelos))} output_dimensions={sorted(map(str, dims))} "
             f"truncate={sorted(map(str, truncs))} input_type={tipos}")
    if modelos != {MODELO_ESPERADO} or dims != {DIMENSION_ESPERADA} or truncs != {"NONE"}:
        fallos.append("alguna petición no llevó cohere.embed-v4.0, output_dimensions=1536 y truncate=NONE")
    if tipos != ["SEARCH_DOCUMENT"] + ["SEARCH_QUERY"] * len(PREGUNTAS):
        fallos.append("los tipos de entrada enviados no fueron 1×SEARCH_DOCUMENT y 3×SEARCH_QUERY")
    if fallos:
        return fallos, llamadas

    # 4. Similitud coseno local y recuperación
    imprimir("")
    imprimir("Similitud coseno (pregunta × documento):")
    imprimir(f"{'pregunta':<10}" + "".join(f"{n:>10}" for n in nombres) + f"{'mejor':>12}  esperado")
    for (texto, esperado), q in zip(PREGUNTAS, vectores_q):
        sims = {n: coseno(q, d) for n, d in zip(nombres, vectores_doc)}
        mejor, empate = clasificar(sims)
        etiqueta = {"agua": "P1 agua", "energia": "P2 elec.", "personal": "P3 acc."}[esperado]
        imprimir(f"{etiqueta:<10}" + "".join(f"{sims[n]:>10.4f}" for n in nombres) + f"{mejor:>12}  {esperado}")
        if empate:
            fallos.append(f"empate en el primer puesto para la pregunta de «{esperado}»")
        elif mejor != esperado:
            fallos.append(f"la pregunta de «{esperado}» encontró primero «{mejor}»")
    return fallos, llamadas


def main(
    argv: list[str] | None = None,
    crear_proveedor: Callable[[], Any] | None = None,
    imprimir: Callable[[str], None] = print,
) -> int:
    analizador = argparse.ArgumentParser(description="Prueba manual de recuperación semántica real con OCI Embed 4.")
    analizador.add_argument("--ejecutar", action="store_true", help="Confirma que quiere hacer llamadas remotas a OCI.")
    argumentos = analizador.parse_args(argv)
    if not argumentos.ejecutar:
        imprimir("Prueba remota NO ejecutada: haría llamadas a OCI y consumiría créditos. Use --ejecutar para confirmar.")
        return EXIT_NO_EJECUTADO
    if crear_proveedor is None:
        from app.services.ingesta.proveedor_oci import ProveedorEmbeddingsOCI as crear_proveedor
    try:
        proveedor = crear_proveedor()
    except Exception as exc:
        imprimir(f"Configuración de OCI inválida o ilegible ({_seguro(exc)}). Revise el .env y el archivo de configuración de OCI.")
        return EXIT_NO_EJECUTADO
    llamadas = 0
    try:
        fallos, llamadas = asyncio.run(_ejecutar(proveedor, imprimir))
    except Exception as exc:
        imprimir(f"FALLO: la llamada remota o su validación fallaron ({_seguro(exc)}).")
        return EXIT_FALLO
    finally:
        cerrar = getattr(proveedor, "cerrar", None)
        if callable(cerrar):
            try:
                cerrar()
            except Exception as exc:
                imprimir(f"No se pudo cerrar el cliente de OCI ({_seguro(exc)}).")
    for fallo in fallos:
        imprimir(f"FALLO: {fallo}")
    if fallos:
        return EXIT_FALLO
    imprimir(f"OK: las tres preguntas recuperaron primero su documento (llamadas a OCI: {llamadas}). "
             "Solo demuestra estos ejemplos: no evalúa documentos GRI reales ni persistencia vectorial.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

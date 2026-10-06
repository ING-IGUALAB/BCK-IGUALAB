"""PRUEBA MANUAL de embeddings reales de OCI (Cohere Embed 4, 1536 componentes).

NO forma parte de pytest ni del arranque. Hace llamadas REMOTAS a OCI Generative AI (consume
créditos): solo se ejecuta con `--ejecutar`. Usa unos pocos textos sintéticos en español, nunca
documentos reales.

Uso (PowerShell, desde la raíz del repositorio, con el entorno virtual del proyecto):
    .venv\\Scripts\\python.exe -m scripts.smoke_embeddings_oci --ejecutar

Configuración: variables `OCI_*` del `.env` local (el módulo `app.config` lo carga) y el
archivo de configuración de OCI indicado por `OCI_CONFIG_FILE`/`OCI_CONFIG_PROFILE`. Este script
no lee ni imprime credenciales, llaves ni el `.env`.

Comprobaciones (todas deben pasar para devolver 0):
 1. La identidad del proveedor es `cohere.embed-v4.0` con dimensión 1536.
 2. La petición REAL al SDK lleva `output_dimensions=1536`, `truncate="NONE"`, el modelo
    `cohere.embed-v4.0` y `input_type="SEARCH_DOCUMENT"` (se observa lo que recibe el SDK; no se
    sustituye ni se simula nada).
 3. Los fragmentos se embeben a través del contrato de ingesta (`embeber_fragmentos`): cantidad
    exacta, dimensión exacta y componentes finitos en TODOS los vectores (se comprueba aquí de
    forma independiente de la validación del contrato).
 4. Una consulta (`embeber_consulta`, `SEARCH_QUERY`) produce un vector de 1536 componentes finitos.

Código de salida: 0 todo verificado; 1 alguna comprobación o la llamada remota falló; 2 no se
ejecutó (falta `--ejecutar`) o la configuración es inválida. Los mensajes solo llevan
identificadores seguros (nombre de la clase de la excepción o código de error): nunca
credenciales, textos, vectores ni respuestas completas del SDK. Los recursos del cliente se
cierran siempre. Nota: cancelar la espera asíncrona no detiene necesariamente la petición remota.
"""
import argparse
import asyncio
import math
import os
import sys
from collections.abc import Callable
from typing import Any

# `app.config` exige JWT_SECRET_KEY al importarse; este script no la usa.
os.environ.setdefault("JWT_SECRET_KEY", "valor-ficticio-solo-para-el-smoke-de-embeddings")

from app.services.ingesta.embeddings import embeber_consulta, embeber_fragmentos  # noqa: E402
from app.services.ingesta.fragmentacion import ParametrosFragmentacion, iterar_fragmentos  # noqa: E402

MODELO_ESPERADO = "cohere.embed-v4.0"
DIMENSION_ESPERADA = 1536
TEXTOS_SINTETICOS = (
    "# Informe sintético de sostenibilidad 2025\n\n"
    "Este texto es inventado para una prueba técnica de embeddings.\n\n"
    "## Agua\n\nLa empresa ficticia consumió una cantidad de agua de ejemplo durante el periodo.\n\n"
    "## Energía\n\nLa planta imaginaria utiliza energía renovable en una parte de su operación.\n\n"
    "## Residuos\n\nEl reciclaje de residuos descrito aquí es un dato de relleno sin valor real.\n"
)
CONSULTA_SINTETICA = "¿Cuánta agua consumió la empresa ficticia?"
TAMANO_LOTE = 4
TIMEOUT_SEGUNDOS = 60

EXIT_OK, EXIT_FALLO, EXIT_NO_EJECUTADO = 0, 1, 2


def _seguro(exc: BaseException) -> str:
    """Código de error de la aplicación o nombre de la clase; nunca `str(exc)`."""
    codigo = getattr(exc, "code", None)
    return codigo if isinstance(codigo, str) and codigo.isidentifier() else type(exc).__name__


def _vector_valido(vector: object) -> bool:
    return (
        isinstance(vector, (tuple, list))
        and len(vector) == DIMENSION_ESPERADA
        and all(isinstance(c, float) and math.isfinite(c) for c in vector)
    )


class _Observador:
    """Envuelve `embed_text` del SDK y registra, sin alterarla, la petición que recibe."""

    def __init__(self, cliente: Any) -> None:
        self.peticiones: list[dict[str, Any]] = []
        self._original = cliente.embed_text
        cliente.embed_text = self._llamar

    def _llamar(self, detalles, *args, **kwargs):
        self.peticiones.append(
            dict(
                modelo=getattr(getattr(detalles, "serving_mode", None), "model_id", None),
                output_dimensions=getattr(detalles, "output_dimensions", None),
                truncate=getattr(detalles, "truncate", None),
                input_type=getattr(detalles, "input_type", None),
                textos=len(getattr(detalles, "inputs", None) or []),
            )
        )
        return self._original(detalles, *args, **kwargs)


async def _comprobar(proveedor: Any, imprimir: Callable[[str], None]) -> list[str]:
    fallos: list[str] = []
    identidad = proveedor.identidad
    imprimir(f"[identidad] proveedor={identidad.proveedor} modelo={identidad.modelo} dimensión={identidad.dimension}")
    if (identidad.modelo, identidad.dimension) != (MODELO_ESPERADO, DIMENSION_ESPERADA):
        fallos.append(f"la identidad debe ser {MODELO_ESPERADO} con {DIMENSION_ESPERADA}; revise OCI_EMBED_MODEL y OCI_EMBED_DIMENSIONS")
        return fallos  # no se hacen llamadas remotas con una configuración que no es la acordada

    observador = _Observador(proveedor._client)

    fragmentos = list(iterar_fragmentos(TEXTOS_SINTETICOS, ParametrosFragmentacion(max_caracteres=90, max_caracteres_contexto=140)))
    vectores = []
    async for lote in embeber_fragmentos(iter(fragmentos), proveedor, tamano_lote=TAMANO_LOTE, timeout_segundos=TIMEOUT_SEGUNDOS):
        vectores.extend(e.vector for e in lote.elementos)
    dimensiones = sorted({len(v) for v in vectores})
    imprimir(
        f"[fragmentos] solicitados={len(fragmentos)} vectores={len(vectores)} "
        f"dimensión solicitada={DIMENSION_ESPERADA} dimensiones recibidas={dimensiones}"
    )
    if len(vectores) != len(fragmentos) or not fragmentos:
        fallos.append(f"cantidad de vectores incorrecta ({len(vectores)} para {len(fragmentos)} fragmentos)")
    if not all(_vector_valido(v) for v in vectores):
        fallos.append("algún vector no tiene exactamente 1536 componentes numéricas finitas")

    consulta = await embeber_consulta(CONSULTA_SINTETICA, proveedor, timeout_segundos=TIMEOUT_SEGUNDOS)
    imprimir(f"[consulta] vectores=1 dimensiones recibidas=[{len(consulta)}]")
    if not _vector_valido(consulta):
        fallos.append("el vector de consulta no tiene exactamente 1536 componentes numéricas finitas")

    esperadas = {"documento": "SEARCH_DOCUMENT", "consulta": "SEARCH_QUERY"}
    for peticion in observador.peticiones:
        if peticion["modelo"] != MODELO_ESPERADO:
            fallos.append("una petición no llevó el modelo cohere.embed-v4.0")
        if peticion["output_dimensions"] != DIMENSION_ESPERADA:
            fallos.append("una petición no configuró output_dimensions=1536")
        if peticion["truncate"] != "NONE":
            fallos.append('una petición no configuró truncate="NONE"')
    tipos = [p["input_type"] for p in observador.peticiones]
    if not observador.peticiones or tipos[:-1] != ["SEARCH_DOCUMENT"] * (len(tipos) - 1) or tipos[-1] != esperadas["consulta"]:
        fallos.append("los tipos de entrada enviados no fueron SEARCH_DOCUMENT (fragmentos) y SEARCH_QUERY (consulta)")
    imprimir(
        f"[peticiones] {len(observador.peticiones)} · modelo={MODELO_ESPERADO} · output_dimensions="
        f"{sorted({p['output_dimensions'] for p in observador.peticiones})} · truncate="
        f"{sorted({str(p['truncate']) for p in observador.peticiones})} · input_type={sorted(set(tipos))}"
    )
    return fallos


def main(
    argv: list[str] | None = None,
    crear_proveedor: Callable[[], Any] | None = None,
    imprimir: Callable[[str], None] = print,
) -> int:
    analizador = argparse.ArgumentParser(description="Prueba manual de embeddings reales de OCI (Embed 4, 1536).")
    analizador.add_argument("--ejecutar", action="store_true", help="Confirma que quiere hacer llamadas remotas a OCI.")
    argumentos = analizador.parse_args(argv)
    if not argumentos.ejecutar:
        imprimir("Prueba remota NO ejecutada: haría llamadas a OCI y consumiría créditos. Use --ejecutar para confirmar.")
        return EXIT_NO_EJECUTADO

    if crear_proveedor is None:
        from app.services.ingesta.proveedor_oci import ProveedorEmbeddingsOCI as crear_proveedor
    try:
        proveedor = crear_proveedor()
    except Exception as exc:  # configuración o archivo de OCI inválido: solo se informa la clase
        imprimir(f"Configuración de OCI inválida o ilegible ({_seguro(exc)}). Revise el .env y el archivo de configuración de OCI.")
        return EXIT_NO_EJECUTADO
    try:
        fallos = asyncio.run(_comprobar(proveedor, imprimir))
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
    imprimir(f"OK: {MODELO_ESPERADO} devuelve vectores de {DIMENSION_ESPERADA} componentes finitas, a través del contrato.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

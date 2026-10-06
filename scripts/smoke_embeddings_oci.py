"""Smoke test: embeddings reales de OCI a través del contrato de ingesta.

Uso (desde la raíz del repo, con el venv activado):
    PYTHONPATH=. python scripts/smoke_embeddings_oci.py
"""
import asyncio

from app.services.ingesta.embeddings import embeber_fragmentos
from app.services.ingesta.fragmentacion import ParametrosFragmentacion, iterar_fragmentos
from app.services.ingesta.proveedor_oci import ProveedorEmbeddingsOCI

TEXTO = (
    "# Memoria Anual 2025\n\n"
    "Introduccion con contenido de prueba para validar los embeddings.\n\n"
    "## Agua\n\nConsumo total de agua de la empresa durante el periodo.\n\n"
    "## Energia\n\nConsumo energetico y fuentes renovables.\n"
)


async def main() -> None:
    proveedor = ProveedorEmbeddingsOCI()
    print("[identidad]", proveedor.identidad)

    parametros = ParametrosFragmentacion(max_caracteres=80, max_caracteres_contexto=120)
    total = 0
    lotes = 0
    async for lote in embeber_fragmentos(
        iterar_fragmentos(TEXTO, parametros),
        proveedor,
        tamano_lote=4,
        timeout_segundos=30,
    ):
        lotes += 1
        for elemento in lote.elementos:
            total += 1
            print(
                f"  fragmento {elemento.fragmento.indice}: "
                f"dim={len(elemento.vector)} prim={elemento.vector[:3]}"
            )

    print(f"[resumen] lotes={lotes} fragmentos={total}")
    print("\nOK: el modelo de embeddings de OCI funciona a traves del contrato.")


if __name__ == "__main__":
    asyncio.run(main())

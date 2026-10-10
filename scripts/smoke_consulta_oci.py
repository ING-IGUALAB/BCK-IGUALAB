"""Smoke test: embedding de CONSULTA real (SEARCH_QUERY) contra OCI.

Verifica el contrato asimétrico: la ingesta indexa con SEARCH_DOCUMENT y la
consulta busca con SEARCH_QUERY. Usa las mismas credenciales/config que la
ingesta (OCI_CONFIG_FILE / OCI_CONFIG_PROFILE o variables OCI_*).

Uso (desde la raíz del repo, con el venv activado):
    PYTHONPATH=. python scripts/smoke_consulta_oci.py
"""
import asyncio

from app.services.ingesta.embeddings import embeber_consulta
from app.services.ingesta.proveedor_oci import ProveedorEmbeddingsOCI

CONSULTA = "¿Cuál fue el consumo total de agua en 2025?"


async def main() -> None:
    proveedor = ProveedorEmbeddingsOCI()
    print("[identidad]", proveedor.identidad)

    vector = await embeber_consulta(CONSULTA, proveedor, timeout_segundos=30)
    print(f"[consulta] dim={len(vector)} prim={vector[:3]}")

    assert len(vector) == proveedor.identidad.dimension, "La dimensión no coincide con la identidad"
    print("\nOK: el embedding de consulta (SEARCH_QUERY) funciona a traves del contrato.")


if __name__ == "__main__":
    asyncio.run(main())

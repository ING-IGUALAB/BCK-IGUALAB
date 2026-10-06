"""Adaptador real de embeddings contra OCI Generative AI (Cohere).

Implementa el contrato `ProveedorEmbeddings` definido en `embeddings.py`.
El SDK de OCI es síncrono, por lo que la llamada se ejecuta en un hilo para no
bloquear el bucle de eventos (el contrato es asíncrono).
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

import oci
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import EmbedTextDetails, OnDemandServingMode

from app.config import settings
from app.services.ingesta.embeddings import IdentidadEmbeddings


class ProveedorEmbeddingsOCI:
    """Proveedor de embeddings respaldado por OCI Generative AI (Cohere)."""

    def __init__(self) -> None:
        if not settings.OCI_COMPARTMENT_ID:
            raise ValueError("Falta OCI_COMPARTMENT_ID en el entorno.")

        self.identidad = IdentidadEmbeddings(
            proveedor="oci-cohere",
            modelo=settings.OCI_EMBED_MODEL,
            dimension=settings.OCI_EMBED_DIMENSIONS,
        )

        config = oci.config.from_file(
            os.path.expanduser(settings.OCI_CONFIG_FILE),
            settings.OCI_CONFIG_PROFILE,
        )
        self._client = GenerativeAiInferenceClient(
            config=config,
            service_endpoint=(
                f"https://inference.generativeai.{settings.OCI_REGION}.oci.oraclecloud.com"
            ),
        )

    async def generar_embeddings(self, textos: Sequence[str]) -> Sequence[Sequence[float]]:
        """Devuelve un vector por texto, en el mismo orden."""
        def _llamar() -> list[list[float]]:
            respuesta = self._client.embed_text(
                EmbedTextDetails(
                    compartment_id=settings.OCI_COMPARTMENT_ID,
                    serving_mode=OnDemandServingMode(model_id=settings.OCI_EMBED_MODEL),
                    inputs=list(textos),
                    input_type="SEARCH_DOCUMENT",
                )
            )
            return [list(vector) for vector in respuesta.data.embeddings]

        return await asyncio.to_thread(_llamar)

"""Adaptador real de embeddings contra OCI Generative AI (Cohere Embed 4).

Implementa el contrato `ProveedorEmbeddings` (y `ProveedorEmbeddingsConsulta`) de
`embeddings.py`. No es un segundo flujo: la validación de cantidad, dimensión y valores
finitos de la respuesta, los errores controlados y el timeout por lote los aplica
`embeddings.py`.

CONFIGURACIÓN ACORDADA (preparada; la verificación real es la prueba manual
`scripts/smoke_embeddings_oci.py`): modelo `cohere.embed-v4.0`, región `us-chicago-1`,
dimensión 1536. La dimensión NO solo se declara en `IdentidadEmbeddings`: se ENVÍA en cada
petición (`output_dimensions`) y la respuesta se valida contra ella. `input_type` es
`SEARCH_DOCUMENT` para fragmentos y `SEARCH_QUERY` para consultas. `truncate="NONE"`: un
texto demasiado largo produce un error del servicio en lugar de un recorte silencioso.

LÍMITES. No se trasladan los de v3 ni los de la consola (512 tokens por texto, 96 textos).
Según la documentación de OCI para Embed 4, en el SDK/API el límite es de 128 000 tokens de
entrada en total por petición; las dimensiones admitidas son 256, 512, 1024 y 1536. El
número máximo de textos por petición vía API no está confirmado: el tamaño de lote lo decide
quien llama (`tamano_lote`) y debe validarse con la prueba real.

PLAZOS Y REINTENTOS. Plazos explícitos de conexión y lectura (`ParametrosIngesta.oci_connect_timeout_segundos`
y `oci_read_timeout_segundos`, fijos en código y aplicados por el SDK) y `oci.retry.NoneRetryStrategy()`: una sola
petición, sin reintentos, hasta decidir una política (primera prueba).

CANCELACIÓN. El SDK de OCI es síncrono y se ejecuta en un hilo (`asyncio.to_thread`).
Cancelar la espera asíncrona (o su `asyncio.timeout`) NO detiene necesariamente el hilo ni
la petición remota: puede seguir hasta su plazo de lectura y consumir créditos aunque nadie
espere ya el resultado.

SECRETOS. Este módulo no registra ni imprime configuración, credenciales ni textos. La
identidad OCI se lee del archivo `OCI_CONFIG_FILE` y su perfil; la llave privada vive fuera
del repositorio (`.oci/` y `*.pem` están ignorados). En contenedores sin ese archivo se arma
la identidad EN MEMORIA con `OCI_USER_OCID`, `OCI_FINGERPRINT`, `OCI_TENANCY_OCID`, `OCI_REGION`
y `OCI_KEY_PEM_B64` (llave PEM en base64); el archivo, si existe, tiene prioridad.
"""
from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Sequence

import oci
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import EmbedTextDetails, OnDemandServingMode

from app.config import settings
from app.services.ingesta.embeddings import IdentidadEmbeddings
from app.services.ingesta.parametros import PARAMETROS_INGESTA, ParametrosIngesta

DIMENSIONES_EMBED_V4 = (256, 512, 1024, 1536)


_VARIABLES_IDENTIDAD = ("OCI_USER_OCID", "OCI_FINGERPRINT", "OCI_TENANCY_OCID", "OCI_KEY_PEM_B64")


def _identidad_desde_variables() -> dict | None:
    """Identidad OCI armada EN MEMORIA con las variables de entorno (contenedores sin archivo `~/.oci/config`).
    `None` si no están las cuatro. La llave privada llega en base64 de una sola línea (`OCI_KEY_PEM_B64`); no se
    escribe en disco ni se registra, y los errores nunca repiten su valor."""
    valores = {nombre: getattr(settings, nombre, None) for nombre in _VARIABLES_IDENTIDAD}
    if not all(isinstance(v, str) and v.strip() for v in valores.values()):
        return None
    try:
        llave = base64.b64decode(valores["OCI_KEY_PEM_B64"].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("OCI_KEY_PEM_B64 debe ser la llave privada PEM en base64 de una sola línea.") from None
    config = {
        "user": valores["OCI_USER_OCID"].strip(),
        "fingerprint": valores["OCI_FINGERPRINT"].strip(),
        "tenancy": valores["OCI_TENANCY_OCID"].strip(),
        "region": settings.OCI_REGION,
        "key_content": llave,
    }
    oci.config.validate_config(config)
    return config


def _cargar_identidad() -> dict:
    """El archivo `OCI_CONFIG_FILE` (con su perfil) manda si existe; si no, las variables `OCI_*` (ver arriba); si
    tampoco están completas, se intenta el archivo y el SDK informa que falta."""
    ruta = os.path.expanduser(settings.OCI_CONFIG_FILE)
    if not os.path.isfile(ruta):
        config = _identidad_desde_variables()
        if config is not None:
            return config
    return oci.config.from_file(ruta, settings.OCI_CONFIG_PROFILE)


class ProveedorEmbeddingsOCI:
    """Proveedor de embeddings respaldado por OCI Generative AI (Cohere Embed 4)."""

    def __init__(self, parametros: ParametrosIngesta | None = None) -> None:
        parametros = parametros if parametros is not None else PARAMETROS_INGESTA
        if not settings.OCI_COMPARTMENT_ID:
            raise ValueError("Falta OCI_COMPARTMENT_ID en el entorno.")
        dimension = settings.OCI_EMBED_DIMENSIONS
        if dimension not in DIMENSIONES_EMBED_V4:
            raise ValueError(f"OCI_EMBED_DIMENSIONS debe ser una de {DIMENSIONES_EMBED_V4}.")
        conexion = parametros.oci_connect_timeout_segundos
        lectura = parametros.oci_read_timeout_segundos

        self.identidad = IdentidadEmbeddings(
            proveedor="oci-cohere",
            modelo=settings.OCI_EMBED_MODEL,
            dimension=dimension,
        )

        config = _cargar_identidad()
        self._client = GenerativeAiInferenceClient(
            config=config,
            service_endpoint=(
                f"https://inference.generativeai.{settings.OCI_REGION}.oci.oraclecloud.com"
            ),
            timeout=(conexion, lectura),
            retry_strategy=oci.retry.NoneRetryStrategy(),
        )

    def _detalles(self, textos: Sequence[str], input_type: str) -> EmbedTextDetails:
        return EmbedTextDetails(
            compartment_id=settings.OCI_COMPARTMENT_ID,
            serving_mode=OnDemandServingMode(model_id=self.identidad.modelo),
            inputs=list(textos),
            input_type=input_type,
            output_dimensions=self.identidad.dimension,  # enviada, no solo declarada
            truncate=EmbedTextDetails.TRUNCATE_NONE,  # sin recortes silenciosos
        )

    def _embed(self, textos: Sequence[str], input_type: str) -> list[list[float]]:
        respuesta = self._client.embed_text(self._detalles(textos, input_type))
        return [list(vector) for vector in respuesta.data.embeddings]

    async def generar_embeddings(self, textos: Sequence[str]) -> Sequence[Sequence[float]]:
        """Ingesta: un vector por texto, en el mismo orden (`SEARCH_DOCUMENT`)."""
        return await asyncio.to_thread(self._embed, textos, "SEARCH_DOCUMENT")

    async def generar_embeddings_consulta(self, textos: Sequence[str]) -> Sequence[Sequence[float]]:
        """Consulta: `SEARCH_QUERY` (asimétrico respecto a la ingesta)."""
        return await asyncio.to_thread(self._embed, textos, "SEARCH_QUERY")

    def cerrar(self) -> None:
        """Libera la sesión HTTP del SDK. Idempotente; no falla si el SDK no la expone."""
        sesion = getattr(getattr(self._client, "base_client", None), "session", None)
        cerrar = getattr(sesion, "close", None)
        if callable(cerrar):
            cerrar()

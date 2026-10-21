"""Cliente LLM del Asistente IA (Módulo 6). Adaptador de INFRAESTRUCTURA: único punto que habla
con el proveedor de chat (DeepSeek, OpenAI-compatible). Se inyecta como protocolo para poder
probar sin red (tests con dobles)."""
from __future__ import annotations

from typing import Protocol

import httpx

from app.core.config import settings
from app.core.exceptions import ExternalServiceError


class ProveedorLlm(Protocol):
    """Capacidad mínima de un modelo de chat."""

    async def completar(self, sistema: str, usuario: str) -> str:
        """Devuelve el texto de la respuesta del modelo."""
        ...


class DeepSeekLlm:
    """Cliente de chat contra DeepSeek (endpoint OpenAI-compatible `/chat/completions`)."""

    def __init__(self, api_key: str, base_url: str, modelo: str, timeout_segundos: float = 60.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._modelo = modelo
        self._timeout = timeout_segundos

    async def completar(self, sistema: str, usuario: str) -> str:
        cuerpo = {
            "model": self._modelo,
            "messages": [
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
            "temperature": 0.2,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=cuerpo,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as exc:  # red / timeout
            raise ExternalServiceError(
                "LLM_PROVIDER_ERROR", "El asistente no está disponible: error del proveedor de IA."
            ) from exc
        if r.status_code != 200:
            raise ExternalServiceError(
                "LLM_PROVIDER_ERROR", "El asistente no está disponible: error del proveedor de IA."
            )
        try:
            contenido = r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise ExternalServiceError(
                "LLM_INVALID_RESPONSE", "El asistente no está disponible: respuesta inválida del proveedor de IA."
            ) from exc
        return contenido if isinstance(contenido, str) else ""


def construir_llm_desde_config() -> ProveedorLlm:
    """Instancia el cliente LLM real desde la configuración (`DEEPSEEK_*`)."""
    return DeepSeekLlm(
        api_key=settings.DEEPSEEK_API_KEY or "",
        base_url=settings.DEEPSEEK_BASE_URL,
        modelo=settings.DEEPSEEK_MODEL,
    )


def llm_configurado() -> bool:
    """True si hay API key de chat configurada (si no, el módulo responde 503)."""
    return bool(settings.DEEPSEEK_API_KEY)

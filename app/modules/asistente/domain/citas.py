"""Post-verificación de citas del Asistente IA (RN-034). Regla pura: sin BD, sin HTTP, sin LLM.

El modelo debe citar las fuentes con `[n]`. Si cita un `n` que NO existe en el contexto
recuperado, esa cita se DESCARTA (no se inserta) y se retira el marcador del texto: nunca
se entregan citas que no se puedan verificar contra el corpus.
"""
from __future__ import annotations

import re

from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado

_REFERENCIA = re.compile(r"\[\s*(\d{1,3})\s*\]")


def extraer_referencias(texto: str) -> set[int]:
    """Números citados como [n] en el texto."""
    return {int(m) for m in _REFERENCIA.findall(texto)}


def procesar_citas(
    texto: str,
    fragmentos: list[FragmentoRecuperado],
) -> tuple[str, list[FragmentoRecuperado]]:
    """Limpia el texto y devuelve `(texto_limpio, fragmentos_citados)`.

    - Retira del texto los marcadores `[n]` con `n` fuera del rango 1..len(fragmentos).
    - Devuelve los fragmentos citados (en el orden del contexto), sin duplicados.
    """
    total = len(fragmentos)
    citados: dict[int, FragmentoRecuperado] = {}

    def _reemplazar(match: re.Match) -> str:
        n = int(match.group(1))
        if 1 <= n <= total:
            citados[n] = fragmentos[n - 1]
            return match.group(0)  # se conserva la cita válida
        return ""  # cita no verificable: se retira

    texto_limpio = _REFERENCIA.sub(_reemplazar, texto).strip()
    return texto_limpio, [citados[n] for n in sorted(citados)]

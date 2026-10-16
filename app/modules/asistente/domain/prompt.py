"""Construcción del prompt del Asistente IA (RN-033/034/035/036). Regla pura: sin BD, sin HTTP, sin LLM."""
from __future__ import annotations

from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado

SISTEMA = (
    "Eres el asistente de Igualab, plataforma de analítica de sostenibilidad (estándares GRI, "
    "sanciones económicas e indicadores ASG). REGLAS: "
    "1) Responde EXCLUSIVAMENTE con la información del CONTEXTO que se te entrega; no uses conocimiento propio. "
    "2) Cita la fuente de cada afirmación con el número entre corchetes, p. ej. [1] o [2][3]. "
    "3) Si el contexto no contiene la respuesta, di explícitamente que no tienes evidencia en el corpus; no inventes. "
    "4) No respondas temas ajenos a sostenibilidad/ASG; si te preguntan otra cosa, declara que está fuera de tu dominio. "
    "5) Responde en español, de forma clara y concreta."
)


def construir_contexto(fragmentos: list[FragmentoRecuperado]) -> str:
    """Convierte los fragmentos recuperados en un contexto numerado [1..n] con su sección."""
    partes: list[str] = []
    for i, f in enumerate(fragmentos, start=1):
        seccion = f.seccion or "(sin sección)"
        partes.append(f"[{i}] (año {f.anio}, sección: {seccion})\n{f.texto_literal}")
    return "\n\n".join(partes)


def construir_prompt_usuario(pregunta: str, fragmentos: list[FragmentoRecuperado]) -> str:
    contexto = construir_contexto(fragmentos)
    return (
        f"CONTEXTO (fragmentos del corpus; cita con su número):\n\n{contexto}\n\n"
        f"---\nPREGUNTA DEL USUARIO:\n{pregunta}\n\n"
        "Respuesta (con citas [n] del contexto):"
    )


def validar_pregunta(pregunta: str) -> None:
    """La pregunta no puede estar vacía ni ser solo espacios (entrada de la API)."""
    if not isinstance(pregunta, str) or not pregunta.strip():
        raise ValueError("La pregunta no puede estar vacía.")

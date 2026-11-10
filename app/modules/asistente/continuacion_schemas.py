"""Contratos HTTP de la CONTINUACIÓN de una conversación guardada (Módulo 6, CU004) — ENTREGA 2.

Una pregunta nueva se interpreta con un contexto conversacional ACOTADO (los últimos 4 intercambios, 12 000 caracteres), se responde
con el RAG actual dentro de la empresa, el sector y el año que ya tiene la conversación, y el servidor guarda la pregunta ORIGINAL y
la respuesta completa. La petición solo trae la pregunta y la clave de idempotencia: no acepta empresa, sector, año, usuario,
ambiente, respuestas, citas, procedencia, orden ni historial reenviado.

ALCANCE (entrega 3). El historial solo ayuda a INTERPRETAR referencias («¿y cuál fue la de mayor importe?»): no es evidencia, no se
reutilizan citas ni respuestas anteriores y la respuesta se recupera y verifica de nuevo con los documentos actuales. No hay resúmenes
persistentes ni embeddings del historial. Cambiar de empresa, sector o año exige una conversación nueva.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.asistente.domain.historial import MAX_PREGUNTA, problemas_de_pregunta
from app.modules.asistente.historial_schemas import MensajeGuardado


class ContinuarConversacionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clave_idempotencia: uuid.UUID = Field(
        description="UUID que genera el frontend para ESTE envío y reutiliza en todos sus reintentos y dobles clics. Se delimita por "
        "conversación: la misma clave con la misma pregunta devuelve el intercambio ya completado (200) o informa de que sigue en "
        "curso (409); con otra pregunta responde 409. Una pregunta nueva exige una clave nueva."
    )
    pregunta: str = Field(
        min_length=1, max_length=MAX_PREGUNTA, description="Pregunta en lenguaje natural, de hasta 2 000 caracteres. Puede referirse a mensajes anteriores de la conversación; "
        "esa referencia se resuelve en el servidor y la pregunta persistida es esta, la original."
    )

    @field_validator("pregunta")
    @classmethod
    def _pregunta_valida(cls, valor: str) -> str:
        problemas = problemas_de_pregunta(valor)  # los mensajes describen la regla; nunca repiten la pregunta
        if problemas:
            raise ValueError("; ".join(problemas))
        return valor


class IntercambioGuardado(BaseModel):
    conversacion_id: uuid.UUID
    clave_idempotencia: uuid.UUID
    pregunta: MensajeGuardado = Field(description="La pregunta persistida (`rol: usuario`, `procedencia: ENVIADO_POR_USUARIO`).")
    respuesta: MensajeGuardado = Field(
        description="La respuesta persistida (`rol: asistente`, `procedencia: GENERADO_POR_SERVIDOR`). Su campo `respuesta` es la "
        "respuesta estructurada COMPLETA del asistente: afirmaciones, citas, `sin_evidencia`, `motivo`, `mensaje` y descartes. Si no "
        "hubo evidencia, `contenido` es la aclaración."
    )
    total_mensajes: int = Field(description="Mensajes que tiene ahora la conversación (máximo 40).")
    actualizada_en: datetime = Field(description="Fecha de actualización ACTUAL de la conversación.")
    reutilizado: bool = Field(
        description="False: este intercambio se acaba de crear (HTTP 201). True: ya estaba completado con la misma clave y la misma "
        "pregunta y se devuelve lo almacenado, sin consultar a la IA (HTTP 200)."
    )

"""Contratos HTTP del historial del Asistente IA (Módulo 6, CU004) — ENTREGA 1: guardar y recuperar conversaciones.

PROCEDENCIA (IMPORTANTE). El asistente no conserva lo que responde: el frontend aporta el historial al guardar. Validar su
estructura NO demuestra que lo generó el backend ni que sus citas se verificaron originalmente, así que cada mensaje se devuelve
con `procedencia = APORTADO_POR_CLIENTE`, valor que fija el servidor. No es una respuesta autenticada, una validación oficial ni un
registro de auditoría.

Esta entrega NO continúa conversaciones (eso lo hace la entrega 2, `continuacion_*`): el historial no se envía al modelo ni se usa como memoria.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import SectorEmpresa
from app.models.asistente import ProcedenciaMensaje
from app.modules.asistente.domain.historial import (
    MAX_MENSAJES,
    MAX_PREGUNTA,
    problemas_de_pregunta,
    problemas_de_respuesta,
)
from app.modules.asistente.schemas import ConsultaResponse

# ------------------------------------------------------------------ Entrada ------------------------------------------------------------------


class MensajeUsuarioEntrada(BaseModel):
    """Pregunta del usuario."""

    model_config = ConfigDict(extra="forbid")

    rol: Literal["usuario"]
    contenido: str = Field(min_length=1, max_length=MAX_PREGUNTA, description="Pregunta tal como se envió a `POST /asistente/consultas`.")


class MensajeAsistenteEntrada(BaseModel):
    """Respuesta del asistente: el cuerpo EXACTO que devolvió `POST /asistente/consultas` (con citas y afirmaciones, o sin evidencia).
    El texto del mensaje lo deriva el servidor de esa respuesta."""

    model_config = ConfigDict(extra="forbid")

    rol: Literal["asistente"]
    respuesta: ConsultaResponse


# Solo estos dos roles: cualquier otro (`system`, `developer`, `tool`…) se rechaza con 422.
MensajeEntrada = Annotated[Union[MensajeUsuarioEntrada, MensajeAsistenteEntrada], Field(discriminator="rol")]


class GuardarConversacionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clave_idempotencia: uuid.UUID = Field(
        description="UUID que genera el frontend al abrir la acción «Guardar conversación» y reutiliza en cada reintento. "
        "Se delimita por usuario y ambiente: la misma clave con el mismo contenido devuelve la conversación ya guardada (200); "
        "con contenido distinto responde 409."
    )
    empresa_id: uuid.UUID = Field(description="Empresa de TODA la conversación.")
    sector: SectorEmpresa = Field(description="Sector de esa empresa (se contrasta en el servidor).")
    anio: int = Field(ge=2000, le=9999, description="Año de los documentos consultados.")
    mensajes: list[MensajeEntrada] = Field(
        min_length=2,
        max_length=MAX_MENSAJES,
        description="Pares pregunta (`usuario`) / respuesta (`asistente`) en orden cronológico; empieza por el usuario y termina "
        "con el asistente. El orden de la lista es el orden de la conversación.",
    )

    @model_validator(mode="after")
    def _validar_conversacion(self) -> "GuardarConversacionRequest":
        # Los mensajes de error describen la regla; nunca incluyen el contenido recibido.
        if len(self.mensajes) % 2 != 0:
            raise ValueError("la conversación debe estar formada por pares pregunta/respuesta")
        problemas: list[str] = []
        for posicion, mensaje in enumerate(self.mensajes):
            esperado = "usuario" if posicion % 2 == 0 else "asistente"
            if mensaje.rol != esperado:
                raise ValueError("los mensajes deben alternar usuario y asistente, empezando por el usuario")
            if isinstance(mensaje, MensajeUsuarioEntrada):
                problemas += problemas_de_pregunta(mensaje.contenido)
            else:
                problemas += problemas_de_respuesta(mensaje.respuesta, self.empresa_id, self.anio)
        if problemas:
            raise ValueError("; ".join(dict.fromkeys(problemas)))
        return self


# ------------------------------------------------------------------ Salida ------------------------------------------------------------------


class ContextoConversacion(BaseModel):
    empresa_id: uuid.UUID
    empresa_nombre: str = Field(description="Nombre ACTUAL de la empresa.")
    empresa_activa: bool = Field(
        description="Estado ACTUAL de la empresa. Una conversación de una empresa desactivada se puede recuperar (es historial), pero "
        "eso NO autoriza nuevas consultas: `POST /asistente/consultas` sigue exigiendo empresa activa."
    )
    sector: SectorEmpresa = Field(description="Sector guardado con la conversación.")
    anio: int


class MensajeGuardado(BaseModel):
    id: uuid.UUID
    orden: int = Field(description="Posición en la conversación, desde 1. Único por conversación.")
    rol: Literal["usuario", "asistente"]
    contenido: str = Field(description="Pregunta, o texto legible de la respuesta (o la aclaración si no hubo evidencia).")
    respuesta: ConsultaResponse | None = Field(
        default=None, description="Solo en mensajes del asistente: la respuesta estructurada (citas, afirmaciones, `sin_evidencia`…)."
    )
    procedencia: ProcedenciaMensaje = Field(
        description="Definida por el servidor. APORTADO_POR_CLIENTE: historial enviado por el frontend; su estructura se validó, pero no "
        "se demuestra que lo generó el backend ni que sus citas se verificaron originalmente. ENVIADO_POR_USUARIO: pregunta enviada a "
        "una conversación guardada. GENERADO_POR_SERVIDOR: respuesta que generó, verificó y persistió el RAG del servidor."
    )
    creado_en: datetime


class ConversacionResumen(BaseModel):
    id: uuid.UUID
    titulo: str = Field(description="Primera pregunta, normalizada y recortada a 80 caracteres (regla determinista).")
    contexto: ContextoConversacion
    total_mensajes: int
    creada_en: datetime
    actualizada_en: datetime


class ConversacionDetalle(ConversacionResumen):
    mensajes: list[MensajeGuardado] = Field(description="En orden estable por `orden`. Máximo 40.")


class ConversacionGuardada(ConversacionDetalle):
    reutilizada: bool = Field(
        default=False,
        description="True si NO se creó una conversación nueva: la misma clave de idempotencia y el mismo contenido ya estaban guardados "
        "(HTTP 200).",
    )


class PaginaConversaciones(BaseModel):
    items: list[ConversacionResumen] = Field(description="Metadatos de cada conversación, sin mensajes.")
    pagina: int = Field(description="Página devuelta (base 1).")
    tamano: int = Field(description="Tamaño de página.")
    total: int = Field(description="Total de conversaciones del usuario en este ambiente.")

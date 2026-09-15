"""
Modelos del módulo de Auditoría — vive aparte porque es TRANSVERSAL: todos
los módulos futuros (Ingesta, Análisis GRI, Reportes) van a escribir aquí,
no solo Usuarios. Por eso no se puso dentro de usuarios.py, aunque hoy sea
el único módulo que ya lo usa.

  - RN-027 / RN-028: qué se audita y qué campos guarda cada registro.
  - RN-029 / RNF-013: inmutabilidad — ver la nota dentro de RegistroAuditoria.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TipoEventoAuditoria(str, enum.Enum):
    """RN-027: catálogo cerrado de eventos que sí se auditan (Configuración
    y Actualización BD quedaron fuera de alcance, según lo confirmado).
    Cuando se agreguen los módulos de Ingesta/Reportes, sus servicios
    seguirán usando ESTOS mismos valores — no hace falta un enum nuevo
    por módulo."""
    INICIO_SESION = "Inicio de sesión"
    CAMBIO_ROL = "Cambio de rol"
    INGESTA_DATOS = "Ingesta de datos"
    RECHAZO_DOCUMENTO = "Rechazo de documento"
    GENERACION_REPORTE = "Generación de reporte"
    DESCARGA = "Descarga"


class RegistroAuditoria(Base):
    """
    RN-028: cuenta + fecha/hora + tipo de acción, como mínimo.
    RN-029 / RNF-013: solo inserción — nunca se expone un UPDATE ni DELETE
    para esta tabla en ningún servicio de ningún módulo (la ausencia de esas
    operaciones en el código ES el mecanismo de inmutabilidad).
    RNF-034: se guarda en UTC; la conversión a America/Lima es solo de
    presentación.
    """
    __tablename__ = "auditoria"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    tipo_evento: Mapped[TipoEventoAuditoria] = mapped_column(SAEnum(TipoEventoAuditoria), nullable=False)
    detalle: Mapped[str] = mapped_column(String(500), nullable=False)
    fecha_hora_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

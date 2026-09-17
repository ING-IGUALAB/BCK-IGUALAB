
import enum
import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TipoEventoAuditoria(str, enum.Enum):

    INICIO_SESION = "Inicio de sesión"
    CAMBIO_ROL = "Cambio de rol"
    INGESTA_DATOS = "Ingesta de datos"
    RECHAZO_DOCUMENTO = "Rechazo de documento"
    GENERACION_REPORTE = "Generación de reporte"
    DESCARGA = "Descarga"


class RegistroAuditoria(Base):

    __tablename__ = "auditoria"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    tipo_evento: Mapped[TipoEventoAuditoria] = mapped_column(SAEnum(TipoEventoAuditoria), nullable=False)
    detalle: Mapped[str] = mapped_column(String(500), nullable=False)
    fecha_hora_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

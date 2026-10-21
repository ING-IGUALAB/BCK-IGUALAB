
import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, DateTime, ForeignKey, Enum as SAEnum, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TipoEventoAuditoria(str, enum.Enum):

    INICIO_SESION = "Inicio de sesión"
    CAMBIO_ROL = "Cambio de rol"
    INGESTA_DATOS = "Ingesta de datos"
    RECHAZO_DOCUMENTO = "Rechazo de documento"
    GENERACION_REPORTE = "Generación de reporte"
    DESCARGA = "Descarga"
    # RN-045: intento de una cuenta distinta del SuperAdmin de acceder a la auditoría. Se agrega al tipo enumerado de PostgreSQL con
    # la migración 0005 (en una base nueva lo crea `create_all` junto con el resto de valores).
    ACCESO_DENEGADO = "Acceso denegado"


class ResultadoAuditoria(str, enum.Enum):
    """Resultado del evento (LTX:RN-043). EXITOSO: la acción se completó. FALLIDO: se intentó y no se completó (credenciales
    inválidas, ingesta rechazada o fallida). DENEGADO: la acción no se permitió (rol, cuenta bloqueada o deshabilitada)."""

    EXITOSO = "EXITOSO"
    FALLIDO = "FALLIDO"
    DENEGADO = "DENEGADO"


class RegistroAuditoria(Base):

    __tablename__ = "auditoria"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("usuarios.id"), nullable=True)
    tipo_evento: Mapped[TipoEventoAuditoria] = mapped_column(SAEnum(TipoEventoAuditoria), nullable=False)
    detalle: Mapped[str] = mapped_column(String(500), nullable=False)
    fecha_hora_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Nulo SOLO en los eventos anteriores a la migración 0005: su resultado no se deduce del detalle y no se inventa.
    resultado: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint("resultado IS NULL OR resultado IN ('EXITOSO', 'FALLIDO', 'DENEGADO')", name="ck_auditoria_resultado_valido"),
    )

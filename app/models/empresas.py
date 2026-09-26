import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Index, String, func, true
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SectorEmpresa(str, enum.Enum):
    MINERIA = "MINERIA"
    PETROLEO = "PETROLEO"
    ENERGIA = "ENERGIA"


class Empresa(Base):
    __tablename__ = "empresas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    sector: Mapped[SectorEmpresa] = mapped_column(
        Enum(SectorEmpresa, name="sector_empresa", validate_strings=True), nullable=False
    )
    activa: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(trim(nombre)) > 0", name="ck_empresas_nombre_no_vacio"),
        Index("uq_empresas_nombre_normalizado", func.lower(func.trim(nombre)), unique=True),
    )

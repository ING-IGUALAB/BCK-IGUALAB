
from __future__ import annotations
import enum
import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Enum as SAEnum, func, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RolUsuario(str, enum.Enum):
    SUPERADMIN = "superadmin"
    ADMINISTRADOR = "administrador"


class Usuario(Base):

    __tablename__ = "usuarios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    correo: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)

    # RNF-002: hash con sal única por usuario (bcrypt ya incorpora la sal en el hash).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    rol: Mapped[RolUsuario] = mapped_column(SAEnum(RolUsuario), nullable=False, default=RolUsuario.ADMINISTRADOR)
    habilitado: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # RNF-031: control de intentos fallidos de login 
    intentos_fallidos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bloqueado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sesiones: Mapped[list["Sesion"]] = relationship(back_populates="usuario", cascade="all, delete-orphan")

    # RNF-012: restricción de integridad en BD, además de la validación en la capa de aplicación
    __table_args__ = (
        Index(
            "uq_un_solo_superadmin",
            "rol",
            unique=True,
            postgresql_where=text("rol = 'SUPERADMIN'"),
        ),
    )


class Sesion(Base):

    __tablename__ = "sesiones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)

    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # RN-036 / RF-005: se actualiza en cada petición autenticada.
    ultima_actividad: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # RF-006 / RF-009: revocación manual (logout) o forzada (deshabilitar cuenta).
    revocada: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    usuario: Mapped["Usuario"] = relationship(back_populates="sesiones")


class TokenRecuperacion(Base):

    __tablename__ = "tokens_recuperacion"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

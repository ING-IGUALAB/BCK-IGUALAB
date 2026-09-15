"""
Modelos del módulo de Usuarios / Autenticación.

Referencia cruzada a las reglas de negocio que estas tablas sostienen:
  - RN-001: conjunto cerrado de dos roles (SuperAdmin, Administrador).
  - RN-002 / RNF-012: unicidad del SuperAdmin garantizada también a nivel de BD.
  - RN-005 / RNF-004 / RNF-005: la sesión se puede revocar y se revisa en
    cada petición — por eso existe `Sesion` en vez de un JWT puramente stateless.
  - RNF-031: bloqueo temporal tras 5 intentos fallidos de login.
"""
from __future__ import annotations
import enum
import uuid
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, Enum as SAEnum, func, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RolUsuario(str, enum.Enum):
    """RN-001: el sistema reconoce únicamente estos dos roles."""
    SUPERADMIN = "superadmin"
    ADMINISTRADOR = "administrador"


class Usuario(Base):
    """
    Cuenta del sistema. RN-007: nombre, correo y contraseña son obligatorios.
    RN-008: toda cuenta creada por la app nace con rol Administrador — el
    valor SUPERADMIN solo puede llegar aquí por transferencia (RN-009) o por
    la carga inicial de despliegue (RN-006), nunca por el endpoint de creación.
    """
    __tablename__ = "usuarios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    correo: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)

    # RNF-002: hash con sal única por usuario (bcrypt ya incorpora la sal en el hash).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    rol: Mapped[RolUsuario] = mapped_column(SAEnum(RolUsuario), nullable=False, default=RolUsuario.ADMINISTRADOR)
    habilitado: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # --- RNF-031: control de intentos fallidos de login ---
    intentos_fallidos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bloqueado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sesiones: Mapped[list["Sesion"]] = relationship(back_populates="usuario", cascade="all, delete-orphan")

    # RNF-012: restricción de integridad en BD, además de la validación en
    # la capa de aplicación (usuario_service.py) — última línea de defensa.
    __table_args__ = (
        Index(
            "uq_un_solo_superadmin",
            "rol",
            unique=True,
            postgresql_where=text("rol = 'SUPERADMIN'"),
        ),
    )


class Sesion(Base):
    """
    RNF-004 / RNF-005: la autorización y la vigencia de la sesión se
    verifican en cada petición contra esta tabla, no solo contra la firma
    del JWT. El `id` de esta fila es el mismo valor que se guarda como
    `jti` dentro del token.
    """
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
    """
    RF-002 / RF-003: enlace de recuperación de contraseña, de un solo uso,
    con vigencia limitada. RNF-009: solo se guarda el HASH del token.
    """
    __tablename__ = "tokens_recuperacion"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    expira_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

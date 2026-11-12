"""Modelos del historial del Asistente IA (Módulo 6, CU004): guardar y recuperar conversaciones (ENTREGA 1) y continuarlas (ENTREGA 2).

Como `documento_ingesta` y `analisis_gri`, este módulo NO se importa desde `app.models.__init__` y sus tablas están en
`scripts.crear_tablas.TABLAS_SIN_DDL_AUTOMATICO`: las crean las migraciones versionadas `0009_historial_asistente.sql` y
`0010_continuacion_asistente.sql` (`app/migraciones/transaccional/`), no `create_all`.

PROCEDENCIA. Hoy el asistente no conserva lo que responde: el frontend aporta el historial al guardar. Validar su estructura NO
demuestra que lo generó el backend ni que sus citas se verificaron originalmente. Por eso cada mensaje lleva una marca de
procedencia que fija el SERVIDOR (nunca el cliente): `APORTADO_POR_CLIENTE`. No es una respuesta autenticada, una validación
oficial ni un registro de auditoría. Desde la entrega 2 los mensajes que el propio servidor genera y persiste llevan otra
procedencia (ver `ProcedenciaMensaje`); los importados en la entrega 1 conservan `APORTADO_POR_CLIENTE`.

- `conversaciones_asistente`: una conversación = un propietario, un ambiente, UNA empresa, UN sector y UN año. La clave de
  idempotencia es única por (ambiente, propietario); `huella_contenido` es el SHA-256 del contexto y los mensajes recibidos, para
  distinguir un reintento (mismo contenido) de un conflicto (contenido distinto con la misma clave).
- `mensajes_asistente`: orden explícito (`orden` >= 1, único por conversación), rol `usuario` o `asistente`, `contenido` y, en los
  del asistente, la respuesta estructurada (`datos_respuesta`: citas, afirmaciones, `sin_evidencia`, `motivo`, etc.).
- `intercambios_asistente` (entrega 2): reserva de una consulta con estado, vencimiento y token de propiedad.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.empresas import SectorEmpresa


class RolMensaje(str, enum.Enum):
    """Únicos roles admitidos. No existen `system`, `developer` ni `tool`: un historial no puede traer instrucciones privilegiadas."""

    USUARIO = "usuario"
    ASISTENTE = "asistente"


class ProcedenciaMensaje(str, enum.Enum):
    """Origen del contenido, SIEMPRE definido por el servidor (nunca por el cliente).

    - APORTADO_POR_CLIENTE: historial importado por «Guardar conversación» (entrega 1). Su estructura se validó, pero no se demuestra
      que lo generó el backend. Nunca se reclasifica retroactivamente.
    - ENVIADO_POR_USUARIO: la pregunta que el usuario envió a una conversación guardada (entrega 2); solo rol `usuario`.
    - GENERADO_POR_SERVIDOR: la respuesta que el RAG del servidor generó, verificó y persistió (entrega 2); solo rol `asistente`.
    """

    APORTADO_POR_CLIENTE = "APORTADO_POR_CLIENTE"
    ENVIADO_POR_USUARIO = "ENVIADO_POR_USUARIO"
    GENERADO_POR_SERVIDOR = "GENERADO_POR_SERVIDOR"


class EstadoIntercambio(str, enum.Enum):
    """Estado de la reserva de una consulta (`intercambios_asistente`)."""

    EN_CURSO = "EN_CURSO"
    COMPLETADO = "COMPLETADO"
    FALLIDO = "FALLIDO"
    VENCIDO = "VENCIDO"


class ConversacionAsistente(Base):
    __tablename__ = "conversaciones_asistente"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ambiente: Mapped[str] = mapped_column(String(32), nullable=False)
    usuario_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("usuarios.id"), nullable=False)
    empresa_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("empresas.id"), nullable=False)
    sector: Mapped[SectorEmpresa] = mapped_column(
        Enum(SectorEmpresa, name="sector_empresa", validate_strings=True, create_type=False), nullable=False
    )
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    titulo: Mapped[str] = mapped_column(String(80), nullable=False)
    clave_idempotencia: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    huella_contenido: Mapped[str] = mapped_column(String(64), nullable=False)
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actualizada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("ambiente", "usuario_id", "clave_idempotencia", name="uq_conversaciones_asistente_idempotencia"),
        CheckConstraint("anio BETWEEN 2000 AND 9999", name="ck_conversaciones_asistente_anio"),
        CheckConstraint("length(trim(titulo)) > 0", name="ck_conversaciones_asistente_titulo_no_vacio"),
        CheckConstraint("huella_contenido ~ '^[0-9a-f]{64}$'", name="ck_conversaciones_asistente_huella"),
        CheckConstraint("actualizada_en >= creada_en", name="ck_conversaciones_asistente_fechas"),
        Index(
            "ix_conversaciones_asistente_listado", "ambiente", "usuario_id", text("actualizada_en DESC"), text("id DESC")
        ),
    )


class MensajeAsistente(Base):
    __tablename__ = "mensajes_asistente"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversacion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversaciones_asistente.id"), nullable=False
    )
    orden: Mapped[int] = mapped_column(Integer, nullable=False)
    rol: Mapped[str] = mapped_column(String(16), nullable=False)
    contenido: Mapped[str] = mapped_column(Text, nullable=False)
    # `none_as_null`: sin él, `None` se guardaría como el JSON `null` (no como NULL de SQL) y violaría los CHECK de esta tabla.
    datos_respuesta: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    procedencia: Mapped[str] = mapped_column(String(32), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("conversacion_id", "orden", name="uq_mensajes_asistente_orden"),
        CheckConstraint("orden >= 1", name="ck_mensajes_asistente_orden_positivo"),
        CheckConstraint("rol IN ('usuario', 'asistente')", name="ck_mensajes_asistente_rol"),
        CheckConstraint("length(trim(contenido)) > 0", name="ck_mensajes_asistente_contenido_no_vacio"),
        CheckConstraint("(rol = 'asistente') = (datos_respuesta IS NOT NULL)", name="ck_mensajes_asistente_datos_solo_asistente"),
        CheckConstraint(
            "datos_respuesta IS NULL OR jsonb_typeof(datos_respuesta) = 'object'", name="ck_mensajes_asistente_datos_objeto"
        ),
        CheckConstraint(
            "(rol = 'usuario' AND procedencia IN ('APORTADO_POR_CLIENTE', 'ENVIADO_POR_USUARIO')) "
            "OR (rol = 'asistente' AND procedencia IN ('APORTADO_POR_CLIENTE', 'GENERADO_POR_SERVIDOR'))",
            name="ck_mensajes_asistente_procedencia",
        ),
    )


class IntercambioAsistente(Base):
    """Reserva duradera y breve de UNA consulta de una conversación (entrega 2): idempotencia, estado, vencimiento y propiedad de la
    ejecución (`token_ejecucion`). Ver la migración 0010 para los estados y las garantías de la base."""

    __tablename__ = "intercambios_asistente"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversacion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversaciones_asistente.id"), nullable=False
    )
    clave_idempotencia: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    huella_pregunta: Mapped[str] = mapped_column(String(64), nullable=False)
    estado: Mapped[str] = mapped_column(String(16), nullable=False)
    token_ejecucion: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    vence_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    codigo_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mensaje_pregunta_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mensajes_asistente.id"), nullable=True
    )
    mensaje_respuesta_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mensajes_asistente.id"), nullable=True
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("conversacion_id", "clave_idempotencia", name="uq_intercambios_asistente_clave"),
        UniqueConstraint("mensaje_pregunta_id", name="uq_intercambios_asistente_mensaje_pregunta"),
        UniqueConstraint("mensaje_respuesta_id", name="uq_intercambios_asistente_mensaje_respuesta"),
        CheckConstraint("estado IN ('EN_CURSO', 'COMPLETADO', 'FALLIDO', 'VENCIDO')", name="ck_intercambios_asistente_estado"),
        CheckConstraint("huella_pregunta ~ '^[0-9a-f]{64}$'", name="ck_intercambios_asistente_huella"),
        CheckConstraint("intentos >= 1", name="ck_intercambios_asistente_intentos"),
        CheckConstraint(
            "(estado = 'COMPLETADO' AND mensaje_pregunta_id IS NOT NULL AND mensaje_respuesta_id IS NOT NULL) "
            "OR (estado <> 'COMPLETADO' AND mensaje_pregunta_id IS NULL AND mensaje_respuesta_id IS NULL)",
            name="ck_intercambios_asistente_mensajes",
        ),
        CheckConstraint("(estado = 'COMPLETADO') = (completado_en IS NOT NULL)", name="ck_intercambios_asistente_completado_en"),
        CheckConstraint("codigo_error IS NULL OR estado = 'FALLIDO'", name="ck_intercambios_asistente_error"),
        CheckConstraint("actualizado_en >= creado_en", name="ck_intercambios_asistente_fechas"),
        Index(
            "uq_intercambios_asistente_en_curso", "conversacion_id", unique=True, postgresql_where=text("estado = 'EN_CURSO'")
        ),
    )

"""Modelos del Módulo 5: análisis y validación de brechas GRI (CU005).

Como `documento_ingesta`, este módulo NO se importa desde `app.models.__init__` y sus tablas están en
`scripts.crear_tablas.TABLAS_SIN_DDL_AUTOMATICO`: las crea la migración versionada `0004_analisis_gri.sql`
(`app/migraciones/transaccional/`), no `create_all`.

Qué se guarda aquí y qué NO. Los resultados del detector (menciones, citas, offsets, sanciones) siguen en
`documentos.analisis` y NO se copian ni se modifican: esa es la evidencia de ingesta. Aquí solo vive lo que decide
una persona:

- `analisis_gri_control`: una fila por (ambiente, empresa, año). `version` aumenta con CADA cambio de estado y sirve
  de control de concurrencia optimista; la fila se bloquea (`FOR UPDATE`) para serializar asignaciones y validaciones.
- `analisis_gri_estados`: estado ACTUAL de cada identidad GRI evaluada (código + edición). Sin fila = sin estado:
  la ausencia NO es un cuarto estado de cumplimiento.
- `analisis_gri_historial`: cambios de estado, SOLO INSERCIÓN (un disparador de PostgreSQL rechaza UPDATE y DELETE).
- `analisis_gri_validaciones`: confirmaciones «Validar análisis», también SOLO INSERCIÓN. Guardan quién, cuándo, sobre
  qué versión de estados, qué documentos y qué estados/puntaje. Una validación es VIGENTE solo mientras la versión de
  estados y el conjunto de documentos elegibles sigan siendo los validados (se calcula al consultar; no se «invalida»
  con UPDATE).

Los estados se guardan como VARCHAR con CHECK (sin tipos enumerados nuevos en PostgreSQL).
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EstadoGri(str, enum.Enum):
    """Únicos estados que un Administrador puede asignar (RN-030). No hay cuarto estado."""

    OK = "OK"
    BAJA_SUSTANCIA = "BAJA_SUSTANCIA"
    SUB_REPORTADO = "SUB_REPORTADO"


# Puntos de cada estado (RN-032).
PUNTOS_POR_ESTADO: dict[EstadoGri, int] = {
    EstadoGri.OK: 100,
    EstadoGri.BAJA_SUSTANCIA: 50,
    EstadoGri.SUB_REPORTADO: 0,
}


class TipoIdentidadGri(str, enum.Enum):
    """Cómo quedó identificada una fila evaluable. Nunca se inventa una edición."""

    EDICION_RESUELTA = "EDICION_RESUELTA"  # el texto o el catálogo fijan la edición
    EDICION_AMBIGUA = "EDICION_AMBIGUA"  # estándar con varias ediciones y el texto no dice cuál
    EDICION_NO_CATALOGADA = "EDICION_NO_CATALOGADA"  # el texto nombra una edición que el catálogo no tiene


_ESTADOS_SQL = ", ".join(f"'{e.value}'" for e in EstadoGri)
_TIPOS_SQL = ", ".join(f"'{t.value}'" for t in TipoIdentidadGri)


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


class AnalisisGriControl(Base):
    __tablename__ = "analisis_gri_control"

    ambiente: Mapped[str] = mapped_column(String(32), primary_key=True)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), primary_key=True)
    anio: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Número de cambios de estado confirmados. Cada cambio lo incrementa en la misma transacción.
    version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("anio >= 2000", name="ck_analisis_control_anio_minimo"),
        CheckConstraint("version >= 0", name="ck_analisis_control_version_no_negativa"),
    )


class AnalisisGriEstado(Base):
    __tablename__ = "analisis_gri_estados"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ambiente: Mapped[str] = mapped_column(String(32), nullable=False)
    empresa_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    # Clave estable de la fila evaluable: «305:2016», «102:ambigua», «102:no-catalogada-2030».
    identidad: Mapped[str] = mapped_column(String(48), nullable=False)
    codigo: Mapped[str] = mapped_column(String(3), nullable=False)
    edicion: Mapped[str | None] = mapped_column(String(4), nullable=True)
    identidad_tipo: Mapped[str] = mapped_column(String(24), nullable=False)
    estado: Mapped[str] = mapped_column(String(16), nullable=False)
    # Versión de ESTA fila: 1 en la primera asignación, +1 en cada cambio. Control de sobrescritura.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    asignado_por: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    asignado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["ambiente", "empresa_id", "anio"],
            ["analisis_gri_control.ambiente", "analisis_gri_control.empresa_id", "analisis_gri_control.anio"],
        ),
        UniqueConstraint("ambiente", "empresa_id", "anio", "identidad", name="uq_analisis_estado_identidad"),
        CheckConstraint(f"estado IN ({_ESTADOS_SQL})", name="ck_analisis_estado_valido"),
        CheckConstraint(f"identidad_tipo IN ({_TIPOS_SQL})", name="ck_analisis_estado_tipo_identidad"),
        CheckConstraint("version >= 1", name="ck_analisis_estado_version_positiva"),
    )


class AnalisisGriHistorial(Base):
    __tablename__ = "analisis_gri_historial"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ambiente: Mapped[str] = mapped_column(String(32), nullable=False)
    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    identidad: Mapped[str] = mapped_column(String(48), nullable=False)
    codigo: Mapped[str] = mapped_column(String(3), nullable=False)
    edicion: Mapped[str | None] = mapped_column(String(4), nullable=True)
    identidad_tipo: Mapped[str] = mapped_column(String(24), nullable=False)
    # NULL solo en la primera asignación: «sin estado» no es un estado.
    estado_anterior: Mapped[str | None] = mapped_column(String(16), nullable=True)
    estado_nuevo: Mapped[str] = mapped_column(String(16), nullable=False)
    # Actor: sesión validada, nunca el payload.
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    cambiado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )
    # Versión del análisis (`analisis_gri_control.version`) que resultó de este cambio.
    version_analisis: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(f"estado_nuevo IN ({_ESTADOS_SQL})", name="ck_analisis_historial_estado_nuevo"),
        CheckConstraint(
            f"estado_anterior IS NULL OR estado_anterior IN ({_ESTADOS_SQL})", name="ck_analisis_historial_estado_anterior"
        ),
        CheckConstraint(
            "estado_anterior IS DISTINCT FROM estado_nuevo", name="ck_analisis_historial_es_un_cambio"
        ),
        CheckConstraint(f"identidad_tipo IN ({_TIPOS_SQL})", name="ck_analisis_historial_tipo_identidad"),
        CheckConstraint("version_analisis >= 1", name="ck_analisis_historial_version_positiva"),
        Index("ix_analisis_historial_consulta", "ambiente", "empresa_id", "anio", "cambiado_en"),
    )


class AnalisisGriValidacion(Base):
    __tablename__ = "analisis_gri_validaciones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ambiente: Mapped[str] = mapped_column(String(32), nullable=False)
    empresa_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    # Orden de las validaciones de la misma empresa/año (1, 2, ...). La última es la que se evalúa.
    numero: Mapped[int] = mapped_column(Integer, nullable=False)
    # Datos sobre los que se validó: versión de estados, huella del conjunto de documentos elegibles y su detalle.
    version_estados: Mapped[int] = mapped_column(Integer, nullable=False)
    huella_documentos: Mapped[str] = mapped_column(String(64), nullable=False)
    documentos: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Foto de los estados validados (identidad, código, edición, estado) y su puntaje EXACTO (suma y cantidad).
    estados: Mapped[list] = mapped_column(JSONB, nullable=False)
    puntaje_suma: Mapped[int] = mapped_column(Integer, nullable=False)
    puntaje_filas: Mapped[int] = mapped_column(Integer, nullable=False)
    validado_por: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    validado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["ambiente", "empresa_id", "anio"],
            ["analisis_gri_control.ambiente", "analisis_gri_control.empresa_id", "analisis_gri_control.anio"],
        ),
        UniqueConstraint("ambiente", "empresa_id", "anio", "numero", name="uq_analisis_validacion_numero"),
        CheckConstraint("numero >= 1", name="ck_analisis_validacion_numero_positivo"),
        CheckConstraint("version_estados >= 1", name="ck_analisis_validacion_version_positiva"),
        CheckConstraint("length(huella_documentos) = 64", name="ck_analisis_validacion_huella"),
        # Nunca se valida sin códigos evaluables: «no disponible» no es un puntaje.
        CheckConstraint(
            "puntaje_filas >= 1 AND puntaje_suma >= 0 AND puntaje_suma <= 100 * puntaje_filas",
            name="ck_analisis_validacion_puntaje",
        ),
        CheckConstraint("jsonb_typeof(documentos) = 'array'", name="ck_analisis_validacion_documentos_arreglo"),
        CheckConstraint("jsonb_typeof(estados) = 'array'", name="ck_analisis_validacion_estados_arreglo"),
    )


TABLAS_ANALISIS_GRI = (
    AnalisisGriControl.__tablename__,
    AnalisisGriEstado.__tablename__,
    AnalisisGriHistorial.__tablename__,
    AnalisisGriValidacion.__tablename__,
)

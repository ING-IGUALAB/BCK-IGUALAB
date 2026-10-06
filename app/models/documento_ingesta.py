"""Modelo transaccional de documentos de ingesta (Etapa 4A).

IMPORTANTE: este módulo NO se importa desde `app.models.__init__` a propósito. El
arranque ejecuta `Base.metadata.create_all`; registrar `Documento` en los metadatos
de `app.models` haría que cualquier despliegue creara tablas y tipos enumerados en
ambientes compartidos sin una migración aprobada (D17). Quien necesite la tabla
(servicios de ingesta, pruebas) importa este módulo de forma explícita. Para
incorporarla, ver `docs/ingesta/07-almacenamiento-minio.md`.

Estados y reglas: `docs/ingesta/03-arquitectura-y-decisiones.md` (Etapa 4A). Las
invariantes más importantes se imponen con restricciones CHECK e índices únicos
parciales, no solo en los servicios.
"""
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.documentos import TipoDocumento
from app.models.empresas import SectorEmpresa


class EstadoProcesamiento(str, enum.Enum):
    EN_PROCESO = "EN_PROCESO"
    COMPLETADO = "COMPLETADO"
    FALLIDO = "FALLIDO"


class ResultadoAnalisis(str, enum.Enum):
    """Resultado de un análisis EJECUTADO correctamente. Ausencia (NULL) = pendiente."""

    CON_HALLAZGOS = "CON_HALLAZGOS"
    OBSERVADO = "OBSERVADO"


class EstadoCompensacion(str, enum.Enum):
    NINGUNA = "NINGUNA"
    PENDIENTE = "PENDIENTE"
    COMPLETADA = "COMPLETADA"


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _enum(clase: type[enum.Enum], nombre: str) -> Enum:
    return Enum(clase, name=nombre, validate_strings=True)


class Documento(Base):
    __tablename__ = "documentos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Prefijo del ambiente (development/qa/uat) con que se guarda el original.
    ambiente: Mapped[str] = mapped_column(String(32), nullable=False)

    empresa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("empresas.id"), nullable=False, index=True)
    anio: Mapped[int] = mapped_column(Integer, nullable=False)
    tipo: Mapped[TipoDocumento] = mapped_column(_enum(TipoDocumento, "tipo_documento"), nullable=False)
    # Copiado de la empresa en el servidor; nunca del formulario.
    sector: Mapped[SectorEmpresa] = mapped_column(
        Enum(SectorEmpresa, name="sector_empresa", validate_strings=True), nullable=False
    )

    # Metadata: jamás se usa como ruta de almacenamiento.
    nombre_archivo: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tamano_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )

    # Referencia al original: clave del objeto (sin URL, sin credenciales) y, si el
    # bucket tiene versionado, la versión creada por esta carga.
    clave_original: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    version_id_original: Mapped[str | None] = mapped_column(String(255), nullable=True)
    almacenamiento_intentado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_almacenado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    estado_procesamiento: Mapped[EstadoProcesamiento] = mapped_column(
        _enum(EstadoProcesamiento, "estado_procesamiento"),
        default=EstadoProcesamiento.EN_PROCESO,
        server_default=EstadoProcesamiento.EN_PROCESO.value,
        nullable=False,
    )
    resultado_analisis: Mapped[ResultadoAnalisis | None] = mapped_column(
        _enum(ResultadoAnalisis, "resultado_analisis"), nullable=True
    )
    completado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    motivo_fallo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fallido_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    estado_compensacion: Mapped[EstadoCompensacion] = mapped_column(
        _enum(EstadoCompensacion, "estado_compensacion"),
        default=EstadoCompensacion.NINGUNA,
        server_default=EstadoCompensacion.NINGUNA.value,
        nullable=False,
    )
    compensacion_intentos: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    ultimo_error_compensacion: Mapped[str | None] = mapped_column(String(64), nullable=True)
    compensada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Mientras sea verdadera ocupa su SHA-256 y su empresa/año/tipo.
    reserva_activa: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)

    __table_args__ = (
        CheckConstraint("anio >= 2000", name="ck_documentos_anio_minimo"),
        CheckConstraint("length(sha256) = 64", name="ck_documentos_sha256_longitud"),
        CheckConstraint("tamano_bytes > 0", name="ck_documentos_tamano_positivo"),
        CheckConstraint("length(trim(nombre_archivo)) > 0", name="ck_documentos_nombre_no_vacio"),
        # Resultado del análisis y publicación van juntos: no hay OBSERVADO ni
        # COMPLETADO sin análisis ejecutado.
        CheckConstraint(
            "(estado_procesamiento = 'COMPLETADO') = (resultado_analisis IS NOT NULL)",
            name="ck_documentos_resultado_solo_si_completado",
        ),
        CheckConstraint(
            "(estado_procesamiento = 'COMPLETADO') = (completado_en IS NOT NULL)",
            name="ck_documentos_completado_en",
        ),
        # Un documento completado tiene su original almacenado.
        CheckConstraint(
            "estado_procesamiento <> 'COMPLETADO' OR original_almacenado_en IS NOT NULL",
            name="ck_documentos_completado_con_original",
        ),
        CheckConstraint(
            "estado_procesamiento <> 'FALLIDO' OR (motivo_fallo IS NOT NULL AND fallido_en IS NOT NULL)",
            name="ck_documentos_fallido_con_motivo",
        ),
        CheckConstraint(
            "estado_compensacion = 'NINGUNA' OR estado_procesamiento = 'FALLIDO'",
            name="ck_documentos_compensacion_solo_si_fallido",
        ),
        # La reserva solo se libera en un fallido sin limpieza pendiente.
        CheckConstraint(
            "reserva_activa OR (estado_procesamiento = 'FALLIDO' "
            "AND estado_compensacion IN ('NINGUNA', 'COMPLETADA'))",
            name="ck_documentos_reserva_solo_liberada_si_limpio",
        ),
        CheckConstraint("compensacion_intentos >= 0", name="ck_documentos_intentos_no_negativos"),
        # Exclusión mutua respaldada por la BD (una consulta previa no basta).
        Index(
            "uq_documentos_sha256_activo",
            "ambiente",
            "sha256",
            unique=True,
            postgresql_where=text("reserva_activa"),
            sqlite_where=text("reserva_activa"),
        ),
        Index(
            "uq_documentos_empresa_anio_tipo_activo",
            "ambiente",
            "empresa_id",
            "anio",
            "tipo",
            unique=True,
            postgresql_where=text("reserva_activa"),
            sqlite_where=text("reserva_activa"),
        ),
    )

    @property
    def disponible_para_rag(self) -> bool:
        """Solo un documento completado con análisis ejecutado. Tener el original
        almacenado no basta."""
        return (
            self.estado_procesamiento is EstadoProcesamiento.COMPLETADO
            and self.resultado_analisis is not None
        )

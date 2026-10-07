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

COORDINADOR DE INGESTA (2026-10-07). Se añaden, SIN tocar las columnas anteriores:
- `analisis` (JSONB en PostgreSQL): el resultado completo de `analizar_documento` serializado
  (`ResultadoAnalisisIngesta.a_dict()`). `resultado_analisis` sigue siendo la clasificación; ambas
  representaciones deben coincidir (CHECK en PostgreSQL y validación en el servicio). Se retira
  (NULL) cuando el intento falla: un `FALLIDO` no conserva citas del documento.
- Progreso persistente por operación (el identificador de operación es `id`): `etapa_actual`,
  `fragmentos_procesados`/`fragmentos_total`, `advertencias`, `progreso_actualizado_en`. El código de
  error seguro es `motivo_fallo` (o `vector_ultimo_error` si la publicación vectorial quedó pendiente).
- Escritura y publicación vectorial: `vector_escritura_intentada_en` (se registra DURABLEMENTE antes de
  insertar el primer lote: obliga a limpiar la base vectorial al compensar), `vector_publicado_en`
  (NULL en un `COMPLETADO` = publicación pendiente, recuperable e idempotente),
  `vector_publicacion_intentos` y `vector_ultimo_error`.
SQL: `db/transaccional/` (instalación nueva y actualización de una tabla existente).

OPERACIONES DE INGESTA (2026-10-07). `OperacionIngesta` es el identificador que el frontend obtiene ANTES de
subir el archivo (`POST /documentos/operaciones`). No es un documento: no tiene hash, empresa ni metadatos
inventados. Se enlaza al documento (`documento_id`) en la MISMA transacción que lo reserva, y se rechaza si una
carga ya la tomó. Tabla excluida del DDL automático del arranque, igual que `documentos`.
"""
import enum
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class EtapaIngesta(str, enum.Enum):
    """Etapas REALES de la ingesta, en orden. Se persisten en `documentos.etapa_actual`.
    `FINALIZADO` solo se alcanza con los fragmentos PUBLICADOS en la base vectorial."""

    RESERVADO = "RESERVADO"
    ALMACENANDO_ORIGINAL = "ALMACENANDO_ORIGINAL"
    INDEXANDO = "INDEXANDO"
    ANALIZANDO = "ANALIZANDO"
    COMPLETANDO = "COMPLETANDO"
    PUBLICANDO = "PUBLICANDO"
    FINALIZADO = "FINALIZADO"


_ETAPAS_SQL = ", ".join(f"'{e.value}'" for e in EtapaIngesta)
# Análisis serializado: JSONB en PostgreSQL, JSON genérico en SQLite (pruebas). Python `None` es SQL NULL.
_JSON_ANALISIS = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


# Vigencia inicial de la propiedad de una operación EN_PROCESO. Provisional (D24): debe
# superar el plazo de la operación más larga que NO renueva la vigencia (la subida).
VIGENCIA_PREDETERMINADA = timedelta(minutes=15)


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _vigencia_inicial() -> datetime:
    return _ahora_utc() + VIGENCIA_PREDETERMINADA


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

    # Propiedad y vigencia de la operación EN_PROCESO. El ejecutor recibe el token al reservar
    # y TODA transición suya lo exige. La recuperación solo toma operaciones con la vigencia
    # vencida y ROTA el token: el ejecutor anterior ya no puede publicar ni registrar nada.
    # La antigüedad (`creado_en`) no interviene: un ejecutor vivo renueva la vigencia.
    ejecucion_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    ejecucion_vigente_hasta: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_vigencia_inicial
    )

    # Mientras sea verdadera ocupa su SHA-256 y su empresa/año/tipo.
    reserva_activa: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)

    # --- Coordinador de ingesta (2026-10-07) -----------------------------------------------------
    # Resultado completo del análisis (a_dict()); `resultado_analisis` es solo la clasificación.
    analisis: Mapped[dict | None] = mapped_column(_JSON_ANALISIS, nullable=True)

    # Progreso persistente. Sin porcentajes: solo etapa real y contadores cuando se conocen.
    etapa_actual: Mapped[str] = mapped_column(
        String(32), default=EtapaIngesta.RESERVADO.value, server_default=EtapaIngesta.RESERVADO.value, nullable=False
    )
    fragmentos_procesados: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    fragmentos_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progreso_actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )
    advertencias: Mapped[list] = mapped_column(
        _JSON_ANALISIS, default=list, server_default=text("'[]'"), nullable=False
    )

    # Escritura y publicación en la base vectorial. No hay atomicidad entre las bases: estos campos
    # son el rastro DURADERO que permite compensar (intento registrado) y repetir la publicación.
    vector_escritura_intentada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vector_publicado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vector_publicacion_intentos: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    vector_ultimo_error: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
        # --- Coordinador de ingesta ---
        CheckConstraint(f"etapa_actual IN ({_ETAPAS_SQL})", name="ck_documentos_etapa_valida"),
        CheckConstraint(
            "fragmentos_procesados >= 0 AND (fragmentos_total IS NULL "
            "OR (fragmentos_total >= 0 AND fragmentos_procesados <= fragmentos_total))",
            name="ck_documentos_fragmentos_progreso",
        ),
        CheckConstraint("vector_publicacion_intentos >= 0", name="ck_documentos_publicacion_intentos"),
        # Solo un COMPLETADO puede estar publicado en la base vectorial, y solo tras intentar escribir.
        CheckConstraint(
            "vector_publicado_en IS NULL OR estado_procesamiento = 'COMPLETADO'",
            name="ck_documentos_vector_publicado_solo_completado",
        ),
        CheckConstraint(
            "vector_publicado_en IS NULL OR vector_escritura_intentada_en IS NOT NULL",
            name="ck_documentos_vector_publicado_con_intento",
        ),
        # Un intento fallido no conserva resultados parciales del análisis (citas del documento).
        CheckConstraint(
            "analisis IS NULL OR estado_procesamiento <> 'FALLIDO'", name="ck_documentos_sin_analisis_si_fallido"
        ),
        # Consistencia entre las dos representaciones y estructura del JSON (PostgreSQL; en SQLite la
        # misma validación la hace el servicio: no hay operadores JSONB).
        CheckConstraint(
            "resultado_analisis IS NULL OR analisis IS NULL OR analisis->>'resultado' = resultado_analisis::text",
            name="ck_documentos_analisis_consistente",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "analisis IS NULL OR (jsonb_typeof(analisis) = 'object' "
            "AND coalesce(analisis->>'resultado', '') IN ('CON_HALLAZGOS', 'OBSERVADO') "
            "AND length(coalesce(analisis->>'version_catalogo', '')) > 0 "
            "AND coalesce(jsonb_typeof(analisis->'motivos'), '') = 'array' "
            "AND coalesce(jsonb_typeof(analisis->'gri'), '') = 'array' "
            "AND coalesce(jsonb_typeof(analisis->'sanciones'), '') = 'array' "
            "AND coalesce(jsonb_typeof(analisis->'advertencias'), '') = 'array')",
            name="ck_documentos_analisis_estructura",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("jsonb_typeof(advertencias) = 'array'", name="ck_documentos_advertencias_arreglo").ddl_if(
            dialect="postgresql"
        ),
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
    def publicacion_vectorial_pendiente(self) -> bool:
        """COMPLETADO en la base transaccional pero con los fragmentos aún sin publicar: situación
        recuperable (se repite `publicar_fragmentos`, idempotente, sin nuevos embeddings ni análisis)."""
        return (
            self.estado_procesamiento is EstadoProcesamiento.COMPLETADO and self.vector_publicado_en is None
        )

    @property
    def disponible_para_rag(self) -> bool:
        """Solo un documento completado con análisis ejecutado. Tener el original
        almacenado no basta."""
        return (
            self.estado_procesamiento is EstadoProcesamiento.COMPLETADO
            and self.resultado_analisis is not None
        )


# --- Operaciones de ingesta (2026-10-07) ---------------------------------------------------------------

class EstadoOperacion(str, enum.Enum):
    """Estado PROPIO de la operación (lo que la operación sabe sin mirar el documento). El estado público que
    ve el frontend se deriva de este y del documento enlazado (`operaciones_ingesta.estado_publico`)."""

    CREADA = "CREADA"  # identificador entregado; todavía no llegó ningún archivo
    EN_CARGA = "EN_CARGA"  # una carga la tomó (exclusiva); aún no hay documento reservado
    CON_DOCUMENTO = "CON_DOCUMENTO"  # el documento está reservado: el desenlace lo dice el documento
    RECHAZADA = "RECHAZADA"  # la carga terminó SIN crear documento; ver `codigo_error`


_ESTADOS_OPERACION_SQL = ", ".join(f"'{e.value}'" for e in EstadoOperacion)


class OperacionIngesta(Base):
    __tablename__ = "operaciones_ingesta"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Ambiente (development/qa/uat) de la aplicación que la creó: toda consulta lo filtra.
    ambiente: Mapped[str] = mapped_column(String(32), nullable=False)
    # Actor: sesión validada que la creó, nunca el payload.
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id"), nullable=False, index=True)
    estado: Mapped[str] = mapped_column(
        String(16), default=EstadoOperacion.CREADA.value, server_default=EstadoOperacion.CREADA.value, nullable=False
    )
    # Referencia al documento reservado por esta operación (misma base: FK real). Único.
    documento_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documentos.id"), nullable=True)
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )
    carga_iniciada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hasta cuándo se espera que una carga EN_CARGA reserve su documento. No es un cierre: pasada esta hora la
    # operación se MUESTRA como interrumpida si sigue sin documento; una carga viva todavía puede enlazarlo.
    carga_vigente_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actualizada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_ahora_utc, server_default=func.now(), nullable=False
    )
    # Rechazo previo a la reserva: código estable y mensaje de la excepción (constantes del backend, nunca
    # contenido del documento ni respuestas de proveedores) y el estado HTTP con que se respondió.
    codigo_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mensaje_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    estado_http: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(f"estado IN ({_ESTADOS_OPERACION_SQL})", name="ck_operaciones_estado_valido"),
        CheckConstraint(
            "(estado = 'CON_DOCUMENTO') = (documento_id IS NOT NULL)", name="ck_operaciones_documento_solo_si_enlazada"
        ),
        CheckConstraint(
            "(estado = 'RECHAZADA') = (codigo_error IS NOT NULL)", name="ck_operaciones_error_solo_si_rechazada"
        ),
        CheckConstraint(
            "(estado = 'CREADA') = (carga_iniciada_en IS NULL)", name="ck_operaciones_carga_solo_si_iniciada"
        ),
        CheckConstraint(
            "(carga_iniciada_en IS NULL) = (carga_vigente_hasta IS NULL)", name="ck_operaciones_vigencia_con_carga"
        ),
        Index(
            "uq_operaciones_documento",
            "documento_id",
            unique=True,
            postgresql_where=text("documento_id IS NOT NULL"),
            sqlite_where=text("documento_id IS NOT NULL"),
        ),
    )

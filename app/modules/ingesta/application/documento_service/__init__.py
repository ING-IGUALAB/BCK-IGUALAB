"""Reserva de documentos y operaciones de almacenamiento/compensación (Etapa 4A).

Secuencia, estados e invariantes: `docs/ingesta/03-arquitectura-y-decisiones.md`
(Etapa 4A). Resumen de las garantías que implementa este módulo:

- NINGUNA transacción de BD permanece abierta durante una llamada al almacén: cada
  paso es una transacción corta que termina con `commit` antes de llamar a MinIO.
- PostgreSQL y MinIO no comparten transacción. No hay atomicidad distribuida: la
  consistencia se logra con estados duraderos, reserva retenida y compensación
  repetible. Nada que no esté `COMPLETADO` es consultable.
- La exclusión de duplicados la imponen índices únicos parciales de la BD; la
  consulta previa solo sirve para informar. Solo se traducen a 409 las violaciones
  de esos índices; cualquier otra `IntegrityError` se propaga.
- La reserva de un intento fallido NO se libera hasta confirmar la limpieza.
- PROPIEDAD DE LA OPERACIÓN. `reservar_documento` entrega un token de ejecución y una
  vigencia. Toda transición del ejecutor (registrar el intento, registrar el original,
  fallar, publicar) exige el token; el ejecutor renueva la vigencia con `renovar_vigencia`.
  La recuperación solo toma operaciones con la vigencia VENCIDA (la antigüedad no cuenta) y
  ROTA el token, de modo que el ejecutor anterior ya no puede publicar.
- LIMPIEZA CON INCERTIDUMBRE. Una subida pudo terminar después del timeout o la
  cancelación. La compensación consulta el desenlace real (`estado_subida`), elimina la
  versión concreta si se conoce, o reconcilia listando las versiones de la clave exacta.
  Nunca declara limpio por «DELETE + HEAD 404», ni mientras la subida siga en curso, ni
  cuando la ausencia no prueba que el objeto no se creó (queda PENDIENTE y requiere
  intervención documentada en `docs/ingesta/07-almacenamiento-minio.md`).
- COORDINADOR (2026-10-07): este módulo guarda el estado duradero que usa `coordinador.py` (progreso, intento
  de escritura vectorial, análisis, publicación vectorial) y las transiciones condicionales con token.
  `publicar_documento` es la compuerta transaccional (`COMPLETADO`) y `publicar_en_base_vectorial` la
  publicación vectorial idempotente y repetible. La compensación es CONJUNTA (base vectorial + MinIO).
  No hay atomicidad entre las dos bases ni MinIO: la consistencia viene de estados duraderos, del cierre del
  documento en la base vectorial y de la compensación repetible.

Cada función recibe una `AsyncSession` y la usa de forma secuencial (nunca se
comparte entre tareas concurrentes). No registra auditoría (Etapa 6).
"""

from ._comun import (
    AlmacenOriginales,
    AsyncSession,
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    Documento,
    Empresa,
    EstadoCompensacion,
    EstadoOperacion,
    EstadoProcesamiento,
    EstadoSubida,
    EtapaIngesta,
    ExternalServiceError,
    IntegrityError,
    MetadatosIngestaRequest,
    NotFoundError,
    OperacionIngesta,
    ReferenciaOriginal,
    ResultadoAnalisis,
    RolUsuario,
    Sequence,
    TAMANO_MAXIMO_BYTES,
    TipoEventoAuditoria,
    Usuario,
    VIGENCIA_PREDETERMINADA,
    _COLUMNAS_DE_RESERVA_SQLITE,
    _PRIORIDAD,
    _RESTRICCIONES_DE_RESERVA,
    _ahora,
    _buscar_conflicto,
    _cargar,
    _condiciones_ejecutor,
    _conflicto_de_estado,
    _entero_no_negativo,
    _error_de_conflicto,
    _es_violacion_de_reserva,
    _exigir_duracion,
    _exigir_instante,
    _exigir_token,
    _iso_utc,
    _json_seguro,
    _terminar_lectura,
    _transicion,
    and_,
    auditar_aparte,
    codigo_seguro,
    dataclass,
    datetime,
    enum,
    es_codigo_valido,
    generar_clave_original,
    hashlib,
    json,
    logger,
    logging,
    or_,
    re,
    registrar_evento,
    select,
    timedelta,
    timezone,
    update,
    uuid,
    validacion,
    validar_ambiente,
    vectores,
)
from .reserva import (
    _SHA256_REGEX,
    _enlazar_operacion,
    _validar_entrada,
    renovar_vigencia,
    reservar_documento,
    verificar_original,
)
from .procesamiento import (
    MAXIMO_ADVERTENCIAS_PROGRESO,
    MAXIMO_BYTES_ANALISIS,
    _problemas_de_estructura,
    _problemas_de_tamano,
    fallar_documento,
    persistir_analisis,
    registrar_progreso,
    validar_analisis,
)
from .compensacion import (
    _anotar_existencia,
    _eliminar_y_verificar,
    _fallar_y_compensar,
    _limpiar_vectorial,
    _reconciliar_y_eliminar,
    _sin_confirmar,
    almacenar_original,
    compensar_documento,
)
from .publicacion import (
    _registrar_fallo_publicacion,
    publicar_documento,
    publicar_en_base_vectorial,
    registrar_intento_vectorial,
)
from .consultas import (
    EstadoProgreso,
    ProgresoIngesta,
    cargar_documento,
    documentos_recuperables,
    estado_de_documento,
    exigir_documento_del_ambiente,
    obtener_progreso,
    progreso_de_documento,
)
from .recuperacion import (
    ResumenRecuperacion,
    _compensar_pendientes,
    _marcar_abandonados,
    _publicar_pendientes,
    recuperar_documentos_pendientes,
)

# --- Compatibilidad -------------------------------------------------------------
# `documento_service` era un único módulo; ahora es un paquete. El código y las pruebas
# tratan el paquete como aquel módulo y sustituyen nombres en él
# (`monkeypatch.setattr(servicio, ...)`). Estas sustituciones se reenvían a los
# sub-módulos que ya tengan el nombre: las llamadas internas resuelven el mismo valor
# que antes de la división, sin cambios de comportamiento.
import sys as _sys
from types import ModuleType as _ModuleType

_SUBMODULOS = ("_comun", "reserva", "procesamiento", "compensacion", "publicacion", "consultas", "recuperacion")


def _reenviar_a_submodulos(nombre: str, valor: object, *, eliminar: bool = False) -> None:
    for _sub in _SUBMODULOS:
        _globales = _sys.modules[f"{__name__}.{_sub}"].__dict__
        if nombre in _globales:
            if eliminar:
                del _globales[nombre]
            else:
                _globales[nombre] = valor


class _ModuloDocumentoService(_ModuleType):
    def __setattr__(self, nombre: str, valor: object) -> None:
        super().__setattr__(nombre, valor)
        _reenviar_a_submodulos(nombre, valor)

    def __delattr__(self, nombre: str) -> None:
        super().__delattr__(nombre)
        _reenviar_a_submodulos(nombre, None, eliminar=True)


_sys.modules[__name__].__class__ = _ModuloDocumentoService

__all__ = [
    'AlmacenOriginales',
    'AsyncSession',
    'AuthorizationError',
    'BusinessValidationError',
    'ConflictError',
    'Documento',
    'Empresa',
    'EstadoCompensacion',
    'EstadoOperacion',
    'EstadoProcesamiento',
    'EstadoProgreso',
    'EstadoSubida',
    'EtapaIngesta',
    'ExternalServiceError',
    'IntegrityError',
    'MAXIMO_ADVERTENCIAS_PROGRESO',
    'MAXIMO_BYTES_ANALISIS',
    'MetadatosIngestaRequest',
    'NotFoundError',
    'OperacionIngesta',
    'ProgresoIngesta',
    'ReferenciaOriginal',
    'ResultadoAnalisis',
    'ResumenRecuperacion',
    'RolUsuario',
    'Sequence',
    'TAMANO_MAXIMO_BYTES',
    'TipoEventoAuditoria',
    'Usuario',
    'VIGENCIA_PREDETERMINADA',
    '_COLUMNAS_DE_RESERVA_SQLITE',
    '_ModuleType',
    '_PRIORIDAD',
    '_RESTRICCIONES_DE_RESERVA',
    '_SHA256_REGEX',
    '_ahora',
    '_anotar_existencia',
    '_buscar_conflicto',
    '_cargar',
    '_compensar_pendientes',
    '_condiciones_ejecutor',
    '_conflicto_de_estado',
    '_eliminar_y_verificar',
    '_enlazar_operacion',
    '_entero_no_negativo',
    '_error_de_conflicto',
    '_es_violacion_de_reserva',
    '_exigir_duracion',
    '_exigir_instante',
    '_exigir_token',
    '_fallar_y_compensar',
    '_iso_utc',
    '_json_seguro',
    '_limpiar_vectorial',
    '_marcar_abandonados',
    '_problemas_de_estructura',
    '_problemas_de_tamano',
    '_publicar_pendientes',
    '_reconciliar_y_eliminar',
    '_registrar_fallo_publicacion',
    '_sin_confirmar',
    '_sys',
    '_terminar_lectura',
    '_transicion',
    '_validar_entrada',
    'almacenar_original',
    'and_',
    'auditar_aparte',
    'cargar_documento',
    'codigo_seguro',
    'compensar_documento',
    'dataclass',
    'datetime',
    'documentos_recuperables',
    'enum',
    'es_codigo_valido',
    'estado_de_documento',
    'exigir_documento_del_ambiente',
    'fallar_documento',
    'generar_clave_original',
    'hashlib',
    'json',
    'logger',
    'logging',
    'obtener_progreso',
    'or_',
    'persistir_analisis',
    'progreso_de_documento',
    'publicar_documento',
    'publicar_en_base_vectorial',
    're',
    'recuperar_documentos_pendientes',
    'registrar_evento',
    'registrar_intento_vectorial',
    'registrar_progreso',
    'renovar_vigencia',
    'reservar_documento',
    'select',
    'timedelta',
    'timezone',
    'update',
    'uuid',
    'validacion',
    'validar_ambiente',
    'validar_analisis',
    'vectores',
    'verificar_original',
]

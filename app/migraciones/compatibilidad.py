"""Compatibilidad del esquema central con el código, comprobada en CADA arranque.

POR QUÉ EXISTE. La migración 0005 (auditoría con `resultado` y solo inserción, reportes inmutables) no declara `objetos`: las
tablas `auditoria` y `reportes_prospeccion` las crea `create_all` y los disparadores no forman parte de la firma que compara el
motor (ver su cabecera). Para el motor, una migración sin objetos queda "aplicada" una vez registrada y nada vuelve a comprobar que
su efecto siga en la base. Y el código SÍ depende de él: el ORM inserta `auditoria.resultado` en cada evento, también en el
inicio de sesión exitoso, de modo que con el esquema anterior TODO inicio de sesión falla con un 500.

QUÉ HACE. Después de preparar el esquema, `exigir_esquema_central` consulta el catálogo de PostgreSQL y comprueba lo que necesita
el código actual: la columna `auditoria.resultado`, todos los miembros de `TipoEventoAuditoria` en el tipo enumerado (los valores
se guardan por NOMBRE), todos los miembros de `SectorEmpresa` en `sector_empresa` (migración 0008: «Otros»), la restricción `ck_auditoria_resultado_valido` y los dos disparadores de solo inserción, activos. Si algo
falta, la aplicación NO arranca: lanza `ErrorMigracion("CENTRAL_SCHEMA_INCOMPATIBLE")` desde el `lifespan`, el contenedor sale y
el despliegue falla a la vista, en lugar de anunciar `/health` y devolver 500 en el login. Eso incluye el caso en que la 0005
falló (o no llegó a ejecutarse porque falló una anterior): no se degrada a un aviso del módulo de análisis GRI.

REPORTES VERSIONADOS (migraciones 0006 y 0007, BUG-15). El ORM inserta `reportes_prospeccion.version` y `validacion_id`, y una segunda
versión de la misma empresa/año exige que la unicidad antigua `uq_reporte_empresa_anio` ya no exista. Si la 0006 no se aplicó, TODA
emisión fallaría con un error de base de datos; si la 0007 no se aplicó, la base aceptaría reportes nuevos sin validación o con la
validación de otra empresa o año. Por eso se exige la DEFINICIÓN de cada protección, no solo su nombre (una restricción con el
nombre esperado pero otras columnas, otra tabla referenciada u otra condición NO supera el control):
- columnas: tipo y nulabilidad de `version` (integer, NOT NULL) y `validacion_id` (uuid, nulable: los históricos conservan NULL);
- restricciones: tipo, columnas en orden, tabla y columnas referenciadas (claves foráneas), condición (CHECK) y que estén validadas;
- disparadores: tabla, momento y evento (BEFORE INSERT / BEFORE UPDATE OR DELETE por fila), función que ejecutan, que esa función
  conserve el `RAISE EXCEPTION` que rechaza, y que estén habilitados.
Además no debe existir la unicidad antigua `uq_reporte_empresa_anio`.

LÍMITES. No comprueba los permisos del rol de la base ni el texto completo de las funciones (solo que sigan rechazando). Un
disparador desactivado a mano también impide el arranque: la protección de RN-044 / RN-041 no se da por vigente si está apagada.
"""
import asyncio
import logging
from dataclasses import dataclass

import asyncpg

from app.core.config import settings
from app.core.logging_config import paso_de_arranque
from app.migraciones.motor import ErrorMigracion, _base_no_disponible, _conectar
from app.models import SectorEmpresa, TipoEventoAuditoria

logger = logging.getLogger("igualab.migraciones")

BASE = "transaccional"
CODIGO = "CENTRAL_SCHEMA_INCOMPATIBLE"

TABLA_AUDITORIA = "auditoria"
TIPO_EVENTO = "tipoeventoauditoria"
TIPO_SECTOR = "sector_empresa"  # lo comparten `empresas.sector` y `documentos.sector`; la migración 0008 añade OTROS
COLUMNA_RESULTADO = "resultado"
RESTRICCION_RESULTADO = "ck_auditoria_resultado_valido"
TABLA_REPORTES = "reportes_prospeccion"
TABLA_VALIDACIONES = "analisis_gri_validaciones"
COLUMNA_VALIDACION = "validacion_id"
COLUMNA_EMPRESA = "empresa_id"
COLUMNA_VERSION = "version"
# (disparador, tabla): los crea la migración 0005.
DISPARADORES_SOLO_INSERCION = (
    ("tr_auditoria_solo_insercion", "auditoria"),
    ("tr_reportes_solo_insercion", TABLA_REPORTES),
)
# `pg_trigger.tgtype`: ROW = 1, BEFORE = 2, INSERT = 4, DELETE = 8, UPDATE = 16.
_FILA_ANTES_DE_INSERTAR = 1 + 2 + 4
_FILA_ANTES_DE_MODIFICAR_O_ELIMINAR = 1 + 2 + 8 + 16


@dataclass(frozen=True)
class DisparadorEsperado:
    nombre: str
    tabla: str
    tgtype: int
    funcion: str


# Definición exigida de cada disparador (nombre, tabla, momento/evento por fila y función que ejecuta).
DISPARADORES_ESPERADOS = (
    DisparadorEsperado("tr_auditoria_solo_insercion", "auditoria", _FILA_ANTES_DE_MODIFICAR_O_ELIMINAR, "igualab_rechazar_modificacion_solo_insercion"),
    DisparadorEsperado("tr_reportes_solo_insercion", TABLA_REPORTES, _FILA_ANTES_DE_MODIFICAR_O_ELIMINAR, "igualab_rechazar_modificacion_solo_insercion"),
    DisparadorEsperado("tr_reportes_exigir_validacion", TABLA_REPORTES, _FILA_ANTES_DE_INSERTAR, "igualab_exigir_validacion_en_reporte"),
)


@dataclass(frozen=True)
class RestriccionEsperada:
    tabla: str
    nombre: str
    tipo: str  # pg_constraint.contype: 'u' única, 'f' clave foránea, 'c' check
    columnas: tuple[str, ...]
    tabla_referenciada: str | None = None
    columnas_referenciadas: tuple[str, ...] = ()
    condicion: str | None = None  # CHECK, sin espacios ni paréntesis


RESTRICCIONES_ESPERADAS = (
    RestriccionEsperada(TABLA_REPORTES, "uq_reporte_empresa_anio_version", "u", (COLUMNA_EMPRESA, "anio", COLUMNA_VERSION)),
    RestriccionEsperada(TABLA_REPORTES, "uq_reporte_validacion", "u", (COLUMNA_VALIDACION,)),
    RestriccionEsperada(TABLA_REPORTES, "ck_reporte_version_positiva", "c", (COLUMNA_VERSION,), condicion="CHECKversion>=1"),
    RestriccionEsperada(TABLA_REPORTES, "fk_reporte_validacion", "f", (COLUMNA_VALIDACION,), TABLA_VALIDACIONES, ("id",)),
    RestriccionEsperada(
        TABLA_REPORTES, "fk_reporte_validacion_empresa_anio", "f", (COLUMNA_VALIDACION, COLUMNA_EMPRESA, "anio"),
        TABLA_VALIDACIONES, ("id", COLUMNA_EMPRESA, "anio"),
    ),
    RestriccionEsperada(TABLA_VALIDACIONES, "uq_analisis_validacion_id_empresa_anio", "u", ("id", COLUMNA_EMPRESA, "anio")),
)
# (columna, tipo, NOT NULL) de `reportes_prospeccion` que introduce la 0006.
COLUMNAS_ESPERADAS_REPORTES = ((COLUMNA_VERSION, "integer", True), (COLUMNA_VALIDACION, "uuid", False))

_SQL_COLUMNA = """
SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = $1 AND a.attname = $2 AND a.attnum > 0 AND NOT a.attisdropped
"""
# Reportes versionados (migraciones 0006 y 0007): lo que ya NO debe existir.
RESTRICCION_REPORTES_OBSOLETA = "uq_reporte_empresa_anio"

_SQL_DEFINICION_COLUMNA = """
SELECT format_type(a.atttypid, a.atttypmod) AS tipo, a.attnotnull AS no_nula
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = $1 AND a.attname = $2 AND a.attnum > 0 AND NOT a.attisdropped
"""
_SQL_DEFINICION_RESTRICCION = """
SELECT con.contype::text AS tipo, con.convalidated AS validada,
  (SELECT array_agg(a.attname::text ORDER BY k.orden) FROM unnest(con.conkey) WITH ORDINALITY AS k(num, orden)
     JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.num) AS columnas,
  (SELECT r.relname::text FROM pg_class r WHERE r.oid = con.confrelid) AS tabla_referenciada,
  (SELECT array_agg(a.attname::text ORDER BY k.orden) FROM unnest(con.confkey) WITH ORDINALITY AS k(num, orden)
     JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.num) AS columnas_referenciadas,
  pg_get_constraintdef(con.oid) AS definicion
FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = $1 AND con.conname = $2
"""

_SQL_ETIQUETAS = """
SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public' AND t.typname = $1
"""
_SQL_RESTRICCION = """
SELECT 1 FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = $1 AND con.conname = $2
"""
_SQL_DISPARADORES = """
SELECT t.tgname AS nombre, c.relname AS tabla, t.tgenabled::text AS estado, t.tgtype::int AS tgtype,
       p.proname::text AS funcion, p.prosrc AS cuerpo
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname = ANY($1::text[])
"""


async def elementos_faltantes(conn: asyncpg.Connection) -> list[str]:
    """Lo que el código actual necesita del esquema central y la base no tiene, solo como nombres (nunca valores)."""
    faltan: list[str] = []
    if not await conn.fetchval(_SQL_COLUMNA, TABLA_AUDITORIA, COLUMNA_RESULTADO):
        faltan.append(f"columna:{TABLA_AUDITORIA}.{COLUMNA_RESULTADO}")
    etiquetas = {fila["enumlabel"] for fila in await conn.fetch(_SQL_ETIQUETAS, TIPO_EVENTO)}
    faltan += [f"valor_de_tipo:{TIPO_EVENTO}.{m.name}" for m in TipoEventoAuditoria if m.name not in etiquetas]
    etiquetas_sector = {fila["enumlabel"] for fila in await conn.fetch(_SQL_ETIQUETAS, TIPO_SECTOR)}
    faltan += [f"valor_de_tipo:{TIPO_SECTOR}.{m.name}" for m in SectorEmpresa if m.name not in etiquetas_sector]
    if not await conn.fetchval(_SQL_RESTRICCION, TABLA_AUDITORIA, RESTRICCION_RESULTADO):
        faltan.append(f"restriccion:{TABLA_AUDITORIA}.{RESTRICCION_RESULTADO}")
    faltan += await _faltantes_de_disparadores(conn)
    faltan += await _faltantes_de_reportes(conn)
    return faltan


def _problema_de_disparador(esperado: DisparadorEsperado, fila) -> str | None:
    """Etiqueta del problema de un disparador existente, o None si cumple su definición."""
    if fila["estado"] not in ("O", "A"):  # 'D' desactivado; 'R' solo dispara en modo réplica (en la práctica, apagado)
        return "disparador_desactivado"
    rechaza = "RAISE EXCEPTION" in (fila["cuerpo"] or "").upper()
    if fila["tgtype"] != esperado.tgtype or fila["funcion"] != esperado.funcion or not rechaza:
        return "disparador_incorrecto"
    return None


async def _faltantes_de_disparadores(conn: asyncpg.Connection) -> list[str]:
    filas = await conn.fetch(_SQL_DISPARADORES, [d.nombre for d in DISPARADORES_ESPERADOS])
    presentes = {(f["nombre"], f["tabla"]): f for f in filas}
    faltan: list[str] = []
    for esperado in DISPARADORES_ESPERADOS:
        fila = presentes.get((esperado.nombre, esperado.tabla))
        problema = "disparador" if fila is None else _problema_de_disparador(esperado, fila)
        if problema:
            faltan.append(f"{problema}:{esperado.tabla}.{esperado.nombre}")
    return faltan


def _restriccion_correcta(esperada: RestriccionEsperada, fila) -> bool:
    if fila["tipo"] != esperada.tipo or not fila["validada"] or tuple(fila["columnas"] or ()) != esperada.columnas:
        return False
    if esperada.tipo == "f" and (
        fila["tabla_referenciada"] != esperada.tabla_referenciada
        or tuple(fila["columnas_referenciadas"] or ()) != esperada.columnas_referenciadas
    ):
        return False
    if esperada.condicion is not None:
        normalizada = "".join(c for c in fila["definicion"] if not c.isspace() and c not in "()")
        return normalizada == esperada.condicion
    return True


async def _faltantes_de_reportes(conn: asyncpg.Connection) -> list[str]:
    faltan: list[str] = []
    for columna, tipo, no_nula in COLUMNAS_ESPERADAS_REPORTES:
        fila = await conn.fetchrow(_SQL_DEFINICION_COLUMNA, TABLA_REPORTES, columna)
        if fila is None:
            faltan.append(f"columna:{TABLA_REPORTES}.{columna}")
        elif (fila["tipo"], fila["no_nula"]) != (tipo, no_nula):
            faltan.append(f"columna_incorrecta:{TABLA_REPORTES}.{columna}")
    for esperada in RESTRICCIONES_ESPERADAS:
        fila = await conn.fetchrow(_SQL_DEFINICION_RESTRICCION, esperada.tabla, esperada.nombre)
        if fila is None:
            faltan.append(f"restriccion:{esperada.tabla}.{esperada.nombre}")
        elif not _restriccion_correcta(esperada, fila):
            faltan.append(f"restriccion_incorrecta:{esperada.tabla}.{esperada.nombre}")
    if await conn.fetchval(_SQL_RESTRICCION, TABLA_REPORTES, RESTRICCION_REPORTES_OBSOLETA):
        faltan.append(f"restriccion_obsoleta:{TABLA_REPORTES}.{RESTRICCION_REPORTES_OBSOLETA}")
    return faltan


async def verificar_esquema_central(url: object, *, timeout_conexion: float = 10.0) -> None:
    """Lanza `ErrorMigracion(CENTRAL_SCHEMA_INCOMPATIBLE)` si la base transaccional no cumple lo que necesita el código."""
    try:
        async with asyncio.timeout(timeout_conexion):
            conn = await _conectar(url, BASE)
    except TimeoutError as exc:
        raise _base_no_disponible(BASE, exc) from None
    try:
        faltan = await elementos_faltantes(conn)
    except asyncpg.PostgresError as exc:
        raise ErrorMigracion(
            "CENTRAL_SCHEMA_CHECK_FAILED", f"No se pudo comprobar el esquema de la base {BASE}.", BASE, {"sqlstate": exc.sqlstate}
        ) from None
    finally:
        await conn.close()
    if faltan:
        raise ErrorMigracion(
            CODIGO,
            "El esquema de la base transaccional no es compatible con esta versión de la aplicación (falta algo que introducen las "
            "migraciones 0005, 0006 o 0007); sin él el inicio de sesión y la auditoría fallan.",
            BASE,
            {"faltan": faltan},
        )


async def exigir_esquema_central() -> None:
    """Paso del `lifespan`: si el esquema es incompatible, registra el motivo y deja que el arranque FALLE."""
    with paso_de_arranque(logger, "Compatibilidad del esquema central"):
        try:
            await verificar_esquema_central(settings.DATABASE_URL)
        except ErrorMigracion as exc:
            # Mensaje y detalles saneados (códigos y nombres de objetos): sin URL, credenciales ni texto del servidor.
            logger.critical(
                "La aplicación NO arranca: %s %s Detalles: %s. Revise en el log anterior por qué no se aplicaron las migraciones 0005, 0006 y 0007 "
                "(permisos del rol sobre auditoria/reportes_prospeccion/tipoeventoauditoria, PostgreSQL >= 12, bloqueos) y reinicie.",
                exc.codigo, exc.mensaje, exc.detalles,
            )
            raise

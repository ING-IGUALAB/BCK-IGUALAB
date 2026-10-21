"""Compatibilidad del esquema central con el código, comprobada en CADA arranque.

POR QUÉ EXISTE. La migración 0005 (auditoría con `resultado` y solo inserción, reportes inmutables) no declara `objetos`: las
tablas `auditoria` y `reportes_prospeccion` las crea `create_all` y los disparadores no forman parte de la firma que compara el
motor (ver su cabecera). Para el motor, una migración sin objetos queda "aplicada" una vez registrada y nada vuelve a comprobar que
su efecto siga en la base. Y el código SÍ depende de él: el ORM inserta `auditoria.resultado` en cada evento, también en el
inicio de sesión exitoso, de modo que con el esquema anterior TODO inicio de sesión falla con un 500.

QUÉ HACE. Después de preparar el esquema, `exigir_esquema_central` consulta el catálogo de PostgreSQL y comprueba lo que necesita
el código actual: la columna `auditoria.resultado`, todos los miembros de `TipoEventoAuditoria` en el tipo enumerado (los valores
se guardan por NOMBRE), la restricción `ck_auditoria_resultado_valido` y los dos disparadores de solo inserción, activos. Si algo
falta, la aplicación NO arranca: lanza `ErrorMigracion("CENTRAL_SCHEMA_INCOMPATIBLE")` desde el `lifespan`, el contenedor sale y
el despliegue falla a la vista, en lugar de anunciar `/health` y devolver 500 en el login. Eso incluye el caso en que la 0005
falló (o no llegó a ejecutarse porque falló una anterior): no se degrada a un aviso del módulo de análisis GRI.

LÍMITES. Comprueba presencia y estado de lo listado, no el texto de la restricción ni los permisos del rol de la base. Un
disparador desactivado a mano también impide el arranque: la protección de RN-044 / RN-041 no se da por vigente si está apagada.
"""
import asyncio
import logging

import asyncpg

from app.core.config import settings
from app.core.logging_config import paso_de_arranque
from app.migraciones.motor import ErrorMigracion, _base_no_disponible, _conectar
from app.models import TipoEventoAuditoria

logger = logging.getLogger("igualab.migraciones")

BASE = "transaccional"
CODIGO = "CENTRAL_SCHEMA_INCOMPATIBLE"

TABLA_AUDITORIA = "auditoria"
TIPO_EVENTO = "tipoeventoauditoria"
COLUMNA_RESULTADO = "resultado"
RESTRICCION_RESULTADO = "ck_auditoria_resultado_valido"
# (disparador, tabla): los crea la migración 0005.
DISPARADORES_SOLO_INSERCION = (
    ("tr_auditoria_solo_insercion", "auditoria"),
    ("tr_reportes_solo_insercion", "reportes_prospeccion"),
)

_SQL_COLUMNA = """
SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname = $1 AND a.attname = $2 AND a.attnum > 0 AND NOT a.attisdropped
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
SELECT t.tgname AS nombre, c.relname AS tabla, t.tgenabled::text AS estado
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname = ANY($1::text[])
"""


async def elementos_faltantes(conn: asyncpg.Connection) -> list[str]:
    """Lo que el código actual necesita del esquema central y la base no tiene, solo como nombres (nunca valores)."""
    faltan: list[str] = []
    if not await conn.fetchval(_SQL_COLUMNA, TABLA_AUDITORIA, COLUMNA_RESULTADO):
        faltan.append(f"columna:{TABLA_AUDITORIA}.{COLUMNA_RESULTADO}")
    etiquetas = {fila["enumlabel"] for fila in await conn.fetch(_SQL_ETIQUETAS, TIPO_EVENTO)}
    faltan += [f"valor_de_tipo:{TIPO_EVENTO}.{m.name}" for m in TipoEventoAuditoria if m.name not in etiquetas]
    if not await conn.fetchval(_SQL_RESTRICCION, TABLA_AUDITORIA, RESTRICCION_RESULTADO):
        faltan.append(f"restriccion:{TABLA_AUDITORIA}.{RESTRICCION_RESULTADO}")
    presentes = {
        (fila["nombre"], fila["tabla"]): fila["estado"]
        for fila in await conn.fetch(_SQL_DISPARADORES, [nombre for nombre, _ in DISPARADORES_SOLO_INSERCION])
    }
    for nombre, tabla in DISPARADORES_SOLO_INSERCION:
        estado = presentes.get((nombre, tabla))
        if estado is None:
            faltan.append(f"disparador:{tabla}.{nombre}")
        elif estado == "D":
            faltan.append(f"disparador_desactivado:{tabla}.{nombre}")
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
            "El esquema de la base transaccional no es compatible con esta versión de la aplicación (falta algo que introduce la "
            "migración 0005); sin él el inicio de sesión y la auditoría fallan.",
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
                "La aplicación NO arranca: %s %s Detalles: %s. Revise en el log anterior por qué no se aplicó la migración 0005 "
                "(permisos del rol sobre auditoria/reportes_prospeccion/tipoeventoauditoria, PostgreSQL >= 12, bloqueos) y reinicie.",
                exc.codigo, exc.mensaje, exc.detalles,
            )
            raise

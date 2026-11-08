"""Catálogo de migraciones de ingesta por base y punto de entrada para el arranque.

AGREGAR UNA MIGRACIÓN. (1) Cree el `.sql` con el siguiente número en `transaccional/` o `vectorial/` (sin BEGIN/COMMIT: el motor
la envuelve en una transacción); (2) declárela abajo con los objetos (tablas/vistas) que crea o modifica; (3) regenere `firmas.json`
con `IGUALAB_REGENERAR_FIRMAS=1 python -m pytest tests/services/test_migraciones_firmas.py`; (4) nunca edite una migración ya
aplicada en algún ambiente (el motor lo detecta por checksum): cree otra.

Las bases son DISTINTAS y se conectan por separado: transaccional con `DATABASE_URL`, vectorial con `VECTOR_DATABASE_URL`, sin
fallback entre ellas.
"""
import logging

from app.core.config import settings
from app.core.logging_config import paso_de_arranque
from app.migraciones.motor import (
    ErrorMigracion,
    EsquemaBase,
    Migracion,
    ResultadoMigracion,
    migrar,
)

logger = logging.getLogger("igualab.migraciones")

ESQUEMA_TRANSACCIONAL = EsquemaBase(
    nombre="transaccional",
    migraciones=(
        Migracion(1, "documentos_etapa_4a", "0001_documentos_etapa_4a.sql", ("documentos",), carpeta="transaccional"),
        Migracion(2, "documentos_coordinador", "0002_documentos_coordinador.sql", ("documentos",), carpeta="transaccional"),
        Migracion(3, "operaciones_ingesta", "0003_operaciones_ingesta.sql", ("operaciones_ingesta",), carpeta="transaccional"),
        # Módulo 5 (análisis y validación GRI). No toca `documentos`: guarda solo lo que decide una persona.
        Migracion(
            4, "analisis_gri", "0004_analisis_gri.sql",
            ("analisis_gri_control", "analisis_gri_estados", "analisis_gri_historial", "analisis_gri_validaciones"),
            carpeta="transaccional",
        ),
        # Auditoría con resultado y solo inserción (RN-043/044/045) y reportes inmutables (RN-041). Sin `objetos` A PROPÓSITO: las
        # tablas ya existen (create_all) y los disparadores no forman parte de la firma, así que no hay adopción posible; el SQL
        # es idempotente (ver su cabecera).
        Migracion(5, "auditoria_resultado_y_solo_insercion", "0005_auditoria_resultado_y_solo_insercion.sql", (), carpeta="transaccional"),
        # Reportes de prospección versionados (BUG-15). Sin `objetos` por la misma razón que la 0005: `reportes_prospeccion` la crea
        # `create_all` antes de las migraciones; el SQL es idempotente y conserva las filas existentes como versión 1.
        Migracion(6, "reportes_versionados", "0006_reportes_versionados.sql", (), carpeta="transaccional"),
        # Integridad de las emisiones (BUG-15, entrega 1): FK compuesta validación/empresa/año, rechazo de reportes nuevos sin validación.
        # La 0006 no se toca. Sin `objetos` (idempotente), como la 0005 y la 0006; añade una UNIQUE a `analisis_gri_validaciones`
        # solo como destino de la FK compuesta.
        Migracion(7, "integridad_de_reportes", "0007_integridad_de_reportes.sql", (), carpeta="transaccional"),
        # Sector «Otros» (OTROS) en el tipo `sector_empresa`, compartido por `empresas.sector` y `documentos.sector`. Sin `objetos`:
        # los tipos no forman parte de la firma y `create_all` ya lo crea con el valor en una instalación nueva (SQL idempotente).
        Migracion(8, "sector_otros", "0008_sector_otros.sql", (), carpeta="transaccional"),
    ),
    # Los crea el arranque de los módulos existentes (`crear_tablas`), que corre ANTES que esta preparación.
    prerrequisitos=(
        ("tabla", "usuarios"), ("tabla", "empresas"), ("tipo", "sector_empresa"),
        ("tabla", "auditoria"), ("tabla", "reportes_prospeccion"), ("tipo", "tipoeventoauditoria"),
    ),
)

ESQUEMA_VECTORIAL = EsquemaBase(
    nombre="vectorial",
    migraciones=(
        Migracion(
            1, "fragmentos_documento", "0001_fragmentos_documento.sql",
            ("fragmentos_documento", "fragmentos_consultables"), carpeta="vectorial",
        ),
        Migracion(2, "cierres_documento", "0002_cierres_documento.sql", ("cierres_documento",), carpeta="vectorial"),
    ),
    requiere_pgvector=True,
)


def _url_vectorial() -> str:
    """URL de la base vectorial, SIN fallback a DATABASE_URL (error controlado si falta)."""
    from app.core.database_vectorial import url_vectorial
    from app.core.exceptions import ExternalServiceError

    try:
        return url_vectorial()
    except ExternalServiceError as exc:
        raise ErrorMigracion(
            "MIGRATION_DB_NOT_CONFIGURED", "La base vectorial no está configurada (VECTOR_DATABASE_URL).", "vectorial",
            {"causa": exc.code},
        ) from None


async def asegurar_esquema_transaccional() -> ResultadoMigracion:
    """Lleva SOLO la base transaccional al último esquema. Lo usa el Módulo 5 (análisis GRI) en el arranque para no depender de que
    la ingesta (OCI, MinIO, base vectorial) esté configurada: sus tablas viven en la base transaccional. Es idempotente: si
    `asegurar_esquema_ingesta` ya las aplicó (o las aplica después) no se ejecuta ningún SQL."""
    with paso_de_arranque(logger, "Esquema transaccional del análisis GRI"):
        return await migrar(settings.DATABASE_URL, ESQUEMA_TRANSACCIONAL)


async def asegurar_esquema_ingesta() -> list[ResultadoMigracion]:
    """Prepara las dos bases, primero la transaccional y luego la vectorial. Se detiene en el primer fallo con `ErrorMigracion`
    (la aplicación mantiene los demás módulos y responde 503 en ingesta). Un arranque sin cambios no ejecuta ningún SQL."""
    resultados = []
    pasos = {"transaccional": "4/6 Migraciones transaccionales", "vectorial": "5/6 Migraciones vectoriales"}
    for esquema, url in ((ESQUEMA_TRANSACCIONAL, lambda: settings.DATABASE_URL), (ESQUEMA_VECTORIAL, _url_vectorial)):
        with paso_de_arranque(logger, pasos[esquema.nombre]):
            resultado = await migrar(url(), esquema)
        resultados.append(resultado)
        if resultado.sin_cambios:
            logger.info("Esquema %s al día (migraciones %s).", esquema.nombre, list(resultado.previas))
        else:
            logger.info("Esquema %s: aplicadas %s, adoptadas %s.", esquema.nombre, list(resultado.aplicadas), list(resultado.adoptadas))
    return resultados

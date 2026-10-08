"""Catálogo de migraciones de ingesta por base y punto de entrada para el arranque.

AGREGAR UNA MIGRACIÓN. (1) Cree el `.sql` con el siguiente número en `transaccional/` o `vectorial/` (sin BEGIN/COMMIT: el motor
la envuelve en una transacción); (2) declárela abajo con los objetos (tablas/vistas) que crea o modifica; (3) regenere `firmas.json`
con `IGUALAB_REGENERAR_FIRMAS=1 python -m pytest tests/services/test_migraciones_firmas.py`; (4) nunca edite una migración ya
aplicada en algún ambiente (el motor lo detecta por checksum): cree otra.

Las bases son DISTINTAS y se conectan por separado: transaccional con `DATABASE_URL`, vectorial con `VECTOR_DATABASE_URL`, sin
fallback entre ellas.
"""
import logging

from app.config import settings
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
    ),
    # Los crea el arranque de los módulos existentes (`crear_tablas`), que corre ANTES que esta preparación.
    prerrequisitos=(("tabla", "usuarios"), ("tabla", "empresas"), ("tipo", "sector_empresa")),
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
    from app.database_vectorial import url_vectorial
    from app.exceptions import ExternalServiceError

    try:
        return url_vectorial()
    except ExternalServiceError as exc:
        raise ErrorMigracion(
            "MIGRATION_DB_NOT_CONFIGURED", "La base vectorial no está configurada (VECTOR_DATABASE_URL).", "vectorial",
            {"causa": exc.code},
        ) from None


async def asegurar_esquema_ingesta() -> list[ResultadoMigracion]:
    """Prepara las dos bases, primero la transaccional y luego la vectorial. Se detiene en el primer fallo con `ErrorMigracion`
    (la aplicación mantiene los demás módulos y responde 503 en ingesta). Un arranque sin cambios no ejecuta ningún SQL."""
    resultados = []
    for esquema, url in ((ESQUEMA_TRANSACCIONAL, lambda: settings.DATABASE_URL), (ESQUEMA_VECTORIAL, _url_vectorial)):
        resultado = await migrar(url(), esquema)
        resultados.append(resultado)
        if resultado.sin_cambios:
            logger.info("Esquema %s al día (migraciones %s).", esquema.nombre, list(resultado.previas))
        else:
            logger.info("Esquema %s: aplicadas %s, adoptadas %s.", esquema.nombre, list(resultado.aplicadas), list(resultado.adoptadas))
    return resultados

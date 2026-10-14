"""Preparación del esquema del Módulo 5 en el arranque (`lifespan`), INDEPENDIENTE de la ingesta.

Las tablas del análisis GRI viven en la base transaccional (migración 0004). Este paso lleva SOLO esa base al último esquema
con el mismo motor de migraciones versionadas: no construye el gestor de ingesta ni exige OCI, MinIO ni la base vectorial, así
que el análisis queda disponible aunque la ingesta no esté configurada. Si ya aplicó las migraciones `asegurar_esquema_ingesta`
(o las aplica después), el motor no ejecuta ningún SQL.

Si falla, la aplicación arranca igual: los demás módulos siguen disponibles y `/analisis-gri` responde 503
`ANALYSIS_SCHEMA_NOT_READY` (sin URLs ni credenciales).
"""
import logging

logger = logging.getLogger("igualab.analisis_gri")


async def preparar_esquema_analisis(app) -> None:
    """Deja `app.state.analisis_error` en `None` (listo) o en `(código, mensaje, detalles)` (no disponible)."""
    from app.migraciones.catalogo import asegurar_esquema_transaccional
    from app.migraciones.motor import ErrorMigracion

    app.state.analisis_error = None
    try:
        await asegurar_esquema_transaccional()
    except ErrorMigracion as exc:
        app.state.analisis_error = (
            "ANALYSIS_SCHEMA_NOT_READY",
            f"El análisis GRI no está disponible: no se pudo preparar el esquema de la base {exc.base}. {exc.mensaje}",
            {"base": exc.base, "motivo": exc.codigo, **exc.detalles},
        )
        # El mensaje y los detalles del ErrorMigracion están saneados (código, versión, SQLSTATE); sin URL ni credenciales.
        # `exc_info=False`: el traceback encadenado podría arrastrar la causa original (URL, credenciales) del driver.
        logger.exception(
            "Análisis GRI no disponible: la preparación del esquema de la base %s falló (%s). %s Detalles: %s",
            exc.base, exc.codigo, exc.mensaje, exc.detalles,
            exc_info=False,
        )
    except Exception as exc:  # noqa: BLE001 - el arranque no debe caerse por este módulo
        app.state.analisis_error = (
            "ANALYSIS_SCHEMA_NOT_READY",
            "El análisis GRI no está disponible por un error de inicialización.",
            {"motivo": type(exc).__name__},
        )
        # Solo el nombre del tipo: el texto de una excepción inesperada puede contener URLs o credenciales.
        logger.exception("Análisis GRI no disponible: error de inicialización (%s).", type(exc).__name__, exc_info=False)

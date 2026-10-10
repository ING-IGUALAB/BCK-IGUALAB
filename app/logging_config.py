import logging
import time
from contextlib import contextmanager


def configure_logging() -> None:
    """Configura logs locales sin interferir con los handlers de Uvicorn."""
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    logging.getLogger("igualab").setLevel(logging.INFO)



@contextmanager
def paso_de_arranque(logger: logging.Logger, paso: str):
    """Registra INFO al iniciar y al terminar un paso del arranque, con su duración. Si el paso falla o se interrumpe también lo
    registra (solo la CLASE de la excepción: su mensaje puede contener hosts o credenciales) y vuelve a lanzarla. No cambia el
    comportamiento del paso. Un `inicio` sin su `fin` en el log indica el paso en el que la aplicación sigue esperando."""
    logger.info("Arranque | %s | inicio", paso)
    inicio = time.monotonic()
    try:
        yield
    except BaseException as exc:
        logger.error("Arranque | %s | falló o se interrumpió tras %.2f s (%s)", paso, time.monotonic() - inicio, type(exc).__name__)
        raise
    logger.info("Arranque | %s | fin en %.2f s", paso, time.monotonic() - inicio)

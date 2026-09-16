import logging


def configure_logging() -> None:
    """Configura logs locales sin interferir con los handlers de Uvicorn."""
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    logging.getLogger("igualab").setLevel(logging.INFO)


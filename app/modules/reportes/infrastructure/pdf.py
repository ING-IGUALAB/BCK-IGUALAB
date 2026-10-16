"""Compilación HTML → PDF del reporte de prospección (Módulo 7). Adaptador de INFRAESTRUCTURA:
único punto que invoca WeasyPrint (la plantilla vive en `plantillas/`). Se inyecta como función para
poder probar el servicio con un doble sin depender del motor de PDF."""
from __future__ import annotations

from app.core.exceptions import ExternalServiceError


def compilar_html_a_pdf(html: str, *, timeout_segundos: float = 60.0) -> bytes:
    """Convierte el HTML del reporte a PDF con WeasyPrint y devuelve los bytes."""
    try:
        import weasyprint

        return weasyprint.HTML(string=html).write_pdf()
    except Exception as exc:  # noqa: BLE001 — WeasyPrint no instalado o error de render
        raise ExternalServiceError(
            "PDF_GENERATION_ERROR", "No se pudo generar el PDF del reporte (motor de PDF no disponible)."
        ) from exc

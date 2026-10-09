"""Reglas de admisión sin dependencias de BD ni HTTP (LTX:RF-012, RF-013).

Se mantiene separado de `validacion` para que `app.schemas` pueda usarlo sin
importar servicios que a su vez dependen de los schemas.
"""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# D07 (2026-10-05): 50 MB = 50 000 000 bytes del archivo, no de la petición multipart.
TAMANO_MAXIMO_BYTES = 50_000_000

# D20 (2026-10-05): de 2000 al año actual en America/Lima, ambos inclusive.
ANIO_MINIMO = 2000
ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Lima")

EXTENSION_PERMITIDA = ".md"
# LTX §9: documentos.nombre_archivo VARCHAR(255).
LONGITUD_MAXIMA_NOMBRE_ARCHIVO = 255

# Solo dígitos ASCII: sin signo, espacios, decimales ni dígitos Unicode.
_ANIO_TEXTO = re.compile(r"\d{1,9}", re.ASCII)


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def anio_actual() -> int:
    return _ahora().astimezone(ZONA_HORARIA_NEGOCIO).year


def interpretar_anio(valor: object) -> int:
    """Acepta un entero o su texto; nunca trunca ni redondea."""
    # bool es subclase de int: debe descartarse antes.
    if isinstance(valor, bool):
        raise ValueError("El año debe ser un número entero, no un valor lógico.")
    if isinstance(valor, int):
        anio = valor
    elif isinstance(valor, str) and _ANIO_TEXTO.fullmatch(valor):
        anio = int(valor)
    else:
        raise ValueError("El año debe ser un número entero sin decimales.")

    maximo = anio_actual()
    if not ANIO_MINIMO <= anio <= maximo:
        raise ValueError(f"El año debe estar entre {ANIO_MINIMO} y {maximo}.")
    return anio

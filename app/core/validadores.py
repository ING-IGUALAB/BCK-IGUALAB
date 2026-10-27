"""Validadores específicos y reutilizables para datos de entrada.

No hay un «texto seguro» genérico: cada campo compone solo las reglas que le corresponden. Ninguna regla recorta, normaliza ni elimina
caracteres: si el valor no es aceptable se RECHAZA (422 REQUEST_VALIDATION_ERROR), y los valores ya almacenados no se tocan.

Por qué existen: un NUL (U+0000) o un sustituto suelto (U+D800..U+DFFF) en un texto que llega a PostgreSQL o a una biblioteca de hash
terminaba en un 500 (`asyncpg.CharacterNotInRepertoireError`, `UnicodeEncodeError`); un campo `String(200)` sin tope de longitud, igual.
"""
import unicodedata
from typing import Annotated

from pydantic import AfterValidator, Field

# Categorías Unicode: Cc = controles (NUL, tabulador, saltos de línea, DEL, C1) y Cs = sustitutos sueltos (no codificables en UTF-8).
_CATEGORIAS_DE_CONTROL = frozenset({"Cc", "Cs"})

# Longitudes máximas = tamaño real de las columnas (`usuarios.nombre` y `empresas.nombre` son VARCHAR(200)).
LONGITUD_MAXIMA_NOMBRE = 200


def sin_caracteres_de_control(valor: str) -> str:
    """Rechaza NUL y demás caracteres de control (incluidos tabuladores y saltos de línea internos) y sustitutos sueltos."""
    if any(unicodedata.category(caracter) in _CATEGORIAS_DE_CONTROL for caracter in valor):
        raise ValueError("No puede contener caracteres de control (como NUL, tabuladores o saltos de línea).")
    return valor


def sin_marcado_html(valor: str) -> str:
    """Rechaza «<» y «>». No limita tildes, ñ, apóstrofes, «&», puntuación ni otros caracteres de nombres legítimos."""
    if "<" in valor or ">" in valor:
        raise ValueError("No puede contener los caracteres «<» ni «>».")
    return valor


def _nombre_de_empresa(valor: str) -> str:
    return sin_marcado_html(sin_caracteres_de_control(valor))


# Nombre de una empresa: 1..200 caracteres, sin caracteres de control ni «<» «>». (El recorte de espacios en los extremos lo sigue
# haciendo `str_strip_whitespace` de cada modelo, como antes.)
NombreEmpresa = Annotated[
    str, Field(min_length=1, max_length=LONGITUD_MAXIMA_NOMBRE), AfterValidator(_nombre_de_empresa)
]

# Nombre de una cuenta: hasta 200 caracteres (la columna) y sin caracteres de control. No se añade un mínimo ni se rechaza «<» «>»:
# solo se corrige lo que provocaba un 500.
NombreUsuario = Annotated[str, Field(max_length=LONGITUD_MAXIMA_NOMBRE), AfterValidator(sin_caracteres_de_control)]

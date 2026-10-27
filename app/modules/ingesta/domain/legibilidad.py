"""Versión LEGIBLE (solo para presentar) de una cita o de una sección del documento.

El análisis guarda la cita y la sección tal como están en el Markdown: con `###`, `**`, `<br>`, barras de tabla, enlaces. Eso
es evidencia y NO se modifica. Aquí se calcula, al presentar, un texto limpio que se muestra junto al original:

    «### Emisiones»                     → «Emisiones»
    «La empresa<br>redujo emisiones»    → «La empresa⏎redujo emisiones»   (el <br> conserva un salto de línea)
    «**Gestión ambiental**»             → «Gestión ambiental»
    «| **Emisiones** | GRI 305-1 |»     → «Emisiones | GRI 305-1»          (las celdas siguen separadas)
    «[informe](https://x.pe/a.pdf)»     → «informe»                        (se conserva el texto del enlace)

Qué se retira: solo las marcas que son FORMATO (`#` de encabezado, `>` de cita, énfasis, enlaces y notas al pie, delimitadores
de tablas y de código en línea, etiquetas HTML de formato y de estructura, escapes de Markdown, reglas horizontales). Se
conservan números, monedas, porcentajes, códigos, signos matemáticos, acentos y cualquier símbolo que sea contenido: nunca se
borra por una expresión regular indiscriminada. Un `*` solitario, «3*4» o `un_nombre` no son formato (ver `vista_markdown`).

Garantías y límites:
- El resultado es TEXTO PLANO. No se interpreta ni se ejecuta HTML, no se carga ningún recurso (una imagen se reduce a su texto
  alternativo) y las etiquetas desconocidas se dejan como texto: quien lo muestre debe escaparlo, como el original.
- No es una transcripción literal: al retirar marcas el texto cambia. Las posiciones del original NO sirven de índice aquí;
  el vínculo con la evidencia sigue siendo el campo original y sus posiciones.
- No reconstruye contenido: si una cita guardada empieza a mitad de palabra (análisis anterior a la corrección de límites),
  se muestra tal cual sin completarla.
- Determinista y sin red, base de datos ni estado.
"""
import re
from functools import lru_cache

from app.modules.ingesta.domain import markdown
from app.modules.ingesta.domain.vista_markdown import _fragmentos_de_codigo, construir_vista

SEPARADOR_RUTA = " > "

_FIN_DE_LINEA = re.compile(r"\r\n|\r|\n")
# Texto de una sola línea sin ningún carácter que pueda ser marca (`#`, `*`, `_`, `~`, `[`, `<`, `>`, `&`, `\`, `|`, acento grave)
# ni de control: no hay nada que retirar (salvo una regla horizontal de guiones, que se descarta en el camino normal).
_SIN_FORMATO = re.compile(r"[^#*_~\[<>&\\|`\x00-\x08\x0a-\x1f\x7f-\x9f]*")
_BLANCOS = re.compile(r"[^\S\n]+")
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_REGLA_HORIZONTAL = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*")
_IMAGEN = re.compile(r"!\[([^\[\]\n]*)\]\((?:[^()\n]*)\)")
# Etiquetas que cierran un bloque o una línea: dejan un salto.
_RUPTURA_HTML = re.compile(
    r"<br\s*/?>|<hr\s*/?>|</(?:p|div|li|tr|h[1-6]|ul|ol|table|thead|tbody|tfoot|blockquote|pre)\s*>", re.IGNORECASE
)
_CELDA_HTML = re.compile(r"</t[dh]\s*>", re.IGNORECASE)
# Marcadores internos (caracteres de control U+0001 y U+0000): separan celdas de una tabla HTML y líneas mientras se limpia.
# Se retiran del contenido antes de empezar, así que no pueden confundirse con él.
_MARCA_DE_CELDA = chr(1)
_MARCA_DE_RUPTURA = chr(0)
# Etiquetas de formato y de estructura: se retiran, se conserva lo que contienen.
_ETIQUETA_DE_FORMATO = re.compile(
    r"</?(?:b|i|u|s|em|strong|small|sup|sub|mark|span|font|a|code|del|ins|abbr|cite|q|center|"
    r"p|div|li|ul|ol|table|thead|tbody|tfoot|tr|td|th|h[1-6]|blockquote|pre)(?:\s[^<>\n]*)?/?>",
    re.IGNORECASE,
)
_ENTIDADES = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_ENTIDAD = re.compile(r"&(nbsp|amp|lt|gt|quot|apos|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});")


def _entidad(m: re.Match) -> str:
    nombre = m.group(1)
    if nombre in _ENTIDADES:
        return _ENTIDADES[nombre]
    codigo = int(nombre[2:], 16) if nombre[1] in "xX" else int(nombre[1:])
    if codigo == 0 or codigo > 0x10FFFF or 0xD800 <= codigo <= 0xDFFF:
        return m.group(0)
    caracter = chr(codigo)
    return " " if caracter.isspace() else (caracter if caracter.isprintable() else m.group(0))


def _fuera_de_codigo(linea: str, patron: re.Pattern, reemplazo: str) -> str:
    """Sustituye `patron` salvo dentro de fragmentos `código en línea` (que se conservan tal cual)."""
    if not patron.search(linea):
        return linea
    protegidos = _fragmentos_de_codigo(linea, 0)
    partes: list[str] = []
    cursor = 0
    for inicio, fin in [*protegidos, (len(linea), len(linea))]:
        partes.append(patron.sub(reemplazo, linea[cursor:inicio]))
        partes.append(linea[inicio:fin])
        cursor = fin
    return "".join(partes)


def _sin_delimitadores_de_codigo(texto: str) -> str:
    """Quita los acentos graves que abren y cierran un fragmento `código en línea`; el contenido del fragmento se conserva."""
    fragmentos = _fragmentos_de_codigo(texto, 0)
    if not fragmentos:
        return texto
    partes: list[str] = []
    cursor = 0
    for inicio, fin in fragmentos:
        partes.append(texto[cursor:inicio])
        contenido = texto[inicio:fin]
        grosor = len(contenido) - len(contenido.lstrip("`"))
        partes.append(contenido[grosor:len(contenido) - grosor])
        cursor = fin
    partes.append(texto[cursor:])
    return "".join(partes)


def _en_linea(texto: str) -> str:
    """Formato en línea de un tramo sin saltos: HTML de formato, imágenes, enlaces, notas, énfasis, código y escapes."""
    texto = _fuera_de_codigo(texto, _CELDA_HTML, _MARCA_DE_CELDA)
    texto = _fuera_de_codigo(texto, _ETIQUETA_DE_FORMATO, "")
    texto = _fuera_de_codigo(texto, _IMAGEN, r"\1")
    texto = construir_vista(texto).plano  # enlaces, notas al pie, énfasis, <br> residual, `>` de cita
    texto = _sin_delimitadores_de_codigo(texto)
    texto = _ESCAPE.sub(r"\1", texto)
    return _ENTIDAD.sub(_entidad, texto)


def _unir_celdas(celdas: list[str]) -> str:
    """Celdas separadas por « | ». Las vacías de los extremos (del `|` inicial o final) no se muestran; las interiores sí, para no
    desplazar las columnas."""
    limpias = [_BLANCOS.sub(" ", celda).strip() for celda in celdas]
    while limpias and not limpias[0]:
        limpias.pop(0)
    while limpias and not limpias[-1]:
        limpias.pop()
    return " | ".join(limpias)


def _separar_rupturas(linea: str) -> list[str]:
    """Parte la línea en cada `<br>` o cierre de bloque HTML que no esté dentro de un fragmento de código."""
    return _fuera_de_codigo(linea, _RUPTURA_HTML, _MARCA_DE_RUPTURA).split(_MARCA_DE_RUPTURA)


def _fila_de_tabla(linea: str) -> list[str]:
    """Una fila de tabla Markdown es UNA línea legible: un `<br>` dentro de una celda se muestra como espacio, para que las celdas
    no se desplacen."""
    if markdown.es_delimitador(linea):
        return []
    celdas = [_fuera_de_codigo(celda, _RUPTURA_HTML, " ") for celda in markdown.celdas(linea)]
    return [_unir_celdas([_en_linea(celda) for celda in celdas])]


def _limpiar_linea(linea: str) -> list[str]:
    """Líneas legibles que produce una línea del Markdown (puede ser ninguna, una o varias si traía `<br>`)."""
    if linea.lstrip().startswith("|"):
        return _fila_de_tabla(linea)
    resultado: list[str] = []
    for tramo in _separar_rupturas(linea):
        if _REGLA_HORIZONTAL.fullmatch(tramo):
            continue
        titulo = markdown.titulo_atx(tramo)
        if titulo is not None:
            tramo = titulo[1]
        resultado.append(_unir_celdas(_en_linea(tramo).split(_MARCA_DE_CELDA)))
    return resultado


@lru_cache(maxsize=4096)
def texto_legible(texto: str) -> str:
    """Versión legible de una cita: sin marcas de formato, con los saltos de línea, de párrafo y de celda conservados.

    Función pura; se memoiza (acotada) porque un documento repite las mismas citas y secciones miles de veces."""
    if _SIN_FORMATO.fullmatch(texto) is not None and _REGLA_HORIZONTAL.fullmatch(texto) is None:  # camino rápido
        return _BLANCOS.sub(" ", texto).strip()
    texto = texto.replace(_MARCA_DE_RUPTURA, " ").replace(_MARCA_DE_CELDA, " ")
    lineas: list[str] = []
    for linea in _FIN_DE_LINEA.split(texto):
        lineas.extend(_limpiar_linea(linea))
    lineas = [_BLANCOS.sub(" ", linea).strip() for linea in lineas]
    # Una sola línea en blanco entre párrafos; ninguna al principio ni al final.
    compactadas: list[str] = []
    for linea in lineas:
        if linea or (compactadas and compactadas[-1]):
            compactadas.append(linea)
    while compactadas and not compactadas[-1]:
        compactadas.pop()
    return "\n".join(compactadas)


@lru_cache(maxsize=4096)
def seccion_legible(seccion: str) -> str:
    """Versión legible de la ruta de secciones («Informe > **Gestión**<br>ambiental»): cada título limpio y en una línea."""
    titulos = []
    for titulo in seccion.split(SEPARADOR_RUTA):
        limpio = _BLANCOS.sub(" ", texto_legible(titulo).replace("\n", " ")).strip()
        if limpio:
            titulos.append(limpio)
    return SEPARADOR_RUTA.join(titulos)

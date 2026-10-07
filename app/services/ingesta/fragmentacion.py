"""Fragmentación trazable de documentos Markdown (LTX:RF-016 parcial, RNF-015, RNF-025).

Recibe el texto ya validado (`DocumentoValidado.texto`) y produce fragmentos
deterministas y ordenados. No depende de FastAPI, de la BD ni de ningún
proveedor de embeddings, y no valida la admisión del documento.

CONTRATO DE SALIDA (`Fragmento`)
- `inicio`/`fin`: posiciones de caracteres (puntos de código Unicode) del texto
  INTERPRETADO que se recibe: inicio inclusivo, fin exclusivo. No son offsets de
  bytes del archivo original ni posiciones de página. La Etapa 1 omite el BOM
  inicial al interpretar, de modo que el rango se mide sobre `texto` sin BOM
  (si `tiene_bom`, la posición en el texto decodificado con BOM sería +1).
- `texto_literal == texto[inicio:fin]`, sin normalizar CRLF, espacios, tildes
  ni Unicode. Es la única evidencia citable.
- Los rangos son consecutivos y sin solapamiento: el primero empieza en 0, cada
  uno empieza donde acaba el anterior y el último acaba en `len(texto)`. La
  concatenación de los `texto_literal` reconstruye exactamente el texto.
- `contexto`: texto añadido SOLO para embeddings (ruta de encabezados cuando
  no está dentro del literal y encabezado de la tabla cuando el fragmento
  empieza en sus filas). Nunca forma parte de la reconstrucción ni es
  evidencia. `texto_embedding == contexto + texto_literal`.
- `ruta_encabezados`: títulos (sin marcas `#`) vigentes para el contenido del
  fragmento; vacía si no hay encabezados antes. Un fragmento con contenido no
  cruza un encabezado, así que la ruta es única.
- `continuacion`: True si el fragmento empieza en medio de un bloque (párrafo,
  fila, bloque de código o encabezado) que superó el límite y se dividió. Es
  la única representación de continuaciones: el texto de origen no se marca ni
  se repite, y el contexto sí se repite (ruta y, en tablas, encabezado).

LÍMITES (en CARACTERES, no en tokens; sin relación con ningún modelo)
- `max_caracteres`: longitud máxima de `texto_literal`. PROVISIONAL.
- `max_caracteres_contexto`: longitud máxima de `contexto`, incluido su
  separador final. Si el contexto no cabe se acorta con «…». PROVISIONAL.
  Por tanto `len(texto_embedding) <= max_caracteres + max_caracteres_contexto`.
- Nunca se trunca el literal: un bloque mayor que el límite se divide en varios
  fragmentos con rangos exactos.

ESTRATEGIA
- Un bloque que cabe en un fragmento no se divide; si no cabe en el espacio que
  queda, empieza un fragmento nuevo. Un encabezado abre un fragmento nuevo salvo
  que el actual solo tenga encabezados (así un título no queda huérfano).
  Consecuencia asumida: las secciones cortas producen fragmentos cortos.
- Tabla que cabe: se mantiene junta. Tabla mayor que el límite: se divide por
  filas, con encabezado + delimitador como unidad; cada fila se divide solo si
  ella sola supera el límite. El encabezado de tabla se repite únicamente en el
  contexto (una línea, acotada), nunca como literal ni la tabla completa.
  Tabla con columnas inconsistentes (decisión 2026-10-07): se divide igual, con
  literales exactos; no se repite su encabezado como contexto porque sugeriría
  relaciones columna-celda que el documento no sostiene. No se rellenan celdas
  ni se reordenan columnas.
- Bloque mayor que el límite: corte preferente en salto de línea, luego fin de
  oración, luego espacio, y por último corte duro; los tres primeros solo se
  aceptan si dejan al menos la mitad del espacio disponible. El corte no separa
  CRLF ni una marca combinante/ZWJ de su carácter base cuando puede evitarse.
- Los encabezados ATX y las cercas de código se reconocen con `markdown`; el
  contenido de un bloque cercado nunca se interpreta como título.

RECURSOS
Se recorre el texto una vez, por líneas, sin copiarlo entero (solo se crean los
literales de cada fragmento). `iterar_fragmentos` los produce de forma
incremental; `fragmentar` los reúne en una lista. El consumo de memoria y el
tiempo con documentos de hasta 50 MB NO están medidos (T27 pendiente).
"""
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, replace

from app.services.ingesta import markdown

# Valores PROVISIONALES hasta definir proveedor y modelo de embeddings (D12).
MAX_CARACTERES_PREDETERMINADO = 2000
MAX_CARACTERES_CONTEXTO_PREDETERMINADO = 400
# Con menos de 2 no se podría avanzar sin partir un CRLF.
MIN_CARACTERES = 2

SEPARADOR_RUTA = " > "
_FIN_DE_CONTEXTO = "\n\n"
_PREFIJO_SECCION = "Sección: "
_PREFIJO_TABLA = "Encabezado de tabla: "
# Margen al leer el encabezado de tabla: se acota igual al armar el contexto.
_MARGEN_ENCABEZADO = 8

_FIN_DE_ORACION = re.compile(r"[.!?…][\"')\]»”’]*\s")
_ESPACIO = re.compile(r"\s")
# Condición necesaria de una fila delimitadora; evita copiar líneas largas.
_SOLO_DELIMITADOR = re.compile(r"[ \t|:\-]*")

_ENCABEZADO = "encabezado"
_TEXTO = "texto"
_CODIGO = "codigo"
_TABLA = "tabla"
_VACIO = "vacio"


def _exigir_entero(nombre: str, valor: object, minimo: int) -> None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ValueError(f"{nombre} debe ser un entero de caracteres, no {type(valor).__name__}.")
    if valor < minimo:
        raise ValueError(f"{nombre} debe ser al menos {minimo}; se recibió {valor}.")


@dataclass(frozen=True)
class ParametrosFragmentacion:
    """Límites en caracteres. Los valores predeterminados son provisionales."""

    max_caracteres: int = MAX_CARACTERES_PREDETERMINADO
    max_caracteres_contexto: int = MAX_CARACTERES_CONTEXTO_PREDETERMINADO

    def __post_init__(self) -> None:
        _exigir_entero("max_caracteres", self.max_caracteres, MIN_CARACTERES)
        _exigir_entero("max_caracteres_contexto", self.max_caracteres_contexto, 0)


@dataclass(frozen=True, slots=True)
class Fragmento:
    indice: int
    inicio: int
    fin: int
    texto_literal: str
    ruta_encabezados: tuple[str, ...]
    contexto: str
    continuacion: bool

    @property
    def seccion(self) -> str:
        return SEPARADOR_RUTA.join(self.ruta_encabezados)

    @property
    def texto_embedding(self) -> str:
        return self.contexto + self.texto_literal


@dataclass(frozen=True, slots=True)
class _Tabla:
    inicio_cuerpo: int
    fin_filas: int
    encabezado: str


@dataclass(frozen=True, slots=True)
class _Bloque:
    tipo: str
    inicio: int
    fin: int
    nivel: int = 0
    titulo: str = ""
    tabla: _Tabla | None = None


# --- Bloques de Markdown ------------------------------------------------------
# Cada bloque incluye las líneas en blanco que lo siguen (o que lo preceden, si
# es el primero), de modo que los bloques cubren el texto sin huecos.

def _saltar_blancas(texto: str, posicion: int) -> int:
    longitud = len(texto)
    while posicion < longitud:
        fin_contenido, fin = markdown.limites_de_linea(texto, posicion)
        if not markdown.es_blanca(texto, posicion, fin_contenido):
            break
        posicion = fin
    return posicion


def _fin_de_codigo(texto: str, posicion: int, marca: str) -> int:
    """Primera posición posterior a la cerca de cierre (o fin del texto)."""
    longitud = len(texto)
    while posicion < longitud:
        fin_contenido, fin = markdown.limites_de_linea(texto, posicion)
        cierra = markdown.cierra_cerca(texto, marca, posicion, fin_contenido)
        posicion = fin
        if cierra:
            break
    return posicion


def _inicia_tabla(texto: str, inicio: int, fin_contenido: int, inicio_siguiente: int) -> bool:
    """Misma definición que el validador; los filtros previos evitan copiar
    líneas largas que no pueden ser encabezado + delimitador."""
    if inicio_siguiente >= len(texto) or not markdown.tiene_pipe(texto, inicio, fin_contenido):
        return False
    siguiente_contenido, _ = markdown.limites_de_linea(texto, inicio_siguiente)
    if _SOLO_DELIMITADOR.fullmatch(texto, inicio_siguiente, siguiente_contenido) is None:
        return False
    return markdown.es_inicio_tabla(
        texto[inicio:fin_contenido], texto[inicio_siguiente:siguiente_contenido]
    )


def _leer_tabla(texto: str, inicio: int, fin_contenido: int, fin: int, cap: int) -> tuple[_Tabla, int]:
    """`inicio` es la línea de encabezado; `fin` el inicio de la línea delimitadora."""
    longitud = len(texto)
    _, posicion = markdown.limites_de_linea(texto, fin)
    inicio_cuerpo = posicion
    while posicion < longitud:
        siguiente_contenido, siguiente = markdown.limites_de_linea(texto, posicion)
        if not markdown.puede_ser_fila(texto, posicion, siguiente_contenido):
            break
        posicion = siguiente
    encabezado = texto[inicio:min(fin_contenido, inicio + cap)].strip()
    return _Tabla(inicio_cuerpo, posicion, encabezado), posicion


def _fin_de_parrafo(texto: str, posicion: int) -> int:
    """`posicion` es el inicio de la línea siguiente a la primera del párrafo."""
    longitud = len(texto)
    while posicion < longitud:
        fin_contenido, fin = markdown.limites_de_linea(texto, posicion)
        if (
            markdown.es_blanca(texto, posicion, fin_contenido)
            or markdown.titulo_atx(texto, posicion, fin_contenido) is not None
            or markdown.marca_apertura_cerca(texto, posicion, fin_contenido) is not None
            or _inicia_tabla(texto, posicion, fin_contenido, fin)
        ):
            break
        posicion = fin
    return posicion


def _bloques(texto: str, cap_encabezado: int) -> Iterator[_Bloque]:
    longitud = len(texto)
    cursor = 0
    while cursor < longitud:
        inicio = _saltar_blancas(texto, cursor)
        if inicio >= longitud:
            yield _Bloque(_VACIO, cursor, longitud)
            return
        fin_contenido, fin = markdown.limites_de_linea(texto, inicio)
        marca = markdown.marca_apertura_cerca(texto, inicio, fin_contenido)
        encabezado = markdown.titulo_atx(texto, inicio, fin_contenido)
        if marca is not None:
            final = _saltar_blancas(texto, _fin_de_codigo(texto, fin, marca))
            bloque = _Bloque(_CODIGO, cursor, final)
        elif encabezado is not None:
            nivel, titulo = encabezado
            bloque = _Bloque(_ENCABEZADO, cursor, _saltar_blancas(texto, fin), nivel, titulo)
        elif _inicia_tabla(texto, inicio, fin_contenido, fin):
            tabla, fin_filas = _leer_tabla(texto, inicio, fin_contenido, fin, cap_encabezado)
            bloque = _Bloque(_TABLA, cursor, _saltar_blancas(texto, fin_filas), tabla=tabla)
        else:
            bloque = _Bloque(_TEXTO, cursor, _saltar_blancas(texto, _fin_de_parrafo(texto, fin)))
        yield bloque
        cursor = bloque.fin


# --- Cortes -------------------------------------------------------------------

def _extiende_caracter(caracter: str) -> bool:
    return caracter == "\u200d" or unicodedata.category(caracter).startswith("M")


def _corte_seguro(texto: str, posicion: int) -> bool:
    """No parte un CRLF ni separa una marca combinante / ZWJ de su base.
    Precondición: `0 < posicion < len(texto)` (todo corte cae dentro de un bloque)."""
    anterior, siguiente = texto[posicion - 1], texto[posicion]
    if anterior == "\r" and siguiente == "\n":
        return False
    return anterior != "\u200d" and not _extiende_caracter(siguiente)


def _ultimo_corte(
    patron: re.Pattern[str], texto: str, inicio: int, minimo: int, limite: int
) -> int | None:
    """Última posición de corte con `minimo <= corte <= limite`. Se busca desde
    `inicio` para no perder coincidencias que cruzan `minimo`."""
    ultimo = None
    for encontrado in patron.finditer(texto, inicio, limite):
        corte = encontrado.end()
        if corte >= minimo and _corte_seguro(texto, corte):
            ultimo = corte
    return ultimo


def _corte_duro(texto: str, inicio: int, limite: int) -> int:
    for corte in range(limite, inicio, -1):
        if _corte_seguro(texto, corte):
            return corte
    return limite


def _buscar_corte(texto: str, inicio: int, presupuesto: int) -> int:
    """Posición de corte en (inicio, inicio + presupuesto], determinista."""
    limite = inicio + presupuesto
    minimo = inicio + max(1, presupuesto // 2)
    for patron in (markdown.FIN_DE_LINEA, _FIN_DE_ORACION, _ESPACIO):
        corte = _ultimo_corte(patron, texto, inicio, minimo, limite)
        if corte is not None:
            return corte
    return _corte_duro(texto, inicio, limite)


def _construir_contexto(titulos: list[str], tabla: _Tabla | None, maximo: int) -> str:
    partes = []
    if titulos:
        partes.append(_PREFIJO_SECCION + SEPARADOR_RUTA.join(titulos))
    if tabla is not None and tabla.encabezado:
        partes.append(_PREFIJO_TABLA + tabla.encabezado)
    espacio = maximo - len(_FIN_DE_CONTEXTO)
    if not partes or espacio < 1:
        return ""
    cuerpo = "\n".join(partes)
    if len(cuerpo) > espacio:
        cuerpo = cuerpo[: espacio - 1] + "…"
    return cuerpo + _FIN_DE_CONTEXTO


# --- Empaquetado --------------------------------------------------------------

class _Empaquetador:
    """Agrupa bloques consecutivos en fragmentos de a lo sumo `max` caracteres."""

    def __init__(self, texto: str, parametros: ParametrosFragmentacion) -> None:
        self._texto = texto
        self._max = parametros.max_caracteres
        self._max_contexto = parametros.max_caracteres_contexto
        # (nivel, título, posición de la línea del encabezado)
        self._pila: list[tuple[int, str, int]] = []
        self._indice = 0
        self._activo = False
        self._inicio = 0
        self._fin = 0
        self._con_cuerpo = False
        self._continuacion = False
        self._tabla: _Tabla | None = None
        self._corte_pendiente = False

    def procesar(self, bloque: _Bloque) -> Iterator[Fragmento]:
        if bloque.tipo == _ENCABEZADO:
            yield from self._encabezado(bloque)
        elif bloque.tipo == _TABLA:
            yield from self._tabla_completa(bloque)
        else:
            yield from self._agregar(bloque.inicio, bloque.fin)

    def terminar(self) -> Iterator[Fragmento]:
        if self._activo:
            yield self._cerrar()

    def _encabezado(self, bloque: _Bloque) -> Iterator[Fragmento]:
        # Se cierra antes de modificar la ruta: el fragmento cerrado no debe
        # tomar un encabezado que no contiene.
        usado = self._fin - self._inicio
        if self._activo and (self._con_cuerpo or bloque.fin - bloque.inicio > self._max - usado):
            yield self._cerrar()
        while self._pila and self._pila[-1][0] >= bloque.nivel:
            self._pila.pop()
        self._pila.append((bloque.nivel, bloque.titulo, bloque.inicio))
        yield from self._agregar(bloque.inicio, bloque.fin, con_cuerpo=False)

    def _tabla_completa(self, bloque: _Bloque) -> Iterator[Fragmento]:
        tabla = bloque.tabla
        if bloque.fin - bloque.inicio <= self._max:
            yield from self._agregar(bloque.inicio, bloque.fin)
            return
        if not self._columnas_consistentes(bloque):
            # Tabla deteriorada: se divide igual y el literal es exacto, pero no
            # se repite su encabezado como contexto; sugeriría una correspondencia
            # columna-celda que el documento no sostiene.
            tabla = replace(tabla, encabezado="")
        # Encabezado + delimitador son una unidad; la última fila arrastra las
        # líneas en blanco finales.
        hay_filas = tabla.inicio_cuerpo < tabla.fin_filas
        yield from self._agregar(bloque.inicio, tabla.inicio_cuerpo if hay_filas else bloque.fin)
        posicion = tabla.inicio_cuerpo
        while posicion < tabla.fin_filas:
            _, fin_linea = markdown.limites_de_linea(self._texto, posicion)
            fin_fila = bloque.fin if fin_linea >= tabla.fin_filas else fin_linea
            yield from self._agregar(posicion, fin_fila, tabla=tabla)
            posicion = fin_linea

    def _columnas_consistentes(self, bloque: _Bloque) -> bool:
        """Mismo criterio que el validador: cada fila (delimitador incluido) tiene
        tantas celdas como el encabezado. Se detiene en la primera diferencia."""
        texto = self._texto
        fin_encabezado, posicion = markdown.limites_de_linea(texto, bloque.inicio)
        esperadas = len(markdown.celdas(texto[bloque.inicio:fin_encabezado]))
        while posicion < bloque.tabla.fin_filas:
            fin_contenido, siguiente = markdown.limites_de_linea(texto, posicion)
            if len(markdown.celdas(texto[posicion:fin_contenido])) != esperadas:
                return False
            posicion = siguiente
        return True

    def _agregar(
        self,
        inicio: int,
        fin: int,
        con_cuerpo: bool = True,
        tabla: _Tabla | None = None,
    ) -> Iterator[Fragmento]:
        while inicio < fin:
            restante = self._max - (self._fin - self._inicio) if self._activo else self._max
            longitud = fin - inicio
            if longitud <= restante:
                if not self._activo:
                    self._abrir(inicio, tabla)
                self._fin = fin
                self._con_cuerpo = self._con_cuerpo or con_cuerpo
                return
            if longitud <= self._max:
                # Cabe en un fragmento vacío, no en lo que queda del actual.
                yield self._cerrar()
                continue
            # Bloque mayor que el límite: se divide. Solo se completa un
            # fragmento que tenga únicamente encabezados, para no dejarlos solos.
            if self._activo and (self._con_cuerpo or restante < self._max // 2):
                yield self._cerrar()
                continue
            corte = _buscar_corte(self._texto, inicio, restante)
            if not self._activo:
                self._abrir(inicio, tabla)
            self._fin = corte
            self._con_cuerpo = self._con_cuerpo or con_cuerpo
            yield self._cerrar()
            self._corte_pendiente = True
            inicio = corte

    def _abrir(self, inicio: int, tabla: _Tabla | None) -> None:
        self._activo = True
        self._inicio = self._fin = inicio
        self._con_cuerpo = False
        self._continuacion = self._corte_pendiente
        self._corte_pendiente = False
        # El encabezado de tabla solo hace falta si el fragmento empieza en sus filas.
        self._tabla = tabla if tabla is not None and inicio >= tabla.inicio_cuerpo else None

    def _cerrar(self) -> Fragmento:
        inicio, fin = self._inicio, self._fin
        titulos_previos = [t for _, t, posicion in self._pila if t and posicion < inicio]
        fragmento = Fragmento(
            indice=self._indice,
            inicio=inicio,
            fin=fin,
            texto_literal=self._texto[inicio:fin],
            ruta_encabezados=tuple(t for _, t, _ in self._pila if t),
            contexto=_construir_contexto(titulos_previos, self._tabla, self._max_contexto),
            continuacion=self._continuacion,
        )
        self._indice += 1
        self._activo = False
        return fragmento


def _generar(texto: str, parametros: ParametrosFragmentacion) -> Iterator[Fragmento]:
    empaquetador = _Empaquetador(texto, parametros)
    cap = parametros.max_caracteres_contexto + _MARGEN_ENCABEZADO
    for bloque in _bloques(texto, cap):
        yield from empaquetador.procesar(bloque)
    yield from empaquetador.terminar()


def iterar_fragmentos(
    texto: str, parametros: ParametrosFragmentacion | None = None
) -> Iterator[Fragmento]:
    """Produce los fragmentos de forma incremental. Los parámetros se validan
    al llamar, no al consumir el iterador."""
    if not isinstance(texto, str):
        raise TypeError(f"El texto a fragmentar debe ser str, no {type(texto).__name__}.")
    if parametros is None:
        parametros = ParametrosFragmentacion()
    elif not isinstance(parametros, ParametrosFragmentacion):
        raise TypeError("Los parámetros deben ser una instancia de ParametrosFragmentacion.")
    return _generar(texto, parametros)


def fragmentar(texto: str, parametros: ParametrosFragmentacion | None = None) -> list[Fragmento]:
    return list(iterar_fragmentos(texto, parametros))

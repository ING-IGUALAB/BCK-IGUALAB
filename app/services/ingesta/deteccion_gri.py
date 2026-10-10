"""Detección inicial de referencias GRI en Markdown (decisión 2026-10-07).

Recibe el texto interpretado (`DocumentoValidado.texto`) y devuelve las
referencias GRI AGRUPADAS POR ESTÁNDAR: «GRI 305» aparece una vez y conserva todas
sus referencias específicas (305-1, 305-2, 305-1-a…) con su cita literal, offsets
y sección. No depende de FastAPI, de la BD ni de ningún proveedor.

QUÉ ES UNA MENCIÓN
Una mención es una referencia escrita en el documento; NO es evidencia de
cumplimiento. El detector no evalúa, no asigna OK / Baja sustancia /
Sub-reportado, no calcula puntaje ESG, no busca ausencias, no compara con el texto
oficial del estándar y no clasifica el documento como OBSERVADO (esa decisión
depende del detector completo de GRI y sanciones, que aún no existe). Un resultado
sin menciones tampoco significa «OBSERVADO»: esta pieza no está integrada.

QUÉ SE RECONOCE
- Con prefijo: `GRI 305`, `GRI 305-1`, `GRI 305-1-a` (también `305-1a`,
  `305-1(a)`, `305-1.a`), `GRI 2-27` y sectoriales `GRI 14.1`, `GRI 14.1.5`.
  Las listas con prefijo (`GRI 305-1, 305-2 y 305-3`) se leen completas; una
  referencia encadenada solo se acepta si tiene forma de revelación o es un
  código de 3 dígitos del catálogo (así no se confunde «GRI 305, 2 sitios»).
  Los rangos («305-1 a 305-7») NO se expanden: solo se registran los extremos que
  el texto escribe con su prefijo.
- Sin prefijo, únicamente en un contexto GRI inequívoco:
  * una columna de tabla cuyo encabezado nombra GRI (se admite el código solo);
  * una sección cuyo título menciona GRI y un índice/contenido: allí se aceptan
    referencias con forma de revelación al inicio de línea o de celda, no números
    sueltos.
  Fuera de esos contextos un número no es una referencia GRI. Los códigos SASB u
  otros marcos y los números ordinarios no se reconocen.

ÍNDICE VERSUS CUERPO
`rol` distingue la mención en un índice GRI («indice») de la mención en el cuerpo
(«cuerpo»). Ninguna implica cumplimiento. La cita es la línea (fila de tabla) que
contiene la referencia: el detector nunca atribuye un párrafo vecino a un código
por estar cerca de una lista de códigos.

EDICIONES Y CÓDIGOS HISTÓRICOS
Se contrasta con `catalogo_gri`. Un código puede tener varias ediciones con
significado distinto (GRI 102 es «General Disclosures 2016» y «Climate Change
2025»). Si el texto no dice cuál, la referencia queda AMBIGUA y se conserva para
revisión; nunca se infiere por otras partes del documento ni se convierte un
código histórico en uno vigente. Lo desconocido (código no catalogado, edición no
catalogada, formato no reconocido, forma incongruente con el tipo de estándar) se
conserva con `motivos_revision`, sin corregirlo.

CONTRATO DE POSICIONES
`inicio`/`fin`, `cita_inicio`/`cita_fin`: posiciones de caracteres del texto
interpretado recibido (misma base que los fragmentos), inicio inclusivo, fin
exclusivo. `texto[inicio:fin] == referencia_original` y
`texto[cita_inicio:cita_fin] == cita`.

LÍMITES CONOCIDOS
- El catálogo identifica estándares, no revelaciones: «305-99» se agrupa bajo GRI
  305 sin comprobar que esa revelación exista.
- Las listas encadenadas no cruzan saltos de línea.
- Solo encabezados ATX y tablas de pipes (ver `markdown`).
- Las referencias en bloques de código se detectan pero llevan `en_codigo=True`.
- Rendimiento con 50 MB sin medir.
"""
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.services.ingesta import markdown
from app.services.ingesta.catalogo_gri import CatalogoGri, EntradaGri, catalogo_predeterminado

SEPARADOR_RUTA = " > "
MAX_LONGITUD_CITA = 400
MARGEN_CITA = 150
MAX_LONGITUD_ENCABEZADO_COLUMNA = 40

ROL_INDICE = "indice"
ROL_CUERPO = "cuerpo"

FORMA_ESTANDAR = "estandar"
FORMA_REVELACION = "revelacion"
FORMA_REVELACION_SUFIJO = "revelacion_con_sufijo"
FORMA_SECTORIAL = "sectorial"
FORMA_NO_RECONOCIDA = "no_reconocida"

IDENTIDAD_EDICION_EXPLICITA = "edicion_explicita"
IDENTIDAD_UNICA_CATALOGADA = "unica_edicion_catalogada"
IDENTIDAD_AMBIGUA = "ambigua"
IDENTIDAD_EDICION_NO_CATALOGADA = "edicion_no_catalogada"
IDENTIDAD_NO_CATALOGADA = "no_catalogada"
IDENTIDAD_SIN_RESOLVER = "sin_resolver"

MOTIVO_NO_CATALOGADO = "estandar_no_catalogado"
MOTIVO_EDICION_AMBIGUA = "edicion_ambigua"
MOTIVO_EDICION_NO_CATALOGADA = "edicion_no_catalogada"
MOTIVO_FORMATO_NO_RECONOCIDO = "formato_no_reconocido"
MOTIVO_FORMATO_INCONGRUENTE = "formato_incongruente"

_PREFIJO = re.compile(
    r"(?<![A-Za-z])GRI(?:[ \t ]+Standards?)?[ \t ]*[-:–]?[ \t ]*(?=\d)"
)
# Código tolerante: se acepta la forma y se clasifica aparte, de modo que algo
# como «305-1.5» o «305-2016» se conserve para revisión en lugar de perderse.
_TOKEN = re.compile(
    r"(?<![\w.])\d{1,3}(?!\d)"
    r"(?:[-.]\d+|[-.]?[A-Za-z](?![A-Za-z0-9])|\([A-Za-z]\))*"
)
_SEPARADOR_ENCADENADO = re.compile(
    r"[ \t]*(?:[,;/&]|\b(?:and|or|[yeo])\b)[ \t]*(?P<prefijo>GRI[ \t ]*[-:–]?[ \t ]*)?"
)
_EDICION_TRAS_NOMBRE = re.compile(
    r"[ \t]*:[ \t]*[^\d\n|:]{1,80}?[ \t]+((?:19|20)\d{2})(?!\d)"
)
_EDICION_ENTRE_PARENTESIS = re.compile(r"[ \t]*\([ \t]*((?:19|20)\d{2})[ \t]*\)")

_FORMA_ESTANDAR = re.compile(r"(\d{1,3})")
_FORMA_REVELACION = re.compile(r"(\d{1,3})-(\d{1,2})")
_FORMA_REVELACION_SUFIJO = re.compile(r"(\d{1,3})-(\d{1,2})(?:[-.]?([A-Za-z])|\(([A-Za-z])\))")
_FORMA_SECTORIAL = re.compile(r"(\d{1,3})\.(\d{1,2})(?:\.(\d{1,2}))?")

_GRI_Y_LUEGO_INDICE = re.compile(r"(?i)\bGRI\b.*\b(?:[ií]ndice|index|contenidos?|content)\b")
_INDICE_Y_LUEGO_GRI = re.compile(r"(?i)\b(?:[ií]ndice|index|contenidos?|content)\b.*\bGRI\b")
_ENCABEZADO_COLUMNA_GRI = re.compile(r"(?i)\bGRI\b")
# Una columna que nombra otro marco no es una columna de códigos GRI.
_OTRO_MARCO = re.compile(r"(?i)\b(?:SASB|TCFD|ISSB|ESRS|SDG|ODS|IFRS|CDP|UNGC|Pacto)\b")
_INICIO_DE_LISTA = re.compile(r"[ \t]*(?:>[ \t]*)*(?:[-*+•][ \t]+)?(?:\*\*|__)?[ \t]*")


@dataclass(frozen=True, slots=True)
class MencionGri:
    referencia_original: str
    referencia_normalizada: str
    inicio: int
    fin: int
    cita: str
    cita_inicio: int
    cita_fin: int
    seccion: str
    rol: str
    prefijada: bool
    forma: str
    codigo: str
    revelacion: str | None
    sufijo: str | None
    edicion_mencionada: str | None
    identidad: str
    en_codigo: bool
    motivos_revision: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GrupoEstandarGri:
    codigo: str
    # Edición resuelta, o None si es ambigua, no catalogada o no se pudo resolver.
    edicion: str | None
    identidad: str
    entrada: EntradaGri | None
    # Ediciones catalogadas para el código (para revisar ambigüedades).
    candidatos: tuple[EntradaGri, ...]
    menciones: tuple[MencionGri, ...]

    @property
    def nombre(self) -> str | None:
        return self.entrada.nombre if self.entrada else None

    @property
    def catalogado(self) -> bool:
        return bool(self.candidatos)

    @property
    def referencias_especificas(self) -> tuple[str, ...]:
        """Referencias normalizadas distintas (305-1, 305-1-a…), en orden de aparición."""
        vistas: dict[str, None] = {}
        for mencion in self.menciones:
            if mencion.revelacion is not None:
                vistas.setdefault(mencion.referencia_normalizada, None)
        return tuple(vistas)

    @property
    def menciones_en_indice(self) -> int:
        return sum(m.rol == ROL_INDICE for m in self.menciones)

    @property
    def menciones_en_cuerpo(self) -> int:
        return sum(m.rol == ROL_CUERPO for m in self.menciones)

    @property
    def motivos_revision(self) -> tuple[str, ...]:
        vistos: dict[str, None] = {}
        for mencion in self.menciones:
            for motivo in mencion.motivos_revision:
                vistos.setdefault(motivo, None)
        return tuple(vistos)

    @property
    def requiere_revision(self) -> bool:
        return bool(self.motivos_revision)


@dataclass(frozen=True, slots=True)
class ResultadoDeteccionGri:
    version_catalogo: str
    grupos: tuple[GrupoEstandarGri, ...]

    @property
    def total_menciones(self) -> int:
        return sum(len(g.menciones) for g in self.grupos)

    @property
    def para_revision(self) -> tuple[GrupoEstandarGri, ...]:
        return tuple(g for g in self.grupos if g.requiere_revision)


# --- Clasificación de un token ---------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Token:
    forma: str
    codigo: str
    revelacion: str | None
    sufijo: str | None
    normalizada: str


def _clasificar(token: str) -> _Token:
    codigo = re.match(r"\d+", token).group()
    if _FORMA_ESTANDAR.fullmatch(token):
        return _Token(FORMA_ESTANDAR, codigo, None, None, codigo)
    encontrado = _FORMA_REVELACION.fullmatch(token)
    if encontrado:
        revelacion = f"{encontrado.group(1)}-{encontrado.group(2)}"
        return _Token(FORMA_REVELACION, codigo, revelacion, None, revelacion)
    encontrado = _FORMA_REVELACION_SUFIJO.fullmatch(token)
    if encontrado:
        revelacion = f"{encontrado.group(1)}-{encontrado.group(2)}"
        sufijo = (encontrado.group(3) or encontrado.group(4)).lower()
        return _Token(FORMA_REVELACION_SUFIJO, codigo, revelacion, sufijo, f"{revelacion}-{sufijo}")
    if _FORMA_SECTORIAL.fullmatch(token):
        return _Token(FORMA_SECTORIAL, codigo, token, None, token)
    return _Token(FORMA_NO_RECONOCIDA, codigo, None, None, token)


# --- Resolución contra el catálogo ----------------------------------------------

def _resolver(
    catalogo: CatalogoGri, token: _Token, edicion: str | None
) -> tuple[EntradaGri | None, str, tuple[EntradaGri, ...], tuple[str, ...]]:
    candidatos = catalogo.por_codigo(token.codigo)
    motivos: list[str] = []
    entrada: EntradaGri | None = None
    if not candidatos:
        identidad = IDENTIDAD_NO_CATALOGADA
        motivos.append(MOTIVO_NO_CATALOGADO)
    elif edicion is not None:
        entrada = catalogo.buscar(token.codigo, edicion)
        if entrada is None:
            identidad = IDENTIDAD_EDICION_NO_CATALOGADA
            motivos.append(MOTIVO_EDICION_NO_CATALOGADA)
        else:
            identidad = IDENTIDAD_EDICION_EXPLICITA
    elif len(candidatos) == 1:
        entrada = candidatos[0]
        identidad = IDENTIDAD_UNICA_CATALOGADA
    else:
        identidad = IDENTIDAD_AMBIGUA
        motivos.append(MOTIVO_EDICION_AMBIGUA)

    if token.forma == FORMA_NO_RECONOCIDA:
        motivos.append(MOTIVO_FORMATO_NO_RECONOCIDO)
    elif candidatos:
        sectorial = {c.tipo == "sectorial" for c in candidatos}
        if (token.forma == FORMA_SECTORIAL and False in sectorial) or (
            token.forma in (FORMA_REVELACION, FORMA_REVELACION_SUFIJO) and True in sectorial
        ):
            motivos.append(MOTIVO_FORMATO_INCONGRUENTE)
    return entrada, identidad, candidatos, tuple(motivos)


# --- Recorrido del documento -----------------------------------------------------

@dataclass(slots=True)
class _Contexto:
    cerca: str | None = None
    pila: list[tuple[int, str]] = field(default_factory=list)
    # Columnas GRI de la tabla en curso (None si no hay tabla).
    columnas_gri: frozenset[int] | None = None
    n_celdas: int = 0
    delimitador_pendiente: bool = False

    @property
    def seccion(self) -> str:
        return SEPARADOR_RUTA.join(titulo for _, titulo in self.pila if titulo)

    @property
    def en_seccion_indice_gri(self) -> bool:
        return any(_titulo_de_indice_gri(titulo) for _, titulo in self.pila)


def _titulo_de_indice_gri(titulo: str) -> bool:
    return _GRI_Y_LUEGO_INDICE.search(titulo) is not None or _INDICE_Y_LUEGO_GRI.search(titulo) is not None


def _limites_de_cita(texto: str, inicio_linea: int, fin_linea: int, inicio: int, fin: int) -> tuple[int, int]:
    """Línea que contiene la referencia, sin espacios en los extremos; si es
    demasiado larga, una ventana alrededor de la referencia (siempre un fragmento
    exacto del texto)."""
    desde, hasta = inicio_linea, fin_linea
    if hasta - desde > MAX_LONGITUD_CITA:
        desde = max(desde, inicio - MARGEN_CITA)
        hasta = min(hasta, fin + MARGEN_CITA)
    while desde < inicio and texto[desde].isspace():
        desde += 1
    while hasta > fin and texto[hasta - 1].isspace():
        hasta -= 1
    return desde, hasta


class _Detector:
    def __init__(self, texto: str, catalogo: CatalogoGri) -> None:
        self.texto = texto
        self.catalogo = catalogo
        self.menciones: list[MencionGri] = []
        self._sectoriales = frozenset(e.codigo for e in catalogo.entradas if e.tipo == "sectorial")
        self._codigos = catalogo.codigos

    # Aceptación de referencias encadenadas / sin prefijo -------------------------

    def _acepta_encadenada(self, token: _Token) -> bool:
        if token.forma == FORMA_ESTANDAR:
            return len(token.codigo) == 3 and token.codigo in self._codigos
        if token.forma in (FORMA_REVELACION, FORMA_REVELACION_SUFIJO):
            return token.codigo in self._codigos
        if token.forma == FORMA_SECTORIAL:
            return token.codigo in self._sectoriales
        return False

    def _acepta_en_seccion_indice(self, token: _Token) -> bool:
        return token.forma != FORMA_ESTANDAR and self._acepta_encadenada(token)

    # Registro -------------------------------------------------------------------

    def _registrar(
        self,
        contexto: _Contexto,
        linea: tuple[int, int],
        inicio: int,
        fin: int,
        token_texto: str,
        edicion: str | None,
        prefijada: bool,
        rol: str,
        en_codigo: bool,
    ) -> None:
        token = _clasificar(token_texto)
        _, identidad, _, motivos = _resolver(self.catalogo, token, edicion)
        cita_inicio, cita_fin = _limites_de_cita(self.texto, linea[0], linea[1], inicio, fin)
        self.menciones.append(
            MencionGri(
                referencia_original=self.texto[inicio:fin],
                referencia_normalizada=token.normalizada,
                inicio=inicio,
                fin=fin,
                cita=self.texto[cita_inicio:cita_fin],
                cita_inicio=cita_inicio,
                cita_fin=cita_fin,
                seccion=contexto.seccion,
                rol=rol,
                prefijada=prefijada,
                forma=token.forma,
                codigo=token.codigo,
                revelacion=token.revelacion,
                sufijo=token.sufijo,
                edicion_mencionada=edicion,
                identidad=identidad,
                en_codigo=en_codigo,
                motivos_revision=motivos,
            )
        )

    # Con prefijo ----------------------------------------------------------------

    def _escanear_prefijadas(
        self,
        contexto: _Contexto,
        inicio_linea: int,
        fin_linea: int,
        celdas_gri: list[tuple[int, int]],
        en_codigo: bool,
    ) -> None:
        texto = self.texto
        linea = texto[inicio_linea:fin_linea]
        if "GRI" not in linea:
            return
        consumido = 0
        for prefijo in _PREFIJO.finditer(linea):
            if prefijo.start() < consumido:
                continue
            consumido = self._registrar_cadena(
                contexto, linea, inicio_linea, fin_linea, celdas_gri, en_codigo, prefijo, consumido
            )

    def _registrar_cadena(
        self,
        contexto: _Contexto,
        linea: str,
        inicio_linea: int,
        fin_linea: int,
        celdas_gri: list[tuple[int, int]],
        en_codigo: bool,
        prefijo: re.Match[str],
        consumido: int,
    ) -> int:
        """Registra la referencia que sigue a `prefijo` y las encadenadas («GRI 305-1, 305-2 y 305-3»).
        Devuelve hasta dónde llegó lo consumido de la línea (`consumido` si no hubo ninguna)."""
        posicion = prefijo.end()
        primera = True
        inicio_prefijo = prefijo.start()
        while True:
            encontrado = _TOKEN.match(linea, posicion)
            if encontrado is None:
                break
            token_texto = encontrado.group()
            token = _clasificar(token_texto)
            if not primera and not self._acepta_encadenada(token):
                break
            inicio_ref = inicio_prefijo
            fin_ref = encontrado.end()
            # La edición escrita justo después de una referencia es de esa referencia.
            edicion, fin_ref = self._edicion_tras(linea, fin_ref, token)
            absoluto = inicio_linea + inicio_ref
            en_columna = any(a <= absoluto < b for a, b in celdas_gri)
            rol = ROL_INDICE if (contexto.en_seccion_indice_gri or en_columna) else ROL_CUERPO
            self._registrar(
                contexto,
                (inicio_linea, fin_linea),
                absoluto,
                inicio_linea + fin_ref,
                token_texto,
                edicion,
                True,
                rol,
                en_codigo,
            )
            consumido = fin_ref
            posicion = fin_ref
            primera = False
            separador = _SEPARADOR_ENCADENADO.match(linea, posicion)
            if separador is None:
                break
            posicion = separador.end()
            # Si la lista repite «GRI», la referencia original lo incluye.
            inicio_prefijo = separador.start("prefijo") if separador.group("prefijo") else posicion
        return consumido

    @staticmethod
    def _edicion_tras(linea: str, posicion: int, token: _Token) -> tuple[str | None, int]:
        """Edición escrita justo después de la referencia: «GRI 306: Waste 2020»
        (solo para el estándar, sin revelación) o «GRI 306 (2020)»."""
        encontrado = _EDICION_ENTRE_PARENTESIS.match(linea, posicion)
        if encontrado is None and token.forma == FORMA_ESTANDAR:
            encontrado = _EDICION_TRAS_NOMBRE.match(linea, posicion)
        if encontrado is None:
            return None, posicion
        return encontrado.group(1), encontrado.end()

    # Sin prefijo (solo en contexto GRI) -----------------------------------------

    def _lista_sin_prefijo(
        self, inicio: int, fin: int, columna: bool
    ) -> Iterator[tuple[int, int]]:
        """Referencias consecutivas al inicio de [inicio, fin). En una columna GRI
        se admite también el código de estándar solo."""
        texto = self.texto
        posicion = _INICIO_DE_LISTA.match(texto, inicio, fin).end()
        primera = True
        while posicion < fin:
            encontrado = _TOKEN.match(texto, posicion, fin)
            if encontrado is None:
                return
            token = _clasificar(encontrado.group())
            if columna:
                # La primera referencia de la celda se conserva aunque su formato no
                # se reconozca (queda para revisión); las siguientes, no.
                aceptada = primera or token.forma != FORMA_NO_RECONOCIDA
            else:
                aceptada = self._acepta_en_seccion_indice(token)
            if not aceptada:
                return
            yield encontrado.start(), encontrado.end()
            primera = False
            separador = _SEPARADOR_ENCADENADO.match(texto, encontrado.end(), fin)
            if separador is None:
                return
            posicion = separador.end()

    def _escanear_sin_prefijo(
        self,
        contexto: _Contexto,
        inicio_linea: int,
        fin_linea: int,
        celdas: list[tuple[int, int]] | None,
        columnas: frozenset[int] = frozenset(),
    ) -> None:
        en_indice = contexto.en_seccion_indice_gri
        if celdas is None:
            if not en_indice:
                return
            zonas = [(inicio_linea, fin_linea, False)]
        else:
            zonas = [
                (a, b, indice in columnas)
                for indice, (a, b) in enumerate(celdas)
                if indice in columnas or en_indice
            ]
        for a, b, columna in zonas:
            for inicio, fin in self._lista_sin_prefijo(a, b, columna):
                self._registrar(
                    contexto,
                    (inicio_linea, fin_linea),
                    inicio,
                    fin,
                    self.texto[inicio:fin],
                    None,
                    False,
                    ROL_INDICE,
                    False,
                )

    # Recorrido ------------------------------------------------------------------

    @staticmethod
    def _es_encabezado_de_columna_gri(celda: str) -> bool:
        """Nombre corto de columna («GRI», «Estándar GRI», «Contenido GRI»). Un título
        largo o con cifras («…2025 (GRI 203-1)») es texto de la tabla, no una columna."""
        return (
            len(celda) <= MAX_LONGITUD_ENCABEZADO_COLUMNA
            and not any(c.isdigit() for c in celda)
            and _ENCABEZADO_COLUMNA_GRI.search(celda) is not None
            and _OTRO_MARCO.search(celda) is None
        )

    def _columnas_gri(self, inicio: int, fin: int) -> tuple[frozenset[int], int]:
        """(índices de las columnas que nombran GRI, número de columnas del encabezado)."""
        columnas = set()
        rangos = markdown.rangos_de_celdas(self.texto, inicio, fin)
        for indice, (a, b) in enumerate(rangos):
            celda = self.texto[a:b]
            if self._es_encabezado_de_columna_gri(celda):
                columnas.add(indice)
        return frozenset(columnas), len(rangos)

    def ejecutar(self) -> None:
        texto = self.texto
        longitud = len(texto)
        contexto = _Contexto()
        posicion = 0
        while posicion < longitud:
            fin_linea, siguiente = markdown.limites_de_linea(texto, posicion)
            self._procesar_linea(contexto, posicion, fin_linea, siguiente)
            posicion = siguiente

    def _procesar_linea(self, contexto: _Contexto, posicion: int, fin_linea: int, siguiente: int) -> None:
        texto = self.texto
        cerca, es_codigo = markdown.actualizar_cerca(texto, contexto.cerca, posicion, fin_linea)
        contexto.cerca = cerca
        if es_codigo:
            contexto.columnas_gri = None
            self._escanear_prefijadas(contexto, posicion, fin_linea, [], True)
            return

        titulo = markdown.titulo_atx(texto, posicion, fin_linea)
        if titulo is not None:
            self._procesar_titulo(contexto, posicion, fin_linea, titulo)
            return

        if contexto.delimitador_pendiente:
            contexto.delimitador_pendiente = False
            return

        es_fila = (
            contexto.columnas_gri is not None
            and markdown.puede_ser_fila(texto, posicion, fin_linea)
        )
        if contexto.columnas_gri is not None and not es_fila:
            contexto.columnas_gri = None

        if es_fila:
            self._procesar_fila(contexto, posicion, fin_linea)
        else:
            self._procesar_texto(contexto, posicion, fin_linea, siguiente)

    def _procesar_titulo(self, contexto: _Contexto, posicion: int, fin_linea: int, titulo: tuple[int, str]) -> None:
        nivel, texto_titulo = titulo
        while contexto.pila and contexto.pila[-1][0] >= nivel:
            contexto.pila.pop()
        contexto.pila.append((nivel, texto_titulo))
        contexto.columnas_gri = None
        self._escanear_prefijadas(contexto, posicion, fin_linea, [], False)

    def _procesar_fila(self, contexto: _Contexto, posicion: int, fin_linea: int) -> None:
        celdas = markdown.rangos_de_celdas(self.texto, posicion, fin_linea)
        # Una fila con distinto número de columnas que el encabezado (tabla
        # deteriorada) no permite saber a qué columna pertenece cada celda:
        # no se usa el encabezado para interpretarla.
        columnas = contexto.columnas_gri if len(celdas) == contexto.n_celdas else frozenset()
        celdas_gri = [celdas[i] for i in columnas]
        self._escanear_prefijadas(contexto, posicion, fin_linea, celdas_gri, False)
        self._escanear_sin_prefijo(contexto, posicion, fin_linea, celdas, columnas)

    def _procesar_texto(self, contexto: _Contexto, posicion: int, fin_linea: int, siguiente: int) -> None:
        if self._inicia_tabla(posicion, fin_linea, siguiente):
            contexto.columnas_gri, contexto.n_celdas = self._columnas_gri(posicion, fin_linea)
            contexto.delimitador_pendiente = True
            # El encabezado es texto de la tabla, no una fila de códigos.
            self._escanear_prefijadas(contexto, posicion, fin_linea, [], False)
        else:
            self._escanear_prefijadas(contexto, posicion, fin_linea, [], False)
            self._escanear_sin_prefijo(contexto, posicion, fin_linea, None)

    def _inicia_tabla(self, inicio: int, fin: int, siguiente: int) -> bool:
        texto = self.texto
        if siguiente >= len(texto) or not markdown.tiene_pipe(texto, inicio, fin):
            return False
        fin_siguiente, _ = markdown.limites_de_linea(texto, siguiente)
        return markdown.es_inicio_tabla(texto[inicio:fin], texto[siguiente:fin_siguiente])


# --- Agrupación ----------------------------------------------------------------------

def _agrupar(catalogo: CatalogoGri, menciones: list[MencionGri]) -> tuple[GrupoEstandarGri, ...]:
    """Una entrada por (código, edición resuelta). Las menciones sin edición
    resuelta de un mismo código (ambiguas o con edición no catalogada) comparten
    grupo; cada mención conserva su propia identidad y sus motivos."""
    grupos: dict[tuple[str, str | None], list[MencionGri]] = {}
    entradas: dict[tuple[str, str | None], EntradaGri | None] = {}
    for mencion in menciones:
        token = _clasificar(mencion.referencia_normalizada)
        entrada, _, _, _ = _resolver(catalogo, token, mencion.edicion_mencionada)
        clave = (mencion.codigo, entrada.edicion if entrada else None)
        grupos.setdefault(clave, []).append(mencion)
        entradas[clave] = entrada
    resultado = []
    for clave, lista in grupos.items():
        lista.sort(key=lambda m: m.inicio)
        identidades = {m.identidad for m in lista}
        if entradas[clave] is not None:
            # Grupo resuelto: explícita si alguna mención nombra la edición; en otro
            # caso, la única edición catalogada.
            identidades = {IDENTIDAD_EDICION_EXPLICITA if IDENTIDAD_EDICION_EXPLICITA in identidades else IDENTIDAD_UNICA_CATALOGADA}
        resultado.append(
            GrupoEstandarGri(
                codigo=clave[0],
                edicion=clave[1],
                identidad=identidades.pop() if len(identidades) == 1 else IDENTIDAD_SIN_RESOLVER,
                entrada=entradas[clave],
                candidatos=catalogo.por_codigo(clave[0]),
                menciones=tuple(lista),
            )
        )
    resultado.sort(key=lambda g: (int(g.codigo), g.edicion or ""))
    return tuple(resultado)


def detectar_referencias_gri(texto: str, catalogo: CatalogoGri | None = None) -> ResultadoDeteccionGri:
    """Detecta y agrupa por estándar las referencias GRI de `texto`."""
    if not isinstance(texto, str):
        raise TypeError(f"El texto debe ser str, no {type(texto).__name__}.")
    catalogo = catalogo or catalogo_predeterminado()
    detector = _Detector(texto, catalogo)
    detector.ejecutar()
    return ResultadoDeteccionGri(
        version_catalogo=catalogo.version_catalogo,
        grupos=_agrupar(catalogo, detector.menciones),
    )

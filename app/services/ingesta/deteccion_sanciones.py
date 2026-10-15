"""Detección determinista de sanciones económicas en Markdown (decisión 2026-10-07).

Recibe el texto interpretado (`DocumentoValidado.texto`) y devuelve las menciones
explícitas de MULTAS O SANCIONES ECONÓMICAS realmente impuestas, tal como las
expresa el documento. Es puramente léxico y determinista: sin LLM, sin OCI y sin
servicios externos. No depende de FastAPI ni de la BD y no valida la admisión.

QUÉ ES UN HALLAZGO
Un hallazgo exige (1) un término de multa o sanción económica, (2) que NO esté en
un contexto excluido y (3) evidencia textual VINCULADA AL TÉRMINO de que la sanción se
impuso o siguió su curso: un monto unido a él, un verbo de imposición/pago («impuesta»,
«pagada», «recibimos una multa») o, más débil, un verbo de trámite («impugnó», «revocó»).
La evidencia más débil se señala en `motivos_revision`.
- «Vinculada» significa dentro del ámbito del término (`_rangos_de_ambito`): como máximo
  6 palabras antes y 12 después, sin cruzar comas, punto y coma ni conjunciones (salvo
  «, que …» o un predicado coordinado como «y está pendiente de pago»), y sin que haya
  entre ambos un objeto ajeno (impuestos, proveedores, planilla…). Que la palabra
  aparezca en otra parte de la cláusula no basta.
- «impuestos» (tributo) no es un verbo de imposición: solo cuentan «impuesta(s)» (femenino,
  concuerda con multa/sanción) y haber + «impuesto». Un pago de impuestos o proveedores
  no confirma el pago de una multa. Lo mismo rige para los calificadores.
Una multa pagada, histórica, impugnada o revocada SIGUE SIENDO un hallazgo: esos
calificadores se conservan con su texto literal y no la convierten en deuda
pendiente ni en sanción nueva.

QUÉ NO ES UN HALLAZGO (se cuenta en `descartes`)
- negaciones de la EXISTENCIA: «no recibimos multas», «no se impusieron multas», «sin
  multas», «ninguna multa», «cero multas». La negación se mira en el verbo que la sigue: si
  es de ESTADO («no pagó», «no ha pagado», «no impugnó», «sin pagar la multa») lo negado es
  el pago o el recurso, no la imposición, y la multa SE CONSERVA sin el calificador negado;
- un monto cero explícito unido al término («multa de S/ 0») y una fila de tabla cuyas
  únicas cifras son ceros y cuyo término no tiene evidencia propia (un total «Monto total
  por multas | S/ | 0»);
- hipotéticas y normativas: «podrían imponerse multas», «riesgo de multas», «en caso
  de incumplimiento», «multa de hasta 100 UIT», «el reglamento establece multas»;
- sanciones disciplinarias o laborales internas: Reglamento Interno de Trabajo,
  medidas disciplinarias, despido, Canal/Código de Ética, hostigamiento…;
- un término de multa sin evidencia de imposición («Multas de SUNAFIL y deterioro
  reputacional» en una matriz de riesgos);
- «sanción» o «sancionado» sin carácter económico explícito: un adjetivo económico, un monto
  vinculado, un enunciado del propio documento sobre su importe («no se especifica el monto») o,
  en una tabla, una columna de importes.
Es una heurística léxica: no se afirma precisión validada. Revisión humana pendiente.

DATOS EXPLÍCITOS, NUNCA INFERIDOS (los ausentes quedan en None)
- `entidad`: solo si el texto la une a la sanción («impuestas por el OEFA», «multa del
  OEFA», «la SUNAT multó»). Dos entidades distintas ligadas ⇒ ninguna + motivo.
- `monto`: solo si el texto lo une al término («multa equivalente a 59.078 UIT»,
  «multa de S/ 5,000», «S/ 3,000 de multa», «nos multó con…»; ver VÍNCULO ENTRE SANCIÓN, VERBO,
  ENTIDAD E IMPORTE) o si una fila bien formada de tabla lo da en una sola celda o en una cifra
  bajo una columna de importes con moneda explícita (ver TABLAS CON ENCABEZADO). NO se vincula
  un monto cercano sin esa relación. Se conserva el texto original: no se convierte UIT a soles, no se
  interpretan separadores ambiguos (`separador_ambiguo=True` en «59.078») y no se
  entienden cantidades en palabras.
- `periodo`: solo si la fecha modifica a la sanción («multa de S/ 5,000 en 2023»,
  «Durante el 2024 recibimos una multa…»). Las demás fechas de la cita quedan en
  `fechas_en_contexto` sin atribuirles relación (p. ej. la fecha de la
  supervisión que originó la multa no es la fecha de la multa).
- Ceros en tablas: un cero NO descarta una fila por sí solo. Una fila con evidencia propia
  («Multa impuesta por OEFA | 1000 | 0») se conserva; si el monto o su unidad no se pueden
  asociar con seguridad, `monto=None` y `motivos_revision` incluye `monto_no_asociado_con_seguridad`.
  Se descarta solo si el término no tiene evidencia propia y las cifras son ceros, o si el único
  dato es un cero monetario explícito («S/ 0»).
- `estado`: no existe. Solo hay `calificadores` literales (pagada, impugnada,
  histórica, revocada_o_anulada, confirmada, pendiente_de_pago).

ANÁFORA
«Esta multa fue pagada íntegramente» (demostrativo + término) no es una sanción
nueva: aporta sus calificadores y su oración a la sanción anterior de la misma
unidad (párrafo). Sin antecedente inmediato se descarta.

CONTRATO DE POSICIONES
Todas las posiciones son caracteres del texto interpretado recibido (sin BOM), inicio
inclusivo y fin exclusivo, la misma base que fragmentos y detector GRI.
`texto[inicio:fin] == referencia_original` y `texto[cita_inicio:cita_fin] == cita`.
La cita es el párrafo, oración(es) relacionadas o fila de tabla; si supera
`MAX_LONGITUD_CITA` es una ventana exacta alrededor del término.

MARKDOWN: VISTA AUXILIAR CON VÍNCULO EXACTO
El análisis se hace sobre una vista de lectura sin marcas en línea (`**negrita**`, `*cursiva*`, `[texto](url)`, `<br>`,
`>`), construida por `vista_markdown` solo borrando o sustituyendo caracteres uno a uno. Todas las posiciones y los textos que
devuelve este módulo se vuelven a calcular sobre el texto ORIGINAL (`texto[inicio:fin] == referencia_original`), que no se
modifica. Las marcas dentro de un tramo («sanción **económica**») quedan dentro de su cita literal; las de los extremos no
forman parte del dato, pero sí de la cita cuando están pegadas a ella.

VÍNCULO ENTRE SANCIÓN, VERBO, ENTIDAD E IMPORTE
- Entre el término y el importe se admiten calificativos («administrativa», «ambiental»), la entidad («del OEFA», «impuesta
  por el OEFA»), una fecha y, si el importe lo introduce un sustantivo explícito («por un monto de», «equivalente a»), cualquier
  expresión intermedia sin comas, sin conjunciones y sin otro término de sanción, un objeto ajeno (ingresos, impuestos…) o un
  saldo. Un «de/por/con» pelado solo vale si lo intermedio son calificativos, entidad o fecha.
- Un «saldo pendiente» o un pago parcial NO es el monto de la sanción: no se vincula (`solo_se_menciona_un_saldo_o_pago_parcial`).

TABLAS CON ENCABEZADO
Solo en filas con el mismo número de columnas que el encabezado (las deterioradas se leen celda por celda, sin encabezado):
- Una cifra pura bajo una columna de importes cuyo encabezado nombra EXPLÍCITAMENTE una única moneda («Monto (S/)») es un
  monto: `texto` es la cifra literal, `unidad` es la moneda tal como está escrita en el encabezado y `unidad_inicio/fin`
  ubican ese encabezado (`vinculo="encabezado_de_columna"`). Dos monedas en el encabezado o un encabezado de otra magnitud
  (ingresos, inversión…) no se usan. Una columna «Moneda» con una única moneda también sirve (`columna_de_moneda`).
- Las columnas de saldo, pendiente o pagado no aportan el monto total; la columna de entidad aporta la entidad.
- Una celda de importe que dice expresamente «No especificado», «Por determinar», «Pendiente de determinación» deja la
  sanción SIN monto y con `monto_no_especificado_en_el_documento`: no cuantificada, nunca cero.
- Si el encabezado o la fila hablan de riesgos, probabilidad, escenarios o estimaciones, nada de lo anterior se aplica.

IMPORTE DESCONOCIDO
«No se especifica el monto» NO es «no hubo sanción». Si el documento dice expresamente que el importe no consta o está
pendiente de determinación (en la misma oración o en la siguiente del mismo párrafo, sin otro término de sanción), la
sanción impuesta se conserva con `monto=None` y `monto_no_especificado_en_el_documento`; esa oración forma parte de la cita.
Para «sanción» sin adjetivo económico, ese enunciado (o una columna de importes) es lo que expresa su carácter económico
(`caracter_economico_en_contexto`). La negación de la existencia («no se impuso ninguna sanción») sigue excluyendo.

ALCANCE DEL CONTEO
`total` cuenta MENCIONES detectadas, no sucesos independientes: el mismo suceso suele aparecer en un párrafo y en una
tabla y se informa dos veces. No se fusionan menciones aunque coincidan entidad y monto (dos sanciones distintas pueden
coincidir) ni se afirma que cada mención sea una sanción distinta.

LÍMITES CONOCIDOS
- Solo español. Términos: multa(s), sanción(es) económica/pecuniaria/monetaria y
  «sanción/sancionado» con monto vinculado. Penalidades contractuales no se cuentan.
- Montos escritos en palabras («dos millones de soles») no se reconocen.
- Filas de tabla con solo un conteo («Número de multas | 2») no se detectan.
- Las filas con distinto número de columnas que el encabezado no se interpretan por
  columnas: cada celda se analiza sola.
- Un término que solo aparece en el encabezado («Multa (S/)») con filas de entidad e importe sin
  término propio no se detecta. Tampoco un «monto no especificado» sin verbo de imposición («Sanción
  económica del OEFA: monto no especificado»): sin evidencia de imposición no se asume que exista.
- Un importe en otra oración («El monto fue de S/ 9,000») no se une a la sanción de la oración anterior.
- Con dos sanciones y dos entidades en la misma cláusula no se adivina cuál es de cuál (`entidad_ambigua`).
- El monto con formato dentro de la cifra («3**,**000») o con saltos de línea conserva su texto literal,
  con las marcas internas (`numero_texto` es la cifra sin ellas).
- Las referencias dentro de bloques de código se ignoran.
- Rendimiento con 50 MB sin medir.
"""
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace

from app.services.ingesta import markdown
from app.services.ingesta.vista_markdown import VistaPlana, construir_vista

# Versión de las reglas de detección. Se persiste en el análisis (`version_detector_sanciones`): un análisis guardado sin
# ella (o con una anterior) lo produjo un detector previo. Sube con cada cambio que pueda alterar el resultado.
VERSION_DETECTOR_SANCIONES = "2026-10-15.1"

SEPARADOR_RUTA = " > "
MAX_LONGITUD_CITA = 900
MARGEN_CITA = 450

TERMINO_MULTA = "multa"
TERMINO_SANCION_ECONOMICA = "sancion_economica"
TERMINO_SANCION_CON_MONTO = "sancion_con_monto"

BASE_MONTO_VINCULADO = "monto_vinculado"
BASE_VERBO_IMPOSICION = "verbo_de_imposicion_o_pago"
BASE_VERBO_TRAMITE = "verbo_de_tramite"
BASE_FILA_DE_TABLA = "fila_de_tabla"
# Carácter económico de una «sanción» genérica expresado por el contexto (importe desconocido o columna de importes).
BASE_CARACTER_ECONOMICO = "caracter_economico_en_contexto"

VINCULO_ENCABEZADO = "encabezado_de_columna"
VINCULO_COLUMNA_MONEDA = "columna_de_moneda"

CAL_PAGADA = "pagada"
CAL_PENDIENTE = "pendiente_de_pago"
CAL_IMPUGNADA = "impugnada"
CAL_HISTORICA = "historica"
CAL_REVOCADA = "revocada_o_anulada"
CAL_CONFIRMADA = "confirmada"

MOTIVO_SOLO_TRAMITE = "evidencia_solo_por_verbo_de_tramite"
MOTIVO_ENTIDAD_AMBIGUA = "entidad_ambigua"
MOTIVO_SEPARADOR_AMBIGUO = "monto_con_separador_ambiguo"
MOTIVO_MONTOS_MULTIPLES = "montos_multiples_en_fila"
MOTIVO_MONTO_NO_ASOCIADO = "monto_no_asociado_con_seguridad"
MOTIVO_MONTO_NO_ESPECIFICADO = "monto_no_especificado_en_el_documento"
MOTIVO_SOLO_SALDO = "solo_se_menciona_un_saldo_o_pago_parcial"

DESCARTE_NEGACION = "negacion"
DESCARTE_HIPOTETICA = "hipotetica_o_normativa"
DESCARTE_DISCIPLINARIA = "disciplinaria_o_laboral_interna"
DESCARTE_MONTO_CERO = "monto_cero"
DESCARTE_SIN_EVIDENCIA = "sin_evidencia_de_imposicion"
DESCARTE_ANAFORA = "anaforica_sin_antecedente"

_I = re.IGNORECASE


class _UnionDePatrones:
    """Varios patrones de palabras completas con raíces distintas (sus coincidencias nunca se solapan) usados como
    uno solo: `finditer` entrega las coincidencias de todos por orden de posición."""

    def __init__(self, *patrones: re.Pattern[str]) -> None:
        self._patrones = patrones

    def finditer(self, texto: str, pos: int = 0, endpos: int | None = None):
        fin = len(texto) if endpos is None else endpos
        coincidencias = [m for patron in self._patrones for m in patron.finditer(texto, pos, fin)]
        return iter(sorted(coincidencias, key=lambda m: m.start()))

# --- Términos --------------------------------------------------------------------------
_MULTA = re.compile(r"\b(?:multas?|multad[oa]s?|mult[óo]|multaron|multamos)\b", _I)
_SANCION_ECONOMICA = re.compile(
    r"\bsanci(?:[óo]n|ones)(?:\s+(?:administrativ|ambiental|tributari|fiscal|regulatori|sectorial|firm|definitiv)\w*)?"
    r"\s+(?:(?:de|con)\s+(?:car[áa]cter|naturaleza|tipo|[íi]ndole|orden)\s+)?"
    r"(?:econ[óo]micas?|pecuniari[ao]s?|monetari[ao]s?|dinerari[ao]s?|patrimoniales?)\b",
    _I,
)
_SANCION_GENERICA = re.compile(
    r"\bsanci(?:[óo]n|ones)\b|\bsancionad[oa]s?\b|\bsancion(?:[óo]|aron|amos)\b", _I
)
_ANAFORA = re.compile(
    r"(?:\b(?:esta|esa|dicha|tal|aquella|las\s+mismas?|la\s+misma|la\s+citada|la\s+mencionada|"
    r"la\s+referida|la\s+aludida|las\s+citadas|las\s+mencionadas|las\s+referidas)\s+"
    r"(?:\w+\s+)?)$",
    _I,
)

# --- Contextos excluidos ------------------------------------------------------------------
_NEGACION = re.compile(
    r"\b(?:no|nunca|jam[aá]s|tampoco|ning[uú]n[ao]?s?|sin|ni|cero|libre\s+de|ausencia\s+de|"
    r"inexistencia\s+de|carece\s+de|carecemos\s+de)\b",
    _I,
)
_NEGACION_POSTERIOR = re.compile(
    r"^\s*(?:[:=(]\s*)?(?:0(?:[.,]0+)?|cero|ninguna?|ning[uú]n)\b", _I
)
_HIPOTETICA_CLAUSULA = re.compile(
    r"\b(?:en\s+caso\s+de|de\s+(?:ser|incumplir|no\s+cumplir|infringir)|riesgos?|eventual(?:es)?|"
    r"potencial(?:es)?|posibles?|hasta|m[aá]xim[ao]s?|m[ií]nim[ao]s?|sujet[oa]s?\s+a|"
    r"expuest[oa]s?|exposici[óo]n|prev[eé]n?|establecen?|contemplan?|tipifican?|"
    r"r[eé]gimen\s+sancionador|se\s+aplicar[aá]n?|ser[aá]n?\s+(?:multad|sancionad)\w+|"
    r"para\s+(?:evitar|prevenir)|con\s+el\s+fin\s+de\s+evitar|a\s+fin\s+de\s+evitar|"
    r"evitar|prevenci[óo]n\s+de|reglamento\s+de\s+sanciones|pueden?\s+(?:ser\s+)?(?:multad|sancionad|imponer)\w*)\b",
    _I,
)
_HIPOTETICA_SI = re.compile(r"(?:^|\s)si\s+(?:(?:se|no|la|el)\s+)?(?:(?:in)?cumpl|infring|comet|produc|detect)", _I)
_MODAL_ANTES = re.compile(r"\b(?:podr\w*|pued\w+|pudier\w+)\b[^,.;]{0,40}$", _I)
_MODAL_IMPOSICION = re.compile(
    r"\b(?:podr\w*|pued\w+|pudier\w+)\s+(?:\w+\s+){0,3}(?:imponer\w*|impuest\w+|aplic\w+|recib\w+|multar\w*|"
    r"sancionar\w*|ser\s+(?:multad|sancionad)\w+|afrontar|enfrentar|incurrir\w*|generar\w*)\b",
    _I,
)
_DISCIPLINARIA = re.compile(
    r"\b(?:reglamento\s+interno|disciplinari\w+|medidas?\s+correctiv\w+|amonestaci\w+|"
    r"suspensi[óo]n\s+(?:sin\s+goce|temporal|laboral)|despido|c[óo]digo\s+de\s+[eé]tica|"
    r"canal\s+de\s+[eé]tica|c[óo]digo\s+de\s+conducta|hostigamiento|faltas?\s+(?:graves?|laborales?)|"
    r"sanciones?\s+laborales?)\b",
    _I,
)
_DELIMITADOR_CLAUSULA = re.compile(
    r";|\b(?:pero|sin\s+embargo|no\s+obstante|aunque|mientras\s+que|si\s+bien|salvo)\b", _I
)

# Negar el HECHO de la sanción («no recibimos multas») la excluye; negar su ESTADO («no pagó la
# multa», «no impugnó») no: la multa existe y solo se niega el pago o el recurso. La negación se
# evalúa mirando el verbo que la sigue.
_VERBO_DE_ESTADO = re.compile(r"(?:pag|cancel|abon|honr|impugn|apel|reclam|recurr|liquid|regulariz|subsan)\w*", _I)
_PRIMER_VERBO = re.compile(
    r"\s*(?:(?:se|nos|le|les|lo|la|los|las|me|te|ha|han|hemos|he|había|habían|haya|hayan|sido|fue|fueron|"
    r"aún|todavía|aun|siquiera|realmente|efectivamente|oportunamente)\s+)*(\w+)",
    _I,
)
_CORTE_DE_ALCANCE_NEGACION = re.compile(r"\b(?:y|e|pero|sino|aunque)\b", _I)

# --- Evidencia de imposición VINCULADA al término -------------------------------------------------
# La evidencia debe estar en el ámbito del término de sanción (ver `_rangos_de_ambito`): unas pocas
# palabras antes o después, sin cruzar comas ni conjunciones. Que la palabra aparezca en otra parte
# de la cláusula no basta. «impuestos» (tributo) NO es «impuesta»: solo se reconocen participios
# femeninos (concuerdan con «multa»/«sanción») o el auxiliar haber + participio.
_FINITOS_IMPOSICION = (
    r"impuso|impusieron|impusimos|aplicó|aplicaron|aplicamos|determinó|determinaron|recibió|recibimos|recibieron|"
    r"pagó|pagaron|pagamos|canceló|cancelaron|abonó|abonaron|notificó|notificaron|registró|registraron|registramos|"
    r"reportó|reportaron|reportamos|tuvo|tuvimos|tuvieron|hubo|incurrió|incurrimos|incurrieron|sancionó|sancionaron|"
    r"multó|multaron|emitió|emitieron|emitimos|dictó|dictaron|ordenó|ordenaron|dispuso|dispusieron|interpuso|interpusieron|"
    r"decretó|decretaron"
)
_PARTICIPIOS_FEMENINOS = (
    r"(?:impuestas?|aplicadas?|determinadas?|notificadas?|pagadas?|canceladas?|abonadas?|registradas?|reportadas?|"
    r"recibidas?|sancionadas?|emitidas?|dictadas?|interpuestas?|ordenadas?|dispuestas?|decretadas?)"
)
_AUXILIAR_Y_PARTICIPIO = (
    r"(?:(?:ha|han|hemos|he|había|habían)\s+(?:impuesto|aplicado|determinado|recibido|pagado|cancelado|abonado|"
    r"notificado|registrado|reportado|emitido|dictado|interpuesto|ordenado|dispuesto|decretado)"
    r"|(?:fue|fuimos|fueron|ha\s+sido|hemos\s+sido|han\s+sido)\s+objeto\s+de)"
)
_EVIDENCIA_ANTES = re.compile(
    rf"\b(?:{_FINITOS_IMPOSICION}|{_AUXILIAR_Y_PARTICIPIO}|{_PARTICIPIOS_FEMENINOS})\b", _I
)
_EVIDENCIA_DESPUES = re.compile(
    rf"\b(?:{_PARTICIPIOS_FEMENINOS}|(?:que|se)\s+(?:se\s+)?(?:nos\s+|le\s+|les\s+)?(?:{_FINITOS_IMPOSICION}))\b", _I
)
_TRAMITE_VINCULADO = _UnionDePatrones(
    re.compile(r"\b(?:impugn|revoc|anul)\w+\b", _I),
    re.compile(r"\bapel(?:ó|ación|adas?|aron|amos)\b", _I),
    re.compile(r"\bconfirm(?:ó|aron|adas?|ación)\b", _I),
)
# Si entre el verbo y el término hay uno de estos objetos, el verbo se refiere a ellos, no a la multa
# («pagó los impuestos asociados a la multa»).
_OBJETO_AJENO = re.compile(
    r"\b(?:impuestos?|tributos?|proveedor\w*|planillas?|salarios?|sueldos?|dividendos?|facturas?|intereses|"
    r"contratistas?|servicios|deudas?|bonos?)\b",
    _I,
)
# Una coma seguida de un relativo («, que…») o de un cue de importe («, por un monto de…», «, equivalente a…») continúa
# la misma frase nominal: no corta el alcance.
_RELATIVO = (
    r"(?!\s*(?:que|la\s+cual|las\s+cuales|el\s+cual|cuy[oa]s?|(?:por|con|de)\s+(?:un\s+|el\s+|su\s+)?(?:monto|importe|cuant[íi]a)|"
    r"(?:por|con|de)\s+(?:(?:un|el|la)\s+)?(?:(?:S/|US\$|U\$S|USD|\$|PEN|EUR|€)\s*)?\d+|"
    r"equivalente\s+a|ascendente\s+a|asciende\s+a|ascendi[óo]\s+a|valorizad[ao]\s+en|por\s+un\s+total\s+de)\b)"
)
_SEPARADOR_DE_ALCANCE = re.compile(r"(?<!\d),|,(?!\d)|;|\b(?:y|e|pero|sino|aunque|ni)\b", _I)
# Una conjunción no corta el alcance si continúa el mismo predicado: «la multa ... y está pendiente de pago».
_FIN_DE_ALCANCE = re.compile(
    rf"(?<!\d),{_RELATIVO}|,(?!\d){_RELATIVO}|;|\s(?:y|e|pero|sino|aunque|ni)\s(?!(?:está|esta|queda|sigue|permanece|continúa|continua|"
    r"se\s+encuentra|será|fue|fueron|ha\s+sido|no|luego|posteriormente|después|también|además|ya|aún|todavía)\b)",
    _I,
)
PALABRAS_ANTES_DEL_TERMINO = 6
PALABRAS_DESPUES_DEL_TERMINO = 12
# El importe suele ir tras calificativos, entidad y un conector: se mira un poco más lejos que para los verbos.
PALABRAS_HASTA_EL_MONTO = 16

# --- Calificadores (literales) -------------------------------------------------------------------
_CALIFICADORES: tuple[tuple[str, "re.Pattern[str] | _UnionDePatrones"], ...] = (
    (CAL_PAGADA, re.compile(
        r"\b(?:(?:pag|cancel|abon|honr)ad[oa]s?|pag(?:ó|aron|amos)|(?:cancel|abon)(?:ó|aron))\b", _I)),
    (CAL_PENDIENTE, re.compile(
        r"\b(?:impag[ao]s?|sin\s+pagar|por\s+pagar|pendientes?\s+de\s+pago|pago\s+pendiente|adeudad[ao]s?|"
        r"obligaciones?\s+de\s+pago\s+pendientes?)\b", _I)),
    (CAL_IMPUGNADA, re.compile(
        r"\b(?:impugn\w+|apel(?:[óo]|aci[óo]n|ad[oa]s?|aron|amos)|recurso\s+de\s+(?:apelaci[óo]n|reconsideraci[óo]n|revisi[óo]n)|"
        r"contencioso[- ]administrativ\w+|demanda\s+de\s+nulidad)\b", _I)),
    (CAL_HISTORICA, re.compile(
        r"\b(?:hist[óo]ric[ao]s?|(?:a[ñn]os|ejercicios|per[ií]odos|periodos)\s+anteriores|"
        r"de\s+(?:un\s+)?(?:a[ñn]o|ejercicio)\s+anterior|antigu[ao]s?)\b", _I)),
    (CAL_REVOCADA, _UnionDePatrones(
        re.compile(r"\b(?:revoc|anul)(?:[óo]|aron|ad[ao]s?|aci[óo]n)\b", _I),
        re.compile(r"\bdej(?:[óo]|ada|aron)\s+sin\s+efecto\b", _I))),
    (CAL_CONFIRMADA, re.compile(r"\bconfirm(?:[óo]|aron|ad[oa]s?|aci[óo]n)\b", _I)),
)
# Un calificador precedido de estas negaciones o hipótesis no se registra.
_PALABRAS_NEGADORAS = (
    re.compile(r"\b(?:no|nunca|jam[aá]s|sin|ni|pudiera)\b", _I),
    re.compile(r"\bning[uú]n[ao]?s?\b", _I),
    re.compile(r"\bpodr(?:[ií]a(?:n|mos)?|[aá]n?)\b", _I),
)
# Lo que sigue a la palabra hasta el final del texto: sin coma, punto ni «;», sin una conjunción que corte, y
# a lo sumo 40 caracteres.
_COLA_DE_NEGACION = re.compile(r"(?:(?!\b(?:y|e|pero|aunque)\b)[^,.;]){0,40}$", _I)


def _niega_el_calificador(previo: str) -> bool:
    return any(
        _COLA_DE_NEGACION.match(previo, m.end()) for patron in _PALABRAS_NEGADORAS for m in patron.finditer(previo)
    )

# --- Montos -----------------------------------------------------------------------------------------
_PREFIJO_MONEDA = r"(?:S/\.?|US\$|U\$S|USD|\$|PEN|EUR|€)"
_NUMERO = r"(?:\d{1,3}(?:[.,'’  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
_ESCALA = r"(?:\s*(?:mil\b|millones?\b|MM\b))?"
_UNIDAD_SUFIJO = r"(?:UITs?|soles|nuevos\s+soles|d[oó]lares(?:\s+americanos)?|USD|PEN|euros?)"
_MONTO_PREFIJO = re.compile(rf"(?P<unidad>{_PREFIJO_MONEDA})\s*(?P<numero>{_NUMERO}){_ESCALA}", _I)
_MONTO_SUFIJO = re.compile(
    rf"(?P<numero>{_NUMERO})(?:\s*(?:mil|millones?(?:\s+de)?))?\s*(?P<unidad>{_UNIDAD_SUFIJO})\b", _I
)
# Calificativos y participios que pueden mediar entre el término y su importe («sanción administrativa», «multa impuesta»).
_CALIFICATIVOS = (
    r"(?:econ[óo]micas?|administrativas?|pecuniarias?|monetarias?|patrimoniales?|ambientales?|laborales?|"
    r"tributari[ao]s?|fiscales?|regulatori[ao]s?|sectoriales?|firmes?|definitivas?|total(?:es)?|adicional(?:es)?|"
    r"hist[óo]ric[ao]s?|anterior(?:es)?|[úu]nic[ao]s?|leves?|graves?|muy\s+graves?|formales?|menor(?:es)?|mayor(?:es)?|"
    r"nuev[ao]s?|similar(?:es)?|impuest[ao]s?|aplicad[ao]s?|emitid[ao]s?|dictad[ao]s?|notificad[ao]s?|determinad[ao]s?|"
    r"recibid[ao]s?|pagad[ao]s?|impugnad[ao]s?|confirmad[ao]s?|cancelad[ao]s?|abonad[ao]s?)"
)
# Conector «débil» entre el término (con su relleno) y el importe; el importe sigue inmediatamente.
_CONECTOR_DEBIL = (
    r"(?:,?(?:\s+(?:ascendente|equivalente|valorizad[ao]|que\s+asciende|que\s+ascendi[óo])\s+a\s+"
    r"|\s+correspondiente\s+a\s+"
    r"|\s+(?:ascendi[óo]|asciende|fue|fueron|sum[óo])\s+(?:a|de|por)\s+"
    r"|\s+(?:de|por|con)(?:\s+(?:un|el|la))?(?:\s+(?:monto|importe|total|valor|suma))?(?:\s+(?:total|de))?\s+"
    r"|\s+total(?:izando)?(?:\s+de)?\s+"
    r"|\s*[:(]\s*))"
)
# Cue «fuerte»: un sustantivo o verbo que dice expresamente que lo que sigue es el importe. Con él se admite cualquier
# expresión intermedia (sin comas, conjunciones, otro término ni objeto ajeno).
_CUE_FUERTE_FINAL = re.compile(
    r"(?:\s+(?:(?:por|con|de|en)\s+)?(?:(?:un|el|la|su)\s+)?(?:monto|importe|cuant[íi]a|suma)\b"
    r"(?:\s+(?:econ[óo]mico|total|aproximado|referencial))*"
    r"(?:\s+(?:(?:es|fue|era|ser[íi]a|ser[áa]|asciende|ascendi[óo]|ascend[íi]a)\s+)?(?:de|por|a|igual\s+a))?"
    r"(?:\s+(?:un|el)\s+total\s+de)?"
    r"|\s+(?:equivalente|ascendente|valorizad[ao]|correspondiente)\s+(?:a|en)"
    r"|\s+(?:que\s+)?(?:asciende|ascendi[óo]|totaliza|totaliz[óo])(?:\s+(?:a|en|de|por))?"
    r"|\s+por\s+(?:un\s+)?(?:total|valor)\s+de)\s*:?\s+\Z",
    _I,
)
# Entre el término y el importe no puede haber otra cosa a la que el importe pertenezca más naturalmente.
_AJENOS_AL_IMPORTE = (
    r"impuestos?|tributos?|proveedor\w*|planillas?|salarios?|sueldos?|dividendos?|facturas?|intereses|contratistas?|"
    r"servicios|deudas?|bonos?|ingresos?|ventas?|facturaci[óo]n|utilidad(?:es)?|ganancias?|inversi[óo]n(?:es)?|"
    r"presupuest\w+|costos?|gastos?|ahorros?|capital|beneficios?"
)
_OBJETO_AJENO_AL_IMPORTE = re.compile(rf"\b(?:{_AJENOS_AL_IMPORTE})\b", _I)
_BARRERA_DE_PUENTE = re.compile(
    rf"\b(?:{_AJENOS_AL_IMPORTE}|saldos?|restantes?|adeudad[ao]s?|multas?|sanci(?:[óo]n|ones)|sancionad[ao]s?)\b", _I
)
# Celda sin dato («-», «N/A», «No aplica») y celda con varias entidades («OEFA / SUNAFIL», «OEFA y SUNAFIL»).
_SIN_DATO = re.compile(r"[\s\-–—.*]*|n\s*/\s*a|no\s+aplica|no\s+corresponde", _I)
_VARIAS_ENTIDADES = re.compile(r"[/;,]|\s(?:y|e|o)\s", _I)
_ENCABEZADO_IMPORTE = re.compile(r"monto|importe|valor|total|multa|sanci|cuant[íi]a|aplicad|determinad", _I)
_SALDO_ANTES_DEL_MONTO = re.compile(
    r"\b(?:saldos?|restantes?|resta|adeudad[ao]s?|por\s+pagar|pendientes?\s+de\s+pago|pago\s+pendiente|deuda)\b"
    r"(?:\s+\w+){0,4}\s*\Z",
    _I,
)
_PALABRA_INTERMEDIA = re.compile(r"\s+[^\s,.;:()|]+")
_CON_O_POR = re.compile(r"\s+(?:con|por)(?:\s+(?:un|el|la)(?:\s+(?:monto|importe)\s+de)?)?\s+", _I)


def _fin_de_verbo_con_monto(texto: str, desde: int, limite: int) -> int | None:
    """Fin de «[hasta 8 palabras] con|por [un|el|la [monto|importe de]]» a partir de `desde`, con el menor número
    de palabras intermedias que permita el resto; None si no hay."""
    posicion = desde
    for _ in range(9):
        encontrado = _CON_O_POR.match(texto, posicion, limite)
        if encontrado is not None:
            return encontrado.end()
        palabra = _PALABRA_INTERMEDIA.match(texto, posicion, limite)
        if palabra is None:
            return None
        posicion = palabra.end()
    return None
_MONTO_ANTES = re.compile(
    rf"(?:(?:{_PREFIJO_MONEDA})\s*{_NUMERO}{_ESCALA}|{_NUMERO}(?:\s*(?:mil|millones?(?:\s+de)?))?\s*{_UNIDAD_SUFIJO}\b)"
    r"\s+(?:de|en|por|como)\s+(?:concepto\s+de\s+)?(?:(?:una|la|esa|esta)\s+)?"
    r"(?P<termino>multas?|sanci(?:[óo]n|ones)(?:\s+econ[óo]micas?)?)\b",
    _I,
)
_SOLO_MONTO = (_MONTO_PREFIJO, _MONTO_SUFIJO)
_CERO_SUELTO = re.compile(r"[\W_]*(?:0+(?:[.,]0+)?|cero)[\W_]*", _I)
_NUMERO_SUELTO = re.compile(r"[\W_]*+\d[\d.,'’   ]*+[\W_]*+")
_ANIO_SUELTO = re.compile(r"(?:19|20)\d{2}")
_ENCABEZADO_DE_MONTO = re.compile(r"monto|importe|valor|total|multa|sanci|s/|soles|uit|usd|us\$", _I)

# --- Importe desconocido y roles de columna -------------------------------------------------------------
# «No se especifica el monto», «el importe está pendiente de determinación»: el documento dice que el importe no consta.
# Eso NO niega la sanción. El monto debe ser de la sanción: «monto de las ventas» no vale.
_NOMBRE_DE_MONTO = r"(?:monto|importe|cuant[íi]a)"
_NOMBRE_DE_MONTO_SUELTO = re.compile(rf"\b{_NOMBRE_DE_MONTO}\b", _I)
_MONTO_ECONOMICO = r"(?:\s+(?:econ[óo]mic|pecuniari|monetari)\w+)?"
_MONTO_DESCONOCIDO = re.compile(
    r"\bno\s+(?:se\s+)?(?:ha\s+|han\s+)?(?:especific|determin|indic|inform|precis|detall|cuantific|fij|defin|consign|"
    r"revel|public|document|report|comunic|declar)\w*\s+(?:a[úu]n\s+|todav[íi]a\s+)?"
    rf"(?:(?:un|el|su|dicho|ning[úu]n|alg[úu]n)\s+)?{_NOMBRE_DE_MONTO}{_MONTO_ECONOMICO}\b"
    r"(?!\s+(?:de|del)\s+(?!(?:(?:la|las|el|los)\s+)?(?:multas?\b|sanci)))"
    rf"|\b{_NOMBRE_DE_MONTO}{_MONTO_ECONOMICO}(?:\s+\w+){{0,8}}?\s+"
    r"(?:(?:se\s+)?(?:encuentra|queda|est[áa]|permanece|sigue|fue|es|ha\s+sido)\s+)?"
    r"(?:(?:a[úu]n\s+)?pendiente\s+de\s+(?:determinaci[óo]n|definici[óo]n|cuantificaci[óo]n|fijaci[óo]n|precisi[óo]n)"
    r"|por\s+(?:determinar|definir|cuantificar|fijar|precisar)|a\s+(?:determinar|definir|fijar)"
    r"|no\s+(?:(?:ha\s+sido|fue|est[áa]|se(?:\s+ha)?)\s+)?(?:especific|determin|definid|inform|indic|precis|cuantific|fij|"
    r"disponible)\w*|sin\s+(?:determinar|especificar|definir|cuantificar|fijar|precisar))"
    rf"|\bsin\s+(?:un\s+)?{_NOMBRE_DE_MONTO}\s+(?:determinad|especificad|definid|fijad|cuantificad|precisad)\w*",
    _I,
)
# Celda de una columna de importes que dice expresamente que no hay cifra.
_CELDA_MONTO_DESCONOCIDO = re.compile(
    r"(?:no\s+(?:especificad|determinad|definid|informad|indicad|precisad|cuantificad|publicad|reportad|revelad)\w*"
    r"|no\s+disponible|n\s*/\s*d|n\.\s*d\.?|por\s+(?:determinar|definir|cuantificar|precisar|fijar)"
    r"|a\s+(?:determinar|definir|fijar)|sin\s+(?:determinar|especificar|definir|cuantificar|precisar)"
    r"|pendiente(?:\s+de\s+(?:determinaci[óo]n|definici[óo]n|cuantificaci[óo]n|fijaci[óo]n|precisi[óo]n))?"
    r"|en\s+(?:determinaci[óo]n|definici[óo]n|evaluaci[óo]n))[\s.*]*",
    _I,
)
_MONEDA_CELDA = re.compile(
    r"(?:S/\.?|US\$|U\$S|\$|USD|PEN|EUR|€|soles|nuevos\s+soles|d[oó]lares(?:\s+americanos)?|euros?|UITs?)", _I
)
# Moneda nombrada en un encabezado («Monto (S/)», «Importe en miles de soles»). Se conserva tal como está escrita.
_MONEDA_ENCABEZADO = re.compile(
    r"(?<![\w$/])(?:(?:en\s+)?(?:miles|millones)\s+de\s+)?"
    r"(?:S/\.?|US\$|U\$S|\$|USD|PEN|EUR|€|UITs?|nuevos\s+soles|soles|d[oó]lares(?:\s+americanos)?|euros?)(?![\w])",
    _I,
)
_ENCABEZADO_ENTIDAD = re.compile(
    r"entidad|autoridad|organismo|\bente\b|regulador|sancionador|[óo]rgano|instituci[óo]n|fiscalizador|emisor", _I
)
_ENCABEZADO_ESTADO = re.compile(r"estado|situaci[óo]n|condici[óo]n|observaci|comentario|etapa", _I)
_ENCABEZADO_COLUMNA_MONEDA = re.compile(r"\s*(?:moneda|divisa|currency)\s*", _I)
# Columnas de importes que no son el monto total de la sanción.
_ENCABEZADO_SALDO = re.compile(
    r"saldo|pendiente|restante|adeudad|deuda|por\s+pagar|impag|pagad|abonad|cancelad|descuent|rebaj|reducci[óo]n|provisi[óo]n",
    _I,
)
# Columnas de importes de otra magnitud.
_ENCABEZADO_AJENO = re.compile(
    r"ingres|venta|factur|inversi|presupuest|costo|gasto|ahorro|utilidad|capital|beneficio|patrimonio|ebitda|activo|pasivo|"
    r"\bimpuestos\b|tribut",
    _I,
)
# Una tabla o fila que habla de esto describe hipótesis, no sanciones impuestas: no se usa el encabezado.
_CONTEXTO_HIPOTETICO_TABLA = re.compile(
    r"riesgos?|probabilidad|potencial\w*|eventual\w*|escenarios?|simulaci\w+|exposici\w+|posibles?|estimad\w+|proyectad\w+|"
    r"presupuest\w+|contingen\w+",
    _I,
)
_NUMERO_PURO = re.compile(rf"{_NUMERO}{_ESCALA}")
_NUMERO_INICIAL = re.compile(_NUMERO)
_SEPARADOR_FINAL = re.compile(r"[.,'’  ](\d+)$")

# --- Entidades explícitas -------------------------------------------------------------------------------
_SIGLAS = (
    r"(?:OEFA|SUNAFIL|SUNAT|OSINERGMIN|OSINERG|ANA|INDECOPI|SMV|SBS|SENACE|SUNASS|OSITRAN|SUCAMEC|SERFOR|"
    r"SERNANP|MINEM|MTPE|MINAM|DIGESA|SUTRAN|SENASA|OSCE|ANLA|SEC|EPA|IBAMA|OSIPTEL|SUNARP|SUNEDU|MIDAGRI|MINSA|MTC|MEF|"
    r"OSINFOR|DICAPI|DIGEMID|SUSALUD|PRODUCE|MIDIS|SENAMHI|INGEMMET|SBN)"
)
# Sigla genérica: solo se acepta donde el texto la presenta como AUTORIDAD que impone («impuesta por el X», «el X impuso»).
# Se excluyen monedas, unidades, formas societarias y siglas de uso común que no son entidades.
_SIGLA_GENERICA = (
    r"(?-i:(?!(?:USD|PEN|EUR|UITS?|IGV|IVA|GRI|ESG|ISO|PDF|SAC|SRL|EIRL|RUC|DNI|CEO|ONG|MWH|TCO|NOX|SOX)\b)"
    r"[A-ZÁÉÍÓÚÑ]{3,10}\b)"
)
_NOMBRE_ENTIDAD = (
    r"(?:(?:Organismo|Autoridad|Superintendencia|Ministerio|Instituto|Tribunal|Direcci[oó]n|Gobierno|Municipalidad|"
    r"Agencia|Comisi[oó]n|Servicio|Oficina|Junta|Consejo)\b"
    r"(?:\s+(?:de|del|de\s+la|de\s+los|de\s+las|para|y|e|en)\b)?"
    r"(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñÁÉÍÓÚÑ]*(?:\s+(?:de|del|de\s+la|y)\b(?=\s+[A-ZÁÉÍÓÚÑ]))?){0,6})"
)
_ENTIDAD = rf"(?P<ent>{_NOMBRE_ENTIDAD}(?:\s*\(\s*{_SIGLAS}\s*\))?|{_SIGLAS})"
_ARTICULO = r"(?:(?i:el|la|los|las)\s+)?"
_ENTIDAD_AGENTE = rf"(?P<ent>{_NOMBRE_ENTIDAD}(?:\s*\(\s*{_SIGLAS}\s*\))?|{_SIGLAS}|{_SIGLA_GENERICA})"
# «multa impuesta por el OEFA de S/ 1000»: la entidad explícita forma parte de la misma frase nominal.
_ENTIDAD_SIN_GRUPO = _ENTIDAD.replace("(?P<ent>", "(?:", 1)
_AGENTE_SIN_GRUPO = _ENTIDAD_AGENTE.replace("(?P<ent>", "(?:", 1)
_ENTIDAD_POR = re.compile(
    r"(?i:impuest[ao]s?|aplicad[ao]s?|determinad[ao]s?|multad[ao]s?|sancionad[ao]s?|emitid[ao]s?|dictad[ao]s?|notificad[ao]s?|"
    r"interpuest[ao]s?|ordenad[ao]s?)"
    rf"\s+(?i:por)\s+(?:(?i:parte\s+de)\s+)?{_ARTICULO}{_ENTIDAD_AGENTE}"
)
# «sanción administrativa del OEFA»: los calificativos intermedios no rompen el vínculo entre el término y su entidad.
_ENTIDAD_TRAS_TERMINO = re.compile(
    rf"(?i:multas?|sanci(?:[óo]n|ones)(?:\s+{_CALIFICATIVOS})*)(?:\s*\([^()\n]{{1,40}}\))?\s+(?:(?i:de|del|de\s+la|por\s+parte\s+de)\s+)"
    rf"{_ARTICULO}{_ENTIDAD}"
)
# «multas del OEFA y de la SUNAFIL»: las entidades coordinadas también quedan ligadas al término.
_ENTIDAD_COORDINADA = re.compile(
    rf"\s*(?:,|(?i:y|e|o))\s*(?:(?i:de|del|de\s+la|por\s+parte\s+de)\s+)?{_ARTICULO}{_ENTIDAD}"
)
# Una celda de tabla que ES una entidad conocida («OEFA», «el Organismo de Evaluación…»).
_ENTIDAD_EXACTA = re.compile(rf"{_ARTICULO}{_ENTIDAD}")
_ENTIDAD_DE_PARTE = re.compile(rf"(?i:de\s+parte\s+de|por\s+parte\s+de)\s+{_ARTICULO}{_ENTIDAD_AGENTE}")
_ENTIDAD_ACTIVA = re.compile(
    rf"{_ENTIDAD_AGENTE}\s+(?:(?i:nos|le|les|la|lo)\s+)?"
    r"(?i:impuso|impusieron|mult[óo]|multaron|sancion[óo]|sancionaron|aplic[óo]|aplicaron|emiti[óo]|emitieron|dict[óo]|"
    r"dictaron|interpuso|interpusieron)"
)

# --- Fechas --------------------------------------------------------------------------------------------
_MES = r"\b(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|se?ptiembre|octubre|noviembre|diciembre)\b"
_FECHA_TEXTO = rf"(?:\d{{1,2}}\s+de\s+)?{_MES}(?:\s+(?:de|del)\s+(?:19|20)\d{{2}})?|\b(?:19|20)\d{{2}}\b"
_FECHA = re.compile(_FECHA_TEXTO, _I)
_ANIO_PREFIJO = r"(?:a[ñn]o\s+|ejercicio\s+|per[ií]odo\s+|periodo\s+)?"
_PERIODO_INICIAL = re.compile(
    rf"\s*(?:En|Durante|Al\s+cierre\s+del?|Hacia|Desde)\s+(?:el\s+)?{_ANIO_PREFIJO}(?P<f>{_FECHA_TEXTO})\s*,?", _I
)
_PERIODO_POSTERIOR = re.compile(
    r"(?:\s+(?:impuest[ao]s?|aplicad[ao]s?|pagad[ao]s?|notificad[ao]s?|recibid[ao]s?))?"
    rf"\s+(?:en|durante|el|del|con\s+fecha\s+(?:de|del))\s+(?:el\s+)?{_ANIO_PREFIJO}(?P<f>{_FECHA_TEXTO})",
    _I,
)

# Entre el término y el importe: calificativos, entidad («del OEFA», «impuesta por el OEFA») y fechas, y luego un conector.
_RELLENO_ENTIDAD = (
    r"(?:\s+(?:impuest[ao]s?|aplicad[ao]s?|emitid[ao]s?|dictad[ao]s?|notificad[ao]s?|determinad[ao]s?|interpuest[ao]s?)"
    rf"\s+por\s+(?:parte\s+de\s+)?(?:(?:el|la|los|las)\s+)?(?-i:{_AGENTE_SIN_GRUPO})"
    rf"|\s+(?:de|del|de\s+la|por\s+parte\s+de|ante)\s+(?:(?:el|la|los|las)\s+)?(?-i:{_ENTIDAD_SIN_GRUPO}))"
)
_RELLENO_DESTINATARIO = (
    r"(?:\s+(?:a|contra)\s+(?:la|el|nuestra|nuestro)\s+(?:empresa|compañ[íi]a|organizaci[óo]n|sociedad|filial|subsidiaria|"
    r"operaci[óo]n|planta|proyecto))"
)
_RELLENO_FECHA = (
    rf"(?:\s+(?:en|el|durante|del|con\s+fecha\s+(?:de|del))\s+(?:el\s+)?{_ANIO_PREFIJO}(?:{_FECHA_TEXTO}))"
)
_ENLACE_MONTO = re.compile(
    rf"(?:{_RELLENO_ENTIDAD}|{_RELLENO_DESTINATARIO}|{_RELLENO_FECHA}|\s+{_CALIFICATIVOS})*{_CONECTOR_DEBIL}", _I
)

# --- Estructura ----------------------------------------------------------------------------------------
_FIN_ORACION = re.compile(r"[.!?]+[\"”’)\]]*\s+")
_ABREVIATURAS = frozenset(
    "sr sra srta ing dr dra lic art arts inc cía cia ltda av núm num nro no n s sa sac etc vs pág pp ej aprox "
    "tel dpto mg prof gral cap".split()
)
_MARCA_DE_LISTA = re.compile(r"[ \t]*(?:[-*+•]|\d{1,3}[.)])[ \t]+")


@dataclass(frozen=True, slots=True)
class Dato:
    """Texto literal del documento con su posición."""

    texto: str
    inicio: int
    fin: int


@dataclass(frozen=True, slots=True)
class MontoSancion:
    # Texto original tal como aparece («59.078 UIT», «S/ 5,000»). No se convierte.
    texto: str
    inicio: int
    fin: int
    # Unidad o moneda escrita en el documento («UIT», «S/», «soles»).
    unidad: str
    numero_texto: str
    separador_ambiguo: bool
    vinculo: str
    # Si la moneda no está dentro de `texto` (encabezado de la columna, columna «Moneda»), dónde está escrita en el original.
    unidad_inicio: int | None = None
    unidad_fin: int | None = None


@dataclass(frozen=True, slots=True)
class Calificador:
    codigo: str
    texto: str
    inicio: int
    fin: int


@dataclass(frozen=True, slots=True)
class SancionDetectada:
    tipo_termino: str
    referencia_original: str
    inicio: int
    fin: int
    cita: str
    cita_inicio: int
    cita_fin: int
    seccion: str
    en_tabla: bool
    base_deteccion: tuple[str, ...]
    entidad: Dato | None
    monto: MontoSancion | None
    periodo: Dato | None
    fechas_en_contexto: tuple[Dato, ...]
    calificadores: tuple[Calificador, ...]
    motivos_revision: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultadoSanciones:
    sanciones: tuple[SancionDetectada, ...]
    # (motivo, cantidad) de menciones descartadas, en orden alfabético.
    descartes: tuple[tuple[str, int], ...] = ()

    @property
    def total(self) -> int:
        return len(self.sanciones)

    @property
    def descartes_por_motivo(self) -> dict[str, int]:
        return dict(self.descartes)


# --- Recorrido estructural -------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Unidad:
    inicio: int
    fin: int
    seccion: str
    tipo: str  # "parrafo" | "celda" | "fila"
    celdas: tuple[tuple[int, int], ...] = ()
    # Texto y rangos de los encabezados de la tabla (solo en filas con el número de columnas del encabezado).
    encabezados: tuple[str, ...] = ()
    encabezados_rangos: tuple[tuple[int, int], ...] = ()


class _RecorridoDeUnidades:
    """Estado del recorrido línea a línea de `_unidades`."""

    def __init__(self, texto: str) -> None:
        self.texto = texto
        self.pila: list[tuple[int, str]] = []
        self.cerca: str | None = None
        self.parrafo: list[int] | None = None  # [inicio, fin]
        self.n_columnas: int | None = None
        self.encabezados: tuple[str, ...] = ()
        self.encabezados_rangos: tuple[tuple[int, int], ...] = ()
        self.delimitador_pendiente = False

    def _seccion(self) -> str:
        return SEPARADOR_RUTA.join(t for _, t in self.pila if t)

    def _cerrar_parrafo(self) -> Iterator[_Unidad]:
        if self.parrafo is not None:
            unidad = _Unidad(self.parrafo[0], self.parrafo[1], self._seccion(), "parrafo")
            self.parrafo = None
            yield unidad

    def recorrer(self) -> Iterator[_Unidad]:
        texto = self.texto
        longitud = len(texto)
        posicion = 0
        while posicion < longitud:
            fin_linea, siguiente = markdown.limites_de_linea(texto, posicion)
            yield from self._linea(posicion, fin_linea, siguiente)
            posicion = siguiente
        yield from self._cerrar_parrafo()

    def _linea(self, posicion: int, fin_linea: int, siguiente: int) -> Iterator[_Unidad]:
        texto = self.texto
        self.cerca, es_codigo = markdown.actualizar_cerca(texto, self.cerca, posicion, fin_linea)
        if es_codigo:
            self.n_columnas = None
            yield from self._cerrar_parrafo()
            return
        titulo = markdown.titulo_atx(texto, posicion, fin_linea)
        if titulo is not None:
            self.n_columnas = None
            yield from self._cerrar_parrafo()  # con la sección anterior: el encabezado aún no la cambia
            nivel, texto_titulo = titulo
            while self.pila and self.pila[-1][0] >= nivel:
                self.pila.pop()
            self.pila.append((nivel, texto_titulo))
            return
        if self.delimitador_pendiente:
            self.delimitador_pendiente = False
            return
        if self.n_columnas is not None:
            if markdown.puede_ser_fila(texto, posicion, fin_linea):
                yield from self._fila(posicion, fin_linea)
                return
            self.n_columnas = None
        if markdown.es_blanca(texto, posicion, fin_linea):
            yield from self._cerrar_parrafo()
            return
        if self._inicia_tabla(posicion, fin_linea, siguiente):
            yield from self._cerrar_parrafo()
            rangos = markdown.rangos_de_celdas(texto, posicion, fin_linea)
            self.n_columnas = len(rangos)
            self.encabezados = tuple(texto[a:b] for a, b in rangos)
            self.encabezados_rangos = tuple(rangos)
            self.delimitador_pendiente = True
            return
        yield from self._parrafo(posicion, fin_linea)

    def _inicia_tabla(self, posicion: int, fin_linea: int, siguiente: int) -> bool:
        texto = self.texto
        if not (markdown.tiene_pipe(texto, posicion, fin_linea) and siguiente < len(texto)):
            return False
        fin_sig, _ = markdown.limites_de_linea(texto, siguiente)
        return markdown.es_inicio_tabla(texto[posicion:fin_linea], texto[siguiente:fin_sig])

    def _fila(self, posicion: int, fin_linea: int) -> Iterator[_Unidad]:
        celdas = markdown.rangos_de_celdas(self.texto, posicion, fin_linea)
        if len(celdas) == self.n_columnas:
            yield _Unidad(
                posicion, fin_linea, self._seccion(), "fila", tuple(celdas), self.encabezados, self.encabezados_rangos
            )
            return
        for a, b in celdas:
            if b > a:
                yield _Unidad(a, b, self._seccion(), "celda")

    def _parrafo(self, posicion: int, fin_linea: int) -> Iterator[_Unidad]:
        texto = self.texto
        inicio_contenido = posicion + len(texto[posicion:fin_linea]) - len(texto[posicion:fin_linea].lstrip())
        if self.parrafo is not None and _MARCA_DE_LISTA.match(texto, posicion, fin_linea):
            yield from self._cerrar_parrafo()
        if self.parrafo is None:
            self.parrafo = [inicio_contenido, fin_linea]
        else:
            self.parrafo[1] = fin_linea


def _unidades(texto: str) -> Iterator[_Unidad]:
    """Párrafos, filas de tabla (si tienen el número de columnas del encabezado) y
    celdas sueltas (si no). Omite encabezados, bloques de código y la cabecera de
    las tablas."""
    return _RecorridoDeUnidades(texto).recorrer()


def _es_letra(caracter: str) -> bool:
    """Letra en sentido Unicode: alfanumérico que no es dígito decimal (ni «_»)."""
    return caracter.isalnum() and not caracter.isdecimal()


def _ultima_palabra(ventana: str) -> str:
    """Última racha de letras al final de `ventana`; si termina en otro símbolo no blanco, ese símbolo; si termina en
    blanco (o está vacía), cadena vacía."""
    if ventana.endswith("\n"):  # `$` de la expresión equivalente admite un salto de línea final
        ventana = ventana[:-1]
    if not ventana or ventana[-1].isspace():
        return ""
    inicio = len(ventana)
    while inicio > 0 and _es_letra(ventana[inicio - 1]):
        inicio -= 1
    return ventana[inicio:] if inicio < len(ventana) else ventana[-1]


def _punto_cierra_oracion(texto: str, inicio: int, punto: re.Match[str]) -> bool:
    """False si el «.» es de una abreviatura, una inicial («S.A.»), «S/.» o va seguido de minúscula;
    un dígito o «)» antes del punto sí cierran la oración."""
    token = _ultima_palabra(texto[max(inicio, punto.start() - 12):punto.start()])
    if token and (token.lower() in _ABREVIATURAS or (len(token) == 1 and (token.isalpha() or token == "/"))):
        return False
    return not texto[punto.end()].islower()


def _oraciones(texto: str, inicio: int, fin: int) -> list[tuple[int, int]]:
    """Oraciones de [inicio, fin): corta tras . ! ? y espacio, salvo abreviaturas,
    iniciales, «S/.» y cuando lo que sigue empieza en minúscula."""
    cortes = [inicio]
    for encontrado in _FIN_ORACION.finditer(texto, inicio, fin):
        siguiente = encontrado.end()
        if siguiente >= fin:
            continue
        puntuacion = texto[encontrado.start():encontrado.end()].strip()
        if puntuacion.startswith(".") and not _punto_cierra_oracion(texto, inicio, encontrado):
            continue
        cortes.append(siguiente)
    cortes.append(fin)
    oraciones = []
    for a, b in zip(cortes, cortes[1:]):
        while a < b and texto[a].isspace():
            a += 1
        while b > a and texto[b - 1].isspace():
            b -= 1
        if b > a:
            oraciones.append((a, b))
    return oraciones


# --- Análisis ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _NumerosDeFila:
    """Cifras de las celdas de una fila distintas de la que contiene el término."""

    montos: list[MontoSancion]  # con unidad (en la celda, en el encabezado o en una columna de moneda) y distintos de cero
    sin_unidad: int  # cifras distintas de cero sin moneda ni UIT
    ceros_con_unidad: int  # «S/ 0»
    ceros_sin_unidad: int  # «0»
    desconocido: bool = False  # una columna de importes dice expresamente «No especificado», «Por determinar»…
    solo_saldo: bool = False  # hay cifras de saldo o pago parcial con moneda y ninguna de monto total


@dataclass(frozen=True, slots=True)
class _Columna:
    """Papel de una columna según su encabezado (solo en tablas bien formadas y sin lenguaje hipotético)."""

    moneda: tuple[str, int, int] | None = None  # (literal, inicio, fin) de la ÚNICA moneda nombrada en el encabezado
    importe: bool = False  # el encabezado nombra un importe de la sanción («Monto», «Multa») o es solo una moneda
    saldo: bool = False  # saldo, pendiente, pagado, descuento: no es el monto total
    ajena: bool = False  # ingresos, inversión…: otra magnitud
    entidad: bool = False
    estado: bool = False
    columna_moneda: bool = False  # la columna contiene la moneda de otra columna

    @property
    def monto(self) -> bool:
        """Una cifra pura de esta columna es un monto: es un importe de la sanción y su moneda consta en el encabezado."""
        return self.importe and self.moneda is not None


@dataclass(slots=True)
class _Termino:
    tipo: str
    inicio: int
    fin: int
    anaforico: bool = False


@dataclass(slots=True)
class _Borrador:
    unidad: _Unidad
    termino: _Termino
    clausula: tuple[int, int]
    oraciones: list[tuple[int, int]]
    base: list[str]
    monto: MontoSancion | None
    en_tabla: bool
    motivos: list[str] = field(default_factory=list)
    adicionales: list[_Termino] = field(default_factory=list)
    indice_celda: int = 0  # celda de la fila donde está el término (si la unidad es una fila)


@dataclass(slots=True)
class _AcumuladorDeFila:
    """Lo que se va sabiendo de las demás celdas de una fila mientras se recorre."""

    montos: list[MontoSancion] = field(default_factory=list)
    sin_unidad: int = 0
    ceros_con_unidad: int = 0
    ceros_sin_unidad: int = 0
    saldos_con_moneda: int = 0
    desconocido: bool = False
    monedas_de_celda: list[tuple[int, int]] = field(default_factory=list)
    candidatas: list[tuple[int, int, bool]] = field(default_factory=list)  # (inicio, fin, es_cero)

class _Analizador:
    def __init__(self, texto: str) -> None:
        self.texto = texto
        self.sanciones: list[SancionDetectada] = []
        self.descartes: dict[str, int] = {}
        self._columnas_cache: dict[tuple[tuple[int, int], ...], tuple[_Columna, ...]] = {}

    def _descartar(self, motivo: str) -> None:
        self.descartes[motivo] = self.descartes.get(motivo, 0) + 1

    # Términos --------------------------------------------------------------------------

    def _terminos(self, inicio: int, fin: int) -> list[_Termino]:
        texto = self.texto
        terminos = [_Termino(TERMINO_MULTA, m.start(), m.end()) for m in _MULTA.finditer(texto, inicio, fin)]
        economicas = [
            _Termino(TERMINO_SANCION_ECONOMICA, m.start(), m.end()) for m in _SANCION_ECONOMICA.finditer(texto, inicio, fin)
        ]
        terminos += economicas
        for m in _SANCION_GENERICA.finditer(texto, inicio, fin):
            if any(e.inicio <= m.start() < e.fin for e in economicas):
                continue
            terminos.append(_Termino(TERMINO_SANCION_CON_MONTO, m.start(), m.end()))
        terminos.sort(key=lambda t: t.inicio)
        for termino in terminos:
            antes = texto[max(inicio, termino.inicio - 40):termino.inicio]
            termino.anaforico = _ANAFORA.search(antes) is not None
        return terminos

    def _clausulas(self, inicio: int, fin: int) -> list[tuple[int, int]]:
        limites = [inicio]
        clausulas = []
        for delimitador in _DELIMITADOR_CLAUSULA.finditer(self.texto, inicio, fin):
            clausulas.append((limites[-1], delimitador.start()))
            limites.append(delimitador.end())
        clausulas.append((limites[-1], fin))
        return clausulas

    # Exclusiones ---------------------------------------------------------------------------

    def _segmento(self, termino: _Termino, clausula: tuple[int, int]) -> tuple[int, int]:
        """Parte de la cláusula entre las comas que rodean al término."""
        texto = self.texto
        inicio = texto.rfind(",", clausula[0], termino.inicio)
        fin = texto.find(",", termino.fin, clausula[1])
        return (clausula[0] if inicio < 0 else inicio + 1, clausula[1] if fin < 0 else fin)

    def _niega_la_existencia(self, termino: _Termino, segmento: tuple[int, int]) -> bool:
        """¿La negación que precede al término niega que la sanción exista?

        Se mira la última negación antes del término, dentro del mismo segmento (sin cruzar comas
        ni conjunciones). Si el verbo que la sigue es de ESTADO (pagar, impugnar, apelar…), lo
        negado es el pago o el recurso, no la imposición: la multa se conserva. Si es otro verbo
        («no recibimos», «no se impusieron») o no hay verbo («sin multas», «ninguna multa»), se
        niega la existencia y se excluye."""
        texto = self.texto
        previo = texto[max(segmento[0], termino.inicio - 100):termino.inicio]
        corte = max((m.end() for m in _CORTE_DE_ALCANCE_NEGACION.finditer(previo)), default=0)
        previo = previo[corte:]
        negaciones = list(_NEGACION.finditer(previo))
        if not negaciones:
            return False
        verbo = _PRIMER_VERBO.match(previo, negaciones[-1].end())
        if verbo is not None and _VERBO_DE_ESTADO.fullmatch(verbo.group(1)):
            return False
        # «No se especifica el monto de la multa»: lo negado es que el importe conste, no que la multa exista.
        if _NOMBRE_DE_MONTO_SUELTO.search(previo, negaciones[-1].end()):
            return False
        return True

    def _motivo_de_exclusion(self, termino: _Termino, clausula: tuple[int, int]) -> str | None:
        texto = self.texto
        a, b = clausula
        if _DISCIPLINARIA.search(texto, a, b):
            return DESCARTE_DISCIPLINARIA
        segmento = self._segmento(termino, clausula)
        if self._niega_la_existencia(termino, segmento) or _NEGACION_POSTERIOR.match(texto[termino.fin:b]):
            return DESCARTE_NEGACION
        if (
            _HIPOTETICA_CLAUSULA.search(texto, a, b)
            or _HIPOTETICA_SI.search(texto, a, b)
            or _MODAL_ANTES.search(texto[segmento[0]:termino.inicio])
            or _MODAL_IMPOSICION.search(texto, segmento[0], segmento[1])
        ):
            return DESCARTE_HIPOTETICA
        return None

    # Montos ----------------------------------------------------------------------------------

    @staticmethod
    def _es_cero(numero: str) -> bool:
        digitos = re.sub(r"\D", "", numero)
        return bool(digitos) and not digitos.strip("0")

    @staticmethod
    def _separador_ambiguo(numero: str) -> bool:
        final = _SEPARADOR_FINAL.search(numero)
        if final is None or len(final.group(1)) != 3:
            return False
        separadores = re.findall(r"[.,'’  ]", numero)
        # 1.234.567 (mismo separador repetido) es de miles; 59.078, 1,500 o 8’044,241 no se interpretan.
        return len(separadores) == 1 or len(set(separadores)) > 1

    def _monto_en(self, inicio: int, limite: int, vinculo: str) -> MontoSancion | None:
        for patron in _SOLO_MONTO:
            encontrado = patron.match(self.texto, inicio, limite)
            if encontrado is not None:
                numero = encontrado.group("numero")
                return MontoSancion(
                    texto=encontrado.group(),
                    inicio=encontrado.start(),
                    fin=encontrado.end(),
                    unidad=encontrado.group("unidad"),
                    numero_texto=numero,
                    separador_ambiguo=self._separador_ambiguo(numero),
                    vinculo=vinculo,
                )
        return None

    def _monto_vinculado(self, termino: _Termino, clausula: tuple[int, int]) -> MontoSancion | None:
        # Se prueba en este orden y gana el primero que encuentra un monto.
        # Un término verbal («multó a la Compañía con S/ 3») se une por la vía del verbo; el resto por conector.
        for buscar in (self._monto_tras_verbo, self._monto_tras_conector, self._monto_antes_del_termino):
            monto = buscar(termino, clausula)
            if monto is not None:
                return monto
        return None

    def _monto_tras_conector(self, termino: _Termino, clausula: tuple[int, int]) -> MontoSancion | None:
        texto = self.texto
        limite = clausula[1]
        enlace = _ENLACE_MONTO.match(texto, termino.fin, limite)
        if enlace is not None:
            monto = self._monto_en(enlace.end(), limite, "termino_y_conector")
            if monto is not None:
                return monto
        return self._monto_con_cue_fuerte(termino, clausula)

    def _primer_monto_del_alcance(self, termino: _Termino, clausula: tuple[int, int]) -> re.Match[str] | None:
        """Primer importe (por posición) dentro del alcance posterior del término."""
        texto = self.texto
        _, (a, b) = self._rangos_de_ambito(termino, clausula, PALABRAS_HASTA_EL_MONTO)
        primero = None
        for patron in _SOLO_MONTO:
            encontrado = patron.search(texto, a, b)
            if encontrado is not None and (primero is None or encontrado.start() < primero.start()):
                primero = encontrado
        return primero

    def _monto_con_cue_fuerte(self, termino: _Termino, clausula: tuple[int, int]) -> MontoSancion | None:
        # «sanción administrativa por incumplimiento de plazos por un monto de S/ 500»: el sustantivo «monto» (o «equivalente
        # a», «asciende a»…) dice que lo que sigue es el importe, aunque medien otras palabras.
        primero = self._primer_monto_del_alcance(termino, clausula)
        if primero is None:
            return None
        cue = _CUE_FUERTE_FINAL.search(self.texto, termino.fin, primero.start())
        if cue is None or _BARRERA_DE_PUENTE.search(self.texto, termino.fin, cue.start()):
            return None
        return self._monto_en(primero.start(), primero.end(), "termino_y_cue_de_importe")

    def _menciona_solo_un_saldo(self, termino: _Termino, clausula: tuple[int, int]) -> bool:
        """El importe más cercano del término es un saldo o un pago parcial («saldo pendiente de S/ 2,000»)."""
        primero = self._primer_monto_del_alcance(termino, clausula)
        return primero is not None and _SALDO_ANTES_DEL_MONTO.search(self.texto, termino.fin, primero.start()) is not None

    def _monto_tras_verbo(self, termino: _Termino, clausula: tuple[int, int]) -> MontoSancion | None:
        # «nos multó con S/ 10», «fuimos multados por 5 UIT»
        texto = self.texto
        limite = clausula[1]
        if not re.fullmatch(r"(?:mult|sancion)(?:[óo]|aron|amos|ad[oa]s?)", texto[termino.inicio:termino.fin], _I):
            return None
        fin_verbo = _fin_de_verbo_con_monto(texto, termino.fin, limite)
        if fin_verbo is None:
            return None
        return self._monto_en(fin_verbo, limite, "verbo")

    def _monto_antes_del_termino(self, termino: _Termino, clausula: tuple[int, int]) -> MontoSancion | None:
        # «S/ 3,000 de multa»
        texto = self.texto
        for antes in _MONTO_ANTES.finditer(texto, clausula[0], clausula[1]):
            if antes.start("termino") != termino.inicio:
                continue
            if any(patron.match(texto, antes.start(), antes.end()) is not None for patron in _SOLO_MONTO):
                monto = self._monto_en(antes.start(), antes.end(), "monto_antes_del_termino")
                if monto is not None:
                    return monto
        return None

    # Entidad, periodo y calificadores -------------------------------------------------------------

    def _entidades_del_texto(self, clausula: tuple[int, int]) -> dict[str, Dato]:
        """Entidades que el texto de la cláusula une explícitamente a la sanción, por clave normalizada."""
        texto = self.texto
        encontradas: dict[str, Dato] = {}
        for patron in (_ENTIDAD_POR, _ENTIDAD_TRAS_TERMINO, _ENTIDAD_DE_PARTE, _ENTIDAD_ACTIVA):
            admite_coordinadas = patron is _ENTIDAD_POR or patron is _ENTIDAD_TRAS_TERMINO
            for m in patron.finditer(texto, clausula[0], clausula[1]):
                coincidencias = [m]
                while admite_coordinadas and (c := _ENTIDAD_COORDINADA.match(texto, coincidencias[-1].end(), clausula[1])):
                    coincidencias.append(c)
                for c in coincidencias:
                    clave = re.sub(r"\s+", " ", c.group("ent"))
                    encontradas.setdefault(clave, Dato(c.group("ent"), c.start("ent"), c.end("ent")))
        return encontradas

    def _entidad(
        self, clausula: tuple[int, int], de_fila: Sequence[Dato] = (), fila_ambigua: bool = False
    ) -> tuple[Dato | None, bool]:
        """(entidad, ambigua). `de_fila` son las entidades de otras celdas de la fila de tabla del término."""
        encontradas = self._entidades_del_texto(clausula)
        for dato in de_fila:
            encontradas.setdefault(re.sub(r"\s+", " ", dato.texto), dato)
        if len(encontradas) == 1 and not fila_ambigua:
            return next(iter(encontradas.values())), False
        return None, len(encontradas) > 1 or fila_ambigua

    def _periodo(self, termino: _Termino, monto: MontoSancion | None, clausula: tuple[int, int], oracion_inicio: int) -> Dato | None:
        texto = self.texto
        if clausula[0] <= oracion_inicio:
            inicial = _PERIODO_INICIAL.match(texto, clausula[0], termino.inicio)
            if inicial is not None:
                return Dato(inicial.group("f"), inicial.start("f"), inicial.end("f"))
        desde = max(termino.fin, monto.fin) if monto is not None else termino.fin
        posterior = _PERIODO_POSTERIOR.match(texto, desde, clausula[1])
        if posterior is not None:
            return Dato(posterior.group("f"), posterior.start("f"), posterior.end("f"))
        return None

    # Ámbito del término -----------------------------------------------------------------------------

    def _rangos_de_ambito(
        self, termino: _Termino, clausula: tuple[int, int], max_despues: int = PALABRAS_DESPUES_DEL_TERMINO
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """((inicio, fin) antes del término, (inicio, fin) después). El ámbito son las pocas palabras
        contiguas al término sin cruzar comas, punto y coma ni conjunciones (salvo que continúe el
        mismo predicado). Es lo que se considera «vinculado» a la sanción."""
        texto = self.texto
        desde = max(clausula[0], termino.inicio - 160)
        previo = texto[desde:termino.inicio]
        corte = max((m.end() for m in _SEPARADOR_DE_ALCANCE.finditer(previo)), default=0)
        inicio = desde + corte
        palabras = [m.start() for m in re.finditer(r"\S+", texto[inicio:termino.inicio])]
        if len(palabras) > PALABRAS_ANTES_DEL_TERMINO:
            inicio += palabras[-PALABRAS_ANTES_DEL_TERMINO]
        limite = min(clausula[1], termino.fin + 160)
        posterior = texto[termino.fin:limite]
        fin_alcance = _FIN_DE_ALCANCE.search(posterior)
        fin = termino.fin + (fin_alcance.start() if fin_alcance else len(posterior))
        palabras = [m.end() for m in re.finditer(r"\S+", texto[termino.fin:fin])]
        if len(palabras) > max_despues:
            fin = termino.fin + palabras[max_despues - 1]
        return (inicio, termino.inicio), (termino.fin, fin)

    def _buscar_vinculado(
        self,
        patron: "re.Pattern[str] | _UnionDePatrones",
        rango: tuple[int, int],
        lado: str,
        limite_izquierdo: int | None = None,
    ) -> re.Match[str] | None:
        """Primera coincidencia de `patron` en el ámbito, descartando las que tienen entre sí y el
        término un objeto ajeno («pagó los impuestos asociados a la multa») y, si se pide, las
        negadas («no fue pagada»)."""
        texto = self.texto
        a, b = rango
        for m in patron.finditer(texto, a, b):
            intermedio = (m.end(), b) if lado == "antes" else (a, m.start())
            if _OBJETO_AJENO.search(texto, *intermedio):
                continue
            if limite_izquierdo is not None:
                previo = texto[max(limite_izquierdo, m.start() - 60):m.start()]
                if _niega_el_calificador(previo):
                    continue
            return m
        return None

    def _calificadores(self, oraciones: list[tuple[int, int]]) -> list[Calificador]:
        """Calificadores literales vinculados a algún término de sanción de las oraciones: el
        mismo término, uno anafórico («esta multa fue pagada») o la «sanción» genérica de la misma
        oración («la impugnación de las sanciones … correspondientes a una multa»)."""
        encontrados: dict[str, Calificador] = {}
        for a, b in oraciones:
            clausulas = self._clausulas(a, b)
            for termino in self._terminos(a, b):
                clausula = next(c for c in clausulas if c[0] <= termino.inicio <= c[1])
                antes, despues = self._rangos_de_ambito(termino, clausula)
                for codigo, patron in _CALIFICADORES:
                    if codigo in encontrados:
                        continue
                    m = self._buscar_vinculado(patron, antes, "antes", clausula[0]) or self._buscar_vinculado(
                        patron, despues, "despues", clausula[0]
                    )
                    if m is not None:
                        encontrados[codigo] = Calificador(codigo, m.group(), m.start(), m.end())
        return sorted(encontrados.values(), key=lambda c: c.inicio)

    # Construcción -----------------------------------------------------------------------------------

    def _evidencia(self, termino: _Termino, clausula: tuple[int, int], monto: MontoSancion | None) -> list[str]:
        """Evidencia de imposición VINCULADA al término: un monto unido a él, un verbo de
        imposición/pago o un participio cercano (propio del término o en su ámbito) o, más débil,
        un verbo de trámite en su ámbito."""
        base = []
        if monto is not None:
            base.append(BASE_MONTO_VINCULADO)
        texto = self.texto
        antes, despues = self._rangos_de_ambito(termino, clausula)
        propio = re.fullmatch(r"(?:mult|sancion)(?:[óo]|aron|amos|ad[oa]s?)", texto[termino.inicio:termino.fin], _I)
        if (
            propio
            or self._buscar_vinculado(_EVIDENCIA_ANTES, antes, "antes")
            or self._buscar_vinculado(_EVIDENCIA_DESPUES, despues, "despues")
        ):
            base.append(BASE_VERBO_IMPOSICION)
        if not base and (
            self._buscar_vinculado(_TRAMITE_VINCULADO, antes, "antes")
            or self._buscar_vinculado(_TRAMITE_VINCULADO, despues, "despues")
        ):
            base.append(BASE_VERBO_TRAMITE)
        return base

    def _limites_de_cita(self, b: _Borrador) -> tuple[int, int]:
        """Rango de la cita: la fila de la tabla o las oraciones del hallazgo, acotado a `MAX_LONGITUD_CITA`
        alrededor del término y sin blancos en los extremos (el término siempre queda dentro)."""
        texto = self.texto
        termino = b.termino
        if b.en_tabla and b.unidad.tipo == "fila":
            cita_inicio, cita_fin = b.unidad.inicio, b.unidad.fin
        else:
            cita_inicio, cita_fin = min(a for a, _ in b.oraciones), max(f for _, f in b.oraciones)
        if cita_fin - cita_inicio > MAX_LONGITUD_CITA:
            cita_inicio = max(cita_inicio, termino.inicio - MARGEN_CITA)
            cita_fin = min(cita_fin, termino.fin + MARGEN_CITA)
        while cita_inicio < termino.inicio and texto[cita_inicio].isspace():
            cita_inicio += 1
        while cita_fin > termino.fin and texto[cita_fin - 1].isspace():
            cita_fin -= 1
        return cita_inicio, cita_fin

    def _fechas_en(self, inicio: int, fin: int) -> list[Dato]:
        fechas = []
        vistas = set()
        for m in _FECHA.finditer(self.texto, inicio, fin):
            if (m.start(), m.end()) not in vistas:
                vistas.add((m.start(), m.end()))
                fechas.append(Dato(m.group(), m.start(), m.end()))
        return fechas

    def _finalizar(self, b: _Borrador) -> SancionDetectada:
        texto = self.texto
        termino = b.termino
        calificadores = self._calificadores(b.oraciones)
        if b.unidad.tipo == "fila":
            propios = {c.codigo for c in calificadores}
            calificadores = sorted(
                [*calificadores, *(c for c in self._calificadores_de_fila(b.unidad, b.indice_celda) if c.codigo not in propios)],
                key=lambda c: c.inicio,
            )
        de_fila, fila_ambigua = self._entidades_de_fila(b.unidad, b.indice_celda)
        entidad, ambigua = self._entidad(b.clausula, de_fila, fila_ambigua)
        motivos = list(b.motivos)
        if ambigua:
            motivos.append(MOTIVO_ENTIDAD_AMBIGUA)
        if b.monto is not None and b.monto.separador_ambiguo:
            motivos.append(MOTIVO_SEPARADOR_AMBIGUO)
        if [x for x in b.base if x != BASE_CARACTER_ECONOMICO] == [BASE_VERBO_TRAMITE]:
            motivos.append(MOTIVO_SOLO_TRAMITE)
        periodo = self._periodo(termino, b.monto, b.clausula, b.oraciones[0][0])
        cita_inicio, cita_fin = self._limites_de_cita(b)
        fechas = self._fechas_en(cita_inicio, cita_fin)
        return SancionDetectada(
            tipo_termino=termino.tipo if termino.tipo != TERMINO_SANCION_CON_MONTO else TERMINO_SANCION_ECONOMICA,
            referencia_original=texto[termino.inicio:termino.fin],
            inicio=termino.inicio,
            fin=termino.fin,
            cita=texto[cita_inicio:cita_fin],
            cita_inicio=cita_inicio,
            cita_fin=cita_fin,
            seccion=b.unidad.seccion,
            en_tabla=b.en_tabla,
            base_deteccion=tuple(b.base),
            entidad=entidad,
            monto=b.monto,
            periodo=periodo,
            fechas_en_contexto=tuple(fechas),
            calificadores=tuple(calificadores),
            motivos_revision=tuple(dict.fromkeys(motivos)),
        )

    # Recorrido ----------------------------------------------------------------------------------------

    def _monto_exacto(self, a: int, b: int) -> MontoSancion | None:
        """El monto si [a, b) ES exactamente un monto (moneda o UIT)."""
        for patron in _SOLO_MONTO:
            if patron.fullmatch(self.texto, a, b) is not None:
                return self._monto_en(a, b, BASE_FILA_DE_TABLA)
        return None

    @staticmethod
    def _cifra_sin_unidad_relevante(unidad: _Unidad, indice: int, celda: str) -> bool:
        """Un número suelto cuenta, salvo un año en una columna que no es de montos."""
        encabezado = unidad.encabezados[indice] if indice < len(unidad.encabezados) else ""
        es_anio = _ANIO_SUELTO.fullmatch(celda.strip()) is not None
        return not es_anio or _ENCABEZADO_DE_MONTO.search(encabezado) is not None

    # Columnas de la tabla ----------------------------------------------------------------------------------------------

    def _fila_hipotetica(self, unidad: _Unidad) -> bool:
        """La fila habla de riesgos, probabilidad, escenarios o estimaciones: no describe sanciones impuestas."""
        return _CONTEXTO_HIPOTETICO_TABLA.search(self.texto, unidad.inicio, unidad.fin) is not None

    def _columna(self, a: int, b: int) -> _Columna:
        texto = self.texto
        encabezado = texto[a:b]
        monedas = list(_MONEDA_ENCABEZADO.finditer(texto, a, b))
        distintas = {re.sub(r"(?i)^(?:en\s+)?(?:miles|millones)\s+de\s+|[.\s]", "", m.group()).lower() for m in monedas}
        # Dos monedas distintas en un encabezado: no se sabe cuál corresponde a la cifra, no se usa ninguna.
        moneda = (monedas[0].group(), monedas[0].start(), monedas[0].end()) if len(distintas) == 1 else None
        solo_moneda = bool(monedas) and re.search(r"\w", _MONEDA_ENCABEZADO.sub("", encabezado)) is None
        saldo = _ENCABEZADO_SALDO.search(encabezado) is not None
        ajena = _ENCABEZADO_AJENO.search(encabezado) is not None
        importe = (solo_moneda or _ENCABEZADO_IMPORTE.search(encabezado) is not None) and not saldo and not ajena
        return _Columna(
            moneda=moneda,
            importe=importe,
            saldo=saldo,
            ajena=ajena,
            entidad=_ENCABEZADO_ENTIDAD.search(encabezado) is not None,
            estado=_ENCABEZADO_ESTADO.search(encabezado) is not None,
            columna_moneda=_ENCABEZADO_COLUMNA_MONEDA.fullmatch(encabezado) is not None,
        )

    def _columnas_de_fila(self, unidad: _Unidad) -> tuple[_Columna, ...]:
        """Papel de cada columna según su encabezado. Sin papel alguno si la tabla o la fila hablan de hipótesis."""
        if self._fila_hipotetica(unidad) or _CONTEXTO_HIPOTETICO_TABLA.search(" ".join(unidad.encabezados)):
            return tuple(_Columna() for _ in unidad.celdas)
        clave = unidad.encabezados_rangos
        if clave not in self._columnas_cache:
            self._columnas_cache[clave] = tuple(self._columna(a, b) for a, b in clave)
        return self._columnas_cache[clave]

    def _monto_de_columna(self, a: int, b: int, columna: _Columna) -> MontoSancion:
        """Cifra pura de [a, b) cuya moneda consta en el encabezado de su columna."""
        celda = self.texto[a:b]
        numero = _NUMERO_INICIAL.match(celda).group()
        unidad, ua, ub = columna.moneda
        return MontoSancion(
            texto=celda,
            inicio=a,
            fin=b,
            unidad=unidad,
            numero_texto=numero,
            separador_ambiguo=self._separador_ambiguo(numero),
            vinculo=VINCULO_ENCABEZADO,
            unidad_inicio=ua,
            unidad_fin=ub,
        )

    def _entidad_de_celda(self, columna: _Columna, a: int, b: int) -> tuple[Dato | None, bool]:
        """(entidad, ambigua) de una celda de la fila: la de una columna cuyo encabezado nombra a la autoridad, o la de una
        celda que ES una entidad conocida. Una celda con varias entidades no se resuelve."""
        celda = self.texto[a:b]
        if not columna.entidad:
            exacta = _ENTIDAD_EXACTA.fullmatch(self.texto, a, b)
            if exacta is None:
                return None, False
            return Dato(exacta.group("ent"), exacta.start("ent"), exacta.end("ent")), False
        if _SIN_DATO.fullmatch(celda) or _NUMERO_PURO.fullmatch(celda) or _CELDA_MONTO_DESCONOCIDO.fullmatch(celda):
            return None, False
        if _VARIAS_ENTIDADES.search(celda):
            return None, True
        return Dato(celda, a, b), False

    def _entidades_de_fila(self, unidad: _Unidad, indice_celda: int) -> tuple[list[Dato], bool]:
        """Entidades escritas en OTRAS celdas de la fila del término (datos, ambigua)."""
        if unidad.tipo != "fila":
            return [], False
        columnas = self._columnas_de_fila(unidad)
        datos: list[Dato] = []
        ambigua = False
        for i, (a, b) in enumerate(unidad.celdas):
            if i == indice_celda or b <= a:
                continue
            dato, varias = self._entidad_de_celda(columnas[i], a, b)
            ambigua = ambigua or varias
            if dato is not None:
                datos.append(dato)
        return datos, ambigua

    def _calificadores_de_fila(self, unidad: _Unidad, indice_celda: int) -> list[Calificador]:
        """Calificadores literales de las celdas de una columna de estado («Pagada», «Impugnada»)."""
        texto = self.texto
        columnas = self._columnas_de_fila(unidad)
        encontrados: list[Calificador] = []
        for i, (a, b) in enumerate(unidad.celdas):
            if i == indice_celda or b <= a or not columnas[i].estado:
                continue
            for codigo, patron in _CALIFICADORES:
                m = next(iter(patron.finditer(texto, a, b)), None)
                if m is not None and not _niega_el_calificador(texto[a:m.start()]):
                    encontrados.append(Calificador(codigo, m.group(), m.start(), m.end()))
        return encontrados

    @staticmethod
    def _registrar_monto(acum: _AcumuladorDeFila, monto: MontoSancion) -> None:
        if _Analizador._es_cero(monto.numero_texto):
            acum.ceros_con_unidad += 1
        else:
            acum.montos.append(monto)

    def _acumular_cifra(self, acum: _AcumuladorDeFila, a: int, b: int, columna: _Columna, es_cero: bool) -> None:
        """Una cifra suelta (o un cero) de otra celda de la fila."""
        celda = self.texto[a:b]
        if columna.moneda is not None and (columna.saldo or columna.ajena):
            if columna.saldo:
                acum.saldos_con_moneda += 1  # no es el monto total; una columna de otra magnitud se ignora
            return
        if columna.monto and _NUMERO_PURO.fullmatch(celda):
            self._registrar_monto(acum, self._monto_de_columna(a, b, columna))
            return
        if es_cero:
            acum.ceros_sin_unidad += 1
        else:
            acum.sin_unidad += 1
        if columna.importe and columna.moneda is None and _NUMERO_PURO.fullmatch(celda):
            acum.candidatas.append((a, b, es_cero))

    def _clasificar_celda(
        self, unidad: _Unidad, i: int, a: int, b: int, columna: _Columna, acum: _AcumuladorDeFila
    ) -> None:
        celda = self.texto[a:b]
        monto = self._monto_exacto(a, b)
        if monto is not None:
            self._registrar_monto(acum, monto)
            return
        es_cero = _CERO_SUELTO.fullmatch(celda) is not None
        if es_cero or (_NUMERO_SUELTO.fullmatch(celda) and self._cifra_sin_unidad_relevante(unidad, i, celda)):
            self._acumular_cifra(acum, a, b, columna, es_cero)
        elif columna.columna_moneda and _MONEDA_CELDA.fullmatch(celda):
            acum.monedas_de_celda.append((a, b))
        elif columna.importe and _CELDA_MONTO_DESCONOCIDO.fullmatch(celda):
            acum.desconocido = True

    def _aplicar_columna_de_moneda(self, acum: _AcumuladorDeFila) -> None:
        """«Monto | Moneda»: la moneda consta en otra celda de la misma fila."""
        if len(acum.monedas_de_celda) != 1 or len(acum.candidatas) != 1:
            return
        (ca, cb, era_cero), (ma, mb) = acum.candidatas[0], acum.monedas_de_celda[0]
        numero = _NUMERO_INICIAL.match(self.texto[ca:cb]).group()
        propio = MontoSancion(
            texto=self.texto[ca:cb],
            inicio=ca,
            fin=cb,
            unidad=self.texto[ma:mb],
            numero_texto=numero,
            separador_ambiguo=self._separador_ambiguo(numero),
            vinculo=VINCULO_COLUMNA_MONEDA,
            unidad_inicio=ma,
            unidad_fin=mb,
        )
        if era_cero:
            acum.ceros_sin_unidad -= 1
        else:
            acum.sin_unidad -= 1
        self._registrar_monto(acum, propio)

    def _fila_montos(self, unidad: _Unidad, indice_celda: int) -> _NumerosDeFila:
        """Clasifica las demás celdas de la fila. Una celda cuenta como monto si ES un monto (moneda o UIT) o si es una
        cifra pura bajo una columna de importes cuyo encabezado nombra UNA moneda; un número suelto sin moneda se cuenta
        aparte porque su unidad no consta. Un año en una columna que no es de montos se ignora."""
        columnas = self._columnas_de_fila(unidad)
        acum = _AcumuladorDeFila()
        for i, (a, b) in enumerate(unidad.celdas):
            if i != indice_celda and b > a:
                self._clasificar_celda(unidad, i, a, b, columnas[i], acum)
        self._aplicar_columna_de_moneda(acum)
        return _NumerosDeFila(
            acum.montos,
            acum.sin_unidad,
            acum.ceros_con_unidad,
            acum.ceros_sin_unidad,
            acum.desconocido,
            solo_saldo=acum.saldos_con_moneda > 0 and not acum.montos,
        )

    def _completar_con_fila(
        self, unidad: _Unidad, indice_celda: int, base: list[str], monto: MontoSancion | None
    ) -> tuple[MontoSancion | None, list[str], list[str]] | None:
        """(monto, base, motivos) tras mirar las demás celdas de la fila; None si la fila descarta el hallazgo
        (el descarte ya queda registrado)."""
        numeros = self._fila_montos(unidad, indice_celda)
        if len(numeros.montos) == 1:
            return numeros.montos[0], [*base, BASE_FILA_DE_TABLA], []
        if len(numeros.montos) > 1:
            return monto, [*base, BASE_FILA_DE_TABLA], [MOTIVO_MONTOS_MULTIPLES]
        if numeros.desconocido:
            # La columna de importes dice expresamente que no hay cifra: sanción no cuantificada (nunca cero).
            return monto, [*base, BASE_FILA_DE_TABLA], [MOTIVO_MONTO_NO_ESPECIFICADO]
        if numeros.solo_saldo:
            return monto, base, [MOTIVO_SOLO_SALDO]
        if numeros.sin_unidad or numeros.ceros_sin_unidad:
            # Hay cifras pero no se puede asociar con seguridad un monto con su unidad. Si el
            # término ya tiene evidencia propia («Multa impuesta por OEFA») la sanción se conserva
            # con monto nulo y advertencia: un cero (p. ej. un saldo) no la descarta. Sin
            # evidencia propia, un total en cero («Monto total por multas | S/ | 0») no es hallazgo.
            if base:
                return monto, base, [MOTIVO_MONTO_NO_ASOCIADO]
            if not numeros.sin_unidad:
                self._descartar(DESCARTE_MONTO_CERO)
                return None
        elif numeros.ceros_con_unidad:
            # Cero monetario explícito («S/ 0») y ninguna otra cifra: no hay multa.
            self._descartar(DESCARTE_MONTO_CERO)
            return None
        return monto, base, []

    def _fila_expresa_un_importe(self, unidad: _Unidad, indice_celda: int) -> bool:
        """¿La fila dice algo sobre el importe (una cifra con moneda, un cero monetario o «No especificado»)? Es lo que
        da carácter económico a una «sanción» genérica de una tabla."""
        numeros = self._fila_montos(unidad, indice_celda)
        return bool(numeros.montos or numeros.desconocido or numeros.ceros_con_unidad)

    # Importe desconocido -------------------------------------------------------------------------------------------------

    def _enunciado_de_importe_desconocido(self, inicio: int, fin: int) -> bool:
        """La oración dice que el importe no consta («no se especifica el monto», «importe pendiente de determinación»)."""
        return any(
            _OBJETO_AJENO_AL_IMPORTE.search(m.group()) is None
            for m in _MONTO_DESCONOCIDO.finditer(self.texto, inicio, fin)
        )

    def _importe_desconocido(
        self, inicio: int, fin: int, siguiente: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        """Oración (la propia o la siguiente del mismo párrafo, si no tiene término de sanción propio) donde el documento
        dice que el importe no consta; None si no lo dice."""
        if self._enunciado_de_importe_desconocido(inicio, fin):
            return (inicio, fin)
        if siguiente is not None and not self._terminos(*siguiente) and self._enunciado_de_importe_desconocido(*siguiente):
            return siguiente
        return None

    def _anexar_anaforico(self, inicio: int, fin: int, borradores_unidad: list[_Borrador]) -> None:
        """«Esta multa…» sin término propio: amplía la cita del hallazgo anterior de la unidad."""
        if not borradores_unidad:
            self._descartar(DESCARTE_ANAFORA)
            return
        anterior = borradores_unidad[-1]
        if (inicio, fin) not in anterior.oraciones:
            anterior.oraciones.append((inicio, fin))

    def _tiene_caracter_economico(
        self, termino: _Termino, monto: MontoSancion | None, enunciado: tuple[int, int] | None, unidad: _Unidad, indice_celda: int
    ) -> bool:
        """«multa» y «sanción económica» lo tienen por su palabra; una «sanción» genérica solo si hay un monto unido, si el
        documento dice algo sobre su importe o, en una tabla, si la fila expresa un importe."""
        if termino.tipo != TERMINO_SANCION_CON_MONTO or monto is not None or enunciado is not None:
            return True
        return unidad.tipo == "fila" and self._fila_expresa_un_importe(unidad, indice_celda)

    def _se_descarta(self, termino: _Termino, clausula: tuple[int, int], monto: MontoSancion | None) -> bool:
        """Registra el descarte y devuelve True si el término está en un contexto excluido o su monto es cero."""
        motivo = self._motivo_de_exclusion(termino, clausula)
        if motivo is None and monto is not None and self._es_cero(monto.numero_texto):
            motivo = DESCARTE_MONTO_CERO
        if motivo is None:
            return False
        self._descartar(motivo)
        return True

    def _candidato(
        self,
        termino: _Termino,
        clausula: tuple[int, int],
        unidad: _Unidad,
        indice_celda: int,
        oracion: tuple[int, int],
        siguiente: tuple[int, int] | None,
    ) -> tuple[MontoSancion | None, list[str], list[str], tuple[int, int] | None] | None:
        """(monto, base, motivos, oración del enunciado de importe desconocido) del término, o None si se descarta (o no es
        hallazgo ni descarte)."""
        monto = self._monto_vinculado(termino, clausula)
        enunciado = None if monto is not None else self._importe_desconocido(*oracion, siguiente)
        if not self._tiene_caracter_economico(termino, monto, enunciado, unidad, indice_celda):
            return None  # «sanción» sin carácter económico explícito: no es un hallazgo ni un descarte
        if self._se_descarta(termino, clausula, monto):
            return None
        base = self._evidencia(termino, clausula, monto)
        motivos = [] if enunciado is None else [MOTIVO_MONTO_NO_ESPECIFICADO]
        if unidad.tipo == "fila" and monto is None:
            completado = self._completar_con_fila(unidad, indice_celda, base, monto)
            if completado is None:
                return None
            monto, base, motivos_de_fila = completado
            motivos += motivos_de_fila
        elif monto is None and self._menciona_solo_un_saldo(termino, clausula):
            motivos.append(MOTIVO_SOLO_SALDO)
        if not base:
            self._descartar(DESCARTE_SIN_EVIDENCIA)
            return None
        if monto is None and termino.tipo == TERMINO_SANCION_CON_MONTO:
            base.append(BASE_CARACTER_ECONOMICO)  # lo expresa el contexto, no un adjetivo ni un importe
        return monto, base, motivos, enunciado

    def _procesar_oracion(
        self,
        unidad: _Unidad,
        inicio: int,
        fin: int,
        indice_celda: int,
        borradores_unidad: list[_Borrador],
        siguiente: tuple[int, int] | None = None,
    ) -> None:
        terminos = self._terminos(inicio, fin)
        if not terminos:
            return
        clausulas = self._clausulas(inicio, fin)
        en_tabla = unidad.tipo in ("fila", "celda")
        de_esta_oracion: list[_Borrador] = []
        for termino in terminos:
            clausula = next(c for c in clausulas if c[0] <= termino.inicio <= c[1])
            if termino.anaforico:
                self._anexar_anaforico(inicio, fin, borradores_unidad)
                continue
            candidato = self._candidato(termino, clausula, unidad, indice_celda, (inicio, fin), siguiente)
            if candidato is None:
                continue
            monto, base, motivos, enunciado = candidato
            if de_esta_oracion and monto is None:
                # Segundo término de la misma oración sin monto propio: reitera al anterior.
                de_esta_oracion[-1].adicionales.append(termino)
                continue
            oraciones = [(inicio, fin)]
            if enunciado is not None and enunciado != (inicio, fin):
                oraciones.append(enunciado)  # la oración que dice que el importe no consta forma parte de la cita
            borrador = _Borrador(unidad, termino, clausula, oraciones, base, monto, en_tabla, motivos, indice_celda=indice_celda)
            de_esta_oracion.append(borrador)
            borradores_unidad.append(borrador)

    def ejecutar(self) -> None:
        for unidad in _unidades(self.texto):
            celdas = unidad.celdas if unidad.tipo == "fila" else ((unidad.inicio, unidad.fin),)
            borradores: list[_Borrador] = []
            for indice, (a, b) in enumerate(celdas):
                oraciones = _oraciones(self.texto, a, b)
                for k, (inicio, fin) in enumerate(oraciones):
                    siguiente = oraciones[k + 1] if k + 1 < len(oraciones) else None
                    self._procesar_oracion(unidad, inicio, fin, indice, borradores, siguiente)
            self.sanciones.extend(self._finalizar(b) for b in borradores)


# --- Vuelta al texto original -------------------------------------------------------------------------------------

def _a_original(sancion: SancionDetectada, vista: VistaPlana) -> SancionDetectada:
    """Reexpresa un hallazgo calculado sobre la vista de lectura en posiciones y textos del ORIGINAL."""
    original = vista.original

    def tramo(inicio: int, fin: int) -> tuple[int, int]:
        return vista.inicio_en_original(inicio), vista.fin_en_original(fin)

    def dato(d: Dato | None) -> Dato | None:
        if d is None:
            return None
        a, b = tramo(d.inicio, d.fin)
        return Dato(original[a:b], a, b)

    monto = sancion.monto
    if monto is not None:
        a, b = tramo(monto.inicio, monto.fin)
        unidad, ua, ub = monto.unidad, None, None
        if monto.unidad_inicio is not None:
            ua, ub = tramo(monto.unidad_inicio, monto.unidad_fin)
            unidad = original[ua:ub]
        monto = replace(monto, texto=original[a:b], inicio=a, fin=b, unidad=unidad, unidad_inicio=ua, unidad_fin=ub)
    a, b = tramo(sancion.inicio, sancion.fin)
    cita_inicio = vista.inicio_con_marcas(sancion.cita_inicio)
    cita_fin = vista.fin_con_marcas(sancion.cita_fin)
    calificadores = []
    for c in sancion.calificadores:
        x, y = tramo(c.inicio, c.fin)
        calificadores.append(Calificador(c.codigo, original[x:y], x, y))
    return replace(
        sancion,
        referencia_original=original[a:b],
        inicio=a,
        fin=b,
        cita=original[cita_inicio:cita_fin],
        cita_inicio=cita_inicio,
        cita_fin=cita_fin,
        entidad=dato(sancion.entidad),
        monto=monto,
        periodo=dato(sancion.periodo),
        fechas_en_contexto=tuple(dato(f) for f in sancion.fechas_en_contexto),
        calificadores=tuple(calificadores),
    )


def detectar_sanciones(texto: str) -> ResultadoSanciones:
    """Detecta las sanciones económicas explícitas de `texto`. Determinista. Las posiciones y los textos devueltos son del
    texto ORIGINAL recibido, aunque el análisis se haga sobre su vista de lectura sin marcas de Markdown."""
    if not isinstance(texto, str):
        raise TypeError(f"El texto debe ser str, no {type(texto).__name__}.")
    vista = construir_vista(texto)
    analizador = _Analizador(vista.plano)
    analizador.ejecutar()
    sanciones = analizador.sanciones
    if not vista.es_identidad:
        sanciones = [_a_original(s, vista) for s in sanciones]
    return ResultadoSanciones(
        sanciones=tuple(sorted(sanciones, key=lambda s: s.inicio)),
        descartes=tuple(sorted(analizador.descartes.items())),
    )

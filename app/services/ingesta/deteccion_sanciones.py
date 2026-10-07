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
- «sanción» o «sancionado» sin carácter económico explícito (adjetivo económico o
  monto vinculado).
Es una heurística léxica: no se afirma precisión validada. Revisión humana pendiente.

DATOS EXPLÍCITOS, NUNCA INFERIDOS (los ausentes quedan en None)
- `entidad`: solo si el texto la une a la sanción («impuestas por el OEFA», «multa del
  OEFA», «la SUNAT multó»). Dos entidades distintas ligadas ⇒ ninguna + motivo.
- `monto`: solo si el texto lo une al término («multa equivalente a 59.078 UIT»,
  «multa de S/ 5,000», «S/ 3,000 de multa», «nos multó con…») o si una fila bien
  formada de tabla lo da en una sola celda. NO se vincula un monto cercano sin esa
  relación. Se conserva el texto original: no se convierte UIT a soles, no se
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

LÍMITES CONOCIDOS
- Solo español. Términos: multa(s), sanción(es) económica/pecuniaria/monetaria y
  «sanción/sancionado» con monto vinculado. Penalidades contractuales no se cuentan.
- Montos escritos en palabras («dos millones de soles») no se reconocen.
- Filas de tabla con solo un conteo («Número de multas | 2») no se detectan.
- Las filas con distinto número de columnas que el encabezado no se interpretan por
  columnas: cada celda se analiza sola.
- Las referencias dentro de bloques de código se ignoran.
- Rendimiento con 50 MB sin medir.
"""
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.services.ingesta import markdown

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

DESCARTE_NEGACION = "negacion"
DESCARTE_HIPOTETICA = "hipotetica_o_normativa"
DESCARTE_DISCIPLINARIA = "disciplinaria_o_laboral_interna"
DESCARTE_MONTO_CERO = "monto_cero"
DESCARTE_SIN_EVIDENCIA = "sin_evidencia_de_imposicion"
DESCARTE_ANAFORA = "anaforica_sin_antecedente"

_I = re.IGNORECASE

# --- Términos --------------------------------------------------------------------------
_MULTA = re.compile(r"\b(?:multas?|multad[oa]s?|mult[óo]|multaron|multamos)\b", _I)
_SANCION_ECONOMICA = re.compile(
    r"\bsanci(?:[óo]n|ones)\s+(?:econ[óo]micas?|pecuniarias?|monetarias?)\b", _I
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
    r"^\s*(?:[:=]|\()?\s*(?:0(?:[.,]0+)?|cero|ninguna?|ning[uú]n)\b", _I
)
_HIPOTETICA_CLAUSULA = re.compile(
    r"\b(?:en\s+caso\s+de|de\s+(?:ser|incumplir|no\s+cumplir|infringir)|riesgos?|eventual(?:es)?|"
    r"potencial(?:es)?|posibles?|hasta|m[aá]xim[ao]s?|m[ií]nim[ao]s?|sujet[oa]s?\s+a|"
    r"expuest[oa]s?|exposici[óo]n|prev[eé]n?|establece[n]?|contempla[n]?|tipifica[n]?|"
    r"r[eé]gimen\s+sancionador|se\s+aplicar[aá]n?|ser[aá]n?\s+(?:multad|sancionad)\w+|"
    r"para\s+(?:evitar|prevenir)|con\s+el\s+fin\s+de\s+evitar|a\s+fin\s+de\s+evitar|"
    r"evitar|prevenci[óo]n\s+de|reglamento\s+de\s+sanciones|puede[n]?\s+(?:ser\s+)?(?:multad|sancionad|imponer)\w*)\b",
    _I,
)
_HIPOTETICA_SI = re.compile(r"(?:^|\s)si\s+(?:se\s+|no\s+|la\s+|el\s+)?(?:incumpl|cumpl|infring|comet|produc|detect)", _I)
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
    r"multó|multaron"
)
_PARTICIPIOS_FEMENINOS = (
    r"(?:impuestas?|aplicadas?|determinadas?|notificadas?|pagadas?|canceladas?|abonadas?|registradas?|reportadas?|"
    r"recibidas?|sancionadas?)"
)
_AUXILIAR_Y_PARTICIPIO = (
    r"(?:ha|han|hemos|he|había|habían)\s+(?:impuesto|aplicado|determinado|recibido|pagado|cancelado|abonado|"
    r"notificado|registrado|reportado)"
)
_EVIDENCIA_ANTES = re.compile(
    rf"\b(?:{_FINITOS_IMPOSICION}|{_AUXILIAR_Y_PARTICIPIO}|{_PARTICIPIOS_FEMENINOS})\b", _I
)
_EVIDENCIA_DESPUES = re.compile(
    rf"\b(?:{_PARTICIPIOS_FEMENINOS}|(?:que|se)\s+(?:se\s+)?(?:nos\s+|le\s+|les\s+)?(?:{_FINITOS_IMPOSICION}))\b", _I
)
_TRAMITE_VINCULADO = re.compile(
    r"\b(?:impugn\w+|apel(?:ó|ación|adas?|aron|amos)|revoc\w+|anul\w+|confirm(?:ó|aron|adas?|ación))\b", _I
)
# Si entre el verbo y el término hay uno de estos objetos, el verbo se refiere a ellos, no a la multa
# («pagó los impuestos asociados a la multa»).
_OBJETO_AJENO = re.compile(
    r"\b(?:impuestos?|tributos?|proveedor\w*|planillas?|salarios?|sueldos?|dividendos?|facturas?|intereses|"
    r"contratistas?|servicios|deudas?|bonos?)\b",
    _I,
)
_RELATIVO = r"(?!\s*(?:que|la\s+cual|las\s+cuales|el\s+cual)\b)"
_SEPARADOR_DE_ALCANCE = re.compile(r"(?<!\d),|,(?!\d)|;|\b(?:y|e|pero|sino|aunque|ni)\b", _I)
# Una conjunción no corta el alcance si continúa el mismo predicado: «la multa ... y está pendiente de pago».
_FIN_DE_ALCANCE = re.compile(
    rf"(?<!\d),{_RELATIVO}|,(?!\d){_RELATIVO}|;|\s(?:y|e|pero|sino|aunque|ni)\s(?!(?:está|esta|queda|sigue|permanece|continúa|continua|"
    r"se\s+encuentra|será|fue|fueron|ha\s+sido|no|luego|posteriormente|después|también|además|ya|aún|todavía)\b)",
    _I,
)
PALABRAS_ANTES_DEL_TERMINO = 6
PALABRAS_DESPUES_DEL_TERMINO = 12

# --- Calificadores (literales) -------------------------------------------------------------------
_CALIFICADORES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CAL_PAGADA, re.compile(
        r"\b(?:pagad[oa]s?|pag(?:ó|aron|amos)|cancelad[oa]s?|cancel(?:ó|aron)|abonad[oa]s?|abon(?:ó|aron)|honrad[oa]s?)\b", _I)),
    (CAL_PENDIENTE, re.compile(
        r"\b(?:impag[ao]s?|sin\s+pagar|por\s+pagar|pendientes?\s+de\s+pago|pago\s+pendiente|adeudad[ao]s?|"
        r"obligaciones?\s+de\s+pago\s+pendientes?)\b", _I)),
    (CAL_IMPUGNADA, re.compile(
        r"\b(?:impugn\w+|apel(?:[óo]|aci[óo]n|ad[oa]s?|aron|amos)|recurso\s+de\s+(?:apelaci[óo]n|reconsideraci[óo]n|revisi[óo]n)|"
        r"contencioso[- ]administrativ\w+|demanda\s+de\s+nulidad)\b", _I)),
    (CAL_HISTORICA, re.compile(
        r"\b(?:hist[óo]ric[ao]s?|(?:a[ñn]os|ejercicios|per[ií]odos|periodos)\s+anteriores|"
        r"de\s+(?:un\s+)?(?:a[ñn]o|ejercicio)\s+anterior|antigu[ao]s?)\b", _I)),
    (CAL_REVOCADA, re.compile(
        r"\b(?:revoc(?:[óo]|aron|ad[ao]s?|aci[óo]n)|anul(?:[óo]|aron|ad[ao]s?|aci[óo]n)|dej(?:[óo]|ada|aron)\s+sin\s+efecto)\b", _I)),
    (CAL_CONFIRMADA, re.compile(r"\bconfirm(?:[óo]|aron|ad[oa]s?|aci[óo]n)\b", _I)),
)
# Un calificador precedido de estas negaciones o hipótesis no se registra.
_NEGA_CALIFICADOR = re.compile(
    r"\b(?:no|nunca|jam[aá]s|ning[uú]n[ao]?s?|sin|ni|podr[ií]a[n]?|podr[aá]n?|pudiera|podr[ií]amos)\b(?:(?!\b(?:y|e|pero|aunque)\b)[^,.;]){0,40}$", _I
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
_CONECTOR_MONTO = (
    r"(?:\s+(?:econ[óo]micas?|administrativas?|pecuniarias?|monetarias?|impuest[ao]s?|total(?:es)?|adicional(?:es)?|"
    r"hist[óo]ric[ao]s?|pagad[ao]s?|impugnad[ao]s?|anterior(?:es)?|[úu]nic[ao]s?|ambiental(?:es)?|laboral(?:es)?|"
    r"tributari[ao]s?|fiscal(?:es)?))*"
    r"(?:\s+(?:ascendente|equivalente|valorizad[ao]|que\s+asciende|que\s+ascendi[óo])\s+a"
    r"|\s+correspondiente\s+a"
    r"|\s+(?:ascendi[óo]|asciende|fue|fueron|sum[óo])\s+(?:a|de|por)"
    r"|\s+(?:de|por|con)(?:\s+(?:un|el|la))?(?:\s+(?:monto|importe|total|valor|suma))?(?:\s+(?:total|de))?"
    r"|\s+total(?:izando)?(?:\s+de)?)\s+"
)
_ENLACE_MONTO = re.compile(_CONECTOR_MONTO, _I)
_VERBO_CON_MONTO = re.compile(
    r"(?:\s+[^\s,.;:()|]+){0,8}?\s+(?:con|por)(?:\s+(?:un|el|la)(?:\s+(?:monto|importe)\s+de)?)?\s+", _I
)
_MONTO_ANTES = re.compile(
    rf"(?:(?:{_PREFIJO_MONEDA})\s*{_NUMERO}{_ESCALA}|{_NUMERO}(?:\s*(?:mil|millones?(?:\s+de)?))?\s*{_UNIDAD_SUFIJO}\b)"
    r"\s+(?:de|en|por|como)\s+(?P<termino>multas?|sanci(?:[óo]n|ones)(?:\s+econ[óo]micas?)?)\b",
    _I,
)
_SOLO_MONTO = (_MONTO_PREFIJO, _MONTO_SUFIJO)
_CERO = re.compile(r"^\W*(?:0+(?:[.,]0+)?|cero|ninguna?|ning[uú]n|-+|—+|–+)\W*$", _I)
_CERO_SUELTO = re.compile(r"[\W_]*(?:0+(?:[.,]0+)?|cero)[\W_]*", _I)
_NUMERO_SUELTO = re.compile(r"[\W_]*\d[\d.,'’   ]*[\W_]*")
_ANIO_SUELTO = re.compile(r"(?:19|20)\d{2}")
_ENCABEZADO_DE_MONTO = re.compile(r"monto|importe|valor|total|multa|sanci|s/|soles|uit|usd|us\$", _I)
_SEPARADOR_FINAL = re.compile(r"[.,'’  ](\d+)$")

# --- Entidades explícitas -------------------------------------------------------------------------------
_SIGLAS = (
    r"(?:OEFA|SUNAFIL|SUNAT|OSINERGMIN|OSINERG|ANA|INDECOPI|SMV|SBS|SENACE|SUNASS|OSITRAN|SUCAMEC|SERFOR|"
    r"SERNANP|MINEM|MTPE|MINAM|DIGESA|SUTRAN|SENASA|OSCE|ANLA|SEC|EPA|IBAMA)"
)
_NOMBRE_ENTIDAD = (
    r"(?:(?:Organismo|Autoridad|Superintendencia|Ministerio|Instituto|Tribunal|Direcci[oó]n|Gobierno|Municipalidad|"
    r"Agencia|Comisi[oó]n|Servicio|Oficina|Junta|Consejo)\b"
    r"(?:\s+(?:de|del|de\s+la|de\s+los|de\s+las|para|y|e|en)\b)?"
    r"(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñÁÉÍÓÚÑ]*(?:\s+(?:de|del|de\s+la|y)\b(?=\s+[A-ZÁÉÍÓÚÑ]))?){0,6})"
)
_ENTIDAD = rf"(?P<ent>{_NOMBRE_ENTIDAD}(?:\s*\(\s*{_SIGLAS}\s*\))?|{_SIGLAS})"
_ARTICULO = r"(?:(?i:el|la|los|las)\s+)?"
# «multa impuesta por el OEFA de S/ 1000»: la entidad explícita forma parte de la misma frase nominal.
_ENTIDAD_SIN_GRUPO = _ENTIDAD.replace("(?P<ent>", "(?:", 1)
_ENLACE_MONTO = re.compile(
    r"(?:\s+(?:impuest[ao]s?|aplicad[ao]s?)\s+por\s+(?:parte\s+de\s+)?(?:(?:el|la|los|las)\s+)?"
    rf"(?-i:{_ENTIDAD_SIN_GRUPO}))?{_CONECTOR_MONTO}",
    _I,
)
_ENTIDAD_POR = re.compile(
    r"(?i:impuest[ao]s?|aplicad[ao]s?|determinad[ao]s?|multad[ao]s?|sancionad[ao]s?|emitid[ao]s?|dictad[ao]s?|notificad[ao]s?)"
    rf"\s+(?i:por)\s+(?:(?i:parte\s+de)\s+)?{_ARTICULO}{_ENTIDAD}"
)
_ENTIDAD_TRAS_TERMINO = re.compile(
    r"(?i:multas?|sanci(?:[óo]n|ones)(?:\s+econ[óo]micas?)?)\s+(?:(?i:de|del|de\s+la|por\s+parte\s+de)\s+)"
    rf"{_ARTICULO}{_ENTIDAD}"
)
# «multas del OEFA y de la SUNAFIL»: las entidades coordinadas también quedan ligadas al término.
_ENTIDAD_COORDINADA = re.compile(
    rf"\s*(?:,|(?i:y|e|o))\s*(?:(?i:de|del|de\s+la|por\s+parte\s+de)\s+)?{_ARTICULO}{_ENTIDAD}"
)
_ENTIDAD_ACTIVA = re.compile(
    rf"{_ENTIDAD}\s+(?:(?i:nos|le|les|la|lo)\s+)?(?i:impuso|impusieron|mult[óo]|multaron|sancion[óo]|sancionaron|aplic[óo]|aplicaron)"
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
    # Texto de los encabezados de la tabla (solo en filas con el número de columnas del encabezado).
    encabezados: tuple[str, ...] = ()


def _unidades(texto: str) -> Iterator[_Unidad]:
    """Párrafos, filas de tabla (si tienen el número de columnas del encabezado) y
    celdas sueltas (si no). Omite encabezados, bloques de código y la cabecera de
    las tablas."""
    longitud = len(texto)
    pila: list[tuple[int, str]] = []
    cerca: str | None = None
    parrafo: list[int] | None = None  # [inicio, fin]
    n_columnas: int | None = None
    encabezados: tuple[str, ...] = ()
    delimitador_pendiente = False
    posicion = 0

    def seccion() -> str:
        return SEPARADOR_RUTA.join(t for _, t in pila if t)

    while posicion < longitud:
        fin_linea, siguiente = markdown.limites_de_linea(texto, posicion)
        cerca, es_codigo = markdown.actualizar_cerca(texto, cerca, posicion, fin_linea)
        pendiente = None
        if es_codigo:
            n_columnas = None
            pendiente = "cortar"
        elif (titulo := markdown.titulo_atx(texto, posicion, fin_linea)) is not None:
            n_columnas = None
            pendiente = "cortar"
            if parrafo is not None:
                yield _Unidad(parrafo[0], parrafo[1], seccion(), "parrafo")
                parrafo = None
            nivel, texto_titulo = titulo
            while pila and pila[-1][0] >= nivel:
                pila.pop()
            pila.append((nivel, texto_titulo))
        if pendiente == "cortar":
            if parrafo is not None:
                yield _Unidad(parrafo[0], parrafo[1], seccion(), "parrafo")
                parrafo = None
            posicion = siguiente
            continue

        if delimitador_pendiente:
            delimitador_pendiente = False
            posicion = siguiente
            continue

        if n_columnas is not None:
            if markdown.puede_ser_fila(texto, posicion, fin_linea):
                celdas = markdown.rangos_de_celdas(texto, posicion, fin_linea)
                if len(celdas) == n_columnas:
                    yield _Unidad(posicion, fin_linea, seccion(), "fila", tuple(celdas), encabezados)
                else:
                    for a, b in celdas:
                        if b > a:
                            yield _Unidad(a, b, seccion(), "celda")
                posicion = siguiente
                continue
            n_columnas = None

        if markdown.es_blanca(texto, posicion, fin_linea):
            if parrafo is not None:
                yield _Unidad(parrafo[0], parrafo[1], seccion(), "parrafo")
                parrafo = None
            posicion = siguiente
            continue

        if markdown.tiene_pipe(texto, posicion, fin_linea) and siguiente < longitud:
            fin_sig, _ = markdown.limites_de_linea(texto, siguiente)
            if markdown.es_inicio_tabla(texto[posicion:fin_linea], texto[siguiente:fin_sig]):
                if parrafo is not None:
                    yield _Unidad(parrafo[0], parrafo[1], seccion(), "parrafo")
                    parrafo = None
                rangos = markdown.rangos_de_celdas(texto, posicion, fin_linea)
                n_columnas = len(rangos)
                encabezados = tuple(texto[a:b] for a, b in rangos)
                delimitador_pendiente = True
                posicion = siguiente
                continue

        inicio_contenido = posicion + len(texto[posicion:fin_linea]) - len(texto[posicion:fin_linea].lstrip())
        if parrafo is not None and _MARCA_DE_LISTA.match(texto, posicion, fin_linea):
            yield _Unidad(parrafo[0], parrafo[1], seccion(), "parrafo")
            parrafo = None
        if parrafo is None:
            parrafo = [inicio_contenido, fin_linea]
        else:
            parrafo[1] = fin_linea
        posicion = siguiente
    if parrafo is not None:
        yield _Unidad(parrafo[0], parrafo[1], seccion(), "parrafo")


def _oraciones(texto: str, inicio: int, fin: int) -> list[tuple[int, int]]:
    """Oraciones de [inicio, fin): corta tras . ! ? y espacio, salvo abreviaturas,
    iniciales, «S/.» y cuando lo que sigue empieza en minúscula."""
    cortes = [inicio]
    for encontrado in _FIN_ORACION.finditer(texto, inicio, fin):
        siguiente = encontrado.end()
        if siguiente >= fin:
            continue
        puntuacion = texto[encontrado.start():encontrado.end()].strip()
        if puntuacion.startswith("."):
            palabra = re.search(r"([^\W\d_]+|\S)$", texto[max(inicio, encontrado.start() - 12):encontrado.start()])
            token = palabra.group(1) if palabra else ""
            if token and (
                token.lower() in _ABREVIATURAS or (len(token) == 1 and (token.isalpha() or token == "/"))
            ):  # abreviatura, inicial («S.A.») o «S/.»; un dígito o «»)» sí cierran la oración
                continue
            if texto[siguiente].islower():
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

    montos: list[MontoSancion]  # con unidad y distintos de cero
    sin_unidad: int  # cifras distintas de cero sin moneda ni UIT
    ceros_con_unidad: int  # «S/ 0»
    ceros_sin_unidad: int  # «0»


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


class _Analizador:
    def __init__(self, texto: str) -> None:
        self.texto = texto
        self.sanciones: list[SancionDetectada] = []
        self.descartes: dict[str, int] = {}

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
        texto = self.texto
        limite = clausula[1]
        enlace = _ENLACE_MONTO.match(texto, termino.fin, limite)
        if enlace is not None:
            monto = self._monto_en(enlace.end(), limite, "termino_y_conector")
            if monto is not None:
                return monto
        # «nos multó con S/ 10», «fuimos multados por 5 UIT»
        if re.fullmatch(r"(?:mult|sancion)(?:[óo]|aron|amos|ad[oa]s?)", texto[termino.inicio:termino.fin], _I):
            verbo = _VERBO_CON_MONTO.match(texto, termino.fin, limite)
            if verbo is not None:
                monto = self._monto_en(verbo.end(), limite, "verbo")
                if monto is not None:
                    return monto
        # «S/ 3,000 de multa»
        for antes in _MONTO_ANTES.finditer(texto, clausula[0], limite):
            if antes.start("termino") == termino.inicio:
                monto = None
                for patron in _SOLO_MONTO:
                    encontrado = patron.match(texto, antes.start(), antes.end())
                    if encontrado is not None:
                        monto = self._monto_en(antes.start(), antes.end(), "monto_antes_del_termino")
                        break
                if monto is not None:
                    return monto
        return None

    # Entidad, periodo y calificadores -------------------------------------------------------------

    def _entidad(self, clausula: tuple[int, int]) -> tuple[Dato | None, bool]:
        texto = self.texto
        encontradas: dict[str, Dato] = {}
        for patron in (_ENTIDAD_POR, _ENTIDAD_TRAS_TERMINO, _ENTIDAD_ACTIVA):
            for m in patron.finditer(texto, clausula[0], clausula[1]):
                coincidencias = [m]
                if patron is not _ENTIDAD_ACTIVA:
                    while (c := _ENTIDAD_COORDINADA.match(texto, coincidencias[-1].end(), clausula[1])) is not None:
                        coincidencias.append(c)
                for c in coincidencias:
                    clave = re.sub(r"\s+", " ", c.group("ent"))
                    encontradas.setdefault(clave, Dato(c.group("ent"), c.start("ent"), c.end("ent")))
        if len(encontradas) == 1:
            return next(iter(encontradas.values())), False
        return None, len(encontradas) > 1

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
        self, termino: _Termino, clausula: tuple[int, int]
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
        if len(palabras) > PALABRAS_DESPUES_DEL_TERMINO:
            fin = termino.fin + palabras[PALABRAS_DESPUES_DEL_TERMINO - 1]
        return (inicio, termino.inicio), (termino.fin, fin)

    def _buscar_vinculado(
        self,
        patron: re.Pattern[str],
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
                if _NEGA_CALIFICADOR.search(previo):
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

    def _finalizar(self, b: _Borrador) -> SancionDetectada:
        texto = self.texto
        termino = b.termino
        calificadores = self._calificadores(b.oraciones)
        entidad, ambigua = self._entidad(b.clausula)
        motivos = list(b.motivos)
        if ambigua:
            motivos.append(MOTIVO_ENTIDAD_AMBIGUA)
        if b.monto is not None and b.monto.separador_ambiguo:
            motivos.append(MOTIVO_SEPARADOR_AMBIGUO)
        if b.base == [BASE_VERBO_TRAMITE]:
            motivos.append(MOTIVO_SOLO_TRAMITE)
        periodo = self._periodo(termino, b.monto, b.clausula, b.oraciones[0][0])
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
        fechas = []
        vistas = set()
        for m in _FECHA.finditer(texto, cita_inicio, cita_fin):
            if (m.start(), m.end()) not in vistas:
                vistas.add((m.start(), m.end()))
                fechas.append(Dato(m.group(), m.start(), m.end()))
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

    def _fila_montos(self, unidad: _Unidad, indice_celda: int) -> _NumerosDeFila:
        """Clasifica las demás celdas de la fila. Una celda cuenta como monto solo si ES un monto
        (moneda o UIT); un número suelto se cuenta aparte porque su unidad no consta. Un año en una
        columna que no es de montos se ignora."""
        texto = self.texto
        montos: list[MontoSancion] = []
        sin_unidad = ceros_con_unidad = ceros_sin_unidad = 0
        for i, (a, b) in enumerate(unidad.celdas):
            if i == indice_celda or b <= a:
                continue
            celda = texto[a:b]
            monto = None
            for patron in _SOLO_MONTO:
                if patron.fullmatch(texto, a, b) is not None:
                    monto = self._monto_en(a, b, BASE_FILA_DE_TABLA)
                    break
            if monto is not None:
                if self._es_cero(monto.numero_texto):
                    ceros_con_unidad += 1
                else:
                    montos.append(monto)
            elif _CERO_SUELTO.fullmatch(celda):
                ceros_sin_unidad += 1
            elif _NUMERO_SUELTO.fullmatch(celda):
                encabezado = unidad.encabezados[i] if i < len(unidad.encabezados) else ""
                es_anio = _ANIO_SUELTO.fullmatch(celda.strip()) is not None
                if not es_anio or _ENCABEZADO_DE_MONTO.search(encabezado):
                    sin_unidad += 1
        return _NumerosDeFila(montos, sin_unidad, ceros_con_unidad, ceros_sin_unidad)

    def _procesar_oracion(
        self,
        unidad: _Unidad,
        inicio: int,
        fin: int,
        indice_celda: int,
        borradores_unidad: list[_Borrador],
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
                if borradores_unidad:
                    anterior = borradores_unidad[-1]
                    if (inicio, fin) not in anterior.oraciones:
                        anterior.oraciones.append((inicio, fin))
                else:
                    self._descartar(DESCARTE_ANAFORA)
                continue
            if termino.tipo == TERMINO_SANCION_CON_MONTO and self._monto_vinculado(termino, clausula) is None:
                continue  # «sanción» sin carácter económico explícito: no es un hallazgo ni un descarte
            motivo = self._motivo_de_exclusion(termino, clausula)
            if motivo is not None:
                self._descartar(motivo)
                continue
            monto = self._monto_vinculado(termino, clausula)
            if monto is not None and self._es_cero(monto.numero_texto):
                self._descartar(DESCARTE_MONTO_CERO)
                continue
            base = self._evidencia(termino, clausula, monto)
            motivos: list[str] = []
            if unidad.tipo == "fila" and monto is None:
                numeros = self._fila_montos(unidad, indice_celda)
                if len(numeros.montos) == 1:
                    monto, base = numeros.montos[0], [*base, BASE_FILA_DE_TABLA]
                elif len(numeros.montos) > 1:
                    base, motivos = [*base, BASE_FILA_DE_TABLA], [MOTIVO_MONTOS_MULTIPLES]
                elif numeros.sin_unidad or numeros.ceros_sin_unidad:
                    # Hay cifras pero no se puede asociar con seguridad un monto con su unidad. Si el
                    # término ya tiene evidencia propia («Multa impuesta por OEFA») la sanción se conserva
                    # con monto nulo y advertencia: un cero (p. ej. un saldo) no la descarta. Sin
                    # evidencia propia, un total en cero («Monto total por multas | S/ | 0») no es hallazgo.
                    if base:
                        motivos = [MOTIVO_MONTO_NO_ASOCIADO]
                    elif not numeros.sin_unidad:
                        self._descartar(DESCARTE_MONTO_CERO)
                        continue
                elif numeros.ceros_con_unidad:
                    # Cero monetario explícito («S/ 0») y ninguna otra cifra: no hay multa.
                    self._descartar(DESCARTE_MONTO_CERO)
                    continue
            if not base:
                self._descartar(DESCARTE_SIN_EVIDENCIA)
                continue
            if de_esta_oracion and monto is None:
                # Segundo término de la misma oración sin monto propio: reitera al anterior.
                de_esta_oracion[-1].adicionales.append(termino)
                continue
            borrador = _Borrador(unidad, termino, clausula, [(inicio, fin)], base, monto, en_tabla, motivos)
            de_esta_oracion.append(borrador)
            borradores_unidad.append(borrador)

    def ejecutar(self) -> None:
        for unidad in _unidades(self.texto):
            celdas = unidad.celdas if unidad.tipo == "fila" else ((unidad.inicio, unidad.fin),)
            borradores: list[_Borrador] = []
            for indice, (a, b) in enumerate(celdas):
                for inicio, fin in _oraciones(self.texto, a, b):
                    self._procesar_oracion(unidad, inicio, fin, indice, borradores)
            self.sanciones.extend(self._finalizar(b) for b in borradores)


def detectar_sanciones(texto: str) -> ResultadoSanciones:
    """Detecta las sanciones económicas explícitas de `texto`. Determinista."""
    if not isinstance(texto, str):
        raise TypeError(f"El texto debe ser str, no {type(texto).__name__}.")
    analizador = _Analizador(texto)
    analizador.ejecutar()
    return ResultadoSanciones(
        sanciones=tuple(sorted(analizador.sanciones, key=lambda s: s.inicio)),
        descartes=tuple(sorted(analizador.descartes.items())),
    )

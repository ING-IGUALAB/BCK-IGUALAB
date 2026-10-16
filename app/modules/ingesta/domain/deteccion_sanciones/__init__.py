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


from .analizador import (
    BASE_FILA_DE_TABLA,
    BASE_MONTO_VINCULADO,
    BASE_VERBO_IMPOSICION,
    BASE_VERBO_TRAMITE,
    CAL_CONFIRMADA,
    CAL_HISTORICA,
    CAL_IMPUGNADA,
    CAL_PAGADA,
    CAL_PENDIENTE,
    CAL_REVOCADA,
    DESCARTE_ANAFORA,
    DESCARTE_DISCIPLINARIA,
    DESCARTE_HIPOTETICA,
    DESCARTE_MONTO_CERO,
    DESCARTE_NEGACION,
    DESCARTE_SIN_EVIDENCIA,
    MARGEN_CITA,
    MAX_LONGITUD_CITA,
    MOTIVO_ENTIDAD_AMBIGUA,
    MOTIVO_MONTO_NO_ASOCIADO,
    MOTIVO_MONTOS_MULTIPLES,
    MOTIVO_SEPARADOR_AMBIGUO,
    MOTIVO_SOLO_TRAMITE,
    PALABRAS_ANTES_DEL_TERMINO,
    PALABRAS_DESPUES_DEL_TERMINO,
    TERMINO_MULTA,
    TERMINO_SANCION_CON_MONTO,
    TERMINO_SANCION_ECONOMICA,
    detectar_sanciones,
)
from .modelos import (
    Calificador,
    Dato,
    MontoSancion,
    ResultadoSanciones,
    SancionDetectada,
)
from .unidades import SEPARADOR_RUTA

__all__ = [
    "BASE_FILA_DE_TABLA",
    "BASE_MONTO_VINCULADO",
    "BASE_VERBO_IMPOSICION",
    "BASE_VERBO_TRAMITE",
    "CAL_CONFIRMADA",
    "CAL_HISTORICA",
    "CAL_IMPUGNADA",
    "CAL_PAGADA",
    "CAL_PENDIENTE",
    "CAL_REVOCADA",
    "DESCARTE_ANAFORA",
    "DESCARTE_DISCIPLINARIA",
    "DESCARTE_HIPOTETICA",
    "DESCARTE_MONTO_CERO",
    "DESCARTE_NEGACION",
    "DESCARTE_SIN_EVIDENCIA",
    "Calificador",
    "Dato",
    "MARGEN_CITA",
    "MAX_LONGITUD_CITA",
    "MOTIVO_ENTIDAD_AMBIGUA",
    "MOTIVO_MONTO_NO_ASOCIADO",
    "MOTIVO_MONTOS_MULTIPLES",
    "MOTIVO_SEPARADOR_AMBIGUO",
    "MOTIVO_SOLO_TRAMITE",
    "MontoSancion",
    "PALABRAS_ANTES_DEL_TERMINO",
    "PALABRAS_DESPUES_DEL_TERMINO",
    "ResultadoSanciones",
    "SEPARADOR_RUTA",
    "SancionDetectada",
    "TERMINO_MULTA",
    "TERMINO_SANCION_CON_MONTO",
    "TERMINO_SANCION_ECONOMICA",
    "detectar_sanciones",
]

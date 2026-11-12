"""Verificación de las respuestas del Asistente IA (LTX:RN-033/034/035; plan T21). Regla pura: sin BD, sin HTTP, sin LLM.

CONTRATO CON EL MODELO. El modelo devuelve un objeto JSON con una lista de AFIRMACIONES; cada una trae su `texto`, las
`referencias` (números de los fragmentos del contexto que la sustentan) y una `cita_textual` (pasaje copiado de uno de esos
fragmentos). Aquí se decide, afirmación por afirmación, si se conserva:

  una afirmación SE CONSERVA solo si (1) tiene texto, (2) tiene al menos una referencia, (3) TODAS sus referencias son números de
  fragmentos entregados (una referencia fuera de rango descarta la afirmación completa: no se «arregla» borrando el marcador),
  (4) su `cita_textual` aparece, tras normalizar mayúsculas, espacios y marcas de formato Markdown, en alguno de los fragmentos
  que cita y tiene al menos `LONGITUD_MINIMA_CITA` caracteres y `PALABRAS_MINIMAS_CITA` palabras, y (5) toda cifra del texto de la
  afirmación (salvo los años) figura en esa cita o en el encabezado de la sección citada.

Las REFERENCIAS que se conservan son solo las de los fragmentos que contienen la cita: un número válido cuyo fragmento no contiene
la cita no se presenta como respaldo (que «el índice existe» no lo prueba). Si no se conserva ninguna afirmación no hay respuesta: el
servicio responde «sin evidencia» y el MOTIVO dice por qué.

LÍMITES REALES. Esto demuestra que (a) cada fuente mostrada existe y contiene el pasaje citado, (b) no queda texto sin referencia y
(c) las cifras que afirma el texto están en la evidencia mostrada. NO demuestra que el pasaje implique lo que dice la afirmación:
un modelo puede citar un pasaje real pero irrelevante, o que la contradice (por negación, cambio de sujeto, año o alcance, o con
una paráfrasis sin cifras), y esta verificación no lo detecta; es una comprobación léxica, no comprensión del texto. Los años no se
comparan. Tampoco se puede verificar aquí el rechazo de consultas ajenas al dominio ni que el modelo ignore instrucciones incrustadas
en un fragmento: son conductas del modelo que el prompt pide y que esta verificación solo acota (una afirmación inducida por un
fragmento sigue necesitando una cita literal válida, y esa cita puede ser la propia frase inducida).
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from app.modules.ingesta.infrastructure.fragmentos_vectoriales import FragmentoRecuperado

LONGITUD_MINIMA_CITA = 20  # una cita más corta («la empresa») está en casi cualquier fragmento y no respalda nada
PALABRAS_MINIMAS_CITA = 3
MAXIMO_AFIRMACIONES = 20
MAXIMO_INTERPRETACION = 4_000  # caracteres de la interpretación (campo aparte de las afirmaciones)

MOTIVO_SIN_FRAGMENTOS = "SIN_FRAGMENTOS"
MOTIVO_MODELO_SIN_EVIDENCIA = "MODELO_SIN_EVIDENCIA"
MOTIVO_AFIRMACIONES_NO_VERIFICABLES = "AFIRMACIONES_NO_VERIFICABLES"
MOTIVO_RESPUESTA_NO_INTERPRETABLE = "RESPUESTA_NO_INTERPRETABLE"
MOTIVO_FUERA_DE_DOMINIO = "FUERA_DE_DOMINIO"

_MARCADOR = re.compile(r"\[\s*\d{1,3}\s*\]")
_CERCA = "```"
# Cifras: números con o sin separadores de miles/decimales («1 200», «1,200», «12,5», «305-1» son «1200», «1200», «125», «305» y «1»).
_CIFRA = re.compile(r"(?:\d{1,3}(?:[ .,]\d{3})+|\d+)(?:[.,]\d+)?")
_ANIO = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True)
class AfirmacionVerificada:
    texto: str
    referencias: tuple[int, ...]
    cita_textual: str


@dataclass(frozen=True)
class ResultadoVerificacion:
    afirmaciones: tuple[AfirmacionVerificada, ...]
    descartadas: int
    motivo: str | None  # None si hay al menos una afirmación verificada
    interpretacion: str | None = None  # solo en el modo con interpretación y con al menos una afirmación verificada
    interpretacion_descartada: bool = False  # el modelo envió una interpretación y no se muestra (ver `verificar_interpretacion`)


def normalizar(texto: str) -> str:
    """Forma comparable: NFKC, minúsculas, sin `*`, `` ` `` ni `|` (marcas de formato y celdas de tabla) y con espacios colapsados."""
    limpio = unicodedata.normalize("NFKC", texto).casefold()
    limpio = limpio.replace("|", " ")
    limpio = re.sub(r"[*`]", "", limpio)
    return " ".join(limpio.split())


def cifras(texto: str) -> set[str]:
    """Cifras de un texto comparables entre formatos (solo sus dígitos, sin separadores). Los años (1900-2099 sueltos) se omiten."""
    resultado: set[str] = set()
    for coincidencia in _CIFRA.finditer(unicodedata.normalize("NFKC", texto)):
        bruto = coincidencia.group()
        if not _ANIO.fullmatch(bruto):
            resultado.add(re.sub(r"\D", "", bruto))
    return resultado


def _sin_cerca_json(candidato: str) -> str:
    """El contenido de una cerca ``` (con `json` opcional tras la apertura); sin cerca completa, el texto tal cual. Sin regex:
    dos cuantificadores de espacios alrededor de un cuerpo libre retroceden de forma cuadrática ante un texto largo con muchos espacios."""
    if len(candidato) < 2 * len(_CERCA) or not (candidato.startswith(_CERCA) and candidato.endswith(_CERCA)):
        return candidato
    interior = candidato[len(_CERCA):-len(_CERCA)]
    if interior[:4].casefold() == "json":
        interior = interior[4:]
    return interior.strip()


def interpretar_respuesta(texto: object) -> dict | None:
    """El objeto JSON de la respuesta del modelo (tolera una cerca ```json y texto alrededor), o `None` si no hay uno."""
    if not isinstance(texto, str):
        return None
    candidato = _sin_cerca_json(texto.strip())
    for intento in (candidato, candidato[candidato.find("{"):candidato.rfind("}") + 1]):
        try:
            dato = json.loads(intento)
        except ValueError:
            continue
        if isinstance(dato, dict):
            return dato
    return None


def _cita_suficiente(cita_normalizada: str) -> bool:
    return len(cita_normalizada) >= LONGITUD_MINIMA_CITA and len(cita_normalizada.split()) >= PALABRAS_MINIMAS_CITA


def _referencias_efectivas(cita_normalizada: str, referencias: tuple[int, ...], fragmentos: list[FragmentoRecuperado]) -> tuple[int, ...]:
    """Solo respaldan las referencias cuyo fragmento CONTIENE la cita; un número válido sin ella no se presenta como fuente."""
    return tuple(n for n in referencias if cita_normalizada in normalizar(fragmentos[n - 1].texto_literal))


def _verificar_afirmacion(cruda: object, fragmentos: list[FragmentoRecuperado]) -> AfirmacionVerificada | None:
    if not isinstance(cruda, dict):
        return None
    texto, referencias, cita = cruda.get("texto"), cruda.get("referencias"), cruda.get("cita_textual")
    if not isinstance(texto, str) or not isinstance(cita, str) or not isinstance(referencias, list) or not referencias:
        return None
    texto = " ".join(_MARCADOR.sub(" ", texto).split())  # los marcadores los pone el servicio, no el modelo
    if not texto:
        return None
    total = len(fragmentos)
    if any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= total for n in referencias):
        return None
    unicas = tuple(sorted(set(referencias)))
    cita_normalizada = normalizar(cita)
    if not _cita_suficiente(cita_normalizada):
        return None
    efectivas = _referencias_efectivas(cita_normalizada, unicas, fragmentos)
    if not efectivas:
        return None
    evidencia = " ".join([cita, *(parte for n in efectivas for parte in fragmentos[n - 1].ruta_encabezados)])
    if not cifras(texto) <= cifras(evidencia):
        return None  # el texto afirma una cifra que ni la cita ni su sección contienen
    return AfirmacionVerificada(texto=texto, referencias=efectivas, cita_textual=" ".join(cita.split()))


def verificar_interpretacion(
    cruda: object, verificadas: list[AfirmacionVerificada], fragmentos: list[FragmentoRecuperado], pregunta: str = ""
) -> str | None:
    """La interpretación del modelo lista para mostrarse, o `None` si no se puede mostrar.

    La interpretación NO es una afirmación con cita: es el análisis del modelo sobre las afirmaciones ya verificadas, y se presenta aparte
    y etiquetada. Por eso no se le exige cita literal. Sí se exige que (1) exista al menos una afirmación verificada (sin hechos
    respaldados no hay nada que interpretar), (2) sea texto no vacío de hasta `MAXIMO_INTERPRETACION` caracteres y (3) toda cifra que
    escriba (salvo los años) figure en las afirmaciones verificadas, en sus citas, en el encabezado de las secciones citadas o en la
    pregunta del usuario: no puede introducir cifras nuevas (tampoco calculadas). LÍMITE: no se comprueba que el razonamiento sea
    correcto ni que se desprenda de las afirmaciones; es la opinión del modelo y así debe mostrarse."""
    if not isinstance(cruda, str) or not verificadas or "\x00" in cruda:
        return None
    texto = " ".join(_MARCADOR.sub(" ", cruda).split())  # sin marcadores [n]: las fuentes son las de las afirmaciones
    if not texto or len(texto) > MAXIMO_INTERPRETACION:
        return None
    evidencia = [pregunta]
    for afirmacion in verificadas:
        evidencia += [afirmacion.texto, afirmacion.cita_textual]
        evidencia += [parte for n in afirmacion.referencias for parte in fragmentos[n - 1].ruta_encabezados]
    if not cifras(texto) <= cifras(" ".join(evidencia)):
        return None
    return texto


def verificar_respuesta(
    texto_del_modelo: object, fragmentos: list[FragmentoRecuperado], *, permitir_interpretacion: bool = False, pregunta: str = ""
) -> ResultadoVerificacion:
    """Conserva solo las afirmaciones verificables (ver el docstring del módulo) y explica por qué no hay respuesta si ninguna lo es.

    Con `permitir_interpretacion` se lee además el campo «interpretacion» (ver `verificar_interpretacion`); sin él se ignora, y las
    afirmaciones se verifican exactamente igual en los dos modos."""
    dato = interpretar_respuesta(texto_del_modelo)
    if dato is None:
        return ResultadoVerificacion((), 0, MOTIVO_RESPUESTA_NO_INTERPRETABLE)
    if dato.get("fuera_de_dominio") is True:
        return ResultadoVerificacion((), 0, MOTIVO_FUERA_DE_DOMINIO)
    crudas = dato.get("afirmaciones")
    if not isinstance(crudas, list):
        return ResultadoVerificacion((), 0, MOTIVO_RESPUESTA_NO_INTERPRETABLE)
    if not crudas:
        return ResultadoVerificacion((), 0, MOTIVO_MODELO_SIN_EVIDENCIA)
    verificadas: list[AfirmacionVerificada] = []
    descartadas = max(0, len(crudas) - MAXIMO_AFIRMACIONES)
    for cruda in crudas[:MAXIMO_AFIRMACIONES]:
        afirmacion = _verificar_afirmacion(cruda, fragmentos)
        if afirmacion is None:
            descartadas += 1
        else:
            verificadas.append(afirmacion)
    if not verificadas:
        return ResultadoVerificacion((), descartadas, MOTIVO_AFIRMACIONES_NO_VERIFICABLES)
    cruda = dato.get("interpretacion") if permitir_interpretacion else None
    interpretacion = verificar_interpretacion(cruda, verificadas, fragmentos, pregunta)
    enviada = isinstance(cruda, str) and bool(cruda.strip())
    return ResultadoVerificacion(
        tuple(verificadas), descartadas, None, interpretacion=interpretacion, interpretacion_descartada=enviada and interpretacion is None
    )

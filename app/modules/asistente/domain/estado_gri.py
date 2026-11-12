"""Estado REGISTRADO en Análisis GRI para el Asistente IA. Regla pura: sin BD, sin HTTP, sin LLM.

QUÉ RESUELVE. El chat responde con fragmentos del documento; el estado de un código GRI (OK, Baja sustancia, Sub-reportado) lo asigna
una persona en Análisis GRI y NUNCA está en el documento. Cuando la pregunta nombra un código GRI explícito y pide su estado o una
brecha, el servidor lee el análisis registrado (la misma lectura y la misma regla de vigencia de Análisis GRI) y COMPONE aquí el texto del
estado. El modelo no decide ni el estado ni su vigencia, y el estado no entra a su prompt.

DÓNDE VA. En `ConsultaResponse.estado_registrado` (texto + estructura), nunca en `afirmaciones` ni `citas`: el estado manual no es una cita del
documento y no puede presentarse como tal. En una pregunta de SOLO ESTADO (`solo_estado`) no hay búsqueda documental: `busqueda_documental` es
NO_REALIZADA y `respuesta` repite el texto del estado. En una pregunta mixta la búsqueda se hace como siempre (REALIZADA) y el mismo bloque se copia
además en `mensaje` por compatibilidad con los clientes que aún no leen `estado_registrado` (con evidencia, solo el bloque; sin ella, el bloque
seguido de «Evidencia documental: …» con el aviso habitual).

LÍMITES. (a) Solo se reconoce un código escrito como «GRI <n>» (también «GRI 305-1», «GRI 102 (2016)»); «el estándar 305» o un número suelto
no cuentan. (b) La pregunta se interpreta con palabras clave: si no aparece una (estado, brecha, validación, vigencia…) no se consulta nada y
el chat se comporta como antes. (c) No existe una regla de negocio que convierta un estado en «hay» o «no hay» brecha: se informa el estado
tal cual. (d) El estado es por ESTÁNDAR; una revelación (305-1) hereda el estado de su estándar y se avisa. (e) «Solo estado» es una gramática
cerrada sobre la pregunta original; brechas, varios códigos, revelaciones, preguntas mixtas y seguimientos sin código siguen el flujo documental.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.modules.analisis_gri.schemas import AnalisisResponse, EstadoFlujo, FilaGri, MotivoNoVigente
from app.modules.asistente.schemas import EstadoRegistrado, FilaEstadoRegistrado, ValidacionRegistrada

MAX_CODIGOS = 3  # estándares distintos que se informan por respuesta
SEPARADOR_EVIDENCIA = "\n\nEvidencia documental: "
TITULO = "Estado registrado en Análisis GRI"

# «GRI 1», «GRI-13», «GRI 305-1», «GRI 305-1-a», «GRI 14.1», «GRI 102 (2016)», «GRI 102-2016». El número es el ESTÁNDAR (1 a 3 cifras, sin
# ceros a la izquierda) y no puede continuar con otro dígito: «GRI 13» es el 13 y nunca el 1; «GRI 1234» no es ningún código.
_CODIGO = re.compile(
    r"(?<![A-Za-z0-9])GRI[\s \-]*(?P<codigo>[1-9][0-9]{0,2})(?![0-9])"
    r"(?P<sufijo>(?:-[0-9]{1,3}(?:-?[a-z]\b|\([a-z]\))?|\.[0-9]+(?:\.[0-9]+)*)(?![0-9]))?"
    r"(?:\s*\(\s*(?P<ed_a>(?:19|20)[0-9]{2})\s*\)|\s*[:\-]\s*(?P<ed_b>(?:19|20)[0-9]{2})(?![0-9]))?",
    re.IGNORECASE,
)
_INTENCION_ESTADO = re.compile(r"\b(?:estados?|brechas?|valid\w*|vigen\w*|sub-?reportad\w*|baja sustancia)\b")
_MENCIONA_BRECHA = re.compile(r"\bbrechas?\b")

_MOTIVO_TEXTO = {
    MotivoNoVigente.ESTADOS_CAMBIARON: "cambiaron los estados después de esa validación",
    MotivoNoVigente.DOCUMENTOS_CAMBIARON: "cambió el conjunto de documentos elegibles",
}


@dataclass(frozen=True)
class CodigoSolicitado:
    codigo: str  # el estándar, p. ej. «1», «13», «305»
    referencia: str  # tal como lo escribió el usuario, p. ej. «GRI 305-1»
    revelacion: bool  # escribió un sufijo de revelación (305-1): el estado sigue siendo el del estándar
    edicion: str | None  # solo si la escribió explícitamente


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)).casefold()


def pide_estado(pregunta: str) -> bool:
    return _INTENCION_ESTADO.search(_sin_acentos(pregunta)) is not None


def menciona_brecha(pregunta: str) -> bool:
    return _MENCIONA_BRECHA.search(_sin_acentos(pregunta)) is not None


def codigos_explicitos(pregunta: str) -> list[CodigoSolicitado]:
    """Códigos GRI escritos con el prefijo «GRI», sin repetir (estándar + edición) y en el orden en que aparecen."""
    vistos: set[tuple[str, str | None]] = set()
    resultado: list[CodigoSolicitado] = []
    for m in _CODIGO.finditer(pregunta):
        edicion = m.group("ed_a") or m.group("ed_b")
        clave = (m.group("codigo"), edicion)
        if clave in vistos:
            continue
        vistos.add(clave)
        referencia = " ".join(m.group(0).split())
        resultado.append(CodigoSolicitado(m.group("codigo"), referencia, m.group("sufijo") is not None, edicion))
    return resultado


def codigos_para_estado(pregunta: str) -> list[CodigoSolicitado]:
    """Los códigos cuyo estado hay que informar: solo si la pregunta pide estado o brecha Y nombra un código explícito."""
    return codigos_explicitos(pregunta) if pide_estado(pregunta) else []


# ------------------------------------------------------------------------------------------------------------------------------------
# «SOLO ESTADO»: la petición CLARA de leer el estado registrado de UN código, que podría responderse sin búsqueda documental ni modelo.
# Es una GRAMÁTICA CERRADA, no la ausencia de palabras: la frase completa (quitando únicamente el código y relleno de contexto de una lista
# fija) debe encajar en una plantilla. Cualquier resto («según el reporte», «y por qué», «qué dice el documento», otro código, otro año, un
# pronombre sin código) la deja fuera, y la pregunta sigue el flujo documental con su verificación de citas.

_MARCA = "zzcodigozz"  # sustituye al código en la frase; no es una palabra que alguien escriba
_RELLENO = re.compile(r"\b(?:de la empresa|de esta empresa|en el analisis gri|en analisis gri|por favor|actualmente|hoy)\b")
_PLANTILLAS = (
    re.compile(rf"(?:y )?(?:que|cual) estado (?:registrado )?(?:actual )?tiene (?:el )?{_MARCA}"),
    re.compile(rf"(?:y )?(?:(?:cual es|dime|indica|indicame|muestrame|quiero saber) )?el estado (?:registrado )?(?:actual )?(?:de|del) {_MARCA}"),
    re.compile(rf"(?:y )?estado (?:registrado )?(?:actual )?(?:de|del) {_MARCA}"),
)


def _frase_normalizada(texto: str) -> str:
    """Sin acentos ni mayúsculas, sin signos de puntuación y con los espacios colapsados."""
    letras = (c if c.isalnum() else " " for c in _sin_acentos(texto))
    return " ".join("".join(letras).split())


def solo_estado(pregunta: str, anio: int) -> CodigoSolicitado | None:
    """El código pedido si la pregunta es, de forma INEQUÍVOCA, solo una petición del estado registrado de un código GRI (del año de la
    conversación); `None` en cualquier otro caso. «¿Y qué estado tiene ese?» (sin código) tampoco lo es: necesita el contexto, o sea, el modelo."""
    coincidencias = list(_CODIGO.finditer(pregunta))
    if len(coincidencias) != 1 or coincidencias[0].group("sufijo") is not None:
        return None  # un solo código, sin revelación: el resto de casos sigue el flujo documental
    frase = _frase_normalizada(_CODIGO.sub(f" {_MARCA} ", pregunta))
    frase = re.sub(rf"\b(?:en|para|del) {anio}\b", " ", frase)  # solo el año de la conversación es relleno admitido
    frase = " ".join(_RELLENO.sub(" ", frase).split())
    if not any(plantilla.fullmatch(frase) for plantilla in _PLANTILLAS):
        return None
    return codigos_explicitos(pregunta)[0]


# ------------------------------------------------------------------------------------------------------------------------------------


def _vigente(analisis: AnalisisResponse) -> bool:
    """La misma regla de `obtener_analisis_validado`: flujo VALIDADO y validación vigente."""
    return analisis.estado_flujo is EstadoFlujo.VALIDADO and analisis.validacion is not None and analisis.validacion.vigente


def _texto_validacion(analisis: AnalisisResponse) -> str:
    validacion = analisis.validacion
    if _vigente(analisis):
        return f"Validación vigente (n.º {validacion.numero}, validada el {validacion.validado_en.date().isoformat()})."
    if validacion is None:
        return "No cuenta con validación vigente: el análisis aún no se ha validado."
    motivos = " y ".join(_MOTIVO_TEXTO[m] for m in validacion.motivos_no_vigente) or "cambió el análisis"
    return f"No cuenta con validación vigente: la última validación (n.º {validacion.numero}) perdió vigencia porque {motivos}."


def _linea_fila(fila: FilaGri, analisis: AnalisisResponse) -> str:
    if fila.estado is None:
        return f"{fila.estandar}: figura en el análisis, pero no tiene estado asignado."
    return f"{fila.estandar}: {fila.estado_etiqueta}. {_texto_validacion(analisis)}"


def _filas_del_pedido(pedido: CodigoSolicitado, analisis: AnalisisResponse) -> tuple[list[FilaGri], list[FilaGri]]:
    """(filas cuyo estado se informa, todas las filas del código). Con una edición explícita solo cuenta una fila resuelta con esa edición:
    nunca se sustituye por otra ni se le atribuye el estado de otra (una identidad ambigua tiene edición nula y nunca coincide)."""
    del_codigo = [f for f in analisis.filas if f.codigo == pedido.codigo]
    if pedido.edicion is None:
        return del_codigo, del_codigo
    return [f for f in del_codigo if f.edicion == pedido.edicion], del_codigo


def _lineas_de_codigo(pedido: CodigoSolicitado, analisis: AnalisisResponse) -> list[str]:
    filas, del_codigo = _filas_del_pedido(pedido, analisis)
    if pedido.edicion is not None and del_codigo and not filas:
        figuran = ", ".join(f.estandar for f in del_codigo)
        return [f"{pedido.referencia}: no hay una fila de esa edición en el análisis; figura(n): {figuran}. No se les atribuye ese estado."]
    if not filas:
        if analisis.estado_flujo is EstadoFlujo.DOCUMENTACION_INSUFICIENTE:
            return [f"{pedido.referencia}: el Análisis GRI no tiene documentos elegibles para esta empresa y año."]
        return [f"{pedido.referencia}: no figura en el Análisis GRI de esta empresa y año (no se concluye nada sobre el documento)."]
    lineas: list[str] = []
    if pedido.revelacion:
        lineas.append(f"{pedido.referencia}: el estado se registra por estándar (GRI {pedido.codigo}), no por revelación.")
    lineas += [_linea_fila(f, analisis) for f in filas]
    if len(filas) > 1 and pedido.edicion is None:
        lineas.append(f"GRI {pedido.codigo} tiene más de una identidad en el análisis; indique la edición para precisar.")
    return lineas


def _componer(
    pedidos: list[CodigoSolicitado], analisis: AnalisisResponse, *, empresa_nombre: str, brecha: bool, maximo: int
) -> tuple[str, int]:
    """(texto, cuántos de los `pedidos` quedaron informados): ver `componer_estado`."""
    mostrados, omitidos = pedidos[:MAX_CODIGOS], len(pedidos) - MAX_CODIGOS
    cabecera = f"{TITULO} ({' '.join(empresa_nombre.split())}, {analisis.anio}):"
    cierre = ["El estado lo asigna una persona en Análisis GRI; detectar un código en el documento no demuestra cumplimiento ni una brecha."]
    if brecha:
        cierre.append("No hay una regla registrada que convierta el estado en «hay» o «no hay» brecha: se informa el estado tal cual.")
    cuerpos = [_lineas_de_codigo(p, analisis) for p in mostrados]

    def armar(cuerpos_: list[list[str]], aviso: bool, con_cierre: int) -> str:
        lineas = [cabecera] + [f"- {linea}" for cuerpo in cuerpos_ for linea in cuerpo]
        if aviso:
            lineas.append(f"- (se omiten {len(pedidos) - len(cuerpos_)} códigos más; pregunte por ellos aparte)")
        return "\n".join(lineas + cierre[:con_cierre])

    candidato = armar(cuerpos, omitidos > 0, len(cierre))
    if len(candidato) > maximo:
        candidato = armar(cuerpos, omitidos > 0, 0)  # sin aclaraciones finales
    while len(candidato) > maximo and len(cuerpos) > 1:
        cuerpos = cuerpos[:-1]  # se omiten códigos del final, con aviso
        candidato = armar(cuerpos, True, 0)
    return candidato, len(cuerpos)


def componer_estado(
    pedidos: list[CodigoSolicitado], analisis: AnalisisResponse, *, empresa_nombre: str, brecha: bool, maximo: int
) -> str:
    """Bloque «Estado registrado en Análisis GRI» (texto plano) con los datos del análisis. Nunca más de `maximo` caracteres: si no cabe,
    se omiten códigos (con aviso) y por último las aclaraciones, pero el estado de cada código informado no se recorta."""
    return _componer(pedidos, analisis, empresa_nombre=empresa_nombre, brecha=brecha, maximo=maximo)[0]


def construir_estado_registrado(
    pedidos: list[CodigoSolicitado], analisis: AnalisisResponse, *, empresa_nombre: str, brecha: bool, maximo: int
) -> EstadoRegistrado:
    """El mismo bloque de texto, más los datos estructurados para que el cliente no tenga que interpretarlo. `codigos_solicitados` y
    `filas` cubren solo los códigos informados en el texto (los omitidos por longitud se avisan en él)."""
    texto, informados = _componer(pedidos, analisis, empresa_nombre=empresa_nombre, brecha=brecha, maximo=maximo)
    cubiertos = pedidos[:informados]
    filas: dict[str, FilaEstadoRegistrado] = {}
    for pedido in cubiertos:
        for fila in _filas_del_pedido(pedido, analisis)[0]:
            filas.setdefault(
                fila.identidad,
                FilaEstadoRegistrado(
                    identidad=fila.identidad, codigo=fila.codigo, edicion=fila.edicion, estandar=fila.estandar,
                    estado=None if fila.estado is None else fila.estado.value, estado_etiqueta=fila.estado_etiqueta,
                ),
            )
    validacion = analisis.validacion
    return EstadoRegistrado(
        texto=texto,
        codigos_solicitados=[p.referencia for p in cubiertos],
        filas=list(filas.values()),
        validacion_vigente=_vigente(analisis),
        validacion=None if validacion is None else ValidacionRegistrada(
            numero=validacion.numero, validado_en=validacion.validado_en, motivos_no_vigente=[m.value for m in validacion.motivos_no_vigente]
        ),
    )

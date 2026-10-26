"""Relleno de la plantilla HTML del reporte de prospección (Módulo 7). Regla pura: no llama a IA,
solo convierte el análisis validado en el HTML. El PDF es un render de datos ya validados (RN-040):
la plantilla HTML se rellena con jinja2 (autoescape) y se compila a PDF con WeasyPrint.

SANCIONES EN EL PDF (decisión 2026-10-19, textos visibles 2026-10-26). Lo que el detector cuenta son REFERENCIAS: apariciones de
una sanción en el texto, no multas distintas. El PDF distingue tres cosas:
- «Multa agrupada»: varias referencias con identidad explícita (autoridad + identificador completo + una distinción
  coincidente), con TODAS sus citas y su procedencia.
- «Referencia individual»: sin evidencia suficiente para unirla a otra. Si coincide con otra solo en autoridad y monto se
  marca «Posible sanción duplicada» y se lista aparte; nunca se une ni se descarta.
- «No se ha comprobado si esta referencia repite una multa»: de un documento procesado antes de existir la agrupación; no se presenta como deduplicada.
Los montos ambiguos conservan su texto original con «Monto pendiente de revisión» y no se suman. El total suma UN monto por
agrupación (nunca las referencias repetidas) y solo montos enteros inequívocos y sin escala. Se presenta únicamente cuando es
completo y fiable; si no, el PDF dice «Total no disponible» con los motivos que los datos respaldan (`MOTIVOS_TOTAL`), sin
ninguna suma parcial, y conserva los montos individuales, las citas y las advertencias. Si no hay sanciones lo dice
(«No se detectaron sanciones económicas»): no es un total imposible de calcular. La disponibilidad se evalúa por multa:
en una agrupación basta que una cita aporte el monto aunque otra lo omita.

Todo lo anterior se calcula al renderizar a partir del análisis; no se recalcula ni se escribe nada al consultarlo, y un análisis
guardado antes de existir la agrupación se sigue leyendo (sus referencias se rotulan «No se ha comprobado si esta referencia repite una multa»).
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2

from app.modules.analisis_gri.schemas import AgrupacionSancion, AnalisisResponse, EstadoGri, SancionVista
from app.modules.ingesta.domain.agrupacion_sanciones import (
    ADV_DATOS_EN_CONFLICTO,
    ADV_IMPORTE_A_REVISAR,
    ADV_POSIBLE_DUPLICADA,
)

_RUTA_PLANTILLA = Path(__file__).resolve().parent.parent / "plantillas" / "reporte_prospeccion.html"

# Clases CSS de los badges de estado (definidas en la plantilla).
_CLASE_ESTADO = {
    "OK": ("ok", "OK"),
    "BAJA_SUSTANCIA": ("baja", "Baja sustancia"),
    "SUB_REPORTADO": ("sub", "Sub-reportado"),
}

# --- Textos visibles de las sanciones ---------------------------------------------------------------------------------
# Lenguaje llano: «monto» (nunca «importe»), «referencia» (no «mención»), sin términos de la agrupación interna.
TXT_SIN_SANCIONES = "No se detectaron sanciones económicas"
TXT_MONTO_PENDIENTE = "Monto pendiente de revisión"
TXT_MONTO_NO_DETECTADO = "Monto de la multa no detectado"
TXT_MONTO_NO_ESPECIFICADO = "Monto no especificado en el documento"
TXT_NOTA_FORMATO = "El formato del número puede tener más de una interpretación. Por eso no se suma al total."
TXT_NOTA_MONTOS_DISTINTOS = "Las referencias a esta multa indican montos distintos. Por eso no se suma al total."
TXT_PUNTAJE_NO_DISPONIBLE = "No disponible"

MOTIVO_REVISION = "REVISION"
MOTIVO_SIN_MONTO = "SIN_MONTO"
MOTIVO_MISMA_MULTA = "POSIBLE_MISMA_MULTA"
MOTIVO_NO_COMPARADAS = "NO_COMPARADAS"
MOTIVO_ANALISIS_ANTIGUO = "ANALISIS_ANTIGUO"
MOTIVO_NO_SUMABLES = "NO_SUMABLES"

# Por qué no puede calcularse un total completo y fiable. Cada motivo se usa solo cuando los datos lo respaldan.
MOTIVOS_TOTAL = {
    MOTIVO_REVISION: "Hay montos que necesitan revisión.",
    MOTIVO_SIN_MONTO: "No se detectó el monto de algunas multas.",
    MOTIVO_MISMA_MULTA: "Algunas referencias podrían corresponder a la misma multa.",
    MOTIVO_NO_COMPARADAS: "No se pudo comprobar si todas las referencias corresponden a multas distintas.",
    MOTIVO_ANALISIS_ANTIGUO: "El análisis guardado no incluye la información necesaria para calcular el total.",
    MOTIVO_NO_SUMABLES: "Hay montos escritos con decimales o con palabras («mil», «millones») que no se suman.",
}

# Texto visible de las advertencias por su código: el mensaje que trae el análisis guardado puede ser de una versión anterior.
_TEXTO_ADVERTENCIA = {
    ADV_POSIBLE_DUPLICADA: "Posible sanción duplicada",
    ADV_DATOS_EN_CONFLICTO: "Las referencias tienen datos que se contradicen; se muestran por separado",
}

# Separadores de miles reconocidos: espacio, coma, punto, apóstrofos, espacio de no corte y espacio fino de no corte.
_SEPARADORES_DE_MILES = " ,.'’  "
_PATRON_SEPARADOR = re.compile("[" + re.escape(_SEPARADORES_DE_MILES) + "]")
_ESCALA = re.compile(r"\b(?:mil|millones?|mm)\b", re.IGNORECASE)
_ENTERO = re.compile(r"\d+")


def _fmt_puntaje(analisis: AnalisisResponse) -> str:
    valor = analisis.puntaje.valor_presentacion
    if valor is None:
        return TXT_PUNTAJE_NO_DISPONIBLE
    return str(int(valor)) if float(valor).is_integer() else str(valor)


def _cita_de_fila(fila) -> tuple[str, str]:
    if not fila.evidencias:
        return "(sin cita de respaldo)", ""
    ev = fila.evidencias[0]
    fuente = " · ".join(x for x in [getattr(ev, "documento_nombre", ""), getattr(ev, "seccion", "")] if x)
    return ev.cita, fuente


def _fila_a_dict(fila) -> dict:
    tema = fila.nombre or fila.estandar
    estado_id = fila.estado.value if fila.estado else ""
    clase, etiqueta = _CLASE_ESTADO.get(estado_id, ("neutro", fila.estado_etiqueta or "sin estado"))
    cita, fuente = _cita_de_fila(fila)
    return {"codigo": fila.codigo, "tema": tema, "estado_color": clase,
            "estado_texto": etiqueta, "cita": cita, "fuente": fuente}


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


# --- Montos -----------------------------------------------------------------------------------------------------------

def _valor_entero(monto) -> int | None:
    """Valor de un monto SOLO si es un entero inequívoco: cifras sin separadores o con un mismo separador de miles repetido,
    sin escala («mil», «millones») ni decimales. Todo lo demás se muestra pero no se suma: no se interpreta ni se redondea."""
    if _ESCALA.search(monto.texto):
        return None
    numero = monto.numero_texto.strip()
    if _ENTERO.fullmatch(numero):
        return int(numero)
    grupos = _PATRON_SEPARADOR.split(numero)
    separadores = {c for c in numero if c in _SEPARADORES_DE_MILES}
    cabeza_valida = len(grupos[0]) <= 3 and _ENTERO.fullmatch(grupos[0]) is not None
    resto_valido = all(len(g) == 3 and g.isdigit() for g in grupos[1:])
    if len(separadores) == 1 and len(grupos) > 2 and cabeza_valida and resto_valido:
        return int("".join(grupos))
    return None


def _texto_de_monto(monto) -> str:
    """El monto tal como está escrito; si la moneda sale del encabezado de la tabla se antepone para que se entienda."""
    if getattr(monto, "unidad_inicio", None) is not None and monto.unidad not in monto.texto:
        return f"{monto.unidad} {monto.texto}"
    return monto.texto


def _presentar_monto(estado: str, montos: list) -> dict:
    """Cómo se muestra el monto de una multa o referencia: el texto original (si lo hay), una etiqueta y una nota. Un monto
    ambiguo no se convierte ni se interpreta; uno ausente no es cero."""
    datos = {"tiene_monto": False, "monto": "", "monto_etiqueta": "", "monto_nota": ""}
    if estado == "CUANTIFICADO" and montos:
        return {**datos, "tiene_monto": True, "monto": _texto_de_monto(montos[0])}
    if estado == "REQUIERE_REVISION":
        textos = list(dict.fromkeys(_texto_de_monto(m) for m in montos))
        if any(m.separador_ambiguo for m in montos):
            nota = TXT_NOTA_FORMATO
        else:
            nota = TXT_NOTA_MONTOS_DISTINTOS if len(textos) > 1 else ""
        return {"tiene_monto": bool(textos), "monto": " / ".join(textos), "monto_etiqueta": TXT_MONTO_PENDIENTE, "monto_nota": nota}
    etiqueta = TXT_MONTO_NO_ESPECIFICADO if estado == "NO_ESPECIFICADO" else TXT_MONTO_NO_DETECTADO
    return {**datos, "monto_etiqueta": etiqueta}


def _sumas_por_unidad(montos: list) -> dict[str, int]:
    """Suma montos explícitos por moneda escrita, UNO por agrupación o referencia. Excluye ambiguos, con escala o decimales."""
    sumas: dict[str, int] = {}
    for m in montos:
        valor = None if m.separador_ambiguo else _valor_entero(m)
        if valor is not None:
            sumas[m.unidad] = sumas.get(m.unidad, 0) + valor
    return sumas


# --- Sanciones --------------------------------------------------------------------------------------------------------

def _cita_a_dict(cita) -> dict:
    fuente = " · ".join(x for x in [cita.documento_nombre, cita.seccion] if x)
    return {"cita": cita.cita, "fuente": fuente, "posicion": f"{cita.cita_inicio}–{cita.cita_fin}"}


def _autoridad(dato) -> tuple[str, str]:
    """(nombre, línea visible). Sin autoridad identificada no se inventa ninguna."""
    nombre = dato.texto if dato else ""
    return nombre, f"Autoridad que aplicó la sanción: {nombre or 'no identificada'}"


def _texto_advertencia(adv) -> str:
    return _TEXTO_ADVERTENCIA.get(adv.codigo, adv.mensaje)


def _unidad_a_dict(a: AgrupacionSancion) -> dict:
    estado = a.estado_importe.value
    citas = [_cita_a_dict(c) for c in a.citas]
    etiqueta = (
        f"Multa agrupada: {a.menciones} referencias identificadas como la misma multa" if a.agrupada else "Referencia individual"
    )
    # «Monto pendiente de revisión» ya se muestra junto al monto: no se repite como advertencia.
    advertencias = [_texto_advertencia(adv) for adv in a.advertencias if adv.codigo != ADV_IMPORTE_A_REVISAR]
    entidad, autoridad_texto = _autoridad(a.autoridad)
    return {
        "id": a.id,
        "tipo": "agrupada" if a.agrupada else "individual",
        "etiqueta": etiqueta,
        "entidad": entidad,
        "autoridad_texto": autoridad_texto,
        "identificador": " · ".join(dict.fromkeys(r.identificador for r in a.identificadores)),
        "numeral": a.numeral.texto if a.numeral else "",
        "infraccion": a.infraccion.texto if a.infraccion else "",
        **_presentar_monto(estado, a.importes),
        "estado_importe": estado,
        "citas": citas,
        "descripcion": citas[0]["cita"] if citas else "",
        "fuente": citas[0]["fuente"] if citas else "",
        "advertencias": advertencias,
        "a_revisar": bool(advertencias) or estado == "REQUIERE_REVISION",
    }


def _estado_de_mencion(s: SancionVista) -> str:
    if s.monto is None:
        return "NO_CONSTA"
    return "REQUIERE_REVISION" if s.monto.separador_ambiguo else "CUANTIFICADO"


def _sancion_a_dict(s: SancionVista) -> dict:
    """Una referencia de un documento procesado SIN agrupación: se muestra sola y rotulada, sin afirmar que esté deduplicada."""
    fuente = " · ".join(x for x in [s.documento_nombre, s.seccion] if x)
    estado = _estado_de_mencion(s)
    entidad, autoridad_texto = _autoridad(s.entidad)
    return {
        "id": "",
        "tipo": "sin_evaluar",
        "etiqueta": "No se ha comprobado si esta referencia repite una multa",
        "entidad": entidad,
        "autoridad_texto": autoridad_texto,
        "identificador": "",
        "numeral": "",
        "infraccion": "",
        **_presentar_monto(estado, [s.monto] if s.monto is not None else []),
        "estado_importe": estado,
        "citas": [{"cita": s.cita, "fuente": fuente, "posicion": f"{s.cita_inicio}–{s.cita_fin}"}],
        "descripcion": s.cita,
        "fuente": fuente,
        "advertencias": [],
        "a_revisar": estado == "REQUIERE_REVISION",
    }


def _vecinos_por_duplicidad(unidades: list[AgrupacionSancion]) -> dict[str, set[str]]:
    vecinos: dict[str, set[str]] = {a.id: set() for a in unidades}
    for a in unidades:
        for adv in a.advertencias:
            if adv.codigo != ADV_POSIBLE_DUPLICADA:
                continue
            for otra in adv.con:
                if otra in vecinos:
                    vecinos[a.id].add(otra)
                    vecinos[otra].add(a.id)
    return vecinos


def _grupos_de_posibles_duplicados(unidades: list[AgrupacionSancion]) -> list[list[str]]:
    """Ids de las unidades unidas por «Posible sanción duplicada» (componentes conexos), en orden de aparición."""
    vecinos = _vecinos_por_duplicidad(unidades)
    orden = {a.id: k for k, a in enumerate(unidades)}
    vistos: set[str] = set()
    grupos: list[list[str]] = []
    for a in unidades:
        pendientes = [a.id] if vecinos[a.id] else []
        componente: list[str] = []
        while pendientes:
            actual = pendientes.pop()
            if actual in vistos:
                continue
            vistos.add(actual)
            componente.append(actual)
            pendientes.extend(vecinos[actual] - vistos)
        if componente:
            grupos.append(sorted(componente, key=orden.__getitem__))
    return grupos


# --- Conteos de referencias -------------------------------------------------------------------------------------------

def _conteos_de_referencias(menciones: list[SancionVista], unidades: list[AgrupacionSancion]) -> dict[str, int]:
    """Conteos POR REFERENCIA, todos derivados de la misma lista: `con_monto` + `sin_monto` = `total`. Las referencias con monto
    pendiente de revisión forman parte de `con_monto` (no son una categoría aparte): un monto ambiguo, o el de una multa cuyas
    referencias indican montos distintos. Una referencia sin monto en una multa agrupada que sí lo tiene en otra cita cuenta
    como sin monto detectado (es lo que ocurre con esa referencia), pero no hace faltar el monto de la multa."""
    estado_de = {a.id: a.estado_importe.value for a in unidades}
    con = pendientes = 0
    for s in menciones:
        if s.monto is None:
            continue
        con += 1
        if s.monto.separador_ambiguo or estado_de.get(getattr(s, "agrupacion_id", None)) == "REQUIERE_REVISION":
            pendientes += 1
    return {"total": len(menciones), "con_monto": con, "sin_monto": len(menciones) - con, "pendientes": pendientes}


def _frase_de_pendientes(con: int, pendientes: int) -> str:
    if pendientes == con:
        return "El monto encontrado necesita revisión" if con == 1 else "Los montos encontrados necesitan revisión"
    verbo = "necesita" if pendientes == 1 else "necesitan"
    return f"{pendientes} de los {con} montos encontrados {verbo} revisión"


def _descripcion_resumen(conteos: dict[str, int], agrupaciones: int, agrupadas: int, disponible: bool) -> str:
    """Descripción del resumen ejecutivo, generada a partir de los conteos (singular/plural según corresponda)."""
    n, con, sin, pendientes = conteos["total"], conteos["con_monto"], conteos["sin_monto"], conteos["pendientes"]
    if n == 0:
        return f"{TXT_SIN_SANCIONES}."
    apertura = "Se encontró 1 referencia a una sanción" if n == 1 else f"Se encontraron {n} referencias a sanciones"
    if n == 1:
        detalle = "con monto detectado" if con else "sin monto detectado"
    else:
        detalle = " y ".join(p for p in (f"{con} con monto detectado" if con else "", f"{sin} sin monto detectado" if sin else "") if p)
    frases = [f"{apertura}: {detalle}."]
    if agrupaciones:
        frases.append(
            f"{agrupadas} de ellas se reúnen en {_plural(agrupaciones, 'multa agrupada', 'multas agrupadas')} "
            "porque el documento las identifica como referencias a una misma multa."
        )
    if pendientes:
        frases.append(f"{_frase_de_pendientes(con, pendientes)}, por lo que el total no está disponible.")
    elif not disponible:
        frases.append("El total no está disponible.")
    return " ".join(frases)


# --- Monto total ------------------------------------------------------------------------------------------------------

def _montos_a_sumar(unidades: list[AgrupacionSancion], sin_unidad: list[SancionVista]) -> list:
    """UN monto por multa: el de cada agrupación con monto determinado y el de cada referencia sin agrupación evaluada.
    Una cita que omita el monto que otra de su agrupación sí trae no cuenta: el estado se evalúa sobre la multa entera."""
    montos = [a.importes[0] for a in unidades if a.estado_importe.value == "CUANTIFICADO" and a.importes]
    montos += [s.monto for s in sin_unidad if s.monto is not None and not s.monto.separador_ambiguo]
    return montos


def _motivos_del_total(analisis, unidades: list[AgrupacionSancion], sin_unidad: list[SancionVista], montos: list) -> list[str]:
    """Códigos de los motivos por los que no hay un total completo y fiable. Vacío = nada impide calcularlo (sigue siendo una
    detección léxica). Cada código se agrega solo si hay datos que lo respalden."""
    resumen = analisis.resumen_sanciones
    estados = {a.estado_importe.value for a in unidades}
    evaluacion = getattr(getattr(resumen, "evaluacion_agrupacion", None), "value", "NO_EVALUADA")
    sin_evaluar = getattr(resumen, "menciones_sin_evaluar", 0)
    motivos = []
    if "REQUIERE_REVISION" in estados or any(s.monto is not None and s.monto.separador_ambiguo for s in sin_unidad):
        motivos.append(MOTIVO_REVISION)
    if {"NO_ESPECIFICADO", "NO_CONSTA"} & estados:
        motivos.append(MOTIVO_SIN_MONTO)
    if getattr(resumen, "posibles_duplicados", 0) or getattr(resumen, "unidades_en_conflicto", 0):
        motivos.append(MOTIVO_MISMA_MULTA)
    # PARCIAL con todas las referencias evaluadas = se superó el límite de comparación. Si además hay referencias sin evaluar,
    # el análisis guardado ya explica la falta de total y no se afirma nada más sobre las evaluadas.
    if evaluacion == "PARCIAL" and not sin_evaluar:
        motivos.append(MOTIVO_NO_COMPARADAS)
    if sin_evaluar or evaluacion == "NO_EVALUADA":
        motivos.append(MOTIVO_ANALISIS_ANTIGUO)
    if any(_valor_entero(m) is None for m in montos):
        motivos.append(MOTIVO_NO_SUMABLES)
    if not motivos and not montos:
        motivos.append(MOTIVO_ANALISIS_ANTIGUO)  # hay sanciones pero ningún monto utilizable: faltan datos para calcular
    return motivos


def _datos_de_sanciones(analisis: AnalisisResponse) -> dict:
    resumen = analisis.resumen_sanciones
    unidades = list(getattr(analisis, "agrupaciones_sanciones", None) or [])
    menciones = list(analisis.sanciones)
    sin_unidad = [s for s in menciones if getattr(s, "agrupacion_id", None) is None]
    tarjetas = [_unidad_a_dict(a) for a in unidades] + [_sancion_a_dict(s) for s in sin_unidad]
    por_id = {t["id"]: t for t in tarjetas if t["id"]}
    conteos = _conteos_de_referencias(menciones, unidades)
    sin_sanciones = conteos["total"] == 0
    montos = _montos_a_sumar(unidades, sin_unidad)
    motivos = [] if sin_sanciones else _motivos_del_total(analisis, unidades, sin_unidad, montos)
    disponible = not sin_sanciones and not motivos
    sumas = _sumas_por_unidad(montos) if disponible else {}
    agrupadas = getattr(resumen, "menciones_agrupadas", 0)
    agrupaciones = getattr(resumen, "agrupaciones_identificadas", 0)
    return {
        "sanciones": tarjetas,
        "posibles_duplicados": [[por_id[i] for i in grupo] for grupo in _grupos_de_posibles_duplicados(unidades)],
        "agrupacion": {
            "evaluacion": getattr(getattr(resumen, "evaluacion_agrupacion", None), "value", "NO_EVALUADA"),
            "unidades": getattr(resumen, "unidades", None),
            "agrupaciones": agrupaciones,
            "menciones_agrupadas": agrupadas,
            "sin_evaluar": getattr(resumen, "menciones_sin_evaluar", 0),
            "posibles_duplicados": getattr(resumen, "posibles_duplicados", 0),
            "conflictos": getattr(resumen, "unidades_en_conflicto", 0),
            "importes_a_revisar": getattr(resumen, "importes_a_revisar", 0),
        },
        "referencias_total": conteos["total"],
        "referencias_con_monto": conteos["con_monto"],
        "referencias_sin_monto": conteos["sin_monto"],
        "referencias_pendientes": conteos["pendientes"],
        "casos_a_revisar": sum(t["a_revisar"] for t in tarjetas),
        "descripcion_sanciones": _descripcion_resumen(conteos, agrupaciones, agrupadas, disponible),
        # Solo hay total cuando es completo y fiable. Si no, no se muestra ninguna suma parcial; los montos individuales y
        # sus advertencias siguen en `sanciones`.
        "sin_sanciones": sin_sanciones,
        "money": [{"unidad": u, "total": f"{v:,.0f}"} for u, v in sumas.items()],
        "money_disponible": disponible,
        "money_motivos_codigos": motivos,
        "money_motivos": [MOTIVOS_TOTAL[m] for m in motivos],
    }


def datos_desde_analisis(analisis: AnalisisResponse, fecha_generacion: str) -> dict:
    """Convierte el análisis validado en el diccionario de la plantilla (valores crudos; jinja2 los escapa)."""
    empresa = analisis.empresa
    val = analisis.validacion

    conteo = {"ok": 0, "baja": 0, "sub": 0}
    for f in analisis.filas:
        if f.estado is EstadoGri.OK:
            conteo["ok"] += 1
        elif f.estado is EstadoGri.BAJA_SUSTANCIA:
            conteo["baja"] += 1
        elif f.estado is EstadoGri.SUB_REPORTADO:
            conteo["sub"] += 1

    puntaje = _fmt_puntaje(analisis)
    sanciones = _datos_de_sanciones(analisis)
    return {
        "empresa_nombre": empresa.nombre,
        "anio": analisis.anio,
        "sector": getattr(empresa.sector, "value", empresa.sector),
        "fecha_generacion": fecha_generacion,
        "puntaje": puntaje,
        "puntaje_disponible": puntaje != TXT_PUNTAJE_NO_DISPONIBLE,
        "puntaje_texto": f"{puntaje}/100" if puntaje != TXT_PUNTAJE_NO_DISPONIBLE else puntaje,
        "conteo_ok": conteo["ok"], "conteo_baja": conteo["baja"], "conteo_sub": conteo["sub"],
        # Son REFERENCIAS (apariciones en el texto), no multas distintas.
        "sanciones_total": sanciones["referencias_total"],
        "sanciones_cuantificadas": sanciones["referencias_con_monto"],
        "sanciones_sin_monto": sanciones["referencias_sin_monto"],
        **sanciones,
        "filas": [_fila_a_dict(f) for f in analisis.filas],
        "version_estados": analisis.version,
        "huella": (val.huella_documentos[:12] + "...") if val else "—",
        "validado_por": val.validado_por.nombre if val else "—",
        "validado_en": val.validado_en.date().isoformat() if val else "—",
    }


def renderizar(datos: dict) -> str:
    """Rellena la plantilla HTML con los datos (jinja2 autoescape = seguro ante XSS en los datos)."""
    plantilla = _RUTA_PLANTILLA.read_text(encoding="utf-8")
    return jinja2.Template(plantilla, autoescape=True).render(**datos)

"""Relleno de la plantilla HTML del reporte de prospección (Módulo 7). Regla pura: no llama a IA,
solo convierte el análisis validado en el HTML. El PDF es un render de datos ya validados (RN-040):
la plantilla HTML se rellena con jinja2 (autoescape) y se compila a PDF con WeasyPrint.

SANCIONES EN EL PDF (decisión 2026-10-19). El PDF distingue tres cosas:
- «Sanción agrupada»: varias menciones con identidad explícita (autoridad + identificador completo + una distinción
  coincidente), con TODAS sus citas y su procedencia.
- «Mención individual»: sin evidencia suficiente para unirla a otra. Si coincide con otra solo en autoridad e importe se
  marca «Posible sanción duplicada» y se lista aparte; nunca se une ni se descarta.
- «Mención sin agrupación evaluada»: de un documento procesado antes de existir la agrupación; no se presenta como deduplicada.
Los importes ambiguos conservan su texto original con «Importe que requiere revisión» y no se suman. El total monetario suma UN
importe por agrupación (nunca las menciones repetidas) y solo importes enteros inequívocos y sin escala. Se presenta únicamente
cuando es completo y fiable; si no (duplicados o conflictos sin resolver, importes por revisar, con escala o decimales, unidades
sin importe determinado, evaluación PARCIAL o sin evaluar) el PDF dice «Total monetario no disponible» con sus motivos, sin
ninguna suma parcial, y conserva los importes individuales, las citas y las advertencias. La disponibilidad se evalúa por unidad
de sanción: en una agrupación basta que una cita aporte el monto aunque otra lo omita.
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2

from app.modules.analisis_gri.schemas import AgrupacionSancion, AnalisisResponse, EstadoGri, SancionVista
from app.modules.ingesta.domain.agrupacion_sanciones import ADV_POSIBLE_DUPLICADA

_RUTA_PLANTILLA = Path(__file__).resolve().parent.parent / "plantillas" / "reporte_prospeccion.html"

# Clases CSS de los badges de estado (definidas en la plantilla).
_CLASE_ESTADO = {
    "OK": ("ok", "OK"),
    "BAJA_SUSTANCIA": ("baja", "Baja sustancia"),
    "SUB_REPORTADO": ("sub", "Sub-reportado"),
}

_ETIQUETA_IMPORTE = {
    "REQUIERE_REVISION": "Importe que requiere revisión",
    "NO_ESPECIFICADO": "Importe no especificado",
}
# Separadores de miles reconocidos: espacio, coma, punto, apóstrofos, espacio de no corte y espacio fino de no corte.
_SEPARADORES_DE_MILES = " ,.'’  "
_PATRON_SEPARADOR = re.compile("[" + re.escape(_SEPARADORES_DE_MILES) + "]")
_ESCALA = re.compile(r"\b(?:mil|millones?|mm)\b", re.IGNORECASE)
_ENTERO = re.compile(r"\d+")


def _fmt_puntaje(analisis: AnalisisResponse) -> str:
    valor = analisis.puntaje.valor_presentacion
    if valor is None:
        return "---"
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


# --- Importes ---------------------------------------------------------------------------------------------------------

def _valor_entero(monto) -> int | None:
    """Valor de un importe SOLO si es un entero inequívoco: cifras sin separadores o con un mismo separador de miles repetido,
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
    """El importe tal como está escrito; si la moneda sale del encabezado de la tabla se antepone para que se entienda."""
    if getattr(monto, "unidad_inicio", None) is not None and monto.unidad not in monto.texto:
        return f"{monto.unidad} {monto.texto}"
    return monto.texto


def _importes_de_unidad(estado: str, importes: list) -> tuple[bool, str]:
    """(tiene_monto, texto). Un importe que requiere revisión muestra TODOS sus textos originales; no se elige uno."""
    if estado == "CUANTIFICADO" and importes:
        return True, _texto_de_monto(importes[0])
    if estado == "REQUIERE_REVISION" and importes:
        return True, " / ".join(dict.fromkeys(_texto_de_monto(m) for m in importes))
    return False, _ETIQUETA_IMPORTE.get(estado, "No cuantificada")


def _sumas_por_unidad(importes: list) -> dict[str, int]:
    """Suma importes explícitos por moneda escrita, UNO por agrupación o mención. Excluye ambiguos, con escala o decimales."""
    sumas: dict[str, int] = {}
    for m in importes:
        valor = None if m.separador_ambiguo else _valor_entero(m)
        if valor is not None:
            sumas[m.unidad] = sumas.get(m.unidad, 0) + valor
    return sumas


# --- Sanciones --------------------------------------------------------------------------------------------------------

def _cita_a_dict(cita) -> dict:
    fuente = " · ".join(x for x in [cita.documento_nombre, cita.seccion] if x)
    return {"cita": cita.cita, "fuente": fuente, "posicion": f"{cita.cita_inicio}–{cita.cita_fin}"}


def _unidad_a_dict(a: AgrupacionSancion) -> dict:
    tiene_monto, monto = _importes_de_unidad(a.estado_importe.value, a.importes)
    citas = [_cita_a_dict(c) for c in a.citas]
    etiqueta = f"Sanción agrupada ({a.menciones} menciones con identidad explícita)" if a.agrupada else "Mención individual"
    return {
        "id": a.id,
        "tipo": "agrupada" if a.agrupada else "individual",
        "etiqueta": etiqueta,
        "entidad": a.autoridad.texto if a.autoridad else "(autoridad no identificada)",
        "identificador": " · ".join(dict.fromkeys(r.identificador for r in a.identificadores)),
        "numeral": a.numeral.texto if a.numeral else "",
        "infraccion": a.infraccion.texto if a.infraccion else "",
        "tiene_monto": tiene_monto,
        "monto": monto,
        "estado_importe": a.estado_importe.value,
        "citas": citas,
        "descripcion": citas[0]["cita"] if citas else "",
        "fuente": citas[0]["fuente"] if citas else "",
        "advertencias": [adv.mensaje for adv in a.advertencias],
    }


def _sancion_a_dict(s: SancionVista) -> dict:
    """Una mención de un documento procesado SIN agrupación: se muestra sola y rotulada, sin afirmar que esté deduplicada."""
    fuente = " · ".join(x for x in [s.documento_nombre, s.seccion] if x)
    if s.monto is None:
        estado = "NO_CONSTA"
    else:
        estado = "REQUIERE_REVISION" if s.monto.separador_ambiguo else "CUANTIFICADO"
    tiene_monto, monto = _importes_de_unidad(estado, [s.monto] if s.monto is not None else [])
    return {
        "id": "",
        "tipo": "sin_evaluar",
        "etiqueta": "Mención sin agrupación evaluada",
        "entidad": s.entidad.texto if s.entidad else "(autoridad no identificada)",
        "identificador": "",
        "numeral": "",
        "infraccion": "",
        "tiene_monto": tiene_monto,
        "monto": monto,
        "estado_importe": estado,
        "citas": [{"cita": s.cita, "fuente": fuente, "posicion": f"{s.cita_inicio}–{s.cita_fin}"}],
        "descripcion": s.cita,
        "fuente": fuente,
        "advertencias": [_ETIQUETA_IMPORTE["REQUIERE_REVISION"]] if estado == "REQUIERE_REVISION" else [],
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


def _importes_a_sumar(analisis: AnalisisResponse) -> tuple[list, int, int]:
    """(importes, excluidos, sin_importe), POR UNIDAD de sanción. Una agrupación aporta UN importe aunque tenga varias
    menciones; solo las CUANTIFICADAS suman. `sin_importe` cuenta las unidades NO_ESPECIFICADO o NO_CONSTA (y las menciones
    sin agrupación evaluada que no traen monto): sin su importe no hay un total completo. Que una cita de una agrupación
    omita el monto que otra sí trae no cuenta: el estado se evalúa sobre la unidad entera."""
    unidades = getattr(analisis, "agrupaciones_sanciones", None) or []
    sin_unidad = [s for s in analisis.sanciones if getattr(s, "agrupacion_id", None) is None]
    importes = [a.importes[0] for a in unidades if a.estado_importe.value == "CUANTIFICADO" and a.importes]
    excluidos = sum(a.estado_importe.value == "REQUIERE_REVISION" for a in unidades)
    sin_importe = sum(a.estado_importe.value in ("NO_ESPECIFICADO", "NO_CONSTA") for a in unidades)
    for s in sin_unidad:
        if s.monto is None:
            sin_importe += 1
        elif s.monto.separador_ambiguo:
            excluidos += 1
        else:
            importes.append(s.monto)
    return importes, excluidos, sin_importe


def _motivos_del_total(resumen, excluidos: int, sumados: int, a_sumar: int, sin_importe: int = 0) -> list[str]:
    """Por qué no puede calcularse un total completo y fiable. Vacío = no queda nada por resolver (sigue siendo una detección léxica)."""
    evaluacion = getattr(getattr(resumen, "evaluacion_agrupacion", None), "value", "NO_EVALUADA")
    motivos = []
    if evaluacion == "PARCIAL":
        motivos.append("la evaluación de agrupación es PARCIAL: pueden quedar menciones repetidas sin comparar")
    if getattr(resumen, "menciones_sin_evaluar", 0) or evaluacion == "NO_EVALUADA":
        motivos.append("hay menciones cuya agrupación no se evaluó: pueden estar repetidas")
    if getattr(resumen, "posibles_duplicados", 0):
        motivos.append("incluye posibles duplicados sin resolver")
    if getattr(resumen, "unidades_en_conflicto", 0):
        motivos.append("hay datos en conflicto sin resolver")
    if excluidos or getattr(resumen, "importes_a_revisar", 0):
        motivos.append("hay importes que requieren revisión y no se suman")
    if sumados < a_sumar:
        motivos.append("hay importes con escala o decimales que no se suman")
    if sin_importe:
        motivos.append("hay sanciones sin importe determinado: una suma parcial no es un total completo")
    return motivos


def _resumen_de_agrupacion(resumen) -> dict:
    return {
        "evaluacion": getattr(getattr(resumen, "evaluacion_agrupacion", None), "value", "NO_EVALUADA"),
        "unidades": getattr(resumen, "unidades", None),
        "agrupaciones": getattr(resumen, "agrupaciones_identificadas", 0),
        "menciones_agrupadas": getattr(resumen, "menciones_agrupadas", 0),
        "sin_evaluar": getattr(resumen, "menciones_sin_evaluar", 0),
        "posibles_duplicados": getattr(resumen, "posibles_duplicados", 0),
        "conflictos": getattr(resumen, "unidades_en_conflicto", 0),
        "importes_a_revisar": getattr(resumen, "importes_a_revisar", 0),
    }


def _datos_de_sanciones(analisis: AnalisisResponse) -> dict:
    resumen = analisis.resumen_sanciones
    unidades = getattr(analisis, "agrupaciones_sanciones", None) or []
    tarjetas = [_unidad_a_dict(a) for a in unidades]
    tarjetas += [_sancion_a_dict(s) for s in analisis.sanciones if getattr(s, "agrupacion_id", None) is None]
    por_id = {t["id"]: t for t in tarjetas if t["id"]}
    importes, excluidos, sin_importe = _importes_a_sumar(analisis)
    sumas = _sumas_por_unidad(importes)
    sumados = sum(1 for m in importes if _valor_entero(m) is not None)
    motivos = _motivos_del_total(resumen, excluidos, sumados, len(importes), sin_importe)
    return {
        "sanciones": tarjetas,
        "posibles_duplicados": [[por_id[i] for i in grupo] for grupo in _grupos_de_posibles_duplicados(unidades)],
        "agrupacion": _resumen_de_agrupacion(resumen),
        # Solo hay total cuando es completo y fiable. Si no, no se muestra ninguna suma parcial; los importes individuales
        # y sus advertencias siguen en `sanciones`.
        "money": [] if motivos else [{"unidad": u, "total": f"{v:,.0f}"} for u, v in sumas.items()],
        "money_disponible": not motivos,
        "money_motivos": motivos,
    }


def datos_desde_analisis(analisis: AnalisisResponse, fecha_generacion: str) -> dict:
    """Convierte el análisis validado en el diccionario de la plantilla (valores crudos; jinja2 los escapa)."""
    empresa = analisis.empresa
    san = analisis.resumen_sanciones
    val = analisis.validacion

    conteo = {"ok": 0, "baja": 0, "sub": 0}
    for f in analisis.filas:
        if f.estado is EstadoGri.OK:
            conteo["ok"] += 1
        elif f.estado is EstadoGri.BAJA_SUSTANCIA:
            conteo["baja"] += 1
        elif f.estado is EstadoGri.SUB_REPORTADO:
            conteo["sub"] += 1

    return {
        "empresa_nombre": empresa.nombre,
        "anio": analisis.anio,
        "sector": getattr(empresa.sector, "value", empresa.sector),
        "fecha_generacion": fecha_generacion,
        "puntaje": _fmt_puntaje(analisis),
        "conteo_ok": conteo["ok"], "conteo_baja": conteo["baja"], "conteo_sub": conteo["sub"],
        # Son MENCIONES (apariciones en el texto), no sanciones únicas.
        "sanciones_total": san.total,
        "sanciones_cuantificadas": san.cuantificadas,
        "sanciones_sin_monto": san.no_cuantificadas,
        **_datos_de_sanciones(analisis),
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

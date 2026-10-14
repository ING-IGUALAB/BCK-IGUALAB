"""Cálculo PURO del análisis GRI (Módulo 5): agrupa en filas evaluables los resultados que la ingesta ya persistió en
`documentos.analisis`, aplica los estados humanos y calcula puntaje y pendientes. No usa la base de datos, FastAPI ni ningún
proveedor, y NO vuelve a detectar, generar embeddings ni modificar citas: solo reorganiza la evidencia existente.

REGLAS (acuerdos del usuario)
- Una fila evaluable es una IDENTIDAD GRI: código + edición. Las menciones de un mismo estándar (`204-1`, `GRI 204`...), aunque
  sean 20 o estén en varios documentos de la misma empresa/año, forman UNA fila, con todas sus evidencias, un estado y una
  contribución al puntaje.
- Ediciones distintas son filas distintas. Una identidad ambigua (estándar con varias ediciones y el texto no dice cuál) o con
  una edición que el catálogo no tiene se conserva como su propia fila, con sus advertencias: nunca se inventa la edición ni se
  fusiona con una identidad resuelta.
- Las referencias a códigos que el catálogo no conoce se muestran como advertencias y NO son filas ni puntúan.
- Identidad: `305:2016` (resuelta), `102:ambigua`, `102:no-catalogada-2030`. Es estable entre documentos y entre consultas.
- El sistema no propone, infiere ni busca estados: `estado` es null hasta que un Administrador lo asigna.

PUNTAJE (RN-032): promedio simple de las filas evaluables, OK = 100, Baja sustancia = 50, Sub-reportado = 0. Sin filas
evaluables no hay puntaje («no disponible»); nunca es cero. Todas Sub-reportadas SÍ da cero. Mientras falte algún estado no se
presenta valor. El REDONDEO es solo de presentación: `valor` conserva el promedio exacto y `valor_presentacion` lo redondea a
`DECIMALES_PRESENTACION` decimales, mitad hacia arriba, con aritmética entera (sin errores de coma flotante).
"""
import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.exceptions import InternalProcessingError
from app.models.analisis_gri import PUNTOS_POR_ESTADO, EstadoGri, TipoIdentidadGri
from app.models.documento_ingesta import ResultadoAnalisis
from app.models.documentos import TipoDocumento
from app.schemas_analisis import (
    ETIQUETAS_ESTADO,
    ActorResumen,
    AdvertenciaAnalisis,
    AdvertenciaFila,
    CalificadorVista,
    DatoTexto,
    DocumentoElegible,
    EstadoFlujo,
    EstadoPuntaje,
    Evidencia,
    FilaGri,
    MontoVista,
    MotivoNoVigente,
    Pendiente,
    PuntajeEsg,
    ResumenSanciones,
    SancionVista,
)
from app.services.ingesta.analisis_ingesta import (
    ADV_GRI_DESCONOCIDA,
    ADV_GRI_EDICION_AMBIGUA,
    ADV_GRI_EDICION_NO_CATALOGADA,
    ADV_GRI_FORMATO,
    CATEGORIA_CALIDAD,
    CATEGORIA_GRI,
)

DECIMALES_PRESENTACION = 1
MAX_REFERENCIAS_ADVERTENCIA = 20
CODIGO_ERROR_DATOS_ALMACENADOS = "ANALYSIS_STORED_DATA_INVALID"

_MOTIVO_A_ADVERTENCIA = {
    "edicion_ambigua": (
        ADV_GRI_EDICION_AMBIGUA,
        "Estándar con varias ediciones y sin edición en el texto. Se conserva como identidad ambigua; no se asumió ninguna edición.",
    ),
    "edicion_no_catalogada": (
        ADV_GRI_EDICION_NO_CATALOGADA,
        "El texto indica una edición que el catálogo no tiene para ese estándar. Se conserva con la edición escrita.",
    ),
    "formato_no_reconocido": (
        ADV_GRI_FORMATO,
        "Referencias con formato no reconocido o incongruente con el tipo de estándar; se conservan tal cual.",
    ),
    "formato_incongruente": (
        ADV_GRI_FORMATO,
        "Referencias con formato no reconocido o incongruente con el tipo de estándar; se conservan tal cual.",
    ),
}


def _datos_invalidos() -> InternalProcessingError:
    return InternalProcessingError(
        CODIGO_ERROR_DATOS_ALMACENADOS,
        "El análisis almacenado de un documento no tiene la estructura esperada.",
    )


@dataclass(frozen=True)
class DocumentoBase:
    """Documento elegible (ya filtrado por el servicio) con su análisis persistido."""

    id: uuid.UUID
    nombre_archivo: str
    sha256: str
    tipo: TipoDocumento
    resultado_analisis: ResultadoAnalisis
    completado_en: datetime
    analisis: dict


@dataclass(frozen=True)
class EstadoAsignado:
    identidad: str
    estado: EstadoGri
    version: int
    asignado_por: ActorResumen
    asignado_en: datetime


@dataclass(frozen=True)
class ValidacionGuardada:
    numero: int
    version_estados: int
    huella_documentos: str
    documentos: list
    estados: list
    puntaje_suma: int
    puntaje_filas: int
    validado_por: ActorResumen
    validado_en: datetime


# --- Identidad -------------------------------------------------------------------------------------------------------

def clave_identidad(codigo: str, edicion_resuelta: str | None, edicion_mencionada: str | None) -> tuple[str, TipoIdentidadGri, str | None]:
    """(clave, tipo, edición) de una mención. Nunca inventa la edición."""
    if edicion_resuelta:
        return f"{codigo}:{edicion_resuelta}", TipoIdentidadGri.EDICION_RESUELTA, edicion_resuelta
    if edicion_mencionada:
        return f"{codigo}:no-catalogada-{edicion_mencionada}", TipoIdentidadGri.EDICION_NO_CATALOGADA, edicion_mencionada
    return f"{codigo}:ambigua", TipoIdentidadGri.EDICION_AMBIGUA, None


def _etiqueta_estandar(codigo: str, tipo: TipoIdentidadGri, edicion: str | None) -> str:
    if tipo is TipoIdentidadGri.EDICION_RESUELTA:
        return f"GRI {codigo} ({edicion})"
    if tipo is TipoIdentidadGri.EDICION_NO_CATALOGADA:
        return f"GRI {codigo} (edición {edicion} no catalogada)"
    return f"GRI {codigo} (edición sin resolver)"


def huella_de_documentos(documentos: list[DocumentoBase]) -> str:
    """SHA-256 del conjunto de documentos elegibles (id + hash del contenido, ordenado). Cambia si se incorpora, sale o se
    sustituye un documento elegible de la empresa/año."""
    pares = sorted([str(d.id), d.sha256] for d in documentos)
    return hashlib.sha256(json.dumps(pares, separators=(",", ":")).encode("utf-8")).hexdigest()


# --- Evidencia -------------------------------------------------------------------------------------------------------

def _evidencia(doc: DocumentoBase, mencion: dict) -> Evidencia:
    try:
        return Evidencia(
            documento_id=doc.id,
            documento_nombre=doc.nombre_archivo,
            seccion=mencion["seccion"],
            rol=mencion["rol"],
            referencia_original=mencion["referencia_original"],
            referencia_normalizada=mencion["referencia_normalizada"],
            cita=mencion["cita"],
            inicio=mencion["inicio"],
            fin=mencion["fin"],
            cita_inicio=mencion["cita_inicio"],
            cita_fin=mencion["cita_fin"],
            en_codigo=bool(mencion.get("en_codigo", False)),
            edicion_mencionada=mencion.get("edicion_mencionada"),
            motivos_revision=list(mencion.get("motivos_revision") or []),
        )
    except (KeyError, TypeError, ValueError):
        raise _datos_invalidos() from None


def _grupos_gri(doc: DocumentoBase) -> list[dict]:
    grupos = doc.analisis.get("gri")
    if not isinstance(grupos, list):
        raise _datos_invalidos()
    return grupos


@dataclass
class _Acumulador:
    codigo: str
    edicion: str | None
    tipo: TipoIdentidadGri
    nombre: str | None = None
    candidatos: dict[tuple[str, str], str] = field(default_factory=dict)
    menciones: list[tuple[DocumentoBase, dict]] = field(default_factory=list)


def _advertencias_de_fila(menciones: list[tuple[DocumentoBase, dict]]) -> list[AdvertenciaFila]:
    por_codigo: dict[str, tuple[str, list[dict]]] = {}
    for _, mencion in menciones:
        vistos: set[str] = set()
        for motivo in mencion.get("motivos_revision") or []:
            if motivo not in _MOTIVO_A_ADVERTENCIA:
                continue
            codigo, mensaje = _MOTIVO_A_ADVERTENCIA[motivo]
            if codigo in vistos:  # una mención con dos motivos del mismo aviso cuenta una vez
                continue
            vistos.add(codigo)
            por_codigo.setdefault(codigo, (mensaje, []))[1].append(mencion)
    return [
        AdvertenciaFila(
            codigo=codigo,
            mensaje=mensaje,
            total_menciones=len(lista),
            referencias=[m["referencia_original"] for m in lista[:MAX_REFERENCIAS_ADVERTENCIA]],
        )
        for codigo, (mensaje, lista) in sorted(por_codigo.items())
    ]


def construir_filas(documentos: list[DocumentoBase]) -> tuple[list[FilaGri], list[AdvertenciaAnalisis]]:
    """Filas evaluables (sin estados) y advertencias de referencias desconocidas y de calidad, en orden estable.
    `documentos` ya viene en orden determinista (completado_en, id)."""
    acumuladores: dict[str, _Acumulador] = {}
    advertencias: list[AdvertenciaAnalisis] = []
    for doc in documentos:
        for grupo in _grupos_gri(doc):
            try:
                codigo = grupo["codigo"]
                menciones = grupo["menciones"]
                if not grupo.get("catalogado"):
                    # Referencia desconocida: advertencia con su evidencia; no es fila ni puntúa.
                    advertencias.append(
                        AdvertenciaAnalisis(
                            codigo=ADV_GRI_DESCONOCIDA,
                            categoria=CATEGORIA_GRI,
                            mensaje="Referencia a un código que el catálogo no conoce. Se conserva sin corregirla, "
                            "no es una fila evaluable y no entra en el puntaje.",
                            documento_id=doc.id,
                            documento_nombre=doc.nombre_archivo,
                            detalles={"codigo_estandar": codigo, "total_menciones": len(menciones)},
                            evidencias=[_evidencia(doc, m) for m in menciones],
                        )
                    )
                    continue
                for mencion in menciones:
                    clave, tipo, edicion = clave_identidad(
                        codigo, grupo.get("edicion"), mencion.get("edicion_mencionada")
                    )
                    acc = acumuladores.get(clave)
                    if acc is None:
                        acc = acumuladores[clave] = _Acumulador(codigo=codigo, edicion=edicion, tipo=tipo)
                    if grupo.get("nombre") and acc.nombre is None and tipo is TipoIdentidadGri.EDICION_RESUELTA:
                        acc.nombre = grupo["nombre"]
                    for candidato in grupo.get("candidatos") or []:
                        acc.candidatos[(candidato["codigo"], candidato["edicion"])] = (
                            f"{candidato['edicion']}: {candidato['nombre']}"
                        )
                    acc.menciones.append((doc, mencion))
            except (KeyError, TypeError):
                raise _datos_invalidos() from None
        advertencias.extend(_advertencias_de_calidad(doc))

    filas = []
    for clave, acc in acumuladores.items():
        evidencias = [_evidencia(doc, m) for doc, m in acc.menciones]
        documentos_fila = list(dict.fromkeys(doc.id for doc, _ in acc.menciones))
        filas.append(
            FilaGri(
                identidad=clave,
                codigo=acc.codigo,
                edicion=acc.edicion,
                identidad_tipo=acc.tipo,
                estandar=_etiqueta_estandar(acc.codigo, acc.tipo, acc.edicion),
                nombre=acc.nombre,
                candidatos=sorted(acc.candidatos.values()),
                requiere_revision=acc.tipo is not TipoIdentidadGri.EDICION_RESUELTA
                or any(m.get("motivos_revision") for _, m in acc.menciones),
                estado=None,
                estado_etiqueta=None,
                version_fila=0,
                total_menciones=len(evidencias),
                menciones_en_indice=sum(e.rol == "indice" for e in evidencias),
                menciones_en_cuerpo=sum(e.rol == "cuerpo" for e in evidencias),
                documentos=documentos_fila,
                advertencias=_advertencias_de_fila(acc.menciones),
                evidencias=evidencias,
            )
        )
    filas.sort(key=lambda f: (int(f.codigo), f.edicion is None, f.edicion or "", f.identidad))
    return filas, advertencias


def _advertencias_de_calidad(doc: DocumentoBase) -> list[AdvertenciaAnalisis]:
    """Avisos de calidad del documento que dejó la ingesta (p. ej. tablas con columnas inconsistentes). Los avisos de GRI y de
    sanciones no se repiten: ya aparecen en cada fila y en cada sanción."""
    guardadas = doc.analisis.get("advertencias")
    if not isinstance(guardadas, list):
        raise _datos_invalidos()
    return [
        AdvertenciaAnalisis(
            codigo=a["codigo"],
            categoria=a["categoria"],
            mensaje=a["mensaje"],
            documento_id=doc.id,
            documento_nombre=doc.nombre_archivo,
            detalles=a.get("detalles") or {},
        )
        for a in guardadas
        if a.get("categoria") == CATEGORIA_CALIDAD
    ]


def aplicar_estados(filas: list[FilaGri], estados: dict[str, EstadoAsignado]) -> list[FilaGri]:
    """Copia de las filas con el estado actual de cada una. Estados de identidades que ya no aparecen (documento no elegible) se
    conservan en la base y en el historial, pero no puntúan ni se muestran."""
    resultado = []
    for fila in filas:
        asignado = estados.get(fila.identidad)
        if asignado is None:
            resultado.append(fila)
            continue
        resultado.append(
            fila.model_copy(
                update={
                    "estado": asignado.estado,
                    "estado_etiqueta": ETIQUETAS_ESTADO[asignado.estado],
                    "version_fila": asignado.version,
                    "asignado_por": asignado.asignado_por,
                    "asignado_en": asignado.asignado_en,
                }
            )
        )
    return resultado


# --- Sanciones -------------------------------------------------------------------------------------------------------

def _dato(valor: dict | None) -> DatoTexto | None:
    if valor is None:
        return None
    return DatoTexto(texto=valor["texto"], inicio=valor["inicio"], fin=valor["fin"])


def construir_sanciones(documentos: list[DocumentoBase]) -> tuple[list[SancionVista], ResumenSanciones]:
    sanciones: list[SancionVista] = []
    for doc in documentos:
        guardadas = doc.analisis.get("sanciones")
        if not isinstance(guardadas, list):
            raise _datos_invalidos()
        try:
            for s in guardadas:
                monto = s.get("monto")
                sanciones.append(
                    SancionVista(
                        documento_id=doc.id,
                        documento_nombre=doc.nombre_archivo,
                        tipo_termino=s["tipo_termino"],
                        referencia_original=s["referencia_original"],
                        cita=s["cita"],
                        inicio=s["inicio"],
                        fin=s["fin"],
                        cita_inicio=s["cita_inicio"],
                        cita_fin=s["cita_fin"],
                        seccion=s["seccion"],
                        en_tabla=bool(s.get("en_tabla", False)),
                        entidad=_dato(s.get("entidad")),
                        monto=None
                        if monto is None
                        else MontoVista(
                            texto=monto["texto"],
                            unidad=monto["unidad"],
                            numero_texto=monto["numero_texto"],
                            separador_ambiguo=bool(monto["separador_ambiguo"]),
                            vinculo=monto["vinculo"],
                            inicio=monto["inicio"],
                            fin=monto["fin"],
                        ),
                        cuantificada=monto is not None,
                        periodo=_dato(s.get("periodo")),
                        calificadores=[
                            CalificadorVista(codigo=c["codigo"], texto=c["texto"], inicio=c["inicio"], fin=c["fin"])
                            for c in s.get("calificadores") or []
                        ],
                        motivos_revision=list(s.get("motivos_revision") or []),
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise _datos_invalidos() from None
    por_unidad: dict[str, int] = defaultdict(int)
    for s in sanciones:
        if s.monto is not None:
            por_unidad[s.monto.unidad] += 1
    cuantificadas = sum(s.cuantificada for s in sanciones)
    resumen = ResumenSanciones(
        total=len(sanciones),
        cuantificadas=cuantificadas,
        no_cuantificadas=len(sanciones) - cuantificadas,
        por_unidad=dict(sorted(por_unidad.items())),
    )
    return sanciones, resumen


def resumir_documentos(documentos: list[DocumentoBase]) -> list[DocumentoElegible]:
    resultado = []
    for doc in documentos:
        grupos, sanciones = doc.analisis.get("gri"), doc.analisis.get("sanciones")
        if not isinstance(grupos, list) or not isinstance(sanciones, list):
            raise _datos_invalidos()
        resultado.append(
            DocumentoElegible(
                documento_id=doc.id,
                nombre_archivo=doc.nombre_archivo,
                tipo=doc.tipo,
                resultado_analisis=doc.resultado_analisis,
                observado=doc.resultado_analisis is ResultadoAnalisis.OBSERVADO,
                completado_en=doc.completado_en,
                version_catalogo=str(doc.analisis.get("version_catalogo", "")),
                total_referencias_gri=sum(len(g.get("menciones") or []) for g in grupos),
                total_sanciones=len(sanciones),
            )
        )
    return resultado


# --- Puntaje ---------------------------------------------------------------------------------------------------------

def redondear_presentacion(suma: int, filas: int, decimales: int = DECIMALES_PRESENTACION) -> float:
    """`suma / filas` redondeado a `decimales`, mitad hacia arriba, con enteros. Solo para mostrar."""
    escala = 10**decimales
    return ((2 * suma * escala + filas) // (2 * filas)) / escala


def calcular_puntaje(filas: list[FilaGri], *, vigente: bool, hay_documentos: bool) -> PuntajeEsg:
    evaluables = len(filas)
    sin_estado = sum(f.estado is None for f in filas)
    base = dict(decimales_presentacion=DECIMALES_PRESENTACION, filas_evaluables=evaluables, filas_sin_estado=sin_estado)
    if evaluables == 0:
        motivo = "SIN_CODIGOS_GRI_EVALUABLES" if hay_documentos else "DOCUMENTACION_INSUFICIENTE"
        mensaje = (
            "Puntaje ESG no disponible: los documentos elegibles no contienen códigos GRI evaluables."
            if hay_documentos
            else "Puntaje ESG no disponible: no hay documentos elegibles para esta empresa y año."
        )
        return PuntajeEsg(
            estado=EstadoPuntaje.NO_DISPONIBLE, definitivo=False, valor=None, valor_presentacion=None,
            suma_puntos=None, filas_contadas=0, motivo=motivo, mensaje=mensaje, **base,
        )
    if sin_estado:
        return PuntajeEsg(
            estado=EstadoPuntaje.PENDIENTE_ESTADOS, definitivo=False, valor=None, valor_presentacion=None,
            suma_puntos=None, filas_contadas=evaluables - sin_estado, motivo="PENDIENTE_ESTADOS",
            mensaje=f"Faltan estados por asignar en {sin_estado} de {evaluables} filas: no se presenta puntaje.", **base,
        )
    suma = sum(PUNTOS_POR_ESTADO[f.estado] for f in filas)
    estado = EstadoPuntaje.VALIDADO if vigente else EstadoPuntaje.CALCULADO_SIN_VALIDAR
    return PuntajeEsg(
        estado=estado, definitivo=vigente, valor=suma / evaluables,
        valor_presentacion=redondear_presentacion(suma, evaluables), suma_puntos=suma, filas_contadas=evaluables,
        motivo=None,
        mensaje=None if vigente else "Calculado con los estados asignados; no es definitivo hasta «Validar análisis».", **base,
    )


# --- Vigencia y flujo ------------------------------------------------------------------------------------------------

def motivos_no_vigente(validacion: ValidacionGuardada | None, version: int, huella: str) -> list[MotivoNoVigente]:
    """Vacío = la validación sigue vigente. Cualquier cambio de estado posterior (aunque se revierta) cambia la versión."""
    if validacion is None:
        return []
    motivos = []
    if validacion.version_estados != version:
        motivos.append(MotivoNoVigente.ESTADOS_CAMBIARON)
    if validacion.huella_documentos != huella:
        motivos.append(MotivoNoVigente.DOCUMENTOS_CAMBIARON)
    return motivos


def determinar_flujo(*, hay_documentos: bool, filas: list[FilaGri], vigente: bool) -> EstadoFlujo:
    if not hay_documentos:
        return EstadoFlujo.DOCUMENTACION_INSUFICIENTE
    if not filas:
        return EstadoFlujo.SIN_CODIGOS_GRI
    if any(f.estado is None for f in filas):
        return EstadoFlujo.PENDIENTE_ESTADOS
    return EstadoFlujo.VALIDADO if vigente else EstadoFlujo.LISTO_PARA_VALIDAR


def construir_pendientes(
    *,
    empresa_activa: bool,
    hay_documentos: bool,
    filas: list[FilaGri],
    validacion: ValidacionGuardada | None,
    motivos: list[MotivoNoVigente],
    flujo: EstadoFlujo,
) -> list[Pendiente]:
    pendientes: list[Pendiente] = []
    if not empresa_activa:
        pendientes.append(Pendiente(
            codigo="EMPRESA_INACTIVA",
            mensaje="La empresa está inactiva: sus documentos no son elegibles para el análisis.",
        ))
    if not hay_documentos:
        pendientes.append(Pendiente(
            codigo="DOCUMENTACION_INSUFICIENTE",
            mensaje="No hay documentos elegibles (completados, con análisis y publicación vectorial confirmada) para esta empresa y año.",
            cantidad=0,
        ))
        return pendientes
    if not filas:
        pendientes.append(Pendiente(
            codigo="SIN_CODIGOS_GRI",
            mensaje="Ningún documento elegible contiene códigos GRI evaluables: el puntaje ESG no está disponible y el reporte "
            "de prospección no se habilita (las sanciones y los observados no lo habilitan por sí solos).",
            cantidad=0,
        ))
        return pendientes
    sin_estado = [f.identidad for f in filas if f.estado is None]
    if sin_estado:
        pendientes.append(Pendiente(
            codigo="ESTADOS_PENDIENTES",
            mensaje=f"Faltan estados por asignar en {len(sin_estado)} de {len(filas)} filas.",
            cantidad=len(sin_estado),
            identidades=sin_estado,
        ))
    elif flujo is EstadoFlujo.LISTO_PARA_VALIDAR:
        if validacion is None:
            pendientes.append(Pendiente(
                codigo="VALIDACION_PENDIENTE", mensaje="Todas las filas tienen estado: falta confirmar «Validar análisis»."
            ))
        else:
            pendientes.append(Pendiente(
                codigo="VALIDACION_NO_VIGENTE",
                mensaje="La validación anterior ya no es vigente (" + ", ".join(m.value for m in motivos)
                + "): hay que volver a validar.",
            ))
    return pendientes


def estados_en_foto(filas: list[FilaGri]) -> list[dict[str, Any]]:
    """Foto de los estados que se guarda en la validación."""
    return [
        {"identidad": f.identidad, "codigo": f.codigo, "edicion": f.edicion, "estado": f.estado.value}
        for f in filas
    ]


def documentos_en_foto(documentos: list[DocumentoBase]) -> list[dict[str, Any]]:
    return [
        {"documento_id": str(d.id), "nombre_archivo": d.nombre_archivo, "sha256": d.sha256}
        for d in sorted(documentos, key=lambda d: str(d.id))
    ]

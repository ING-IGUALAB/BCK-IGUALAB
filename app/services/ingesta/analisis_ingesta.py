"""Resultado combinado del análisis de ingesta: GRI + sanciones (decisión 2026-10-07).

Ejecuta los dos detectores sobre el MISMO texto interpretado y clasifica el análisis:

- CON_HALLAZGOS: hay al menos una referencia a un estándar GRI que el catálogo
  conoce (cualquier edición, también ambigua o no catalogada) o al menos una
  sanción económica detectada.
- OBSERVADO: ambos detectores terminaron bien y no encontraron ninguno de esos dos
  hallazgos. Sigue siendo un documento válido para RAG (ver 06-acuerdos).

Reglas:
- Una referencia a un estándar conocido con edición ambigua NO es ausencia de GRI:
  cuenta como hallazgo y queda señalada para revisión.
- Una referencia a un código que el catálogo no conoce no cuenta como hallazgo ni se
  presume del catálogo: se conserva como advertencia. Un documento con solo
  referencias desconocidas y sin sanciones es OBSERVADO.
- Si un detector falla se lanza `AnalisisIngestaError`; nunca se devuelve OBSERVADO ni
  un resultado parcial. Detector no ejecutado o con error = análisis pendiente.
- Las advertencias de calidad (p. ej. tablas con columnas inconsistentes) viajan en el
  resultado pero NO influyen en la clasificación.
- No asigna estados de cumplimiento (OK, Baja sustancia, Sub-reportado) ni puntaje ESG.

Este módulo NO marca documentos como COMPLETADO, no persiste, no publica fragmentos
y no está conectado a `documento_service`: es la pieza que invocará el futuro
coordinador de ingesta. `a_dict()` entrega una forma serializable en JSON para que ese
coordinador la persista (el esquema de persistencia aún no existe ni está aprobado).
"""
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

from app.exceptions import AppException
from app.models.documento_ingesta import ResultadoAnalisis
from app.services.ingesta.catalogo_gri import CatalogoGri, catalogo_predeterminado
from app.services.ingesta.deteccion_gri import (
    GrupoEstandarGri,
    ResultadoDeteccionGri,
    detectar_referencias_gri,
)
from app.services.ingesta.deteccion_sanciones import ResultadoSanciones, detectar_sanciones
from app.services.ingesta.validacion import AdvertenciaCalidad, DocumentoValidado

CODIGO_ERROR_ANALISIS = "INGESTION_ANALYSIS_FAILED"
MAX_DETALLES_ADVERTENCIA = 20

MOTIVO_REFERENCIAS_GRI = "referencias_gri_catalogadas"
MOTIVO_SANCIONES = "sanciones_economicas"
MOTIVO_SIN_HALLAZGOS = "sin_referencias_gri_catalogadas_ni_sanciones_economicas"

CATEGORIA_CALIDAD = "calidad_documento"
CATEGORIA_GRI = "gri"
CATEGORIA_SANCIONES = "sanciones"

ADV_GRI_DESCONOCIDA = "GRI_REFERENCIA_DESCONOCIDA"
ADV_GRI_EDICION_AMBIGUA = "GRI_EDICION_AMBIGUA"
ADV_GRI_EDICION_NO_CATALOGADA = "GRI_EDICION_NO_CATALOGADA"
ADV_GRI_FORMATO = "GRI_FORMATO_A_REVISAR"
ADV_SANCION_A_REVISAR = "SANCION_A_REVISAR"


class AnalisisIngestaError(AppException):
    """Un detector falló. El análisis NO se completó y no debe tratarse como OBSERVADO.

    `details` identifica el detector y la clase de la excepción; nunca el mensaje de
    la causa ni contenido del documento.
    """


@dataclass(frozen=True, slots=True)
class AdvertenciaAnalisis:
    codigo: str
    categoria: str
    mensaje: str
    detalles: dict


@dataclass(frozen=True)
class ResultadoAnalisisIngesta:
    resultado: ResultadoAnalisis
    motivos: tuple[str, ...]
    version_catalogo: str
    gri: ResultadoDeteccionGri
    sanciones: ResultadoSanciones
    advertencias: tuple[AdvertenciaAnalisis, ...]

    @property
    def hay_referencias_gri_catalogadas(self) -> bool:
        return MOTIVO_REFERENCIAS_GRI in self.motivos

    @property
    def hay_sanciones(self) -> bool:
        return MOTIVO_SANCIONES in self.motivos

    def a_dict(self) -> dict:
        """Forma serializable en JSON (solo dict, list, str, int, bool y None) para el
        coordinador. Los datos ausentes son None."""
        datos = {
            "resultado": self.resultado.value,
            "motivos": list(self.motivos),
            "version_catalogo": self.version_catalogo,
            "gri": [
                {
                    "codigo": grupo.codigo,
                    "edicion": grupo.edicion,
                    "nombre": grupo.nombre,
                    "identidad": grupo.identidad,
                    "catalogado": grupo.catalogado,
                    "candidatos": [
                        {"codigo": c.codigo, "edicion": c.edicion, "nombre": c.nombre} for c in grupo.candidatos
                    ],
                    "motivos_revision": list(grupo.motivos_revision),
                    "menciones": [asdict(m) for m in grupo.menciones],
                }
                for grupo in self.gri.grupos
            ],
            "sanciones": [asdict(s) for s in self.sanciones.sanciones],
            "descartes_sanciones": dict(self.sanciones.descartes),
            "advertencias": [asdict(a) for a in self.advertencias],
        }
        return json.loads(json.dumps(datos, ensure_ascii=False))


def _fallo(detector: str, exc: Exception) -> AnalisisIngestaError:
    return AnalisisIngestaError(
        CODIGO_ERROR_ANALISIS,
        "El análisis del documento no pudo completarse; no se clasifica como observado.",
        details={"detector": detector, "tipo_error": type(exc).__name__},
    )


def _advertencias_gri(grupos: Sequence[GrupoEstandarGri]) -> list[AdvertenciaAnalisis]:
    advertencias = []
    por_motivo = (
        ("estandar_no_catalogado", ADV_GRI_DESCONOCIDA,
         "Referencia a un código que el catálogo no conoce. Se conserva sin corregirla ni asumir que es un estándar GRI."),
        ("edicion_ambigua", ADV_GRI_EDICION_AMBIGUA,
         "Estándar conocido con varias ediciones y sin edición en el texto. Cuenta como referencia GRI y queda para revisión."),
        ("edicion_no_catalogada", ADV_GRI_EDICION_NO_CATALOGADA,
         "El texto indica una edición que el catálogo no tiene para ese estándar."),
    )
    for grupo in sorted(grupos, key=lambda g: (int(g.codigo), g.edicion or "")):
        for motivo, codigo, mensaje in por_motivo:
            menciones = [m for m in grupo.menciones if motivo in m.motivos_revision]
            if not menciones:
                continue
            advertencias.append(
                AdvertenciaAnalisis(
                    codigo=codigo,
                    categoria=CATEGORIA_GRI,
                    mensaje=mensaje,
                    detalles={
                        "codigo_estandar": grupo.codigo,
                        "candidatos": [f"{c.codigo}:{c.edicion}" for c in grupo.candidatos],
                        "total_menciones": len(menciones),
                        "referencias": [m.referencia_original for m in menciones[:MAX_DETALLES_ADVERTENCIA]],
                    },
                )
            )
        formato = [
            m for m in grupo.menciones
            if {"formato_no_reconocido", "formato_incongruente"} & set(m.motivos_revision)
        ]
        if formato:
            advertencias.append(
                AdvertenciaAnalisis(
                    codigo=ADV_GRI_FORMATO,
                    categoria=CATEGORIA_GRI,
                    mensaje="Referencias con formato no reconocido o incongruente con el tipo de estándar; se conservan tal cual.",
                    detalles={
                        "codigo_estandar": grupo.codigo,
                        "total_menciones": len(formato),
                        "referencias": [m.referencia_original for m in formato[:MAX_DETALLES_ADVERTENCIA]],
                    },
                )
            )
    return advertencias


def _advertencias_sanciones(sanciones: ResultadoSanciones) -> list[AdvertenciaAnalisis]:
    motivos: dict[str, list] = {}
    for sancion in sanciones.sanciones:
        for motivo in sancion.motivos_revision:
            motivos.setdefault(motivo, []).append(sancion)
    return [
        AdvertenciaAnalisis(
            codigo=ADV_SANCION_A_REVISAR,
            categoria=CATEGORIA_SANCIONES,
            mensaje="Sanción detectada con un dato que requiere revisión humana.",
            detalles={
                "motivo": motivo,
                "total": len(lista),
                "posiciones": [s.inicio for s in lista[:MAX_DETALLES_ADVERTENCIA]],
            },
        )
        for motivo, lista in sorted(motivos.items())
    ]


def analizar_texto(
    texto: str,
    *,
    catalogo: CatalogoGri | None = None,
    advertencias_calidad: Sequence[AdvertenciaCalidad] = (),
    detector_gri: Callable[[str, CatalogoGri], ResultadoDeteccionGri] = detectar_referencias_gri,
    detector_sanciones: Callable[[str], ResultadoSanciones] = detectar_sanciones,
) -> ResultadoAnalisisIngesta:
    """Ejecuta ambos detectores sobre `texto` y clasifica. Los parámetros `detector_*`
    permiten sustituirlos en pruebas de fallo; en producción se usan los reales."""
    if not isinstance(texto, str):
        raise TypeError(f"El texto debe ser str, no {type(texto).__name__}.")
    catalogo = catalogo or catalogo_predeterminado()
    try:
        gri = detector_gri(texto, catalogo)
    except Exception as exc:
        raise _fallo("gri", exc) from None
    try:
        sanciones = detector_sanciones(texto)
    except Exception as exc:
        raise _fallo("sanciones", exc) from None

    motivos = []
    if any(grupo.catalogado for grupo in gri.grupos):
        motivos.append(MOTIVO_REFERENCIAS_GRI)
    if sanciones.total:
        motivos.append(MOTIVO_SANCIONES)
    resultado = ResultadoAnalisis.CON_HALLAZGOS if motivos else ResultadoAnalisis.OBSERVADO
    if not motivos:
        motivos.append(MOTIVO_SIN_HALLAZGOS)

    advertencias = [
        AdvertenciaAnalisis(a.codigo, CATEGORIA_CALIDAD, a.mensaje, a.detalles) for a in advertencias_calidad
    ]
    advertencias += _advertencias_gri(gri.grupos)
    advertencias += _advertencias_sanciones(sanciones)
    return ResultadoAnalisisIngesta(
        resultado=resultado,
        motivos=tuple(motivos),
        version_catalogo=gri.version_catalogo,
        gri=gri,
        sanciones=sanciones,
        advertencias=tuple(advertencias),
    )


def analizar_documento(documento: DocumentoValidado, **kwargs) -> ResultadoAnalisisIngesta:
    """Analiza el texto de un documento ya validado; sus advertencias de calidad
    (tablas inconsistentes) se incluyen sin afectar la clasificación."""
    return analizar_texto(documento.texto, advertencias_calidad=documento.advertencias, **kwargs)

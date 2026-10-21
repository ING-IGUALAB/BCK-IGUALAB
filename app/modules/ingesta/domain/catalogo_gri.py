"""Catálogo local y versionado de estándares GRI (decisión 2026-10-07).

Se carga desde `datos/catalogo_gri.json`; la aplicación NUNCA descarga fuentes. El
catálogo identifica ESTÁNDARES («GRI 305»), no revelaciones («305-1») ni temas de
los estándares sectoriales («14.1»). Por tanto una referencia específica solo se
contrasta con su estándar: el catálogo no demuestra que esa revelación exista.

- `version_catalogo` identifica la recopilación; es independiente de la `edicion`
  de cada estándar (2016, 2018, 2020, 2021, 2024, 2025…).
- La clave natural es (`codigo`, `edicion`). Un mismo código puede tener varias
  entradas con significado distinto: GRI 102 es «General Disclosures 2016»
  (universal histórico) y «Climate Change 2025» (tema nuevo); GRI 306 tiene
  «Effluents and Waste 2016» y «Waste 2020».
- `reemplazado_por` es información del catálogo. Ningún detector debe usarla
  para convertir un código histórico en uno vigente.
- No es el catálogo corporativo de 40 códigos pendiente de aprobación (D-seed):
  su cobertura exacta está en `cobertura` del JSON.
"""
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

RUTA_CATALOGO_PREDETERMINADA = Path(__file__).parent / "datos" / "catalogo_gri.json"
ESQUEMA_SOPORTADO = 1
# Prefijo de contexto de los errores del catálogo completo (los de cada entrada llevan su etiqueta).
_CONTEXTO_CATALOGO = "catálogo"

TIPOS = ("universal", "tematico", "sectorial")
CATEGORIAS = ("Universal", "Económica", "Ambiental", "Social", "Sectorial")
# La vigencia se mide a la fecha de corte del catálogo (`recopilado_en`):
# - vigente: en vigor;
# - publicado_no_vigente: publicado por GRI, con fecha efectiva futura;
# - parcialmente_vigente: en parte reemplazado (p. ej. GRI 306: Effluents and Waste 2016
#   conserva en vigor la revelación 306-3);
# - historico: ya no está en vigor (reemplazado); retirado: GRI lo retiró.
VIGENCIAS = ("vigente", "publicado_no_vigente", "parcialmente_vigente", "historico", "retirado")
EN_VIGOR = ("vigente", "parcialmente_vigente")
PREFIJO_URL_OFICIAL = "https://www.globalreporting.org/"
PREFIJO_URL_OFICIAL_SIN_WWW = "https://globalreporting.org/"

_CODIGO = re.compile(r"[1-9]\d{0,2}", re.ASCII)
_EDICION = re.compile(r"(?:19|20)\d{2}", re.ASCII)
_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)  # solo dígitos ASCII


class CatalogoGriInvalido(ValueError):
    """El archivo del catálogo no cumple el esquema; no se usa un catálogo a medias."""


@dataclass(frozen=True, slots=True)
class EntradaGri:
    codigo: str
    nombre: str
    edicion: str
    version_documento: str | None
    tipo: str
    categoria: str
    categoria_origen: str
    vigencia: str
    efectiva_desde: str | None
    efectiva_hasta: str | None
    reemplazado_por: tuple[tuple[str, str], ...]
    # True si el reemplazo alcanza solo a parte del estándar (revelaciones concretas).
    reemplazo_parcial: bool
    reemplazo_efectivo_desde: str | None
    url_oficial: str | None
    url_comprobada: bool
    fecha_listada_en_fuente: str | None
    metodo_verificacion: str
    fuente: str
    verificado_en: str
    nota: str | None

    @property
    def etiqueta(self) -> str:
        return f"GRI {self.codigo}: {self.nombre} {self.edicion}"

    def vigente_en(self, fecha: str) -> bool | None:
        """¿Está en vigor (al menos en parte) en `fecha` (AAAA-MM-DD)? None si el
        catálogo no tiene fecha efectiva verificada para decidirlo. Un estándar
        retirado nunca está en vigor."""
        if not _FECHA.fullmatch(fecha):
            raise ValueError("La fecha debe tener formato AAAA-MM-DD.")
        if self.vigencia == "retirado":
            return False
        if self.efectiva_desde is None:
            return None
        return self.efectiva_desde <= fecha and (self.efectiva_hasta is None or fecha <= self.efectiva_hasta)


@dataclass(frozen=True)
class CatalogoGri:
    version_catalogo: str
    recopilado_en: str
    fuente_principal: str
    cobertura: dict[str, Any]
    entradas: tuple[EntradaGri, ...]

    def __post_init__(self) -> None:
        indice: dict[str, list[EntradaGri]] = {}
        for entrada in self.entradas:
            indice.setdefault(entrada.codigo, []).append(entrada)
        # frozen: se fija el índice una sola vez, derivado de `entradas`.
        object.__setattr__(self, "_por_codigo", {k: tuple(v) for k, v in indice.items()})

    def por_codigo(self, codigo: str) -> tuple[EntradaGri, ...]:
        return self._por_codigo.get(codigo, ())  # type: ignore[attr-defined]

    def buscar(self, codigo: str, edicion: str) -> EntradaGri | None:
        for entrada in self.por_codigo(codigo):
            if entrada.edicion == edicion:
                return entrada
        return None

    @property
    def codigos(self) -> frozenset[str]:
        return frozenset(self._por_codigo)  # type: ignore[attr-defined]


def _texto(datos: dict, campo: str, contexto: str, *, opcional: bool = False) -> str | None:
    valor = datos.get(campo)
    if valor is None and opcional:
        return None
    if not isinstance(valor, str) or not valor.strip():
        raise CatalogoGriInvalido(f"{contexto}: «{campo}» debe ser un texto no vacío.")
    return valor


def _fecha_opcional(datos: dict, campo: str, contexto: str) -> str | None:
    valor = _texto(datos, campo, contexto, opcional=True)
    if valor is not None and not _FECHA.fullmatch(valor):
        raise CatalogoGriInvalido(f"{contexto}: «{campo}» debe tener formato AAAA-MM-DD.")
    return valor


def _logico(datos: dict, campo: str, contexto: str) -> bool:
    valor = datos.get(campo)
    if not isinstance(valor, bool):
        raise CatalogoGriInvalido(f"{contexto}: «{campo}» debe ser lógico.")
    return valor


def _coherencia_de_vigencia(entrada: EntradaGri, corte: str) -> None:
    """La vigencia declarada debe coincidir con las fechas efectivas y la fecha de corte."""
    desde, hasta = entrada.efectiva_desde, entrada.efectiva_hasta
    if desde and hasta and desde > hasta:
        raise CatalogoGriInvalido(f"{entrada.etiqueta}: efectiva_desde posterior a efectiva_hasta.")
    incoherente = {
        "publicado_no_vigente": desde is None or desde <= corte,
        "vigente": (desde is not None and desde > corte) or (hasta is not None and hasta < corte),
        "parcialmente_vigente": desde is None or desde > corte or (hasta is not None and hasta < corte),
        "historico": hasta is None or hasta >= corte,
        "retirado": False,
    }[entrada.vigencia]
    if incoherente:
        raise CatalogoGriInvalido(
            f"{entrada.etiqueta}: la vigencia «{entrada.vigencia}» no coincide con sus fechas a {corte}."
        )


def _codigo_y_edicion(datos: dict, contexto: str) -> tuple[str, str]:
    codigo = _texto(datos, "codigo", contexto)
    if not _CODIGO.fullmatch(codigo):
        raise CatalogoGriInvalido(f"{contexto}: código «{codigo}» no válido (1 a 3 dígitos, sin ceros a la izquierda).")
    edicion = _texto(datos, "edicion", contexto)
    if not _EDICION.fullmatch(edicion):
        raise CatalogoGriInvalido(f"{contexto}: edición «{edicion}» no es un año.")
    return codigo, edicion


def _clasificacion(datos: dict, contexto: str) -> tuple[str, str, str]:
    """(tipo, categoría, vigencia), cada uno dentro de sus valores permitidos."""
    tipo = _texto(datos, "tipo", contexto)
    categoria = _texto(datos, "categoria", contexto)
    vigencia = _texto(datos, "vigencia", contexto)
    for valor, permitidos, nombre in ((tipo, TIPOS, "tipo"), (categoria, CATEGORIAS, "categoria"), (vigencia, VIGENCIAS, "vigencia")):
        if valor not in permitidos:
            raise CatalogoGriInvalido(f"{contexto}: {nombre} «{valor}» no permitido.")
    return tipo, categoria, vigencia


def _verificacion(datos: dict, contexto: str, url: str | None) -> dict:
    verificacion = datos.get("verificacion")
    if not isinstance(verificacion, dict):
        raise CatalogoGriInvalido(f"{contexto}: falta «verificacion».")
    if not isinstance(verificacion.get("url_comprobada"), bool):
        raise CatalogoGriInvalido(f"{contexto}: «url_comprobada» debe ser lógico.")
    if verificacion["url_comprobada"] and url is None:
        raise CatalogoGriInvalido(f"{contexto}: no puede tener la URL comprobada sin URL.")
    return verificacion


def _reemplazos(datos: dict, contexto: str) -> list[tuple[str, str]]:
    reemplazos = []
    for destino in datos.get("reemplazado_por") or []:
        if not isinstance(destino, dict) or not _CODIGO.fullmatch(str(destino.get("codigo"))) \
                or not _EDICION.fullmatch(str(destino.get("edicion"))):
            raise CatalogoGriInvalido(f"{contexto}: «reemplazado_por» mal formado.")
        reemplazos.append((destino["codigo"], destino["edicion"]))
    return reemplazos


def _entrada(datos: Any, posicion: int) -> EntradaGri:
    contexto = f"entradas[{posicion}]"
    if not isinstance(datos, dict):
        raise CatalogoGriInvalido(f"{contexto}: debe ser un objeto.")
    codigo, edicion = _codigo_y_edicion(datos, contexto)
    contexto = f"GRI {codigo} {edicion}"
    tipo, categoria, vigencia = _clasificacion(datos, contexto)
    url = _texto(datos, "url_oficial", contexto, opcional=True)
    if url is not None and not url.startswith((PREFIJO_URL_OFICIAL, PREFIJO_URL_OFICIAL_SIN_WWW)):
        raise CatalogoGriInvalido(f"{contexto}: la URL oficial debe estar en globalreporting.org.")
    verificacion = _verificacion(datos, contexto, url)
    reemplazos = _reemplazos(datos, contexto)
    version_documento = _texto(datos, "version_documento", contexto, opcional=True)
    return EntradaGri(
        codigo=codigo,
        nombre=_texto(datos, "nombre", contexto),
        edicion=edicion,
        version_documento=version_documento,
        tipo=tipo,
        categoria=categoria,
        categoria_origen=_texto(datos, "categoria_origen", contexto),
        vigencia=vigencia,
        efectiva_desde=_fecha_opcional(datos, "efectiva_desde", contexto),
        efectiva_hasta=_fecha_opcional(datos, "efectiva_hasta", contexto),
        reemplazado_por=tuple(reemplazos),
        reemplazo_parcial=_logico(datos, "reemplazo_parcial", contexto),
        reemplazo_efectivo_desde=_fecha_opcional(datos, "reemplazo_efectivo_desde", contexto),
        url_oficial=url,
        url_comprobada=verificacion["url_comprobada"],
        fecha_listada_en_fuente=_fecha_opcional(datos, "fecha_listada_en_fuente", contexto),
        metodo_verificacion=_texto(verificacion, "metodo", contexto),
        fuente=_texto(verificacion, "fuente", contexto),
        verificado_en=_texto(verificacion, "verificado_en", contexto),
        nota=_texto(datos, "nota", contexto, opcional=True),
    )


def _validar_claves_y_reemplazos(entradas: tuple[EntradaGri, ...]) -> None:
    claves = [(e.codigo, e.edicion) for e in entradas]
    repetidas = sorted({c for c in claves if claves.count(c) > 1})
    if repetidas:
        raise CatalogoGriInvalido(f"Entradas duplicadas (código, edición): {repetidas}")
    existentes = set(claves)
    for entrada in entradas:
        for destino in entrada.reemplazado_por:
            if destino not in existentes:
                raise CatalogoGriInvalido(f"{entrada.etiqueta}: reemplazo {destino} inexistente en el catálogo.")


def _validar_cobertura_por_vigencia(cobertura: dict, entradas: tuple[EntradaGri, ...]) -> None:
    por_vigencia: dict[str, int] = {}
    for entrada in entradas:
        por_vigencia[entrada.vigencia] = por_vigencia.get(entrada.vigencia, 0) + 1
    esperado = {
        "por_vigencia": por_vigencia,
        "publicados_por_gri": len(entradas) - por_vigencia.get("retirado", 0),
        "en_vigor_en_la_fecha_de_corte": sum(por_vigencia.get(v, 0) for v in EN_VIGOR),
        "publicados_no_vigentes_aun": por_vigencia.get("publicado_no_vigente", 0),
    }
    for clave, valor in esperado.items():
        if cobertura.get(clave) != valor:
            raise CatalogoGriInvalido(f"La cobertura declarada «{clave}» no coincide con las entradas.")


def interpretar_catalogo(datos: Any) -> CatalogoGri:
    """Valida el contenido ya leído del JSON. No acepta duplicados (código, edición)
    ni referencias de reemplazo a entradas que no existan en el propio catálogo."""
    if not isinstance(datos, dict):
        raise CatalogoGriInvalido("El catálogo debe ser un objeto JSON.")
    if datos.get("esquema") != ESQUEMA_SOPORTADO:
        raise CatalogoGriInvalido(f"Esquema {datos.get('esquema')!r} no soportado; se esperaba {ESQUEMA_SOPORTADO}.")
    version = _texto(datos, "version_catalogo", _CONTEXTO_CATALOGO)
    cobertura = datos.get("cobertura")
    if not isinstance(cobertura, dict):
        raise CatalogoGriInvalido("catálogo: falta «cobertura».")
    crudas = datos.get("entradas")
    if not isinstance(crudas, list) or not crudas:
        raise CatalogoGriInvalido("catálogo: «entradas» debe ser una lista no vacía.")
    entradas = tuple(_entrada(cruda, i) for i, cruda in enumerate(crudas))

    _validar_claves_y_reemplazos(entradas)
    if cobertura.get("total_entradas") != len(entradas):
        raise CatalogoGriInvalido("La cobertura declarada no coincide con el número de entradas.")
    recopilado_en = _texto(datos, "recopilado_en", _CONTEXTO_CATALOGO)
    if not _FECHA.fullmatch(recopilado_en):
        raise CatalogoGriInvalido("catálogo: «recopilado_en» debe tener formato AAAA-MM-DD.")
    for entrada in entradas:
        _coherencia_de_vigencia(entrada, recopilado_en)
    _validar_cobertura_por_vigencia(cobertura, entradas)
    return CatalogoGri(
        version_catalogo=version,
        recopilado_en=recopilado_en,
        fuente_principal=_texto(datos, "fuente_principal", _CONTEXTO_CATALOGO),
        cobertura=cobertura,
        entradas=entradas,
    )


def cargar_catalogo(ruta: Path | str | None = None) -> CatalogoGri:
    """Lee y valida el catálogo local. Sin red."""
    destino = Path(ruta) if ruta is not None else RUTA_CATALOGO_PREDETERMINADA
    try:
        datos = json.loads(destino.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogoGriInvalido(f"No se pudo leer el catálogo GRI: {type(exc).__name__}.") from None
    return interpretar_catalogo(datos)


@lru_cache(maxsize=1)
def catalogo_predeterminado() -> CatalogoGri:
    return cargar_catalogo()

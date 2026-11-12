"""Reglas PURAS del historial del Asistente IA (entrega 1: guardar y recuperar). Sin base de datos, sin red y sin IA.

QUÉ SE VALIDA. La estructura, los tamaños, el orden y la consistencia interna del historial que aporta el cliente: roles admitidos,
pares pregunta/respuesta, y que las referencias de cada afirmación apunten a citas existentes, de la empresa y el año de la
conversación. QUÉ NO SE VALIDA NI SE PRUEBA. Que el backend haya generado ese contenido, que las citas se hayan verificado contra
los documentos o que el texto citado exista: el cliente puede enviar lo que quiera dentro de esa estructura. Por eso el servidor
marca cada mensaje con su procedencia (`APORTADO_POR_CLIENTE`).

Los mensajes de error NUNCA incluyen el contenido recibido (preguntas, respuestas, citas): solo describen qué regla falló.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

from app.models import SectorEmpresa
from app.modules.asistente.domain.citas import (
    MOTIVO_AFIRMACIONES_NO_VERIFICABLES,
    MOTIVO_FUERA_DE_DOMINIO,
    MOTIVO_MODELO_SIN_EVIDENCIA,
    MOTIVO_RESPUESTA_NO_INTERPRETABLE,
    MOTIVO_SIN_FRAGMENTOS,
)
from app.modules.asistente.schemas import BusquedaDocumental, ConsultaResponse

# --- Límites (una conversación admitida siempre se puede recuperar completa) ---------------------------------------------------
MAX_MENSAJES = 40  # 20 pares pregunta/respuesta
MAX_PREGUNTA = 2000  # igual que `ConsultaRequest.pregunta`
MAX_TEXTO_RESPUESTA = 20_000
MAX_MENSAJE_AUSENCIA = 1_000
MAX_AFIRMACIONES = 50
MAX_CITAS = 20
MAX_REFERENCIAS_POR_AFIRMACION = 20
MAX_TEXTO_AFIRMACION = 2_000
MAX_NOMBRE = 255
MAX_SECCION = 1_000
MAX_EXTRACTO = 300  # el servicio actual recorta el extracto a 300 caracteres
MAX_DESCARTADAS = 1_000
MAX_BYTES_CONVERSACION = 1_000_000  # JSON canónico UTF-8 de todo el contenido; por encima se responde 413
LONGITUD_MAXIMA_TITULO = 80  # igual que `conversaciones_asistente.titulo`

MOTIVOS_SIN_EVIDENCIA = frozenset(
    {
        MOTIVO_SIN_FRAGMENTOS,
        MOTIVO_MODELO_SIN_EVIDENCIA,
        MOTIVO_AFIRMACIONES_NO_VERIFICABLES,
        MOTIVO_RESPUESTA_NO_INTERPRETABLE,
        MOTIVO_FUERA_DE_DOMINIO,
    }
)


def titulo_desde_pregunta(pregunta: str) -> str:
    """Título determinista: la primera pregunta con los espacios (y saltos de línea) colapsados; si pasa de 80 caracteres se
    corta en 79 y se añade «…». La misma pregunta siempre produce el mismo título."""
    texto = " ".join(pregunta.split())
    if len(texto) <= LONGITUD_MAXIMA_TITULO:
        return texto
    return texto[: LONGITUD_MAXIMA_TITULO - 1].rstrip() + "…"


def contenido_de_respuesta(respuesta: ConsultaResponse) -> str:
    """Texto legible del mensaje del asistente: la respuesta o, si no hay evidencia, la aclaración. Lo deriva el servidor."""
    return (respuesta.mensaje or "") if respuesta.sin_evidencia else respuesta.respuesta


def _hay_nul(valor: object) -> bool:
    if isinstance(valor, str):
        return "\x00" in valor
    if isinstance(valor, dict):
        return any(_hay_nul(k) or _hay_nul(v) for k, v in valor.items())
    if isinstance(valor, (list, tuple)):
        return any(_hay_nul(v) for v in valor)
    return False


def contiene_nul(valor: object) -> bool:
    """True si el valor (texto, o dict/lista anidados) trae el carácter NUL, que PostgreSQL no admite en TEXT ni en JSONB."""
    return _hay_nul(valor)


def _texto_valido(texto: str, maximo: int) -> bool:
    return bool(texto.strip()) and len(texto) <= maximo


def _problemas_de_estructura(r: ConsultaResponse) -> list[str]:
    problemas: list[str] = []
    if len(r.respuesta) > MAX_TEXTO_RESPUESTA:
        problemas.append("el texto de la respuesta supera el máximo permitido")
    if not 0 <= r.afirmaciones_descartadas <= MAX_DESCARTADAS:
        problemas.append("afirmaciones_descartadas está fuera de rango")
    if r.mensaje is not None and len(r.mensaje) > MAX_MENSAJE_AUSENCIA:
        problemas.append("el mensaje de la respuesta supera el máximo permitido")
    if len(r.afirmaciones) > MAX_AFIRMACIONES:
        problemas.append("la respuesta tiene demasiadas afirmaciones")
    if len(r.citas) > MAX_CITAS:
        problemas.append("la respuesta tiene demasiadas citas")
    return problemas


def _problemas_sin_evidencia(r: ConsultaResponse) -> list[str]:
    problemas: list[str] = []
    if r.motivo not in MOTIVOS_SIN_EVIDENCIA:
        problemas.append("una respuesta sin evidencia debe traer un motivo reconocido")
    if r.respuesta != "" or r.afirmaciones or r.citas:
        problemas.append("una respuesta sin evidencia no puede traer texto, afirmaciones ni citas")
    if r.mensaje is None or not r.mensaje.strip():
        problemas.append("una respuesta sin evidencia debe traer su aclaración")
    return problemas


def _problemas_de_cita(c, empresa_id: uuid.UUID, anio: int) -> list[str]:
    problemas: list[str] = []
    if c.referencia < 1:
        problemas.append("las referencias de las citas deben ser enteros positivos")
    if c.empresa_id != empresa_id or c.anio != anio:
        problemas.append("una cita no corresponde a la empresa y el año de la conversación")
    if not _texto_valido(c.documento_nombre, MAX_NOMBRE) or not _texto_valido(c.empresa_nombre, MAX_NOMBRE):
        problemas.append("una cita trae un nombre vacío o demasiado largo")
    if len(c.seccion) > MAX_SECCION or len(c.extracto) > MAX_EXTRACTO:
        problemas.append("una cita supera el tamaño permitido de sección o extracto")
    return problemas


def _problemas_de_afirmacion(a, referencias_de_citas: set[int]) -> list[str]:
    problemas: list[str] = []
    if not _texto_valido(a.texto, MAX_TEXTO_AFIRMACION) or not _texto_valido(a.cita_textual, MAX_TEXTO_AFIRMACION):
        problemas.append("una afirmación trae texto o cita textual vacío o demasiado largo")
    if not a.referencias or len(a.referencias) > MAX_REFERENCIAS_POR_AFIRMACION:
        problemas.append("cada afirmación debe tener entre 1 y 20 referencias")
    if any(n not in referencias_de_citas for n in a.referencias):
        problemas.append("una afirmación referencia una cita que no existe")
    return problemas


def _problemas_con_evidencia(r: ConsultaResponse, empresa_id: uuid.UUID, anio: int) -> list[str]:
    problemas: list[str] = []
    if r.motivo is not None:
        problemas.append("una respuesta con evidencia no puede traer motivo de ausencia")
    if not r.respuesta.strip():
        problemas.append("una respuesta con evidencia debe traer texto")
    if not r.afirmaciones or not r.citas:
        problemas.append("una respuesta con evidencia debe traer afirmaciones y citas")
        return problemas
    referencias = [c.referencia for c in r.citas]
    if len(set(referencias)) != len(referencias):
        problemas.append("las referencias de las citas no pueden repetirse")
    for c in r.citas:
        problemas += _problemas_de_cita(c, empresa_id, anio)
    conjunto = set(referencias)
    usadas: set[int] = set()
    for a in r.afirmaciones:
        problemas += _problemas_de_afirmacion(a, conjunto)
        usadas.update(a.referencias)
    if usadas != conjunto:
        problemas.append("toda cita debe estar referenciada por alguna afirmación")
    return problemas


def _problemas_del_estado_adjunto(r: ConsultaResponse) -> list[str]:
    """El estado registrado que acompaña a una respuesta DOCUMENTAL (no la sustituye ni la exime de sus citas)."""
    e = r.estado_registrado
    if e is None:
        return []
    if not e.texto.strip() or len(e.texto) > MAX_MENSAJE_AUSENCIA:
        return ["el estado registrado trae un texto vacío o demasiado largo"]
    return []


def _problemas_solo_estado(r: ConsultaResponse) -> list[str]:
    """Una respuesta SIN búsqueda documental (`NO_REALIZADA`): válida únicamente como lectura del estado registrado. No es una vía para
    afirmaciones sin cita: no admite afirmaciones, citas, ausencia de evidencia ni más texto que el del propio estado."""
    problemas: list[str] = []
    e = r.estado_registrado
    if e is None:
        return ["una respuesta sin búsqueda documental debe traer el estado registrado"]
    if not e.texto.strip() or len(e.texto) > MAX_MENSAJE_AUSENCIA:
        problemas.append("el estado registrado trae un texto vacío o demasiado largo")
    if r.sin_evidencia or r.motivo is not None:
        problemas.append("una respuesta de solo estado no declara ausencia de evidencia ni trae motivo")
    if r.afirmaciones or r.citas or r.afirmaciones_descartadas:
        problemas.append("una respuesta de solo estado no puede traer afirmaciones, citas ni afirmaciones descartadas")
    if r.mensaje is not None:
        problemas.append("una respuesta de solo estado no trae mensaje adicional")
    if r.respuesta != e.texto:
        problemas.append("la respuesta de solo estado debe ser el texto del estado registrado")
    return problemas


def problemas_de_respuesta(r: ConsultaResponse, empresa_id: uuid.UUID, anio: int) -> list[str]:
    """Reglas violadas por una respuesta del asistente aportada en el historial (lista vacía = estructura consistente)."""
    problemas = _problemas_de_estructura(r)
    if r.busqueda_documental is BusquedaDocumental.NO_REALIZADA:
        problemas += _problemas_solo_estado(r)
    else:  # búsqueda REALIZADA (también para toda respuesta anterior a este campo): las exigencias de citas no cambian
        problemas += _problemas_sin_evidencia(r) if r.sin_evidencia else _problemas_con_evidencia(r, empresa_id, anio)
        problemas += _problemas_del_estado_adjunto(r)
    if _hay_nul(r.model_dump(mode="json")):
        problemas.append("la respuesta contiene caracteres no admitidos")
    return list(dict.fromkeys(problemas))  # sin repetir, en orden


def problemas_de_pregunta(contenido: str) -> list[str]:
    if not contenido.strip():
        return ["la pregunta no puede estar vacía"]
    if "\x00" in contenido:
        return ["la pregunta contiene caracteres no admitidos"]
    return []


def _json_canonico(valor: object) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def contenido_canonico(empresa_id: uuid.UUID, sector: SectorEmpresa, anio: int, mensajes: Sequence[dict]) -> str:
    """Representación estable del contexto y los mensajes recibidos (sin la clave de idempotencia)."""
    return _json_canonico({"empresa_id": str(empresa_id), "sector": sector.value, "anio": anio, "mensajes": list(mensajes)})


def huella_de_contenido(canonico: str) -> str:
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()

"""Casos de uso del Asistente IA — RAG (Módulo 6, CU004). Orquesta la recuperación y la respuesta.

Flujo (RF-023/024 · RN-033..037 · RNF-010):
  sector + empresa + año + pregunta
  → la empresa existe, está activa y es del sector (en la base; no se confía en el frontend)
  → embedding de CONSULTA (SEARCH_QUERY) → `documentos_elegibles_para_consulta` (completados, con análisis, NO deshabilitados)
  → `buscar_similares` (pgvector) limitado a ambiente, empresa, año, sector y a esos documentos, ANTES de ordenar y limitar
  → filtrar por `documentos_recuperables` (solo COMPLETADO con análisis, no deshabilitado y empresa activa)
  → prompt con procedencia (empresa, documento, año, sección) y contenido como DATO → LLM
  → verificación afirmación por afirmación (`domain/citas.py`): referencia válida y cita literal del fragmento citado.

Si ninguna afirmación se puede respaldar, se declara explícitamente que no hay evidencia (RN-035) y se omite todo texto del modelo;
nunca se borra un marcador para conservar una afirmación sin fuente (RN-033/034). Ver los límites en `domain/citas.py`.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessValidationError, ConflictError
from app.models import Empresa, SectorEmpresa
from app.models.documento_ingesta import Documento
from app.modules.asistente.domain.citas import (
    MOTIVO_AFIRMACIONES_NO_VERIFICABLES,
    MOTIVO_FUERA_DE_DOMINIO,
    MOTIVO_MODELO_SIN_EVIDENCIA,
    MOTIVO_RESPUESTA_NO_INTERPRETABLE,
    MOTIVO_SIN_FRAGMENTOS,
    verificar_respuesta,
)
from app.modules.analisis_gri.application.servicio import consultar_analisis
from app.modules.asistente.domain.estado_gri import (
    SEPARADOR_EVIDENCIA,
    TITULO,
    CodigoSolicitado,
    codigos_para_estado,
    construir_estado_registrado,
    menciona_brecha,
    solo_estado,
)
from app.modules.asistente.domain.historial import MAX_MENSAJE_AUSENCIA
from app.modules.asistente.domain.prompt import construir_prompt_usuario, nuevo_codigo_de_marcador, sistema_para, validar_pregunta
from app.modules.asistente.infrastructure.llm import ProveedorLlm
from app.modules.asistente.schemas import Afirmacion, BusquedaDocumental, Cita, ConsultaResponse
from app.modules.empresas import service as empresa_service
from app.modules.ingesta.application.documento_service import documentos_elegibles_para_consulta, documentos_recuperables
from app.modules.ingesta.domain.embeddings import ProveedorEmbeddingsConsulta, embeber_consulta
from app.modules.ingesta.domain.parametros import PARAMETROS_INGESTA
from app.modules.ingesta.infrastructure.fragmentos_vectoriales import (
    FragmentoRecuperado,
    buscar_similares,
)

if TYPE_CHECKING:  # solo para anotar: `continuacion_service` importa este módulo (evita el ciclo de importación)
    from app.modules.asistente.continuacion_service import RecursosRag

# INTERRUPTOR DEL MODO CON INTERPRETACIÓN. Valor fijo en código (no es una variable de entorno: los `.env` del servidor no los
# administra el equipo). True: además de las afirmaciones con cita literal, el modelo puede añadir un bloque APARTE de interpretación
# apoyado en ellas. False: el asistente responde solo con afirmaciones citadas, con el prompt y el `k` anteriores. Para desactivarlo,
# cambiar esta línea a False y desplegar.
PERMITE_INTERPRETACION = True

LIMITES_POR_DEFECTO = 4  # k de recuperación (RAG simple)
LIMITE_CON_INTERPRETACION = 8  # k cuando se permite interpretar: interpretar exige más contexto que localizar un dato


def _limite_efectivo(limite: int | None, interpretar: bool) -> int:
    """El `k` pedido por el llamador o, si no pidió ninguno, el del modo vigente."""
    if limite is not None:
        return limite
    return LIMITE_CON_INTERPRETACION if interpretar else LIMITES_POR_DEFECTO

_MENSAJES = {
    MOTIVO_SIN_FRAGMENTOS: (
        "No hay documentos publicados de esa empresa, sector y año que permitan responder. "
        "Verifique la selección o que el documento esté ingerido y publicado."
    ),
    MOTIVO_MODELO_SIN_EVIDENCIA: (
        "No se encontró evidencia en los documentos de la empresa y el año seleccionados para responder esa pregunta. "
        "Puede reformularla."
    ),
    MOTIVO_AFIRMACIONES_NO_VERIFICABLES: (
        "El asistente no pudo respaldar ninguna afirmación con una cita verificable de los documentos, así que no se muestra "
        "ninguna respuesta. Puede reformular la pregunta."
    ),
    MOTIVO_RESPUESTA_NO_INTERPRETABLE: (
        "La respuesta del asistente no tuvo el formato verificable esperado, así que no se muestra. Intente de nuevo."
    ),
    MOTIVO_FUERA_DE_DOMINIO: (
        "La consulta está fuera del dominio del asistente (sostenibilidad empresarial, indicadores GRI, sanciones o el contenido "
        "de los documentos ingeridos)."
    ),
}


def _sin_evidencia(motivo: str, descartadas: int = 0) -> ConsultaResponse:
    return ConsultaResponse(
        respuesta="", sin_evidencia=True, motivo=motivo, mensaje=_MENSAJES[motivo], afirmaciones_descartadas=descartadas
    )


async def _empresa_consultable(db: AsyncSession, empresa_id: uuid.UUID, sector: SectorEmpresa) -> Empresa:
    """La empresa existe (404), está habilitada (400) y pertenece al sector seleccionado (400): se comprueba en la base."""
    empresa = await empresa_service.obtener_empresa(db, empresa_id)
    if not empresa.activa:
        raise BusinessValidationError("COMPANY_INACTIVE", "La empresa está inactiva; no se puede consultar sobre ella.")
    if empresa.sector != sector:
        raise BusinessValidationError(
            "COMPANY_SECTOR_MISMATCH", "El sector indicado no corresponde a la empresa seleccionada."
        )
    return empresa


async def _responder_documental(
    db: AsyncSession,
    db_vectorial: AsyncSession,
    *,
    pregunta: str,
    ambiente: str,
    proveedor_embeddings: ProveedorEmbeddingsConsulta,
    proveedor_llm: ProveedorLlm,
    sector: SectorEmpresa,
    empresa_id: uuid.UUID,
    anio: int,
    limite: int | None = None,
) -> ConsultaResponse:
    """Parte DOCUMENTAL de la respuesta (RAG): una consulta limitada a un ambiente, sector, empresa y año, con afirmaciones respaldadas
    por fuentes verificables; `responder_consulta` le agrega, si corresponde, el estado.

    Con `PERMITE_INTERPRETACION` activo cambian tres cosas y solo esas: el prompt de sistema (pide además una interpretación
    aparte), el `k` por omisión y la lectura del campo «interpretacion». Las afirmaciones se verifican igual en los dos modos."""
    interpretar = PERMITE_INTERPRETACION
    try:
        validar_pregunta(pregunta)
    except ValueError as exc:
        # El dominio lanza ValueError genérico; en la frontera HTTP eso sería un 500.
        raise BusinessValidationError("EMPTY_QUESTION", str(exc)) from None

    empresa = await _empresa_consultable(db, empresa_id, sector)
    empresa_nombre, empresa_sector = empresa.nombre, empresa.sector
    await db.rollback()  # fin de la lectura: no se mantiene una transacción abierta durante las llamadas a proveedores

    # 1) embedding de la consulta (asimétrico: SEARCH_QUERY)
    vector = await embeber_consulta(
        pregunta,
        proveedor_embeddings,
        timeout_segundos=PARAMETROS_INGESTA.timeout_embeddings_segundos,
    )

    # 2) documentos ELEGIBLES (completados, con análisis, no deshabilitados, de empresa activa) de esa empresa y año, ANTES de buscar: la base
    #    vectorial no sabe qué documentos se deshabilitaron, y si se filtraran después de ordenar y limitar, los fragmentos de un documento
    #    retirado (que conserva sus vectores) podrían ocupar los primeros lugares y dejar sin evidencia al documento que lo reemplazó.
    elegibles = await documentos_elegibles_para_consulta(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
    if not elegibles:
        return _sin_evidencia(MOTIVO_SIN_FRAGMENTOS)

    # 3) recuperación vectorial (exacta por coseno), SIEMPRE limitada a ambiente, empresa, año, sector y a los documentos elegibles
    #    (el filtro se aplica en SQL, antes de ordenar y limitar)
    candidatos = await buscar_similares(
        db_vectorial,
        ambiente=ambiente,
        vector_consulta=vector,
        identidad=proveedor_embeddings.identidad,
        empresa_ids=[empresa_id],
        limite=_limite_efectivo(limite, interpretar),
        anio=anio,
        sector=empresa_sector,
        documento_ids=elegibles,
    )

    # 4) visibilidad transaccional: solo documentos COMPLETADOS con análisis, no deshabilitados y empresa activa. Defensa en profundidad:
    #    aunque la búsqueda se equivocara, un fragmento de otra empresa o año no entra al contexto, y un documento deshabilitado entre
    #    la lectura de elegibles y la búsqueda tampoco.
    ids = {f.documento_id for f in candidatos}
    recuperables = await documentos_recuperables(db, ambiente=ambiente, documento_ids=ids) if ids else set()
    fragmentos = [
        f for f in candidatos if f.documento_id in recuperables and f.empresa_id == empresa_id and f.anio == anio
    ]
    if not fragmentos:
        return _sin_evidencia(MOTIVO_SIN_FRAGMENTOS)

    nombres = await _nombres_documentos(db, {f.documento_id for f in fragmentos})
    await db.rollback()

    # 5) prompt (con procedencia) + LLM
    prompt_usuario = construir_prompt_usuario(
        pregunta, fragmentos, empresa_nombre=empresa_nombre, nombres_documentos=nombres, codigo=nuevo_codigo_de_marcador()
    )
    desde_llm = await proveedor_llm.completar(sistema_para(interpretar), prompt_usuario)

    # 6) verificación de las afirmaciones (RN-033/034/035) y, si el modo lo permite, de la interpretación aparte
    resultado = verificar_respuesta(desde_llm, fragmentos, permitir_interpretacion=interpretar, pregunta=pregunta)
    if resultado.motivo is not None:
        return _sin_evidencia(resultado.motivo, resultado.descartadas)

    usadas = sorted({n for a in resultado.afirmaciones for n in a.referencias})
    citas = [
        _a_cita(n, fragmentos[n - 1], nombres.get(fragmentos[n - 1].documento_id, ""), empresa_nombre) for n in usadas
    ]
    afirmaciones = [Afirmacion(texto=a.texto, referencias=list(a.referencias), cita_textual=a.cita_textual) for a in resultado.afirmaciones]
    texto = "\n".join(f"{a.texto} " + "".join(f"[{n}]" for n in a.referencias) for a in resultado.afirmaciones)
    return ConsultaResponse(
        respuesta=texto,
        afirmaciones=afirmaciones,
        citas=citas,
        sin_evidencia=False,
        afirmaciones_descartadas=resultado.descartadas,
        interpretacion=resultado.interpretacion,
        interpretacion_descartada=resultado.interpretacion_descartada,
    )


async def responder_consulta(
    db: AsyncSession,
    db_vectorial: AsyncSession,
    *,
    pregunta: str,
    ambiente: str,
    proveedor_embeddings: ProveedorEmbeddingsConsulta,
    proveedor_llm: ProveedorLlm,
    sector: SectorEmpresa,
    empresa_id: uuid.UUID,
    anio: int,
    limite: int | None = None,
) -> ConsultaResponse:
    """Responde una consulta del Asistente IA. Punto COMÚN de `POST /asistente/consultas` y de la continuación de conversaciones.

    La parte documental es el RAG de siempre. Si además la pregunta (en la continuación, la pregunta AUTÓNOMA ya interpretada) nombra un
    código GRI explícito y pide su estado o una brecha, el servidor agrega el estado REGISTRADO en Análisis GRI de la misma empresa, año y
    ambiente, compuesto sin el modelo (ver `domain/estado_gri.py`). Sin esa petición el comportamiento es idéntico al anterior."""
    respuesta = await _responder_documental(
        db, db_vectorial, pregunta=pregunta, ambiente=ambiente, proveedor_embeddings=proveedor_embeddings, proveedor_llm=proveedor_llm,
        sector=sector, empresa_id=empresa_id, anio=anio, limite=limite,
    )
    pedidos = codigos_para_estado(pregunta)
    if not pedidos:
        return respuesta
    return await _con_estado_registrado(db, respuesta, pedidos, pregunta=pregunta, ambiente=ambiente, empresa_id=empresa_id, anio=anio)


async def _con_estado_registrado(
    db: AsyncSession, respuesta: ConsultaResponse, pedidos: list[CodigoSolicitado], *, pregunta: str, ambiente: str,
    empresa_id: uuid.UUID, anio: int,
) -> ConsultaResponse:
    """Agrega el estado a una respuesta DOCUMENTAL (mixta): `estado_registrado` (estructura + texto) y, por compatibilidad con los clientes que aún
    no lo leen, el mismo bloque en `mensaje`. NO toca `respuesta`, `afirmaciones`, `citas`, `sin_evidencia`, `motivo` ni `busqueda_documental`
    (REALIZADA): el estado manual no es una cita del documento. Sin evidencia documental, `mensaje` = estado + «Evidencia documental: » + el
    aviso de siempre."""
    maximo, cola = MAX_MENSAJE_AUSENCIA, ""
    if respuesta.sin_evidencia:
        cola = SEPARADOR_EVIDENCIA + (respuesta.mensaje or "")
        maximo -= len(cola)
    try:
        analisis = await consultar_analisis(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)  # misma lectura y vigencia de Análisis GRI
    except ConflictError:  # ANALYSIS_CHANGING: no se pierde la parte documental
        return respuesta.model_copy(
            update={"mensaje": f"{TITULO}: no se pudo leer en este momento porque el análisis está cambiando; vuelva a preguntar." + cola}
        )
    estado = construir_estado_registrado(
        pedidos, analisis, empresa_nombre=analisis.empresa.nombre, brecha=menciona_brecha(pregunta), maximo=maximo
    )
    return respuesta.model_copy(update={"estado_registrado": estado, "mensaje": estado.texto + cola})


async def responder_solo_estado(
    db: AsyncSession, *, pregunta: str, ambiente: str, sector: SectorEmpresa, empresa_id: uuid.UUID, anio: int
) -> ConsultaResponse:
    """Responde una pregunta INEQUÍVOCA de solo estado (`solo_estado`) únicamente desde Análisis GRI: sin recursos vectoriales, sin
    embeddings y sin modelo. Conserva el control de empresa (existe, activa, del sector) y la misma lectura y regla de vigencia de Análisis GRI,
    limitadas al ambiente, la empresa y el año de la consulta. La respuesta es válida SIN citas porque no hubo búsqueda documental
    (`NO_REALIZADA`): no declara ausencia de evidencia y el estado no es una cita ni una conclusión sobre una brecha. Si el análisis está
    cambiando se propaga el 409 `ANALYSIS_CHANGING` (no hay otra fuente que mostrar)."""
    try:
        validar_pregunta(pregunta)
    except ValueError as exc:
        raise BusinessValidationError("EMPTY_QUESTION", str(exc)) from None
    pedido = solo_estado(pregunta, anio)
    if pedido is None:  # defensa: el llamador ya clasificó; nunca se responde "solo estado" a otra cosa
        raise ValueError("La pregunta no es de solo estado.")
    await _empresa_consultable(db, empresa_id, sector)
    await db.rollback()
    analisis = await consultar_analisis(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
    estado = construir_estado_registrado(
        [pedido], analisis, empresa_nombre=analisis.empresa.nombre, brecha=False, maximo=MAX_MENSAJE_AUSENCIA
    )
    return ConsultaResponse(
        respuesta=estado.texto, sin_evidencia=False, estado_registrado=estado, busqueda_documental=BusquedaDocumental.NO_REALIZADA
    )


async def consultar_directa(
    db: AsyncSession,
    *,
    recursos: RecursosRag,
    pregunta: str,
    ambiente: str,
    sector: SectorEmpresa,
    empresa_id: uuid.UUID,
    anio: int,
) -> ConsultaResponse:
    """`POST /asistente/consultas`: decide ANTES de tocar los recursos documentales. Una pregunta de solo estado se responde desde Análisis
    GRI sin prepararlos. Todo lo demás (documental, mixta, ambigua, sin código) sigue el flujo de siempre: se preparan los recursos
    (mismos errores 503/502 que antes) y se ejecuta el RAG con su verificación de citas, más el estado si se pidió."""
    try:
        validar_pregunta(pregunta)
    except ValueError as exc:
        raise BusinessValidationError("EMPTY_QUESTION", str(exc)) from None
    if solo_estado(pregunta, anio) is not None:
        return await responder_solo_estado(db, pregunta=pregunta, ambiente=ambiente, sector=sector, empresa_id=empresa_id, anio=anio)
    embeddings, llm = recursos.preparar()  # 503 si falta el esquema vectorial o un proveedor
    async with recursos.sesion_vectorial() as db_vectorial:
        return await responder_consulta(
            db, db_vectorial, pregunta=pregunta, ambiente=ambiente, proveedor_embeddings=embeddings, proveedor_llm=llm,
            sector=sector, empresa_id=empresa_id, anio=anio,
        )


async def _nombres_documentos(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    filas = (await db.execute(select(Documento.id, Documento.nombre_archivo).where(Documento.id.in_(ids)))).all()
    return {fila.id: fila.nombre_archivo for fila in filas}


def _a_cita(referencia: int, f: FragmentoRecuperado, documento_nombre: str, empresa_nombre: str) -> Cita:
    extracto = f.texto_literal.strip()
    if len(extracto) > 300:
        extracto = extracto[:297] + "…"
    return Cita(
        referencia=referencia,
        documento_id=f.documento_id,
        documento_nombre=documento_nombre,
        empresa_id=f.empresa_id,
        empresa_nombre=empresa_nombre,
        seccion=f.seccion or "(sin sección)",
        anio=f.anio,
        extracto=extracto,
    )

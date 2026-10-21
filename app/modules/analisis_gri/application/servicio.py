"""Servicio del Módulo 5 (CU005): consulta, evaluación manual, historial y validación del análisis GRI por empresa y año.

DATOS Y FUENTES. Solo usa lo ALMACENADO en la base transaccional: `documentos.analisis` (resultado de la ingesta, que no se
modifica ni se vuelve a calcular) y las tablas del Módulo 5. No usa el gestor de ingesta, ni OCI, ni MinIO, ni la base vectorial,
ni ningún proveedor de IA: sigue funcionando aunque esos servicios no estén disponibles.

ELEGIBILIDAD. Un documento aporta datos solo si es del ambiente pedido, de la empresa y año, está COMPLETADO, tiene análisis
persistido y su publicación vectorial está CONFIRMADA (`vector_publicado_en`), y la empresa está activa. Fallidos, en proceso y
publicaciones pendientes no se exponen. OBSERVADO sí es elegible (está indexado para RAG) pero no aporta filas GRI ni habilita
por sí solo el reporte, y tampoco bloquea un análisis sustentado en otros documentos.

CONCURRENCIA. Toda escritura bloquea la fila `analisis_gri_control` de la empresa/año (`FOR UPDATE`): asignaciones y validaciones
de una misma empresa/año se serializan. Dos controles optimistas evitan sobrescrituras silenciosas (409):
- por fila: `version_fila_esperada` al asignar un estado;
- por análisis: `version` (cambios de estado) y `huella_documentos` (documentos elegibles) al validar.
Estado, historial y versión se confirman en UNA transacción; si cualquier paso falla no queda nada.

VIGENCIA. Una validación es vigente solo mientras la versión de estados y el conjunto de documentos elegibles sean los validados.
Se calcula al consultar: nada se actualiza ni se borra (las validaciones y el historial son de solo inserción).
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models import Empresa, Usuario
from app.models.analisis_gri import (
    AnalisisGriControl,
    AnalisisGriEstado,
    AnalisisGriHistorial,
    AnalisisGriValidacion,
    EstadoGri,
    TipoIdentidadGri,
)
from app.models.documento_ingesta import Documento, EstadoProcesamiento
from app.modules.analisis_gri.schemas import (
    ActorResumen,
    AnalisisResponse,
    AsignarEstadoResponse,
    EmpresaResumen,
    EstadoFlujo,
    HistorialItem,
    PaginaHistorial,
    ValidacionVista,
    ValidarAnalisisResponse,
)
from app.modules.analisis_gri.domain import calculo
from app.modules.analisis_gri.domain.calculo import DocumentoBase, EstadoAsignado, ValidacionGuardada

logger = logging.getLogger("igualab.analisis_gri")

MAX_LECTURAS_CONSISTENTES = 4
TAMANO_MAXIMO_PAGINA = 100


def _ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Lectura ---------------------------------------------------------------------------------------------------------

async def _empresa(db: AsyncSession, empresa_id: uuid.UUID) -> Empresa:
    # Columnas, no entidad: no depende del mapa de identidad de la sesión.
    fila = (
        await db.execute(
            select(Empresa.id, Empresa.nombre, Empresa.sector, Empresa.activa).where(Empresa.id == empresa_id)
        )
    ).one_or_none()
    if fila is None:
        raise NotFoundError("COMPANY_NOT_FOUND", "La empresa indicada no existe.")
    return fila


async def _documentos_elegibles(
    db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int
) -> list[DocumentoBase]:
    consulta = (
        select(
            Documento.id,
            Documento.nombre_archivo,
            Documento.sha256,
            Documento.tipo,
            Documento.resultado_analisis,
            Documento.completado_en,
            Documento.analisis,
        )
        .join(Empresa, Empresa.id == Documento.empresa_id)
        .where(
            Documento.ambiente == ambiente,
            Documento.empresa_id == empresa_id,
            Documento.anio == anio,
            Documento.estado_procesamiento == EstadoProcesamiento.COMPLETADO,
            Documento.resultado_analisis.is_not(None),
            Documento.analisis.is_not(None),
            Documento.vector_publicado_en.is_not(None),
            Empresa.activa.is_(True),
        )
        .order_by(Documento.completado_en, Documento.id)
    )
    return [
        DocumentoBase(
            id=f.id,
            nombre_archivo=f.nombre_archivo,
            sha256=f.sha256,
            tipo=f.tipo,
            resultado_analisis=f.resultado_analisis,
            completado_en=f.completado_en,
            analisis=f.analisis,
        )
        for f in (await db.execute(consulta)).all()
    ]


async def _version_de_control(db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int, *, bloquear: bool = False) -> int:
    consulta = select(AnalisisGriControl.version).where(
        AnalisisGriControl.ambiente == ambiente,
        AnalisisGriControl.empresa_id == empresa_id,
        AnalisisGriControl.anio == anio,
    )
    if bloquear:
        consulta = consulta.with_for_update()
    version = (await db.execute(consulta)).scalar_one_or_none()
    return 0 if version is None else version


async def _asegurar_y_bloquear_control(db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int) -> int:
    """Crea (si falta) la fila de control y la bloquea hasta el fin de la transacción. Devuelve su versión."""
    await db.execute(
        pg_insert(AnalisisGriControl)
        .values(ambiente=ambiente, empresa_id=empresa_id, anio=anio)
        .on_conflict_do_nothing(index_elements=["ambiente", "empresa_id", "anio"])
    )
    return await _version_de_control(db, ambiente, empresa_id, anio, bloquear=True)


async def _estados_actuales(db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int) -> dict[str, EstadoAsignado]:
    consulta = (
        select(
            AnalisisGriEstado.identidad,
            AnalisisGriEstado.estado,
            AnalisisGriEstado.version,
            AnalisisGriEstado.asignado_por,
            AnalisisGriEstado.asignado_en,
            Usuario.nombre,
        )
        .join(Usuario, Usuario.id == AnalisisGriEstado.asignado_por)
        .where(
            AnalisisGriEstado.ambiente == ambiente,
            AnalisisGriEstado.empresa_id == empresa_id,
            AnalisisGriEstado.anio == anio,
        )
    )
    return {
        f.identidad: EstadoAsignado(
            identidad=f.identidad,
            estado=EstadoGri(f.estado),
            version=f.version,
            asignado_por=ActorResumen(id=f.asignado_por, nombre=f.nombre),
            asignado_en=f.asignado_en,
        )
        for f in (await db.execute(consulta)).all()
    }


async def _ultima_validacion(db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int) -> ValidacionGuardada | None:
    v = AnalisisGriValidacion
    fila = (
        await db.execute(
            select(
                v.numero, v.version_estados, v.huella_documentos, v.documentos, v.estados,
                v.puntaje_suma, v.puntaje_filas, v.validado_por, v.validado_en, Usuario.nombre,
            )
            .join(Usuario, Usuario.id == v.validado_por)
            .where(v.ambiente == ambiente, v.empresa_id == empresa_id, v.anio == anio)
            .order_by(v.numero.desc())
            .limit(1)
        )
    ).one_or_none()
    if fila is None:
        return None
    return ValidacionGuardada(
        numero=fila.numero,
        version_estados=fila.version_estados,
        huella_documentos=fila.huella_documentos,
        documentos=fila.documentos,
        estados=fila.estados,
        puntaje_suma=fila.puntaje_suma,
        puntaje_filas=fila.puntaje_filas,
        validado_por=ActorResumen(id=fila.validado_por, nombre=fila.nombre),
        validado_en=fila.validado_en,
    )


@dataclass
class _Instantanea:
    """Todo lo necesario, leído de forma consistente."""

    empresa: object
    documentos: list[DocumentoBase]
    version: int
    huella: str
    estados: dict[str, EstadoAsignado]
    validacion: ValidacionGuardada | None


async def _leer(db: AsyncSession, ambiente: str, empresa_id: uuid.UUID, anio: int, *, consistente: bool) -> _Instantanea:
    """Lee los datos del análisis. Con `consistente=True` (lectura sin bloqueo) repite la lectura si la versión de estados cambió
    mientras se leía, de modo que versión, estados y validación correspondan al mismo instante. Dentro de una escritura la fila de
    control ya está bloqueada y no hace falta."""
    empresa = await _empresa(db, empresa_id)
    for _ in range(MAX_LECTURAS_CONSISTENTES if consistente else 1):
        antes = await _version_de_control(db, ambiente, empresa_id, anio)
        documentos = await _documentos_elegibles(db, ambiente, empresa_id, anio)
        estados = await _estados_actuales(db, ambiente, empresa_id, anio)
        validacion = await _ultima_validacion(db, ambiente, empresa_id, anio)
        despues = await _version_de_control(db, ambiente, empresa_id, anio)
        if antes == despues:
            return _Instantanea(empresa, documentos, antes, calculo.huella_de_documentos(documentos), estados, validacion)
    raise ConflictError(
        "ANALYSIS_CHANGING", "El análisis está cambiando; vuelva a consultarlo.", details={"reintentar": True}
    )


def _vista_validacion(validacion: ValidacionGuardada | None, motivos: list) -> ValidacionVista | None:
    if validacion is None:
        return None
    return ValidacionVista(
        numero=validacion.numero,
        vigente=not motivos,
        motivos_no_vigente=motivos,
        validado_por=validacion.validado_por,
        validado_en=validacion.validado_en,
        version_estados=validacion.version_estados,
        huella_documentos=validacion.huella_documentos,
        documentos_validados=len(validacion.documentos),
        filas_validadas=len(validacion.estados),
        puntaje_suma=validacion.puntaje_suma,
        puntaje_filas=validacion.puntaje_filas,
    )


def _construir(ambiente: str, anio: int, datos: _Instantanea) -> AnalisisResponse:
    empresa = datos.empresa
    filas, advertencias = calculo.construir_filas(datos.documentos)
    filas = calculo.aplicar_estados(filas, datos.estados)
    motivos = calculo.motivos_no_vigente(datos.validacion, datos.version, datos.huella)
    hay_documentos = bool(datos.documentos)
    # Una validación solo cuenta si hay filas que respaldar; las vacías no existen (CHECK en la base).
    vigente = datos.validacion is not None and not motivos and bool(filas)
    flujo = calculo.determinar_flujo(hay_documentos=hay_documentos, filas=filas, vigente=vigente)
    sanciones, resumen, agrupaciones = calculo.construir_sanciones_y_agrupaciones(datos.documentos)
    return AnalisisResponse(
        ambiente=ambiente,
        empresa=EmpresaResumen(id=empresa.id, nombre=empresa.nombre, sector=empresa.sector, activa=empresa.activa),
        anio=anio,
        version=datos.version,
        huella_documentos=datos.huella,
        estado_flujo=flujo,
        habilita_reporte=flujo is EstadoFlujo.VALIDADO,
        version_catalogo=sorted({str(d.analisis.get("version_catalogo", "")) for d in datos.documentos}),
        documentos=calculo.resumir_documentos(datos.documentos),
        filas=filas,
        advertencias=advertencias,
        sanciones=sanciones,
        agrupaciones_sanciones=agrupaciones,
        resumen_sanciones=resumen,
        puntaje=calculo.calcular_puntaje(filas, vigente=flujo is EstadoFlujo.VALIDADO, hay_documentos=hay_documentos),
        pendientes=calculo.construir_pendientes(
            empresa_activa=empresa.activa, hay_documentos=hay_documentos, filas=filas,
            validacion=datos.validacion, motivos=motivos, flujo=flujo,
        ),
        validacion=_vista_validacion(datos.validacion, motivos),
    )


async def consultar_analisis(db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int) -> AnalisisResponse:
    """CU005 pasos 1-2 y 5: filas GRI con evidencia, sanciones, puntaje, pendientes y validación. Solo lectura."""
    try:
        datos = await _leer(db, ambiente, empresa_id, anio, consistente=True)
        return _construir(ambiente, anio, datos)
    finally:
        await db.rollback()  # termina la transacción de solo lectura; no deja la conexión «idle in transaction»


# --- Escritura -------------------------------------------------------------------------------------------------------

async def _registrar_historial(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    identidad: str,
    codigo: str,
    edicion: str | None,
    identidad_tipo: TipoIdentidadGri,
    estado_anterior: EstadoGri | None,
    estado_nuevo: EstadoGri,
    usuario_id: uuid.UUID,
    cambiado_en: datetime,
    version_analisis: int,
) -> uuid.UUID:
    """Inserta la fila de historial (solo inserción). Función aparte para poder comprobar que un fallo aquí deshace TODO."""
    registro = AnalisisGriHistorial(
        id=uuid.uuid4(),
        ambiente=ambiente,
        empresa_id=empresa_id,
        anio=anio,
        identidad=identidad,
        codigo=codigo,
        edicion=edicion,
        identidad_tipo=identidad_tipo.value,
        estado_anterior=None if estado_anterior is None else estado_anterior.value,
        estado_nuevo=estado_nuevo.value,
        usuario_id=usuario_id,
        cambiado_en=cambiado_en,
        version_analisis=version_analisis,
    )
    db.add(registro)
    await db.flush()
    return registro.id


async def asignar_estado(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    identidad: str,
    estado: EstadoGri,
    version_fila_esperada: int,
    actor: Usuario,
) -> AsignarEstadoResponse:
    """CU005 pasos 3-4: asigna o cambia el estado de UNA fila y registra el historial. El autor es `actor` (sesión validada).
    No se propone ni se infiere ningún estado; la fila debe existir en el análisis actual (documentos elegibles)."""
    try:
        empresa = await _empresa(db, empresa_id)
        version_antes = await _asegurar_y_bloquear_control(db, ambiente, empresa_id, anio)
        datos = await _leer(db, ambiente, empresa_id, anio, consistente=False)
        filas, _ = calculo.construir_filas(datos.documentos)
        fila = next((f for f in filas if f.identidad == identidad), None)
        if fila is None:
            raise NotFoundError(
                "ANALYSIS_ROW_NOT_FOUND",
                "La identidad GRI indicada no está en el análisis de esta empresa y año.",
                details={"identidad": identidad, "empresa_activa": empresa.activa},
            )
        actual = datos.estados.get(identidad)
        version_fila_actual = 0 if actual is None else actual.version
        if version_fila_esperada != version_fila_actual:
            raise ConflictError(
                "ANALYSIS_ROW_VERSION_CONFLICT",
                "La fila cambió desde que se consultó (otro Administrador asignó un estado): vuelva a consultarla.",
                details={
                    "identidad": identidad,
                    "version_fila_esperada": version_fila_esperada,
                    "version_fila_actual": version_fila_actual,
                    "estado_actual": None if actual is None else actual.estado.value,
                },
            )
        motivos_previos = calculo.motivos_no_vigente(datos.validacion, datos.version, datos.huella)
        tenia_validacion_vigente = datos.validacion is not None and not motivos_previos and bool(filas)
        ahora = _ahora_utc()
        if actual is not None and actual.estado is estado:
            await db.commit()  # sin cambios: ni historial ni versión
            return AsignarEstadoResponse(
                identidad=identidad, cambio=False, estado_anterior=estado, estado=estado,
                version_fila=actual.version, version_analisis=datos.version,
                asignado_por=actual.asignado_por, asignado_en=actual.asignado_en,
                validacion_previa_invalidada=False,
            )
        version_nueva = version_antes + 1
        if actual is None:
            nueva_version_fila = 1
            db.add(
                AnalisisGriEstado(
                    id=uuid.uuid4(), ambiente=ambiente, empresa_id=empresa_id, anio=anio, identidad=identidad,
                    codigo=fila.codigo, edicion=fila.edicion, identidad_tipo=fila.identidad_tipo.value,
                    estado=estado.value, version=1, asignado_por=actor.id, asignado_en=ahora,
                )
            )
        else:
            nueva_version_fila = actual.version + 1
            await db.execute(
                update(AnalisisGriEstado)
                .where(
                    AnalisisGriEstado.ambiente == ambiente,
                    AnalisisGriEstado.empresa_id == empresa_id,
                    AnalisisGriEstado.anio == anio,
                    AnalisisGriEstado.identidad == identidad,
                )
                .values(estado=estado.value, version=nueva_version_fila, asignado_por=actor.id, asignado_en=ahora)
            )
        await db.flush()
        await _registrar_historial(
            db, ambiente=ambiente, empresa_id=empresa_id, anio=anio, identidad=identidad, codigo=fila.codigo,
            edicion=fila.edicion, identidad_tipo=fila.identidad_tipo,
            estado_anterior=None if actual is None else actual.estado, estado_nuevo=estado,
            usuario_id=actor.id, cambiado_en=ahora, version_analisis=version_nueva,
        )
        await db.execute(
            update(AnalisisGriControl)
            .where(
                AnalisisGriControl.ambiente == ambiente,
                AnalisisGriControl.empresa_id == empresa_id,
                AnalisisGriControl.anio == anio,
            )
            .values(version=version_nueva, actualizado_en=ahora)
        )
        await db.commit()  # estado + historial + versión: una sola transacción
    except Exception:
        await db.rollback()
        raise
    logger.info(
        "Análisis GRI: estado asignado (empresa=%s, anio=%s, identidad=%s, version_analisis=%s)",
        empresa_id, anio, identidad, version_nueva,
    )
    return AsignarEstadoResponse(
        identidad=identidad, cambio=True, estado_anterior=None if actual is None else actual.estado, estado=estado,
        version_fila=nueva_version_fila, version_analisis=version_nueva,
        asignado_por=ActorResumen(id=actor.id, nombre=actor.nombre), asignado_en=ahora,
        validacion_previa_invalidada=tenia_validacion_vigente,
    )


async def consultar_historial(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    identidad: str | None = None,
    pagina: int = 1,
    tamano: int = 20,
) -> PaginaHistorial:
    """Cambios de estado de la empresa/año (recientes primero), opcionalmente de una sola identidad. Solo lectura."""
    h = AnalisisGriHistorial
    try:
        await _empresa(db, empresa_id)
        filtro = [h.ambiente == ambiente, h.empresa_id == empresa_id, h.anio == anio]
        if identidad is not None:
            filtro.append(h.identidad == identidad)
        total = (await db.execute(select(func.count()).select_from(h).where(*filtro))).scalar_one()
        filas = (
            await db.execute(
                select(h, Usuario.nombre)
                .join(Usuario, Usuario.id == h.usuario_id)
                .where(*filtro)
                .order_by(h.cambiado_en.desc(), h.version_analisis.desc(), h.id)
                .offset((pagina - 1) * tamano)
                .limit(tamano)
            )
        ).all()
        items = [
            HistorialItem(
                id=r.id, identidad=r.identidad, codigo=r.codigo, edicion=r.edicion,
                identidad_tipo=TipoIdentidadGri(r.identidad_tipo),
                estado_anterior=None if r.estado_anterior is None else EstadoGri(r.estado_anterior),
                estado_nuevo=EstadoGri(r.estado_nuevo), usuario=ActorResumen(id=r.usuario_id, nombre=nombre),
                cambiado_en=r.cambiado_en, version_analisis=r.version_analisis,
            )
            for r, nombre in filas
        ]
        return PaginaHistorial(items=items, total=total, pagina=pagina, tamano=tamano)
    finally:
        await db.rollback()


async def validar_analisis(
    db: AsyncSession,
    *,
    ambiente: str,
    empresa_id: uuid.UUID,
    anio: int,
    version_esperada: int,
    huella_esperada: str,
    actor: Usuario,
) -> ValidarAnalisisResponse:
    """CU005 paso 6 / «Validar análisis». Procede solo si los datos son los que el Administrador consultó, existen documentos
    elegibles y filas GRI, y TODAS las filas tienen estado. Persiste quién, cuándo y sobre qué versión y documentos.
    409 con código específico en cada caso que no procede. No genera ningún reporte."""
    try:
        await _empresa(db, empresa_id)
        await _asegurar_y_bloquear_control(db, ambiente, empresa_id, anio)
        datos = await _leer(db, ambiente, empresa_id, anio, consistente=False)
        if version_esperada != datos.version or huella_esperada != datos.huella:
            raise ConflictError(
                "ANALYSIS_VERSION_CONFLICT",
                "Los datos cambiaron desde que se consultaron (estados o documentos elegibles): consulte de nuevo antes de validar.",
                details={
                    "version_esperada": version_esperada,
                    "version_actual": datos.version,
                    "documentos_cambiaron": huella_esperada != datos.huella,
                },
            )
        if not datos.documentos:
            raise ConflictError(
                "ANALYSIS_NO_ELIGIBLE_DOCUMENTS",
                "No hay documentos elegibles (completados, con análisis y publicación vectorial confirmada) de una empresa "
                "activa para este año: no hay nada que validar.",
            )
        filas, _ = calculo.construir_filas(datos.documentos)
        filas = calculo.aplicar_estados(filas, datos.estados)
        if not filas:
            raise ConflictError(
                "ANALYSIS_NO_GRI_CODES",
                "Los documentos elegibles no contienen códigos GRI evaluables: el puntaje ESG no está disponible y no se "
                "puede validar el análisis (las sanciones o los observados no lo habilitan por sí solos).",
            )
        sin_estado = [f.identidad for f in filas if f.estado is None]
        if sin_estado:
            raise ConflictError(
                "ANALYSIS_PENDING_STATES",
                f"Faltan estados por asignar en {len(sin_estado)} de {len(filas)} filas: no se puede validar.",
                details={"filas_sin_estado": sin_estado, "total_filas": len(filas)},
            )
        if datos.validacion is not None and not calculo.motivos_no_vigente(datos.validacion, datos.version, datos.huella):
            raise ConflictError(
                "ANALYSIS_ALREADY_VALIDATED",
                "El análisis ya tiene una validación vigente.",
                details={"numero": datos.validacion.numero},
            )
        puntaje = calculo.calcular_puntaje(filas, vigente=True, hay_documentos=True)
        ahora = _ahora_utc()
        numero = 1 if datos.validacion is None else datos.validacion.numero + 1
        db.add(
            AnalisisGriValidacion(
                id=uuid.uuid4(), ambiente=ambiente, empresa_id=empresa_id, anio=anio, numero=numero,
                version_estados=datos.version, huella_documentos=datos.huella,
                documentos=calculo.documentos_en_foto(datos.documentos), estados=calculo.estados_en_foto(filas),
                puntaje_suma=puntaje.suma_puntos, puntaje_filas=puntaje.filas_contadas,
                validado_por=actor.id, validado_en=ahora,
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    logger.info(
        "Análisis GRI validado (empresa=%s, anio=%s, numero=%s, version=%s)", empresa_id, anio, numero, datos.version
    )
    validacion = ValidacionGuardada(
        numero=numero, version_estados=datos.version, huella_documentos=datos.huella,
        documentos=calculo.documentos_en_foto(datos.documentos), estados=calculo.estados_en_foto(filas),
        puntaje_suma=puntaje.suma_puntos, puntaje_filas=puntaje.filas_contadas,
        validado_por=ActorResumen(id=actor.id, nombre=actor.nombre), validado_en=ahora,
    )
    return ValidarAnalisisResponse(
        validacion=_vista_validacion(validacion, []),
        puntaje=puntaje,
        estado_flujo=EstadoFlujo.VALIDADO,
        habilita_reporte=True,
    )


# --- Para el Módulo 7 -------------------------------------------------------------------------------------------------

async def obtener_analisis_validado(db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int) -> AnalisisResponse:
    """Función reutilizable por el Módulo 7 (reporte de prospección). Comprueba la elegibilidad EN LA BASE (nunca una bandera
    del frontend) y entrega el análisis cuya validación sigue vigente: filas con estado y evidencia, sanciones, puntaje
    DEFINITIVO, versión, huella de documentos y datos de la validación.

    Lanza `ConflictError("ANALYSIS_NOT_VALIDATED")` con el `estado_flujo` y los pendientes si no hay validación vigente, no hay
    documentos elegibles o no hay filas GRI (solo sanciones u observados no bastan). `NotFoundError` si la empresa no existe.
    No genera el reporte ni lo registra: eso lo hace el Módulo 7."""
    analisis = await consultar_analisis(db, ambiente=ambiente, empresa_id=empresa_id, anio=anio)
    if analisis.estado_flujo is not EstadoFlujo.VALIDADO or analisis.validacion is None or not analisis.validacion.vigente:
        raise ConflictError(
            "ANALYSIS_NOT_VALIDATED",
            "El análisis GRI de esta empresa y año no tiene una validación vigente: no se puede generar el reporte.",
            details={
                "estado_flujo": analisis.estado_flujo.value,
                "pendientes": [p.codigo for p in analisis.pendientes],
            },
        )
    return analisis

"""Etapa 4A: reserva de documentos, duplicados, almacenamiento y compensación.

Nivel: SQLite aislado + ALMACÉN SIMULADO EN MEMORIA. Verifica la lógica secuencial y las
restricciones de BD; NO demuestra la concurrencia de PostgreSQL (ver
`test_documento_postgres.py`) ni el comportamiento de MinIO real.
"""
import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.exception_handlers import register_exception_handlers
from app.exceptions import (
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    ExternalServiceError,
    ExternalServiceTimeoutError,
    NotFoundError,
)
from app.models import RolUsuario, SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from app.request_id import RequestIDMiddleware
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta import documento_service as servicio
from app.services.ingesta.almacenamiento import (
    EstadoSubida,
    ReferenciaOriginal,
    SubidaConocida,
    generar_clave_original,
)
from app.services.ingesta.reglas import TAMANO_MAXIMO_BYTES
from tests.ayudantes_ingesta import (
    BOM,
    AlmacenEnMemoria,
    SesionSQLite,
    crear_empresa,
    crear_usuario,
    metadatos,
    sha256_de,
)

CONTENIDO = BOM + "# Memoria anual\r\nÑandú, acción y pingüino.\r\n".encode("utf-8")


@pytest.fixture
async def e():
    db = SesionSQLite()
    usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
    empresa = await crear_empresa(db)
    entorno = SimpleNamespace(db=db, usuario=usuario, empresa=empresa, almacen=AlmacenEnMemoria(sesion=db))
    yield entorno
    db.cerrar()


def datos_unicos(n: int) -> bytes:
    return f"# Documento {n}\n".encode("utf-8")


async def reservar(e, datos=CONTENIDO, *, nombre="Memoria anual 2025.md", empresa=None, anio=2025,
                   tipo=TipoDocumento.MEMORIA_ANUAL, sector=None, ambiente="development", usuario=None):
    documento = await servicio.reservar_documento(
        e.db,
        metadatos=metadatos(empresa or e.empresa, anio, tipo, sector),
        nombre_archivo=nombre,
        sha256=sha256_de(datos),
        tamano_bytes=len(datos),
        usuario_id=(usuario or e.usuario).id,
        ambiente=ambiente,
    )
    return documento, datos


def recargar(e, documento: Documento) -> Documento:
    e.db.sync.expire_all()
    return e.db.sync.get(Documento, documento.id)


async def completar(e, documento, datos, resultado=ResultadoAnalisis.OBSERVADO):
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    return await servicio.publicar_documento(
        e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=resultado
    )


async def fallar_subida(e, documento, datos, error=None):
    e.almacen.fallos["guardar"] = [error or ExternalServiceError("STORAGE_ERROR", "El almacenamiento devolvió un error.")]
    with pytest.raises(ExternalServiceError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)


async def conflicto(e, **kwargs) -> ConflictError:
    with pytest.raises(ConflictError) as capturado:
        await reservar(e, **kwargs)
    return capturado.value


# =============================== Reserva: metadatos y estado inicial ===============================

async def test_la_reserva_registra_metadatos_y_deja_el_documento_en_proceso(e):
    documento, datos = await reservar(e)
    assert documento.ambiente == "development"
    assert (documento.empresa_id, documento.anio, documento.tipo) == (e.empresa.id, 2025, TipoDocumento.MEMORIA_ANUAL)
    assert documento.sector is e.empresa.sector
    assert documento.nombre_archivo == "Memoria anual 2025.md"
    assert (documento.sha256, documento.tamano_bytes) == (hashlib.sha256(datos).hexdigest(), len(datos))
    assert documento.usuario_id == e.usuario.id and documento.creado_en is not None
    assert documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert documento.resultado_analisis is None
    assert documento.estado_compensacion is EstadoCompensacion.NINGUNA and documento.reserva_activa is True
    assert documento.original_almacenado_en is None and documento.almacenamiento_intentado_en is None
    assert documento.disponible_para_rag is False
    assert e.almacen.llamadas == []  # reservar no toca el almacén
    assert e.db.in_transaction() is False


async def test_la_clave_usa_ambiente_y_uuid_del_servidor_nunca_el_nombre_recibido(e):
    documento, _ = await reservar(e, nombre="../../x Memoria anual 2025.md".replace("../../", ""))
    assert documento.clave_original == generar_clave_original("development", documento.id)
    assert "Memoria" not in documento.clave_original and ".md" == documento.clave_original[-3:]
    assert documento.nombre_archivo.startswith("x Memoria")  # el nombre es solo metadata
    qa, _ = await reservar(e, datos_unicos(1), ambiente="qa")
    assert qa.clave_original.startswith("qa/") and qa.ambiente == "qa"


async def test_el_sector_se_toma_de_la_empresa_y_el_declarado_solo_se_contrasta(e):
    documento, _ = await reservar(e, sector=SectorEmpresa.MINERIA)
    assert documento.sector is SectorEmpresa.MINERIA
    with pytest.raises(BusinessValidationError) as capturado:
        await reservar(e, datos_unicos(2), anio=2024, sector=SectorEmpresa.ENERGIA)
    assert capturado.value.code == "COMPANY_SECTOR_MISMATCH"


async def test_empresa_inexistente_o_inactiva_no_reserva_nada(e):
    with pytest.raises(NotFoundError):
        await servicio.reservar_documento(
            e.db,
            metadatos=MetadatosIngestaRequest(empresa_id=uuid.uuid4(), anio=2025, tipo=TipoDocumento.MEMORIA_ANUAL),
            nombre_archivo="a.md", sha256="a" * 64, tamano_bytes=3, usuario_id=e.usuario.id, ambiente="development",
        )
    inactiva = await crear_empresa(e.db, nombre="Inactiva", activa=False)
    with pytest.raises(BusinessValidationError) as capturado:
        await reservar(e, empresa=inactiva)
    assert capturado.value.code == "COMPANY_INACTIVE"
    assert e.db.sync.scalars(select(Documento)).all() == []


async def test_solo_un_superadmin_habilitado_puede_reservar(e):
    admin = await crear_usuario(e.db, rol=RolUsuario.ADMINISTRADOR)
    with pytest.raises(AuthorizationError) as capturado:
        await reservar(e, usuario=admin)
    assert capturado.value.code == "INGESTION_FORBIDDEN"
    with pytest.raises(AuthorizationError):
        await reservar(e, usuario=SimpleNamespace(id=uuid.uuid4()))
    e.db.sync.execute(update(type(e.usuario)).where(type(e.usuario).id == e.usuario.id).values(habilitado=False))
    e.db.sync.commit()
    with pytest.raises(AuthorizationError):
        await reservar(e)
    assert e.db.sync.scalars(select(Documento)).all() == []


@pytest.mark.parametrize("cambios", [
    dict(sha256="A" * 64), dict(sha256="a" * 63), dict(sha256="g" * 64), dict(sha256=None),
    dict(tamano_bytes=0), dict(tamano_bytes=-1), dict(tamano_bytes=True), dict(tamano_bytes=TAMANO_MAXIMO_BYTES + 1),
    dict(tamano_bytes=1.5), dict(nombre_archivo="../../x.md"), dict(nombre_archivo="a\nb.md"), dict(nombre_archivo="x.pdf"),
])
async def test_entradas_invalidas_se_rechazan_sin_reservar(e, cambios):
    valores = dict(nombre_archivo="a.md", sha256="a" * 64, tamano_bytes=3)
    valores.update(cambios)
    with pytest.raises((BusinessValidationError, ValueError)):
        await servicio.reservar_documento(
            e.db, metadatos=metadatos(e.empresa), usuario_id=e.usuario.id, ambiente="development", **valores
        )
    assert e.db.sync.scalars(select(Documento)).all() == []


@pytest.mark.parametrize("ambiente", ["", "Dev", "../qa", "a/b", None])
async def test_ambiente_invalido(e, ambiente):
    with pytest.raises(ValueError):
        await servicio.reservar_documento(
            e.db, metadatos=metadatos(e.empresa), nombre_archivo="a.md", sha256="a" * 64, tamano_bytes=3,
            usuario_id=e.usuario.id, ambiente=ambiente,
        )


async def test_el_tamano_maximo_exacto_se_acepta_pero_no_uno_mas():
    db = SesionSQLite()
    try:
        usuario, empresa = await crear_usuario(db), await crear_empresa(db)
        for n, tamano in enumerate((TAMANO_MAXIMO_BYTES, TAMANO_MAXIMO_BYTES + 1)):
            args = dict(metadatos=metadatos(empresa, anio=2025 - n), nombre_archivo="a.md", sha256=f"{n}" * 64,
                        tamano_bytes=tamano, usuario_id=usuario.id, ambiente="development")
            if n == 0:
                assert (await servicio.reservar_documento(db, **args)).tamano_bytes == 50_000_000
            else:
                with pytest.raises(BusinessValidationError):
                    await servicio.reservar_documento(db, **args)
    finally:
        db.cerrar()


# =============================== Duplicados ===============================

async def test_documento_completado_bloquea_por_hash_con_fecha_y_cuenta_originales(e):
    original, datos = await reservar(e)
    await completar(e, original, datos)
    otra = await crear_empresa(e.db, nombre="Otra Empresa")
    error = await conflicto(e, datos=datos, empresa=otra, anio=2020)  # mismo contenido, otra combinación

    assert error.code == "DOCUMENT_ALREADY_INGESTED"
    assert error.details["criterio"] == ["sha256"]
    assert error.details["documento_id"] == str(original.id)
    assert error.details["cuenta"] == "superadmin@pruebas.invalid"
    cargado = datetime.fromisoformat(error.details["cargado_en"])
    assert cargado.tzinfo is not None and cargado.utcoffset() == timedelta(0)
    assert abs((cargado - recargar(e, original).creado_en.replace(tzinfo=timezone.utc)).total_seconds()) < 1
    visible = f"{error} {error.message} {error.details!r}"
    assert sha256_de(datos) not in visible and "Memoria anual 2025.md" not in visible and original.clave_original not in visible


async def test_documento_completado_bloquea_por_empresa_anio_tipo(e):
    original, datos = await reservar(e)
    await completar(e, original, datos)
    error = await conflicto(e, datos=datos_unicos(7))
    assert error.code == "DOCUMENT_ALREADY_INGESTED" and error.details["criterio"] == ["empresa_anio_tipo"]
    ambos = await conflicto(e, datos=datos)
    assert ambos.details["criterio"] == ["sha256", "empresa_anio_tipo"]


@pytest.mark.parametrize("resultado", list(ResultadoAnalisis))
async def test_un_completado_bloquea_duplicados_incluido_observado(e, resultado):
    original, datos = await reservar(e)
    await completar(e, original, datos, resultado)
    assert (await conflicto(e, datos=datos)).code == "DOCUMENT_ALREADY_INGESTED"


async def test_otro_tipo_o_anio_o_empresa_con_otro_contenido_si_se_admite(e):
    original, datos = await reservar(e)
    await completar(e, original, datos)
    await reservar(e, datos_unicos(1), tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    await reservar(e, datos_unicos(2), anio=2024)
    otra = await crear_empresa(e.db, nombre="Otra", sector=SectorEmpresa.PETROLEO)
    await reservar(e, datos_unicos(3), empresa=otra)


async def test_carga_en_curso_impide_otra_reserva_equivalente_sin_revelar_la_cuenta(e):
    await reservar(e)
    por_hash = await conflicto(e, datos=CONTENIDO, anio=2020)
    por_combinacion = await conflicto(e, datos=datos_unicos(1))
    for error, criterio in ((por_hash, ["sha256"]), (por_combinacion, ["empresa_anio_tipo"])):
        assert error.code == "DOCUMENT_UPLOAD_IN_PROGRESS" and error.details == {"criterio": criterio}


async def test_si_hay_varios_conflictos_se_informa_primero_el_completado(e):
    a, datos_a = await reservar(e, datos_unicos(1), tipo=TipoDocumento.MEMORIA_ANUAL)
    await completar(e, a, datos_a)
    await reservar(e, datos_unicos(2), tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    # Mismo hash que `a` (completado) y misma combinación que el reporte (en curso).
    error = await conflicto(e, datos=datos_a, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    assert error.code == "DOCUMENT_ALREADY_INGESTED"


async def test_cada_ambiente_tiene_su_propio_espacio_de_reservas(e):
    a, datos = await reservar(e)
    en_qa, _ = await reservar(e, datos, ambiente="qa")
    assert a.id != en_qa.id


async def test_solo_se_traducen_las_violaciones_de_los_indices_de_reserva(e, monkeypatch):
    # Se desactiva la consulta previa para que decida únicamente la restricción de la BD.
    await reservar(e)

    async def sin_consulta(*args, **kwargs):
        return None

    monkeypatch.setattr(servicio, "_buscar_conflicto", sin_consulta)
    with pytest.raises(ConflictError) as capturado:
        await reservar(e, datos_unicos(1))  # misma empresa/año/tipo
    assert capturado.value.code == "DOCUMENT_RESERVATION_CONFLICT"
    with pytest.raises(ConflictError):
        await reservar(e, CONTENIDO, anio=2020)  # mismo hash
    assert len(e.db.sync.scalars(select(Documento)).all()) == 1


async def test_cualquier_otra_integrityerror_se_propaga_sin_traducir(e, monkeypatch):
    mismo_id = uuid.uuid4()
    monkeypatch.setattr(servicio.uuid, "uuid4", lambda: mismo_id)
    await reservar(e)
    with pytest.raises(IntegrityError):  # clave primaria repetida: no es una reserva duplicada
        await reservar(e, datos_unicos(1), anio=2024)


def test_clasificacion_de_integrityerror():
    def error(texto, causa=None):
        original = Exception(texto)
        if causa is not None:
            original.__cause__ = causa
        return IntegrityError("INSERT", {}, original)

    causa = Exception("duplicado")
    causa.constraint_name = "uq_documentos_sha256_activo"
    assert servicio._es_violacion_de_reserva(error("adaptado", causa))
    assert servicio._es_violacion_de_reserva(error('duplicate key violates unique constraint "uq_documentos_empresa_anio_tipo_activo"'))
    assert servicio._es_violacion_de_reserva(error("UNIQUE constraint failed: documentos.ambiente, documentos.sha256"))
    assert not servicio._es_violacion_de_reserva(error('duplicate key violates unique constraint "documentos_pkey"'))
    assert not servicio._es_violacion_de_reserva(error('violates foreign key constraint "documentos_empresa_id_fkey"'))
    assert not servicio._es_violacion_de_reserva(error("UNIQUE constraint failed: documentos.clave_original"))
    assert not servicio._es_violacion_de_reserva(error("CHECK constraint failed: ck_documentos_tamano_positivo"))


# =============================== Almacenamiento del original ===============================

async def test_el_original_se_guarda_con_los_bytes_exactos_bom_incluido_y_sha256_intacto(e):
    documento, datos = await reservar(e)
    guardado = await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)

    assert e.almacen.objetos == {(documento.clave_original, None): datos}
    assert e.almacen.objetos[(documento.clave_original, None)].startswith(BOM)
    assert sha256_de(e.almacen.objetos[(documento.clave_original, None)]) == guardado.sha256 == sha256_de(datos)
    assert guardado.original_almacenado_en is not None and guardado.almacenamiento_intentado_en is not None
    # Con el original almacenado NO está disponible ni completado.
    assert guardado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert guardado.resultado_analisis is None and guardado.disponible_para_rag is False
    await servicio.verificar_original(e.db, e.almacen, documento.id)


async def test_ninguna_transaccion_de_bd_esta_abierta_durante_las_llamadas_al_almacen(e):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    await servicio.verificar_original(e.db, e.almacen, documento.id)
    otro, datos2 = await reservar(e, datos_unicos(5), anio=2024)
    e.almacen.guardar_y_fallar = True  # resultado incierto: la compensación consulta, lista y elimina
    await fallar_subida(e, otro, datos2)  # incluye fallar + compensar
    operaciones = [op for op, _ in e.almacen.llamadas]
    assert {"guardar", "leer", "estado_subida", "listar_versiones", "eliminar"} <= set(operaciones)
    assert e.almacen.transaccion_abierta_en_llamada and not any(e.almacen.transaccion_abierta_en_llamada)


async def test_contenido_distinto_del_reservado_se_rechaza_sin_cambiar_estado_ni_subir(e):
    documento, datos = await reservar(e)
    for malo in (datos + b"x", datos[:-1], b"x" * len(datos), "texto"):
        with pytest.raises(BusinessValidationError) as capturado:
            await servicio.almacenar_original(e.db, e.almacen, documento.id, malo, token=documento.ejecucion_token)
        assert capturado.value.code == "ORIGINAL_CONTENT_MISMATCH"
    assert e.almacen.llamadas == []
    assert recargar(e, documento).almacenamiento_intentado_en is None


async def test_el_original_solo_se_sube_una_vez_y_nunca_se_sobrescribe(e):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    with pytest.raises(ConflictError) as capturado:
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert capturado.value.code == "DOCUMENT_STATE_CONFLICT"
    assert [op for op, _ in e.almacen.llamadas].count("guardar") == 1


async def test_un_almacen_de_otro_ambiente_se_rechaza(e):
    documento, datos = await reservar(e)
    with pytest.raises(BusinessValidationError) as capturado:
        await servicio.almacenar_original(e.db, AlmacenEnMemoria("qa"), documento.id, datos, token=documento.ejecucion_token)
    assert capturado.value.code == "STORAGE_ENVIRONMENT_MISMATCH"
    assert recargar(e, documento).almacenamiento_intentado_en is None


async def test_documento_inexistente_o_ya_fallido_no_se_almacena(e):
    with pytest.raises(NotFoundError):
        await servicio.almacenar_original(e.db, e.almacen, uuid.uuid4(), CONTENIDO, token=uuid.uuid4())
    documento, datos = await reservar(e)
    await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", token=documento.ejecucion_token)
    with pytest.raises(ConflictError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert e.almacen.llamadas == []


async def test_con_versionado_se_guarda_la_version_y_la_compensacion_elimina_solo_esa_version(e):
    e.almacen.versionado = True
    documento, datos = await reservar(e)
    guardado = await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert guardado.version_id_original == "version-1"
    e.almacen.objetos[(documento.clave_original, "version-previa")] = b"otra version"  # ajena a esta operación
    await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", requiere_compensacion=True, token=documento.ejecucion_token)
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert e.almacen.claves() == {documento.clave_original}
    assert (documento.clave_original, "version-previa") in e.almacen.objetos


async def test_con_versionado_y_subida_incierta_la_version_se_encuentra_y_se_elimina_por_su_id(e):
    """Antes (limitación): eliminar sin versión dejaba la versión detrás de una marca de borrado y
    la compensación informaba éxito. Ahora la versión desconocida se reconcilia listando la clave."""
    e.almacen.versionado = True
    e.almacen.guardar_y_fallar = True
    documento, datos = await reservar(e)
    await fallar_subida(e, documento, datos, ExternalServiceTimeoutError("STORAGE_TIMEOUT", "plazo"))
    assert recargar(e, documento).estado_compensacion is EstadoCompensacion.COMPLETADA
    assert e.almacen.objetos == {} and e.almacen.marcas == set()  # sin versión residual ni marca de borrado
    assert ("eliminar", documento.clave_original) in e.almacen.llamadas
    assert recargar(e, documento).disponible_para_rag is False


# =============================== Fallos de subida, compensación y reintento ===============================

async def test_fallo_de_subida_compensado_libera_la_reserva_y_permite_reintentar(e):
    documento, datos = await reservar(e)
    await fallar_subida(e, documento, datos)
    fallido = recargar(e, documento)
    assert fallido.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert fallido.motivo_fallo == "STORAGE_ERROR" and fallido.fallido_en is not None
    assert fallido.estado_compensacion is EstadoCompensacion.COMPLETADA and fallido.compensada_en is not None
    assert fallido.reserva_activa is False and fallido.disponible_para_rag is False
    assert e.almacen.objetos == {}
    # Reintento: mismo contenido y misma combinación, con un documento nuevo.
    reintento, _ = await reservar(e, datos)
    assert reintento.id != documento.id and reintento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    await servicio.almacenar_original(e.db, e.almacen, reintento.id, datos, token=reintento.ejecucion_token)
    assert e.almacen.claves() == {reintento.clave_original}


async def test_timeout_con_resultado_incierto_deja_el_objeto_y_la_compensacion_lo_elimina(e):
    e.almacen.guardar_y_fallar = True  # el objeto SÍ se creó aunque la llamada falló
    documento, datos = await reservar(e)
    with pytest.raises(ExternalServiceTimeoutError):
        e.almacen.fallos["guardar"] = [ExternalServiceTimeoutError("STORAGE_TIMEOUT", "plazo", details={"resultado_incierto": True})]
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert recargar(e, documento).motivo_fallo == "STORAGE_TIMEOUT"
    assert e.almacen.objetos == {}  # compensado
    assert recargar(e, documento).reserva_activa is False


async def test_compensacion_fallida_conserva_la_reserva_y_no_informa_limpieza(e):
    documento, datos = await reservar(e)
    e.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
    e.almacen.guardar_y_fallar = True
    await fallar_subida(e, documento, datos)

    pendiente = recargar(e, documento)
    assert pendiente.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert pendiente.estado_compensacion is EstadoCompensacion.PENDIENTE
    assert pendiente.reserva_activa is True and pendiente.compensada_en is None
    assert pendiente.compensacion_intentos == 1 and pendiente.ultimo_error_compensacion == "STORAGE_ERROR"
    assert len(e.almacen.objetos) == 1  # el residuo sigue ahí y está registrado
    error = await conflicto(e, datos=datos)
    assert error.code == "DOCUMENT_CLEANUP_PENDING"
    assert (await conflicto(e, datos=datos_unicos(9))).code == "DOCUMENT_CLEANUP_PENDING"


async def test_si_el_objeto_sigue_existiendo_tras_eliminar_la_compensacion_no_se_confirma(e):
    documento, datos = await reservar(e)
    e.almacen.guardar_y_fallar = True
    e.almacen.eliminar_sin_efecto = True
    await fallar_subida(e, documento, datos)
    pendiente = recargar(e, documento)
    assert pendiente.estado_compensacion is EstadoCompensacion.PENDIENTE and pendiente.reserva_activa is True
    assert pendiente.ultimo_error_compensacion == "STORAGE_CLEANUP_NOT_CONFIRMED"


async def test_un_error_al_comprobar_la_ausencia_tampoco_confirma_la_limpieza(e):
    documento, datos = await reservar(e)
    e.almacen.fallos["existe"] = [ExternalServiceError("STORAGE_TIMEOUT", "plazo")]
    await fallar_subida(e, documento, datos)
    assert recargar(e, documento).estado_compensacion is EstadoCompensacion.PENDIENTE


async def test_la_recuperacion_completa_la_compensacion_y_recien_entonces_libera_la_reserva(e):
    documento, datos = await reservar(e)
    e.almacen.guardar_y_fallar = True
    e.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
    await fallar_subida(e, documento, datos)
    assert (await conflicto(e, datos=datos)).code == "DOCUMENT_CLEANUP_PENDING"

    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc), limite=10
    )
    assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (0, 1, 0)
    limpio = recargar(e, documento)
    assert limpio.estado_compensacion is EstadoCompensacion.COMPLETADA and limpio.reserva_activa is False
    assert limpio.ultimo_error_compensacion is None and e.almacen.objetos == {}
    await reservar(e, datos)  # ahora sí se puede reintentar


async def test_recuperacion_que_vuelve_a_fallar_acumula_intentos_y_mantiene_la_reserva(e):
    documento, datos = await reservar(e)
    e.almacen.guardar_y_fallar = True
    e.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "1"), ExternalServiceError("STORAGE_ERROR", "2")]
    await fallar_subida(e, documento, datos)
    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc), limite=10
    )
    assert (resumen.compensados, resumen.pendientes) == (0, 1)
    pendiente = recargar(e, documento)
    assert pendiente.compensacion_intentos == 2 and pendiente.reserva_activa is True


async def test_la_compensacion_es_repetible_e_idempotente(e):
    documento, datos = await reservar(e)
    await fallar_subida(e, documento, datos)
    llamadas = len(e.almacen.llamadas)
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert len(e.almacen.llamadas) == llamadas  # ya estaba limpio: no vuelve a tocar el almacén


async def test_eliminar_ya_eliminado_no_es_error_aunque_se_repita_la_llamada(e):
    documento, datos = await reservar(e)
    e.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
    await fallar_subida(e, documento, datos)  # objeto nunca creado, compensación pendiente
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.COMPLETADA


async def test_la_compensacion_solo_toca_objetos_propios(e):
    ajeno = f"development/documentos/{uuid.uuid4()}/original.md"
    e.almacen.poner_ajeno(ajeno)
    otro, otros_datos = await reservar(e, datos_unicos(1), anio=2024)
    await servicio.almacenar_original(e.db, e.almacen, otro.id, otros_datos, token=otro.ejecucion_token)
    documento, datos = await reservar(e, datos_unicos(2), anio=2023)
    e.almacen.guardar_y_fallar = True
    await fallar_subida(e, documento, datos)
    assert e.almacen.claves() == {ajeno, otro.clave_original}
    eliminadas = [clave for op, clave in e.almacen.llamadas if op == "eliminar"]
    assert eliminadas == [documento.clave_original]


@pytest.mark.parametrize("preparar", ["en_proceso", "completado"])
async def test_nunca_se_compensa_un_documento_en_proceso_ni_completado(e, preparar):
    documento, datos = await reservar(e)
    if preparar == "completado":
        await completar(e, documento, datos)
    else:
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    antes = list(e.almacen.llamadas)
    with pytest.raises(ConflictError):
        await servicio.compensar_documento(e.db, e.almacen, documento.id)
    assert e.almacen.llamadas == antes and (documento.clave_original, None) in e.almacen.objetos


async def test_compensar_un_documento_inexistente(e):
    with pytest.raises(NotFoundError):
        await servicio.compensar_documento(e.db, e.almacen, uuid.uuid4())


async def test_un_almacen_de_otro_ambiente_no_compensa(e):
    documento, datos = await reservar(e)
    await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", requiere_compensacion=True, token=documento.ejecucion_token)
    with pytest.raises(BusinessValidationError):
        await servicio.compensar_documento(e.db, AlmacenEnMemoria("qa"), documento.id)
    assert recargar(e, documento).reserva_activa is True


async def test_una_clave_rechazada_por_el_almacen_deja_la_compensacion_pendiente_sin_borrar(e):
    documento, datos = await reservar(e)
    await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", requiere_compensacion=True, token=documento.ejecucion_token)
    e.almacen.objetos[(documento.clave_original, None)] = datos  # la subida llegó a crear el objeto
    e.almacen.subidas[documento.clave_original] = SubidaConocida(EstadoSubida.CREADA)
    e.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_INVALID_KEY", "no es propia")]
    assert await servicio.compensar_documento(e.db, e.almacen, documento.id) is EstadoCompensacion.PENDIENTE
    assert recargar(e, documento).ultimo_error_compensacion == "STORAGE_INVALID_KEY"


# =============================== fallar_documento ===============================

async def test_fallar_sin_intento_de_subida_libera_la_reserva_de_inmediato(e):
    documento, datos = await reservar(e)
    fallido = await servicio.fallar_documento(e.db, documento.id, "VALIDACION_POSTERIOR", token=documento.ejecucion_token)
    assert fallido.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert fallido.estado_compensacion is EstadoCompensacion.NINGUNA and fallido.reserva_activa is False
    await reservar(e, datos)


async def test_fallar_con_intento_de_subida_deja_la_compensacion_pendiente_y_retiene_la_reserva(e):
    e.almacen.guardar_y_fallar = True
    documento, datos = await reservar(e)
    e.almacen.fallos["guardar"] = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    fallido = await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", token=documento.ejecucion_token)
    assert fallido.estado_compensacion is EstadoCompensacion.PENDIENTE and fallido.reserva_activa is True


async def test_fallar_con_false_explicito_y_un_intento_registrado_es_un_conflicto(e):
    documento, datos = await reservar(e)
    e.almacen.fallos["guardar"] = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    with pytest.raises(ConflictError):
        await servicio.fallar_documento(e.db, documento.id, "X_ERR", requiere_compensacion=False, token=documento.ejecucion_token)
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO


@pytest.mark.parametrize("motivo", ["", "minusculas", "con espacio", "X", "Texto libre del usuario", None, 5, "A" * 65])
async def test_el_motivo_debe_ser_un_codigo_no_texto_libre(e, motivo):
    documento, _ = await reservar(e)
    with pytest.raises(ValueError):
        await servicio.fallar_documento(e.db, documento.id, motivo, token=documento.ejecucion_token)
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO


async def test_fallar_dos_veces_o_un_documento_inexistente_es_un_conflicto(e):
    documento, _ = await reservar(e)
    await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", token=documento.ejecucion_token)
    with pytest.raises(ConflictError):
        await servicio.fallar_documento(e.db, documento.id, "CARGA_CANCELADA", token=documento.ejecucion_token)
    with pytest.raises(ConflictError):
        await servicio.fallar_documento(e.db, uuid.uuid4(), "CARGA_CANCELADA", token=uuid.uuid4())


# =============================== Cancelación, abandono y recuperación ===============================

class AlmacenBloqueante(AlmacenEnMemoria):
    """Crea el objeto y luego queda esperando: simula una subida en curso que se cancela."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subiendo = asyncio.Event()

    async def guardar(self, clave, contenido, sha256):
        self.objetos[(clave, None)] = bytes(contenido)
        self.llamadas.append(("guardar", clave))
        self.subiendo.set()
        await asyncio.sleep(30)


async def test_la_cancelacion_no_toca_la_bd_y_la_recuperacion_limpia_la_reserva_abandonada(e):
    almacen = AlmacenBloqueante(sesion=e.db)
    documento, datos = await reservar(e)
    tarea = asyncio.create_task(servicio.almacenar_original(e.db, almacen, documento.id, datos, token=documento.ejecucion_token))
    await almacen.subiendo.wait()
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    # La cancelación no se convierte en éxito ni en fallo registrado: queda EN_PROCESO, recuperable.
    pendiente = recargar(e, documento)
    assert pendiente.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert pendiente.almacenamiento_intentado_en is not None and pendiente.original_almacenado_en is None
    assert pendiente.reserva_activa is True and len(almacen.objetos) == 1
    assert (await conflicto(e, datos=datos)).code == "DOCUMENT_UPLOAD_IN_PROGRESS"

    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, almacen, ahora=datetime.now(timezone.utc) + timedelta(days=1), limite=10
    )
    assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (1, 1, 0)
    limpio = recargar(e, documento)
    assert limpio.motivo_fallo == "RESERVA_ABANDONADA" and limpio.reserva_activa is False
    assert almacen.objetos == {}
    await reservar(e, datos)


async def test_la_recuperacion_respeta_el_umbral_el_limite_y_el_ambiente(e):
    viejo, _ = await reservar(e, datos_unicos(1), anio=2022)
    reciente, _ = await reservar(e, datos_unicos(2), anio=2023)
    en_qa, _ = await reservar(e, datos_unicos(3), anio=2022, ambiente="qa")
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    e.db.sync.execute(update(Documento).where(Documento.id.in_([viejo.id, en_qa.id])).values(ejecucion_vigente_hasta=ayer))
    e.db.sync.commit()

    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc), limite=10
    )
    assert resumen.abandonados == 1  # solo el viejo del ambiente del almacén
    assert recargar(e, viejo).estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert recargar(e, reciente).estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert recargar(e, en_qa).estado_procesamiento is EstadoProcesamiento.EN_PROCESO


async def test_el_limite_de_la_recuperacion_se_aplica(e):
    for n in range(3):
        d, _ = await reservar(e, datos_unicos(n), anio=2020 + n)
    ayer = datetime.now(timezone.utc) - timedelta(days=1)
    e.db.sync.execute(update(Documento).values(ejecucion_vigente_hasta=ayer))
    e.db.sync.commit()
    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc), limite=2
    )
    assert resumen.abandonados == 2


@pytest.mark.parametrize("cambios", [
    dict(ahora=datetime(2025, 1, 1)), dict(ahora="ayer"),
    dict(limite=0), dict(limite=-1), dict(limite=True), dict(limite=1.5),
])
async def test_parametros_invalidos_de_la_recuperacion(e, cambios):
    valores = dict(ahora=datetime.now(timezone.utc), limite=5)
    valores.update(cambios)
    with pytest.raises(ValueError):
        await servicio.recuperar_documentos_pendientes(e.db, e.almacen, **valores)


async def test_la_recuperacion_no_toca_completados_ni_en_proceso_recientes(e):
    original, datos = await reservar(e)
    await completar(e, original, datos)
    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc) + timedelta(days=1), limite=10
    )
    assert (resumen.abandonados, resumen.compensados, resumen.pendientes) == (0, 0, 0)
    assert recargar(e, original).disponible_para_rag and (original.clave_original, None) in e.almacen.objetos


# =============================== verificación de integridad ===============================

async def test_verificar_original_detecta_bytes_alterados_y_objeto_ausente(e):
    documento, datos = await reservar(e)
    with pytest.raises(ConflictError):
        await servicio.verificar_original(e.db, e.almacen, documento.id)  # aún sin original
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    e.almacen.objetos[(documento.clave_original, None)] = datos[:-1] + b"X"
    with pytest.raises(ExternalServiceError) as capturado:
        await servicio.verificar_original(e.db, e.almacen, documento.id)
    assert capturado.value.code == "STORAGE_INTEGRITY_ERROR"
    assert capturado.value.details == {"operacion": "verificar"}
    del e.almacen.objetos[(documento.clave_original, None)]
    with pytest.raises(ExternalServiceError) as ausente:
        await servicio.verificar_original(e.db, e.almacen, documento.id)
    assert ausente.value.code == "STORAGE_OBJECT_NOT_FOUND"


# =============================== Publicación prematura y empresa ===============================

async def test_un_documento_con_solo_el_original_no_se_publica(e):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=False, resultado_analisis=None)
    assert capturado.value.code == "DOCUMENT_NOT_PUBLISHABLE"
    assert capturado.value.details == {"faltantes": ["indexacion_confirmada", "resultado_analisis"]}
    asegurado = recargar(e, documento)
    assert asegurado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO and not asegurado.disponible_para_rag
    assert asegurado.resultado_analisis is None and asegurado.completado_en is None


@pytest.mark.parametrize(("indexacion", "resultado", "faltante"), [
    (True, None, ["resultado_analisis"]),
    (False, ResultadoAnalisis.OBSERVADO, ["indexacion_confirmada"]),
    ("si", ResultadoAnalisis.OBSERVADO, ["indexacion_confirmada"]),
    (1, ResultadoAnalisis.OBSERVADO, ["indexacion_confirmada"]),
    (True, "OBSERVADO", ["resultado_analisis"]),  # solo el tipo del dominio, no un texto
])
async def test_la_compuerta_exige_confirmaciones_estrictas(e, indexacion, resultado, faltante):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=indexacion, resultado_analisis=resultado)
    assert capturado.value.details == {"faltantes": faltante}
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.EN_PROCESO


async def test_no_se_publica_sin_original_almacenado(e):
    documento, _ = await reservar(e)
    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS)
    assert capturado.value.details == {"faltantes": ["original_almacenado"]}


async def test_empresa_desactivada_antes_de_publicar_impide_la_publicacion(e):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    e.empresa.activa = False
    await e.db.commit()
    with pytest.raises(BusinessValidationError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
    assert capturado.value.code == "COMPANY_INACTIVE"
    asegurado = recargar(e, documento)
    assert asegurado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO and not asegurado.disponible_para_rag


async def test_cambio_de_sector_de_la_empresa_tambien_se_revalida(e):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    e.empresa.sector = SectorEmpresa.ENERGIA
    await e.db.commit()
    with pytest.raises(BusinessValidationError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
    assert capturado.value.code == "COMPANY_SECTOR_MISMATCH"


async def test_con_todo_confirmado_y_empresa_valida_la_compuerta_permite_completar(e):
    documento, datos = await reservar(e)
    completo = await completar(e, documento, datos, ResultadoAnalisis.CON_HALLAZGOS)
    assert completo.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert completo.resultado_analisis is ResultadoAnalisis.CON_HALLAZGOS
    assert completo.completado_en is not None and completo.disponible_para_rag is True
    assert completo.reserva_activa is True  # un completado sigue bloqueando duplicados
    with pytest.raises(ConflictError) as repetido:  # no se publica dos veces
        await servicio.publicar_documento(
            e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
    assert repetido.value.details == {"faltantes": ["documento_en_proceso"]}


async def test_nada_asigna_observado_ni_completa_por_si_solo(e):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    otro, otros = await reservar(e, datos_unicos(1), anio=2024)
    await fallar_subida(e, otro, otros)
    for d in (documento, otro):
        actual = recargar(e, d)
        assert actual.resultado_analisis is None and actual.completado_en is None and not actual.disponible_para_rag


# =============================== Contrato uniforme y datos sensibles ===============================

def _app(e):
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    # Rutas solo de prueba: el endpoint real de ingesta pertenece a la Etapa 6.
    @app.post("/reservar")
    async def ruta_reservar():
        await reservar(e)

    @app.post("/subir/{documento_id}")
    async def ruta_subir(documento_id: uuid.UUID):
        token = e.db.sync.get(Documento, documento_id).ejecucion_token
        await servicio.almacenar_original(e.db, e.almacen, documento_id, CONTENIDO, token=token)

    return app


async def test_un_duplicado_responde_409_con_el_contrato_uniforme_y_sin_datos_sensibles(e):
    original, datos = await reservar(e)
    await completar(e, original, datos)
    cliente = TestClient(_app(e))
    respuesta = await asyncio.to_thread(cliente.post, "/reservar")

    assert respuesta.status_code == 409
    error = respuesta.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "DOCUMENT_ALREADY_INGESTED"
    assert error["details"]["cuenta"] == "superadmin@pruebas.invalid" and "cargado_en" in error["details"]
    assert error["request_id"] == respuesta.headers["x-request-id"]
    for prohibido in (original.sha256, original.clave_original, "Memoria anual 2025.md", "password", "hash-de-prueba"):
        assert prohibido not in respuesta.text


async def test_un_fallo_del_almacen_responde_502_sin_credenciales_contenido_ni_claves(e):
    documento, datos = await reservar(e)
    secreto = ExternalServiceError(
        "STORAGE_ERROR", "El almacenamiento devolvió un error.",
        details={"operacion": "guardar", "codigo_s3": "AccessDenied", "estado_http": 403},
    )
    e.almacen.fallos["guardar"] = [secreto]
    cliente = TestClient(_app(e))
    respuesta = await asyncio.to_thread(cliente.post, f"/subir/{documento.id}")
    assert respuesta.status_code == 502 and respuesta.json()["error"]["code"] == "STORAGE_ERROR"
    for prohibido in (documento.clave_original, documento.sha256, "Ñandú", "development/"):
        assert prohibido not in respuesta.text
    assert recargar(e, documento).estado_procesamiento is EstadoProcesamiento.FALLIDO


# =============================== Rutas de fallo y carreras de estado ===============================

async def test_si_la_bd_falla_al_registrar_el_fallo_se_relanza_el_error_del_almacen_y_queda_recuperable(e, monkeypatch):
    documento, datos = await reservar(e)
    e.almacen.guardar_y_fallar = True
    e.almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]

    async def bd_caida(*args, **kwargs):
        raise RuntimeError("la base de datos no responde")

    monkeypatch.setattr(servicio, "fallar_documento", bd_caida)
    with pytest.raises(ExternalServiceError) as capturado:  # NO se enmascara con el error de la BD
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert capturado.value.code == "STORAGE_ERROR"
    monkeypatch.undo()

    asegurado = recargar(e, documento)
    assert asegurado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert asegurado.almacenamiento_intentado_en is not None and asegurado.reserva_activa is True
    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc) + timedelta(days=1), limite=5)
    assert (resumen.abandonados, resumen.compensados) == (1, 1) and e.almacen.objetos == {}


async def test_si_tampoco_funciona_el_rollback_el_error_original_sigue_siendo_el_del_almacen(e, monkeypatch):
    documento, datos = await reservar(e)
    e.almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_TIMEOUT", "plazo")]

    async def falla(*args, **kwargs):
        raise RuntimeError("sin conexión")

    monkeypatch.setattr(servicio, "fallar_documento", falla)
    monkeypatch.setattr(e.db, "rollback", falla)
    with pytest.raises(ExternalServiceError) as capturado:
        await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    assert capturado.value.code == "STORAGE_TIMEOUT"


async def test_si_el_documento_cambia_de_estado_durante_la_subida_se_informa_conflicto_y_el_objeto_queda_para_compensar(e):
    documento, datos = await reservar(e)

    class AlmacenQueCambiaElEstado(AlmacenEnMemoria):
        async def guardar(self, clave, contenido, sha256):
            referencia = await super().guardar(clave, contenido, sha256)
            # Otro proceso (p. ej. la recuperación) lo marca fallido mientras se sube.
            e.db.sync.execute(update(Documento).where(Documento.id == documento.id).values(
                estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="RESERVA_ABANDONADA",
                fallido_en=datetime.now(timezone.utc), estado_compensacion=EstadoCompensacion.PENDIENTE))
            e.db.sync.commit()
            return referencia

    almacen = AlmacenQueCambiaElEstado(sesion=e.db)
    with pytest.raises(ConflictError) as capturado:
        await servicio.almacenar_original(e.db, almacen, documento.id, datos, token=documento.ejecucion_token)
    assert capturado.value.code == "DOCUMENT_STATE_CONFLICT"
    assert len(almacen.objetos) == 1 and recargar(e, documento).original_almacenado_en is None
    assert await servicio.compensar_documento(e.db, almacen, documento.id) is EstadoCompensacion.COMPLETADA
    assert almacen.objetos == {}


async def test_la_recuperacion_omite_lo_que_otro_proceso_ya_resolvio(e, monkeypatch):
    primero, _ = await reservar(e, datos_unicos(1), anio=2022)
    segundo, _ = await reservar(e, datos_unicos(2), anio=2023)
    e.db.sync.execute(update(Documento).values(ejecucion_vigente_hasta=datetime.now(timezone.utc) - timedelta(days=1)))
    e.db.sync.commit()
    original = servicio.fallar_documento

    async def uno_ya_resuelto(db, documento_id, motivo, **kwargs):
        if documento_id == primero.id:
            raise ConflictError("DOCUMENT_STATE_CONFLICT", "ya resuelto")
        return await original(db, documento_id, motivo, **kwargs)

    monkeypatch.setattr(servicio, "fallar_documento", uno_ya_resuelto)
    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc), limite=10)
    assert resumen.abandonados == 1
    assert recargar(e, primero).estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert recargar(e, segundo).estado_procesamiento is EstadoProcesamiento.FALLIDO


async def test_la_recuperacion_omite_compensaciones_que_dejaron_de_estar_pendientes(e, monkeypatch):
    documento, datos = await reservar(e)
    e.almacen.guardar_y_fallar = True
    e.almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
    await fallar_subida(e, documento, datos)

    async def ya_no_pendiente(*args, **kwargs):
        raise ConflictError("DOCUMENT_STATE_CONFLICT", "otro proceso la completó")

    monkeypatch.setattr(servicio, "compensar_documento", ya_no_pendiente)
    resumen = await servicio.recuperar_documentos_pendientes(
        e.db, e.almacen, ahora=datetime.now(timezone.utc), limite=10)
    assert (resumen.compensados, resumen.pendientes) == (0, 0)


async def test_si_el_documento_deja_de_estar_en_proceso_durante_la_publicacion_no_se_completa(e, monkeypatch):
    documento, datos = await reservar(e)
    await servicio.almacenar_original(e.db, e.almacen, documento.id, datos, token=documento.ejecucion_token)
    original = servicio._transicion

    async def pierde_la_carrera(db, documento_id, condiciones, valores):
        if "completado_en" in valores:
            return False  # otro proceso cambió el estado entre la lectura y la actualización
        return await original(db, documento_id, condiciones, valores)

    monkeypatch.setattr(servicio, "_transicion", pierde_la_carrera)
    with pytest.raises(ConflictError) as capturado:
        await servicio.publicar_documento(
            e.db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
    assert capturado.value.code == "DOCUMENT_STATE_CONFLICT"
    monkeypatch.undo()
    asegurado = recargar(e, documento)
    assert asegurado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO and not asegurado.disponible_para_rag

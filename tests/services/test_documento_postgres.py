"""Etapa 4A: restricciones y CONCURRENCIA sobre un PostgreSQL REAL y AISLADO.

El clúster es temporal (ver `conftest.py`): se crea con `initdb`, escucha en 127.0.0.1 y se
destruye al terminar; no usa DATABASE_URL ni bases compartidas. Si PostgreSQL no está
disponible estas pruebas se OMITEN (`pytest -rs` muestra el motivo) y NO se dan por
pasadas: SQLite y los dobles no demuestran la concurrencia de PostgreSQL.

El almacén de originales sigue siendo un doble en memoria: nada aquí prueba MinIO.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.exceptions import ConflictError, ExternalServiceError, NotFoundError
from app.models import SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta import documento_service as servicio
from tests.ayudantes_ingesta import AlmacenEnMemoria, crear_empresa, crear_usuario, metadatos, sha256_de

MEMORIA = TipoDocumento.MEMORIA_ANUAL
REPORTE = TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI


class Mundo:
    def __init__(self, fabrica, usuario, empresas):
        self.fabrica, self.usuario, self.empresas = fabrica, usuario, empresas
        self.empresa = empresas[0]


@pytest.fixture
async def mundo(fabrica_pg):
    async with fabrica_pg() as db:
        usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
        empresas = [await crear_empresa(db, nombre=f"Empresa {n}") for n in range(8)]
    return Mundo(fabrica_pg, usuario, empresas)


def datos_de(n: int) -> bytes:
    return f"# Documento {n}\n".encode("utf-8")


async def reservar(mundo, datos, empresa=None, anio=2025, tipo=MEMORIA, ambiente="development", db=None):
    async def hacer(sesion):
        return await servicio.reservar_documento(
            sesion,
            metadatos=metadatos(empresa or mundo.empresa, anio, tipo),
            nombre_archivo="Memoria anual 2025.md",
            sha256=sha256_de(datos),
            tamano_bytes=len(datos),
            usuario_id=mundo.usuario.id,
            ambiente=ambiente,
        )

    if db is not None:
        return await hacer(db)
    async with mundo.fabrica() as sesion:  # una sesión propia por intento: nunca se comparten
        return await hacer(sesion)


async def filas(mundo, consulta="SELECT count(*) FROM documentos WHERE reserva_activa"):
    async with mundo.fabrica() as db:
        return (await db.execute(text(consulta))).scalar_one()


def separar(resultados):
    exitos = [r for r in resultados if isinstance(r, Documento)]
    errores = [r for r in resultados if not isinstance(r, Documento)]
    return exitos, errores


# =============================== Esquema y restricciones reales ===============================

async def test_el_esquema_real_tiene_indices_parciales_checks_claves_foraneas_y_tipos(mundo):
    async with mundo.fabrica() as db:
        indices = dict((await db.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'documentos'"))).all())
        checks = set((await db.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'documentos'::regclass AND contype = 'c'"))).scalars())
        foraneas = set((await db.execute(text(
            "SELECT confrelid::regclass::text FROM pg_constraint WHERE conrelid = 'documentos'::regclass AND contype = 'f'"))).scalars())
        tipos = set((await db.execute(text(
            "SELECT typname FROM pg_type WHERE typname IN ('tipo_documento','estado_procesamiento','resultado_analisis','estado_compensacion')"))).scalars())
    for nombre, columnas in (("uq_documentos_sha256_activo", "(ambiente, sha256)"),
                             ("uq_documentos_empresa_anio_tipo_activo", "(ambiente, empresa_id, anio, tipo)")):
        definicion = indices[nombre]
        assert "UNIQUE" in definicion
        assert columnas in definicion
        assert "WHERE reserva_activa" in definicion
    assert {"ck_documentos_resultado_solo_si_completado", "ck_documentos_reserva_solo_liberada_si_limpio",
            "ck_documentos_completado_con_original", "ck_documentos_compensacion_solo_si_fallido"} <= checks
    assert foraneas == {"empresas", "usuarios"}
    assert tipos == {"tipo_documento", "estado_procesamiento", "resultado_analisis", "estado_compensacion"}


@pytest.mark.parametrize(("cambios", "restriccion"), [
    (dict(resultado_analisis=ResultadoAnalisis.OBSERVADO), "ck_documentos_resultado_solo_si_completado"),
    (dict(estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ResultadoAnalisis.OBSERVADO,
          completado_en=datetime.now(timezone.utc)), "ck_documentos_completado_con_original"),
    (dict(reserva_activa=False), "ck_documentos_reserva_solo_liberada_si_limpio"),
    (dict(estado_compensacion=EstadoCompensacion.PENDIENTE), "ck_documentos_compensacion_solo_si_fallido"),
    (dict(estado_procesamiento=EstadoProcesamiento.FALLIDO), "ck_documentos_fallido_con_motivo"),
    (dict(sha256="a" * 10), "ck_documentos_sha256_longitud"),
])
async def test_postgresql_rechaza_estados_incoherentes(mundo, cambios, restriccion):
    valores = dict(ambiente="development", empresa_id=mundo.empresa.id, anio=2025, tipo=MEMORIA, sector=mundo.empresa.sector,
                   nombre_archivo="a.md", sha256="a" * 64, tamano_bytes=3, usuario_id=mundo.usuario.id,
                   clave_original=f"development/documentos/{uuid.uuid4()}/original.md")
    valores.update(cambios)
    async with mundo.fabrica() as db:
        db.add(Documento(**valores))
        with pytest.raises(IntegrityError) as capturado:
            await db.commit()
        assert restriccion in str(capturado.value.orig)


async def test_las_claves_foraneas_se_aplican_y_no_se_traducen_a_409(mundo):
    async with mundo.fabrica() as db:
        db.add(Documento(ambiente="development", empresa_id=uuid.uuid4(), anio=2025, tipo=MEMORIA, sector=SectorEmpresa.MINERIA,
                         nombre_archivo="a.md", sha256="a" * 64, tamano_bytes=3, usuario_id=mundo.usuario.id,
                         clave_original=f"development/documentos/{uuid.uuid4()}/original.md"))
        with pytest.raises(IntegrityError) as capturado:
            await db.commit()
        # El texto del servidor depende de su idioma; el tipo de violación no.
        assert "ForeignKeyViolationError" in str(capturado.value.orig)
        assert not servicio._es_violacion_de_reserva(capturado.value)
    metadatos_de_empresa_inexistente = MetadatosIngestaRequest(empresa_id=uuid.uuid4(), anio=2025, tipo=MEMORIA)
    sesion = mundo.fabrica()
    with pytest.raises(NotFoundError):
        async with sesion as db:
            await servicio.reservar_documento(
                db, metadatos=metadatos_de_empresa_inexistente,
                nombre_archivo="a.md", sha256="a" * 64, tamano_bytes=3, usuario_id=mundo.usuario.id, ambiente="development")


async def test_reserva_en_postgresql_estado_inicial_utc_y_sin_transaccion_abierta(mundo):
    async with mundo.fabrica() as db:
        documento = await reservar(mundo, datos_de(1), db=db)
        assert db.in_transaction() is False
    assert documento.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert documento.resultado_analisis is None
    assert documento.disponible_para_rag is False
    assert documento.creado_en.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - documento.creado_en) < timedelta(minutes=1)
    assert documento.sector is mundo.empresa.sector
    assert documento.usuario_id == mundo.usuario.id


# =============================== Concurrencia: exclusión respaldada por la BD ===============================

@pytest.mark.parametrize("forzar_carrera", [False, True], ids=["carrera-natural", "sin-consulta-previa"])
async def test_cargas_concurrentes_con_el_mismo_contenido_dejan_una_sola_reserva(mundo, monkeypatch, forzar_carrera):
    traducidas = []
    original = servicio._es_violacion_de_reserva

    def contar(exc):
        resultado = original(exc)
        traducidas.append(resultado)
        return resultado

    monkeypatch.setattr(servicio, "_es_violacion_de_reserva", contar)
    if forzar_carrera:
        async def sin_consulta(*args, **kwargs):
            return None  # todas pasan la consulta previa: decide solo el índice único
        monkeypatch.setattr(servicio, "_buscar_conflicto", sin_consulta)

    datos = datos_de(1)  # mismo hash; cada tarea usa otra empresa (la combinación no choca)
    resultados = await asyncio.gather(
        *(reservar(mundo, datos, empresa=empresa) for empresa in mundo.empresas), return_exceptions=True)
    exitos, errores = separar(resultados)

    assert len(exitos) == 1
    assert len(errores) == 7
    assert all(isinstance(error, ConflictError) for error in errores), errores  # nunca IntegrityError cruda
    assert {error.code for error in errores} <= {"DOCUMENT_UPLOAD_IN_PROGRESS", "DOCUMENT_RESERVATION_CONFLICT"}
    assert await filas(mundo) == 1
    if forzar_carrera:
        assert traducidas == [True] * 7  # los 7 perdedores fueron violaciones esperadas del índice, traducidas


@pytest.mark.parametrize("forzar_carrera", [False, True], ids=["carrera-natural", "sin-consulta-previa"])
async def test_cargas_concurrentes_para_la_misma_empresa_anio_tipo_dejan_una_sola_reserva(mundo, monkeypatch, forzar_carrera):
    if forzar_carrera:
        async def sin_consulta(*args, **kwargs):
            return None
        monkeypatch.setattr(servicio, "_buscar_conflicto", sin_consulta)
    resultados = await asyncio.gather(*(reservar(mundo, datos_de(n)) for n in range(8)), return_exceptions=True)
    exitos, errores = separar(resultados)
    assert len(exitos) == 1
    assert all(isinstance(e, ConflictError) for e in errores)
    assert len(errores) == 7
    assert await filas(mundo) == 1


async def test_ambos_criterios_a_la_vez_admiten_una_reserva_por_criterio_y_ninguna_mas(mundo):
    mismo_hash = datos_de(0)
    tareas = []
    for n, empresa in enumerate(mundo.empresas[:4]):                      # grupo A: mismo contenido, otras empresas
        tareas.append(("A", reservar(mundo, mismo_hash, empresa=empresa)))
    for n in range(4):                                                    # grupo B: misma combinación, otros contenidos
        tareas.append(("B", reservar(mundo, datos_de(100 + n), empresa=mundo.empresas[7])))
    resultados = await asyncio.gather(*(t for _, t in tareas), return_exceptions=True)
    exitos = [(grupo, r) for (grupo, _), r in zip(tareas, resultados) if isinstance(r, Documento)]
    assert sorted(grupo for grupo, _ in exitos) == ["A", "B"]
    assert all(isinstance(r, ConflictError) for r in resultados if not isinstance(r, Documento))
    assert await filas(mundo) == 2


async def test_cargas_concurrentes_de_combinaciones_distintas_no_se_bloquean(mundo):
    resultados = await asyncio.gather(
        *(reservar(mundo, datos_de(n), empresa=empresa, anio=2010 + n) for n, empresa in enumerate(mundo.empresas)),
        return_exceptions=True)
    exitos, errores = separar(resultados)
    assert len(exitos) == 8
    assert errores == []


async def test_un_completado_bloquea_cargas_concurrentes_con_fecha_y_cuenta_originales(mundo):
    almacen = AlmacenEnMemoria()
    datos = datos_de(1)
    async with mundo.fabrica() as db:
        original = await reservar(mundo, datos, db=db)
        await servicio.almacenar_original(db, almacen, original.id, datos, token=original.ejecucion_token)
        await servicio.publicar_documento(db, original.id, token=original.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
    resultados = await asyncio.gather(
        *(reservar(mundo, datos, empresa=empresa, anio=2000 + n) for n, empresa in enumerate(mundo.empresas)),
        return_exceptions=True)
    assert all(isinstance(r, ConflictError) and r.code == "DOCUMENT_ALREADY_INGESTED" for r in resultados)
    detalle = resultados[0].details
    assert detalle["cuenta"] == "superadmin@pruebas.invalid"
    assert detalle["documento_id"] == str(original.id)
    cargado = datetime.fromisoformat(detalle["cargado_en"])
    assert cargado.utcoffset() == timedelta(0)
    assert abs(cargado - original.creado_en) < timedelta(seconds=1)
    assert await filas(mundo) == 1


async def test_cualquier_otra_integrityerror_se_propaga_en_postgresql(mundo, monkeypatch):
    mismo_id = uuid.uuid4()
    monkeypatch.setattr(servicio.uuid, "uuid4", lambda: mismo_id)
    await reservar(mundo, datos_de(1))
    contenido_nuevo = datos_de(2)
    with pytest.raises(IntegrityError) as capturado:  # clave primaria repetida
        await reservar(mundo, contenido_nuevo, anio=2024)
    assert "documentos_pkey" in str(capturado.value.orig)
    assert not isinstance(capturado.value, ConflictError)


# =============================== Fallo, compensación y reintento en PostgreSQL ===============================

async def test_reintento_tras_fallo_compensado_y_reserva_retenida_mientras_hay_compensacion_pendiente(mundo):
    async with mundo.fabrica() as db:
        almacen = AlmacenEnMemoria(sesion=db)
        datos = datos_de(1)
        documento = await reservar(mundo, datos, db=db)
        almacen.guardar_y_fallar = True
        almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
        almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
        with pytest.raises(ExternalServiceError):
            await servicio.almacenar_original(db, almacen, documento.id, datos, token=documento.ejecucion_token)

        # Compensación pendiente: la reserva se conserva y el reintento se bloquea.
        pendiente = await db.get(Documento, documento.id, populate_existing=True)
        assert pendiente.estado_compensacion is EstadoCompensacion.PENDIENTE
        assert pendiente.reserva_activa is True
        assert len(almacen.objetos) == 1
        await db.commit()
        with pytest.raises(ConflictError) as bloqueado:
            await reservar(mundo, datos, db=db)
        assert bloqueado.value.code == "DOCUMENT_CLEANUP_PENDING"

        # Recuperación: se limpia y SOLO ENTONCES se libera la reserva.
        resumen = await servicio.recuperar_documentos_pendientes(
            db, almacen, ahora=datetime.now(timezone.utc), limite=5)
        assert resumen.compensados == 1
        assert almacen.objetos == {}
        limpio = await db.get(Documento, documento.id, populate_existing=True)
        assert limpio.reserva_activa is False
        assert limpio.estado_compensacion is EstadoCompensacion.COMPLETADA
        await db.commit()
        reintento = await reservar(mundo, datos, db=db)
        assert reintento.id != documento.id
        # Nunca hubo una transacción abierta durante una llamada al almacén (sesión real).
        assert almacen.transaccion_abierta_en_llamada
        assert not any(almacen.transaccion_abierta_en_llamada)


class AlmacenLento(AlmacenEnMemoria):
    async def eliminar(self, referencia):
        await asyncio.sleep(0.1)  # abre una ventana para que las compensaciones se solapen
        await super().eliminar(referencia)


async def test_compensaciones_concurrentes_del_mismo_documento_son_idempotentes(mundo):
    datos = datos_de(1)
    almacen = AlmacenLento()
    async with mundo.fabrica() as db:
        documento = await reservar(mundo, datos, db=db)
        almacen.guardar_y_fallar = True
        almacen.fallos["guardar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
        almacen.fallos["eliminar"] = [ExternalServiceError("STORAGE_ERROR", "falla")]
        with pytest.raises(ExternalServiceError):
            await servicio.almacenar_original(db, almacen, documento.id, datos, token=documento.ejecucion_token)

    async def compensar():
        async with mundo.fabrica() as sesion:
            return await servicio.compensar_documento(sesion, almacen, documento.id)

    resultados = await asyncio.gather(*(compensar() for _ in range(5)))
    assert resultados == [EstadoCompensacion.COMPLETADA] * 5
    assert almacen.objetos == {}
    async with mundo.fabrica() as db:
        final = await db.get(Documento, documento.id)
    assert final.reserva_activa is False
    assert final.compensada_en is not None
    assert final.ultimo_error_compensacion is None


async def test_liberada_la_reserva_solo_una_de_varias_cargas_concurrentes_la_obtiene(mundo):
    datos = datos_de(1)
    almacen = AlmacenEnMemoria()
    async with mundo.fabrica() as db:
        documento = await reservar(mundo, datos, db=db)
        await servicio.fallar_documento(db, documento.id, "CARGA_CANCELADA", token=documento.ejecucion_token)  # sin intento: se libera de inmediato
    resultados = await asyncio.gather(*(reservar(mundo, datos) for _ in range(6)), return_exceptions=True)
    exitos, errores = separar(resultados)
    assert len(exitos) == 1
    assert all(isinstance(e, ConflictError) for e in errores)
    assert await filas(mundo) == 1
    assert await filas(mundo, "SELECT count(*) FROM documentos") == 2  # el fallido queda como historial


async def test_nunca_se_publican_dos_veces_aunque_dos_procesos_lo_intenten_a_la_vez(mundo):
    datos = datos_de(1)
    almacen = AlmacenEnMemoria()
    async with mundo.fabrica() as db:
        documento = await reservar(mundo, datos, db=db)
        await servicio.almacenar_original(db, almacen, documento.id, datos, token=documento.ejecucion_token)

    async def publicar():
        async with mundo.fabrica() as sesion:
            return await servicio.publicar_documento(
                sesion, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS)

    resultados = await asyncio.gather(*(publicar() for _ in range(4)), return_exceptions=True)
    exitos, errores = separar(resultados)
    assert len(exitos) == 1
    assert all(isinstance(e, ConflictError) for e in errores)
    async with mundo.fabrica() as db:
        final = await db.get(Documento, documento.id)
    assert final.estado_procesamiento is EstadoProcesamiento.COMPLETADO
    assert final.disponible_para_rag is True


async def test_empresa_desactivada_impide_publicar_en_postgresql(mundo):
    datos = datos_de(1)
    almacen = AlmacenEnMemoria()
    async with mundo.fabrica() as db:
        documento = await reservar(mundo, datos, db=db)
        documento_id = documento.id  # un rollback posterior expira las instancias cargadas
        token = documento.ejecucion_token
        await servicio.almacenar_original(db, almacen, documento_id, datos, token=token)
        await db.execute(text("UPDATE empresas SET activa = false WHERE id = :id"), {"id": mundo.empresa.id})
        await db.commit()
        from app.exceptions import BusinessValidationError
        with pytest.raises(BusinessValidationError) as capturado:
            await servicio.publicar_documento(
                db, documento_id, token=token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
        assert capturado.value.code == "COMPANY_INACTIVE"
        asegurado = await db.get(Documento, documento_id, populate_existing=True)
        assert asegurado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
        assert not asegurado.disponible_para_rag


# ============ Etapa 4A (cierre): un solo intento de almacenamiento y vigencia de la operación ============

class AlmacenLento(AlmacenEnMemoria):
    """Retiene la subida el tiempo suficiente para que los demás intentos coincidan con ella."""

    async def guardar(self, clave, contenido, sha256):
        await asyncio.sleep(0.3)
        return await super().guardar(clave, contenido, sha256)


async def test_ocho_intentos_simultaneos_de_almacenar_el_mismo_documento_solo_llegan_una_vez_al_almacen(mundo):
    datos = datos_de(1)
    documento = await reservar(mundo, datos)
    almacen = AlmacenLento()

    async def intentar():
        async with mundo.fabrica() as sesion:  # una sesión propia por intento: nunca se comparten
            return await servicio.almacenar_original(
                sesion, almacen, documento.id, datos, token=documento.ejecucion_token
            )

    resultados = await asyncio.gather(*(intentar() for _ in range(8)), return_exceptions=True)
    exitos, errores = separar(resultados)
    assert len(exitos) == 1
    assert len(errores) == 7
    assert all(isinstance(e, ConflictError) for e in errores)
    assert [op for op, _ in almacen.llamadas].count("guardar") == 1  # el UPDATE condicional dejó pasar uno
    assert len(almacen.objetos) == 1


async def test_renovar_y_recuperar_a_la_vez_nunca_dejan_al_ejecutor_y_a_la_recuperacion_con_la_misma_operacion(mundo):
    """Dos sesiones reales compiten por una operación con la vigencia vencida: o gana la renovación
    (sigue EN_PROCESO) o gana la recuperación (FALLIDO y el ejecutor ya no puede renovar), nunca ambas."""
    almacen = AlmacenEnMemoria()
    gano_renovacion = gano_recuperacion = 0
    for ronda in range(12):
        documento = await reservar(mundo, datos_de(100 + ronda), anio=2000 + ronda)
        async with mundo.fabrica() as db:
            await db.execute(
                text("UPDATE documentos SET ejecucion_vigente_hasta = now() - interval '1 second' WHERE id = :id"),
                {"id": documento.id},
            )
            await db.commit()

        async def renovar():
            async with mundo.fabrica() as sesion:
                return await servicio.renovar_vigencia(sesion, documento.id, token=documento.ejecucion_token)

        async def recuperar():
            async with mundo.fabrica() as sesion:
                return await servicio.recuperar_documentos_pendientes(sesion, almacen, limite=50)

        renovacion, recuperacion = await asyncio.gather(renovar(), recuperar(), return_exceptions=True)
        assert not isinstance(recuperacion, Exception)
        async with mundo.fabrica() as db:
            estado = (await db.get(Documento, documento.id, populate_existing=True)).estado_procesamiento
        if isinstance(renovacion, ConflictError):
            assert estado is EstadoProcesamiento.FALLIDO
            gano_recuperacion += 1
        else:
            assert not isinstance(renovacion, Exception)
            assert estado is EstadoProcesamiento.EN_PROCESO
            gano_renovacion += 1
    assert gano_renovacion + gano_recuperacion == 12


async def test_el_ejecutor_anterior_no_publica_tras_la_recuperacion_en_postgresql(mundo):
    datos = datos_de(7)
    almacen = AlmacenEnMemoria()
    documento = await reservar(mundo, datos)
    viejo = documento.ejecucion_token
    async with mundo.fabrica() as db:
        await servicio.almacenar_original(db, almacen, documento.id, datos, token=viejo)
        await db.execute(
            text("UPDATE documentos SET ejecucion_vigente_hasta = now() - interval '1 second' WHERE id = :id"),
            {"id": documento.id},
        )
        await db.commit()
    async with mundo.fabrica() as db:
        resumen = await servicio.recuperar_documentos_pendientes(db, almacen, limite=10)
    assert resumen.abandonados == 1

    async def publicar():
        async with mundo.fabrica() as sesion:
            return await servicio.publicar_documento(
                sesion, documento.id, token=viejo, indexacion_confirmada=True,
                resultado_analisis=ResultadoAnalisis.OBSERVADO,
            )

    resultados = await asyncio.gather(*(publicar() for _ in range(4)), return_exceptions=True)
    assert all(isinstance(r, ConflictError) for r in resultados)
    async with mundo.fabrica() as db:
        final = await db.get(Documento, documento.id, populate_existing=True)
    assert final.estado_procesamiento is EstadoProcesamiento.FALLIDO
    assert final.disponible_para_rag is False

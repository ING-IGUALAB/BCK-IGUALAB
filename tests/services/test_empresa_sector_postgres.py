"""Cambio de sector de una empresa frente a la reserva de documentos, sobre un PostgreSQL REAL y AISLADO (clúster temporal de
`conftest.py`; sin `DATABASE_URL` ni bases compartidas). Si PostgreSQL no está disponible estas pruebas se OMITEN y no cuentan
como ejecutadas: SQLite y los dobles no demuestran bloqueos de fila.

Regla: una empresa con documentos no cambia de sector. La edición y la reserva se serializan con un bloqueo de FILA de la
empresa (edición: FOR NO KEY UPDATE; reserva: FOR SHARE) que dura hasta el fin de su transacción corta. Las pruebas pausan una
de las dos operaciones DENTRO de su transacción para fijar cada orden de llegada, y una tercera las lanza sin pausas.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.core.exceptions import BusinessValidationError, ConflictError
from app.models import Empresa, SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import Documento, EstadoProcesamiento, ResultadoAnalisis
from app.modules.empresas import service as empresa_service
from app.modules.ingesta.application import documento_service as servicio
from app.modules.ingesta.application.documento_service import reserva as modulo_reserva
from app.schemas import EditarEmpresaRequest
from tests.ayudantes_ingesta import crear_empresa, crear_usuario, metadatos, sha256_de

MEMORIA = TipoDocumento.MEMORIA_ANUAL
ESPERA_BLOQUEO = 0.6  # segundos que se concede a una operación para demostrar que sigue esperando el bloqueo


class Mundo:
    def __init__(self, fabrica, usuario, empresa):
        self.fabrica, self.usuario, self.empresa = fabrica, usuario, empresa


@pytest.fixture
async def mundo(fabrica_pg):
    async with fabrica_pg() as db:
        usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
        empresa = await crear_empresa(db, nombre="Empresa Carrera", sector=SectorEmpresa.MINERIA)
    return Mundo(fabrica_pg, usuario, empresa)


async def editar_sector(mundo, sector, empresa=None):
    async with mundo.fabrica() as db:
        return await empresa_service.editar_empresa(
            db, (empresa or mundo.empresa).id, EditarEmpresaRequest(sector=sector)
        )


async def reservar(mundo, numero, empresa=None, sector_declarado=None):
    datos = f"# Documento {numero}\n".encode()
    async with mundo.fabrica() as db:
        return await servicio.reservar_documento(
            db,
            metadatos=metadatos(empresa or mundo.empresa, 2025, MEMORIA, sector=sector_declarado),
            nombre_archivo="Memoria anual 2025.md",
            sha256=sha256_de(datos),
            tamano_bytes=len(datos),
            usuario_id=mundo.usuario.id,
            ambiente="development",
        )


async def leer(mundo, empresa):
    async with mundo.fabrica() as db:
        sector = (await db.execute(select(Empresa.sector).where(Empresa.id == empresa.id))).scalar_one()
        documentos = (await db.execute(select(Documento.sector).where(Documento.empresa_id == empresa.id))).scalars().all()
    return sector, list(documentos)


def pausar_en(monkeypatch, objeto, nombre):
    """Reemplaza `objeto.nombre` por una versión que, tras ejecutar la original, avisa y espera la señal de seguir.
    La pausa ocurre DENTRO de la transacción de quien la llama."""
    dentro, seguir = asyncio.Event(), asyncio.Event()
    original = getattr(objeto, nombre)

    async def pausada(*args, **kwargs):
        resultado = await original(*args, **kwargs)
        dentro.set()
        await seguir.wait()
        return resultado

    monkeypatch.setattr(objeto, nombre, pausada)
    return dentro, seguir


async def esperar(evento):
    await asyncio.wait_for(evento.wait(), 15)


# ============================================ el cambio llega primero ============================================

async def test_si_el_cambio_de_sector_va_primero_la_reserva_espera_y_copia_el_sector_nuevo(mundo, monkeypatch):
    dentro, seguir = pausar_en(monkeypatch, empresa_service, "_tiene_documentos")
    edicion = asyncio.create_task(editar_sector(mundo, SectorEmpresa.ENERGIA))
    await esperar(dentro)  # la edición ya comprobó «sin documentos» y conserva el bloqueo de la fila

    reserva = asyncio.create_task(reservar(mundo, 1))
    await asyncio.sleep(ESPERA_BLOQUEO)
    assert not reserva.done()  # la reserva no puede copiar el sector viejo: espera a la edición

    seguir.set()
    editada, documento = await asyncio.gather(edicion, reserva)
    assert editada.sector is SectorEmpresa.ENERGIA
    assert documento.sector is SectorEmpresa.ENERGIA
    assert await leer(mundo, mundo.empresa) == (SectorEmpresa.ENERGIA, [SectorEmpresa.ENERGIA])


async def test_si_el_cambio_va_primero_una_reserva_con_el_sector_anterior_declarado_se_rechaza_sin_crear_documento(
    mundo, monkeypatch
):
    dentro, seguir = pausar_en(monkeypatch, empresa_service, "_tiene_documentos")
    edicion = asyncio.create_task(editar_sector(mundo, SectorEmpresa.PETROLEO))
    await esperar(dentro)
    reserva = asyncio.create_task(reservar(mundo, 2, sector_declarado=SectorEmpresa.MINERIA))
    await asyncio.sleep(ESPERA_BLOQUEO)
    assert not reserva.done()

    seguir.set()
    resultados = await asyncio.gather(edicion, reserva, return_exceptions=True)
    assert resultados[0].sector is SectorEmpresa.PETROLEO
    assert isinstance(resultados[1], BusinessValidationError)
    assert resultados[1].code == "COMPANY_SECTOR_MISMATCH"
    assert await leer(mundo, mundo.empresa) == (SectorEmpresa.PETROLEO, [])


# ============================================ la reserva llega primero ============================================

async def test_si_la_reserva_va_primero_el_cambio_de_sector_espera_y_se_rechaza(mundo, monkeypatch):
    dentro, seguir = pausar_en(monkeypatch, modulo_reserva, "_buscar_conflicto")
    reserva = asyncio.create_task(reservar(mundo, 3))
    await esperar(dentro)  # la reserva ya leyó la empresa y conserva el bloqueo compartido; aún no confirmó

    edicion = asyncio.create_task(editar_sector(mundo, SectorEmpresa.ENERGIA))
    await asyncio.sleep(ESPERA_BLOQUEO)
    assert not edicion.done()  # la edición no puede decidir «sin documentos» mientras la reserva no confirme

    seguir.set()
    documento, resultado = (await asyncio.gather(reserva, edicion, return_exceptions=True))
    assert documento.sector is SectorEmpresa.MINERIA
    assert isinstance(resultado, ConflictError)
    assert resultado.code == "COMPANY_SECTOR_CHANGE_BLOCKED"
    assert await leer(mundo, mundo.empresa) == (SectorEmpresa.MINERIA, [SectorEmpresa.MINERIA])


# ============================================ sin pausas, muchas veces ============================================

async def test_cambios_y_reservas_simultaneos_nunca_dejan_un_documento_con_otro_sector_que_su_empresa(fabrica_pg):
    async with fabrica_pg() as db:
        usuario = await crear_usuario(db, correo="superadmin@pruebas.invalid")
        empresas = [await crear_empresa(db, nombre=f"Empresa {n}") for n in range(6)]
    mundo = Mundo(fabrica_pg, usuario, empresas[0])
    destinos = [SectorEmpresa.ENERGIA, SectorEmpresa.PETROLEO]
    tareas = []
    for n, empresa in enumerate(empresas):
        tareas.append(editar_sector(mundo, destinos[n % 2], empresa))
        tareas.append(reservar(mundo, 100 + n, empresa))
    resultados = await asyncio.gather(*tareas, return_exceptions=True)

    errores = [r for r in resultados if isinstance(r, Exception)]
    assert all(isinstance(e, ConflictError) and e.code == "COMPANY_SECTOR_CHANGE_BLOCKED" for e in errores), errores
    for empresa in empresas:
        sector, sectores_de_documentos = await leer(mundo, empresa)
        assert len(sectores_de_documentos) == 1  # la reserva siempre prospera: no se declaró sector
        assert sectores_de_documentos == [sector]  # y coincide con el de su empresa, gane quien gane


# ============================================ alcance y duración del bloqueo ============================================

@pytest.mark.parametrize("cambios", [
    dict(estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ResultadoAnalisis.OBSERVADO,
         completado_en=datetime(2026, 10, 1, tzinfo=timezone.utc), original_almacenado_en=datetime(2026, 10, 1, tzinfo=timezone.utc)),
    dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR",
         fallido_en=datetime(2026, 10, 1, tzinfo=timezone.utc), reserva_activa=False),
], ids=["completado-observado", "fallido-con-reserva-liberada"])
async def test_un_documento_en_cualquier_estado_bloquea_el_cambio_en_la_base_real(mundo, cambios):
    async with mundo.fabrica() as db:
        db.add(Documento(
            ambiente="qa", empresa_id=mundo.empresa.id, anio=2019, tipo=MEMORIA, sector=mundo.empresa.sector,
            nombre_archivo="a.md", sha256="b" * 64, tamano_bytes=3, usuario_id=mundo.usuario.id,
            clave_original=f"qa/documentos/{uuid.uuid4()}/original.md", **cambios,
        ))
        await db.commit()
    with pytest.raises(ConflictError) as capturado:
        await editar_sector(mundo, SectorEmpresa.ENERGIA)
    assert capturado.value.code == "COMPANY_SECTOR_CHANGE_BLOCKED"
    assert (await leer(mundo, mundo.empresa))[0] is SectorEmpresa.MINERIA


async def _bloqueo_libre(mundo) -> bool:
    """¿Se puede tomar ya el bloqueo exclusivo de la fila de la empresa sin esperar? (NOWAIT falla si alguien lo conserva)"""
    async with mundo.fabrica() as db:
        try:
            await db.execute(select(Empresa.id).where(Empresa.id == mundo.empresa.id).with_for_update(nowait=True))
        except DBAPIError:
            return False
        return True


async def test_el_bloqueo_se_libera_al_terminar_la_reserva_y_al_rechazar_la_edicion(mundo):
    await reservar(mundo, 7)  # la reserva termina: no deja la empresa bloqueada mientras se sube el original, se indexa, etc.
    assert await _bloqueo_libre(mundo)
    with pytest.raises(ConflictError):
        await editar_sector(mundo, SectorEmpresa.ENERGIA)  # rechazada por documentos
    assert await _bloqueo_libre(mundo)
    assert (await leer(mundo, mundo.empresa))[0] is SectorEmpresa.MINERIA


async def test_una_reserva_rechazada_por_duplicado_no_deja_la_empresa_bloqueada(mundo):
    await reservar(mundo, 8)
    with pytest.raises(ConflictError):
        await reservar(mundo, 8)  # mismo contenido: conflicto de reserva
    assert await _bloqueo_libre(mundo)

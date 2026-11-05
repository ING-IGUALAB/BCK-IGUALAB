"""Tests del servicio de Reportes (Módulo 7) con dobles: ramas de la emisión versionada, reutilización, reintentos, traducción de
conflictos, vista previa, descarga y reglas puras del versionado. Lo que depende de PostgreSQL (restricciones únicas, bloqueos,
carreras, migración) se prueba con una base real en `test_reportes_versionado_postgres.py`: un doble no lo demuestra."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

import app.modules.reportes.service as svc
from app.core.exceptions import ConflictError, NotFoundError
from app.modules.reportes.service import generar_reporte, obtener_pdf, vista_previa
from app.modules.reportes.versionado import ValidacionRef

EMPRESA, ANIO = uuid.uuid4(), 2025
VALIDACION = ValidacionRef(uuid.uuid4(), 3, "h" * 64)


class FakeDB:
    def __init__(self, reporte=None):
        self.reporte = reporte
        self.agregados = []
        self.reversiones = 0
        self.confirmaciones = 0

    def add(self, obj):
        self.agregados.append(obj)

    async def commit(self):
        self.confirmaciones += 1

    async def rollback(self):
        self.reversiones += 1

    async def get(self, _modelo, _id):
        return self.reporte


def _fila(**extra):
    base = dict(
        id=uuid.uuid4(), empresa_id=EMPRESA, empresa_nombre="Minera", anio=ANIO, version=1, es_ultima_version=True,
        validacion_id=VALIDACION.id, validacion_numero=3, generado_por=uuid.uuid4(), generado_por_nombre="Ana", sha256="a" * 64,
        creado_en=datetime(2026, 11, 3, 10, tzinfo=timezone.utc), numero_snapshot=None, huella_snapshot=None,
    )
    return SimpleNamespace(**{**base, **extra})


class _PreparacionDoble(svc._Preparacion):
    """Igual que la real, pero el HTML es un texto fijo que identifica la versión (no se renderiza la plantilla)."""

    def html(self) -> str:
        return f"<html>v{self.version}</html>"


def _preparacion(existente=None, version=2):
    analisis = SimpleNamespace(validacion=SimpleNamespace(id=VALIDACION.id, numero=3, huella_documentos=VALIDACION.huella))
    return _PreparacionDoble(analisis, VALIDACION, existente, version, datetime(2026, 11, 3, 12, tzinfo=timezone.utc))


def _compilador():
    llamadas = []

    def compilar(html):
        llamadas.append(html)
        return b"%PDF-1.4 doble"

    compilar.llamadas = llamadas  # type: ignore[attr-defined]
    return compilar


def _parchear(monkeypatch, *, preparaciones, publicaciones=()):
    """`_preparar` devuelve las preparaciones en orden; `_publicar` los resultados dados (o lanza si es una excepción)."""
    pendientes, resultados = list(preparaciones), list(publicaciones)

    async def preparar(*a, **k):
        return pendientes.pop(0)

    async def publicar(*a, **k):
        r = resultados.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(svc, "_preparar", preparar)
    monkeypatch.setattr(svc, "_publicar", publicar)


async def _generar(db, compilar):
    return await generar_reporte(db, ambiente="development", empresa_id=EMPRESA, anio=ANIO, usuario_id=uuid.uuid4(), compilar_pdf=compilar)


# --- emisión ------------------------------------------------------------------------------------------------------------

async def test_si_la_validacion_ya_tiene_version_se_reutiliza_sin_renderizar(monkeypatch):
    existente = _fila(version=4)
    _parchear(monkeypatch, preparaciones=[_preparacion(existente, version=4)])
    compilar = _compilador()
    r = await _generar(FakeDB(), compilar)
    assert (r.id, r.version, r.reutilizado) == (existente.id, 4, True)
    assert compilar.llamadas == []


async def test_una_validacion_sin_version_renderiza_una_vez_y_devuelve_lo_publicado(monkeypatch):
    emitido = SimpleNamespace(version=2, reutilizado=False)
    _parchear(monkeypatch, preparaciones=[_preparacion()], publicaciones=[emitido])
    compilar = _compilador()
    assert await _generar(FakeDB(), compilar) is emitido
    assert compilar.llamadas == ["<html>v2</html>"]


async def test_si_la_version_candidata_quedo_obsoleta_se_vuelve_a_renderizar(monkeypatch):
    emitido = SimpleNamespace(version=3, reutilizado=False)
    _parchear(monkeypatch, preparaciones=[_preparacion(version=2), _preparacion(version=3)], publicaciones=[None, emitido])
    compilar = _compilador()
    assert await _generar(FakeDB(), compilar) is emitido
    assert compilar.llamadas == ["<html>v2</html>", "<html>v3</html>"]


async def test_si_la_numeracion_nunca_se_estabiliza_responde_conflicto_de_version(monkeypatch):
    _parchear(
        monkeypatch,
        preparaciones=[_preparacion(version=v) for v in range(2, 2 + svc.MAX_INTENTOS_DE_VERSION)],
        publicaciones=[None] * svc.MAX_INTENTOS_DE_VERSION,
    )
    db = FakeDB()
    compilar = _compilador()
    with pytest.raises(ConflictError) as e:
        await _generar(db, compilar)
    assert e.value.code == "REPORT_VERSION_CONFLICT"


async def test_un_fallo_al_publicar_deshace_la_transaccion_y_se_propaga(monkeypatch):
    _parchear(monkeypatch, preparaciones=[_preparacion()], publicaciones=[RuntimeError("falla")])
    db = FakeDB()
    compilar = _compilador()
    with pytest.raises(RuntimeError):
        await _generar(db, compilar)
    assert db.reversiones == 1
    assert db.agregados == []


async def test_un_fallo_del_pdf_no_llega_a_publicar(monkeypatch):
    _parchear(monkeypatch, preparaciones=[_preparacion()], publicaciones=[])

    def roto(_html):
        raise ConflictError("PDF_GENERATION_ERROR", "sin motor")

    db = FakeDB()
    with pytest.raises(ConflictError):
        await _generar(db, roto)  # `publicaciones` vacía: si se llamara a `_publicar` fallaría con IndexError


# --- traducción de violaciones de restricciones -------------------------------------------------------------------------

def _violacion(nombre):
    causa = SimpleNamespace(constraint_name=nombre)
    return IntegrityError("INSERT", {}, SimpleNamespace(__cause__=causa))


async def test_la_violacion_de_la_validacion_se_resuelve_devolviendo_la_version_que_la_gano(monkeypatch):
    ganadora = _fila(version=2)

    async def de_validacion(*a, **k):
        return ganadora

    async def reutilizar(db, existente_id):
        return SimpleNamespace(id=existente_id, reutilizado=True)

    monkeypatch.setattr(svc, "_reporte_de_validacion", de_validacion)
    monkeypatch.setattr(svc, "_reutilizar", reutilizar)
    db = FakeDB()
    r = await svc._tras_violacion(db, _violacion(svc.RESTRICCION_UNICA_DE_VALIDACION), EMPRESA, ANIO, _preparacion())
    assert (r.id, r.reutilizado, db.reversiones) == (ganadora.id, True, 1)


async def test_la_violacion_de_la_version_es_un_conflicto_de_numeracion():
    db, violacion, preparacion = FakeDB(), _violacion(svc.RESTRICCION_UNICA_DE_VERSION), _preparacion()
    with pytest.raises(ConflictError) as e:
        await svc._tras_violacion(db, violacion, EMPRESA, ANIO, preparacion)
    assert e.value.code == "REPORT_VERSION_CONFLICT"


@pytest.mark.parametrize("nombre", ["reportes_prospeccion_sha256_key", "reportes_prospeccion_generado_por_fkey", None])
async def test_cualquier_otra_violacion_se_propaga_sin_convertirse_en_conflicto(nombre):
    excepcion, db, preparacion = _violacion(nombre), FakeDB(), _preparacion()
    with pytest.raises(IntegrityError) as e:
        await svc._tras_violacion(db, excepcion, EMPRESA, ANIO, preparacion)
    assert e.value is excepcion


async def test_la_violacion_de_la_validacion_sin_version_ganadora_se_propaga(monkeypatch):
    async def ninguna(*a, **k):
        return None

    monkeypatch.setattr(svc, "_reporte_de_validacion", ninguna)
    db, violacion, preparacion = FakeDB(), _violacion(svc.RESTRICCION_UNICA_DE_VALIDACION), _preparacion()
    with pytest.raises(IntegrityError):
        await svc._tras_violacion(db, violacion, EMPRESA, ANIO, preparacion)


def test_el_html_lleva_la_version_y_la_fecha_de_emision_en_utc(monkeypatch):
    capturado = {}

    def datos(analisis, fecha, version):
        capturado.update(fecha=fecha, version=version)
        return {}

    monkeypatch.setattr(svc, "datos_desde_analisis", datos)
    monkeypatch.setattr(svc, "renderizar", lambda d: "<html/>")
    prep = svc._Preparacion(None, VALIDACION, None, 5, datetime(2026, 11, 3, 23, 30, tzinfo=timezone.utc))
    assert prep.html() == "<html/>"
    assert capturado == {"fecha": "2026-11-03", "version": 5}


# --- vista previa y descarga ----------------------------------------------------------------------------------------------

async def test_vista_previa_candidata_usa_el_mismo_constructor_y_no_guarda(monkeypatch):
    _parchear(monkeypatch, preparaciones=[_preparacion(version=7)])
    db = FakeDB()
    compilar = _compilador()
    pdf = await vista_previa(db, ambiente="development", empresa_id=EMPRESA, anio=ANIO, compilar_pdf=compilar)
    assert pdf == b"%PDF-1.4 doble"
    assert compilar.llamadas == ["<html>v7</html>"]
    assert db.agregados == []
    assert db.confirmaciones == 0


async def test_vista_previa_de_una_emision_existente_devuelve_los_bytes_almacenados_sin_compilar(monkeypatch):
    existente = _fila(version=4)
    _parchear(monkeypatch, preparaciones=[_preparacion(existente, version=4)])
    pedidos = []

    async def almacenado(db, reporte_id):
        pedidos.append(reporte_id)
        return b"%PDF-1.4 ORIGINAL"

    monkeypatch.setattr(svc, "_pdf_almacenado", almacenado)
    db = FakeDB()
    compilar = _compilador()
    pdf = await vista_previa(db, ambiente="development", empresa_id=EMPRESA, anio=ANIO, compilar_pdf=compilar)
    assert pdf == b"%PDF-1.4 ORIGINAL"
    assert pedidos == [existente.id]
    assert compilar.llamadas == []  # no se vuelve a compilar ni a reconstruir con datos actuales
    assert db.agregados == []
    assert db.confirmaciones == 0


async def test_obtener_pdf_inexistente():
    db, reporte_id, usuario_id = FakeDB(), uuid.uuid4(), uuid.uuid4()
    with pytest.raises(NotFoundError) as e:
        await obtener_pdf(db, reporte_id, usuario_id=usuario_id)
    assert e.value.code == "REPORT_NOT_FOUND"


async def test_descargar_registra_evento_descarga_con_la_version():
    from app.models import RegistroAuditoria, TipoEventoAuditoria

    reporte = SimpleNamespace(pdf=b"%PDF-1.4", empresa_id=uuid.uuid4(), anio=2025, version=2)
    db = FakeDB(reporte)
    assert await obtener_pdf(db, uuid.uuid4(), usuario_id=uuid.uuid4()) == b"%PDF-1.4"
    (evento,) = [o for o in db.agregados if isinstance(o, RegistroAuditoria)]
    assert evento.tipo_evento is TipoEventoAuditoria.DESCARGA
    assert evento.resultado == "EXITOSO"
    assert "version=2" in evento.detalle
    assert db.confirmaciones == 1

"""Tests del servicio de Reportes (Módulo 7) con dobles: genera, snapshot y un reporte por empresa/año."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.modules.analisis_gri.schemas import EstadoGri
from app.modules.reportes.service import generar_reporte, listar_reportes, obtener_pdf, vista_previa


def _analisis():
    evid = SimpleNamespace(cita="cita")
    fila = SimpleNamespace(codigo="305", nombre="Emisiones", estandar="GRI 305", estado=EstadoGri.OK,
                           estado_etiqueta="OK", evidencias=[evid])
    return SimpleNamespace(
        empresa=SimpleNamespace(nombre="Minera", sector="MINERIA"), anio=2025, version=1,
        filas=[fila], puntaje=SimpleNamespace(valor_presentacion=100.0),
        resumen_sanciones=SimpleNamespace(total=0, cuantificadas=0, no_cuantificadas=0),
        sanciones=[],
        validacion=SimpleNamespace(huella_documentos="h" * 64,
                                   validado_por=SimpleNamespace(nombre="A"),
                                   validado_en=SimpleNamespace(date=lambda: SimpleNamespace(isoformat=lambda: "2026-10-15"))),
        model_dump=lambda mode=None: {"empresa": "Minera", "anio": 2025},
    )


class FakeDB:
    def __init__(self, existente=None):
        self.existente = existente
        self.agregados = []

    def add(self, obj):
        self.agregados.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        # simula los defaults del motor (id, creado_en)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "creado_en", None) is None:
            obj.creado_en = datetime.now(timezone.utc)

    async def execute(self, _q):
        db = self

        class R:
            def scalar_one_or_none(self):
                return db.existente

        return R()

    async def get(self, _model, _id):
        return self.existente


def _parchear(monkeypatch, *, compilar=b"%PDF-1.4 fake"):
    import app.modules.reportes.service as svc

    async def _validado(*a, **k):
        return _analisis()

    monkeypatch.setattr(svc, "obtener_analisis_validado", _validado)
    monkeypatch.setattr(svc, "compilar_html_a_pdf", lambda html, **k: compilar)
    return svc


async def test_generar_crea_reporte_con_snapshot_y_sha(monkeypatch):
    _parchear(monkeypatch)
    db = FakeDB()
    res = await generar_reporte(db, ambiente="development", empresa_id=uuid.uuid4(),
                                anio=2025, usuario_id=uuid.uuid4(),
                                compilar_pdf=lambda tex, **k: b"%PDF-1.4 fake")
    assert res.sha256 and len(res.sha256) == 64
    assert any(hasattr(o, "snapshot") for o in db.agregados)


async def test_generar_actualiza_si_ya_existe(monkeypatch):
    _parchear(monkeypatch)
    existente = SimpleNamespace(empresa_id=uuid.uuid4(), anio=2025, pdf=b"", sha256="x",
                                snapshot={}, generado_por=uuid.uuid4(), id=uuid.uuid4(),
                                creado_en=None)
    db = FakeDB(existente=existente)
    res = await generar_reporte(db, ambiente="development", empresa_id=existente.empresa_id,
                                anio=2025, usuario_id=uuid.uuid4(),
                                compilar_pdf=lambda tex, **k: b"%PDF-1.4 fake")
    assert res.id == existente.id  # mismo registro (un reporte por empresa/año, RN-041)
    assert existente.sha256 != "x"  # regenerado


async def test_no_validado_lanza_conflicto(monkeypatch):
    import app.modules.reportes.service as svc
    from app.core.exceptions import ConflictError

    async def _no(*a, **k):
        raise ConflictError("ANALYSIS_NOT_VALIDATED", "no validado")

    monkeypatch.setattr(svc, "obtener_analisis_validado", _no)
    with pytest.raises(ConflictError):
        await generar_reporte(FakeDB(), ambiente="development", empresa_id=uuid.uuid4(),
                              anio=2025, usuario_id=uuid.uuid4(),
                              compilar_pdf=lambda tex, **k: b"%PDF-1.4 fake")


async def test_vista_previa_no_guarda(monkeypatch):
    _parchear(monkeypatch)
    db = FakeDB()
    pdf = await vista_previa(db, ambiente="development", empresa_id=uuid.uuid4(),
                             anio=2025, compilar_pdf=lambda tex, **k: b"%PDF-1.4")
    assert pdf == b"%PDF-1.4"
    assert db.agregados == []  # la vista previa NO persiste


def test_obtener_pdf_inexistente():
    from app.core.exceptions import NotFoundError

    async def _correr():
        await obtener_pdf(FakeDB(), uuid.uuid4(), usuario_id=uuid.uuid4())

    with pytest.raises(NotFoundError):
        import anyio
        anyio.run(_correr)


async def test_generar_registra_evento_generacion_reporte(monkeypatch):
    _parchear(monkeypatch)
    from app.models import RegistroAuditoria, TipoEventoAuditoria

    db = FakeDB()
    await generar_reporte(db, ambiente="development", empresa_id=uuid.uuid4(), anio=2025,
                          usuario_id=uuid.uuid4(), compilar_pdf=lambda tex, **k: b"%PDF-1.4")
    eventos = [o for o in db.agregados if isinstance(o, RegistroAuditoria)]
    assert eventos and eventos[0].tipo_evento is TipoEventoAuditoria.GENERACION_REPORTE


async def test_descargar_registra_evento_descarga():
    from app.models import RegistroAuditoria, TipoEventoAuditoria

    reporte = SimpleNamespace(pdf=b"%PDF-1.4", empresa_id=uuid.uuid4(), anio=2025)
    db = FakeDB(existente=reporte)
    pdf = await obtener_pdf(db, uuid.uuid4(), usuario_id=uuid.uuid4())
    assert pdf == b"%PDF-1.4"
    eventos = [o for o in db.agregados if isinstance(o, RegistroAuditoria)]
    assert eventos and eventos[0].tipo_evento is TipoEventoAuditoria.DESCARGA

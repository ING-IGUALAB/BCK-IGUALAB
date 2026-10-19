"""Arranque del Módulo 5: el esquema se prepara por sí solo, sin depender de la ingesta, y un fallo no tumba la aplicación.
Sin bases de datos ni servicios: el motor de migraciones y el gestor se sustituyen por dobles."""
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core.exceptions import ServiceUnavailableError
from app.migraciones import catalogo, motor
from app.migraciones.motor import ErrorMigracion
from app.modules.analisis_gri.application.arranque import preparar_esquema_analisis
from app.modules.ingesta.application import gestor as modulo_gestor

SECRETOS = ("CLAVE-SECRETA", "usuario_secreto", "host-secreto")


@pytest.fixture
def url_secreta(monkeypatch):
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", "postgresql+asyncpg://usuario_secreto:CLAVE-SECRETA@host-secreto/bd")


async def test_prepara_solo_la_base_transaccional_con_su_esquema(monkeypatch, url_secreta):
    llamadas = []

    async def migrar_doble(url, esquema, **kwargs):
        llamadas.append((url, esquema.nombre, [m.version for m in esquema.migraciones]))
        return motor.ResultadoMigracion(esquema.nombre, aplicadas=(4,))

    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    app = SimpleNamespace(state=SimpleNamespace())
    await preparar_esquema_analisis(app)
    assert app.state.analisis_error is None
    assert llamadas == [(catalogo.settings.DATABASE_URL, "transaccional", [1, 2, 3, 4, 5])]  # nunca la base vectorial


async def test_un_fallo_de_migracion_deja_el_modulo_no_disponible_sin_secretos_y_sin_tumbar_el_arranque(monkeypatch, caplog, url_secreta):
    async def migrar_falla(url, esquema, **kwargs):
        raise ErrorMigracion("MIGRATION_FAILED", "La migración 0004 (analisis_gri) falló en la base transaccional.", "transaccional",
                             {"version": 4, "sqlstate": "42501"})

    monkeypatch.setattr(catalogo, "migrar", migrar_falla)
    app = SimpleNamespace(state=SimpleNamespace())
    with caplog.at_level(logging.INFO):
        await preparar_esquema_analisis(app)  # no lanza
    codigo, mensaje, detalles = app.state.analisis_error
    assert codigo == "ANALYSIS_SCHEMA_NOT_READY"
    assert detalles == {"base": "transaccional", "motivo": "MIGRATION_FAILED", "version": 4, "sqlstate": "42501"}
    texto = " ".join([mensaje, str(detalles), *[r.getMessage() for r in caplog.records]])
    assert not any(secreto in texto for secreto in SECRETOS)
    assert any("Análisis GRI no disponible" in r.getMessage() for r in caplog.records)


async def test_un_error_inesperado_tampoco_tumba_el_arranque(monkeypatch, url_secreta):
    async def migrar_falla(url, esquema, **kwargs):
        raise RuntimeError("detalle con host-secreto")

    monkeypatch.setattr(catalogo, "migrar", migrar_falla)
    app = SimpleNamespace(state=SimpleNamespace())
    await preparar_esquema_analisis(app)
    codigo, mensaje, detalles = app.state.analisis_error
    assert codigo == "ANALYSIS_SCHEMA_NOT_READY"
    assert detalles == {"motivo": "RuntimeError"}
    assert "host-secreto" not in mensaje


async def test_el_lifespan_prepara_el_esquema_del_modulo_aunque_la_ingesta_no_este_configurada(monkeypatch, url_secreta):
    from app import main

    migradas = []

    async def sin_hacer():
        pass

    async def migrar_doble(url, esquema, **kwargs):
        migradas.append(esquema.nombre)
        return motor.ResultadoMigracion(esquema.nombre)

    def construir_sin_configuracion(*args, **kwargs):
        raise ServiceUnavailableError("INGESTION_NOT_CONFIGURED", "Falta configuración de almacenamiento y embeddings.",
                                      details={"componentes": [{"componente": "almacenamiento", "motivo": "falta"}]})

    monkeypatch.setattr(main, "crear_tablas", sin_hacer)
    monkeypatch.setattr(main, "crear_superadmin_inicial", sin_hacer)
    monkeypatch.setattr(main, "exigir_esquema_central", sin_hacer)  # su verificación contra PostgreSQL real está en test_esquema_central_postgres
    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    monkeypatch.setattr(modulo_gestor, "construir_gestor", construir_sin_configuracion)
    async with main.app.router.lifespan_context(main.app):
        assert main.app.state.ingesta is None
        assert main.app.state.ingesta_error[0] == "INGESTION_NOT_CONFIGURED"
        assert main.app.state.analisis_error is None  # el módulo 5 quedó listo
    assert migradas == ["transaccional"]  # solo la del módulo; la ingesta no llegó a migrar nada


def test_los_endpoints_del_modulo_estan_registrados_y_documentados():
    from app import main

    rutas = {(m, r.path) for r in main.app.routes if hasattr(r, "methods") for m in r.methods}
    base = "/analisis-gri/empresas/{empresa_id}/anios/{anio}"
    assert {("GET", base), ("PUT", base + "/codigos/{identidad}/estado"), ("GET", base + "/historial"), ("POST", base + "/validacion")} <= rutas
    esquema = main.app.openapi()
    assert all(esquema["paths"][base + s] for s in ("", "/historial", "/validacion", "/codigos/{identidad}/estado"))
    assert {"AnalisisResponse", "AsignarEstadoRequest", "ValidarAnalisisRequest", "PaginaHistorial"} <= set(esquema["components"]["schemas"])


def test_el_router_no_importa_proveedores_ni_el_gestor_de_ingesta():
    """El módulo trabaja con datos almacenados: importarlo no arrastra OCI, MinIO/boto3, el cliente de IA ni el gestor."""
    codigo = (
        "import os, sys; os.environ.setdefault('JWT_SECRET_KEY', 'x');"
        "import app.modules.analisis_gri.router, app.modules.analisis_gri.application.servicio;"
        "prohibidos = ['oci', 'boto3', 'botocore', 'openai', 'app.modules.ingesta.application.gestor', 'app.modules.ingesta.infrastructure.proveedor_oci',"
        " 'app.modules.ingesta.infrastructure.almacenamiento_s3', 'app.modules.ingesta.domain.embeddings', 'app.core.database_vectorial'];"
        "print(sorted(m for m in prohibidos if m in sys.modules))"
    )
    resultado = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, timeout=120, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip().splitlines()[-1] == "[]"

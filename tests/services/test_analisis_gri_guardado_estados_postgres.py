"""Guardado de estados GRI bajo cambios rápidos y concurrentes, sobre un PostgreSQL REAL, AISLADO y TEMPORAL (`conftest.py`).

Qué se comprueba (Módulo 5, CU005):
- cada cambio aceptado queda guardado con su historial y su versión, y la consulta posterior coincide con lo confirmado;
- dos escrituras sobre la misma fila y versión: gana una y el resto recibe 409 `ANALYSIS_ROW_VERSION_CONFLICT` (nunca 5xx);
- cambios sobre filas distintas no se pierden ni repiten versiones;
- una versión antigua se rechaza sin ningún efecto parcial;
- las condiciones de versión de la propia escritura impiden la pérdida de actualizaciones aunque faltara el bloqueo de la fila de
  control (defensa en profundidad), sin convertir la carrera en un 500;
- se conservan las reglas de repetir el mismo estado y de invalidar una validación anterior.

Sin red, proveedores ni gestor de ingesta. Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`).
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.application import servicio
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    MD_GRI_204_Y_302,
    asegurar_esquema_real,
    asignar,
    cliente_http,
    comprobar_error,
    consultar,
    construir_app,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    fila_de,
    sembrar_documento,
    validar,
)

IDENTIDADES = ("204:2016", "302:2016", "305:2016")
TABLAS = ("analisis_gri_control", "analisis_gri_estados", "analisis_gri_historial", "analisis_gri_validaciones")


@pytest_asyncio.fixture
async def m5(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_Y_302, tipo=TipoDocumento.MEMORIA_ANUAL)
    return SimpleNamespace(fabrica=fabrica_pg, admin=admin, otro=otro, empresa=empresa)


async def _volcado(m5) -> dict[str, list]:
    """Contenido completo de las tablas del módulo (sin columnas generadas al azar), para comparar antes y después."""
    columnas = {
        "analisis_gri_control": "ambiente, empresa_id, anio, version",
        "analisis_gri_estados": "identidad, estado, version, asignado_por, asignado_en",
        "analisis_gri_historial": "identidad, estado_anterior, estado_nuevo, usuario_id, cambiado_en, version_analisis",
        "analisis_gri_validaciones": "numero, version_estados, huella_documentos, validado_por",
    }
    async with m5.fabrica() as db:
        return {
            tabla: sorted(tuple(map(str, f)) for f in (await db.execute(text(f"SELECT {cols} FROM {tabla}"))).all())
            for tabla, cols in columnas.items()
        }


async def _historial(m5) -> list[tuple]:
    async with m5.fabrica() as db:
        filas = await db.execute(
            text("SELECT identidad, estado_anterior, estado_nuevo, version_analisis FROM analisis_gri_historial ORDER BY version_analisis")
        )
        return [tuple(f) for f in filas.all()]


# ============================================ Cambios rápidos y secuenciales ============================================

async def test_cambios_rapidos_sobre_filas_distintas_con_versiones_actuales_quedan_guardados(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        inicial = await consultar(c, m5.empresa)
        assert [f["identidad"] for f in inicial["filas"]] == list(IDENTIDADES)
        versiones = {i: fila_de(inicial, i)["version_fila"] for i in IDENTIDADES}  # 0: sin estado
        confirmados: list[dict] = []
        # Dos vueltas seguidas sin consultar entre una petición y otra: cada petición usa la versión de la respuesta anterior.
        for estado in ("OK", "SUB_REPORTADO"):
            for identidad in IDENTIDADES:
                r = await asignar(c, m5.empresa, identidad, estado, versiones[identidad])
                assert r.status_code == 200, r.text
                cuerpo = r.json()
                versiones[identidad] = cuerpo["version_fila"]
                confirmados.append(cuerpo)
        assert [x["version_analisis"] for x in confirmados] == [1, 2, 3, 4, 5, 6]
        assert all(x["cambio"] is True for x in confirmados)
        final = await consultar(c, m5.empresa)
    assert final["version"] == 6
    for identidad in IDENTIDADES:
        fila = fila_de(final, identidad)
        assert (fila["estado"], fila["version_fila"]) == ("SUB_REPORTADO", 2)
    assert final["puntaje"]["estado"] == "CALCULADO_SIN_VALIDAR"
    assert final["puntaje"]["valor"] == 0.0  # tres filas Sub-reportado: el puntaje corresponde a los estados confirmados
    historial = await _historial(m5)
    assert [h[3] for h in historial] == [1, 2, 3, 4, 5, 6]  # una versión por cambio, sin huecos ni repetidos
    assert [(h[1], h[2]) for h in historial] == [(None, "OK")] * 3 + [("OK", "SUB_REPORTADO")] * 3


# ============================================ Misma fila, misma versión ============================================

async def test_ocho_cambios_simultaneos_sobre_la_misma_fila_y_version_dejan_uno_y_conflictos_claros(m5):
    estados = ["OK", "BAJA_SUSTANCIA", "SUB_REPORTADO", "OK", "BAJA_SUSTANCIA", "SUB_REPORTADO", "OK", "BAJA_SUSTANCIA"]
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        respuestas = await asyncio.gather(
            *[asignar(c1 if i % 2 else c2, m5.empresa, "204:2016", e, 0) for i, e in enumerate(estados)]
        )
        ganadoras = [r for r in respuestas if r.status_code == 200]
        assert len(ganadoras) == 1  # ningún 5xx: el resto es un conflicto informado
        for r in respuestas:
            if r.status_code != 200:
                error = comprobar_error(r, 409, "ANALYSIS_ROW_VERSION_CONFLICT")
                assert error["details"]["version_fila_esperada"] == 0
                assert error["details"]["version_fila_actual"] == 1
                assert error["details"]["version_analisis_actual"] == 1
                assert error["details"]["estado_actual"] == ganadoras[0].json()["estado"]
        final = await consultar(c1, m5.empresa)
    fila = fila_de(final, "204:2016")
    assert (fila["estado"], fila["version_fila"]) == (ganadoras[0].json()["estado"], 1)
    assert final["version"] == 1
    assert len(await _historial(m5)) == 1  # solo el cambio aceptado deja historial


# ============================================ Filas distintas ============================================

async def test_cambios_simultaneos_sobre_filas_distintas_se_guardan_todos_con_versiones_unicas(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        # La PRIMERA escritura de la empresa/año también compite por crear la fila de control.
        pedidos = [
            asignar(c1, m5.empresa, "204:2016", "OK"),
            asignar(c2, m5.empresa, "302:2016", "BAJA_SUSTANCIA"),
            asignar(c1, m5.empresa, "305:2016", "SUB_REPORTADO"),
            asignar(c2, m5.empresa, "204:2016", "SUB_REPORTADO"),  # misma fila que la primera y versión 0: puede perder
        ]
        respuestas = await asyncio.gather(*pedidos)
        codigos = [r.status_code for r in respuestas]
        assert codigos[1:3] == [200, 200]
        assert {codigos[0], codigos[3]} == {200, 409}
        final = await consultar(c1, m5.empresa)
    aceptadas = [r.json() for r in respuestas if r.status_code == 200]
    assert sorted(x["version_analisis"] for x in aceptadas) == [1, 2, 3]
    assert final["version"] == 3
    assert {f["identidad"] for f in final["filas"] if f["estado"] is not None} == set(IDENTIDADES)
    assert [h[3] for h in await _historial(m5)] == [1, 2, 3]
    for x in aceptadas:  # la consulta posterior coincide con cada respuesta confirmada
        assert fila_de(final, x["identidad"])["estado"] == x["estado"]
        assert fila_de(final, x["identidad"])["version_fila"] == x["version_fila"]


# ============================================ Versión antigua ============================================

async def test_una_version_antigua_se_rechaza_sin_efectos_parciales(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        assert (await asignar(c, m5.empresa, "204:2016", "OK", 0)).status_code == 200
        assert (await asignar(c, m5.empresa, "204:2016", "BAJA_SUSTANCIA", 1)).status_code == 200
        antes = await _volcado(m5)
        for version_vieja in (0, 1, 7):
            r = await asignar(c, m5.empresa, "204:2016", "SUB_REPORTADO", version_vieja)
            error = comprobar_error(r, 409, "ANALYSIS_ROW_VERSION_CONFLICT")
            assert error["details"]["version_fila_actual"] == 2
            assert error["details"]["estado_actual"] == "BAJA_SUSTANCIA"
        assert await _volcado(m5) == antes  # ni estado, ni historial, ni versiones, ni validaciones cambiaron
        # Una fila distinta con versión correcta no se ve afectada por el rechazo anterior.
        assert (await asignar(c, m5.empresa, "305:2016", "OK", 0)).status_code == 200


# ============================================ Qué se traduce a 409 y qué no ============================================

async def test_una_violacion_que_no_es_la_unicidad_esperada_no_se_oculta_como_conflicto(m5):
    """Solo `uq_analisis_estado_identidad` es un conflicto esperado. Una clave foránea rota (autor inexistente) es un defecto real."""
    impostor = SimpleNamespace(id=uuid.uuid4(), nombre="No existe")
    antes = await _volcado(m5)
    async with m5.fabrica() as db:
        with pytest.raises(IntegrityError) as capturado:
            await servicio.asignar_estado(
                db, ambiente="development", empresa_id=m5.empresa.id, anio=2025, identidad="204:2016",
                estado=EstadoGri.OK, version_fila_esperada=0, actor=impostor,
            )
    assert not servicio._es_unicidad_de_estado(capturado.value)
    assert await _volcado(m5) == antes  # el rollback no dejó estado, historial ni versión


async def test_la_unicidad_esperada_si_es_conflicto_y_se_identifica_por_la_restriccion(m5, monkeypatch):
    _carrera_sin_bloqueo(monkeypatch)
    detectadas: list[bool] = []
    real = servicio._es_unicidad_de_estado
    monkeypatch.setattr(servicio, "_es_unicidad_de_estado", lambda exc: detectadas.append(real(exc)) or detectadas[-1])
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        respuestas = await asyncio.gather(
            asignar(c1, m5.empresa, "204:2016", "OK", 0), asignar(c2, m5.empresa, "204:2016", "SUB_REPORTADO", 0)
        )
    assert sorted(r.status_code for r in respuestas) == [200, 409]
    assert detectadas == [True]  # la violación traducida fue exactamente la restricción de unicidad por identidad


async def test_el_409_informa_valores_del_instante_del_rechazo_y_no_deja_la_transaccion_abierta(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        assert (await asignar(c, m5.empresa, "204:2016", "OK", 0)).status_code == 200
        rechazo = comprobar_error(await asignar(c, m5.empresa, "204:2016", "SUB_REPORTADO", 0), 409, "ANALYSIS_ROW_VERSION_CONFLICT")
        assert rechazo["details"]["version_analisis_actual"] == 1
        # El rollback liberó el bloqueo: no hay transacciones abiertas y la siguiente escritura no espera.
        abiertas = await sql_pg(m5, "SELECT count(*) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()")
        assert abiertas == [(0,)]
        siguiente = await asyncio.wait_for(asignar(c, m5.empresa, "305:2016", "OK", 0), 5)
        assert siguiente.status_code == 200
    # El valor informado NO es una garantía de vigencia: ya quedó atrás.
    assert siguiente.json()["version_analisis"] == 2 != rechazo["details"]["version_analisis_actual"]


async def sql_pg(m5, consulta: str):
    async with m5.fabrica() as db:
        return [tuple(f) for f in (await db.execute(text(consulta))).all()]


# ============================================ Reglas de repetición e invalidación ============================================

async def test_repetir_el_estado_no_cambia_nada_y_un_cambio_real_invalida_la_validacion(m5):
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c:
        for identidad in IDENTIDADES:
            assert (await asignar(c, m5.empresa, identidad, "OK", 0)).status_code == 200
        consultado = await consultar(c, m5.empresa)
        assert (await validar(c, m5.empresa, consultado)).status_code == 201
        antes = await _volcado(m5)

        repetido = await asignar(c, m5.empresa, "204:2016", "OK", 1)
        assert repetido.status_code == 200
        assert repetido.json()["cambio"] is False
        assert repetido.json()["validacion_previa_invalidada"] is False
        assert await _volcado(m5) == antes  # repetir el mismo estado: sin historial ni versión
        assert (await consultar(c, m5.empresa))["validacion"]["vigente"] is True

        cambio = await asignar(c, m5.empresa, "204:2016", "SUB_REPORTADO", 1)
        assert cambio.status_code == 200
        assert cambio.json()["validacion_previa_invalidada"] is True
        despues = await consultar(c, m5.empresa)
    assert despues["validacion"]["vigente"] is False
    assert despues["habilita_reporte"] is False
    assert despues["version"] == consultado["version"] + 1
    assert len(await _historial(m5)) == 4


# ============================================ Defensa en profundidad sin bloqueo ============================================

def _carrera_sin_bloqueo(monkeypatch) -> None:
    """Quita el `FOR UPDATE` de la fila de control y hace que las dos peticiones lean ANTES de que alguna escriba: la carrera es
    determinista y solo las condiciones de versión de la propia escritura pueden evitar la pérdida de actualizaciones."""
    leer_real = servicio._leer
    barrera = asyncio.Barrier(2)

    async def sin_bloquear(db, ambiente, empresa_id, anio):
        await db.execute(
            servicio.pg_insert(servicio.AnalisisGriControl)
            .values(ambiente=ambiente, empresa_id=empresa_id, anio=anio)
            .on_conflict_do_nothing(index_elements=["ambiente", "empresa_id", "anio"])
        )
        await db.commit()  # la fila de control existe (FK) y NO queda bloqueada
        return await servicio._version_de_control(db, ambiente, empresa_id, anio)

    async def leer_y_esperar(*args, **kwargs):
        datos = await leer_real(*args, **kwargs)
        await asyncio.wait_for(barrera.wait(), 10)
        return datos

    monkeypatch.setattr(servicio, "_asegurar_y_bloquear_control", sin_bloquear)
    monkeypatch.setattr(servicio, "_leer", leer_y_esperar)


@pytest.mark.parametrize("fila_previa", [False, True], ids=["primera-asignacion", "fila-con-estado"])
async def test_sin_el_bloqueo_las_condiciones_de_version_impiden_perder_una_actualizacion(m5, monkeypatch, fila_previa):
    version = 0
    async with cliente_http(construir_app(m5.fabrica, m5.admin)) as c1, cliente_http(construir_app(m5.fabrica, m5.otro)) as c2:
        if fila_previa:
            assert (await asignar(c1, m5.empresa, "204:2016", "OK", 0)).status_code == 200  # con el servicio normal
            version = 1
        _carrera_sin_bloqueo(monkeypatch)
        respuestas = await asyncio.gather(
            asignar(c1, m5.empresa, "204:2016", "BAJA_SUSTANCIA", version),
            asignar(c2, m5.empresa, "204:2016", "SUB_REPORTADO", version),
        )
        assert sorted(r.status_code for r in respuestas) == [200, 409], [r.text for r in respuestas]  # nunca un 500
        comprobar_error(next(r for r in respuestas if r.status_code == 409), 409, "ANALYSIS_ROW_VERSION_CONFLICT")
        ganadora = next(r for r in respuestas if r.status_code == 200).json()
        monkeypatch.undo()
        final = await consultar(c1, m5.empresa)
    fila = fila_de(final, "204:2016")
    assert (fila["estado"], fila["version_fila"]) == (ganadora["estado"], version + 1)
    historial = await _historial(m5)
    assert len(historial) == (2 if fila_previa else 1)  # el cambio rechazado no dejó historial
    assert final["version"] == len(historial)
    assert [h[3] for h in historial] == list(range(1, len(historial) + 1))

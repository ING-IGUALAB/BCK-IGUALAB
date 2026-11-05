"""Reportes de prospección VERSIONADOS (BUG-15, LTX:RN-041 reformulada) sobre un PostgreSQL REAL, AISLADO y TEMPORAL.

REAL: migraciones 0004, 0005 y 0006 reales, restricciones únicas, disparadores de solo inserción, transacciones, bloqueos y
carreras entre solicitudes, el servicio real del Módulo 5 (estados y validaciones) y los roles por HTTP. CONTROLADO: el motor
HTML→PDF es un doble que devuelve bytes distintos en cada llamada y guarda el HTML recibido (permite comparar contenido y detectar
recompilaciones); `test_pdf_real_*` usa el motor real cuando el entorno lo permite. Sin PostgreSQL estas pruebas se OMITEN.
"""
from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.exceptions import ConflictError, ExternalServiceError
from app.models import TipoDocumento
from app.models.analisis_gri import EstadoGri
from app.modules.analisis_gri.application import servicio
from app.modules.reportes import service as servicio_reportes
from app.modules.reportes.service import generar_reporte
from tests.ayudantes_analisis_gri import (
    MD_GRI_204_305,
    MD_GRI_204_Y_302,
    asegurar_esquema_auditoria,
    asegurar_esquema_real,
    cliente_http,
    completar_esquema_reportes,
    crear_base_aislada,
    comprobar_error,
    crear_administrador,
    crear_empresa,
    crear_superadmin,
    preparar_esquema_anterior_a_la_0007,
    sembrar_documento,
)
from tests.services.test_reportes_postgres import construir_app

RUTA = "/reportes"
AMBIENTE = "development"
COLUMNAS = "id, empresa_id, anio, version, validacion_id, generado_por, pdf, sha256, snapshot::text AS snapshot, creado_en"


class CompiladorRegistro:
    """Devuelve bytes distintos en cada llamada y conserva el HTML recibido."""

    def __init__(self):
        self.htmls: list[str] = []

    @property
    def llamadas(self) -> int:
        return len(self.htmls)

    def __call__(self, html: str) -> bytes:
        self.htmls.append(html)
        return f"%PDF-1.4 compilacion-{len(self.htmls)}".encode()


@pytest_asyncio.fixture
async def m7(fabrica_pg, url_pg_aislado):
    await asegurar_esquema_real(url_pg_aislado)
    await asegurar_esquema_auditoria(url_pg_aislado)
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        otra = await crear_empresa(db, "Petrolera Sur")
        for e in (empresa, otra):
            await sembrar_documento(db, e, admin, texto=MD_GRI_204_305, anio=2025)
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=2024)
    return SimpleNamespace(
        fabrica=fabrica_pg, admin=admin, otro=otro, superadmin=superadmin, empresa=empresa, otra=otra, compilador=CompiladorRegistro()
    )


@pytest_asyncio.fixture
async def m7_anterior(url_pg_aislado):
    """Base nueva con el esquema de los ambientes que tienen la 0006 pero NO la 0007 (los reportes históricos aún pueden tener
    `validacion_id` nulo). El test siembra lo histórico y luego llama a `actualizar_a_la_0007`."""
    url = await crear_base_aislada(url_pg_aislado)
    await preparar_esquema_anterior_a_la_0007(url)
    motor = create_async_engine(url)
    fabrica = async_sessionmaker(motor, expire_on_commit=False)
    async with fabrica() as db:
        admin = await crear_administrador(db, "Ana Admin")
        otro = await crear_administrador(db, "Beto Admin")
        superadmin = await crear_superadmin(db)
        empresa = await crear_empresa(db, "Minera Andina")
        await sembrar_documento(db, empresa, admin, texto=MD_GRI_204_305, anio=2025)
    entorno = SimpleNamespace(
        fabrica=fabrica, admin=admin, otro=otro, superadmin=superadmin, empresa=empresa, otra=empresa, compilador=CompiladorRegistro(),
        url=url,
    )
    yield entorno
    await motor.dispose()


async def actualizar_a_la_0007(m7) -> None:
    await completar_esquema_reportes(m7.url)


# ------------------------------------------------------------------ Ayudantes ------------------------------------------------------------------

def _kw(empresa, anio: int) -> dict:
    return dict(ambiente=AMBIENTE, empresa_id=empresa.id, anio=anio)


async def analisis(m7, empresa=None, anio: int = 2025):
    async with m7.fabrica() as db:
        return await servicio.consultar_analisis(db, **_kw(empresa or m7.empresa, anio))


async def asignar_estados(m7, estados: list[EstadoGri], empresa=None, anio: int = 2025) -> None:
    """Asigna `estados[i]` a la fila i (en el orden del análisis). Una lista más corta deja el resto sin tocar."""
    empresa = empresa or m7.empresa
    actual = await analisis(m7, empresa, anio)
    for fila, estado in zip(actual.filas, estados):
        async with m7.fabrica() as db:
            await servicio.asignar_estado(
                db, identidad=fila.identidad, estado=estado, version_fila_esperada=fila.version_fila, actor=m7.admin,
                **_kw(empresa, anio),
            )


async def validar(m7, empresa=None, anio: int = 2025) -> None:
    empresa = empresa or m7.empresa
    actual = await analisis(m7, empresa, anio)
    async with m7.fabrica() as db:
        await servicio.validar_analisis(
            db, version_esperada=actual.version, huella_esperada=actual.huella_documentos, actor=m7.admin, **_kw(empresa, anio)
        )


async def revisar_y_validar(m7, estados: list[EstadoGri], empresa=None, anio: int = 2025) -> None:
    await asignar_estados(m7, estados, empresa, anio)
    await validar(m7, empresa, anio)


OK, SUB, BAJA = EstadoGri.OK, EstadoGri.SUB_REPORTADO, EstadoGri.BAJA_SUSTANCIA


async def sql(m7, consulta: str, **parametros):
    async with m7.fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


async def reportes(m7, empresa=None, anio: int | None = None):
    filtro = "" if empresa is None else f" WHERE empresa_id = '{empresa.id}'" + ("" if anio is None else f" AND anio = {anio}")
    return [tuple(f) for f in await sql(m7, f"SELECT {COLUMNAS} FROM reportes_prospeccion{filtro} ORDER BY empresa_id, anio, version")]


async def eventos(m7, tipo: str):
    return await sql(m7, "SELECT usuario_id, resultado, detalle FROM auditoria WHERE tipo_evento = :t", t=tipo)


async def emitir(m7, usuario=None, empresa=None, anio: int = 2025):
    async with cliente_http(construir_app(m7, usuario or m7.admin)) as c:
        return await c.post(RUTA, json={"empresa_id": str((empresa or m7.empresa).id), "anio": anio})


async def get(m7, ruta: str, usuario=None, **params):
    async with cliente_http(construir_app(m7, usuario or m7.admin)) as c:
        return await c.get(ruta, params=params)


def params(empresa=None, anio: int = 2025) -> dict:
    return {"empresa_id": str((empresa or SimpleNamespace(id=None)).id), "anio": anio}


async def validacion_id(m7, numero: int, empresa=None, anio: int = 2025):
    filas = await sql(
        m7, "SELECT id FROM analisis_gri_validaciones WHERE empresa_id = :e AND anio = :a AND numero = :n",
        e=(empresa or m7.empresa).id, a=anio, n=numero,
    )
    return filas[0][0]


async def escenario_v1(m7):
    """Revisión + validación #1 con puntaje 50 (OK, SUB_REPORTADO) y versión 1 emitida."""
    await revisar_y_validar(m7, [OK, SUB])
    r = await emitir(m7)
    assert r.status_code == 201, r.text
    return r.json()


async def escenario_validacion_2(m7):
    """Tras la versión 1: un cambio de estado invalida la validación #1 y la validación #2 (puntaje 100) queda vigente."""
    await asignar_estados(m7, [OK, OK])
    await validar(m7)


# ============================================ 1 · Primera emisión ============================================

async def test_primera_emision_es_la_version_1_con_su_validacion_su_pdf_y_su_snapshot(m7):
    await revisar_y_validar(m7, [OK, SUB])
    r = await emitir(m7)
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    (fila,) = await reportes(m7)
    assert (cuerpo["version"], cuerpo["es_ultima_version"], cuerpo["reutilizado"]) == (1, True, False)
    assert cuerpo["validacion_id"] == str(fila[4]) == str(await validacion_id(m7, 1))
    assert cuerpo["validacion_numero"] == 1
    assert cuerpo["id"] == str(fila[0])
    assert fila[3] == 1
    assert cuerpo["sha256"] == fila[7] == hashlib.sha256(fila[6]).hexdigest()
    assert f'"id": "{fila[4]}"' in fila[8]  # el snapshot guarda la validación que lo respalda
    assert '"estado_flujo": "VALIDADO"' in fila[8]
    detalle = (await eventos(m7, "GENERACION_REPORTE"))[0][2]
    assert "version=1" in detalle
    assert "validacion=1" in detalle
    html = m7.compilador.htmls[0]
    assert "Versión 1 del reporte" in html
    assert "Emitido el" in html


# ============================================ 2 · Nueva validación → versión 2 ============================================

async def test_una_nueva_validacion_habilita_la_version_2_con_los_resultados_actualizados(m7):
    v1 = await escenario_v1(m7)
    await escenario_validacion_2(m7)
    r = await emitir(m7)
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert (v2["version"], v2["validacion_numero"], v2["es_ultima_version"]) == (2, 2, True)
    assert v2["id"] != v1["id"]
    assert v2["validacion_id"] != v1["validacion_id"]
    filas = await reportes(m7)
    assert [f[3] for f in filas] == [1, 2]
    assert '"valor_presentacion": 50.0' in filas[0][8]
    assert '"valor_presentacion": 100.0' in filas[1][8]
    assert "50/100" in m7.compilador.htmls[0]
    assert "100/100" in m7.compilador.htmls[1]
    assert "Versión 2 del reporte" in m7.compilador.htmls[1]
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 2


async def test_caso_equivalente_al_observado_50_de_100_y_luego_un_puntaje_distinto_tras_otra_validacion(m7):
    """Primera emisión con 50/100; después aparece un documento con un código GRI más y una segunda validación cambia el puntaje."""
    await revisar_y_validar(m7, [OK, SUB])
    v1 = (await emitir(m7)).json()
    async with m7.fabrica() as db:
        await sembrar_documento(db, m7.empresa, m7.admin, texto=MD_GRI_204_Y_302, anio=2025, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    comprobar_error(await emitir(m7), 409, "ANALYSIS_NOT_VALIDATED")  # el análisis cambió: la validación #1 ya no es vigente
    previo = await analisis(m7)
    assert len(previo.filas) == 3
    await asignar_estados(m7, [OK, OK, SUB])
    await validar(m7)
    puntaje = (await analisis(m7)).puntaje.valor_presentacion
    assert puntaje == 66.7
    v2 = (await emitir(m7)).json()
    assert (v1["version"], v2["version"]) == (1, 2)
    assert "50/100" in m7.compilador.htmls[0]
    assert "66.7/100" in m7.compilador.htmls[-1]
    # la descarga de la versión 1 sigue entregando su PDF (50/100), la vigente entrega el nuevo
    assert (await get(m7, f"{RUTA}/{v1['id']}/archivo")).content == (await reportes(m7))[0][6]
    vigente = await get(m7, f"{RUTA}/vigente/archivo", **params(m7.empresa))
    assert vigente.content == (await reportes(m7))[1][6]


# ============================================ 3 · La versión 1 no cambia ============================================

async def test_la_version_1_conserva_sus_bytes_su_snapshot_y_su_fila_completa(m7):
    v1 = await escenario_v1(m7)
    antes = (await reportes(m7))[0]
    await escenario_validacion_2(m7)
    assert (await emitir(m7)).status_code == 201
    filas = await reportes(m7)
    assert filas[0] == antes  # id, versión, validación, autor, PDF, SHA-256, snapshot y fecha idénticos
    descarga = await get(m7, f"{RUTA}/{v1['id']}/archivo")
    assert descarga.content == antes[6]
    assert hashlib.sha256(descarga.content).hexdigest() == v1["sha256"]
    for sentencia in ("UPDATE reportes_prospeccion SET pdf = '\\x00'::bytea", "DELETE FROM reportes_prospeccion"):
        consulta = text(sentencia)
        async with m7.fabrica() as db:
            with pytest.raises(DBAPIError) as e:
                await db.execute(consulta)
            assert "solo inserción" in str(e.value.orig)
    assert (await reportes(m7)) == filas


# ============================================ 4 · Idempotencia por validación ============================================

async def test_repetir_la_solicitud_con_la_misma_validacion_reutiliza_la_version_sin_crear_otra(m7):
    v1 = await escenario_v1(m7)
    antes, compilaciones, eventos_antes = await reportes(m7), m7.compilador.llamadas, len(await eventos(m7, "GENERACION_REPORTE"))
    for usuario in (m7.admin, m7.otro):
        r = await emitir(m7, usuario)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == v1["id"]
        assert r.json()["reutilizado"] is True
        assert r.json()["version"] == 1
    assert await reportes(m7) == antes
    assert m7.compilador.llamadas == compilaciones  # ni siquiera se recompiló
    assert len(await eventos(m7, "GENERACION_REPORTE")) == eventos_antes
    # una versión 2 emitida también se reutiliza
    await escenario_validacion_2(m7)
    v2 = (await emitir(m7)).json()
    otra_vez = await emitir(m7)
    assert (otra_vez.status_code, otra_vez.json()["id"], otra_vez.json()["version"]) == (200, v2["id"], 2)
    assert len(await reportes(m7)) == 2


# ============================================ 5 y 6 · Concurrencia ============================================

async def _con_barrera(monkeypatch, participantes: int) -> None:
    """Todas las solicitudes llegan renderizadas a la transacción final al mismo tiempo: la carrera es determinista y la deciden
    los bloqueos y las restricciones de la base, no el orden de llegada."""
    barrera = asyncio.Barrier(participantes)
    original = servicio_reportes._publicar

    async def con_barrera(*args, **kwargs):
        await barrera.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(servicio_reportes, "_publicar", con_barrera)


async def _emitir_en_paralelo(m7, usuarios: list):
    async def intento(usuario):
        async with m7.fabrica() as db:
            try:
                return await generar_reporte(
                    db, ambiente=AMBIENTE, empresa_id=m7.empresa.id, anio=2025, usuario_id=usuario.id, compilar_pdf=m7.compilador
                )
            except ConflictError as exc:
                return exc

    return await asyncio.gather(*(intento(u) for u in usuarios))


async def test_solicitudes_simultaneas_para_la_misma_validacion_crean_una_sola_version(m7, monkeypatch):
    await revisar_y_validar(m7, [OK, SUB])
    await _con_barrera(monkeypatch, 3)
    resultados = await _emitir_en_paralelo(m7, [m7.admin, m7.otro, m7.admin])
    assert not [r for r in resultados if isinstance(r, ConflictError)]
    assert m7.compilador.llamadas == 3, "las tres debían renderizar antes de la carrera"
    assert sorted(r.reutilizado for r in resultados) == [False, True, True]
    assert len({r.id for r in resultados}) == 1
    assert [f[3] for f in await reportes(m7)] == [1]
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 1  # los eventos de las perdedoras se deshicieron con su transacción


async def test_la_numeracion_concurrente_no_duplica_versiones_tras_una_primera_emision(m7, monkeypatch):
    await escenario_v1(m7)
    await escenario_validacion_2(m7)
    await _con_barrera(monkeypatch, 4)
    resultados = await _emitir_en_paralelo(m7, [m7.admin, m7.otro, m7.admin, m7.otro])
    assert not [r for r in resultados if isinstance(r, ConflictError)]
    assert {r.version for r in resultados} == {2}
    assert sum(not r.reutilizado for r in resultados) == 1
    assert [f[3] for f in await reportes(m7)] == [1, 2]


async def test_si_otro_proceso_ocupa_la_version_candidata_se_traduce_solo_ese_conflicto(m7, monkeypatch):
    await escenario_v1(m7)
    await escenario_validacion_2(m7)
    antes = await reportes(m7)

    async def siempre_cero(*args, **kwargs):  # la numeración que ve esta solicitud está desactualizada: intenta la versión 1
        return 0

    monkeypatch.setattr(servicio_reportes, "_version_maxima", siempre_cero)
    comprobar_error(await emitir(m7), 409, "REPORT_VERSION_CONFLICT")
    assert await reportes(m7) == antes
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 1


async def test_si_la_version_candidata_cambia_antes_de_publicar_se_vuelve_a_renderizar(m7, monkeypatch):
    await escenario_v1(m7)
    await escenario_validacion_2(m7)
    original = servicio_reportes._version_maxima
    respuestas = iter([1, 7])  # 1.ª lectura (preparación): candidata 2 · 2.ª (publicación): otra emisión avanzó → reintento

    async def con_cambio(*args, **kwargs):
        return next(respuestas, None) or await original(*args, **kwargs)

    monkeypatch.setattr(servicio_reportes, "_version_maxima", con_cambio)
    compilaciones = m7.compilador.llamadas
    r = await emitir(m7)
    assert r.status_code == 201
    assert r.json()["version"] == 2
    assert m7.compilador.llamadas == compilaciones + 2  # renderizó dos veces: la primera candidata quedó obsoleta
    assert [f[3] for f in await reportes(m7)] == [1, 2]


async def test_una_violacion_ajena_a_la_unicidad_no_se_disfraza_de_conflicto(m7):
    from sqlalchemy.exc import IntegrityError
    import uuid

    await revisar_y_validar(m7, [OK, SUB])
    autor_inexistente = uuid.uuid4()
    async with m7.fabrica() as db:
        with pytest.raises(IntegrityError):  # autor inexistente: viola una clave foránea, no la unicidad de la versión
            await generar_reporte(
                db, ambiente=AMBIENTE, empresa_id=m7.empresa.id, anio=2025, usuario_id=autor_inexistente, compilar_pdf=m7.compilador
            )
    assert await reportes(m7) == []


# ============================================ 7 · El origen cambia durante la generación ============================================

async def _cambiar_estado(m7):
    await asignar_estados(m7, [OK, OK])


async def _cambiar_y_revalidar(m7):
    await asignar_estados(m7, [OK, OK])
    await validar(m7)


@pytest.mark.parametrize("cambio", [_cambiar_estado, _cambiar_y_revalidar], ids=["estado_sin_validar", "otra_validacion"])
async def test_si_el_origen_cambia_mientras_se_genera_no_se_publica_un_reporte_obsoleto(m7, monkeypatch, cambio):
    await revisar_y_validar(m7, [OK, SUB])
    original = servicio_reportes._preparar

    async def preparar_y_cambiar(*args, **kwargs):
        prep = await original(*args, **kwargs)  # el snapshot ya se capturó: lo que sigue ocurre "durante" el renderizado
        await cambio(m7)
        return prep

    monkeypatch.setattr(servicio_reportes, "_preparar", preparar_y_cambiar)
    comprobar_error(await emitir(m7), 409, "REPORT_SOURCE_CHANGED")
    assert await reportes(m7) == []
    assert await eventos(m7, "GENERACION_REPORTE") == []
    monkeypatch.setattr(servicio_reportes, "_preparar", original)
    if cambio is _cambiar_y_revalidar:  # la validación nueva sí se emite en la solicitud siguiente
        assert (await emitir(m7)).json()["validacion_numero"] == 2
    else:  # sin nueva validación sigue sin poder emitirse
        comprobar_error(await emitir(m7), 409, "ANALYSIS_NOT_VALIDATED")


# ============================================ 8 · Fallos de PDF o de persistencia ============================================

async def test_un_fallo_del_pdf_o_de_la_persistencia_conserva_las_versiones_anteriores(m7, monkeypatch):
    await escenario_v1(m7)
    await escenario_validacion_2(m7)
    antes = await reportes(m7)

    def pdf_roto(_html):
        raise ExternalServiceError("PDF_GENERATION_ERROR", "motor de PDF no disponible")

    m7_roto = SimpleNamespace(**{**vars(m7), "compilador": pdf_roto})
    async with cliente_http(construir_app(m7_roto, m7.admin)) as c:
        r = await c.post(RUTA, json={"empresa_id": str(m7.empresa.id), "anio": 2025})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "PDF_GENERATION_ERROR"
    assert await reportes(m7) == antes

    def evento_roto(*args, **kwargs):
        raise RuntimeError("fallo de persistencia simulado")

    monkeypatch.setattr(servicio_reportes, "registrar_evento", evento_roto)
    r = await emitir(m7)
    assert r.status_code == 500
    assert await reportes(m7) == antes  # sin versión parcial ni evento
    assert len(await eventos(m7, "GENERACION_REPORTE")) == 1
    monkeypatch.undo()
    assert (await emitir(m7)).status_code == 201  # y la emisión siguiente funciona
    assert [f[3] for f in await reportes(m7)] == [1, 2]


# ============================================ 9 · Cambios GRI sin nueva validación ============================================

async def test_cambiar_estados_sin_validar_de_nuevo_no_habilita_la_emision(m7):
    await escenario_v1(m7)
    await asignar_estados(m7, [OK, OK])
    antes = await reportes(m7)
    error = comprobar_error(await emitir(m7), 409, "ANALYSIS_NOT_VALIDATED")
    assert error["details"]["estado_flujo"] == "LISTO_PARA_VALIDAR"
    assert await reportes(m7) == antes
    disponible = (await get(m7, f"{RUTA}/disponibilidad", empresa_id=str(m7.empresa.id), anio=2025)).json()["anios"][0]
    assert (disponible["puede_generar"], disponible["motivo_no_generar"]) == (False, "PENDIENTE_VALIDACION")
    assert disponible["validacion_anterior_no_vigente"] is True
    assert disponible["actualizacion_pendiente"] is False  # no hay validación posterior; hay cambios sin validar


# ============================================ 10 · Vista previa = emisión ============================================

async def test_la_vista_previa_y_la_emision_parten_del_mismo_contenido_validado(m7):
    await escenario_v1(m7)
    await escenario_validacion_2(m7)
    antes = await reportes(m7)
    async with cliente_http(construir_app(m7, m7.admin)) as c:
        previa = await c.post(RUTA + "/vista-previa", json={"empresa_id": str(m7.empresa.id), "anio": 2025})
        assert previa.status_code == 200
        assert previa.content.startswith(b"%PDF")
        assert await reportes(m7) == antes  # no persiste
        assert (await c.post(RUTA, json={"empresa_id": str(m7.empresa.id), "anio": 2025})).status_code == 201
        despues_de_emitir = await c.post(RUTA + "/vista-previa", json={"empresa_id": str(m7.empresa.id), "anio": 2025})
    html_previa, html_emision = m7.compilador.htmls[-2:]  # la vista previa posterior a la emisión NO compila
    assert html_previa == html_emision  # mismo puntaje, mismos estados, misma versión, misma fecha
    assert "100/100" in html_previa
    assert "Versión 2 del reporte" in html_previa
    assert despues_de_emitir.status_code == 200
    assert despues_de_emitir.content == (await reportes(m7))[1][6]  # ya emitida: los bytes ALMACENADOS de esa emisión
    assert len(await reportes(m7)) == 2


# ============================================ 11 · Descarga vigente e historial ============================================

async def test_descarga_vigente_historial_y_descarga_por_identificador(m7):
    v1 = await escenario_v1(m7)
    pdf1 = (await reportes(m7))[0][6]
    vigente = await get(m7, f"{RUTA}/vigente/archivo", **params(m7.empresa))
    assert (vigente.status_code, vigente.content, vigente.headers["x-reporte-version"]) == (200, pdf1, "1")

    await escenario_validacion_2(m7)  # validación posterior sin emitir
    pendiente = comprobar_error(await get(m7, f"{RUTA}/vigente/archivo", **params(m7.empresa)), 409, "REPORT_UPDATE_PENDING")
    assert pendiente["details"]["reporte_id"] == v1["id"]
    assert pendiente["details"]["puede_generar"] is True
    meta = (await get(m7, f"{RUTA}/vigente", **params(m7.empresa))).json()
    assert (meta["id"], meta["version"], meta["es_ultima_version"]) == (v1["id"], 1, True)
    assert (meta["actualizacion_pendiente"], meta["reporte_actualizado"], meta["puede_generar"]) == (True, False, True)
    assert (await get(m7, f"{RUTA}/{v1['id']}/archivo")).content == pdf1  # la histórica sigue disponible por identificador

    v2 = (await emitir(m7)).json()
    pdf2 = (await reportes(m7))[1][6]
    vigente = await get(m7, f"{RUTA}/vigente/archivo", **params(m7.empresa))
    assert (vigente.status_code, vigente.content, vigente.headers["x-reporte-version"]) == (200, pdf2, "2")
    assert vigente.content != pdf1
    meta = (await get(m7, f"{RUTA}/vigente", **params(m7.empresa))).json()
    assert (meta["id"], meta["actualizacion_pendiente"], meta["motivo_no_generar"]) == (v2["id"], False, "REPORTE_ACTUALIZADO")
    assert (await get(m7, f"{RUTA}/{v1['id']}/archivo")).content == pdf1
    assert (await get(m7, f"{RUTA}/{v2['id']}/archivo")).content == pdf2

    historial = (await get(m7, RUTA)).json()
    assert [(x["version"], x["es_ultima_version"]) for x in historial] == [(2, True), (1, False)]
    assert [x["id"] for x in historial] == [v2["id"], v1["id"]]  # el historial conserva las dos versiones, la nueva primero
    assert historial[1]["validacion_numero"] == 1
    assert historial[0]["validacion_numero"] == 2
    assert [x["version"] for x in (await get(m7, RUTA, limite=1)).json()] == [2]
    assert [x["version"] for x in (await get(m7, RUTA, limite=1, desplazamiento=1)).json()] == [1]
    assert (await get(m7, RUTA, empresa_id=str(m7.otra.id))).json() == []
    assert len((await get(m7, RUTA, empresa_id=str(m7.empresa.id), anio=2025)).json()) == 2
    assert (await get(m7, RUTA, anio=2024)).json() == []
    assert [e[1] for e in await eventos(m7, "DESCARGA")].count("EXITOSO") == 5


async def test_sin_reportes_la_consulta_vigente_es_404(m7):
    comprobar_error(await get(m7, f"{RUTA}/vigente", **params(m7.empresa)), 404, "REPORT_NOT_FOUND")
    comprobar_error(await get(m7, f"{RUTA}/vigente/archivo", **params(m7.empresa)), 404, "REPORT_NOT_FOUND")


# ============================================ 12 · Disponibilidad ============================================

async def _anio(m7, anio: int = 2025, empresa=None) -> dict:
    r = await get(m7, f"{RUTA}/disponibilidad", empresa_id=str((empresa or m7.empresa).id), anio=anio)
    assert r.status_code == 200, r.text
    return r.json()["anios"][0]


async def test_la_disponibilidad_distingue_reporte_actualizado_y_actualizacion_pendiente(m7):
    await revisar_y_validar(m7, [OK, SUB])
    sin_reporte = await _anio(m7)
    assert (sin_reporte["estado"], sin_reporte["puede_generar"], sin_reporte["versiones_emitidas"]) == ("VALIDACION_VIGENTE", True, 0)

    await emitir(m7)
    actualizado = await _anio(m7)
    assert (actualizado["estado"], actualizado["puede_generar"], actualizado["motivo_no_generar"]) == ("REPORTE_GENERADO", False, "REPORTE_ACTUALIZADO")
    assert (actualizado["reporte_actualizado"], actualizado["actualizacion_pendiente"], actualizado["reporte_version"]) == (True, False, 1)

    await escenario_validacion_2(m7)
    pendiente = await _anio(m7)
    assert (pendiente["estado"], pendiente["puede_generar"], pendiente["motivo_no_generar"]) == ("ACTUALIZACION_PENDIENTE", True, None)
    assert (pendiente["reporte_actualizado"], pendiente["actualizacion_pendiente"]) == (False, True)
    assert (pendiente["reporte_version"], pendiente["ultima_validacion_numero"], pendiente["reporte_validacion_numero"]) == (1, 2, 1)

    await emitir(m7)
    nuevo = await _anio(m7)
    assert (nuevo["estado"], nuevo["puede_generar"], nuevo["reporte_version"], nuevo["versiones_emitidas"]) == ("REPORTE_GENERADO", False, 2, 2)

    # una validación posterior que deja de ser vigente sigue pendiente de emitir, pero ya no puede emitirse
    await asignar_estados(m7, [SUB, OK])
    await validar(m7)  # validación #3
    await asignar_estados(m7, [OK, OK])  # y vuelve a invalidarse
    revalidar = await _anio(m7)
    assert (revalidar["estado"], revalidar["puede_generar"], revalidar["actualizacion_pendiente"]) == ("PENDIENTE_VALIDACION", False, True)
    assert revalidar["motivo_no_generar"] == "PENDIENTE_VALIDACION"


async def test_un_anio_con_reportes_no_bloquea_otro_anio_ni_otra_empresa(m7):
    await escenario_v1(m7)
    anios = (await get(m7, f"{RUTA}/disponibilidad", empresa_id=str(m7.empresa.id))).json()["anios"]
    assert [(a["anio"], a["estado"]) for a in anios] == [(2025, "REPORTE_GENERADO"), (2024, "PENDIENTE_REVISION")]
    assert (await _anio(m7, 2025, m7.otra))["estado"] == "PENDIENTE_REVISION"
    assert (await _anio(m7, 2025, m7.otra))["versiones_emitidas"] == 0


# ============================================ 14 · Aislamiento ============================================

async def test_las_versiones_se_numeran_por_empresa_y_anio(m7):
    await revisar_y_validar(m7, [OK, SUB])
    await revisar_y_validar(m7, [OK, OK], anio=2024)
    await revisar_y_validar(m7, [BAJA, OK], empresa=m7.otra)
    assert (await emitir(m7)).json()["version"] == 1
    assert (await emitir(m7, anio=2024)).json()["version"] == 1
    assert (await emitir(m7, empresa=m7.otra)).json()["version"] == 1
    await asignar_estados(m7, [OK, OK])
    await validar(m7)
    assert (await emitir(m7)).json()["version"] == 2
    assert [(f[1], f[2], f[3]) for f in await reportes(m7)].count((m7.empresa.id, 2024, 1)) == 1
    assert len(await reportes(m7, m7.otra)) == 1
    assert len(await reportes(m7, m7.empresa, 2024)) == 1
    assert len(await reportes(m7, m7.empresa, 2025)) == 2


# ============================================ 15 · Permisos y errores de negocio ============================================

async def test_permisos_y_errores_de_negocio_de_todos_los_endpoints(m7):
    destinos = [
        ("POST", RUTA, {"empresa_id": str(m7.empresa.id), "anio": 2025}, None),
        ("POST", RUTA + "/vista-previa", {"empresa_id": str(m7.empresa.id), "anio": 2025}, None),
        ("GET", RUTA, None, None),
        ("GET", RUTA + "/vigente", None, params(m7.empresa)),
        ("GET", RUTA + "/vigente/archivo", None, params(m7.empresa)),
        ("GET", RUTA + "/disponibilidad", None, {"empresa_id": str(m7.empresa.id)}),
        ("GET", f"{RUTA}/{m7.empresa.id}/archivo", None, None),
    ]
    async with cliente_http(construir_app(m7, None)) as sin_sesion, cliente_http(construir_app(m7, m7.superadmin)) as super_:
        for metodo, url, cuerpo, consulta in destinos:
            comprobar_error(await sin_sesion.request(metodo, url, json=cuerpo, params=consulta), 401, "INVALID_SESSION")
            comprobar_error(await super_.request(metodo, url, json=cuerpo, params=consulta), 403, "FORBIDDEN")
    assert await reportes(m7) == []
    assert m7.compilador.llamadas == 0

    class _Inexistente:
        id = "00000000-0000-4000-8000-000000000000"

    comprobar_error(await emitir(m7, empresa=_Inexistente), 404, "COMPANY_NOT_FOUND")
    comprobar_error(await emitir(m7), 409, "ANALYSIS_NOT_VALIDATED")  # sin revisar ni validar
    comprobar_error(await get(m7, f"{RUTA}/{m7.empresa.id}/archivo"), 404, "REPORT_NOT_FOUND")
    comprobar_error(await get(m7, f"{RUTA}/vigente", empresa_id=str(m7.empresa.id), anio=1999), 422, "REQUEST_VALIDATION_ERROR")
    comprobar_error(await get(m7, RUTA, limite=0), 422, "REQUEST_VALIDATION_ERROR")
    async with cliente_http(construir_app(m7, m7.admin)) as c:
        comprobar_error(await c.post(RUTA, json={"empresa_id": str(m7.empresa.id), "anio": 2025, "version": 5}), 422, "REQUEST_VALIDATION_ERROR")
    assert await reportes(m7) == []


# ============================================ Reportes históricos (anteriores al versionado) ============================================

async def _insertar_historico(m7, snapshot_json: str) -> None:
    """Reporte con la forma de antes del versionado: versión 1 y sin `validacion_id`."""
    async with m7.fabrica() as db:
        await db.execute(
            text(
                "INSERT INTO reportes_prospeccion (id, empresa_id, anio, version, validacion_id, generado_por, pdf, sha256, snapshot)"
                " VALUES (gen_random_uuid(), :e, 2025, 1, NULL, :u, :pdf, :h, CAST(:s AS jsonb))"
            ),
            {"e": m7.empresa.id, "u": m7.admin.id, "pdf": b"%PDF-1.4 historico", "h": "c" * 64, "s": snapshot_json},
        )
        await db.commit()


async def test_un_reporte_historico_sin_vinculo_se_reutiliza_solo_si_su_snapshot_declara_esa_validacion(m7_anterior):
    import json

    m7 = m7_anterior
    await revisar_y_validar(m7, [OK, SUB])
    actual = await analisis(m7)
    snapshot = {"validacion": {"numero": actual.validacion.numero, "huella_documentos": actual.validacion.huella_documentos}}
    await _insertar_historico(m7, json.dumps(snapshot))
    await actualizar_a_la_0007(m7)  # la actualización conserva el histórico con validacion_id NULL
    antes = await reportes(m7)
    r = await emitir(m7)
    assert r.status_code == 200
    assert r.json()["reutilizado"] is True
    assert r.json()["version"] == 1
    assert r.json()["validacion_id"] is None
    assert r.json()["validacion_numero"] == 1
    assert await reportes(m7) == antes
    assert m7.compilador.llamadas == 0
    disponible = await _anio(m7)
    assert (disponible["estado"], disponible["reporte_actualizado"]) == ("REPORTE_GENERADO", True)


async def test_un_reporte_historico_de_otra_validacion_o_sin_datos_no_respalda_la_actual(m7_anterior):
    m7 = m7_anterior
    await revisar_y_validar(m7, [OK, SUB])
    await _insertar_historico(m7, "{}")  # snapshot sin datos de validación: no se supone nada
    await actualizar_a_la_0007(m7)
    anio = await _anio(m7)
    assert anio["estado"] == "VALIDACION_VIGENTE"  # validación vigente sin emisión verificablemente asociada
    assert anio["puede_generar"] is True
    assert anio["origen_verificable"] is False
    assert anio["reporte_actualizado"] is False
    assert anio["actualizacion_pendiente"] is False  # no hay forma de saber que exista una validación posterior
    r = await emitir(m7)
    assert r.status_code == 201
    assert r.json()["version"] == 2
    assert r.json()["validacion_numero"] == 1
    assert [f[3] for f in await reportes(m7)] == [1, 2]
    assert (await reportes(m7))[0][4] is None  # el histórico sigue sin vínculo


# ============================================ PDF real ============================================

async def test_pdf_real_contiene_la_version_el_puntaje_y_la_fecha_de_emision(m7):
    pytest.importorskip("weasyprint", exc_type=(ImportError, OSError))
    pypdf = pytest.importorskip("pypdf")
    import io

    from app.modules.reportes.infrastructure.pdf import compilar_html_a_pdf

    compilaciones = []

    def contando(html):
        compilaciones.append(html)
        return compilar_html_a_pdf(html)

    m7.compilador = contando
    await revisar_y_validar(m7, [OK, SUB])
    v1 = (await emitir(m7)).json()
    await escenario_validacion_2(m7)
    v2 = (await emitir(m7)).json()
    # La vista previa de una emisión existente entrega los bytes almacenados SIN recompilar (no exige un motor determinista).
    async with cliente_http(construir_app(m7, m7.admin)) as c:
        previa = await c.post(RUTA + "/vista-previa", json={"empresa_id": str(m7.empresa.id), "anio": 2025})
    assert previa.content == (await reportes(m7))[1][6]
    assert len(compilaciones) == 2
    textos = []
    for creado in (v1, v2):
        pdf = (await get(m7, f"{RUTA}/{creado['id']}/archivo")).content
        assert pdf[:5] == b"%PDF-"
        textos.append(" ".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(pdf)).pages))
    assert "Versión 1 del reporte" in textos[0]
    assert "50/100" in textos[0]
    assert "Versión 2 del reporte" in textos[1]
    assert "100/100" in textos[1]
    assert v1["creado_en"][:10] in textos[0]
    assert v2["creado_en"][:10] in textos[1]

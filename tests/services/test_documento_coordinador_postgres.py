"""SQL del coordinador de ingesta, EJECUTADO tal cual en un PostgreSQL REAL, AISLADO y TEMPORAL (el clúster de
`conftest.py`: `initdb` local en 127.0.0.1, sin `DATABASE_URL` ni bases compartidas; se destruye al terminar).

- El DDL que genera el modelo (`generar_ddl`) es la referencia de instalación nueva.
- La migración `app/migraciones/transaccional/0002_documentos_coordinador.sql` actualiza una tabla creada con el DDL de la Etapa 4A
  (congelado en `tests/services/datos/documentos_etapa_4a.sql`) y deja un esquema IDÉNTICO al de instalación nueva.
- Las restricciones CHECK (consistencia análisis/clasificación, estructura del JSON, publicación vectorial, etapas,
  contadores) rechazan en la propia BD lo que el servicio ya valida.
- El arranque (`crear_tablas`) no crea `documentos` ni sus tipos aunque el modelo esté cargado (D17).

Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`) y NO cuentan como ejecutadas.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.exceptions import BusinessValidationError, ConflictError
from app.models.documento_ingesta import EstadoProcesamiento, ResultadoAnalisis
from app.services.ingesta import documento_service as servicio
from scripts.generar_ddl_documentos import generar_ddl
from tests.ayudantes_ingesta import SesionSQLite, crear_empresa, crear_usuario, metadatos, sha256_de
from tests.services.test_documento_ddl_postgres import _crear_base_con_prerequisitos

RAIZ = Path(__file__).resolve().parents[2]
MIGRACIONES = RAIZ / "app" / "migraciones" / "transaccional"
SQL_ACTUALIZACION = MIGRACIONES / "0002_documentos_coordinador.sql"
SQL_ETAPA_4A = Path(__file__).parent / "datos" / "documentos_etapa_4a.sql"

COLUMNAS_NUEVAS = {
    "analisis", "etapa_actual", "fragmentos_procesados", "fragmentos_total", "progreso_actualizado_en",
    "advertencias", "vector_escritura_intentada_en", "vector_publicado_en", "vector_publicacion_intentos",
    "vector_ultimo_error",
}


def leer(ruta: Path) -> str:
    texto = ruta.read_text(encoding="utf-8")
    # La migración 0002 no trae transacción propia (la pone el motor): aquí se envuelve para probar su atomicidad.
    return "BEGIN;" + chr(10) + texto + chr(10) + "COMMIT;" if ruta == SQL_ACTUALIZACION else texto


async def ejecutar(esquema, sql: str) -> None:
    conexion = await esquema.conectar()
    try:
        await conexion.execute(sql)
    finally:
        await conexion.close()


async def consultar(esquema, sql: str, *args):
    conexion = await esquema.conectar()
    try:
        return await conexion.fetch(sql, *args)
    finally:
        await conexion.close()


async def descripcion_del_esquema(esquema) -> dict:
    columnas = await consultar(
        esquema,
        "SELECT column_name, data_type, udt_name, is_nullable, column_default, character_maximum_length "
        "FROM information_schema.columns WHERE table_name = 'documentos' ORDER BY ordinal_position",
    )
    restricciones = await consultar(
        esquema,
        "SELECT conname, pg_get_constraintdef(oid) AS definicion FROM pg_constraint "
        "WHERE conrelid = 'documentos'::regclass ORDER BY conname",
    )
    indices = await consultar(
        esquema, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'documentos' ORDER BY indexname"
    )
    return {
        "columnas": [tuple(c.values()) for c in columnas],
        "restricciones": [tuple(r.values()) for r in restricciones],
        "indices": [tuple(i.values()) for i in indices],
    }


@pytest_asyncio.fixture
async def esquema_nuevo(url_pg_aislado):
    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    await ejecutar(esquema, generar_ddl())
    return esquema


@pytest_asyncio.fixture
async def esquema_4a(url_pg_aislado):
    esquema = await _crear_base_con_prerequisitos(url_pg_aislado)
    await ejecutar(esquema, leer(SQL_ETAPA_4A))
    return esquema


# ============================================ Los archivos SQL ============================================

def test_la_migracion_0002_no_abre_transacciones_y_documenta_su_naturaleza():
    texto = SQL_ACTUALIZACION.read_text(encoding="utf-8")
    cabecera = texto.split("ALTER TABLE")[0]
    assert "Migración automática" in cabecera
    assert "Sin IF NOT EXISTS" in cabecera
    assert not any(linea.strip() in ("BEGIN;", "COMMIT;") for linea in texto.splitlines())


async def test_la_actualizacion_deja_el_mismo_esquema_que_la_instalacion_nueva(esquema_nuevo, esquema_4a):
    antes = await descripcion_del_esquema(esquema_4a)
    assert COLUMNAS_NUEVAS.isdisjoint({c[0] for c in antes["columnas"]})

    await ejecutar(esquema_4a, leer(SQL_ACTUALIZACION))

    nuevo, actualizado = await descripcion_del_esquema(esquema_nuevo), await descripcion_del_esquema(esquema_4a)
    assert COLUMNAS_NUEVAS <= {c[0] for c in actualizado["columnas"]}
    assert actualizado["columnas"] == nuevo["columnas"]  # tipos, nulabilidad, valores por defecto y orden
    assert actualizado["restricciones"] == nuevo["restricciones"]  # nombres y definiciones, una a una
    assert actualizado["indices"] == nuevo["indices"] == antes["indices"]  # los índices de reserva no cambian
    assert any(n == "ck_documentos_analisis_consistente" for n, _ in actualizado["restricciones"])


async def test_la_actualizacion_conserva_las_filas_existentes_con_valores_por_defecto(esquema_4a):
    conexion = await esquema_4a.conectar()
    try:
        documento_id = await esquema_4a.insertar(conexion)
    finally:
        await conexion.close()
    await ejecutar(esquema_4a, leer(SQL_ACTUALIZACION))
    (fila,) = await consultar(esquema_4a, "SELECT * FROM documentos WHERE id = $1", documento_id)
    assert fila["etapa_actual"] == "RESERVADO"
    assert fila["fragmentos_procesados"] == 0
    assert fila["fragmentos_total"] is None
    assert fila["analisis"] is None
    assert json.loads(fila["advertencias"]) == []
    assert fila["vector_publicacion_intentos"] == 0
    assert fila["vector_escritura_intentada_en"] is None
    assert fila["vector_publicado_en"] is None
    assert fila["estado_procesamiento"] == "EN_PROCESO"
    assert fila["reserva_activa"] is True  # lo anterior intacto


async def test_la_actualizacion_es_atomica_y_no_se_puede_aplicar_dos_veces(esquema_4a):
    await ejecutar(esquema_4a, leer(SQL_ACTUALIZACION))
    valor_leer = leer(SQL_ACTUALIZACION)
    with pytest.raises(asyncpg.DuplicateColumnError):
        await ejecutar(esquema_4a, valor_leer)


async def test_si_la_actualizacion_falla_a_mitad_no_deja_columnas_a_medias(esquema_4a):
    # Una restricción homónima previa hace fallar el segundo ALTER, DESPUÉS de añadir las columnas.
    await ejecutar(esquema_4a, "ALTER TABLE documentos ADD CONSTRAINT ck_documentos_etapa_valida CHECK (true)")
    valor_leer_2 = leer(SQL_ACTUALIZACION)
    with pytest.raises(asyncpg.DuplicateObjectError):
        await ejecutar(esquema_4a, valor_leer_2)
    columnas = {c["column_name"] for c in await consultar(
        esquema_4a, "SELECT column_name FROM information_schema.columns WHERE table_name = 'documentos'")}
    assert COLUMNAS_NUEVAS.isdisjoint(columnas)  # la transacción deshizo todo


# ============================================ Restricciones de la propia BD ============================================

ESTRUCTURA = {"resultado": "OBSERVADO", "motivos": ["x"], "version_catalogo": "v", "gri": [], "sanciones": [], "advertencias": []}


def completado(**cambios) -> dict:
    ahora = datetime.now(timezone.utc)
    valores = dict(
        estado_procesamiento="COMPLETADO", resultado_analisis="OBSERVADO", completado_en=ahora,
        original_almacenado_en=ahora, almacenamiento_intentado_en=ahora, vector_escritura_intentada_en=ahora,
        analisis=json.dumps(ESTRUCTURA),
    )
    valores.update(cambios)
    return valores


async def insertar_esperando(esquema, restriccion: str | None, **valores):
    conexion = await esquema.conectar()
    try:
        if restriccion is None:
            return await esquema.insertar(conexion, **valores)
        with pytest.raises(asyncpg.CheckViolationError) as capturado:
            await esquema.insertar(conexion, **valores)
        assert capturado.value.constraint_name == restriccion
    finally:
        await conexion.close()


async def test_un_documento_completado_con_analisis_consistente_se_acepta(esquema_nuevo):
    await insertar_esperando(esquema_nuevo, None, anio=2021, **completado())
    await insertar_esperando(esquema_nuevo, None, anio=2022, **completado(
        resultado_analisis="CON_HALLAZGOS", analisis=json.dumps({**ESTRUCTURA, "resultado": "CON_HALLAZGOS"})
    ))
    # En proceso: el análisis puede existir antes de completar (resultado_analisis sigue nulo).
    await insertar_esperando(esquema_nuevo, None, anio=2023, analisis=json.dumps(ESTRUCTURA), original_almacenado_en=datetime.now(timezone.utc))


@pytest.mark.parametrize(
    ("cambios", "restriccion"),
    [
        ({"analisis": json.dumps({**ESTRUCTURA, "resultado": "CON_HALLAZGOS"})}, "ck_documentos_analisis_consistente"),
        ({"resultado_analisis": "CON_HALLAZGOS"}, "ck_documentos_analisis_consistente"),
        ({"analisis": json.dumps({k: v for k, v in ESTRUCTURA.items() if k != "gri"})}, "ck_documentos_analisis_estructura"),
        ({"analisis": json.dumps({**ESTRUCTURA, "sanciones": {}})}, "ck_documentos_analisis_estructura"),
        ({"analisis": json.dumps({**ESTRUCTURA, "version_catalogo": ""})}, "ck_documentos_analisis_estructura"),
        ({"analisis": json.dumps(["no", "es", "objeto"])}, "ck_documentos_analisis_estructura"),
        ({"vector_escritura_intentada_en": None}, "ck_documentos_vector_publicado_con_intento"),
    ],
)
async def test_la_bd_rechaza_un_completado_con_analisis_inconsistente_o_publicacion_sin_intento(esquema_nuevo, cambios, restriccion):
    valores = completado(vector_publicado_en=datetime.now(timezone.utc))
    valores.update(cambios)
    await insertar_esperando(esquema_nuevo, restriccion, **valores)


@pytest.mark.parametrize(
    ("cambios", "restriccion"),
    [
        # EN_PROCESO: el análisis existe antes de completar y la clasificación sigue nula.
        ({"analisis": json.dumps({**ESTRUCTURA, "resultado": "OTRO"})}, "ck_documentos_analisis_estructura"),
        ({"analisis": json.dumps({k: v for k, v in ESTRUCTURA.items() if k != "resultado"})}, "ck_documentos_analisis_estructura"),
        ({"analisis": json.dumps({k: v for k, v in ESTRUCTURA.items() if k != "motivos"})}, "ck_documentos_analisis_estructura"),
        ({"etapa_actual": "OTRA"}, "ck_documentos_etapa_valida"),
        ({"fragmentos_procesados": 5, "fragmentos_total": 3}, "ck_documentos_fragmentos_progreso"),
        ({"fragmentos_procesados": -1}, "ck_documentos_fragmentos_progreso"),
        ({"fragmentos_total": -1}, "ck_documentos_fragmentos_progreso"),
        ({"vector_publicacion_intentos": -1}, "ck_documentos_publicacion_intentos"),
        (
            {"vector_publicado_en": datetime.now(timezone.utc), "vector_escritura_intentada_en": datetime.now(timezone.utc)},
            "ck_documentos_vector_publicado_solo_completado",
        ),
        ({"advertencias": "{}"}, "ck_documentos_advertencias_arreglo"),
        (
            {
                "estado_procesamiento": "FALLIDO", "motivo_fallo": "X_ERROR", "fallido_en": datetime.now(timezone.utc),
                "analisis": json.dumps(ESTRUCTURA),
            },
            "ck_documentos_sin_analisis_si_fallido",
        ),
    ],
)
async def test_la_bd_rechaza_progreso_y_estados_incoherentes(esquema_nuevo, cambios, restriccion):
    await insertar_esperando(esquema_nuevo, restriccion, **cambios)


async def test_un_fallido_sin_analisis_y_progreso_valido_se_acepta(esquema_nuevo):
    ahora = datetime.now(timezone.utc)
    await insertar_esperando(
        esquema_nuevo, None, estado_procesamiento="FALLIDO", motivo_fallo="X_ERROR", fallido_en=ahora,
        etapa_actual="INDEXANDO", fragmentos_procesados=2, fragmentos_total=5, advertencias=json.dumps([{"codigo": "A"}]),
    )


# ============================================ El arranque no crea la tabla ============================================

async def test_el_arranque_no_crea_documentos_ni_sus_tipos_aunque_el_modelo_este_cargado(url_pg_aislado):
    from app.database import Base
    from app.services.ingesta import coordinador  # noqa: F401  (registra Documento en los metadatos)
    from scripts import crear_tablas

    assert "documentos" in Base.metadata.tables  # SÍ está cargado: el riesgo es real
    assert "documentos" not in {t.name for t in crear_tablas.tablas_a_crear()}

    base = f"arranque_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(url_pg_aislado.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await admin.execute(f'CREATE DATABASE "{base}"')
    finally:
        await admin.close()
    url = url_pg_aislado.rsplit("/", 1)[0] + f"/{base}"
    motor = create_async_engine(url)
    try:
        async with motor.begin() as conexion:
            await conexion.run_sync(crear_tablas._crear_permitidas)
            await conexion.run_sync(crear_tablas._crear_permitidas)  # idempotente
    finally:
        await motor.dispose()
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    conexion = await asyncpg.connect(dsn)
    try:
        tablas = {r["tablename"] for r in await conexion.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        tipos = {r["typname"] for r in await conexion.fetch("SELECT typname FROM pg_type WHERE typtype = 'e'")}
    finally:
        await conexion.close()
    assert {"usuarios", "empresas", "auditoria"} <= tablas
    assert "documentos" not in tablas
    assert {"estado_procesamiento", "resultado_analisis", "estado_compensacion", "tipo_documento"}.isdisjoint(tipos)


# ============================================ Validación del servicio (sin PostgreSQL) ============================================

@pytest.mark.parametrize(
    "analisis",
    [
        None, [], "texto", {},
        {**ESTRUCTURA, "resultado": "OTRO"},
        {**ESTRUCTURA, "version_catalogo": "  "},
        {**ESTRUCTURA, "gri": "no"},
        {**ESTRUCTURA, "advertencias": None},
        {**ESTRUCTURA, "puntaje": float("nan")},
        {**ESTRUCTURA, "x": object()},
    ],
)
def test_validar_analisis_rechaza_estructuras_invalidas_sin_repetir_su_contenido(analisis):
    with pytest.raises(BusinessValidationError) as capturado:
        servicio.validar_analisis(analisis)
    assert capturado.value.code == "INVALID_ANALYSIS"
    assert "texto" not in capturado.value.message


def test_validar_analisis_acepta_la_forma_de_a_dict():
    from app.services.ingesta.analisis_ingesta import analizar_texto

    forma = analizar_texto("Ver GRI 305-1. Se impuso una multa de S/ 900.").a_dict()
    assert servicio.validar_analisis(forma) is forma
    assert json.loads(json.dumps(forma)) == forma  # JSON estricto de ida y vuelta


def test_validar_analisis_rechaza_los_excesivamente_grandes(monkeypatch):
    monkeypatch.setattr(servicio, "MAXIMO_BYTES_ANALISIS", 100)
    with pytest.raises(BusinessValidationError) as capturado:
        servicio.validar_analisis({**ESTRUCTURA, "motivos": ["x" * 500]})
    assert capturado.value.details == {"campos": ["demasiado_grande"]}


async def preparar(db, *, con_analisis, resultado="OBSERVADO"):
    """Documento EN_PROCESO listo para finalizar (original e intento vectorial registrados)."""
    usuario, empresa = await crear_usuario(db), await crear_empresa(db)
    datos = b"# Memoria\n"
    documento = await servicio.reservar_documento(
        db, metadatos=metadatos(empresa), nombre_archivo="m.md", sha256=sha256_de(datos), tamano_bytes=len(datos),
        usuario_id=usuario.id, ambiente="development",
    )
    from sqlalchemy import update

    from app.models.documento_ingesta import Documento

    await db.execute(update(Documento).where(Documento.id == documento.id).values(
        original_almacenado_en=datetime.now(timezone.utc), almacenamiento_intentado_en=datetime.now(timezone.utc)))
    await db.commit()
    await servicio.registrar_intento_vectorial(db, documento.id, token=documento.ejecucion_token)
    if con_analisis:
        await servicio.persistir_analisis(
            db, documento.id, token=documento.ejecucion_token, analisis={**ESTRUCTURA, "resultado": resultado}
        )
    return documento, usuario


async def test_finalizar_exigiendo_analisis_persistido_y_consistente():
    db = SesionSQLite()
    try:
        documento, _ = await preparar(db, con_analisis=False)
        with pytest.raises(ConflictError) as capturado:
            await servicio.publicar_documento(
                db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True,
                resultado_analisis=ResultadoAnalisis.OBSERVADO, exigir_analisis_persistido=True,
            )
        assert "analisis_persistido" in capturado.value.details["faltantes"]
        assert (await servicio._cargar(db, documento.id)).estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    finally:
        db.cerrar()


async def test_finalizar_con_una_clasificacion_distinta_del_analisis_persistido_se_rechaza():
    db = SesionSQLite()
    try:
        documento, _ = await preparar(db, con_analisis=True, resultado="OBSERVADO")
        with pytest.raises(ConflictError) as capturado:
            await servicio.publicar_documento(
                db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True,
                resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS, exigir_analisis_persistido=True,
            )
        assert capturado.value.details["faltantes"] == ["analisis_consistente"]
        cargado = await servicio._cargar(db, documento.id)
        assert cargado.resultado_analisis is None
        assert cargado.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    finally:
        db.cerrar()


async def test_finalizar_consistente_audita_el_exito_en_la_misma_transaccion():
    db = SesionSQLite()
    try:
        documento, usuario = await preparar(db, con_analisis=True, resultado="CON_HALLAZGOS")
        completado_ = await servicio.publicar_documento(
            db, documento.id, token=documento.ejecucion_token, indexacion_confirmada=True,
            resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS, exigir_analisis_persistido=True,
        )
        assert completado_.analisis["resultado"] == completado_.resultado_analisis.value
        assert completado_.vector_publicado_en is None
        assert completado_.publicacion_vectorial_pendiente
        assert completado_.etapa_actual == "PUBLICANDO"
        filas = db.sync.execute(__import__("sqlalchemy").text("SELECT detalle, usuario_id FROM auditoria")).all()
        assert len(filas) == 1
        assert "Ingesta completada" in filas[0][0]
        assert "publicacion_vectorial=pendiente" in filas[0][0]
    finally:
        db.cerrar()


async def test_persistir_analisis_exige_ser_el_ejecutor_y_haber_indexado():
    db = SesionSQLite()
    try:
        documento, _ = await preparar(db, con_analisis=False)
        token_ajeno = uuid.uuid4()
        with pytest.raises(ConflictError):
            await servicio.persistir_analisis(db, documento.id, token=token_ajeno, analisis=ESTRUCTURA)  # no es el dueño
        with pytest.raises(BusinessValidationError):
            await servicio.persistir_analisis(db, documento.id, token=documento.ejecucion_token, analisis={"resultado": "x"})
        assert (await servicio._cargar(db, documento.id)).analisis is None
    finally:
        db.cerrar()


async def test_el_progreso_solo_lo_escribe_el_ejecutor_y_es_coherente():
    db = SesionSQLite()
    try:
        documento, _ = await preparar(db, con_analisis=False)
        token = documento.ejecucion_token
        await servicio.registrar_progreso(db, documento.id, token=token, fragmentos_total=4, fragmentos_procesados=2)
        valor_uuid_uuid4 = uuid.uuid4()
        with pytest.raises(ConflictError):
            await servicio.registrar_progreso(db, documento.id, token=valor_uuid_uuid4, fragmentos_procesados=3)
        with pytest.raises(ValueError):
            await servicio.registrar_progreso(db, documento.id, token=token, fragmentos_procesados=9)  # > total
        with pytest.raises(ValueError):
            await servicio.registrar_progreso(db, documento.id, token=token, fragmentos_procesados=True)
        progreso = await servicio.obtener_progreso(db, documento.id)
        assert (progreso.fragmentos_procesados, progreso.fragmentos_total, progreso.finalizado) == (2, 4, False)
    finally:
        db.cerrar()

"""Etapa 4A: el SQL que produce `scripts.generar_ddl_documentos`, EJECUTADO tal cual en un
PostgreSQL REAL, AISLADO y TEMPORAL (el clúster de `conftest.py`: `initdb` local en
127.0.0.1, sin `DATABASE_URL` ni bases compartidas; se destruye al terminar).

Cada prueba crea su propia base vacía dentro de ese clúster, le aplica los prerrequisitos
(`empresas`, `usuarios` y el tipo `sector_empresa`, que ya existen en los ambientes reales)
y después el DDL generado. No se usa `create_all` para la tabla `documentos`: se prueba el
archivo `.sql`. Si PostgreSQL no está disponible estas pruebas se OMITEN (`pytest -rs`) y NO
se dan por ejecutadas.

No prueba MinIO, ni la aplicación del esquema en ningún ambiente compartido.
"""
import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Empresa, RolUsuario, SectorEmpresa, Usuario
from app.models.documento_ingesta import Documento
from scripts.generar_ddl_documentos import generar_ddl

TIPOS_NUEVOS = {
    "tipo_documento": {"MEMORIA_ANUAL", "REPORTE_SOSTENIBILIDAD_GRI"},
    "estado_procesamiento": {"EN_PROCESO", "COMPLETADO", "FALLIDO"},
    "resultado_analisis": {"CON_HALLAZGOS", "OBSERVADO"},
    "estado_compensacion": {"NINGUNA", "PENDIENTE", "COMPLETADA"},
}


def _dsn(url_pg_aislado: str, base: str) -> str:
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", url_pg_aislado).rsplit("/", 1)[0] + f"/{base}"


class Esquema:
    def __init__(self, url_pg: str, base: str, empresas: list, usuario):
        self.url_pg, self.base, self.empresas, self.usuario = url_pg, base, empresas, usuario

    @property
    def dsn(self) -> str:
        return _dsn(self.url_pg, self.base)

    async def conectar(self) -> asyncpg.Connection:
        return await asyncpg.connect(self.dsn)

    async def insertar(self, conexion, **cambios):
        valores = dict(
            id=uuid.uuid4(), ambiente="development", empresa_id=self.empresas[0], anio=2025,
            tipo="MEMORIA_ANUAL", sector="MINERIA", nombre_archivo="memoria.md",
            sha256=uuid.uuid4().hex * 2, tamano_bytes=10, usuario_id=self.usuario,
            clave_original=f"development/documentos/{uuid.uuid4()}/original.md",
            ejecucion_token=uuid.uuid4(),
            ejecucion_vigente_hasta=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        valores.update(cambios)
        columnas = ", ".join(valores)
        marcadores = ", ".join(f"${n}" for n in range(1, len(valores) + 1))
        await conexion.execute(f"INSERT INTO documentos ({columnas}) VALUES ({marcadores})", *valores.values())
        return valores["id"]


async def _crear_base_con_prerequisitos(url_pg_aislado: str) -> Esquema:
    base = f"ddl_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_dsn(url_pg_aislado, "postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{base}"')
    finally:
        await admin.close()
    url = url_pg_aislado.rsplit("/", 1)[0] + f"/{base}"
    motor = create_async_engine(url)
    try:
        async with motor.begin() as conexion:  # solo los prerrequisitos que ya existen en los ambientes
            await conexion.run_sync(Base.metadata.create_all, tables=[Usuario.__table__, Empresa.__table__])
            # `create_all(tables=...)` crea los enumerados de TODO el MetaData cargado, también los de
            # `documentos`; en los ambientes reales esos tipos no existen. Se retiran para que el DDL
            # generado los cree como lo haría en un ambiente real.
            await conexion.exec_driver_sql(
                "DROP TYPE IF EXISTS " + ", ".join(TIPOS_NUEVOS)
            )
        fabrica = async_sessionmaker(motor, expire_on_commit=False)
        async with fabrica() as db:
            usuario = Usuario(nombre="Super", correo="super@pruebas.invalid", password_hash="h",
                              rol=RolUsuario.SUPERADMIN, habilitado=True)
            empresas = [Empresa(nombre=f"Empresa {n}", sector=SectorEmpresa.MINERIA, activa=True) for n in range(8)]
            db.add_all([usuario, *empresas])
            await db.commit()
            return Esquema(url_pg_aislado, base, [e.id for e in empresas], usuario.id)
    finally:
        await motor.dispose()


@pytest_asyncio.fixture
async def esquema(url_pg_aislado):
    entorno = await _crear_base_con_prerequisitos(url_pg_aislado)
    conexion = await entorno.conectar()
    try:
        await conexion.execute(generar_ddl())  # EL archivo .sql, tal cual
    finally:
        await conexion.close()
    return entorno


async def consultar(esquema, sql, *args):
    conexion = await esquema.conectar()
    try:
        return await conexion.fetch(sql, *args)
    finally:
        await conexion.close()


async def rechazo(esquema, excepcion, restriccion=None, **cambios):
    conexion = await esquema.conectar()
    try:
        with pytest.raises(excepcion) as capturado:
            await esquema.insertar(conexion, **cambios)
        if restriccion is not None:
            # Una fila puede romper varias restricciones a la vez; PostgreSQL informa la primera que evalúa.
            aceptadas = {restriccion} if isinstance(restriccion, str) else restriccion
            assert capturado.value.constraint_name in aceptadas
    finally:
        await conexion.close()


# --- Prerrequisitos y objetos creados -------------------------------------------------------------

async def test_sin_los_prerrequisitos_el_sql_falla_y_con_ellos_se_aplica(url_pg_aislado):
    base = f"ddl_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_dsn(url_pg_aislado, "postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{base}"')
    finally:
        await admin.close()
    vacia = await asyncpg.connect(_dsn(url_pg_aislado, base))
    try:
        with pytest.raises(asyncpg.PostgresError):  # no existen empresas, usuarios ni sector_empresa
            await vacia.execute(generar_ddl())
    finally:
        await vacia.close()


async def test_el_sql_crea_los_cuatro_enumerados_nuevos_y_no_toca_sector_empresa(esquema):
    filas = await consultar(
        esquema,
        "SELECT t.typname, array_agg(e.enumlabel::text) AS valores FROM pg_type t "
        "JOIN pg_enum e ON e.enumtypid = t.oid GROUP BY t.typname",
    )
    existentes = {fila["typname"]: set(fila["valores"]) for fila in filas}
    for nombre, valores in TIPOS_NUEVOS.items():
        assert existentes[nombre] == valores
    assert existentes["sector_empresa"] == {sector.name for sector in SectorEmpresa}  # el de los prerrequisitos


def test_el_sql_no_crea_otra_vez_los_tipos_ni_tablas_de_los_prerrequisitos():
    ddl = generar_ddl()
    assert "CREATE TYPE sector_empresa" not in ddl and "CREATE TABLE empresas" not in ddl
    assert ddl.count("CREATE TABLE documentos") == 1 and "CREATE UNIQUE INDEX uq_documentos_sha256_activo" in ddl


async def test_claves_foraneas_hacia_empresas_y_usuarios(esquema):
    filas = await consultar(
        esquema,
        "SELECT a.attname AS columna, c.confrelid::regclass::text AS destino FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
        "WHERE c.conrelid = 'documentos'::regclass AND c.contype = 'f'",
    )
    assert {(f["columna"], f["destino"]) for f in filas} == {("empresa_id", "empresas"), ("usuario_id", "usuarios")}
    await rechazo(esquema, asyncpg.ForeignKeyViolationError, empresa_id=uuid.uuid4())
    await rechazo(esquema, asyncpg.ForeignKeyViolationError, usuario_id=uuid.uuid4())


async def test_las_restricciones_check_del_modelo_existen_con_sus_nombres(esquema):
    filas = await consultar(
        esquema, "SELECT conname FROM pg_constraint WHERE conrelid = 'documentos'::regclass AND contype = 'c'"
    )
    del_modelo = {c.name for c in Documento.__table__.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert del_modelo and {f["conname"] for f in filas} == del_modelo


async def test_indices_unicos_parciales_de_reserva_y_clave_unica(esquema):
    filas = await consultar(esquema, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'documentos'")
    definiciones = {f["indexname"]: f["indexdef"] for f in filas}
    sha = definiciones["uq_documentos_sha256_activo"]
    empresa = definiciones["uq_documentos_empresa_anio_tipo_activo"]
    for definicion in (sha, empresa):
        assert definicion.startswith("CREATE UNIQUE INDEX") and "WHERE reserva_activa" in definicion
    assert "(ambiente, sha256)" in sha and "(ambiente, empresa_id, anio, tipo)" in empresa
    assert any("clave_original" in d and "UNIQUE" in d.upper() for d in definiciones.values())


# --- CHECK: cada invariante se hace cumplir en la base -----------------------------------------------

@pytest.mark.parametrize("cambios, restriccion", [
    (dict(anio=1999), "ck_documentos_anio_minimo"),
    (dict(sha256="a" * 63), "ck_documentos_sha256_longitud"),
    (dict(tamano_bytes=0), "ck_documentos_tamano_positivo"),
    (dict(nombre_archivo="   "), "ck_documentos_nombre_no_vacio"),
    (dict(estado_procesamiento="COMPLETADO"),
     {"ck_documentos_resultado_solo_si_completado", "ck_documentos_completado_en",
      "ck_documentos_completado_con_original"}),
    (dict(resultado_analisis="OBSERVADO"), "ck_documentos_resultado_solo_si_completado"),
    (dict(estado_procesamiento="FALLIDO"), "ck_documentos_fallido_con_motivo"),
    (dict(estado_compensacion="PENDIENTE"), "ck_documentos_compensacion_solo_si_fallido"),
    (dict(reserva_activa=False), "ck_documentos_reserva_solo_liberada_si_limpio"),
    (dict(compensacion_intentos=-1), "ck_documentos_intentos_no_negativos"),
])
async def test_check_rechaza_filas_que_rompen_una_invariante(esquema, cambios, restriccion):
    await rechazo(esquema, asyncpg.CheckViolationError, restriccion, **cambios)


async def test_una_reserva_solo_se_libera_en_un_fallido_sin_limpieza_pendiente(esquema):
    ahora = datetime.now(timezone.utc)
    fallido = dict(estado_procesamiento="FALLIDO", motivo_fallo="STORAGE_ERROR", fallido_en=ahora)
    await rechazo(esquema, asyncpg.CheckViolationError, "ck_documentos_reserva_solo_liberada_si_limpio",
                  reserva_activa=False, estado_compensacion="PENDIENTE", **fallido)
    conexion = await esquema.conectar()
    try:
        await esquema.insertar(conexion, reserva_activa=False, estado_compensacion="COMPLETADA", **fallido)
        await esquema.insertar(conexion, reserva_activa=False, estado_compensacion="NINGUNA", **fallido)
    finally:
        await conexion.close()


async def test_un_completado_exige_resultado_fecha_y_original(esquema):
    ahora = datetime.now(timezone.utc)
    await rechazo(esquema, asyncpg.CheckViolationError, "ck_documentos_completado_con_original",
                  estado_procesamiento="COMPLETADO", resultado_analisis="OBSERVADO", completado_en=ahora)
    conexion = await esquema.conectar()
    try:
        await esquema.insertar(conexion, estado_procesamiento="COMPLETADO", resultado_analisis="OBSERVADO",
                               completado_en=ahora, original_almacenado_en=ahora)
    finally:
        await conexion.close()


# --- Exclusión mutua de reservas, también concurrente ----------------------------------------------------------

async def test_reservas_duplicadas_por_sha256_o_por_empresa_anio_tipo_se_rechazan_con_su_indice(esquema):
    conexion = await esquema.conectar()
    try:
        await esquema.insertar(conexion, sha256="a" * 64, anio=2025)
    finally:
        await conexion.close()
    await rechazo(esquema, asyncpg.UniqueViolationError, "uq_documentos_sha256_activo",
                  sha256="a" * 64, anio=2024)  # mismo contenido, otro año
    await rechazo(esquema, asyncpg.UniqueViolationError, "uq_documentos_empresa_anio_tipo_activo",
                  anio=2025)  # misma empresa/año/tipo, otro contenido
    conexion = await esquema.conectar()
    try:
        await esquema.insertar(conexion, sha256="b" * 64, anio=2025, tipo="REPORTE_SOSTENIBILIDAD_GRI")  # otro tipo
        await esquema.insertar(conexion, sha256="c" * 64, anio=2025, empresa_id=esquema.empresas[1])  # otra empresa
        await esquema.insertar(conexion, sha256="a" * 64, anio=2025, ambiente="qa")  # otro ambiente
    finally:
        await conexion.close()


async def _insertar_concurrente(esquema, n: int, variacion) -> list:
    conexiones = [await esquema.conectar() for _ in range(n)]
    arranque = asyncio.Event()

    async def intentar(indice, conexion):
        await arranque.wait()
        try:
            await esquema.insertar(conexion, **variacion(indice))
            return "ok"
        except asyncpg.UniqueViolationError as exc:
            return exc.constraint_name

    try:
        tareas = [asyncio.create_task(intentar(i, c)) for i, c in enumerate(conexiones)]
        await asyncio.sleep(0)
        arranque.set()
        return await asyncio.gather(*tareas)
    finally:
        for conexion in conexiones:
            await conexion.close()


async def test_ocho_reservas_concurrentes_con_el_mismo_sha256_solo_admiten_una(esquema):
    resultados = await _insertar_concurrente(
        esquema, 8, lambda i: dict(sha256="d" * 64, anio=2000 + i)  # distinto año: solo choca el contenido
    )
    assert resultados.count("ok") == 1
    assert resultados.count("uq_documentos_sha256_activo") == 7
    assert (await consultar(esquema, "SELECT count(*) AS n FROM documentos"))[0]["n"] == 1


async def test_ocho_reservas_concurrentes_con_la_misma_empresa_anio_tipo_solo_admiten_una(esquema):
    resultados = await _insertar_concurrente(
        esquema, 8, lambda i: dict(sha256=f"{i:x}" * 64, anio=2025)  # distinto contenido: solo choca la combinación
    )
    assert resultados.count("ok") == 1
    assert resultados.count("uq_documentos_empresa_anio_tipo_activo") == 7
    assert (await consultar(esquema, "SELECT count(*) AS n FROM documentos"))[0]["n"] == 1


async def test_tras_liberar_la_reserva_se_puede_volver_a_reservar_y_el_liberado_conserva_su_fila(esquema):
    conexion = await esquema.conectar()
    try:
        id_inicial = await esquema.insertar(conexion, sha256="e" * 64)
        await conexion.execute(
            "UPDATE documentos SET estado_procesamiento = 'FALLIDO', motivo_fallo = 'X_ERR', fallido_en = now(), "
            "estado_compensacion = 'COMPLETADA', reserva_activa = false WHERE id = $1", id_inicial,
        )
        await esquema.insertar(conexion, sha256="e" * 64)
        assert (await conexion.fetchval("SELECT count(*) FROM documentos")) == 2
    finally:
        await conexion.close()

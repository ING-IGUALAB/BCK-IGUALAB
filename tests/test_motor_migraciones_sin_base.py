"""Lógica del motor de migraciones con una CONEXIÓN SIMULADA (sin PostgreSQL): corre también en CI. Prueba las decisiones del motor
—orden, bloqueo, adopción verificada, discrepancias, checksum, pgvector, rollback, errores— pero NO demuestra el comportamiento de
PostgreSQL (transacciones, bloqueos reales, DDL): eso lo hacen `tests/services/test_migraciones.py` y `test_migraciones_firmas.py`
contra bases reales y aisladas."""
import asyncio
import contextlib
import copy

import asyncpg
import pytest

from app.migraciones import motor
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL, ESQUEMA_VECTORIAL
from app.migraciones.motor import ErrorMigracion, EsquemaBase, Migracion, diferencias, migrar


def filas_de(firma: dict) -> tuple[list, list, list]:
    return (
        [{"nombre": n, "tipo": t, "no_nulo": nn} for n, t, nn in firma["columnas"]],
        [{"nombre": n} for n in firma["restricciones"]],
        [{"nombre": n, "unico": u, "parcial": p} for n, u, p in firma["indices"]],
    )


class ConexionSimulada:
    """Modela lo mínimo que usa el motor: bloqueo asesor, extensión, objetos con firma, registro y transacciones con rollback."""

    def __init__(self, esquema: EsquemaBase, objetos: dict | None = None, registro: list | None = None, *,
                 extension: bool = True, puede_crear_extension: bool = True, prerrequisitos: bool = True, candado_ocupado: int = 0):
        self.objetos = copy.deepcopy(objetos or {})
        self.registro = copy.deepcopy(registro or [])
        self.extension, self.puede_crear, self.prerrequisitos = extension, puede_crear_extension, prerrequisitos
        self.candado_ocupado, self.candado = candado_ocupado, False
        self.efectos = {m.sql(): m for m in esquema.migraciones}
        self.firmas = motor.firmas_versionadas().get(esquema.nombre, {}) if not esquema.firmas else esquema.firmas
        self.ejecutadas: list[int] = []
        self.fallar_en: set[int] = set()
        self.cerrada = False
        self.fallo_al_liberar = False
        self.fallo_en_registro = False

    # --- consultas ----------------------------------------------------------------------------------
    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            if self.candado_ocupado > 0:
                self.candado_ocupado -= 1
                return False
            self.candado = True
            return True
        if "pg_advisory_unlock" in sql:
            self.candado = False
            return True
        if "pg_extension" in sql:
            return 1 if self.extension else None
        if "to_regclass" in sql:
            return self.prerrequisitos or args[0] not in ("public.usuarios", "public.empresas")
        if "pg_type" in sql:
            return self.prerrequisitos
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "FROM igualab_migraciones" in sql:
            return [dict(f) for f in sorted(self.registro, key=lambda f: f["version"])]
        firma = self.objetos.get(args[0])
        if firma is None:
            return []
        columnas, restricciones, indices = filas_de(firma)
        if "pg_attribute" in sql:
            return columnas
        return restricciones if "pg_constraint" in sql else indices

    async def execute(self, sql, *args):
        if "pg_advisory_unlock" in sql:
            if self.fallo_al_liberar:
                raise asyncpg.PostgresError("no se pudo liberar")
            self.candado = False
            return
        if sql.strip().startswith("CREATE TABLE IF NOT EXISTS igualab_migraciones"):
            return
        if sql == "CREATE EXTENSION IF NOT EXISTS vector":
            if not self.puede_crear:
                raise asyncpg.InsufficientPrivilegeError("permiso denegado")
            self.extension = True
            return
        if sql.startswith("INSERT INTO igualab_migraciones"):
            if self.fallo_en_registro:
                raise asyncpg.PostgresError("registro")
            self.registro.append({"version": args[0], "nombre": args[1], "checksum": args[2], "origen": args[3]})
            return
        m = self.efectos[sql]
        if m.version in self.fallar_en:
            raise asyncpg.UndefinedTableError("falla simulada")
        self.ejecutadas.append(m.version)
        for objeto in m.objetos:
            self.objetos[objeto] = copy.deepcopy(self.firmas[objeto][m.version])

    @contextlib.asynccontextmanager
    async def transaction(self):
        copia = (copy.deepcopy(self.objetos), copy.deepcopy(self.registro), list(self.ejecutadas))
        try:
            yield
        except BaseException:
            self.objetos, self.registro, self.ejecutadas = copia  # ROLLBACK
            raise

    async def close(self):
        self.cerrada = True


@pytest.fixture
def conectar(monkeypatch):
    def preparar(conexion: ConexionSimulada):
        async def falsa(url, base, timeout):
            return conexion

        monkeypatch.setattr(motor, "_conectar", falsa)
        return conexion

    return preparar


def firma(objeto: str, version: int, esquema=ESQUEMA_TRANSACCIONAL):
    return motor.firmas_versionadas()[esquema.nombre][objeto][version]


# ============================================ Instalación, repetición, adopción ============================================

async def test_instalacion_nueva_aplica_en_orden_registra_y_libera_el_bloqueo(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    resultado = await migrar("postgresql+asyncpg://u@h/db", ESQUEMA_TRANSACCIONAL)
    assert (resultado.aplicadas, resultado.adoptadas, resultado.previas) == ((1, 2, 3), (), ())
    assert c.ejecutadas == [1, 2, 3] and [f["origen"] for f in c.registro] == ["aplicada"] * 3
    assert [f["checksum"] for f in c.registro] == [m.checksum() for m in ESQUEMA_TRANSACCIONAL.migraciones]
    assert c.candado is False and c.cerrada


async def test_segundo_arranque_no_ejecuta_nada(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    await migrar("x", ESQUEMA_TRANSACCIONAL)
    c.ejecutadas.clear()
    resultado = await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert resultado.sin_cambios and resultado.previas == (1, 2, 3) and c.ejecutadas == []


async def test_adopta_un_esquema_existente_solo_si_coincide_con_la_firma(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, {"documentos": firma("documentos", 2), "operaciones_ingesta": firma("operaciones_ingesta", 3)}))
    resultado = await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert resultado.adoptadas == (1, 2, 3) and resultado.aplicadas == () and c.ejecutadas == []
    assert [f["origen"] for f in c.registro] == ["adoptada"] * 3


async def test_actualiza_desde_la_etapa_4a_adoptando_la_primera_y_aplicando_el_resto(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, {"documentos": firma("documentos", 1)}))
    resultado = await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert (resultado.adoptadas, resultado.aplicadas) == ((1,), (2, 3)) and c.ejecutadas == [2, 3]
    assert c.objetos["documentos"] == firma("documentos", 2)


@pytest.mark.parametrize("cambio", ["columna_extra", "columna_menos", "tipo", "indice_menos", "restriccion_menos", "indice_no_parcial"])
async def test_un_esquema_distinto_no_se_adopta_ni_se_modifica(conectar, cambio):
    actual = copy.deepcopy(firma("documentos", 2))
    if cambio == "columna_extra":
        actual["columnas"].append(["extra", "integer", False])
    elif cambio == "columna_menos":
        actual["columnas"].pop()
    elif cambio == "tipo":
        actual["columnas"][0][1] = "text"
    elif cambio == "indice_menos":
        actual["indices"].pop()
    elif cambio == "restriccion_menos":
        actual["restricciones"].pop()
    else:
        actual["indices"][-1][2] = not actual["indices"][-1][2]
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, {"documentos": actual}))
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and error.value.detalles["objeto"] == "documentos"
    assert error.value.detalles["diferencias"] and c.registro == [] and c.ejecutadas == [] and c.objetos["documentos"] == actual


async def test_una_migracion_a_medias_o_sin_objetos_conocidos(conectar):
    esquema = EsquemaBase("t", (Migracion(1, "dos", "0001_documentos_etapa_4a.sql", ("a", "b"), carpeta="transaccional"),),
                          firmas={"a": {1: firma("documentos", 2)}, "b": {1: firma("operaciones_ingesta", 3)}})
    conectar(ConexionSimulada(esquema, {"a": firma("documentos", 2)}))  # a en su estado, b ausente
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", esquema)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and "a medias" in error.value.mensaje
    assert error.value.detalles["objetos"] == {"a": "adoptar", "b": "aplicar"}


async def test_un_objeto_con_firma_desconocida_sin_firmas_versionadas_es_conflicto(conectar):
    esquema = EsquemaBase("t", (Migracion(1, "x", "0001_documentos_etapa_4a.sql", ("raro",), carpeta="transaccional"),))
    conectar(ConexionSimulada(esquema, {"raro": firma("documentos", 1)}))
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", esquema)
    assert error.value.detalles["diferencias"] == {}


async def test_una_migracion_sin_objetos_siempre_se_aplica(conectar):
    esquema = EsquemaBase("t", (Migracion(1, "sin_objetos", "0001_documentos_etapa_4a.sql", (), carpeta="transaccional"),))
    c = conectar(ConexionSimulada(esquema))
    assert (await migrar("x", esquema)).aplicadas == (1,) and c.ejecutadas == [1]


async def test_checksum_editado_y_objeto_registrado_ausente(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    await migrar("x", ESQUEMA_TRANSACCIONAL)
    c.registro[1]["checksum"] = "0" * 64
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_CHECKSUM_MISMATCH" and error.value.detalles == {"version": 2}
    c.registro[1]["checksum"] = ESQUEMA_TRANSACCIONAL.migraciones[1].checksum()
    del c.objetos["operaciones_ingesta"]
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_SCHEMA_MISMATCH" and error.value.detalles["objeto"] == "operaciones_ingesta"


async def test_versiones_de_una_aplicacion_mas_nueva_se_respetan(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    await migrar("x", ESQUEMA_TRANSACCIONAL)
    c.registro.append({"version": 9, "nombre": "futura", "checksum": "a" * 64, "origen": "aplicada"})
    resultado = await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert resultado.desconocidas == (9,) and resultado.sin_cambios


async def test_prerrequisitos_ausentes_no_crean_nada(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, prerrequisitos=False))
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_PREREQUISITE_MISSING"
    assert set(error.value.detalles["faltan"]) == {"tabla:usuarios", "tabla:empresas", "tipo:sector_empresa"}
    assert c.registro == [] and c.ejecutadas == [] and c.candado is False and c.cerrada


# ============================================ Fallos, rollback y bloqueo ============================================

async def test_un_fallo_deshace_la_migracion_y_conserva_las_anteriores(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    c.fallar_en = {2}
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_FAILED" and error.value.detalles["version"] == 2
    assert error.value.detalles["sqlstate"] == "42P01" and "se deshizo por completo" in error.value.mensaje
    assert [f["version"] for f in c.registro] == [1] and c.ejecutadas == [1]  # la 2 no dejó rastro; la 3 no corrió
    assert c.candado is False and c.cerrada


async def test_si_falla_el_registro_la_migracion_tambien_se_deshace(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    c.fallo_en_registro = True
    with pytest.raises(ErrorMigracion):
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert c.ejecutadas == [] and "documentos" not in c.objetos  # el DDL tampoco quedó


async def test_migracion_no_transaccional_se_aplica_sin_envoltura(conectar):
    esquema = EsquemaBase("t", (Migracion(1, "x", "0001_documentos_etapa_4a.sql", ("documentos",), transaccional=False, carpeta="transaccional"),),
                          firmas={"documentos": {1: firma("documentos", 1)}})
    c = conectar(ConexionSimulada(esquema))
    assert (await migrar("x", esquema)).aplicadas == (1,) and c.ejecutadas == [1]
    c2 = conectar(ConexionSimulada(esquema))
    c2.fallar_en = {1}
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", esquema)
    assert "no es transaccional" in error.value.mensaje


async def test_un_error_de_postgresql_fuera_de_una_migracion_se_convierte_en_error_controlado(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))

    async def explota(*args, **kwargs):
        raise asyncpg.PostgresError("detalle con datos")

    c.fetch = explota
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL)
    assert error.value.codigo == "MIGRATION_FAILED" and "detalle con datos" not in f"{error.value.mensaje} {error.value.detalles}"
    assert c.candado is False and c.cerrada


async def test_el_bloqueo_ocupado_se_espera_y_luego_procede(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, candado_ocupado=2))
    assert (await migrar("x", ESQUEMA_TRANSACCIONAL, espera_candado=5)).aplicadas == (1, 2, 3) and c.candado is False


async def test_si_el_bloqueo_no_se_consigue_a_tiempo_no_se_toca_nada(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, candado_ocupado=10**6))
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_TRANSACCIONAL, espera_candado=0.3)
    assert error.value.codigo == "MIGRATION_LOCK_TIMEOUT" and c.registro == [] and c.ejecutadas == [] and c.cerrada


async def test_un_fallo_al_liberar_el_bloqueo_no_oculta_el_resultado(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL))
    c.fallo_al_liberar = True
    assert (await migrar("x", ESQUEMA_TRANSACCIONAL)).aplicadas == (1, 2, 3) and c.cerrada  # cerrar libera el bloqueo igualmente


async def test_versiones_duplicadas_se_rechazan_antes_de_conectar(monkeypatch):
    async def no_conectar(*args, **kwargs):
        raise AssertionError("no debía conectar")

    monkeypatch.setattr(motor, "_conectar", no_conectar)
    m = ESQUEMA_TRANSACCIONAL.migraciones[0]
    with pytest.raises(ValueError):
        await migrar("x", EsquemaBase("t", (m, m)))


# ============================================ pgvector ============================================

async def test_pgvector_presente_no_intenta_crearlo(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_VECTORIAL, extension=True, puede_crear_extension=False))
    assert (await migrar("x", ESQUEMA_VECTORIAL)).aplicadas == (1, 2) and c.ejecutadas == [1, 2]


async def test_pgvector_ausente_se_crea_si_el_rol_puede_y_se_verifica(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_VECTORIAL, extension=False))
    assert (await migrar("x", ESQUEMA_VECTORIAL)).aplicadas == (1, 2) and c.extension is True


async def test_pgvector_ausente_sin_permiso_informa_el_requisito_y_no_crea_nada(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_VECTORIAL, extension=False, puede_crear_extension=False))
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_VECTORIAL)
    assert error.value.codigo == "PGVECTOR_REQUIRED" and error.value.detalles == {"sqlstate": "42501"}
    assert "CREATE EXTENSION vector" in error.value.mensaje and c.registro == [] and c.ejecutadas == [] and c.extension is False


async def test_si_el_create_extension_no_falla_pero_la_extension_no_aparece_no_se_finge(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_VECTORIAL, extension=False))

    async def silencioso(sql, *args):
        return None

    c.execute = silencioso  # «tuvo éxito» pero no creó nada
    with pytest.raises(ErrorMigracion) as error:
        await migrar("x", ESQUEMA_VECTORIAL)
    assert error.value.codigo == "PGVECTOR_REQUIRED" and error.value.detalles == {"sqlstate": None}


async def test_la_base_transaccional_no_comprueba_pgvector(conectar):
    c = conectar(ConexionSimulada(ESQUEMA_TRANSACCIONAL, extension=False, puede_crear_extension=False))
    assert (await migrar("x", ESQUEMA_TRANSACCIONAL)).aplicadas == (1, 2, 3) and c.extension is False


# ============================================ Conexión ============================================

@pytest.mark.parametrize("fallo", [OSError("rechazada"), asyncio.TimeoutError(), asyncpg.InvalidPasswordError("clave incorrecta")])
async def test_los_fallos_de_conexion_solo_informan_la_clase(monkeypatch, fallo):
    async def conectar_falla(**kwargs):
        raise fallo

    monkeypatch.setattr(motor.asyncpg, "connect", conectar_falla)
    with pytest.raises(ErrorMigracion) as error:
        await migrar("postgresql+asyncpg://usuario:CLAVE-SECRETA@servidor-secreto:5432/base", ESQUEMA_TRANSACCIONAL)
    texto = f"{error.value.mensaje} {error.value.detalles}"
    assert error.value.codigo == "MIGRATION_DB_UNAVAILABLE" and error.value.detalles == {"causa": type(fallo).__name__}
    assert "SECRETA" not in texto and "servidor-secreto" not in texto and "clave incorrecta" not in texto


def test_los_argumentos_de_conexion_salen_de_la_url_explicita():
    assert motor._argumentos_de_conexion("postgresql+asyncpg://u:p%40ss@h:6543/bd?ssl=require", "t") == {
        "host": "h", "port": 6543, "user": "u", "password": "p@ss", "database": "bd", "ssl": "require"}
    por_defecto = motor._argumentos_de_conexion("postgresql://u@/bd", "t")
    assert (por_defecto["host"], por_defecto["port"], por_defecto["password"], "ssl" in por_defecto) == ("localhost", 5432, None, False)
    for invalida in (None, "", "  ", "nada", "mysql://u@h/b", 5):
        with pytest.raises(ErrorMigracion) as error:
            motor._argumentos_de_conexion(invalida, "t")
        assert error.value.codigo == "MIGRATION_DB_NOT_CONFIGURED"


# ============================================ Piezas puras ============================================

def test_el_checksum_ignora_el_tipo_de_salto_de_linea_y_cambia_con_el_contenido(tmp_path):
    (tmp_path / "a.sql").write_bytes(b"SELECT 1;\r\nSELECT 2;\r\n")
    (tmp_path / "b.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    (tmp_path / "c.sql").write_bytes(b"SELECT 1;\nSELECT 3;\n")
    a, b, c = (Migracion(1, "m", f"{n}.sql", (), carpeta=str(tmp_path)) for n in "abc")
    assert a.checksum() == b.checksum() != c.checksum() and a.sql() == b.sql()


def test_diferencias_describe_cada_tipo_de_discrepancia_solo_con_nombres():
    esperado = {"columnas": [["a", "integer", True], ["b", "text", False], ["c", "text", False]],
                "restricciones": ["pk", "ck"], "indices": [["ix1", False, False], ["ix2", True, True]]}
    actual = {"columnas": [["a", "bigint", True], ["b", "text", False], ["d", "text", False]],
              "restricciones": ["pk", "otra"], "indices": [["ix1", False, False], ["ix2", True, False], ["ix3", False, False]]}
    assert diferencias(actual, esperado) == {
        "columnas_faltantes": ["c"], "columnas_sobrantes": ["d"], "columnas_distintas": ["a"],
        "restricciones_faltantes": ["ck"], "restricciones_sobrantes": ["otra"],
        "indices_sobrantes": ["ix3"], "indices_distintos": ["ix2"],
    }
    assert diferencias(esperado, esperado) == {}


def test_la_version_del_objeto_es_la_mas_alta_que_coincide_exactamente():
    f1, f2 = firma("documentos", 1), firma("documentos", 2)
    assert motor._version_del_objeto(None, {1: f1, 2: f2}) == 0
    assert motor._version_del_objeto(copy.deepcopy(f1), {1: f1, 2: f2}) == 1
    assert motor._version_del_objeto(copy.deepcopy(f2), {1: f1, 2: f2}) == 2
    assert motor._version_del_objeto(copy.deepcopy(f2), {1: f2, 2: f2}) == 2  # firmas iguales: gana la más alta
    assert motor._version_del_objeto({"columnas": [], "restricciones": [], "indices": []}, {1: f1}) is None


def test_el_candado_es_estable_y_distinto_por_base():
    assert motor._clave_candado("transaccional") == motor._clave_candado("transaccional")
    assert motor._clave_candado("transaccional") != motor._clave_candado("vectorial")
    assert 0 <= motor._clave_candado("transaccional") < 2**63  # cabe en el bigint del bloqueo asesor


# ============================================ Punto de entrada (sin bases) ============================================

async def test_asegurar_esquema_usa_cada_url_en_orden_y_sin_fallback(monkeypatch):
    from app.migraciones import catalogo

    llamadas = []

    async def migrar_doble(url, esquema, **kwargs):
        llamadas.append((url, esquema.nombre))
        return motor.ResultadoMigracion(esquema.nombre, aplicadas=(1,))

    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", "postgresql+asyncpg://u@transaccional/db")
    monkeypatch.setattr(catalogo.settings, "VECTOR_DATABASE_URL", "postgresql+asyncpg://u@vectorial/db")
    resultados = await catalogo.asegurar_esquema_ingesta()
    assert [r.base for r in resultados] == ["transaccional", "vectorial"]
    assert llamadas == [("postgresql+asyncpg://u@transaccional/db", "transaccional"), ("postgresql+asyncpg://u@vectorial/db", "vectorial")]


@pytest.mark.parametrize("valor", [None, "", "  ", "mysql://u@h/db"])
async def test_sin_url_vectorial_valida_no_se_cae_a_la_transaccional(monkeypatch, valor):
    from app.migraciones import catalogo

    llamadas = []

    async def migrar_doble(url, esquema, **kwargs):
        llamadas.append(esquema.nombre)
        return motor.ResultadoMigracion(esquema.nombre)

    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", "postgresql+asyncpg://u@transaccional/db")
    monkeypatch.setattr(catalogo.settings, "VECTOR_DATABASE_URL", valor)
    with pytest.raises(ErrorMigracion) as error:
        await catalogo.asegurar_esquema_ingesta()
    assert error.value.base == "vectorial" and error.value.codigo == "MIGRATION_DB_NOT_CONFIGURED"
    assert llamadas == ["transaccional"]  # la vectorial jamás se migró contra la URL transaccional


async def test_el_primer_fallo_detiene_la_preparacion(monkeypatch):
    from app.migraciones import catalogo

    async def migrar_doble(url, esquema, **kwargs):
        raise ErrorMigracion("MIGRATION_FAILED", "falló", esquema.nombre, {"version": 2})

    monkeypatch.setattr(catalogo, "migrar", migrar_doble)
    monkeypatch.setattr(catalogo.settings, "DATABASE_URL", "postgresql+asyncpg://u@t/db")
    with pytest.raises(ErrorMigracion) as error:
        await catalogo.asegurar_esquema_ingesta()
    assert error.value.base == "transaccional"  # no continúa con la vectorial

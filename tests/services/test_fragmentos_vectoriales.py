"""Etapa 4B: persistencia de fragmentos y embeddings en PostgreSQL + pgvector.

Dos niveles, que NO se mezclan:
- Validaciones y configuración: sin base de datos (una sesión que falla si se usa); se ejecutan siempre.
- INTEGRACIÓN: contra PostgreSQL con la extensión `vector` REAL, en un contenedor Docker desechable y
  aislado (`url_pgvector_aislado` en `conftest.py`) con el esquema aplicado desde
  `db/vector/001_fragmentos_documento.sql`. Si Docker o la imagen no están disponibles, esas pruebas se
  OMITEN con motivo (`pytest -rs`) y no cuentan como ejecutadas.

Los vectores son SINTÉTICOS: no se llama a OCI. Nada toca bases compartidas.
"""
import asyncio
import math
import random
import subprocess
import sys
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import settings
from app.database_vectorial import (
    cerrar_motor_vectorial,
    obtener_motor_vectorial,
    url_vectorial,
)
from app.exceptions import ConflictError, ExternalServiceError
from app.models import SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import ResultadoAnalisis
from app.services.ingesta import documento_service as servicio
from app.services.ingesta import fragmentos_vectoriales as fv
from app.services.ingesta.embeddings import FragmentoEmbebido, IdentidadEmbeddings, LoteEmbebido
from app.services.ingesta.fragmentacion import ParametrosFragmentacion, iterar_fragmentos
from tests.ayudantes_ingesta import (
    AlmacenEnMemoria, SesionSQLite, crear_empresa, crear_usuario, metadatos, sha256_de,
)

DIM = 1536
IDENTIDAD = IdentidadEmbeddings("oci-cohere", "cohere.embed-v4.0", DIM)
DOC_A, DOC_B = uuid.uuid4(), uuid.uuid4()
EMPRESA, OTRA_EMPRESA = uuid.uuid4(), uuid.uuid4()
TEXTO = (
    "# Informe 2025\r\n\r\n## Agua ñandú 🌊\r\n\r\nConsumo total de agua.\r\n\r\n"
    "| Indicador | Valor |\r\n|---|---|\r\n| Agua | 10 |\r\n\r\n"
    "## Título " + "muy largo " * 70 + "\r\n\r\nTexto final del documento.\r\n"
)


def vec(semilla: int) -> tuple[float, ...]:
    azar = random.Random(semilla)
    return tuple(azar.uniform(-1.0, 1.0) for _ in range(DIM))


def eje(i: int, mezcla: dict[int, float] | None = None) -> tuple[float, ...]:
    v = [0.0] * DIM
    v[i] = 1.0
    for j, valor in (mezcla or {}).items():
        v[j] = valor
    return tuple(v)


def fragmentos_reales():
    return list(iterar_fragmentos(TEXTO, ParametrosFragmentacion(max_caracteres=200, max_caracteres_contexto=400)))


def lote(fragmentos=None, vectores=None, numero=0, identidad=IDENTIDAD) -> LoteEmbebido:
    fragmentos = fragmentos if fragmentos is not None else fragmentos_reales()
    return LoteEmbebido(
        numero, identidad,
        tuple(FragmentoEmbebido(f, (vectores[i] if vectores else vec(i))) for i, f in enumerate(fragmentos)),
    )


def sintetico(indice: int, literal: str = "x", inicio: int | None = None, **cambios):
    base = fragmentos_reales()[0]
    inicio = indice * 10 if inicio is None else inicio
    return replace(base, indice=indice, inicio=inicio, fin=inicio + len(literal), texto_literal=literal, **cambios)


def args(**cambios):
    base = dict(ambiente="development", documento_id=DOC_A, empresa_id=EMPRESA, anio=2025,
                tipo=TipoDocumento.MEMORIA_ANUAL, sector=SectorEmpresa.MINERIA)
    base.update(cambios)
    return base


async def guardar(fabrica, lote_a_guardar=None, **cambios):
    async with fabrica() as db:
        return await fv.guardar_lote(db, lote=lote_a_guardar or lote(), **args(**cambios))


async def sql(fabrica, consulta: str, **parametros):
    async with fabrica() as db:
        return (await db.execute(text(consulta), parametros)).all()


class SesionProhibida:
    """Una sesión que no debe usarse: prueba que se rechaza ANTES de tocar la base."""

    def __init__(self):
        self.usos = 0

    def in_transaction(self):
        return False

    async def execute(self, *a, **k):
        self.usos += 1
        raise AssertionError("no debía tocar la base")

    async def commit(self):
        self.usos += 1

    async def rollback(self):
        self.usos += 1


# ===================================== Validación (sin base de datos) =====================================

@pytest.mark.parametrize("vector", [
    tuple([0.5] * 1535), tuple([0.5] * 1537), (), tuple([0.5] * 1024),               # dimensión
    tuple([0.5] * 1535 + [math.nan]), tuple([0.5] * 1535 + [math.inf]),              # valores inválidos
    tuple([0.5] * 1535 + [-math.inf]), tuple([0.5] * 1535 + [True]),
    tuple([0.5] * 1535 + ["0.5"]), tuple([0.5] * 1535 + [None]), tuple([0.5] * 1535 + [1e39]),
    tuple([0.0] * DIM),                                                              # norma cero
    tuple([1e-50] * DIM),                                                            # no nulo en double, CERO en float32
    tuple([-1e-50] * DIM), tuple([1e-30] * DIM), tuple([1e19] * DIM),                # norma no representable en float32 (NaN en pgvector)
    tuple([3.5e38] * DIM),                                                           # fuera del rango de float32
    "x" * DIM, b"x" * DIM, None, 42,
])
async def test_vectores_invalidos_se_rechazan_antes_de_persistir(vector):
    sesion = SesionProhibida()
    elementos = (FragmentoEmbebido(sintetico(0), vec(1)), FragmentoEmbebido(sintetico(1), vector))
    valor_loteembebido = LoteEmbebido(0, IDENTIDAD, elementos)
    valor_args = args()
    with pytest.raises(ValueError):
        await fv.guardar_lote(sesion, lote=valor_loteembebido, **valor_args)
    assert sesion.usos == 0  # ni siquiera se abrió la transacción


@pytest.mark.parametrize("cambios", [
    dict(ambiente="Produccion"), dict(ambiente=""), dict(ambiente=None),
    dict(documento_id="no-uuid"), dict(empresa_id=None), dict(anio=1999), dict(anio=True), dict(anio="2025"),
    dict(tipo=""), dict(tipo=None), dict(sector="  "), dict(sector="Mine\x00ria"),
])
async def test_metadatos_invalidos_se_rechazan_antes_de_persistir(cambios):
    sesion = SesionProhibida()
    valor_lote = lote()
    valor_args_2 = args(**cambios)
    with pytest.raises(ValueError):
        await fv.guardar_lote(sesion, lote=valor_lote, **valor_args_2)
    assert sesion.usos == 0


@pytest.mark.parametrize("construir", [
    lambda: LoteEmbebido(0, IdentidadEmbeddings("oci-cohere", "modelo-viejo", 1024), (FragmentoEmbebido(sintetico(0), vec(1)),)),
    lambda: LoteEmbebido(0, IDENTIDAD, ()),
    lambda: lote([sintetico(0), sintetico(0, inicio=50)]),                      # índice repetido en el lote
    lambda: lote([sintetico(-1)]),                                              # índice negativo
    lambda: lote([replace(sintetico(0), inicio=10, fin=10, texto_literal="")]),                  # vacío (fin == inicio)
    lambda: lote([replace(sintetico(0), inicio=5, fin=3, texto_literal="x")]),  # fin < inicio
    lambda: lote([replace(sintetico(0), texto_literal="xx", fin=sintetico(0).inicio + 1)]),  # longitud != fin - inicio
    lambda: lote([sintetico(0, literal="a\x00b")]),
    lambda: lote([sintetico(0, contexto="ctx\x00")]),
    lambda: lote([sintetico(0, ruta_encabezados=("ok", "ma\x00l"))]),
    lambda: lote([sintetico(0, continuacion="no")]),
    lambda: "no es un lote",
])
async def test_lotes_inconsistentes_se_rechazan_antes_de_persistir(construir):
    sesion = SesionProhibida()
    valor_construir = construir()
    valor_args_3 = args()
    with pytest.raises(ValueError):
        await fv.guardar_lote(sesion, lote=valor_construir, **valor_args_3)
    assert sesion.usos == 0


@pytest.mark.parametrize("vector", [
    tuple([1e-20] * DIM),                                # pequeño pero válido en float32
    tuple([1.0] + [1e-50] * (DIM - 1)),                  # la mayoría se vuelve 0 en float32; el vector sigue siendo no nulo
    tuple([1e-10] * DIM), tuple([1.0] * DIM), tuple([1e17] * DIM),
    tuple([float(i + 1) for i in range(DIM)]),
])
def test_vectores_que_siguen_siendo_validos_en_float32_se_aceptan(vector):
    literal = fv._validar_vector(vector, "El vector")
    assert literal.startswith("[")
    assert literal.endswith("]")
    assert literal.count(",") == DIM - 1


def test_el_literal_enviado_es_exactamente_la_representacion_float32():
    literal = fv._validar_vector(tuple([0.1] * DIM), "El vector")
    valor = float(literal[1:].split(",")[0])
    assert valor == fv._a_float32(0.1)
    assert valor != 0.1  # lo validado es lo almacenado, no el double original


async def test_una_sesion_con_transaccion_abierta_se_rechaza():
    class Abierta(SesionProhibida):
        def in_transaction(self):
            return True

    sesion = Abierta()
    for operacion in (
        fv.guardar_lote(sesion, lote=lote(), **args()),
        fv.publicar_fragmentos(sesion, ambiente="development", documento_id=DOC_A),
        fv.eliminar_fragmentos(sesion, ambiente="development", documento_id=DOC_A),
        fv.contar_fragmentos(sesion, ambiente="development", documento_id=DOC_A),
        fv.buscar_similares(sesion, ambiente="development", vector_consulta=vec(1), identidad=IDENTIDAD, empresa_ids=[EMPRESA], limite=3),
    ):
        with pytest.raises(RuntimeError):
            await operacion
    assert sesion.usos == 0


@pytest.mark.parametrize("cambios", [
    dict(vector_consulta=tuple([0.5] * 1535)), dict(vector_consulta=tuple([0.0] * DIM)),
    dict(vector_consulta=tuple([math.nan] * DIM)), dict(limite=0), dict(limite=1001), dict(limite=True),
    dict(empresa_ids=None), dict(empresa_ids="abc"), dict(empresa_ids=["no-uuid"]), dict(ambiente="X"),
    dict(anio="2025"), dict(tipo=""), dict(sector=""),
    dict(identidad=None), dict(identidad="cohere.embed-v4.0"),
    dict(identidad=IdentidadEmbeddings("oci-cohere", "cohere.embed-v4.0", 1024)),  # otra dimensión
    dict(vector_consulta=tuple([1e-50] * DIM)), dict(vector_consulta=tuple([1e-30] * DIM)),
    dict(vector_consulta=tuple([1e19] * DIM)), dict(vector_consulta=tuple([3.5e38] * DIM)),
])
async def test_busqueda_con_parametros_invalidos(cambios):
    sesion = SesionProhibida()
    valores = dict(ambiente="development", vector_consulta=vec(1), identidad=IDENTIDAD, empresa_ids=[EMPRESA], limite=3)
    valores.update(cambios)
    with pytest.raises(ValueError):
        await fv.buscar_similares(sesion, **valores)
    assert sesion.usos == 0


async def test_busqueda_sin_empresas_devuelve_vacio_sin_consultar():
    sesion = SesionProhibida()
    assert await fv.buscar_similares(sesion, ambiente="development", vector_consulta=vec(1), identidad=IDENTIDAD, empresa_ids=[], limite=3) == []
    assert sesion.usos == 0


@pytest.mark.parametrize("operacion", ["publicar_fragmentos", "eliminar_fragmentos", "contar_fragmentos"])
async def test_operaciones_por_documento_validan_ambiente_y_documento(operacion):
    sesion = SesionProhibida()
    for cambios in (dict(ambiente="Mal"), dict(documento_id="x"), dict(documento_id=None)):
        valores = dict(ambiente="development", documento_id=DOC_A)
        valores.update(cambios)
        valor_getattr = getattr(fv, operacion)
        with pytest.raises(ValueError):
            await valor_getattr(sesion, **valores)
    assert sesion.usos == 0


# ===================================== Configuración sin fallback =====================================

def test_sin_vector_database_url_no_hay_fallback_y_el_error_no_revela_ninguna_url(monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", None)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://usuario:CLAVE-TRANSACCIONAL@host/db")
    for valor in (None, "", "   "):
        monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", valor)
        with pytest.raises(ExternalServiceError) as capturado:
            url_vectorial()
        assert capturado.value.code == "VECTOR_DATABASE_NOT_CONFIGURED"
        assert "CLAVE-TRANSACCIONAL" not in f"{capturado.value} {capturado.value.details}"
        with pytest.raises(ExternalServiceError):  # tampoco crea un motor con DATABASE_URL
            obtener_motor_vectorial()


def test_una_url_vectorial_con_otro_controlador_se_rechaza_sin_repetirla(monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", "mysql://usuario:CLAVE-SECRETA@host/db")
    with pytest.raises(ExternalServiceError) as capturado:
        url_vectorial()
    assert capturado.value.code == "VECTOR_DATABASE_URL_INVALID"
    assert "CLAVE-SECRETA" not in str(capturado.value)


async def test_el_motor_vectorial_se_crea_de_forma_perezosa_con_la_url_configurada(monkeypatch):
    monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", "postgresql+asyncpg://usuario:clave@127.0.0.1:1/db")
    try:
        motor = obtener_motor_vectorial()  # no conecta
        assert obtener_motor_vectorial() is motor
        assert motor.url.database == "db"
    finally:
        await cerrar_motor_vectorial()
    assert obtener_motor_vectorial() is not motor  # tras cerrar, se crea otro
    await cerrar_motor_vectorial()


def test_la_falta_de_la_url_vectorial_no_bloquea_el_arranque_de_otros_modulos():
    """En un proceso limpio, sin .env y sin VECTOR_DATABASE_URL, la aplicación importa y el valor es None."""
    codigo = (
        "import os, dotenv; dotenv.load_dotenv = lambda *a, **k: False; "
        "os.environ.pop('VECTOR_DATABASE_URL', None); "
        "os.environ.update(JWT_SECRET_KEY='x', DATABASE_URL='postgresql+asyncpg://u:p@h/db'); "
        "import app.main, app.config as c; print(repr(c.settings.VECTOR_DATABASE_URL))"
    )
    resultado = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, timeout=120)
    assert resultado.returncode == 0, resultado.stderr[-500:]
    assert resultado.stdout.strip().endswith("None")


# ===================================== Integración con pgvector REAL =====================================

async def test_la_extension_vector_real_y_el_esquema_del_archivo_sql(fabrica_vectorial):
    version = (await sql(fabrica_vectorial, "SELECT extversion FROM pg_extension WHERE extname = 'vector'"))[0][0]
    assert version  # pgvector real instalado
    tipo = (await sql(fabrica_vectorial,
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'fragmentos_documento'::regclass AND attname = 'embedding'"))[0][0]
    assert tipo == "vector(1536)"
    indices = {f[0] for f in await sql(fabrica_vectorial, "SELECT indexdef FROM pg_indexes WHERE tablename = 'fragmentos_documento'")}
    assert not any("hnsw" in d.lower() or "ivfflat" in d.lower() for d in indices)  # sin índice vectorial todavía
    claves_foraneas = await sql(fabrica_vectorial, "SELECT 1 FROM pg_constraint WHERE conrelid = 'fragmentos_documento'::regclass AND contype = 'f'")
    assert claves_foraneas == []  # sin FK entre bases


async def test_persistencia_y_lectura_conservan_texto_literal_y_metadatos(fabrica_vectorial):
    fragmentos = fragmentos_reales()
    assert len(fragmentos) >= 2
    assert any(len(t) > 500 for f in fragmentos for t in f.ruta_encabezados)  # encabezado largo
    assert await guardar(fabrica_vectorial, lote(fragmentos)) == len(fragmentos)

    filas = await sql(fabrica_vectorial,
        "SELECT indice, texto_literal, contexto, inicio, fin, continuacion, ruta_encabezados, empresa_id, anio, tipo, sector, "
        "embedding_proveedor, embedding_modelo, embedding_dimension, vector_dims(embedding), publicado, ambiente, documento_id "
        "FROM fragmentos_documento ORDER BY indice")
    assert len(filas) == len(fragmentos)
    for fila, f in zip(filas, fragmentos):
        assert fila.texto_literal == f.texto_literal
        assert fila.texto_literal == TEXTO[f.inicio:f.fin]  # sin alterar (CRLF, ñ, emoji)
        assert (fila.contexto, fila.inicio, fila.fin, fila.continuacion) == (f.contexto, f.inicio, f.fin, f.continuacion)
        assert tuple(fila.ruta_encabezados) == f.ruta_encabezados
        assert (fila.empresa_id, fila.anio, fila.tipo, fila.sector) == (EMPRESA, 2025, "MEMORIA_ANUAL", "MINERIA")
        assert (fila.embedding_proveedor, fila.embedding_modelo, fila.embedding_dimension) == ("oci-cohere", "cohere.embed-v4.0", DIM)
        assert fila[14] == DIM
        assert fila.publicado is False
        assert (fila.ambiente, fila.documento_id) == ("development", DOC_A)

    # Tras publicar, la lectura de recuperación devuelve lo mismo y la composición del embedding se reproduce.
    assert await publicar(fabrica_vectorial) == len(fragmentos)
    async with fabrica_vectorial() as db:
        hallados = await fv.buscar_similares(db, ambiente="development", vector_consulta=vec(0), identidad=IDENTIDAD, empresa_ids=[EMPRESA], limite=50)
    por_indice = {h.indice: h for h in hallados}
    assert set(por_indice) == {f.indice for f in fragmentos}
    for f in fragmentos:
        h = por_indice[f.indice]
        assert h.texto_literal == f.texto_literal
        assert h.contexto == f.contexto
        assert h.texto_embedding == f.texto_embedding  # lo que se envió al proveedor, sin cambiar la composición
        assert (h.inicio, h.fin, h.continuacion, h.ruta_encabezados, h.seccion) == (f.inicio, f.fin, f.continuacion, f.ruta_encabezados, f.seccion)
    assert hallados[0].indice == 0
    assert hallados[0].similitud == pytest.approx(1.0, abs=1e-6)  # vec(0) vs sí mismo


async def test_el_vector_guardado_conserva_sus_componentes(fabrica_vectorial):
    v = vec(7)
    await guardar(fabrica_vectorial, lote([sintetico(0)], [v]))
    guardado = (await sql(fabrica_vectorial, "SELECT embedding::text FROM fragmentos_documento"))[0][0]
    leidos = [float(x) for x in guardado.strip("[]").split(",")]
    assert len(leidos) == DIM
    assert all(abs(a - b) < 1e-6 for a, b in zip(leidos, v))  # float4: ~7 cifras


async def publicar(fabrica, ambiente="development", documento=DOC_A):
    async with fabrica() as db:
        return await fv.publicar_fragmentos(db, ambiente=ambiente, documento_id=documento)


async def eliminar(fabrica, ambiente="development", documento=DOC_A):
    async with fabrica() as db:
        return await fv.eliminar_fragmentos(db, ambiente=ambiente, documento_id=documento)


async def contar(fabrica, ambiente="development", documento=DOC_A):
    async with fabrica() as db:
        return await fv.contar_fragmentos(db, ambiente=ambiente, documento_id=documento)


async def buscar(fabrica, consulta, ambiente="development", empresas=(EMPRESA,), limite=10, identidad=IDENTIDAD, **filtros):
    async with fabrica() as db:
        return await fv.buscar_similares(db, ambiente=ambiente, vector_consulta=consulta, identidad=identidad, empresa_ids=list(empresas), limite=limite, **filtros)


async def test_el_esquema_rechaza_filas_incoherentes_aunque_se_salten_el_servicio(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0)]))
    base = ("INSERT INTO fragmentos_documento (ambiente, documento_id, indice, empresa_id, anio, tipo, sector, texto_literal, "
            "contexto, inicio, fin, continuacion, embedding, embedding_proveedor, embedding_modelo, embedding_dimension%s) "
            "VALUES ('development', gen_random_uuid(), 0, gen_random_uuid(), 2025, 't', 's', %s, '', %s, %s, false, "
            "array_fill(0.5::real, ARRAY[%s])::vector, 'p', 'm', %s%s)")
    casos = {
        "literal no mide fin-inicio": base % ("", "'abc'", 0, 5, 1536, 1536, ""),
        "dimension del vector": base % ("", "'a'", 0, 1, 1024, 1536, ""),
        "dimension declarada": base % ("", "'a'", 0, 1, 1536, 1024, ""),
        "publicado sin fecha": base % (", publicado", "'a'", 0, 1, 1536, 1536, ", true"),
    }
    for nombre, consulta in casos.items():
        with pytest.raises(DBAPIError):
            await sql(fabrica_vectorial, consulta), nombre
    # Una fila válida con el mismo molde sí entra (el molde no es la causa de los rechazos).
    async with fabrica_vectorial() as db:
        await db.execute(text(base % ("", "'a'", 0, 1, 1536, 1536, "")))
        await db.commit()
    assert (await sql(fabrica_vectorial, "SELECT count(*) FROM fragmentos_documento"))[0][0] == 2


async def test_los_fragmentos_se_insertan_no_publicados_y_las_lecturas_los_excluyen(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0), sintetico(1)], [eje(0), eje(1)]))
    assert await contar(fabrica_vectorial) == fv.ConteoFragmentos(total=2, publicados=0)
    assert (await sql(fabrica_vectorial, "SELECT count(*) FROM fragmentos_documento WHERE NOT publicado"))[0][0] == 2
    assert (await sql(fabrica_vectorial, "SELECT count(*) FROM fragmentos_consultables"))[0][0] == 0
    assert await buscar(fabrica_vectorial, eje(0)) == []  # nada recuperable sin publicar


async def test_publicar_es_idempotente_y_conserva_la_fecha(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0), sintetico(1)], [eje(0), eje(1)]))
    assert await publicar(fabrica_vectorial) == 2
    primera = (await sql(fabrica_vectorial, "SELECT max(publicado_en) FROM fragmentos_documento"))[0][0]
    assert await contar(fabrica_vectorial) == fv.ConteoFragmentos(total=2, publicados=2)
    assert await publicar(fabrica_vectorial) == 0  # repetirla no cambia nada
    assert (await sql(fabrica_vectorial, "SELECT max(publicado_en) FROM fragmentos_documento"))[0][0] == primera
    assert await publicar(fabrica_vectorial, documento=uuid.uuid4()) == 0  # documento inexistente: no es error
    assert len(await buscar(fabrica_vectorial, eje(0))) == 2


async def test_eliminar_es_idempotente_y_no_afecta_a_otros_documentos_ni_ambientes(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0), sintetico(1)], [eje(0), eje(1)]))                       # A en development
    await guardar(fabrica_vectorial, lote([sintetico(0)], [eje(0)]), documento_id=DOC_B)                           # B en development
    await guardar(fabrica_vectorial, lote([sintetico(0)], [eje(0)]), ambiente="qa")                                # A en qa
    for ambiente, documento in (("development", DOC_A), ("development", DOC_B), ("qa", DOC_A)):
        await publicar(fabrica_vectorial, ambiente, documento)

    assert await eliminar(fabrica_vectorial) == 2  # A/development, publicados incluidos
    assert await eliminar(fabrica_vectorial) == 0  # idempotente
    assert await contar(fabrica_vectorial) == fv.ConteoFragmentos(0, 0)
    assert await contar(fabrica_vectorial, documento=DOC_B) == fv.ConteoFragmentos(1, 1)
    assert await contar(fabrica_vectorial, ambiente="qa") == fv.ConteoFragmentos(1, 1)
    assert await eliminar(fabrica_vectorial, documento=uuid.uuid4()) == 0
    assert len(await buscar(fabrica_vectorial, eje(0))) == 1  # solo B queda en development


async def test_aislamiento_entre_ambientes(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "dev")], [eje(0)]))
    await guardar(fabrica_vectorial, lote([sintetico(0, "qa!")], [eje(0)]), ambiente="qa")  # mismo documento e índice: permitido por ambiente
    assert await publicar(fabrica_vectorial, "development") == 1
    assert await contar(fabrica_vectorial, "qa") == fv.ConteoFragmentos(1, 0)  # publicar en development no publica qa
    assert await buscar(fabrica_vectorial, eje(0), ambiente="qa") == []
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), ambiente="development")] == ["dev"]
    await publicar(fabrica_vectorial, "qa")
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), ambiente="qa")] == ["qa!"]
    assert await buscar(fabrica_vectorial, eje(0), ambiente="uat") == []


async def test_un_indice_duplicado_del_documento_se_rechaza_y_no_altera_lo_existente(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "original")], [eje(0)]))
    valor_lote_2 = lote([sintetico(0, "intruso")], [eje(1)])
    with pytest.raises(ConflictError) as capturado:
        await guardar(fabrica_vectorial, valor_lote_2)
    assert capturado.value.code == "FRAGMENTS_ALREADY_PERSISTED"
    assert [f.texto_literal for f in await sql(fabrica_vectorial, "SELECT texto_literal FROM fragmentos_documento")] == ["original"]


async def test_un_fallo_a_mitad_del_lote_no_deja_filas_parciales(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "previo")], [eje(0)]))
    # 1) Conflicto en la última fila: los índices 1 y 2 NO deben quedar.
    valor_lote_3 = lote([sintetico(1), sintetico(2), sintetico(0)], [eje(1), eje(2), eje(3)])
    with pytest.raises(ConflictError):
        await guardar(fabrica_vectorial, valor_lote_3)
    assert [f.indice for f in await sql(fabrica_vectorial, "SELECT indice FROM fragmentos_documento ORDER BY indice")] == [0]
    # 2) Error de datos de la base (entero fuera de rango) en la tercera fila: no es un conflicto y tampoco deja filas.
    fuera_de_rango = replace(sintetico(5, "x"), inicio=2**31, fin=2**31 + 1)
    valor_lote_4 = lote([sintetico(3), sintetico(4), fuera_de_rango], [eje(1), eje(2), eje(3)])
    with pytest.raises(DBAPIError):
        await guardar(fabrica_vectorial, valor_lote_4)
    assert [f.indice for f in await sql(fabrica_vectorial, "SELECT indice FROM fragmentos_documento ORDER BY indice")] == [0]
    # La sesión sigue siendo utilizable tras el fallo y el mismo lote correcto sí se guarda.
    assert await guardar(fabrica_vectorial, lote([sintetico(3), sintetico(4)], [eje(1), eje(2)])) == 2


async def test_busqueda_exacta_por_coseno_con_vectores_sinteticos(fabrica_vectorial):
    d0, d1, d2 = eje(0), eje(0, {1: 1.0}), eje(2)  # ángulos conocidos respecto de la consulta e0
    await guardar(fabrica_vectorial, lote([sintetico(0, "d0"), sintetico(1, "d1"), sintetico(2, "d2")], [d0, d1, d2]))
    await publicar(fabrica_vectorial)
    hallados = await buscar(fabrica_vectorial, eje(0))
    assert [h.texto_literal for h in hallados] == ["d0", "d1", "d2"]
    assert [h.similitud for h in hallados] == pytest.approx([1.0, math.sqrt(0.5), 0.0], abs=1e-5)
    # El coseno no depende de la magnitud de la consulta ni de los documentos.
    escalada = tuple(c * 37.5 for c in eje(0))
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, escalada)] == ["d0", "d1", "d2"]
    # Consulta opuesta: orden inverso y similitud negativa (rango [-1, 1]).
    opuesta = [h for h in await buscar(fabrica_vectorial, tuple(-c for c in eje(0)))]
    assert [h.texto_literal for h in opuesta][0] in ("d2",)
    assert opuesta[-1].similitud == pytest.approx(-1.0, abs=1e-5)
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), limite=2)] == ["d0", "d1"]


async def test_busqueda_con_empate_es_determinista_y_respeta_los_filtros(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "a"), sintetico(1, "b")], [eje(0), eje(0)]))  # empate exacto
    await guardar(fabrica_vectorial, lote([sintetico(0, "o")], [eje(0)]), documento_id=DOC_B, empresa_id=OTRA_EMPRESA, anio=2024,
                  tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI, sector=SectorEmpresa.ENERGIA)
    await publicar(fabrica_vectorial, documento=DOC_A)
    await publicar(fabrica_vectorial, documento=DOC_B)
    ambos = (EMPRESA, OTRA_EMPRESA)
    orden = [(h.documento_id, h.indice) for h in await buscar(fabrica_vectorial, eje(0), empresas=ambos)]
    assert orden == sorted(orden, key=lambda p: (str(p[0]), p[1]))
    assert len(orden) == 3  # desempate por documento e índice
    assert {h.texto_literal for h in await buscar(fabrica_vectorial, eje(0))} == {"a", "b"}  # solo la empresa pedida
    assert await buscar(fabrica_vectorial, eje(0), empresas=[uuid.uuid4()]) == []
    assert {h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), empresas=ambos, anio=2024)} == {"o"}
    assert {h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), empresas=ambos, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)} == {"o"}
    assert {h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), empresas=ambos, sector=SectorEmpresa.ENERGIA)} == {"o"}
    assert await buscar(fabrica_vectorial, eje(0), empresas=ambos, anio=1999 + 30) == []


async def test_publicar_un_documento_no_publica_otro(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "a")], [eje(0)]))
    await guardar(fabrica_vectorial, lote([sintetico(0, "b")], [eje(0)]), documento_id=DOC_B)
    await publicar(fabrica_vectorial, documento=DOC_A)
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0))] == ["a"]
    assert await contar(fabrica_vectorial, documento=DOC_B) == fv.ConteoFragmentos(1, 0)


# ===================== Las DOS condiciones: publicado (vectorial) y COMPLETADO (transaccional) =====================

async def test_publicado_no_basta_hay_que_comprobar_completado_en_la_base_transaccional(fabrica_vectorial):
    transaccional = SesionSQLite()
    try:
        usuario = await crear_usuario(transaccional, correo="superadmin@pruebas.invalid")
        empresa = await crear_empresa(transaccional)
        datos = b"# Documento\n"
        documento = await servicio.reservar_documento(
            transaccional, metadatos=metadatos(empresa), nombre_archivo="d.md", sha256=sha256_de(datos),
            tamano_bytes=len(datos), usuario_id=usuario.id, ambiente="development",
        )
        token = documento.ejecucion_token
        almacen = AlmacenEnMemoria()
        await servicio.almacenar_original(transaccional, almacen, documento.id, datos, token=token)

        # 1) Fragmentos insertados (no publicados) y, por error, publicados ANTES de completar el documento.
        await guardar(fabrica_vectorial, lote([sintetico(0, "contenido")], [eje(0)]),
                      documento_id=documento.id, empresa_id=empresa.id)
        await publicar(fabrica_vectorial, documento=documento.id)
        hallados = await buscar(fabrica_vectorial, eje(0), empresas=[empresa.id])
        assert len(hallados) == 1  # la base vectorial lo muestra...
        ids = [h.documento_id for h in hallados]
        assert await servicio.documentos_recuperables(transaccional, ambiente="development", documento_ids=ids) == set()  # ...el contrato lo descarta

        # 2) Orden correcto: completar en la base transaccional y entonces publicar en la vectorial.
        await servicio.publicar_documento(
            transaccional, documento.id, token=token, indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
        assert await servicio.documentos_recuperables(transaccional, ambiente="development", documento_ids=ids) == {documento.id}

        # 3) Empresa desactivada: ya no es recuperable aunque siga publicado y COMPLETADO.
        empresa.activa = False
        await transaccional.commit()
        assert await servicio.documentos_recuperables(transaccional, ambiente="development", documento_ids=ids) == set()
    finally:
        transaccional.cerrar()


async def test_documentos_recuperables_filtra_estado_ambiente_y_empresa():
    transaccional = SesionSQLite()
    try:
        usuario = await crear_usuario(transaccional, correo="superadmin@pruebas.invalid")
        empresa = await crear_empresa(transaccional)
        otra = await crear_empresa(transaccional, nombre="Otra")

        async def nuevo(n, empresa_=empresa, ambiente="development"):
            datos = f"# Documento {n}\n".encode()
            d = await servicio.reservar_documento(
                transaccional, metadatos=metadatos(empresa_, 2000 + n), nombre_archivo="d.md", sha256=sha256_de(datos),
                tamano_bytes=len(datos), usuario_id=usuario.id, ambiente=ambiente)
            return d, datos

        completo, datos = await nuevo(1)
        almacen = AlmacenEnMemoria()
        await servicio.almacenar_original(transaccional, almacen, completo.id, datos, token=completo.ejecucion_token)
        await servicio.publicar_documento(transaccional, completo.id, token=completo.ejecucion_token,
                                          indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS)
        en_proceso, _ = await nuevo(2)
        inactiva_doc, d3 = await nuevo(3, otra)
        await servicio.almacenar_original(transaccional, almacen, inactiva_doc.id, d3, token=inactiva_doc.ejecucion_token)
        await servicio.publicar_documento(transaccional, inactiva_doc.id, token=inactiva_doc.ejecucion_token,
                                          indexacion_confirmada=True, resultado_analisis=ResultadoAnalisis.OBSERVADO)
        otra.activa = False
        await transaccional.commit()
        fallido, _ = await nuevo(4)
        await servicio.fallar_documento(transaccional, fallido.id, "CARGA_CANCELADA", token=fallido.ejecucion_token)

        ids = [completo.id, en_proceso.id, inactiva_doc.id, fallido.id, uuid.uuid4()]
        assert await servicio.documentos_recuperables(transaccional, ambiente="development", documento_ids=ids) == {completo.id}
        assert await servicio.documentos_recuperables(transaccional, ambiente="qa", documento_ids=ids) == set()
        assert await servicio.documentos_recuperables(transaccional, ambiente="development", documento_ids=[]) == set()
        with pytest.raises(ValueError):
            await servicio.documentos_recuperables(transaccional, ambiente="development", documento_ids=["x"])
        with pytest.raises(ValueError):
            await servicio.documentos_recuperables(transaccional, ambiente="Mal", documento_ids=ids)
    finally:
        transaccional.cerrar()


# ===================================== Ramas de error y dependencia de sesión =====================================

class SesionQueFalla(SesionProhibida):
    def __init__(self, error):
        super().__init__()
        self.error, self.rollbacks, self.commits = error, 0, 0

    async def execute(self, *a, **k):
        raise self.error

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize("operacion", ["guardar", "publicar", "eliminar", "contar", "buscar"])
@pytest.mark.parametrize("error", [
    RuntimeError("fallo de la base"),
    IntegrityError("INSERT", {}, Exception("otra restricción distinta")),
])
async def test_cualquier_fallo_hace_rollback_y_se_propaga_sin_convertirse_en_conflicto(operacion, error):
    sesion = SesionQueFalla(error)
    llamadas = {
        "guardar": lambda: fv.guardar_lote(sesion, lote=lote(), **args()),
        "publicar": lambda: fv.publicar_fragmentos(sesion, ambiente="development", documento_id=DOC_A),
        "eliminar": lambda: fv.eliminar_fragmentos(sesion, ambiente="development", documento_id=DOC_A),
        "contar": lambda: fv.contar_fragmentos(sesion, ambiente="development", documento_id=DOC_A),
        "buscar": lambda: fv.buscar_similares(sesion, ambiente="development", vector_consulta=vec(1), identidad=IDENTIDAD, empresa_ids=[EMPRESA], limite=3),
    }
    with pytest.raises(type(error)) as capturado:
        await llamadas[operacion]()
    assert not isinstance(capturado.value, ConflictError)
    assert sesion.rollbacks == 1
    assert sesion.commits == 0  # nada confirmado


async def test_la_dependencia_de_sesion_vectorial_usa_la_url_configurada_y_hace_rollback_ante_errores(url_pgvector_aislado, monkeypatch):
    from app.database_vectorial import fabrica_sesiones_vectoriales, get_vector_db

    monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", url_pgvector_aislado)
    try:
        generador = get_vector_db()
        sesion = await generador.__anext__()
        assert (await sesion.execute(text("SELECT 1"))).scalar_one() == 1
        valor_runtimeerror = RuntimeError("fallo en la solicitud")
        with pytest.raises(RuntimeError):
            await generador.athrow(valor_runtimeerror)
        assert not sesion.in_transaction()  # el rollback cerró la transacción
        async with fabrica_sesiones_vectoriales()() as otra:
            assert (await otra.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await cerrar_motor_vectorial()


async def test_sin_url_la_dependencia_de_sesion_falla_de_forma_controlada(monkeypatch):
    from app.database_vectorial import get_vector_db

    monkeypatch.setattr(settings, "VECTOR_DATABASE_URL", None)
    valor_vector_db = get_vector_db()
    with pytest.raises(ExternalServiceError) as capturado:
        await valor_vector_db.__anext__()
    assert capturado.value.code == "VECTOR_DATABASE_NOT_CONFIGURED"


# ===================================== Aislamiento por identidad del embedding (pgvector real) =====================================

OTRO_MODELO = IdentidadEmbeddings("oci-cohere", "modelo-incompatible-1536", DIM)  # misma dimensión, otro espacio vectorial
OTRO_PROVEEDOR = IdentidadEmbeddings("otro-proveedor", "cohere.embed-v4.0", DIM)  # mismo nombre de modelo, otro proveedor


async def test_un_modelo_incompatible_de_1536_queda_excluido_aunque_sea_mas_similar_con_limite_1(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "compatible")], [eje(0, {1: 1.0})]))  # coseno 0.7071 con la consulta
    await guardar(fabrica_vectorial, lote([sintetico(0, "incompatible")], [eje(0)], identidad=OTRO_MODELO), documento_id=DOC_B)
    await publicar(fabrica_vectorial, documento=DOC_A)
    await publicar(fabrica_vectorial, documento=DOC_B)

    # El incompatible tiene similitud 1.0 (mayor) y limite=1: si el filtro se aplicara después del LIMIT no habría resultados.
    hallados = await buscar(fabrica_vectorial, eje(0), limite=1)
    assert [h.texto_literal for h in hallados] == ["compatible"]
    assert hallados[0].similitud == pytest.approx(math.sqrt(0.5), abs=1e-5)
    assert (hallados[0].embedding_proveedor, hallados[0].embedding_modelo) == ("oci-cohere", "cohere.embed-v4.0")
    # Con la identidad del otro modelo ocurre lo contrario: solo se ve el suyo.
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), limite=1, identidad=OTRO_MODELO)] == ["incompatible"]
    # Una identidad sin fragmentos no devuelve nada (no se rellena con otros modelos).
    assert await buscar(fabrica_vectorial, eje(0), limite=10, identidad=IdentidadEmbeddings("oci-cohere", "no-existe", DIM)) == []


async def test_el_proveedor_tambien_aisla_aunque_el_nombre_del_modelo_coincida(fabrica_vectorial):
    await guardar(fabrica_vectorial, lote([sintetico(0, "oci")], [eje(0, {1: 1.0})]))
    await guardar(fabrica_vectorial, lote([sintetico(0, "otro-proveedor")], [eje(0)], identidad=OTRO_PROVEEDOR), documento_id=DOC_B)
    await publicar(fabrica_vectorial, documento=DOC_A)
    await publicar(fabrica_vectorial, documento=DOC_B)
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), limite=1)] == ["oci"]
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), limite=1, identidad=OTRO_PROVEEDOR)] == ["otro-proveedor"]


async def test_la_identidad_se_suma_a_los_filtros_de_ambiente_empresa_y_publicacion(fabrica_vectorial):
    # Todos con vectores idénticos a la consulta (similitud 1.0), salvo el válido (menos similar): solo debe ganar el válido.
    sin_publicar, en_qa, otro_modelo = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await guardar(fabrica_vectorial, lote([sintetico(0, "valido")], [eje(0, {1: 0.5})]))                                   # DOC_A
    await guardar(fabrica_vectorial, lote([sintetico(0, "no-publicado")], [eje(0)]), documento_id=sin_publicar)
    await guardar(fabrica_vectorial, lote([sintetico(0, "otra-empresa")], [eje(0)]), documento_id=DOC_B, empresa_id=OTRA_EMPRESA)
    await guardar(fabrica_vectorial, lote([sintetico(0, "otro-ambiente")], [eje(0)]), ambiente="qa", documento_id=en_qa)
    await guardar(fabrica_vectorial, lote([sintetico(0, "otro-modelo")], [eje(0)], identidad=OTRO_MODELO), documento_id=otro_modelo)
    for ambiente, documento in (("development", DOC_A), ("development", DOC_B), ("qa", en_qa), ("development", otro_modelo)):
        await publicar(fabrica_vectorial, ambiente, documento)
    assert [h.texto_literal for h in await buscar(fabrica_vectorial, eje(0), limite=1)] == ["valido"]
    assert await contar(fabrica_vectorial, documento=sin_publicar) == fv.ConteoFragmentos(1, 0)


# ===================================== float32 contra pgvector REAL =====================================

def _literal(valor: float) -> str:
    return "[" + ",".join([repr(valor)] * DIM) + "]"


@pytest.mark.parametrize("valor", [1e-20, 1e-19, 1e-10, 1.0, 1e10, 1e17])
async def test_lo_que_se_acepta_funciona_en_pgvector_con_similitud_finita(fabrica_vectorial, valor):
    vector = tuple([valor] * DIM)
    await guardar(fabrica_vectorial, lote([sintetico(0, "x")], [vector]))
    await publicar(fabrica_vectorial)
    hallados = await buscar(fabrica_vectorial, vector)
    assert len(hallados) == 1
    assert math.isfinite(hallados[0].similitud)
    assert hallados[0].similitud == pytest.approx(1.0, abs=1e-4)  # consigo mismo


async def test_un_vector_valido_que_sigue_siendo_no_nulo_tras_la_conversion_se_persiste_y_se_busca(fabrica_vectorial):
    vector = tuple([1.0] + [1e-50] * (DIM - 1))  # en float32 queda [1, 0, 0, ...]
    await guardar(fabrica_vectorial, lote([sintetico(0, "casi-cero")], [vector]))
    await publicar(fabrica_vectorial)
    almacenado = (await sql(fabrica_vectorial, "SELECT vector_norm(embedding) FROM fragmentos_documento"))[0][0]
    assert almacenado == pytest.approx(1.0)
    hallados = await buscar(fabrica_vectorial, vector)
    assert hallados[0].similitud == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("valor", [1e-50, 1e-30, 1e19, 0.0])
async def test_lo_que_se_rechaza_es_justamente_lo_que_pgvector_no_sabe_comparar(fabrica_vectorial, valor):
    # 1) pgvector real: el coseno de estos vectores consigo mismos es NaN (indefinido).
    distancia = (await sql(fabrica_vectorial, f"SELECT '{_literal(valor)}'::vector <=> '{_literal(valor)}'::vector"))[0][0]
    assert math.isnan(distancia)
    # 2) La aplicación los rechaza ANTES de persistir y de buscar, tanto como documento como como consulta.
    vector = tuple([valor] * DIM)
    valor_lote_5 = lote([sintetico(0, "x")], [vector])
    with pytest.raises(ValueError):
        await guardar(fabrica_vectorial, valor_lote_5)
    assert (await sql(fabrica_vectorial, "SELECT count(*) FROM fragmentos_documento"))[0][0] == 0
    with pytest.raises(ValueError):
        await buscar(fabrica_vectorial, vector)

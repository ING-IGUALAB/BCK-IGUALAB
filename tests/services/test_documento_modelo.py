"""Etapa 4A: modelo `Documento`, restricciones de BD y DDL de revisión.

Nivel: SQLite aislado en memoria (las restricciones CHECK y los índices únicos parciales
se ejecutan de verdad) y generación de DDL PostgreSQL SIN ejecutarlo. La concurrencia y
el comportamiento real de PostgreSQL se prueban en `test_documento_postgres.py`.
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Empresa, RolUsuario, SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import (
    Documento,
    EstadoCompensacion,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from scripts.generar_ddl_documentos import generar_ddl
from tests.ayudantes_ingesta import SesionSQLite, crear_empresa, crear_usuario

RAIZ = Path(__file__).resolve().parents[2]
AHORA = datetime.now(timezone.utc)


@pytest.fixture
async def entorno():
    db = SesionSQLite()
    usuario = await crear_usuario(db)
    empresa = await crear_empresa(db)
    yield db, usuario, empresa
    db.cerrar()


def documento(entorno, **cambios) -> Documento:
    _, usuario, empresa = entorno
    valores = dict(
        ambiente="development",
        empresa_id=empresa.id,
        anio=2025,
        tipo=TipoDocumento.MEMORIA_ANUAL,
        sector=empresa.sector,
        nombre_archivo="Memoria anual 2025.md",
        sha256="a" * 64,
        tamano_bytes=10,
        usuario_id=usuario.id,
        clave_original=f"development/documentos/{uuid.uuid4()}/original.md",
    )
    valores.update(cambios)
    return Documento(**valores)


async def guardar(entorno, **cambios) -> Documento:
    db = entorno[0]
    nuevo = documento(entorno, **cambios)
    db.add(nuevo)
    await db.commit()
    return nuevo


async def rechaza(entorno, **cambios):
    db = entorno[0]
    db.add(documento(entorno, **cambios))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# --- Metadatos, relaciones y estado inicial ---------------------------------------------------------

async def test_estado_inicial_y_valores_por_defecto(entorno):
    d = await guardar(entorno)
    assert isinstance(d.id, uuid.UUID)
    assert d.estado_procesamiento is EstadoProcesamiento.EN_PROCESO
    assert d.resultado_analisis is None  # análisis pendiente: NO es OBSERVADO
    assert d.estado_compensacion is EstadoCompensacion.NINGUNA and d.compensacion_intentos == 0
    assert d.reserva_activa is True
    assert d.completado_en is None and d.original_almacenado_en is None and d.almacenamiento_intentado_en is None
    assert d.motivo_fallo is None and d.version_id_original is None
    assert d.creado_en is not None
    assert d.disponible_para_rag is False


async def test_metadatos_persistidos_y_sector_de_la_empresa(entorno):
    _, usuario, empresa = entorno
    d = await guardar(entorno, tamano_bytes=1234, sha256="b" * 64)
    recargado = entorno[0].sync.get(Documento, d.id)
    assert (recargado.empresa_id, recargado.anio, recargado.tipo) == (empresa.id, 2025, TipoDocumento.MEMORIA_ANUAL)
    assert recargado.sector is empresa.sector is SectorEmpresa.MINERIA
    assert (recargado.nombre_archivo, recargado.sha256, recargado.tamano_bytes) == (
        "Memoria anual 2025.md", "b" * 64, 1234)
    assert recargado.usuario_id == usuario.id


def test_relaciones_con_empresa_y_cuenta_estan_declaradas():
    destinos = {(fk.parent.name, fk.column.table.name, fk.column.name) for fk in Documento.__table__.foreign_keys}
    assert destinos == {("empresa_id", "empresas", "id"), ("usuario_id", "usuarios", "id")}
    assert Documento.__table__.c.clave_original.unique is True  # sin URL ni credenciales: solo una clave
    assert not any("url" in c.name or "password" in c.name or "secret" in c.name for c in Documento.__table__.c)


def test_disponible_para_rag_requiere_completado_con_analisis():
    base = dict(estado_procesamiento=EstadoProcesamiento.EN_PROCESO, resultado_analisis=None)
    assert Documento(**base).disponible_para_rag is False
    assert Documento(estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=None).disponible_para_rag is False
    assert Documento(estado_procesamiento=EstadoProcesamiento.FALLIDO, resultado_analisis=None).disponible_para_rag is False
    for resultado in ResultadoAnalisis:
        completo = Documento(estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=resultado)
        assert completo.disponible_para_rag is True  # también OBSERVADO


# --- CHECK: invariantes impuestas por la BD ----------------------------------------------------------------

@pytest.mark.parametrize("cambios", [
    dict(resultado_analisis=ResultadoAnalisis.OBSERVADO),                 # OBSERVADO sin completar
    dict(resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS),
    dict(estado_procesamiento=EstadoProcesamiento.COMPLETADO, completado_en=AHORA,
         original_almacenado_en=AHORA),                                   # completado sin análisis
    dict(estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ResultadoAnalisis.OBSERVADO,
         original_almacenado_en=AHORA),                                   # sin completado_en
    dict(estado_procesamiento=EstadoProcesamiento.COMPLETADO, resultado_analisis=ResultadoAnalisis.OBSERVADO,
         completado_en=AHORA),                                            # sin original almacenado
    dict(estado_procesamiento=EstadoProcesamiento.FALLIDO),               # fallido sin motivo
    dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="X_ERR"),  # sin fecha
    dict(estado_compensacion=EstadoCompensacion.PENDIENTE),               # compensación sin fallo
    dict(estado_compensacion=EstadoCompensacion.COMPLETADA),
    dict(reserva_activa=False),                                           # liberar un EN_PROCESO
    dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR", fallido_en=AHORA,
         estado_compensacion=EstadoCompensacion.PENDIENTE, reserva_activa=False),  # liberar sin limpiar
    dict(sha256="a" * 63), dict(sha256="a" * 65), dict(tamano_bytes=0), dict(tamano_bytes=-1),
    dict(nombre_archivo="   "), dict(anio=1999), dict(compensacion_intentos=-1),
])
async def test_la_bd_rechaza_estados_incoherentes(entorno, cambios):
    await rechaza(entorno, **cambios)


async def test_estados_coherentes_si_se_aceptan(entorno):
    completo = dict(estado_procesamiento=EstadoProcesamiento.COMPLETADO, completado_en=AHORA, original_almacenado_en=AHORA)
    d1 = await guardar(entorno, sha256="1" * 64, tipo=TipoDocumento.MEMORIA_ANUAL, resultado_analisis=ResultadoAnalisis.OBSERVADO, **completo)
    d2 = await guardar(entorno, sha256="2" * 64, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI,
                       resultado_analisis=ResultadoAnalisis.CON_HALLAZGOS, **completo)
    fallido = dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR", fallido_en=AHORA)
    d3 = await guardar(entorno, sha256="3" * 64, anio=2024, estado_compensacion=EstadoCompensacion.COMPLETADA,
                       reserva_activa=False, **fallido)
    d4 = await guardar(entorno, sha256="4" * 64, anio=2023, estado_compensacion=EstadoCompensacion.PENDIENTE, **fallido)
    assert d1.disponible_para_rag and d2.disponible_para_rag and not d3.disponible_para_rag and not d4.disponible_para_rag


# --- Índices únicos parciales -----------------------------------------------------------------------------

async def test_sha256_duplicado_con_reserva_activa_se_rechaza(entorno):
    await guardar(entorno, sha256="c" * 64)
    await rechaza(entorno, sha256="c" * 64, anio=2024)  # otra empresa/año/tipo, mismo contenido


async def test_empresa_anio_tipo_duplicado_con_reserva_activa_se_rechaza(entorno):
    await guardar(entorno, sha256="d" * 64)
    await rechaza(entorno, sha256="e" * 64)  # mismo empresa/año/tipo, otro contenido


async def test_combinaciones_distintas_conviven(entorno):
    await guardar(entorno, sha256="1" * 64)
    await guardar(entorno, sha256="2" * 64, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI)
    await guardar(entorno, sha256="3" * 64, anio=2024)
    otra = await crear_empresa(entorno[0], nombre="Otra empresa", sector=SectorEmpresa.ENERGIA)
    await guardar(entorno, sha256="4" * 64, empresa_id=otra.id, sector=otra.sector)


async def test_el_mismo_contenido_puede_existir_en_otro_ambiente(entorno):
    await guardar(entorno, sha256="f" * 64)
    await guardar(entorno, sha256="f" * 64, ambiente="qa", clave_original=f"qa/documentos/{uuid.uuid4()}/original.md")


async def test_un_fallido_ya_compensado_no_ocupa_la_reserva_pero_uno_pendiente_si(entorno):
    fallido = dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR", fallido_en=AHORA)
    await guardar(entorno, sha256="9" * 64, estado_compensacion=EstadoCompensacion.COMPLETADA,
                  reserva_activa=False, **fallido)
    await guardar(entorno, sha256="9" * 64)                      # reintento: mismo contenido y combinación
    await rechaza(entorno, sha256="9" * 64, anio=2020)           # y ahora sí hay un activo
    await rechaza(entorno, sha256="8" * 64)                      # la combinación también está ocupada
    otro = dict(estado_compensacion=EstadoCompensacion.PENDIENTE, **fallido)
    await guardar(entorno, sha256="7" * 64, anio=2022, **otro)
    await rechaza(entorno, sha256="7" * 64, anio=2021)           # pendiente: sigue reservando el hash


async def test_se_permiten_muchos_intentos_fallidos_y_compensados_del_mismo_contenido(entorno):
    fallido = dict(estado_procesamiento=EstadoProcesamiento.FALLIDO, motivo_fallo="STORAGE_ERROR", fallido_en=AHORA,
                   estado_compensacion=EstadoCompensacion.COMPLETADA, reserva_activa=False)
    for _ in range(3):
        await guardar(entorno, sha256="6" * 64, **fallido)


# --- Integridad referencial en la BD aislada no aplica a SQLite: ver PostgreSQL -----------------------------


# --- DDL de revisión y no registro en el arranque ---------------------------------------------------------------

def test_el_ddl_de_revision_contiene_indices_parciales_checks_y_solo_tipos_nuevos():
    ddl = generar_ddl()
    assert "CREATE TABLE documentos" in ddl
    assert "CREATE UNIQUE INDEX uq_documentos_sha256_activo ON documentos (ambiente, sha256) WHERE reserva_activa" in ddl
    assert ("CREATE UNIQUE INDEX uq_documentos_empresa_anio_tipo_activo ON documentos "
            "(ambiente, empresa_id, anio, tipo) WHERE reserva_activa") in ddl
    for tipo in ("tipo_documento", "estado_procesamiento", "resultado_analisis", "estado_compensacion"):
        assert f"CREATE TYPE {tipo} AS ENUM" in ddl
    assert "CREATE TYPE sector_empresa" not in ddl  # ya existe por el módulo de empresas
    assert "ck_documentos_reserva_solo_liberada_si_limpio" in ddl
    assert "REFERENCES empresas (id)" in ddl and "REFERENCES usuarios (id)" in ddl
    assert "DROP" not in ddl and "ALTER" not in ddl and "password" not in ddl.lower()


def test_generar_el_ddl_no_abre_conexiones(monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.engine import Engine

    def prohibido(*args, **kwargs):
        raise AssertionError("no debe conectarse a ninguna base")

    monkeypatch.setattr(Engine, "connect", prohibido)
    assert "CREATE TABLE documentos" in generar_ddl()


def test_el_arranque_no_crea_documentos_ni_operaciones_aunque_la_aplicacion_cargue_sus_modelos():
    """`lifespan` ejecuta `crear_tablas` (D17). Desde que el router de documentos forma parte de la aplicación,
    `import app.main` SÍ registra `Documento` y `OperacionIngesta` en los metadatos (el router y el gestor los usan);
    antes no los cargaba y esta prueba exigía que NO estuvieran registradas. La garantía que importa se mantiene y se
    comprueba aquí en un proceso limpio: lo que ejecuta el arranque (`crear_tablas._crear_permitidas`) no crea ninguna de
    las dos tablas, y `app.models` sigue sin exportar `Documento`. La versión sobre PostgreSQL real está en
    `test_operaciones_ingesta_sql.py`."""
    codigo = (
        "import app.main\n"
        "from sqlalchemy import create_engine, inspect\n"
        "from app.database import Base\n"
        "import app.models as m\n"
        "from scripts import crear_tablas\n"
        "motor = create_engine('sqlite://')\n"
        "with motor.begin() as conexion:\n"
        "    crear_tablas._crear_permitidas(conexion)\n"
        "tablas = set(inspect(motor).get_table_names())\n"
        "print(sorted(t for t in ('documentos', 'operaciones_ingesta') if t in Base.metadata.tables), "
        "hasattr(m, 'Documento'), sorted(t for t in ('documentos', 'operaciones_ingesta') if t in tablas), "
        "{'usuarios', 'empresas', 'auditoria'} <= tablas)\n"
    )
    entorno = {**os.environ, "JWT_SECRET_KEY": "clave-solo-para-esta-prueba", "PYTHONPATH": str(RAIZ)}
    resultado = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ, env=entorno, capture_output=True, text=True, timeout=120
    )
    assert resultado.returncode == 0, resultado.stderr[-500:]
    # registradas en los metadatos: sí; exportadas por app.models: no; creadas por el arranque: ninguna.
    assert resultado.stdout.strip().splitlines()[-1] == "['documentos', 'operaciones_ingesta'] False [] True"

"""Imprime el DDL PostgreSQL de las tablas de ingesta generado desde los MODELOS, para REVISIÓN y para las pruebas.

Las tablas NO se crean con este script: las crean las migraciones versionadas de `app/migraciones` al arrancar la aplicación.
Este DDL es la referencia contra la que las pruebas comprueban que las migraciones (0001 + 0002 de la base transaccional) dejan
EXACTAMENTE el esquema de los modelos, y `generar_ddl_operaciones()` produce el texto de la migración 0003
(`app/migraciones/transaccional/0003_operaciones_ingesta.sql`; una prueba comprueba que coincide).

NO se conecta a ninguna base de datos ni ejecuta nada: solo escribe SQL en la salida estándar.

Uso (PowerShell, desde la raíz del repositorio):
    .venv/Scripts/python.exe -m scripts.generar_ddl_documentos > documentos.sql
    .venv/Scripts/python.exe -m scripts.generar_ddl_documentos --operaciones > operaciones.sql
"""
import os

# `app.config` exige JWT_SECRET_KEY al importarse; este script no la usa.
os.environ.setdefault("JWT_SECRET_KEY", "valor-ficticio-solo-para-generar-ddl")

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.dialects.postgresql.base import CreateEnumType  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.models.documento_ingesta import Documento, OperacionIngesta  # noqa: E402

# Ya existe por el módulo de empresas; no se vuelve a crear.
TIPOS_EXISTENTES = {"sector_empresa"}


def generar_ddl() -> str:
    dialecto = postgresql.dialect()
    tabla = Documento.__table__
    sentencias = [
        "\n".join(
            [
                "-- Documentos de ingesta (Etapa 4A + coordinador): DDL de referencia generado desde el modelo.",
                "-- Lo crean las migraciones 0001 y 0002 de app/migraciones/transaccional (no se ejecuta a mano).",
                "-- Requiere las tablas `empresas` y `usuarios` y el tipo `sector_empresa` (ya existentes).",
            ]
        )
    ]
    tipos_vistos: set[str] = set()
    for columna in tabla.columns:
        tipo = getattr(columna.type, "name", None)
        if columna.type.__class__.__name__ != "Enum" or tipo in TIPOS_EXISTENTES or tipo in tipos_vistos:
            continue
        tipos_vistos.add(tipo)
        sentencias.append(str(CreateEnumType(columna.type.adapt(postgresql.ENUM)).compile(dialect=dialecto)).strip() + ";")
    sentencias.append(str(CreateTable(tabla).compile(dialect=dialecto)).strip() + ";")
    for indice in sorted(tabla.indexes, key=lambda i: i.name):
        sentencias.append(str(CreateIndex(indice).compile(dialect=dialecto)).strip() + ";")
    return "\n\n".join(sentencias) + "\n"


def generar_ddl_operaciones() -> str:
    """DDL de `operaciones_ingesta` (identificador de operación entregado antes de cargar el archivo).
    Texto de la migración `app/migraciones/transaccional/0003_operaciones_ingesta.sql`, que va DESPUÉS de `documentos`: la tabla
    referencia `usuarios` y `documentos`. Sin tipos enumerados nuevos."""
    dialecto = postgresql.dialect()
    tabla = OperacionIngesta.__table__
    sentencias = [
        "\n".join(
            [
                "-- Migración 0003 (transaccional): operaciones de ingesta (identificador entregado ANTES de cargar el archivo).",
                "-- La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta a mano.",
                "-- Requiere las tablas `usuarios` y `documentos`. Sin tipos enumerados nuevos: el estado es VARCHAR con CHECK.",
            ]
        ),
        str(CreateTable(tabla).compile(dialect=dialecto)).strip() + ";",
    ]
    for indice in sorted(tabla.indexes, key=lambda i: i.name):
        sentencias.append(str(CreateIndex(indice).compile(dialect=dialecto)).strip() + ";")
    return "\n\n".join(sentencias) + "\n"


if __name__ == "__main__":
    import sys

    print(generar_ddl_operaciones() if "--operaciones" in sys.argv[1:] else generar_ddl())

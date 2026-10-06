"""Imprime el DDL PostgreSQL de la tabla `documentos` para REVISIÓN (Etapa 4A).

NO se conecta a ninguna base de datos ni ejecuta nada: solo escribe SQL en la salida
estándar. No hay un mecanismo de migraciones aprobado (D17) y el arranque no crea
esta tabla (el modelo no está registrado en `app.models`). Quien administre cada base
debe revisar y aplicar el SQL con el procedimiento que se acuerde; ver
`docs/ingesta/07-almacenamiento-minio.md`.

Uso (PowerShell, desde la raíz del repositorio):
    .venv\\Scripts\\python.exe -m scripts.generar_ddl_documentos > documentos.sql
"""
import os

# `app.config` exige JWT_SECRET_KEY al importarse; este script no la usa.
os.environ.setdefault("JWT_SECRET_KEY", "valor-ficticio-solo-para-generar-ddl")

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.dialects.postgresql.base import CreateEnumType  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.models.documento_ingesta import Documento  # noqa: E402

# Ya existe por el módulo de empresas; no se vuelve a crear.
TIPOS_EXISTENTES = {"sector_empresa"}


def generar_ddl() -> str:
    dialecto = postgresql.dialect()
    tabla = Documento.__table__
    sentencias = [
        "-- Etapa 4A: documentos de ingesta. DDL PARA REVISIÓN: no se ejecuta desde la aplicación.",
        "-- Requiere las tablas `empresas` y `usuarios` y el tipo `sector_empresa` (ya existentes).",
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


if __name__ == "__main__":
    print(generar_ddl())

# --- Etapa 1: build — instala dependencias en un entorno aislado ---
# Base Alpine: es la opción con Python 3.13 cuyos paquetes del sistema tienen correcciones disponibles (Debian 13 deja sin corregir cientos de
# hallazgos de libc, libxml2, util-linux...). Ver ci/README.md (comparación de bases) y el control Trivy del Jenkinsfile.
FROM python:3.13-alpine AS build

WORKDIR /app

# Compiladores y cabeceras SOLO en esta etapa (no pasan a la imagen final): por si alguna dependencia no publica rueda musllinux.
RUN apk add --no-cache gcc musl-dev libffi-dev postgresql-dev

# Usa SOLO requirements.txt (producción) — nunca requirements-dev.txt,
# para que pytest/httpx no terminen en la imagen final.
COPY requirements.txt .
# --no-compile: pip no genera __pycache__/*.pyc al instalar. Son ~22 000 de los ~48 000 archivos que luego copia `COPY --from=build`
# (17 000 solo del SDK de OCI) y no se necesitan: Python regenera el bytecode al importar. Menos archivos que copiar entre etapas.
RUN pip install --user --no-cache-dir --no-compile -r requirements.txt


# --- Etapa 2: producción — imagen final, sin herramientas de compilación ni pip ---
FROM python:3.13-alpine

WORKDIR /app

# apk upgrade: parches de seguridad publicados después de construir la imagen base.
# libpq: solo la librería de conexión de PostgreSQL (asyncpg no la necesita, pero se conserva por paridad con la imagen anterior).
# pango/harfbuzz/cairo/gdk-pixbuf/...: runtime de WeasyPrint (M7 reportes PDF).
# pip, setuptools y wheel NO se usan en ejecución (las dependencias ya están instaladas): se desinstalan de verdad, con sus archivos y los
# paquetes que vendorizan, y se retira la rueda de pip que trae ensurepip. No se oculta nada: se elimina el software que no hace falta.
RUN apk upgrade --no-cache \
    && apk add --no-cache libpq pango harfbuzz cairo gdk-pixbuf libffi shared-mime-info font-dejavu \
    && python -m pip uninstall -y pip setuptools wheel \
    && rm -rf /usr/local/lib/python3.13/ensurepip/_bundled

COPY --from=build /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Código de la aplicación. tests/ queda fuera a propósito (ver .dockerignore).
COPY app/ ./app/
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

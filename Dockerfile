# --- Etapa 1: build — instala dependencias en un entorno aislado ---
FROM python:3.11-slim AS build

WORKDIR /app

# libpq-dev: necesario para compilar asyncpg (driver de PostgreSQL).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Usa SOLO requirements.txt (producción) — nunca requirements-dev.txt,
# para que pytest/httpx no terminen en la imagen final.
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# --- Etapa 2: producción — imagen final, sin herramientas de compilación ---
FROM python:3.11-slim

WORKDIR /app

# libpq5: solo la librería de conexión en tiempo de ejecución.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Código de la aplicación. tests/ queda fuera a propósito (ver .dockerignore).
COPY app/ ./app/
COPY scripts/ ./scripts/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

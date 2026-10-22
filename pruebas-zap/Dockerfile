# Preparación y cierre del ZAP API Scan: Node 22 (mismo digest que pruebas-http) y SOLO la biblioteca estándar, sin npm ni dependencias.
# El código se copia a la imagen; los contenedores escriben únicamente en los directorios montados (trabajo, privado, resultados).
FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

ENV NODE_ENV=production \
    NODE_OPTIONS=--no-deprecation \
    HOME=/tmp

WORKDIR /opt/pruebas-zap

COPY alcance ./alcance
COPY reglas ./reglas
COPY hooks ./hooks
COPY scripts/preparar.mjs scripts/finalizar.mjs ./scripts/
COPY scripts/lib ./scripts/lib

# El usuario lo fija ejecutar-zap.sh (-u uid:gid).
CMD ["node", "-e", "console.log('Uso: node scripts/preparar.mjs | scripts/finalizar.mjs (ver README.md)')"]

# Imagen reproducible para ejecutar las colecciones: Node y Newman con versión FIJA (package-lock.json) y sin depender de
# nada instalado en el servidor. El código de pruebas se copia a la imagen: el contenedor solo escribe sus reportes.
FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

ENV NODE_ENV=production \
    NODE_OPTIONS=--no-deprecation \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

WORKDIR /opt/pruebas-http

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

COPY colecciones ./colecciones
COPY muestras ./muestras
COPY scripts ./scripts

# El usuario lo fija Jenkins (-u uid:gid) o `docker run`; el ejecutor solo escribe en /tmp y en el directorio de resultados.
ENV HOME=/tmp
CMD ["node", "-e", "console.log('Uso: node scripts/ejecutar.mjs --coleccion integracion|e2e --entorno local|qa|uat (ver README.md)')"]

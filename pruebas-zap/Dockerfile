# Preparación y cierre del ZAP API Scan: Node 22 (mismo digest que pruebas-http) y SOLO la biblioteca estándar, sin npm ni dependencias.
# El código se copia a la imagen; los contenedores escriben únicamente en los volúmenes /trabajo, /privado y /resultados.
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

# 1) El código debe ser legible por un usuario no root aunque el checkout de Jenkins use un umask restrictivo (COPY conserva los modos).
# 2) Los directorios de datos de la ejecución son del usuario 1000:1000 (el usuario `node` de esta imagen y el `zap` del contenedor de
#    ZAP, que debe leer las credenciales y escribir los reportes). Se montan como VOLÚMENES NOMBRADOS: Docker los inicializa con el
#    propietario y los permisos de estos directorios, sin depender del propietario de ninguna carpeta del anfitrión de Jenkins.
#    /privado (estado y credenciales) queda en 0700: solo ese usuario.
RUN chmod -R a+rX /opt/pruebas-zap \
 && mkdir -p /trabajo /privado /resultados \
 && chown 1000:1000 /trabajo /privado /resultados \
 && chmod 0750 /trabajo /resultados \
 && chmod 0700 /privado

USER 1000:1000

CMD ["node", "-e", "console.log('Uso: node scripts/preparar.mjs | scripts/finalizar.mjs (ver README.md)')"]

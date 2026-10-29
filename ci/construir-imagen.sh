#!/bin/sh
# Construye UNA sola vez la imagen del backend que se analizará con Trivy y, si se aprueba, se desplegará.
#
# La identifica de forma inequívoca por ejecución (repositorio por ambiente + número de build + commit) y registra su ID en
# ci-estado/imagen.env. Los stages posteriores usan ese ID: no se vuelve a construir ni a descargar ninguna imagen después del análisis.
#
# Entrada: AMBIENTE (development | qa | uat) y NUMERO_BUILD.
# No necesita ni toca el .env: .dockerignore lo excluye del contexto, así que ningún secreto entra en la imagen.
set -eu

: "${AMBIENTE:?Falta AMBIENTE}"
: "${NUMERO_BUILD:?Falta NUMERO_BUILD}"
case "$AMBIENTE" in
    development | qa | uat) ;;
    *) echo "ERROR: AMBIENTE no reconocido: $AMBIENTE" >&2; exit 1 ;;
esac
case "$NUMERO_BUILD" in
    '' | *[!0-9]*) echo "ERROR: NUMERO_BUILD debe ser numérico." >&2; exit 1 ;;
esac

REPOSITORIO="igualab-backend-$AMBIENTE"
COMMIT="$(git rev-parse --short=12 HEAD)"
IMAGEN_REF="$REPOSITORIO:b$NUMERO_BUILD-$COMMIT"

# Limpieza acotada: solo etiquetas de ESTE repositorio de imágenes (las de ejecuciones anteriores). Si una sigue en uso por el
# contenedor desplegado, Docker se niega a borrarla y se conserva. No se tocan otras imágenes, contenedores, volúmenes ni cachés.
for previa in $(docker image ls --filter "reference=$REPOSITORIO" --format '{{.Repository}}:{{.Tag}}'); do
    if [ "$previa" != "$IMAGEN_REF" ]; then
        if docker image rm "$previa" > /dev/null 2>&1; then
            echo "Imagen anterior retirada: $previa"
        else
            echo "Imagen anterior conservada (en uso): $previa"
        fi
    fi
done

# --pull: parte de la base oficial más reciente de la etiqueta (parches de seguridad) antes del análisis.
docker build --pull \
    --label "org.opencontainers.image.revision=$COMMIT" \
    --label "igualab.jenkins.build=$NUMERO_BUILD" \
    -t "$IMAGEN_REF" .

IMAGEN_ID="$(docker image inspect --format '{{.Id}}' "$IMAGEN_REF")"
case "$IMAGEN_ID" in
    sha256:????????????????????????????????????????????????????????????????) ;;
    *) echo "ERROR: ID de imagen inesperado: $IMAGEN_ID" >&2; exit 1 ;;
esac

mkdir -p ci-estado
printf 'IMAGEN_REF=%s\nIMAGEN_ID=%s\n' "$IMAGEN_REF" "$IMAGEN_ID" > ci-estado/imagen.env
echo "Imagen construida: $IMAGEN_REF"
echo "ID de la imagen:   $IMAGEN_ID"

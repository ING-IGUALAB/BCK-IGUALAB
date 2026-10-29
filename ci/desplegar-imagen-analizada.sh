#!/bin/sh
# Despliega con Docker Compose EXACTAMENTE la imagen que construyó ci/construir-imagen.sh y aprobó el stage «Trivy».
#
# Es la única operación que detiene o reemplaza el servicio actual (`down`) y solo se alcanza si Trivy aprobó: un bloqueo o un error
# técnico del análisis detiene el pipeline antes y deja funcionando el despliegue anterior.
# No construye ni descarga ninguna imagen: `up --no-build --pull never` falla si la imagen analizada ya no existe.
#
# Entrada: COMPOSE_PROJECT, IMAGEN_REF e IMAGEN_ID (ci-estado/imagen.env). Requiere el .env del ambiente ya presente.
set -eu

: "${COMPOSE_PROJECT:?Falta COMPOSE_PROJECT}"
: "${IMAGEN_REF:?Falta IMAGEN_REF}"
: "${IMAGEN_ID:?Falta IMAGEN_ID}"

# 1. La referencia sigue apuntando a la imagen que se analizó.
id_actual="$(docker image inspect --format '{{.Id}}' "$IMAGEN_REF")" || {
    echo "ERROR: la imagen analizada $IMAGEN_REF ya no existe en el agente; no se despliega." >&2
    exit 1
}
if [ "$id_actual" != "$IMAGEN_ID" ]; then
    echo "ERROR: $IMAGEN_REF apunta a $id_actual, no a la imagen analizada $IMAGEN_ID; no se despliega." >&2
    exit 1
fi

# 2. Reemplazo del servicio con esa imagen (Compose la toma de BACKEND_IMAGE).
BACKEND_IMAGE="$IMAGEN_REF"
export BACKEND_IMAGE
docker compose -p "$COMPOSE_PROJECT" down
docker compose -p "$COMPOSE_PROJECT" up -d --no-build --pull never

# 3. El contenedor en ejecución usa la imagen analizada.
contenedor="$(docker compose -p "$COMPOSE_PROJECT" ps -q backend)"
if [ -z "$contenedor" ]; then
    echo "ERROR: no se encontró el contenedor del backend tras el despliegue." >&2
    exit 1
fi
id_en_uso="$(docker inspect --format '{{.Image}}' "$contenedor")"
if [ "$id_en_uso" != "$IMAGEN_ID" ]; then
    echo "ERROR: el contenedor usa $id_en_uso y no la imagen analizada $IMAGEN_ID." >&2
    exit 1
fi
echo "Desplegada la imagen analizada: $IMAGEN_REF ($IMAGEN_ID)"

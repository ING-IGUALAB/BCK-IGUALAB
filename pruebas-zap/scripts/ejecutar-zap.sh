#!/bin/sh
# Orquestador del stage «OWASP ZAP» (Jenkins, rama qa). POSIX sh: solo requiere Docker en el nodo.
#
#   sh pruebas-zap/scripts/ejecutar-zap.sh            ejecuta el análisis (ver pruebas-zap/README.md)
#   sh pruebas-zap/scripts/ejecutar-zap.sh limpiar    elimina SOLO el contenedor de ZAP de esta ejecución y completa el cierre sintético si faltó
#
# Variables (Jenkins las define; los secretos llegan por entorno, nunca por argumentos):
#   IGUALAB_API_URL, IGUALAB_E2E_SUPERADMIN_CORREO, IGUALAB_E2E_SUPERADMIN_PASSWORD, IGUALAB_PRUEBAS_ENTORNO=qa, BRANCH_NAME
# Opcionales: ZAP_ID_EJECUCION (por defecto BUILD_TAG), ZAP_LIMITE_MINUTOS (80), ZAP_ASCAN_MINUTOS (60), ZAP_REGLA_MINUTOS (5),
#   ZAP_HILOS (3), ZAP_RETARDO_MS (0), ZAP_RED (proxy_net), ZAP_PROYECTO_COMPOSE_QA (igualab-backend-qa).
#
# Códigos de salida (0 y 2 los trata Jenkins como éxito e INESTABLE; cualquier otro falla el build): ver lib/codigos.mjs.
#   0 ok · 1 reglas FAIL · 2 advertencias · 3 error de ZAP · 4 configuración · 5 backend no disponible · 6 tiempo agotado
#   7 reportes · 8 secretos · 9 evidencia (autenticación/análisis activo) · 10 limpieza · 11 alcance
set -u

MODO="${1:-ejecutar}"
RAIZ=$(cd "$(dirname "$0")/.." && pwd)
BASE="${ZAP_DIR_BASE:-$(cd "$RAIZ/.." && pwd)}"

# Imagen oficial FIJADA por versión Y digest (índice multi-arquitectura, verificado contra el registro de Docker Hub).
ZAP_IMAGEN='zaproxy/zap-stable:2.17.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6'
IMAGEN_PRUEBAS='igualab-pruebas-zap:ci'
RED="${ZAP_RED:-proxy_net}"
PROYECTO_QA="${ZAP_PROYECTO_COMPOSE_QA:-igualab-backend-qa}"

ID_EJECUCION=$(printf '%s' "${ZAP_ID_EJECUCION:-${BUILD_TAG:-local-$(date -u +%Y%m%d%H%M%S)}}" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_.-' '-' | cut -c1-60)
CONTENEDOR="igualab-zap-$ID_EJECUCION"

TRABAJO="$BASE/zap-trabajo"      # entrada y salida de ZAP; NO se archiva (los reportes crudos pueden tener secretos)
PRIVADO="$BASE/zap-privado"      # tokens y estado efímeros; NO se archiva
RESULTADOS="$BASE/zap-resultados" # lo único que Jenkins archiva (redactado y verificado)

LIMITE_MIN="${ZAP_LIMITE_MINUTOS:-80}"
LIMITE_SEG="${ZAP_LIMITE_SEGUNDOS:-$((LIMITE_MIN * 60))}"
ASCAN_MIN="${ZAP_ASCAN_MINUTOS:-60}"
REGLA_MIN="${ZAP_REGLA_MINUTOS:-5}"
HILOS="${ZAP_HILOS:-3}"
RETARDO_MS="${ZAP_RETARDO_MS:-0}"
SONDEO="${ZAP_SONDEO_SEGUNDOS:-5}"

log() { printf '%s\n' "$*"; }

# Quita JWT y cabeceras de autorización de cualquier texto que pase por consola o por el log archivado.
redactar_flujo() {
    sed -E \
        -e 's/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/[JWT-REDACTADO]/g' \
        -e 's/([Bb]earer[[:space:]]+)[A-Za-z0-9._~+\/=-]{12,}/\1[REDACTADO]/g' \
        -e 's/([Aa]uthorization:[[:space:]]*).*/\1[REDACTADO]/'
}

# Elimina únicamente el contenedor creado por ESTA ejecución: se comprueba la etiqueta antes de tocarlo.
limpiar_contenedor() {
    if docker inspect "$CONTENEDOR" >/dev/null 2>&1; then
        etiqueta=$(docker inspect -f '{{ index .Config.Labels "igualab.zap.ejecucion" }}' "$CONTENEDOR" 2>/dev/null)
        if [ "$etiqueta" = "$ID_EJECUCION" ]; then
            log "Eliminando el contenedor de ZAP de esta ejecución ($CONTENEDOR)."
            docker rm -f "$CONTENEDOR" >/dev/null 2>&1
        else
            log "El contenedor $CONTENEDOR no lleva la etiqueta de esta ejecución: no se toca."
        fi
    fi
}

UID_GID="$(id -u):$(id -g)"
ENTORNO_DOCKER="-e IGUALAB_API_URL -e IGUALAB_E2E_SUPERADMIN_CORREO -e IGUALAB_E2E_SUPERADMIN_PASSWORD -e IGUALAB_PRUEBAS_ENTORNO -e BRANCH_NAME -e JENKINS_URL -e BUILD_NUMBER -e ZAP_CONFIRMO_DESTINO_QA -e ZAP_ESPERA_SEGUNDOS"

# Modo `limpiar` (post del stage, también tras cancelación o timeout): elimina el contenedor de ZAP de esta ejecución y, si el cierre
# normal no llegó a cerrar la cuenta y la empresa sintéticas, lo ejecuta ahora. Es idempotente y no toca nada más.
if [ "$MODO" = "limpiar" ]; then
    limpiar_contenedor
    if [ -f "$PRIVADO/estado.json" ] && [ ! -f "$TRABAJO/.cierre-completo" ]; then
        log "Cierre de emergencia de los datos sintéticos (el cierre normal no se completó)."
        mkdir -p "$RESULTADOS"
        docker run --rm --network "$RED" -u "$UID_GID" $ENTORNO_DOCKER             -v "$TRABAJO:/trabajo" -v "$PRIVADO:/privado" -v "$RESULTADOS:/resultados"             "$IMAGEN_PRUEBAS" node scripts/finalizar.mjs --trabajo /trabajo --privado /privado --resultados /resultados --id "$ID_EJECUCION"             --prep-codigo 0 --zap-codigo ninguno --tiempo-agotado 2 --limite-minutos "$((LIMITE_SEG / 60))"             --imagen "$ZAP_IMAGEN" --comando "cierre de emergencia" 2>&1 | redactar_flujo
    fi
    exit 0
fi

# Una interrupción (cancelación o timeout de Jenkins → TERM/INT) elimina el contenedor de ZAP de esta ejecución y nada más.
trap limpiar_contenedor EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

case "$MODO" in ejecutar) ;; *) log "Modo desconocido: $MODO"; exit 4 ;; esac

for d in "$TRABAJO" "$PRIVADO" "$RESULTADOS"; do
    [ -n "$d" ] && [ "$d" != "/" ] && rm -rf "$d"
done
mkdir -p "$TRABAJO" "$PRIVADO" "$RESULTADOS"
chmod 777 "$TRABAJO"   # ZAP corre con otro usuario dentro del contenedor y escribe aquí sus reportes
chmod 755 "$PRIVADO" "$RESULTADOS"
CONSOLA="$RESULTADOS/consola-zap.log"
: > "$CONSOLA"

registrar() { printf '%s\n' "$*" | redactar_flujo | tee -a "$CONSOLA"; }

registrar "== OWASP ZAP · ejecución $ID_EJECUCION · imagen $ZAP_IMAGEN =="
registrar "Límite global de ZAP: ${LIMITE_SEG} s · análisis activo máx. ${ASCAN_MIN} min (por regla ${REGLA_MIN} min) · hilos ${HILOS} · retardo ${RETARDO_MS} ms"

PREP_CODIGO=0
if ! docker build -q -t "$IMAGEN_PRUEBAS" "$RAIZ" >/dev/null; then
    registrar "ERROR: no se pudo construir la imagen de preparación (pruebas-zap/Dockerfile)."
    exit 3
fi
if ! docker pull -q "$ZAP_IMAGEN" >/dev/null; then
    registrar "ERROR: no se pudo descargar la imagen de ZAP fijada por digest ($ZAP_IMAGEN). Docker valida el contenido contra el digest."
    exit 3
fi

# --- Destino: debe ser el backend QA y estar en la red indicada. Si el host es un contenedor local se comprueba su proyecto Compose.
HOST=$(printf '%s' "${IGUALAB_API_URL:-}" | sed -E 's#^[A-Za-z]+://([^/:]+).*#\1#')
if ! docker network inspect "$RED" >/dev/null 2>&1; then
    registrar "ERROR: la red de Docker '$RED' no existe en este nodo."
    PREP_CODIGO=4
elif [ -n "$HOST" ] && docker inspect "$HOST" >/dev/null 2>&1; then
    PROYECTO=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$HOST" 2>/dev/null)
    REDES=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$HOST" 2>/dev/null)
    if [ "$PROYECTO" != "$PROYECTO_QA" ]; then
        registrar "ERROR: el contenedor '$HOST' pertenece al proyecto Compose '${PROYECTO:-desconocido}', no a '$PROYECTO_QA'. No se escanea."
        PREP_CODIGO=4
    else
        case " $REDES " in
            *" $RED "*) registrar "Destino verificado: contenedor '$HOST' del proyecto '$PROYECTO' conectado a '$RED'." ;;
            *) registrar "ERROR: el contenedor '$HOST' no está conectado a la red '$RED'."; PREP_CODIGO=4 ;;
        esac
    fi
else
    registrar "Aviso: '$HOST' no es un contenedor de este nodo; la identificación de QA se basa en el nombre del host (o en ZAP_CONFIRMO_DESTINO_QA)."
fi

if [ "$PREP_CODIGO" -eq 0 ]; then
    registrar "--- Preparación (alcance, cuentas sintéticas, especificación filtrada) ---"
    SALIDA_PREP="$TRABAJO/.preparacion.log"
    # shellcheck disable=SC2086
    docker run --rm --network "$RED" -u "$UID_GID" $ENTORNO_DOCKER \
        -v "$TRABAJO:/trabajo" -v "$PRIVADO:/privado" \
        "$IMAGEN_PRUEBAS" node scripts/preparar.mjs --trabajo /trabajo --privado /privado --id "$ID_EJECUCION" > "$SALIDA_PREP" 2>&1
    PREP_CODIGO=$?
    redactar_flujo < "$SALIDA_PREP" | tee -a "$CONSOLA"
    rm -f "$SALIDA_PREP"
fi

COMANDO_ZAP="zap-api-scan.py -t openapi-filtrado.json -f openapi -c zap-api-scan.reglas.conf -r backend-zap.html -J backend-zap.json --hook=/zap/wrk/zap_hooks.py -T 10 -z \"-config scanner.threadPerHost=$HILOS -config scanner.delayInMs=$RETARDO_MS -config scanner.maxScanDurationInMins=$ASCAN_MIN -config scanner.maxRuleDurationInMins=$REGLA_MIN\""
ZAP_CODIGO=ninguno
TIEMPO_AGOTADO=0

if [ "$PREP_CODIGO" -eq 0 ]; then
    registrar "--- ZAP API Scan (análisis ACTIVO habilitado; no se usa -S) ---"
    registrar "Comando: $COMANDO_ZAP"
    if ! docker run -d --name "$CONTENEDOR" --network "$RED" --label "igualab.zap.ejecucion=$ID_EJECUCION" \
        -v "$TRABAJO:/zap/wrk:rw" -v "$PRIVADO:/zap/privado:ro" \
        "$ZAP_IMAGEN" zap-api-scan.py -t openapi-filtrado.json -f openapi -c zap-api-scan.reglas.conf \
        -r backend-zap.html -J backend-zap.json --hook=/zap/wrk/zap_hooks.py -T 10 \
        -z "-config scanner.threadPerHost=$HILOS -config scanner.delayInMs=$RETARDO_MS -config scanner.maxScanDurationInMins=$ASCAN_MIN -config scanner.maxRuleDurationInMins=$REGLA_MIN" \
        >/dev/null; then
        registrar "ERROR: no se pudo iniciar el contenedor de ZAP."
        ZAP_CODIGO=3
    else
        # Progreso en vivo (filtrado). El código de salida sale del contenedor, no de esta tubería.
        docker logs -f "$CONTENEDOR" 2>&1 | redactar_flujo | tee -a "$CONSOLA" &
        PID_LOGS=$!
        FIN=$(($(date +%s) + LIMITE_SEG))
        while :; do
            CORRIENDO=$(docker inspect -f '{{.State.Running}}' "$CONTENEDOR" 2>/dev/null) || CORRIENDO=false
            [ "$CORRIENDO" = "true" ] || break
            if [ "$(date +%s)" -ge "$FIN" ]; then
                TIEMPO_AGOTADO=1
                break
            fi
            sleep "$SONDEO"
        done
        if [ "$TIEMPO_AGOTADO" -eq 1 ]; then
            registrar "ERROR: se superó el límite global de ${LIMITE_SEG} s. Se detiene y elimina el contenedor de ZAP de esta ejecución."
            limpiar_contenedor
            ZAP_CODIGO=124
        else
            ZAP_CODIGO=$(docker inspect -f '{{.State.ExitCode}}' "$CONTENEDOR" 2>/dev/null) || ZAP_CODIGO=3
            docker rm "$CONTENEDOR" >/dev/null 2>&1
        fi
        wait "$PID_LOGS"
        registrar "ZAP terminó con el código $ZAP_CODIGO (0 éxito · 1 reglas FAIL · 2 advertencias · otro: error técnico)."
        # Los archivos creados por ZAP pertenecen a su usuario: se abren a todos para que Jenkins pueda borrar el espacio de trabajo.
        docker run --rm --entrypoint find -v "$TRABAJO:/zap/wrk" "$ZAP_IMAGEN" /zap/wrk -user zap -exec chmod a+rwX {} + >/dev/null 2>&1 \
            || registrar "Aviso: no se pudieron normalizar los permisos de $TRABAJO (el deleteDir() final podría fallar)."
    fi
fi

registrar "--- Cierre, reportes y veredicto ---"
SALIDA_FIN="$TRABAJO/.cierre.log"
# shellcheck disable=SC2086
docker run --rm --network "$RED" -u "$UID_GID" $ENTORNO_DOCKER \
    -v "$TRABAJO:/trabajo" -v "$PRIVADO:/privado" -v "$RESULTADOS:/resultados" \
    "$IMAGEN_PRUEBAS" node scripts/finalizar.mjs --trabajo /trabajo --privado /privado --resultados /resultados --id "$ID_EJECUCION" \
    --prep-codigo "$PREP_CODIGO" --zap-codigo "$ZAP_CODIGO" --tiempo-agotado "$TIEMPO_AGOTADO" --limite-minutos "$((LIMITE_SEG / 60))" \
    --imagen "$ZAP_IMAGEN" --comando "$COMANDO_ZAP" > "$SALIDA_FIN" 2>&1
RC=$?
redactar_flujo < "$SALIDA_FIN" | tee -a "$CONSOLA"
rm -f "$SALIDA_FIN"

# Solo 0..11 son códigos del ejecutor; cualquier otro (p. ej. docker run falló) es un error técnico.
case "$RC" in 0|1|2|3|4|5|6|7|8|9|10|11) ;; *) registrar "ERROR: el cierre terminó con un código inesperado ($RC)."; RC=3 ;; esac
registrar "== OWASP ZAP · código de salida $RC =="
exit "$RC"

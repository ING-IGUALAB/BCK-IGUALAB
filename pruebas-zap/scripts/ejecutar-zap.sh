#!/bin/sh
# Orquestador del stage «OWASP ZAP» (Jenkins, rama qa). POSIX sh: solo requiere Docker en el nodo.
#
#   sh pruebas-zap/scripts/ejecutar-zap.sh            ejecuta el análisis (ver pruebas-zap/README.md)
#   sh pruebas-zap/scripts/ejecutar-zap.sh limpiar    elimina SOLO el contenedor de ZAP de esta ejecución, completa el cierre sintético si
#                                                     faltó y libera los volúmenes de datos (idempotente)
#
# Variables (Jenkins las define; los secretos llegan por entorno, nunca por argumentos):
#   IGUALAB_API_URL, IGUALAB_E2E_SUPERADMIN_CORREO, IGUALAB_E2E_SUPERADMIN_PASSWORD, IGUALAB_PRUEBAS_ENTORNO=qa, BRANCH_NAME
# Opcionales: ZAP_ID_EJECUCION (por defecto BUILD_TAG), ZAP_LIMITE_MINUTOS (80), ZAP_ASCAN_MINUTOS (60), ZAP_REGLA_MINUTOS (5),
#   ZAP_HILOS (3), ZAP_RETARDO_MS (0), ZAP_RED (proxy_net), ZAP_PROYECTO_COMPOSE_QA (igualab-backend-qa).
#
# DATOS DE LA EJECUCIÓN. Estado, credenciales y reportes crudos viven en tres VOLÚMENES NOMBRADOS de Docker (trabajo, privado y
# resultados), no en carpetas del espacio de trabajo de Jenkins. Los volúmenes heredan de la imagen de pruebas-zap el propietario
# 1000:1000 (el usuario `node` de esa imagen y el `zap` del contenedor de ZAP) y el modo (privado 0700), así que ningún contenedor
# necesita root ni chmod 777 y el resultado no depende de quién sea el dueño de las carpetas del nodo (uid de Jenkins, user namespaces,
# SELinux, Jenkins dentro de un contenedor…). Al anfitrión solo vuelve, con `docker cp`, la carpeta zap-resultados/ ya redactada.
#
# Códigos de salida (0 y 2 los trata Jenkins como éxito e INESTABLE; cualquier otro falla el build): ver lib/codigos.mjs.
#   0 ok · 1 reglas FAIL · 2 advertencias · 3 error de ZAP · 4 configuración · 5 backend no disponible · 6 tiempo agotado
#   7 reportes · 8 secretos · 9 evidencia (autenticación/análisis activo) · 10 limpieza · 11 alcance · 12 archivos/permisos
set -u

MODO="${1:-ejecutar}"
RAIZ=$(cd "$(dirname "$0")/.." && pwd)
BASE="${ZAP_DIR_BASE:-$(cd "$RAIZ/.." && pwd)}"

# Imagen oficial FIJADA por versión Y digest (índice multi-arquitectura, verificado contra el registro de Docker Hub).
ZAP_IMAGEN='zaproxy/zap-stable:2.17.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6'
IMAGEN_PRUEBAS='igualab-pruebas-zap:ci'
RED="${ZAP_RED:-proxy_net}"
PROYECTO_QA="${ZAP_PROYECTO_COMPOSE_QA:-igualab-backend-qa}"
USUARIO_CONTENEDOR='1000:1000' # no root; coincide con el usuario `zap` de la imagen de ZAP y con el propietario de los volúmenes

ID_EJECUCION=$(printf '%s' "${ZAP_ID_EJECUCION:-${BUILD_TAG:-local-$(date -u +%Y%m%d%H%M%S)}}" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_.-' '-' | cut -c1-60)
CONTENEDOR="igualab-zap-$ID_EJECUCION"
DATOS="igualab-zap-datos-$ID_EJECUCION" # contenedor SIN arrancar que solo sirve para `docker cp` hacia/desde los volúmenes
VOL_TRABAJO="igualab-zap-trabajo-$ID_EJECUCION"     # entrada y salida de ZAP; los reportes crudos NO se archivan
VOL_PRIVADO="igualab-zap-privado-$ID_EJECUCION"     # estado y credenciales efímeras; NO se archiva
VOL_RESULTADOS="igualab-zap-resultados-$ID_EJECUCION" # lo único que vuelve al anfitrión (redactado y verificado)

RESULTADOS="$BASE/zap-resultados" # único directorio del espacio de trabajo; lo archiva Jenkins

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

# ¿El recurso Docker (contenedor o volumen) lleva la etiqueta de ESTA ejecución? Nada se elimina sin comprobarlo.
etiqueta_ok() { # $1 = container | volume, $2 = nombre
    if [ "$1" = volume ]; then
        _e=$(docker volume inspect -f '{{ index .Labels "igualab.zap.ejecucion" }}' "$2" 2>/dev/null)
    else
        _e=$(docker inspect -f '{{ index .Config.Labels "igualab.zap.ejecucion" }}' "$2" 2>/dev/null)
    fi
    [ "$_e" = "$ID_EJECUCION" ]
}

# Elimina únicamente el contenedor de ZAP creado por ESTA ejecución.
limpiar_contenedor() {
    if docker inspect "$CONTENEDOR" >/dev/null 2>&1; then
        if etiqueta_ok container "$CONTENEDOR"; then
            log "Eliminando el contenedor de ZAP de esta ejecución ($CONTENEDOR)."
            docker rm -f "$CONTENEDOR" >/dev/null 2>&1
        else
            log "El contenedor $CONTENEDOR no lleva la etiqueta de esta ejecución: no se toca."
        fi
    fi
}

crear_contenedor_de_datos() {
    docker create --name "$DATOS" --label "igualab.zap.ejecucion=$ID_EJECUCION" -u "$USUARIO_CONTENEDOR" \
        -v "$VOL_TRABAJO:/trabajo" -v "$VOL_PRIVADO:/privado" -v "$VOL_RESULTADOS:/resultados" "$IMAGEN_PRUEBAS" >/dev/null
}

crear_datos() {
    for _v in "$VOL_TRABAJO" "$VOL_PRIVADO" "$VOL_RESULTADOS"; do
        docker volume create --label "igualab.zap.ejecucion=$ID_EJECUCION" "$_v" >/dev/null || return 1
    done
    crear_contenedor_de_datos
}

# Libera los volúmenes y el contenedor de datos de ESTA ejecución (con sus etiquetas comprobadas).
liberar_datos() {
    if etiqueta_ok container "$DATOS"; then docker rm -f -v "$DATOS" >/dev/null 2>&1; fi
    for _v in "$VOL_TRABAJO" "$VOL_PRIVADO" "$VOL_RESULTADOS"; do
        if etiqueta_ok volume "$_v"; then
            docker volume rm "$_v" >/dev/null 2>&1 || log "Aviso: no se pudo eliminar el volumen $_v."
        fi
    done
}

# Trae al anfitrión (con la propiedad del usuario que ejecuta Jenkins) lo que el cierre dejó en el volumen de resultados.
traer_resultados() { # $1 = conservar-consola: no pisa la consola del anfitrión (modo limpiar)
    _t="$RESULTADOS/.traido"
    rm -rf "$_t"
    mkdir -p "$_t" || return 1
    if docker cp "$DATOS:/resultados/." "$_t/" >/dev/null 2>&1; then
        for _f in "$_t"/* "$_t"/.[!.]*; do
            [ -e "$_f" ] || continue
            _n=$(basename "$_f")
            if [ "${1:-}" = conservar-consola ] && [ "$_n" = consola-zap.log ] && [ -s "$RESULTADOS/consola-zap.log" ]; then continue; fi
            cp -f "$_f" "$RESULTADOS/$_n" || log "Aviso: no se pudo copiar $_n a $RESULTADOS."
        done
        rm -rf "$_t"
        return 0
    fi
    rm -rf "$_t"
    return 1
}

# Garantiza que SIEMPRE exista un resumen de fallo, aunque el cierre no haya podido escribir el suyo (permisos, Docker caído…).
# Solo escribe si falta; no contiene secretos ni cuerpos de respuestas. $1 = código, $2 = motivo (sin comillas ni saltos de línea).
resumen_de_emergencia() {
    [ -f "$RESULTADOS/resumen-zap.json" ] && [ -f "$RESULTADOS/resumen-zap.md" ] && return 0
    printf '{\n  "id_ejecucion": "%s",\n  "veredicto": "FALLO TECNICO",\n  "codigo_salida": %s,\n  "motivo": "%s",\n  "nota": "Resumen minimo generado por ejecutar-zap.sh porque el cierre no pudo producir el suyo. Ver consola-zap.log."\n}\n' \
        "$ID_EJECUCION" "$1" "$2" > "$RESULTADOS/resumen-zap.json" 2>/dev/null
    printf '# OWASP ZAP - resumen minimo de fallo\n\n**Veredicto:** FALLO TECNICO (codigo de salida %s)  \n**Motivo:** %s\n\nEl cierre no pudo escribir su resumen completo; consulte `consola-zap.log`. El analisis NO se considera realizado.\nLa exclusion de OCI/DeepSeek/correo sigue pendiente de aceptacion del TA y la cobertura no es completa.\n' \
        "$1" "$2" > "$RESULTADOS/resumen-zap.md" 2>/dev/null
}

UID_HOST="$(id -u):$(id -g)"
ENTORNO_DOCKER="-e IGUALAB_API_URL -e IGUALAB_E2E_SUPERADMIN_CORREO -e IGUALAB_E2E_SUPERADMIN_PASSWORD -e IGUALAB_PRUEBAS_ENTORNO -e BRANCH_NAME -e JENKINS_URL -e BUILD_NUMBER -e ZAP_CONFIRMO_DESTINO_QA -e ZAP_ESPERA_SEGUNDOS"

# Modo `limpiar` (post del stage, también tras cancelación o timeout): elimina el contenedor de ZAP de esta ejecución y, si los volúmenes
# de datos siguen existiendo, completa el cierre de la cuenta y la empresa sintéticas (el cierre sabe si ya estaba hecho), trae los
# resultados y libera los volúmenes. Es idempotente y no toca nada que no lleve la etiqueta de esta ejecución.
if [ "$MODO" = "limpiar" ]; then
    limpiar_contenedor
    if etiqueta_ok volume "$VOL_TRABAJO"; then
        log "Cierre de emergencia de los datos sintéticos (los volúmenes de la ejecución siguen existiendo)."
        mkdir -p "$RESULTADOS"
        etiqueta_ok container "$DATOS" || crear_contenedor_de_datos
        # shellcheck disable=SC2086
        docker run --rm --network "$RED" -u "$USUARIO_CONTENEDOR" $ENTORNO_DOCKER \
            -v "$VOL_TRABAJO:/trabajo" -v "$VOL_PRIVADO:/privado" -v "$VOL_RESULTADOS:/resultados" \
            "$IMAGEN_PRUEBAS" node scripts/finalizar.mjs --trabajo /trabajo --privado /privado --resultados /resultados --id "$ID_EJECUCION" \
            --prep-codigo 0 --zap-codigo ninguno --tiempo-agotado 2 --limite-minutos "$((LIMITE_SEG / 60))" \
            --imagen "$ZAP_IMAGEN" --comando "cierre de emergencia" 2>&1 | redactar_flujo
        traer_resultados conservar-consola || log "Aviso: no se pudieron traer los resultados del volumen."
        resumen_de_emergencia 6 "La ejecucion fue cancelada o interrumpida antes de terminar"
        liberar_datos
    fi
    exit 0
fi

# Una interrupción (cancelación o timeout de Jenkins → TERM/INT) elimina el contenedor de ZAP de esta ejecución y nada más; los
# volúmenes quedan para que el `post` complete el cierre sintético.
trap limpiar_contenedor EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

case "$MODO" in ejecutar) ;; *) log "Modo desconocido: $MODO"; exit 4 ;; esac

# El ÚNICO directorio del anfitrión. Si no se puede ni crear, no hay dónde dejar nada: se dice por consola y se sale con 12.
[ -n "$RESULTADOS" ] && [ "$RESULTADOS" != "/" ] && rm -rf "$RESULTADOS"
if ! mkdir -p "$RESULTADOS" || ! : > "$RESULTADOS/consola-zap.log"; then
    log "ERROR: [fase: anfitrion] no se puede crear/escribir $RESULTADOS (uid:gid $UID_HOST). Revise el propietario y los permisos del espacio de trabajo de Jenkins."
    exit 12
fi
CONSOLA="$RESULTADOS/consola-zap.log"

registrar() { printf '%s\n' "$*" | redactar_flujo | tee -a "$CONSOLA"; }

# Salida temprana con resumen mínimo garantizado (aún no se han creado cuentas sintéticas si se llama antes de la preparación).
salir_con() { # $1 = código, $2 = motivo
    registrar "ERROR: $2"
    resumen_de_emergencia "$1" "$2"
    liberar_datos
    exit "$1"
}

registrar "== OWASP ZAP · ejecución $ID_EJECUCION · imagen $ZAP_IMAGEN =="
registrar "Límite global de ZAP: ${LIMITE_SEG} s · análisis activo máx. ${ASCAN_MIN} min (por regla ${REGLA_MIN} min) · hilos ${HILOS} · retardo ${RETARDO_MS} ms"
# Diagnóstico del entorno (sin secretos): ayuda a distinguir problemas de permisos del nodo (rootless, userns, SELinux, Jenkins en contenedor).
registrar "Orquestador: uid:gid $UID_HOST · espacio de trabajo $BASE · $([ -f /.dockerenv ] && echo 'dentro de un contenedor' || echo 'en el anfitrión') · contenedores de datos como $USUARIO_CONTENEDOR (sin root)"
registrar "Docker: $(docker info --format 'seguridad={{.SecurityOptions}} · SO={{.OperatingSystem}}' 2>&1 | head -1 | cut -c1-200)"

PREP_CODIGO=0
if ! docker build -q -t "$IMAGEN_PRUEBAS" "$RAIZ" >/dev/null; then
    salir_con 3 "no se pudo construir la imagen de preparacion (pruebas-zap/Dockerfile)"
fi
if ! docker pull -q "$ZAP_IMAGEN" >/dev/null; then
    salir_con 3 "no se pudo descargar la imagen de ZAP fijada por digest (Docker valida el contenido contra el digest)"
fi
if ! crear_datos; then
    salir_con 12 "[fase: volumenes] no se pudieron crear los volumenes de datos de la ejecucion (docker volume create / docker create)"
fi
registrar "Volúmenes de datos creados: trabajo, privado (0700) y resultados, propiedad de $USUARIO_CONTENEDOR."

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
    SALIDA_PREP="$RESULTADOS/.preparacion.log"
    # shellcheck disable=SC2086
    docker run --rm --network "$RED" -u "$USUARIO_CONTENEDOR" $ENTORNO_DOCKER \
        -v "$VOL_TRABAJO:/trabajo" -v "$VOL_PRIVADO:/privado" \
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
        -v "$VOL_TRABAJO:/zap/wrk:rw" -v "$VOL_PRIVADO:/zap/privado:ro" \
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
    fi
fi

registrar "--- Cierre, reportes y veredicto ---"
# La consola filtrada hasta aquí entra al volumen para que el cierre la verifique también (el archivo se reemplaza por el verificado).
docker cp "$CONSOLA" "$DATOS:/resultados/consola-zap.log" >/dev/null 2>&1 || registrar "Aviso: no se pudo copiar la consola al volumen de resultados."
SALIDA_FIN="$RESULTADOS/.cierre.log"
# shellcheck disable=SC2086
docker run --rm --network "$RED" -u "$USUARIO_CONTENEDOR" $ENTORNO_DOCKER \
    -v "$VOL_TRABAJO:/trabajo" -v "$VOL_PRIVADO:/privado" -v "$VOL_RESULTADOS:/resultados" \
    "$IMAGEN_PRUEBAS" node scripts/finalizar.mjs --trabajo /trabajo --privado /privado --resultados /resultados --id "$ID_EJECUCION" \
    --prep-codigo "$PREP_CODIGO" --zap-codigo "$ZAP_CODIGO" --tiempo-agotado "$TIEMPO_AGOTADO" --limite-minutos "$((LIMITE_SEG / 60))" \
    --imagen "$ZAP_IMAGEN" --comando "$COMANDO_ZAP" > "$SALIDA_FIN" 2>&1
RC=$?
traer_resultados || registrar "Aviso: no se pudieron traer los resultados del volumen al espacio de trabajo."
redactar_flujo < "$SALIDA_FIN" | tee -a "$CONSOLA"
rm -f "$SALIDA_FIN"

# Solo 0..12 son códigos del ejecutor; cualquier otro (p. ej. docker run falló) es un error técnico y el cierre NO se da por hecho.
CIERRE_EJECUTADO=1
case "$RC" in 0|1|2|3|4|5|6|7|8|9|10|11|12) ;; *) registrar "ERROR: el cierre terminó con un código inesperado ($RC)."; RC=3; CIERRE_EJECUTADO=0 ;; esac
# Si el cierre no pudo escribir su resumen, queda uno mínimo (nunca un artifact sin explicación).
resumen_de_emergencia "$RC" "El cierre no pudo escribir su resumen (codigo $RC); ver consola-zap.log"
# Los volúmenes (con las credenciales efímeras) se liberan salvo que la limpieza sintética deba reintentarse desde el `post`.
if [ "$RC" -ne 10 ] && [ "$CIERRE_EJECUTADO" -eq 1 ]; then liberar_datos; fi
registrar "== OWASP ZAP · código de salida $RC =="
exit "$RC"

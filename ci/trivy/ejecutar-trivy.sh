#!/bin/sh
# Stage «Trivy»: analiza las vulnerabilidades de la imagen YA construida (ci/construir-imagen.sh) antes de cualquier despliegue.
#
# Entrada: IMAGEN_REF e IMAGEN_ID (ci-estado/imagen.env) y AMBIENTE (development | qa | uat; solo nombra la caché de Trivy).
# Salida (trivy-resultados/, lo único que se archiva): trivy-report.html, trivy-report.json, resumen-trivy.txt y consola-trivy.log.
# Código de salida: 0 aprobada · 1 bloqueada por vulnerabilidades · 2 error técnico. Cualquier valor distinto de 0 detiene el pipeline.
#
# Flujo con UN solo análisis: `docker save` de la imagen por ID -> `trivy image --input` (JSON con todas las severidades) -> el
# evaluador valida ese JSON, decide el bloqueo y escribe el JSON saneado y el resumen -> `trivy convert` produce el HTML desde ese JSON.
#
# Transferencia SIN bind mounts: Jenkins puede correr dentro de un contenedor y un `-v <ruta del workspace>:/trabajo` lo resuelve el demonio
# de Docker en SU host, donde esa ruta no existe (llega vacía). Por eso el tar, el evaluador y los resultados viajan por volúmenes Docker
# nombrados creados para esta ejecución, con `docker cp` (que transfiere por la API, no por rutas del host). Antes de analizar se comprueba
# desde un contenedor que imagen.tar existe, es legible, no está vacío y es idéntico (SHA-256) al exportado. Al terminar, y también si falla,
# los resultados vuelven a trivy-resultados/ y se eliminan solo los recursos de esta ejecución.
#
# Política (no hay forma de debilitarla desde aquí): sin --ignore-unfixed, .trivyignore, VEX ni exclusiones; el contenedor de Trivy no
# hereda variables del agente (docker run no pasa el entorno) y arranca en un directorio sin trivy.yaml ni archivos de ignorados.
# Opcional (solo pruebas del propio flujo): TRIVY_RED_DOCKER, red del contenedor de Trivy (p. ej. `none` para simular que no hay red).
set -eu

# Versión oficial FIJADA (etiqueta + digest del índice multi-arquitectura). La plantilla HTML viene dentro: /contrib/html.tpl.
TRIVY_IMAGEN="ghcr.io/aquasecurity/trivy:0.76.1@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
# Intérprete (solo biblioteca estándar, sin red) que ejecuta evaluar_trivy.py; fijado por digest como Trivy.
PYTHON_IMAGEN="python:3.13-slim-bookworm@sha256:4178fea840d0d85a1f4d4e768b4cc2ad71b5df43954e1836650c6d239039a4a3"

: "${IMAGEN_REF:?Falta IMAGEN_REF}"
: "${IMAGEN_ID:?Falta IMAGEN_ID}"
AMBIENTE="${AMBIENTE:-local}"
case "$AMBIENTE" in
    '' | *[!a-z0-9-]*) echo "ERROR: AMBIENTE no válido: $AMBIENTE" >&2; exit 2 ;;
esac

RAIZ="$(pwd)"
RESULTADOS="$RAIZ/trivy-resultados"
TRABAJO="$RAIZ/trivy-trabajo"
CACHE_VOLUMEN="igualab-trivy-cache-$AMBIENTE"
# Recursos propios de ESTA ejecución (nombre único por build y proceso): nunca se tocan los de otra ejecución ni la caché de la base.
EJECUCION="${BUILD_NUMBER:-0}-$$"
VOL_TRABAJO="igualab-trivy-trabajo-$AMBIENTE-$EJECUCION"
VOL_RESULTADOS="igualab-trivy-resultados-$AMBIENTE-$EJECUCION"
AUXILIAR="igualab-trivy-aux-$AMBIENTE-$EJECUCION"
USUARIO="$(id -u):$(id -g)"
RESUMEN="$RESULTADOS/resumen-trivy.txt"
CONSOLA="$RESULTADOS/consola-trivy.log"
VOLUMENES_CREADOS=""
AUXILIAR_CREADO=""

rm -rf "$RESULTADOS" "$TRABAJO"
mkdir -p "$RESULTADOS" "$TRABAJO"
: > "$CONSOLA"

# Elimina solo lo creado por esta ejecución: el tar local, el contenedor auxiliar y sus dos volúmenes (primero el contenedor).
limpiar() {
    rm -rf "$TRABAJO"
    if [ -n "$AUXILIAR_CREADO" ]; then
        docker rm -f "$AUXILIAR" > /dev/null 2>&1 || echo "aviso: no se pudo eliminar el contenedor $AUXILIAR" >> "$CONSOLA"
    fi
    if [ -n "$VOLUMENES_CREADOS" ]; then
        docker volume rm -f "$VOL_TRABAJO" "$VOL_RESULTADOS" > /dev/null 2>&1 || echo "aviso: no se pudieron eliminar los volúmenes de la ejecución" >> "$CONSOLA"
    fi
}
trap limpiar EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Trae a trivy-resultados/ lo que los contenedores escribieron en el volumen de resultados (resumen del evaluador, JSON saneado, HTML).
recuperar_resultados() {
    [ -n "$AUXILIAR_CREADO" ] || return 0
    docker cp "$AUXILIAR:/resultados/." "$RESULTADOS/" >> "$CONSOLA" 2>&1
}

# Error técnico: nunca deja un reporte que aparente un análisis válido; conserva el diagnóstico y bloquea el despliegue.
fallo_tecnico() {
    if ! recuperar_resultados; then
        echo "aviso: no se pudieron recuperar los resultados del volumen" >> "$CONSOLA"
    fi
    rm -f "$RESULTADOS/trivy-report.html" "$RESULTADOS/trivy-report.json"
    if [ ! -s "$RESUMEN" ] || [ "${2:-}" != "ya-resumido" ]; then
        {
            echo "RESUMEN TRIVY · Igualab"
            echo
            echo "Veredicto: ERROR TÉCNICO (el análisis NO es válido; no se aprueba el despliegue)"
            echo "Motivo: $1"
            echo
            echo "Imagen: $IMAGEN_REF"
            echo "ID de la imagen (Docker): $IMAGEN_ID"
            echo "Analizador: $TRIVY_IMAGEN"
            echo "Un error técnico NO significa que la imagen no tenga vulnerabilidades: no se evaluó. Ver consola-trivy.log."
        } > "$RESUMEN"
    fi
    cat "$RESUMEN"
    exit 2
}

# 1. Protección de la política: ninguna configuración del repositorio puede suprimir hallazgos.
sospechosos="$(find . -maxdepth 4 \
    \( -path ./venv -o -path ./.venv -o -path ./node_modules -o -path ./.git -o -path ./ci -o -path ./tests \) -prune -o \
    \( -name '.trivyignore*' -o -name 'trivy.yaml' -o -name 'trivy.yml' -o -name 'trivy-*.yaml' -o -name '*.openvex.json' \
       -o -name '*.vex.json' -o -name '.vex' \) -print)"
if [ -n "$sospechosos" ]; then
    fallo_tecnico "hay archivos que podrían suprimir hallazgos (la política no admite excepciones): $(echo "$sospechosos" | tr '\n' ' ')"
fi

# 2. La imagen a analizar es exactamente la construida y registrada.
id_actual="$(docker image inspect --format '{{.Id}}' "$IMAGEN_ID" 2>> "$CONSOLA")" || fallo_tecnico "la imagen $IMAGEN_ID no existe en el agente"
id_de_ref="$(docker image inspect --format '{{.Id}}' "$IMAGEN_REF" 2>> "$CONSOLA")" || fallo_tecnico "la referencia $IMAGEN_REF no existe en el agente"
if [ "$id_actual" != "$IMAGEN_ID" ] || [ "$id_de_ref" != "$IMAGEN_ID" ]; then
    fallo_tecnico "la referencia $IMAGEN_REF ya no apunta al ID registrado $IMAGEN_ID"
fi

# 3. Se analiza el contenido de ESA imagen (por ID), sin montar el socket de Docker en el contenedor de Trivy. `docker save -o` lo escribe el
#    cliente en el filesystem del agente (puede ser un contenedor de Jenkins); de ahí pasa al volumen con `docker cp` (paso 5).
echo "Exportando la imagen $IMAGEN_REF ($IMAGEN_ID)..."
docker save -o trivy-trabajo/imagen.tar "$IMAGEN_ID" >> "$CONSOLA" 2>&1 || fallo_tecnico "no se pudo exportar la imagen con docker save"
[ -s trivy-trabajo/imagen.tar ] || fallo_tecnico "docker save no generó imagen.tar en el agente (archivo ausente o vacío)"

# 4. Caché de Trivy: volumen propio por ambiente, separado de los artifacts. Solo guarda la base de vulnerabilidades (la caché de capas
#    es en memoria), así que no crece con cada build y Trivy sigue actualizando la base cuando corresponde.
docker volume create "$CACHE_VOLUMEN" > /dev/null || fallo_tecnico "no se pudo preparar el volumen de caché de Trivy"
docker run --rm --user 0:0 -v "$CACHE_VOLUMEN:/cache" --entrypoint sh "$TRIVY_IMAGEN" -c "chown -R $USUARIO /cache" \
    >> "$CONSOLA" 2>&1 || fallo_tecnico "no se pudo preparar la caché de Trivy (¿se pudo descargar $TRIVY_IMAGEN?)"

# 5. Transferencia por volúmenes nombrados de esta ejecución (no por rutas del agente, que el demonio de Docker interpretaría en su host).
#    El contenedor auxiliar nunca se inicia: solo sirve de destino de `docker cp` y de origen de los resultados.
VOLUMENES_CREADOS=1
docker volume create "$VOL_TRABAJO" > /dev/null 2>> "$CONSOLA" || fallo_tecnico "no se pudo crear el volumen de trabajo de la ejecución"
docker volume create "$VOL_RESULTADOS" > /dev/null 2>> "$CONSOLA" || fallo_tecnico "no se pudo crear el volumen de resultados de la ejecución"
docker create --name "$AUXILIAR" -v "$VOL_TRABAJO:/trabajo" -v "$VOL_RESULTADOS:/resultados" "$TRIVY_IMAGEN" > /dev/null 2>> "$CONSOLA" \
    || fallo_tecnico "no se pudo crear el contenedor auxiliar de transferencia"
AUXILIAR_CREADO=1
docker cp trivy-trabajo/imagen.tar "$AUXILIAR:/trabajo/imagen.tar" >> "$CONSOLA" 2>&1 \
    || fallo_tecnico "no se pudo copiar imagen.tar al volumen de análisis"
docker cp ci/trivy/evaluar_trivy.py "$AUXILIAR:/trabajo/evaluar_trivy.py" >> "$CONSOLA" 2>&1 \
    || fallo_tecnico "no se pudo copiar el evaluador al volumen de análisis"
docker run --rm --user 0:0 -v "$VOL_TRABAJO:/trabajo" -v "$VOL_RESULTADOS:/resultados" --entrypoint sh "$TRIVY_IMAGEN" \
    -c "chown -R $USUARIO /trabajo /resultados" >> "$CONSOLA" 2>&1 || fallo_tecnico "no se pudieron asignar permisos a los volúmenes de la ejecución"

# Antes de analizar: desde un contenedor, con el mismo usuario que ejecutará Trivy, imagen.tar existe, es legible y no está vacío; y su SHA-256
# coincide con el del archivo exportado (se calcula también en un contenedor, leyendo el archivo por la entrada estándar, sin depender del agente).
sha_exportado="$(docker run --rm -i --user "$USUARIO" --entrypoint sha256sum "$TRIVY_IMAGEN" - < trivy-trabajo/imagen.tar 2>> "$CONSOLA")" \
    || fallo_tecnico "no se pudo calcular el SHA-256 de la imagen exportada"
verificacion="$(docker run --rm --user "$USUARIO" -v "$VOL_TRABAJO:/trabajo:ro" --entrypoint sh "$TRIVY_IMAGEN" \
    -c 'f=/trabajo/imagen.tar; [ -f "$f" ] && [ -r "$f" ] && [ -s "$f" ] && sha256sum "$f"' 2>> "$CONSOLA")" \
    || fallo_tecnico "imagen.tar no existe, no es legible o está vacío en el volumen de análisis"
sha_exportado="${sha_exportado%% *}"
sha_en_volumen="${verificacion%% *}"
case "$sha_exportado" in *[!0-9a-f]* | '') fallo_tecnico "SHA-256 de la imagen exportada no válido" ;; esac
if [ "${#sha_exportado}" -ne 64 ] || [ "$sha_exportado" != "$sha_en_volumen" ]; then
    fallo_tecnico "imagen.tar del volumen de análisis no es idéntico al exportado (SHA-256 distinto)"
fi
echo "imagen.tar verificado en el volumen de análisis (SHA-256 $sha_exportado)."

ejecutar_trivy() {
    if [ -n "${TRIVY_RED_DOCKER:-}" ]; then
        docker run --rm --user "$USUARIO" --network "$TRIVY_RED_DOCKER" -e HOME=/tmp \
            -v "$VOL_TRABAJO:/trabajo" -v "$VOL_RESULTADOS:/resultados" -v "$CACHE_VOLUMEN:/cache" -w /trabajo "$TRIVY_IMAGEN" "$@"
    else
        docker run --rm --user "$USUARIO" -e HOME=/tmp \
            -v "$VOL_TRABAJO:/trabajo" -v "$VOL_RESULTADOS:/resultados" -v "$CACHE_VOLUMEN:/cache" -w /trabajo "$TRIVY_IMAGEN" "$@"
    fi
}

# 6. Análisis único. `--exit-code 0` solo evita que Trivy mezcle «hay hallazgos» con «falló»: un fallo real de Trivy sí devuelve un
#    código distinto de 0 (y bloquea). La decisión sobre los hallazgos la toma el evaluador, sobre todas las severidades del mismo JSON.
echo "Analizando con Trivy (paquetes del sistema operativo y dependencias de aplicación)..."
ejecutar_trivy image --input /trabajo/imagen.tar --cache-dir /cache --cache-backend memory \
    --scanners vuln --pkg-types os,library --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
    --format json --output /trabajo/trivy-crudo.json --exit-code 0 --ignorefile "" \
    --no-progress --skip-version-check --timeout 15m >> "$CONSOLA" 2>&1 \
    || fallo_tecnico "Trivy no pudo completar el análisis (descarga de la base de vulnerabilidades, lectura de la imagen o ejecución)"

docker run --rm --user "$USUARIO" -e HOME=/tmp -v "$VOL_TRABAJO:/trabajo" -v "$CACHE_VOLUMEN:/cache" -w /trabajo --entrypoint sh "$TRIVY_IMAGEN" \
    -c 'trivy version --format json --cache-dir /cache > /trabajo/trivy-version.json' >> "$CONSOLA" 2>&1 \
    || fallo_tecnico "no se pudo obtener la versión de Trivy ni la información de su base de vulnerabilidades"

# 7. Evaluación del mismo JSON (sin red: solo lee archivos).
codigo_evaluador=0
docker run --rm --network none --user "$USUARIO" -e HOME=/tmp -w /tmp \
    -v "$VOL_TRABAJO:/trabajo:ro" -v "$VOL_RESULTADOS:/resultados" "$PYTHON_IMAGEN" \
    python -I /trabajo/evaluar_trivy.py --crudo /trabajo/trivy-crudo.json --version-trivy /trabajo/trivy-version.json \
    --tar /trabajo/imagen.tar --imagen-ref "$IMAGEN_REF" --imagen-id "$IMAGEN_ID" --trivy-imagen "$TRIVY_IMAGEN" \
    --saneado /resultados/trivy-report.json --resumen /resultados/resumen-trivy.txt >> "$CONSOLA" 2>&1 || codigo_evaluador=$?
case "$codigo_evaluador" in
    0 | 10) ;;
    20) fallo_tecnico "el resultado de Trivy no es válido o está incompleto" ya-resumido ;;
    *) fallo_tecnico "no se pudo ejecutar el evaluador del resultado (código $codigo_evaluador)" ;;
esac

# 8. HTML desde el MISMO JSON saneado y recuperación de los resultados al workspace (lo único que se archiva).
ejecutar_trivy convert --format template --template "@/contrib/html.tpl" --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
    --ignorefile "" --output /resultados/trivy-report.html /resultados/trivy-report.json >> "$CONSOLA" 2>&1 \
    || fallo_tecnico "no se pudo generar trivy-report.html"
recuperar_resultados || fallo_tecnico "no se pudieron recuperar los resultados del volumen de la ejecución"
[ -s "$RESULTADOS/trivy-report.html" ] || fallo_tecnico "trivy-report.html quedó vacío"
[ -s "$RESULTADOS/trivy-report.json" ] || fallo_tecnico "trivy-report.json no se recuperó"
[ -s "$RESUMEN" ] || fallo_tecnico "resumen-trivy.txt no se recuperó"

cat "$RESUMEN"
if [ "$codigo_evaluador" -eq 10 ]; then
    echo "TRIVY: BLOQUEADO. La imagen no se despliega; el despliegue anterior sigue funcionando." >&2
    exit 1
fi
echo "TRIVY: APROBADO. La imagen analizada es la que se desplegará: $IMAGEN_ID"

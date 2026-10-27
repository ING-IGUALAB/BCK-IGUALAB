#!/bin/sh
# Regresión de PERMISOS con Docker REAL (Linux): reproduce el EACCES del build 36 de Jenkins QA y comprueba la corrección.
#
#   sh pruebas-zap/pruebas/permisos-real.sh
#
# No necesita backend, QA ni red (los contenedores corren con --network none), ni root en el anfitrión: los directorios «creados por
# otro contenedor» se generan con la propia imagen como root. Sale con 0 si todo pasa, 1 si algo falla y 2 si no hay Docker.
#
# El fallo original: la preparación (uid no root) hacía su PRIMERA escritura, /privado/estado.json, en un directorio del que no era
# dueño (root 755, creado por otro contenedor/daemon) y terminaba con «Error (EACCES)» sin decir qué operación ni qué ruta; el cierre
# también fallaba al escribir en /resultados y no dejaba ningún resumen.
set -u
RAIZ=$(cd "$(dirname "$0")/.." && pwd)
IMG='igualab-pruebas-zap:permisos'
FALLOS=0
ok() { printf '  PASS  %s\n' "$*"; }
mal() { printf '  FAIL  %s\n' "$*"; FALLOS=$((FALLOS + 1)); }
contiene() { # $1=texto $2=patron fijo $3=descripcion
    if printf '%s' "$1" | grep -qF -- "$2"; then ok "$3"; else mal "$3 (no aparece: $2)"; fi
}
no_contiene() {
    if printf '%s' "$1" | grep -qF -- "$2"; then mal "$3 (APARECE: $2)"; else ok "$3"; fi
}

docker info >/dev/null 2>&1 || { echo "Docker no está disponible: la regresión de permisos no se puede ejecutar."; exit 2; }
docker build -q -t "$IMG" "$RAIZ" >/dev/null || { echo "No se pudo construir la imagen de pruebas-zap."; exit 2; }

T=$(mktemp -d)
VOLS=""
limpiar_todo() {
    # Los directorios de otro propietario se borran con la misma imagen como root (sin sudo en el anfitrión).
    docker run --rm -u 0:0 -v "$T:/t" --entrypoint sh "$IMG" -c 'rm -rf /t/* /t/.[!.]*' >/dev/null 2>&1
    rmdir "$T" 2>/dev/null
    for v in $VOLS; do docker volume rm "$v" >/dev/null 2>&1; done
}
trap limpiar_todo EXIT

# Carpeta del anfitrión creada por «otro contenedor»: propietario root y el modo indicado.
de_otro_contenedor() { # $1=ruta $2=modo
    mkdir -p "$1"
    docker run --rm -u 0:0 -v "$1:/d" --entrypoint sh "$IMG" -c "chown 0:0 /d && chmod $2 /d"
}

SECRETO='Secreto-Que-No-Debe-Salir-1!'

echo "== 1) PREPARACIÓN con uid 1001 y /privado creado por otro contenedor (root 755): debe fallar RÁPIDO y diciendo qué pasó"
mkdir -p "$T/c1/trabajo" && chmod 777 "$T/c1/trabajo"
de_otro_contenedor "$T/c1/privado" 755
SALIDA=$(docker run --rm --network none -u 1001:1001 -e IGUALAB_E2E_SUPERADMIN_PASSWORD="$SECRETO" \
    -v "$T/c1/trabajo:/trabajo" -v "$T/c1/privado:/privado" "$IMG" node scripts/preparar.mjs --trabajo /trabajo --privado /privado --id permisos 2>&1)
RC=$?
[ "$RC" -eq 12 ] && ok "código de salida 12 (ARCHIVOS), no 3 («error inesperado»)" || mal "código de salida $RC (esperado 12)"
contiene "$SALIDA" '[fase: montajes]' 'dice la fase'
contiene "$SALIDA" '/privado/.sonda' 'dice la operación y la ruta segura del contenedor'
contiene "$SALIDA" 'EACCES' 'dice el código de error'
contiene "$SALIDA" 'uid=1001 gid=1001' 'dice con qué identidad corre'
contiene "$SALIDA" 'propietario=0:0' 'dice quién es el dueño de la carpeta'
no_contiene "$SALIDA" 'Error (EACCES)' 'ya no termina con el genérico «Error (EACCES)»'
no_contiene "$SALIDA" "$SECRETO" 'no filtra la contraseña del entorno'
no_contiene "$SALIDA" 'Alcance:' 'falla ANTES de contactar al backend y de crear nada'
[ ! -e "$T/c1/privado/estado.json" ] && ok "no se escribió nada en el directorio ajeno" || mal "apareció estado.json"

echo "== 2) CIERRE con /privado y /resultados de otro contenedor: debe tolerarlo, informar y dejar el resumen por consola"
mkdir -p "$T/c2/trabajo" && chmod 777 "$T/c2/trabajo"
de_otro_contenedor "$T/c2/privado" 700
de_otro_contenedor "$T/c2/resultados" 755
SALIDA=$(docker run --rm --network none -u 1001:1001 -e IGUALAB_E2E_SUPERADMIN_PASSWORD="$SECRETO" \
    -v "$T/c2/trabajo:/trabajo" -v "$T/c2/privado:/privado" -v "$T/c2/resultados:/resultados" "$IMG" \
    node scripts/finalizar.mjs --trabajo /trabajo --privado /privado --resultados /resultados --id permisos \
    --prep-codigo 12 --zap-codigo ninguno --tiempo-agotado 0 --limite-minutos 80 --imagen img --comando c 2>&1)
RC=$?
[ "$RC" -eq 12 ] && ok "código de salida 12 (ARCHIVOS)" || mal "código de salida $RC (esperado 12)"
contiene "$SALIDA" '[fase: cierre] leer /privado/estado.json falló con EACCES' 'registra que no pudo leer el estado privado (fase, operación, ruta, código)'
contiene "$SALIDA" 'escribir sonda en /resultados/.sonda' 'registra que /resultados no es escribible'
contiene "$SALIDA" 'No se pudo comprobar si quedaron recursos sintéticos' 'NO afirma que la limpieza esté hecha si no pudo comprobarlo'
contiene "$SALIDA" 'revise en QA la cuenta zap-permisos@igualab-zap.example.com' 'indica qué revisar en QA (nombres deterministas)'
contiene "$SALIDA" 'no se pudo escribir en resultados; se muestra aquí' 'muestra el resumen por consola cuando no puede archivarlo'
contiene "$SALIDA" 'OWASP ZAP' 'el resumen mostrado tiene contenido'
no_contiene "$SALIDA" "$SECRETO" 'no filtra la contraseña del entorno'

echo "== 3) CAMINO CORRECTO: volúmenes nombrados (propietario 1000:1000, privado 0700), contenedores no root, sin chmod 777"
for n in t p r; do V="igualab-zap-regresion-$n-$$"; VOLS="$VOLS $V"; docker volume create "$V" >/dev/null; done
VT="igualab-zap-regresion-t-$$"; VP="igualab-zap-regresion-p-$$"; VR="igualab-zap-regresion-r-$$"
docker create --name "igualab-zap-regresion-datos-$$" -u 1000:1000 -v "$VT:/trabajo" -v "$VP:/privado" -v "$VR:/resultados" "$IMG" >/dev/null
INFO=$(docker run --rm -u 1000:1000 -v "$VT:/trabajo" -v "$VP:/privado" -v "$VR:/resultados" "$IMG" sh -c 'for d in /trabajo /privado /resultados; do stat -c "%n %u:%g %a" $d; done')
contiene "$INFO" '/privado 1000:1000 700' '/privado es de 1000:1000 y 0700 (solo ese usuario)'
contiene "$INFO" '/trabajo 1000:1000 750' '/trabajo es de 1000:1000 y 0750 (no 777)'
contiene "$INFO" '/resultados 1000:1000 750' '/resultados es de 1000:1000 y 0750 (no 777)'
SALIDA=$(docker run --rm --network none -u 1000:1000 -v "$VT:/trabajo" -v "$VP:/privado" "$IMG" node scripts/preparar.mjs --trabajo /trabajo --privado /privado --id permisos 2>&1)
RC=$?
contiene "$SALIDA" 'Montajes de datos: uid=1000 gid=1000' 'la preparación supera la comprobación de montajes'
no_contiene "$SALIDA" '[fase: montajes]' 'sin error de permisos en los montajes'
[ "$RC" -eq 4 ] && ok "falla después, por configuración (faltan variables), código 4" || mal "código $RC (esperado 4 por variables faltantes)"
OTRO=$(docker run --rm -u 1001:1001 -v "$VP:/privado" "$IMG" sh -c 'ls /privado 2>&1; echo x > /privado/otro 2>&1' 2>&1)
contiene "$OTRO" 'Permission denied' 'otro uid NO puede leer ni escribir /privado'

echo "== 4) CIERRE con preparación fallida antes de crear nada: resumen de fallo garantizado en el volumen"
SALIDA=$(docker run --rm --network none -u 1000:1000 -v "$VT:/trabajo" -v "$VP:/privado" -v "$VR:/resultados" "$IMG" \
    node scripts/finalizar.mjs --trabajo /trabajo --privado /privado --resultados /resultados --id permisos \
    --prep-codigo 4 --zap-codigo ninguno --tiempo-agotado 0 --limite-minutos 80 --imagen img --comando c 2>&1)
RC=$?
[ "$RC" -eq 4 ] && ok "código de salida 4 (el de la preparación)" || mal "código de salida $RC (esperado 4)"
LISTA=$(docker run --rm -u 1000:1000 -v "$VR:/resultados" "$IMG" ls /resultados)
contiene "$LISTA" 'resumen-zap.json' 'existe resumen-zap.json'
contiene "$LISTA" 'resumen-zap.md' 'existe resumen-zap.md'
contiene "$LISTA" 'alcance-zap.json' 'existe alcance-zap.json'
contiene "$SALIDA" 'No se crearon recursos sintéticos ni sesiones que cerrar' 'registra que no había nada que limpiar'
docker rm -f "igualab-zap-regresion-datos-$$" >/dev/null 2>&1

echo
if [ "$FALLOS" -eq 0 ]; then echo "REGRESIÓN DE PERMISOS: todo correcto."; exit 0; fi
echo "REGRESIÓN DE PERMISOS: $FALLOS comprobación(es) fallida(s)."
exit 1

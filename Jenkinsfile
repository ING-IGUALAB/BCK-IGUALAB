pipeline {
    agent any

    options {
        buildDiscarder(logRotator(numToKeepStr: '1', artifactNumToKeepStr: '1'))
        disableConcurrentBuilds()
        timestamps()
        skipDefaultCheckout(true)
    }

    stages {
        stage('Checkout Repo') {
            steps {
                checkout scm
            }
        }

        // --- ETAPA TEMPORAL DE DIAGNÓSTICO (eliminar tras investigar el 500 del chat RAG) ---
        // Vuelca a la consola del build el traceback de los request_ids indicados, leyéndolo
        // del contenedor backend VIVO antes de que Deploy lo reinicie. Solo lectura; no toca la app.
        stage('Diagnóstico logs chat (temporal)') {
            when {
                branch 'development'
            }
            steps {
                withCredentials([
                    file(credentialsId: 'IGUALAB_BACKEND_DEV', variable: 'SECRET_FILE')
                ]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-development']) {
                        sh '''
set +e
rm -f .env
cp "$SECRET_FILE" .env
echo '--- Estado del contenedor backend ---'
docker compose -p "$COMPOSE_PROJECT" ps -a
echo '--- Tracebacks de los request_ids pedidos ---'
docker compose -p "$COMPOSE_PROJECT" logs --no-color --tail=600 backend 2>&1 | grep -B3 -A60 -E "04bea0c4-20a6-4f33-918c-9132276e9f9f|416267cc-63db-4f45-8d2f-4ac6a415a7e2"
if [ $? -ne 0 ]; then
    echo '--- IDs no encontrados: últimas trazas de error del backend ---'
    docker compose -p "$COMPOSE_PROJECT" logs --no-color --tail=400 backend 2>&1 | grep -B3 -A60 -E "Traceback|Error inesperado|Error de base de datos" | tail -150
fi
echo '--- Últimas líneas crudas del backend (contexto) ---'
docker compose -p "$COMPOSE_PROJECT" logs --no-color --tail=60 backend
'''
                    }
                }
            }
        }

        stage('Tests (Contenedor Python)') {
            agent {
                dockerfile {
                    filename 'Dockerfile.ci'
                    reuseNode true
                }
            }
            steps {
                sh '''
                    set -eu
                    ci_venv="$(mktemp -d)"
                    python -m venv "$ci_venv"
                    "$ci_venv/bin/python" -m pip install --upgrade pip
                    "$ci_venv/bin/pip" install --no-cache-dir -r requirements.txt -r requirements-dev.txt
                    JWT_SECRET_KEY=ci-test-secret MAIL_USERNAME=ci@igualab.pe MAIL_PASSWORD=ci-password MAIL_FROM=ci@igualab.pe "$ci_venv/bin/python" -m pytest tests/ --cov=app --cov-report=term-missing --cov-report=xml:coverage.xml
                '''
            }
        }

        stage('SonarQube Analysis') {
            when {
                anyOf {
                    branch 'qa'
                    branch 'uat'
                }
            }
            agent {
                dockerfile {
                    filename 'Dockerfile.ci'
                    reuseNode true
                }
            }
            environment {
                scannerHome = tool 'SonarScanner'
            }
            steps {
                script {
                    def sonarUserHome = "${env.WORKSPACE}/.sonar"

                    withEnv(["SONAR_USER_HOME=${sonarUserHome}"]) {
                        sh 'python --version'
                        sh 'java --version'

                        if (env.BRANCH_NAME == 'qa') {
                            withSonarQubeEnv('SonarQube-Server') {
                                sh "${scannerHome}/bin/sonar-scanner -Dsonar.projectKey=BCK-IGUALAB-QA -Dsonar.userHome=${sonarUserHome}"
                            }
                        } else {
                            withSonarQubeEnv('SonarQube-Server') {
                                sh "${scannerHome}/bin/sonar-scanner -Dsonar.projectKey=BCK-IGUALAB-UAT -Dsonar.userHome=${sonarUserHome}"
                            }
                        }
                    }
                }
            }
        }

        stage('Quality Gate') {
            when {
                anyOf {
                    branch 'qa'
                    branch 'uat'
                }
            }
            steps {
                timeout(time: 1, unit: 'HOURS') {
                    waitForQualityGate abortPipeline: true
                }
            }
        }

        stage('Deploy Dev (Docker Compose)') {
            when {
                branch 'development'
            }
            steps {
                withCredentials([
                    file(credentialsId: 'IGUALAB_BACKEND_DEV', variable: 'SECRET_FILE'),
                    file(credentialsId: 'IGUALAB_KEY_ORACLE', variable: 'IGUALAB_KEY_ORACLE_FILE')
                ]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-development']) {
                        sh '''
set -eu
rm -f .env
cp "$SECRET_FILE" .env
'''
                        script { incorporarLlaveOci() }
                        sh '''
set -eu
docker compose -p "$COMPOSE_PROJECT" down
docker compose -p "$COMPOSE_PROJECT" up -d --build
sleep 25

echo '--- Estado del backend ---'
docker compose -p "$COMPOSE_PROJECT" ps -a

echo '--- Logs de arranque ---'
docker compose -p "$COMPOSE_PROJECT" logs --no-color --tail=200 backend

echo '--- Diagnóstico OCI dentro del backend ---'
docker compose -p "$COMPOSE_PROJECT" exec -T backend python - <<'PY'
import os
from pathlib import Path
import oci
from cryptography.hazmat.primitives.serialization import load_pem_private_key

ruta = Path(
    os.environ.get("OCI_CONFIG_FILE") or "~/.oci/config"
).expanduser()
perfil = os.environ.get("OCI_CONFIG_PROFILE") or "svc-embeddings"

print("OCI_CONFIG_FILE:", ruta)
print("Archivo existe:", ruta.is_file())
print("Perfil solicitado:", perfil)
print("OCI_KEY_PEM_B64 configurada:",
      bool(os.environ.get("OCI_KEY_PEM_B64", "").strip()))

if ruta.is_file():
    try:
        config = oci.config.from_file(str(ruta), perfil)
        oci.config.validate_config(config)
        print("Configuración y perfil: OK")

        llave = config.get("key_file")
        existe = bool(llave) and Path(llave).expanduser().is_file()
        print("Archivo de llave existe:", existe)

        if existe:
            with open(Path(llave).expanduser(), "rb") as archivo:
                passphrase = config.get("pass_phrase")
                load_pem_private_key(
                    archivo.read(),
                    password=passphrase.encode() if passphrase else None,
                )
            print("Llave privada legible: SÍ")
    except Exception as error:
        print("Verificación falló:", type(error).__name__)
else:
    print("Configuración por archivo: NO DISPONIBLE")
    try:
        from app.modules.ingesta.infrastructure.proveedor_oci import _cargar_identidad

        config = _cargar_identidad()
        print("Identidad por variables OCI_*: OK")
        load_pem_private_key(config["key_content"].encode(), password=None)
        print("Llave privada legible (variables): SÍ")
    except Exception as error:
        print("Identidad por variables falló:", type(error).__name__)
PY
'''
                        // ===== INICIO DIAGNÓSTICO TEMPORAL: BASE VECTORIAL (retirar: borrar todo hasta FIN) =====
                        // Solo lectura, dentro del contenedor backend ya levantado. Imprime etapa, duración, clase de error y SQLSTATE;
                        // nunca URLs completas, usuario, contraseña ni str(exc). Su resultado NO cambia el del despliegue.
                        catchError(buildResult: 'SUCCESS', stageResult: 'SUCCESS') {
                            sh(returnStatus: true, script: '''
set +e
set +x
echo '--- DIAGNÓSTICO TEMPORAL: conectividad a las bases (solo lectura; no altera el resultado del despliegue) ---'
if command -v timeout >/dev/null 2>&1; then LIMITE='timeout 200'; else LIMITE=''; fi
$LIMITE docker compose -p "$COMPOSE_PROJECT" exec -T backend python -u - <<'PY'
import asyncio
import os
import socket
import sys
import time

import asyncpg
from sqlalchemy.engine import make_url

PLAZO_DNS = 5.0
PLAZO_TCP = 10.0
PLAZO_PG = 30.0
PLAZO_SELECT = 10.0
PLAZO_CIERRE = 5.0
PLAZO_APP = 10.0
MAX_DIRECCIONES = 2
SSL_CONOCIDOS = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def linea(base, texto):
    print("[DIAG-BD] " + base + " | " + texto, flush=True)


def causa(exc):
    # Solo clases, SQLSTATE y errno: el mensaje de la excepcion puede contener host, usuario o la URL.
    clases, sqlstate, errno = [], None, None
    actual = exc
    for _ in range(5):
        if actual is None:
            break
        clases.append(type(actual).__name__)
        if sqlstate is None and isinstance(getattr(actual, "sqlstate", None), str):
            sqlstate = actual.sqlstate[:5]
        if errno is None and isinstance(getattr(actual, "errno", None), int):
            errno = actual.errno
        actual = actual.__cause__ or actual.__context__
    return "clase=%s sqlstate=%s errno=%s cadena=%s" % (
        clases[0], sqlstate or "n/a", "n/a" if errno is None else errno, ">".join(clases),
    )


async def etapa(base, nombre, plazo, crear):
    inicio = time.monotonic()
    try:
        async with asyncio.timeout(plazo):
            valor = await crear()
    except Exception as exc:
        duracion = time.monotonic() - inicio
        linea(base, "%s: FALLA tras %.2f s (plazo %.0f s) %s" % (nombre, duracion, plazo, causa(exc)))
        return False, duracion, None
    duracion = time.monotonic() - inicio
    linea(base, "%s: OK en %.2f s (plazo %.0f s)" % (nombre, duracion, plazo))
    return True, duracion, valor


def abrir_tcp(ip, puerto):
    async def abrir():
        _, escritor = await asyncio.open_connection(ip, puerto)
        escritor.close()
        try:
            await escritor.wait_closed()
        except Exception:
            pass
    return abrir


async def sondear(base, url, definida):
    resultado = {}
    linea(base, "variable definida en el entorno del contenedor: %s" % ("si" if definida else "NO (se usa el valor por defecto de la aplicacion)"))
    if not isinstance(url, str) or not url.strip():
        linea(base, "URL NO CONFIGURADA")
        return {"destino": None, "resultado": "sin configuracion"}
    try:
        destino = make_url(url.strip())
    except Exception as exc:
        linea(base, "URL no interpretable " + causa(exc))
        return {"destino": None, "resultado": "URL no interpretable"}
    host = destino.host or "localhost"
    puerto = destino.port or 5432
    ssl = destino.query.get("ssl")
    linea(base, "destino host=%s puerto=%s base=%s controlador=%s ssl=%s otros_parametros=%s" % (
        host, puerto, destino.database, destino.drivername,
        ssl if ssl in SSL_CONOCIDOS else ("otro" if ssl else "no"),
        ",".join(sorted(k for k in destino.query if k != "ssl")) or "ninguno",
    ))
    resultado["destino"] = (host, puerto, destino.database)

    bucle = asyncio.get_running_loop()
    ok_dns, dur_dns, direcciones = await etapa(
        base, "1/4 DNS", PLAZO_DNS, lambda: bucle.getaddrinfo(host, puerto, type=socket.SOCK_STREAM)
    )
    resultado["dns"] = "OK %.2f s" % dur_dns if ok_dns else "FALLA %.2f s" % dur_dns
    ok_tcp = False
    if ok_dns:
        familias = ["IPv6" if d[0] == socket.AF_INET6 else "IPv4" for d in direcciones]
        linea(base, "1/4 DNS: %d direccion(es), orden de intento: %s" % (len(familias), ",".join(familias)))
        tcp = []
        for numero, d in enumerate(direcciones[:MAX_DIRECCIONES], 1):
            ok, dur, _ = await etapa(
                base, "2/4 TCP direccion %d (%s)" % (numero, familias[numero - 1]), PLAZO_TCP, abrir_tcp(d[4][0], puerto)
            )
            tcp.append("%s %s %.2f s" % (familias[numero - 1], "OK" if ok else "FALLA", dur))
            ok_tcp = ok_tcp or ok
        resultado["tcp"] = "; ".join(tcp)
    else:
        linea(base, "2/4 TCP: omitida (no se resolvio el nombre)")
        resultado["tcp"] = "omitida"

    try:
        from app.migraciones.motor import _argumentos_de_conexion
        argumentos = _argumentos_de_conexion(url, base)
    except Exception as exc:
        linea(base, "3/4 PostgreSQL: no se pudo preparar la configuracion efectiva " + causa(exc))
        resultado["pg"] = "sin configuracion"
        return resultado

    conexion = None
    try:
        ok_pg, dur_pg, conexion = await etapa(
            base, "3/4 PostgreSQL connect", PLAZO_PG, lambda: asyncpg.connect(**argumentos, timeout=PLAZO_PG)
        )
        if ok_pg and dur_pg > PLAZO_APP:
            linea(base, "EVIDENCIA: conecto en %.2f s, MAS que los %.0f s que la aplicacion concede al arrancar (evaluar ampliar ese plazo)" % (dur_pg, PLAZO_APP))
        elif ok_pg:
            linea(base, "conecto en %.2f s: DENTRO de los %.0f s que la aplicacion concede al arrancar" % (dur_pg, PLAZO_APP))
        elif dur_pg >= PLAZO_PG - 1:
            linea(base, "NO conecto dentro de %.0f s (la aplicacion concede %.0f s): ampliar el plazo no bastaria con este valor" % (PLAZO_PG, PLAZO_APP))
        resultado["pg"] = ("OK " if ok_pg else "FALLA ") + "%.2f s" % dur_pg
        if ok_pg:
            ok_sel, dur_sel, valor = await etapa(
                base, "4/4 SELECT 1", PLAZO_SELECT, lambda: conexion.fetchval("SELECT 1", timeout=PLAZO_SELECT)
            )
            resultado["select"] = ("OK " if ok_sel and valor == 1 else "FALLA ") + "%.2f s" % dur_sel
        else:
            linea(base, "4/4 SELECT 1: omitida (no hay conexion)")
            resultado["select"] = "omitida"
    finally:
        if conexion is not None:
            try:
                async with asyncio.timeout(PLAZO_CIERRE):
                    await conexion.close()
            except Exception:
                conexion.terminate()
    return resultado


async def main():
    print("[DIAG-BD] python %s | asyncpg %s" % (sys.version.split()[0], asyncpg.__version__), flush=True)
    try:
        from app.core.config import settings
        efectivas = {"vectorial": settings.VECTOR_DATABASE_URL, "transaccional": settings.DATABASE_URL}
        app_env = settings.APP_ENV
    except Exception as exc:
        linea("entorno", "no se pudo leer la configuracion de la aplicacion " + causa(exc))
        efectivas = {"vectorial": os.environ.get("VECTOR_DATABASE_URL"), "transaccional": os.environ.get("DATABASE_URL")}
        app_env = os.environ.get("APP_ENV")
    linea("entorno", "APP_ENV efectivo=%s | APP_ENV en el entorno del contenedor=%s" % (app_env, os.environ.get("APP_ENV", "(no definida)")))
    variables = {"vectorial": "VECTOR_DATABASE_URL", "transaccional": "DATABASE_URL"}
    resultados = {}
    # Primero la vectorial (objeto del diagnostico) y luego la transaccional como control.
    for base in ("vectorial", "transaccional"):
        resultados[base] = await sondear(base, efectivas[base], bool(os.environ.get(variables[base])))
    print("[DIAG-BD] ---------------- RESUMEN ----------------", flush=True)
    for base, r in resultados.items():
        linea(base, "dns=%s | tcp=[%s] | pg=%s | select=%s" % (
            r.get("dns", "n/a"), r.get("tcp", "n/a"), r.get("pg", "n/a"), r.get("select", "n/a"),
        ))
    destinos = [r.get("destino") for r in resultados.values()]
    linea("comparacion", "misma combinacion host/puerto/base en ambas: %s" % ("si" if destinos[0] is not None and destinos[0] == destinos[1] else "no"))
    linea("comparacion", "mismo host y puerto en ambas: %s" % ("si" if None not in destinos and destinos[0][:2] == destinos[1][:2] else "no"))


try:
    asyncio.run(main())
except BaseException as exc:
    print("[DIAG-BD] el diagnostico termino con error " + causa(exc), flush=True)
PY
echo "--- Fin del diagnóstico temporal (código de salida: $?; no afecta al despliegue) ---"
exit 0
''')
                        }
                        // ===== FIN DIAGNÓSTICO TEMPORAL: BASE VECTORIAL =====
                    }
                }
            }
        }    

        stage('Deploy QA (Docker Compose)') {
            when {
                branch 'qa'
            }
            steps {
                withCredentials([
                    file(credentialsId: 'IGUALAB_BACKEND_QA', variable: 'SECRET_FILE'),
                    file(credentialsId: 'IGUALAB_KEY_ORACLE', variable: 'IGUALAB_KEY_ORACLE_FILE')
                ]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-qa']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
                        '''
                        script { incorporarLlaveOci() }
                        sh '''
                            set -eu
                            docker compose -p "$COMPOSE_PROJECT" down
                            docker compose -p "$COMPOSE_PROJECT" up -d --build
                        '''
                    }
                }
            }
        }

        // Pruebas HTTP del backend DESPLEGADO en QA (colecciones Postman ejecutadas con Newman; ver pruebas-http/README.md).
        // Solo en la rama qa. Nunca se ejecutan automáticamente contra UAT: la demostración en UAT es manual (README).
        // Credencial de Jenkins requerida: IGUALAB_QA_SUPERADMIN (Username with password = correo y contraseña VIGENTE del
        // SuperAdmin de QA). Opcional: variable de entorno IGUALAB_QA_API_URL si el backend no es alcanzable por el nombre de su
        // contenedor en la red proxy_net.
        stage('Integration Tests') {
            when {
                beforeAgent true
                branch 'qa'
            }
            agent {
                dockerfile {
                    filename 'Dockerfile'
                    dir 'pruebas-http'
                    reuseNode true
                    args '--network proxy_net'
                }
            }
            environment {
                IGUALAB_PRUEBAS_ENTORNO = 'qa'
                IGUALAB_API_URL = "${env.IGUALAB_QA_API_URL ?: 'http://igualab-backend-qa-backend-1:8000'}"
            }
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'IGUALAB_QA_SUPERADMIN',
                    usernameVariable: 'IGUALAB_E2E_SUPERADMIN_CORREO',
                    passwordVariable: 'IGUALAB_E2E_SUPERADMIN_PASSWORD'
                )]) {
                    sh 'node /opt/pruebas-http/scripts/ejecutar.mjs --coleccion integracion --resultados "$WORKSPACE/pruebas-http/resultados"'
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: 'pruebas-http/resultados/integracion/**', allowEmptyArchive: false
                    junit testResults: 'pruebas-http/resultados/integracion/junit.xml', allowEmptyResults: false
                }
            }
        }

        stage('E2E Tests') {
            when {
                beforeAgent true
                branch 'qa'
            }
            agent {
                dockerfile {
                    filename 'Dockerfile'
                    dir 'pruebas-http'
                    reuseNode true
                    args '--network proxy_net'
                }
            }
            environment {
                IGUALAB_PRUEBAS_ENTORNO = 'qa'
                IGUALAB_API_URL = "${env.IGUALAB_QA_API_URL ?: 'http://igualab-backend-qa-backend-1:8000'}"
            }
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'IGUALAB_QA_SUPERADMIN',
                    usernameVariable: 'IGUALAB_E2E_SUPERADMIN_CORREO',
                    passwordVariable: 'IGUALAB_E2E_SUPERADMIN_PASSWORD'
                )]) {
                    sh 'node /opt/pruebas-http/scripts/ejecutar.mjs --coleccion e2e --resultados "$WORKSPACE/pruebas-http/resultados"'
                }
            }
            post {
                always {
                    archiveArtifacts artifacts: 'pruebas-http/resultados/e2e/**', allowEmptyArchive: false
                    junit testResults: 'pruebas-http/resultados/e2e/junit.xml', allowEmptyResults: false
                }
            }
        }

        // Análisis de seguridad dinámico del backend DESPLEGADO en QA: OWASP ZAP API Scan (imagen oficial fijada por versión y digest) con
        // análisis ACTIVO sobre el OpenAPI real filtrado (pruebas-zap/alcance/operaciones.json). Solo en la rama qa, tras E2E Tests; nunca
        // contra UAT. Guía, alcance y límites: pruebas-zap/README.md. Credencial de Jenkins: IGUALAB_QA_SUPERADMIN (la misma de los
        // stages anteriores; no se crea otro SuperAdmin). Opcional: IGUALAB_QA_API_URL si el backend no es alcanzable por el nombre de su
        // contenedor en proxy_net.
        // Código de salida del ejecutor: 0 éxito · 2 INESTABLE (advertencias WARN o análisis activo parcial) · 1 reglas FAIL · otro: error
        // técnico (3 ZAP, 4 configuración, 5 backend, 6 tiempo agotado, 7 reportes, 8 secretos, 9 evidencia, 10 limpieza, 11 alcance, 12 archivos/permisos).
        // El límite global del stage es el `timeout` de abajo (100 min); el de ZAP es ZAP_LIMITE_MINUTOS (80 min por defecto).
        stage('OWASP ZAP') {
            when {
                beforeAgent true
                branch 'qa'
            }
            environment {
                IGUALAB_PRUEBAS_ENTORNO = 'qa'
                IGUALAB_API_URL = "${env.IGUALAB_QA_API_URL ?: 'http://igualab-backend-qa-backend-1:8000'}"
                ZAP_ID_EJECUCION = "${env.BUILD_TAG}"
            }
            steps {
                timeout(time: 100, unit: 'MINUTES') {
                    withCredentials([usernamePassword(
                        credentialsId: 'IGUALAB_QA_SUPERADMIN',
                        usernameVariable: 'IGUALAB_E2E_SUPERADMIN_CORREO',
                        passwordVariable: 'IGUALAB_E2E_SUPERADMIN_PASSWORD'
                    )]) {
                        script {
                            def codigo = sh(returnStatus: true, script: 'sh pruebas-zap/scripts/ejecutar-zap.sh')
                            if (codigo == 0) {
                                echo 'OWASP ZAP: sin alertas FAIL ni WARN dentro del alcance escaneado (no equivale a "backend seguro"; ver zap-resultados/resumen-zap.md).'
                            } else if (codigo == 2) {
                                unstable('OWASP ZAP: advertencias (WARN) o análisis activo parcial. Revise zap-resultados/resumen-zap.md y backend-zap.html en los artefactos.')
                            } else if (codigo == 1) {
                                error('OWASP ZAP: alertas de nivel FAIL (ver backend-zap.html y resumen-zap.md en los artefactos).')
                            } else {
                                error("OWASP ZAP: error técnico (código ${codigo}); el análisis NO se considera realizado. Ver zap-resultados/resumen-zap.md y consola-zap.log.")
                            }
                        }
                    }
                }
            }
            post {
                always {
                    // Tras una cancelación o un timeout: elimina SOLO el contenedor de ZAP de esta ejecución y completa el cierre de la
                    // cuenta y la empresa sintéticas si el cierre normal no llegó a hacerlo. Es idempotente.
                    withCredentials([usernamePassword(
                        credentialsId: 'IGUALAB_QA_SUPERADMIN',
                        usernameVariable: 'IGUALAB_E2E_SUPERADMIN_CORREO',
                        passwordVariable: 'IGUALAB_E2E_SUPERADMIN_PASSWORD'
                    )]) {
                        sh 'sh pruebas-zap/scripts/ejecutar-zap.sh limpiar'
                    }
                    // Solo zap-resultados/ (reportes redactados, resumen, alcance, cobertura y consola filtrada). Los reportes crudos, los tokens y el
                    // estado viven en volúmenes de Docker de la ejecución y NUNCA se archivan. Siempre existen consola-zap.log y un resumen (mínimo si hace falta).
                    archiveArtifacts artifacts: 'zap-resultados/**', allowEmptyArchive: true
                }
            }
        }

        stage('Deploy UAT (Docker Compose)') {
            when {
                branch 'uat'
            }
            steps {
                withCredentials([
                    file(credentialsId: 'IGUALAB_BACKEND_UAT', variable: 'SECRET_FILE'),
                    file(credentialsId: 'IGUALAB_KEY_ORACLE', variable: 'IGUALAB_KEY_ORACLE_FILE')
                ]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-uat']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
                        '''
                        script { incorporarLlaveOci() }
                        sh '''
                            set -eu
                            docker compose -p "$COMPOSE_PROJECT" down
                            docker compose -p "$COMPOSE_PROJECT" up -d --build
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            sh 'rm -f .env .env.nuevo'
            // Después de archivar (los stages archivan en su propio post) y de retirar los .env: vacía el espacio de trabajo.
            deleteDir()
        }
    }
}

// Incorpora la llave privada OCI (Secret file IGUALAB_KEY_ORACLE, ruta en IGUALAB_KEY_ORACLE_FILE) al .env TEMPORAL como
// OCI_KEY_PEM_B64 (base64 de una sola línea), que el Compose entrega al contenedor con `env_file: .env`. Reemplaza cualquier
// asignación previa de esa variable. La llave y su base64 nunca se imprimen ni viajan como argumentos: `set +x` evita que Jenkins
// (`sh -xe`) muestre las líneas, y el valor solo pasa por tuberías y redirecciones. Se ejecuta DENTRO de withCredentials.
def incorporarLlaveOci() {
    sh '''
        set -eu
        set +x
        umask 077
        [ -n "${IGUALAB_KEY_ORACLE_FILE:-}" ] && [ -s "$IGUALAB_KEY_ORACLE_FILE" ] || { echo 'ERROR: la credencial IGUALAB_KEY_ORACLE no entregó un archivo con contenido.'; exit 1; }
        grep -q -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "$IGUALAB_KEY_ORACLE_FILE" || { echo 'ERROR: IGUALAB_KEY_ORACLE no parece una llave privada PEM.'; exit 1; }
        {
            printf 'OCI_KEY_PEM_B64='
            base64 < "$IGUALAB_KEY_ORACLE_FILE" | tr -d '[:space:]'
            echo
            sed -E '/^[[:space:]]*(export[[:space:]]+)?OCI_KEY_PEM_B64[[:space:]]*=/d' .env
        } > .env.nuevo
        mv .env.nuevo .env
        chmod 600 .env
        [ "$(grep -c -E '^OCI_KEY_PEM_B64=.' .env)" = 1 ] || { echo 'ERROR: el .env debe tener exactamente una asignación de OCI_KEY_PEM_B64.'; exit 1; }
        [ "$(grep -c -E '^[[:space:]]*(export[[:space:]]+)?OCI_KEY_PEM_B64[[:space:]]*=' .env)" = 1 ] || { echo 'ERROR: asignaciones duplicadas de OCI_KEY_PEM_B64.'; exit 1; }
        sed -n 's/^OCI_KEY_PEM_B64=//p' .env | base64 -d | cmp -s - "$IGUALAB_KEY_ORACLE_FILE" || { echo 'ERROR: OCI_KEY_PEM_B64 no reproduce la llave original.'; exit 1; }
        echo 'OCI_KEY_PEM_B64 incorporada al .env temporal (valor no mostrado).'
    '''
}

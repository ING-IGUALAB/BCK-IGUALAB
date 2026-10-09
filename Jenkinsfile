pipeline {
    agent any

    options {
        buildDiscarder(logRotator(numToKeepStr: '5'))
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
                withCredentials([file(credentialsId: 'IGUALAB_BACKEND_DEV', variable: 'SECRET_FILE')]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-development']) {
                        sh '''
set -eu
rm -f .env
cp "$SECRET_FILE" .env
docker compose -p "$COMPOSE_PROJECT" down
docker compose -p "$COMPOSE_PROJECT" up -d --build

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
PY

echo '--- Estado del backend ---'
docker compose -p "$COMPOSE_PROJECT" ps -a

echo '--- Logs de arranque ---'
docker compose -p "$COMPOSE_PROJECT" logs --no-color --tail=200 backend
'''
                    }
                }
            }
        }    

        stage('Deploy QA (Docker Compose)') {
            when {
                branch 'qa'
            }
            steps {
                withCredentials([file(credentialsId: 'IGUALAB_BACKEND_QA', variable: 'SECRET_FILE')]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-qa']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
                            docker compose -p "$COMPOSE_PROJECT" down
                            docker compose -p "$COMPOSE_PROJECT" up -d --build
                        '''
                    }
                }
            }
        }

        stage('Deploy UAT (Docker Compose)') {
            when {
                branch 'uat'
            }
            steps {
                withCredentials([file(credentialsId: 'IGUALAB_BACKEND_UAT', variable: 'SECRET_FILE')]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-uat']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
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
            sh 'rm -f .env'
        }
    }
}
